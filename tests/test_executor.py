"""Phase 2 acceptance — 명령 실행."""

from __future__ import annotations

import asyncio
import time
import unittest

from revshell_mcp import executor
from revshell_mcp.executor import (
    _cap_body,
    _FrameScanner,
    _scan_shell,
    _validate_all,
    execute,
    session_write,
)
from revshell_mcp.manager import Config, SessionManager
from revshell_mcp.session import Buffer

from .mock_target import MockTarget, free_port
from .test_manager import wait_for

S = b"__Sdeadbeefcafe__"
E = b"__Edeadbeefcafe__"


def drive(chunks, max_bytes=65536, s=S, e=E):
    """§5.2 의 루프 순서(feed 먼저, compact 나중)를 그대로 재현한다."""
    sc = _FrameScanner(s, e)
    frame = bytearray()
    capped = False
    for chunk in chunks:
        frame.extend(chunk)
        done = sc.feed(frame)
        if done is not None:
            return done, capped, sc
        capped |= sc.compact(frame, max_bytes)
    return None, capped, sc


# ---------------------------------------------------------------------------
# §5.1 입력 검증 — 순수 함수
# ---------------------------------------------------------------------------


class ValidationTest(unittest.TestCase):
    # [12] 거부되어야 하는 것들
    def test_rejects(self):
        # 기대값은 영어 메시지의 부분 문자열이다 (§2.3 언어 규칙)
        cases = {
            "": "empty",
            "   ": "empty",
            "ls;": "operator",
            "ls \\": "escape",
            "ls &": "operator",
            "ls &  ": "operator",  # strip 이후에 검사해야 잡힌다
            "echo hi >": "operator",
            "echo hi >>": "operator",
            "cat a |": "operator",
            "a && ": "operator",
            'echo "hi': "quote",
            "echo 'hi": "quote",
            "ls # 확인": "comment",
            "# 그냥 주석": "comment",
            "ls -la ; # 확인": "comment",
            "echo a\nb": "newline",
        }
        for cmd, expect in cases.items():
            with self.subTest(cmd=cmd):
                err = _validate_all(cmd, confirm=False)
                self.assertIsNotNone(err, f"{cmd!r} 가 통과했다")
                self.assertIn(expect, err)

    # [13] 통과해야 하는 것들 — 오거부 회귀 테스트
    def test_accepts(self):
        cases = [
            "find . -exec ls {} \\;",  # 이스케이프된 ; 는 연산자가 아니다
            "find . -name '*.log' -exec rm {} \\;",
            'echo "it\'s fine"',  # " 안의 홀수 개 ' 는 정상
            'grep "don\'t" f',
            "echo 'a#b'",  # 따옴표 안의 # 는 주석이 아니다
            "echo a#b",  # 단어 중간의 # 도 주석이 아니다
            "echo 'ls &'",  # 따옴표 안에서 끝나는 연산자
            "id",
            "ls -la /tmp",
            "cat /etc/passwd | head -3",
            "rm -rf /tmp/build",  # 루트가 아니므로 게이트에 안 걸린다
            'echo "a\\"b"',  # " 안의 이스케이프된 "
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                self.assertIsNone(_validate_all(cmd, confirm=False), f"{cmd!r} 가 거부됐다")

    # [11] 파괴적 명령 게이트 — 좁게 잡는다
    def test_destructive_gate(self):
        blocked = ["rm -rf /", "rm -rf /*", "rm -rf / --no-preserve-root",
                   "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda",
                   "shutdown -h now", "reboot", ":(){ :|:& };:"]
        for cmd in blocked:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(_validate_all(cmd, confirm=True) or None
                                     if False else _validate_all(cmd, confirm=False))
        # confirm=True 면 통과한다
        self.assertIsNone(_validate_all("rm -rf /", confirm=True))
        # 오탐이 게이트 자체보다 위험하다 — /tmp 는 통과
        self.assertIsNone(_validate_all("rm -rf /tmp/build", confirm=False))
        self.assertIsNone(_validate_all("rm -rf ./build", confirm=False))

    def test_mask_preserves_length(self):
        # 마스크 길이가 원문과 1:1 이어야 out[-1] 기준 주석 판정이 어긋나지 않는다
        for cmd in ["echo 'a b' c", 'x "y\\"z" w', "a\\ b", "plain"]:
            err, mask = _scan_shell(cmd)
            self.assertIsNone(err, cmd)
            self.assertEqual(len(mask), len(cmd), cmd)

    def test_escaped_space_is_not_word_boundary(self):
        # echo a\ #b 는 '한 단어' 이므로 # 는 주석이 아니다
        self.assertIsNone(_validate_all("echo a\\ #b", confirm=False))


# ---------------------------------------------------------------------------
# §5.3 / §5.5 스캐너 — 단위
# ---------------------------------------------------------------------------


class ScannerTest(unittest.TestCase):
    # [3] 프레임 전체가 첫 청크에 한 번에 도착해도 정상 완성된다
    def test_whole_frame_in_one_chunk(self):
        blob = b"echo noise\r\n" + S + b"\nhello\n" + E + b"0\n"
        done, capped, sc = drive([blob])
        self.assertIsNotNone(done, "compact 가 feed 보다 먼저 돌면 S 마커가 지워진다")
        body, rc = done
        self.assertEqual(body, b"hello\n")
        self.assertEqual(rc, 0)
        self.assertFalse(capped)

    def test_byte_at_a_time(self):
        blob = b"noise" + S + b"\nhi\n" + E + b"3\n"
        done, _, _ = drive([blob[i : i + 1] for i in range(len(blob))])
        self.assertEqual(done, (b"hi\n", 3))

    # [9] S 마커 도착 전 64 KiB 이상의 선행 노이즈
    def test_large_pre_s_noise(self):
        noise = b"garbage line\n" * 6000  # ~78 KiB
        chunks = [noise[i : i + 4096] for i in range(0, len(noise), 4096)]
        chunks.append(S + b"\nok\n" + E + b"0\n")
        done, capped, sc = drive(chunks)
        self.assertEqual(done, (b"ok\n", 0))
        self.assertFalse(capped)  # 선행 노이즈 폐기는 사용자 데이터 손실이 아니다

    def test_pre_s_noise_does_not_grow(self):
        sc = _FrameScanner(S, E)
        frame = bytearray()
        for _ in range(50):
            frame.extend(b"x" * 4096)
            self.assertIsNone(sc.feed(frame))
            sc.compact(frame, 65536)
        self.assertLessEqual(len(frame), len(S))  # 1단계가 오버랩만 남긴다

    # [4-보조] rc 개행 도착 전에는 완성으로 보지 않는다 (§5.4)
    def test_rc_needs_newline(self):
        done, _, _ = drive([S + b"\nx\n" + E + b"1"])  # 개행 없음
        self.assertIsNone(done)
        done, _, _ = drive([S + b"\nx\n" + E + b"12", b"\n"])
        self.assertEqual(done, (b"x\n", 12))  # 두 자리도 온전히

    # [7] 프레임 상한 — 가운데를 버리고 앞뒤를 남긴다.
    #     도착 패턴과 무관하게 상한이 지켜져야 한다.
    def test_output_capped_regardless_of_arrival_pattern(self):
        body = b"".join(b"%06d\n" % i for i in range(20000))  # 140 KB
        blob = S + b"\n" + body + E + b"0\n"
        patterns = {
            "한 청크": [blob],  # feed 가 먼저 완성시켜 compact 가 안 돈다
            "1KB 분할": [blob[i : i + 1024] for i in range(0, len(blob), 1024)],
            "64B 분할": [blob[i : i + 64] for i in range(0, len(blob), 64)],
        }
        for name, chunks in patterns.items():
            with self.subTest(pattern=name):
                done, capped, _ = drive(chunks, max_bytes=4096)
                self.assertIsNotNone(done)
                out, rc = done
                out, hit = _cap_body(out, 4096)  # 반환 직전 상한 (§5.5)
                self.assertEqual(rc, 0)
                self.assertTrue(capped or hit, "상한에 걸렸는데 capped 가 안 섰다")
                self.assertLessEqual(len(out), 4096)
                self.assertTrue(out.startswith(b"000000\n"), out[:20])
                self.assertTrue(out.rstrip().endswith(b"019999"), out[-20:])

    def test_cap_body_keeps_both_ends(self):
        body = b"A" * 100 + b"M" * 1000 + b"Z" * 100
        out, hit = _cap_body(body, 200)
        self.assertTrue(hit)
        self.assertEqual(len(out), 200)
        self.assertEqual(out[:100], b"A" * 100)
        self.assertEqual(out[-100:], b"Z" * 100)
        self.assertEqual(_cap_body(b"short", 200), (b"short", False))

    # [8] _shift 의 e_at 보정 — 통합 테스트로는 도달 불가능하므로 단위로 (§5.3)
    def test_shift_adjusts_e_at(self):
        sc = _FrameScanner(S, E)
        sc.e_at, sc.scan_from = 100, 200
        sc._shift(30)
        self.assertEqual(sc.e_at, 70)
        self.assertEqual(sc.scan_from, 170)

    def test_shift_keeps_frame_consistent_when_stage0_relaxed(self):
        """0단계를 완화한 미래의 코드가 깨지지 않는지 검증한다.

        e_at 이 잡힌 뒤 2단계 압축이 일어나는 상황을 인위적으로 만든다.
        _shift 의 e_at 보정을 빼면 body 와 rc 가 모두 어긋나 실패한다.
        """
        noise = b"echo garbage\r\n"
        frame = bytearray(noise + S + b"\nBODY\n" + E)  # rc 아직 미도착
        sc = _FrameScanner(S, E)
        self.assertIsNone(sc.feed(frame))
        self.assertEqual(sc.s_at, len(noise))
        self.assertGreaterEqual(sc.e_at, 0)

        n = sc.s_at  # 0단계를 우회하고 2단계만 강제 실행
        del frame[:n]
        sc._shift(n)
        sc.s_at = 0

        frame.extend(b"7\n")
        done = sc.feed(frame)
        self.assertEqual(done, (b"BODY\n", 7))

    def test_stale_markers_dropped_from_body(self):
        stale = b"__Saaaaaaaaaaaa__" + b"__Ebbbbbbbbbbbb__"
        done, _, _ = drive([S + b"\nout" + stale + b"put\n" + E + b"0\n"])
        self.assertEqual(done, (b"output\n", 0))

    def test_split_marker_across_chunks(self):
        blob = S + b"\nz\n" + E + b"0\n"
        # 마커 한가운데를 갈라 보낸다
        done, _, _ = drive([blob[:8], blob[8:20], blob[20:]])
        self.assertEqual(done, (b"z\n", 0))


# ---------------------------------------------------------------------------
# 통합 — 실제 셸 상대
# ---------------------------------------------------------------------------


class ExecTest(unittest.IsolatedAsyncioTestCase):
    MODE = "pipe"
    BUFFER = 1 << 20

    async def asyncSetUp(self):
        self.port = free_port()
        self.mgr = SessionManager(Config(buffer_bytes=self.BUFFER))
        await self.mgr.listener_start(self.port)
        self.target = await MockTarget(self.port, self.MODE).start()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        self.sess = next(iter(self.mgr.sessions.values()))
        # PTY 판별 프로브가 exec_lock 을 잡으므로 끝나기를 기다린다 (§4.8)
        await wait_for(lambda: self.sess.has_pty is not None)

    async def asyncTearDown(self):
        await self.target.stop()
        await self.mgr.close_all()

    # [1] session_exec("id") -> output + exit_code 0
    async def test_exec_id(self):
        r = await execute(self.sess, "id", timeout=10)
        self.assertTrue(r["complete"], r)
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("uid=", r["output"])
        self.assertIsNone(r["hint"])
        self.assertFalse(r["truncated"])
        self.assertFalse(r["capped"])
        self.assertGreater(r["next_since"], 0)

    # [2] session_exec("false") -> exit_code 1
    async def test_exec_exit_codes(self):
        self.assertEqual((await execute(self.sess, "false", timeout=10))["exit_code"], 1)
        self.assertEqual((await execute(self.sess, "true", timeout=10))["exit_code"], 0)
        # 서브셸로 낸다 — `exit 42` 를 그대로 보내면 셸 자신이 죽어 세션이 끊긴다
        r = await execute(self.sess, "sh -c 'exit 42'", timeout=10)
        self.assertEqual(r["exit_code"], 42)
        r = await execute(self.sess, "ls /nonexistent-xyz 2>/dev/null", timeout=10)
        self.assertNotEqual(r["exit_code"], 0)

    async def test_exec_korean_output(self):
        r = await execute(self.sess, "echo 한국어 출력 테스트", timeout=10)
        self.assertTrue(r["complete"], r)
        self.assertEqual(r["output"].strip(), "한국어 출력 테스트")
        self.assertNotIn("�", r["output"])

    async def test_output_has_no_markers_or_echo(self):
        r = await execute(self.sess, "echo clean", timeout=10)
        self.assertEqual(r["output"].strip(), "clean")
        self.assertNotIn("__S", r["output"])
        self.assertNotIn("__E", r["output"])
        self.assertNotIn("printf", r["output"])

    # [4] 타임아웃은 실패가 아니다 — 부분 출력과 함께 돌려준다
    async def test_timeout_returns_partial_output(self):
        t0 = time.monotonic()
        r = await execute(self.sess, "echo before; sleep 30", timeout=2.0)
        elapsed = time.monotonic() - t0
        self.assertFalse(r["complete"])
        self.assertIsNone(r["exit_code"])
        self.assertIn("before", r["output"])
        self.assertIsNotNone(r["hint"])
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 4.0)

    async def test_timeout_then_read_continues(self):
        r = await execute(self.sess, "echo A; sleep 1; echo B", timeout=0.3)
        self.assertFalse(r["complete"])
        self.assertIsNotNone(r["next_since"])
        rest = await self.mgr.session_read(
            self.sess.id, since=r["next_since"], wait_ms=3000
        )
        got = rest["data"]
        for _ in range(5):
            if "B" in got:
                break
            rest = await self.mgr.session_read(
                self.sess.id, since=rest["next_since"], wait_ms=1000
            )
            got += rest["data"]
        self.assertIn("B", got)

    # [6] 겹친 exec 은 즉시 실패하고 next_since 는 null
    async def test_overlapping_exec_is_busy(self):
        task = asyncio.create_task(execute(self.sess, "sleep 2", timeout=5))
        await asyncio.sleep(0.15)
        self.assertTrue(self.sess.exec_lock.locked())
        self.assertTrue(self.mgr.session_list()["sessions"][0]["busy"])

        t0 = time.monotonic()
        r = await execute(self.sess, "echo second", timeout=5)
        self.assertLess(time.monotonic() - t0, 0.5)  # 즉시 반환
        self.assertFalse(r["complete"])
        self.assertIsNone(r["next_since"])  # 아무것도 읽지 않았다
        self.assertEqual(r["output"], "")
        self.assertIn("NOT started", r["hint"])
        self.assertIn("retry", r["hint"])
        await task

    # [11]/[12] 거부 경로도 스키마를 전부 채운다
    async def test_rejected_schema_is_complete(self):
        r = await execute(self.sess, "rm -rf /", timeout=5)
        for k in ("session_id", "output", "exit_code", "next_since",
                  "complete", "truncated", "capped", "hint"):
            self.assertIn(k, r)
        self.assertFalse(r["complete"])
        self.assertIsNone(r["next_since"])
        self.assertIn("confirm=true", r["hint"])
        ok = await execute(self.sess, "rm -rf /tmp/nonexistent-xyz", timeout=10)
        self.assertTrue(ok["complete"], ok)

    # [7] seq 1 40000 -> capped, truncated=false, 앞뒤가 모두 남는다
    async def test_large_output_is_capped_not_truncated(self):
        r = await execute(self.sess, "seq 1 40000", timeout=20, max_bytes=65536)
        self.assertTrue(r["complete"], r["hint"])
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["capped"])
        self.assertFalse(r["truncated"])
        self.assertLessEqual(len(r["output"].encode()), 65536)
        # PTY 모드는 ONLCR 로 \n -> \r\n 이 되므로 줄바꿈 중립으로 본다
        norm = r["output"].replace("\r\n", "\n")
        self.assertTrue(norm.startswith("1\n2\n3\n"), norm[:40])
        self.assertIn("40000", norm[-50:])

    # [14] 100k 라인에서 폴링 루프가 O(n^2) 로 퇴화하지 않는다
    async def test_no_quadratic_blowup(self):
        t0 = time.monotonic()
        r = await execute(self.sess, "seq 1 100000", timeout=30, max_bytes=65536)
        elapsed = time.monotonic() - t0
        self.assertTrue(r["complete"], r["hint"])
        self.assertLess(elapsed, 10.0, f"100k 라인에 {elapsed:.1f}s — 스캐너를 의심하라")

    async def test_session_write_warns_during_exec(self):
        task = asyncio.create_task(execute(self.sess, "sleep 1", timeout=5))
        await asyncio.sleep(0.15)
        w = await session_write(self.sess, "echo x")
        self.assertIn("wrote directly to a session with a session_exec in flight",
                      w["warnings"])
        self.assertGreater(w["bytes_written"], 0)
        await task

    async def test_session_write_gate(self):
        w = await session_write(self.sess, "rm -rf /")
        self.assertEqual(w["bytes_written"], 0)
        self.assertTrue(w["warnings"])

    async def test_dead_session_exec_is_incomplete(self):
        await self.target.kill_shell()
        await wait_for(lambda: self.sess.state == "dead")
        r = await execute(self.sess, "id", timeout=5)
        self.assertFalse(r["complete"])
        self.assertIn("closed", r["hint"])


