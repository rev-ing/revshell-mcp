"""Phase 0 acceptance — 소켓 없이 도는 코드."""

from __future__ import annotations

import asyncio
import time
import unittest

from revshell_mcp.session import Buffer, _skip_orphan_continuations, _utf8_safe_end

HAN = "한".encode()  # ed 95 9c
GUK = "국".encode()  # ea b5 ad


class TestUtf8Helpers(unittest.TestCase):
    def test_safe_end_complete(self):
        self.assertEqual(_utf8_safe_end(b"abc"), 3)
        self.assertEqual(_utf8_safe_end(HAN), 3)

    def test_safe_end_trims_partial_tail(self):
        self.assertEqual(_utf8_safe_end(HAN[:1]), 0)
        self.assertEqual(_utf8_safe_end(HAN[:2]), 0)
        self.assertEqual(_utf8_safe_end(b"ab" + HAN[:2]), 2)

    def test_safe_end_leaves_garbage_to_replace(self):
        # 깨진 바이트는 그대로 넘겨 errors="replace" 에 맡긴다
        self.assertEqual(_utf8_safe_end(b"ab\xff"), 3)

    def test_skip_orphan(self):
        self.assertEqual(_skip_orphan_continuations(bytearray(HAN), 1), 3)
        self.assertEqual(_skip_orphan_continuations(bytearray(b"abc"), 1), 1)