class ExecPtyTest(ExecTest):
    """[5] PTY 모드 — 에코와 PS1 이 살아 있는 환경에서 같은 검증을 반복한다.

    프레이밍 방식이 strip_echo 없이 동작한다는 주장을 증명하는 것이 목적이다.
    """

    MODE = "pty"

    async def test_pty_echo_and_ps1_not_in_output(self):
        r = await execute(self.sess, "echo pty-ok", timeout=10)
        self.assertTrue(r["complete"], r)
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["output"].strip(), "pty-ok")
        self.assertNotIn("echo pty-ok", r["output"])  # 명령어 반향
        self.assertNotIn("mock$", r["output"])  # PS1
        self.assertNotIn("printf", r["output"])

    async def test_pty_echo_really_happens(self):
        """에코가 실제로 일어나는 환경인지 확인한다 — 아니면 위 테스트가 무의미하다."""
        await self.sess.write(b"echo canary-probe\n")
        await wait_for(lambda: b"canary" in bytes(self.sess.buffer._buf), timeout=5)
        raw = bytes(self.sess.buffer._buf).decode(errors="replace")
        self.assertGreaterEqual(raw.count("canary-probe"), 2, "에코가 없다 (PTY 아님)")


# ---------------------------------------------------------------------------
# [10] read_capped 연속 참 — 링버퍼를 키워 잠재 조건을 인위적으로 만든다
# ---------------------------------------------------------------------------


class ReadCappedTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = free_port()
        self.mgr = SessionManager(Config(buffer_bytes=8 << 20))  # 8 MiB
        await self.mgr.listener_start(self.port)
        self.target = await MockTarget(self.port, "pipe").start()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        self.sess = next(iter(self.mgr.sessions.values()))
        # PTY 판별 프로브가 exec_lock 을 잡으므로 끝나기를 기다린다 (§4.8)
        await wait_for(lambda: self.sess.has_pty is not None)
        self._saved = executor.EXEC_READ_MAX
        # 읽기 상한을 낮춰 read_capped 가 연속으로 참이 되게 만든다.
        # 실제 배포값(1 MiB)에서는 링버퍼 상한과 같아 구조적으로 발현하지 않는다.
        executor.EXEC_READ_MAX = 64

    async def asyncTearDown(self):
        executor.EXEC_READ_MAX = self._saved
        await self.target.stop()
        await self.mgr.close_all()

    async def test_returns_within_timeout_while_capped(self):
        t0 = time.monotonic()
        r = await execute(self.sess, "seq 1 3000000", timeout=1.0, max_bytes=65536)
        elapsed = time.monotonic() - t0
        # deadline 검사가 read_capped 분기보다 아래에 있으면 여기서 timeout 을
        # 한참 넘겨 반환한다 (§5.2-3).
        self.assertLess(elapsed, 4.0, f"timeout=1.0 인데 {elapsed:.1f}s 걸렸다")
        self.assertFalse(r["complete"])

    async def test_still_correct_while_capped(self):
        r = await execute(self.sess, "seq 1 500", timeout=15, max_bytes=65536)
        self.assertTrue(r["complete"], r["hint"])
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["output"].startswith("1\n"))
        self.assertIn("500", r["output"][-20:])


if __name__ == "__main__":
    unittest.main()