class TestBuffer(unittest.IsolatedAsyncioTestCase):
    # [1] 링버퍼가 오버플로 시 앞에서 버리고 커서를 전진시킨다
    async def test_overflow_drops_front_and_advances_cursor(self):
        b = Buffer(max_bytes=10)
        await b.append(b"0123456789")
        self.assertEqual(b.end(), 10)
        await b.append(b"abcde")
        self.assertEqual(b.end(), 15)
        self.assertEqual(b.buffered(), 10)
        data, nxt, truncated, _ = b.read_since(0, 1 << 20)
        self.assertEqual(data, b"56789abcde")
        self.assertEqual(nxt, 15)
        self.assertTrue(truncated)  # 요청 지점이 이미 버려졌다

    # [2] read_since(0) -> read_since(next_since) 반복이 중복·누락 없이 이어진다
    async def test_sequential_reads_are_gapless(self):
        b = Buffer(max_bytes=1 << 20)
        payload = bytes(range(256)) * 40  # 10240 bytes, 비 UTF-8 포함
        for i in range(0, len(payload), 777):
            await b.append(payload[i : i + 777])

        out, since = bytearray(), 0
        while True:
            data, since, truncated, capped = b.read_since(since, 1000)
            self.assertFalse(truncated)
            if not data:
                break
            out.extend(data)
        self.assertEqual(bytes(out), payload)
        self.assertEqual(since, b.end())

    # [3] 한글을 3바이트 중간에서 잘라 두 번에 나눠 append 해도 최종 디코드가 온전하다
    async def test_multibyte_split_across_appends(self):
        b = Buffer(max_bytes=1 << 20)
        await b.append(b"ok:" + HAN[:2])  # 문자 중간에서 끊긴 채 도착
        data, since, _, _ = b.read_since(0, 1 << 20)
        self.assertEqual(data, b"ok:")  # 미완성 꼬리는 아직 내주지 않는다
        self.assertEqual(since, 3)

        await b.append(HAN[2:] + GUK)
        data2, since2, _, _ = b.read_since(since, 1 << 20)
        self.assertEqual(data2, HAN + GUK)
        self.assertEqual((data + data2).decode("utf-8"), "ok:한국")
        self.assertNotIn("�", (data + data2).decode("utf-8", errors="replace"))
        self.assertEqual(since2, b.end())

    # [3b] 4096 청크 경계를 한글 중간에 떨어뜨린 실전형 케이스
    async def test_korean_stream_never_corrupts(self):
        b = Buffer(max_bytes=1 << 20)
        text = ("한국어 출력 테스트 " * 500).encode()
        for i in range(0, len(text), 4096):
            await b.append(text[i : i + 4096])
        out, since = bytearray(), 0
        while True:
            data, since, _, _ = b.read_since(since, 4096)
            if not data:
                break
            # 조각 단위로도 항상 온전히 디코드된다
            data.decode("utf-8")
            out.extend(data)
        self.assertEqual(bytes(out), text)

    # [4] capped=true 일 때 next_since 가 버퍼 끝이 아니라 '반환 지점' 이다
    async def test_capped_next_since_is_return_point(self):
        b = Buffer(max_bytes=1 << 20)
        await b.append(b"x" * 100)
        data, nxt, truncated, capped = b.read_since(0, 10)
        self.assertTrue(capped)
        self.assertFalse(truncated)
        self.assertEqual(len(data), 10)
        self.assertEqual(nxt, 10)  # 100(버퍼 끝)이면 90바이트가 조용히 유실된다
        self.assertNotEqual(nxt, b.end())

        rest, nxt2, _, capped2 = b.read_since(nxt, 1 << 20)
        self.assertFalse(capped2)
        self.assertEqual(len(rest), 90)
        self.assertEqual(nxt2, 100)

    # [4b] cap 이 멀티바이트 중간에 떨어져도 next_since 가 어긋나지 않는다
    async def test_capped_and_utf8_boundary_agree(self):
        b = Buffer(max_bytes=1 << 20)
        await b.append(b"ab" + HAN + GUK)
        data, nxt, _, capped = b.read_since(0, 4)  # 'ab' + 한의 앞 2바이트
        self.assertTrue(capped)
        self.assertEqual(data, b"ab")
        self.assertEqual(nxt, 2)  # 4 가 아니다 — 실제 반환한 바이트 수 기준
        rest, nxt2, _, _ = b.read_since(nxt, 1 << 20)
        self.assertEqual((data + rest).decode("utf-8"), "ab한국")
        self.assertEqual(nxt2, b.end())

    # [5] 오버플로 절단이 멀티바이트 중간에 걸려도 선두에 고아 바이트가 남지 않는다
    async def test_overflow_cut_inside_multibyte(self):
        b = Buffer(max_bytes=4)
        await b.append(b"a" + HAN + GUK)  # 7 bytes -> drop 3, 고아 스킵으로 4
        data, _, _, _ = b.read_since(b.end() - b.buffered(), 1 << 20)
        self.assertEqual(data, GUK)
        self.assertEqual(data.decode("utf-8"), "국")
        self.assertNotIn("�", data.decode("utf-8", errors="replace"))

    # [6] wait_change 가 타임아웃 시 예외 없이 현재 세대를 반환한다
    async def test_wait_change_timeout_returns_gen(self):
        b = Buffer()
        g = b.gen()
        t0 = time.monotonic()
        got = await b.wait_change(g, 0.05)
        self.assertEqual(got, g)
        self.assertGreaterEqual(time.monotonic() - t0, 0.04)

    # [7] 세대 카운터로 동작하며 대기자를 놓치지 않는다
    async def test_wait_change_does_not_miss_writer(self):
        b = Buffer()
        g = b.gen()
        await b.append(b"early")  # 대기에 들어가기 '전' 에 도착한 데이터
        t0 = time.monotonic()
        got = await b.wait_change(g, 5.0)  # 즉시 반환해야 한다
        self.assertNotEqual(got, g)
        self.assertLess(time.monotonic() - t0, 0.5)

    async def test_wait_change_wakes_on_notify(self):
        b = Buffer()
        g = b.gen()

        async def later():
            await asyncio.sleep(0.05)
            await b.append(b"late")

        asyncio.create_task(later())
        t0 = time.monotonic()
        got = await b.wait_change(g, 5.0)
        self.assertNotEqual(got, g)
        self.assertLess(time.monotonic() - t0, 1.0)

    async def test_bump_gen_wakes_waiter(self):
        b = Buffer()
        g = b.gen()

        async def later():
            await asyncio.sleep(0.05)
            await b.bump_gen()

        asyncio.create_task(later())
        got = await b.wait_change(g, 5.0)
        self.assertNotEqual(got, g)

    async def test_read_beyond_end_clamps_to_end(self):
        # 끝을 넘어선 since 는 버퍼 끝으로 클램프한다. 요청값을 그대로 돌려주면
        # 잘못된 커서가 영구히 굳는다 — 자기 교정되는 쪽을 택한다.
        b = Buffer()
        await b.append(b"abc")
        data, nxt, truncated, capped = b.read_since(99, 100)
        self.assertEqual(data, b"")
        self.assertEqual(nxt, b.end())
        self.assertFalse(truncated)
        self.assertFalse(capped)


if __name__ == "__main__":
    unittest.main()
