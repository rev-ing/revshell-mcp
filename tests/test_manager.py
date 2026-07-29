"""Phase 1 acceptance — 왕복 성립."""

from __future__ import annotations

import asyncio
import time
import unittest

from revshell_mcp.manager import Config, SessionManager
from revshell_mcp.session import Session

from .mock_target import MockTarget, free_port


async def wait_for(pred, timeout=5.0, tick=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        await asyncio.sleep(tick)
    return pred()


class _FakeReader:
    def __init__(self, exc):
        self.exc = exc

    async def read(self, n):
        await asyncio.sleep(0)
        raise self.exc


class _FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = free_port()
        self.mgr = SessionManager(Config())
        self.targets: list[MockTarget] = []

    async def asyncTearDown(self):
        for t in self.targets:
            await t.stop()
        await self.mgr.close_all()

    async def _target(self, mode="pipe") -> MockTarget:
        t = await MockTarget(self.port, mode).start()
        self.targets.append(t)
        return t

    async def _one_session(self, mode="pipe") -> Session:
        await self.mgr.listener_start(self.port, accept=0)
        await self._target(mode)
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        self.assertEqual(len(self.mgr.sessions), 1)
        sess = next(iter(self.mgr.sessions.values()))
        # PTY 판별 프로브가 exec_lock 과 버퍼를 건드리므로 끝나기를 기다린다 (§4.8)
        await wait_for(lambda: sess.has_pty is not None or sess.state == "dead")
        return sess

    # [1] listener_start 가 200ms 내 반환한다
    async def test_listener_start_is_immediate(self):
        t0 = time.monotonic()
        r = await self.mgr.listener_start(self.port)
        elapsed = time.monotonic() - t0
        self.assertIn("listener_id", r)
        self.assertLess(elapsed, 0.2)

    # [2] --allow-any-interface 없이 host="0.0.0.0" 이 거부된다
    async def test_bind_gate_rejects_any_interface(self):
        r = await self.mgr.listener_start(self.port, host="0.0.0.0")
        self.assertIn("error", r)
        self.assertIn("--allow-any-interface", r["error"])
        self.assertIsNone(self.mgr.listener)

    async def test_bind_gate_opens_with_flag(self):
        mgr = SessionManager(Config(allow_any_interface=True))
        r = await mgr.listener_start(free_port(), host="0.0.0.0")
        self.assertIn("listener_id", r)
        await mgr.close_all()

    # [3] 같은 포트 재호출은 쿼터를 누적하고, 다른 포트는 거부 (§4.9)
    async def test_listener_requota_and_single(self):
        a = await self.mgr.listener_start(self.port)
        self.assertEqual(a["accept_remaining"], 1)
        b = await self.mgr.listener_start(self.port)
        self.assertEqual(a["listener_id"], b["listener_id"])  # 리스너는 하나
        self.assertEqual(b["accept_remaining"], 2)  # 멱등이 아니라 누적이다
        c = await self.mgr.listener_start(free_port())
        self.assertIn("error", c)
        self.assertIn(str(self.port), c["error"])

    # ---- 수락 쿼터 (§4.9) --------------------------------------------

    async def _connect_n(self, n, gap=0.2):
        """타겟 n 개를 순차 접속시킨다. 닫힌 뒤의 시도는 조용히 무시한다."""
        for _ in range(n):
            try:
                await self._target()
            except OSError:
                pass
            await asyncio.sleep(gap)
        await asyncio.sleep(0.3)

    def _live(self):
        return [s for s in self.mgr.sessions.values() if s.state == "active"]

    # 기본값은 1회 수락 후 자동 중단
    async def test_accept_one_by_default(self):
        r = await self.mgr.listener_start(self.port)
        self.assertEqual(r["accept_remaining"], 1)
        await self._connect_n(3)
        self.assertEqual(len(self._live()), 1)
        self.assertIsNone(self.mgr.listener, "쿼터 소진 후 리스너가 안 닫혔다")
        self.assertIsNone(self.mgr.session_list()["listener"])

    async def test_accept_n(self):
        await self.mgr.listener_start(self.port, accept=2)
        await self._connect_n(4)
        self.assertEqual(len(self._live()), 2)
        self.assertIsNone(self.mgr.listener)

    async def test_accept_zero_is_unlimited(self):
        r = await self.mgr.listener_start(self.port, accept=0)
        self.assertIsNone(r["accept_remaining"])  # null = 무제한
        await self._connect_n(3)
        self.assertEqual(len(self._live()), 3)
        self.assertIsNotNone(self.mgr.listener, "무제한인데 닫혔다")

    async def test_requota_accumulates_across_calls(self):
        # n 번 호출 = n 개 수락
        for _ in range(3):
            await self.mgr.listener_start(self.port)
        self.assertEqual(self.mgr.listener.remaining, 3)
        await self._connect_n(5)
        self.assertEqual(len(self._live()), 3)
        self.assertIsNone(self.mgr.listener)

    # host 만 달라도 거부한다 — 조용히 무시된 채 쿼터만 오르면 안 된다
    async def test_requota_refuses_different_host(self):
        mgr = SessionManager(Config(allow_any_interface=True))
        port = free_port()
        await mgr.listener_start(port, host="127.0.0.1", accept=1)
        r = await mgr.listener_start(port, host="0.0.0.0", accept=2)
        self.assertIn("error", r)
        self.assertIn("bind address", r["error"])
        self.assertEqual(mgr.listener.remaining, 1)  # 쿼터가 오르면 안 된다
        self.assertEqual(mgr.listener.host, "127.0.0.1")  # 바인드도 그대로
        await mgr.close_all()

    async def test_requota_refuses_different_port_message(self):
        await self.mgr.listener_start(self.port)
        r = await self.mgr.listener_start(free_port())
        self.assertIn("error", r)
        self.assertIn("port", r["error"])
        self.assertEqual(self.mgr.listener.remaining, 1)

    async def test_negative_accept_rejected(self):
        r = await self.mgr.listener_start(self.port, accept=-1)
        self.assertIn("error", r)
        self.assertIsNone(self.mgr.listener)

    # 살아있는 세션은 쿼터 소진에도 영향받지 않는다
    async def test_quota_exhaustion_keeps_session(self):
        await self.mgr.listener_start(self.port)
        await self._connect_n(2)
        sess = self._live()[0]
        await wait_for(lambda: sess.has_pty is not None)
        await sess.write(b"echo after-quota\n")
        got, since = "", 0
        for _ in range(10):
            r = await self.mgr.session_read(sess.id, since=since, wait_ms=1000)
            got += r["data"]
            since = r["next_since"]
            if "after-quota" in got:
                break
        self.assertIn("after-quota", got)

    # [4] mock target 접속 후 session_list 에 세션이 나타난다
    async def test_session_appears_in_list(self):
        sess = await self._one_session()
        listing = self.mgr.session_list()["sessions"]
        self.assertEqual(len(listing), 1)
        row = listing[0]
        self.assertEqual(row["id"], sess.id)
        self.assertEqual(row["state"], "active")
        self.assertFalse(row["busy"])
        self.assertFalse(row["write_stalled"])
        self.assertIn("127.0.0.1", row["peer"])
        for key in ("age_sec", "end_cursor", "buffered_bytes"):
            self.assertIn(key, row)

    # [5] 4개 동시 접속이 각각 독립 세션으로 잡힌다
    async def test_four_concurrent_sessions(self):
        await self.mgr.listener_start(self.port, accept=0)
        await asyncio.gather(*(self._target() for _ in range(4)))
        await wait_for(lambda: len(self.mgr.sessions) == 4)
        self.assertEqual(len(self.mgr.sessions), 4)
        self.assertEqual(len({s.id for s in self.mgr.sessions.values()}), 4)
        # 버퍼도 각각 독립이어야 한다
        buffers = {id(s.buffer) for s in self.mgr.sessions.values()}
        self.assertEqual(len(buffers), 4)

    # [6] max_sessions 초과 연결이 거부되고 소켓이 닫힌다
    async def test_max_sessions_rejects_and_closes(self):
        mgr = SessionManager(Config(max_sessions=2))
        port = free_port()
        await mgr.listener_start(port, accept=0)
        conns = []
        for _ in range(2):
            conns.append(await asyncio.open_connection("127.0.0.1", port))
        await wait_for(lambda: len(mgr.sessions) == 2)

        r, w = await asyncio.open_connection("127.0.0.1", port)
        # 거부된 연결은 서버가 즉시 닫으므로 EOF 가 온다
        self.assertEqual(await asyncio.wait_for(r.read(1), 2.0), b"")
        self.assertEqual(len(mgr.sessions), 2)
        w.close()
        for _, cw in conns:
            cw.close()
        await mgr.close_all()

    # [7] dead 세션으로 상한을 채운 뒤에도 새 연결이 수락된다 (퇴출 로직)
    async def test_eviction_of_dead_sessions(self):
        mgr = SessionManager(Config(max_sessions=2))
        port = free_port()
        await mgr.listener_start(port, accept=0)
        for _ in range(2):
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
        await wait_for(lambda: len(mgr.sessions) == 2)
        await wait_for(lambda: all(s.state == "dead" for s in mgr.sessions.values()))

        r, w = await asyncio.open_connection("127.0.0.1", port)
        accepted = await wait_for(
            lambda: any(s.state == "active" for s in mgr.sessions.values())
        )
        self.assertTrue(accepted, "dead 세션이 퇴출되지 않아 새 연결이 거부됐다")
        self.assertEqual(len(mgr.sessions), 2)  # 하나 치우고 하나 받았다
        w.close()
        await mgr.close_all()

    # [8] wait_ms 타임아웃 시 빈 data + 동일 next_since
    async def test_read_wait_timeout_returns_same_cursor(self):
        sess = await self._one_session()
        await wait_for(lambda: sess.buffer.end() > 0, timeout=0.3)  # 있으면 흡수
        cur = sess.buffer.end()
        t0 = time.monotonic()
        r = await self.mgr.session_read(sess.id, since=cur, wait_ms=500)
        elapsed = time.monotonic() - t0
        self.assertEqual(r["data"], "")
        self.assertEqual(r["next_since"], cur)
        self.assertEqual(r["state"], "active")
        self.assertFalse(r["truncated"])
        self.assertFalse(r["capped"])
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertLess(elapsed, 1.5)

    async def test_read_returns_immediately_when_data_present(self):
        sess = await self._one_session()
        # 프로브(§4.8) 프레임이 버퍼 앞에 이미 들어 있으므로 그 뒤부터 읽는다
        cur = sess.buffer.end()
        await sess.write(b"echo hello\n")
        got, since = "", cur
        for _ in range(10):
            r = await self.mgr.session_read(sess.id, since=since, wait_ms=1000)
            got += r["data"]
            since = r["next_since"]
            if "hello" in got:
                break
        self.assertIn("hello", got)
        self.assertGreater(since, cur)

    # [9] 타겟이 죽으면 wait_ms 가 남아 있어도 즉시 반환하고 state: dead
    async def test_dead_target_returns_early(self):
        sess = await self._one_session()
        target = self.targets[0]
        cur = sess.buffer.end()
        task = asyncio.create_task(
            self.mgr.session_read(sess.id, since=cur, wait_ms=5000)
        )
        await asyncio.sleep(0.05)
        await target.kill_shell()
        t0 = time.monotonic()
        r = await asyncio.wait_for(task, 3.0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(r["state"], "dead")
        self.assertEqual(sess.state, "dead")

    # [10] 펌프에서 임의 예외(OSError 주입)가 나도 state: dead 로 전이한다
    async def test_pump_survives_arbitrary_exception(self):
        w = _FakeWriter()
        sess = Session(
            id="sess_test",
            reader=_FakeReader(OSError(110, "ETIMEDOUT")),
            writer=w,
            peer=("10.0.0.1", 1234),
        )
        g0 = sess.buffer.gen()
        await sess.pump()  # 예외가 밖으로 새면 안 된다
        self.assertEqual(sess.state, "dead")
        self.assertTrue(w.closed)
        self.assertNotEqual(sess.buffer.gen(), g0)  # 대기자 통지도 나갔다

    async def test_pump_reraises_cancellation(self):
        w = _FakeWriter()
        sess = Session(
            id="sess_c",
            reader=_FakeReader(asyncio.CancelledError()),
            writer=w,
            peer=("10.0.0.1", 1),
        )
        with self.assertRaises(asyncio.CancelledError):
            await sess.pump()
        self.assertEqual(sess.state, "dead")
        self.assertTrue(w.closed)

    # [11] pump_task.cancel() 후에도 대기 중인 session_read 가 풀린다
    async def test_cancelled_pump_releases_waiter(self):
        sess = await self._one_session()
        cur = sess.buffer.end()
        task = asyncio.create_task(
            self.mgr.session_read(sess.id, since=cur, wait_ms=5000)
        )
        await asyncio.sleep(0.05)
        sess.pump_task.cancel()
        t0 = time.monotonic()
        r = await asyncio.wait_for(task, 3.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(r["state"], "dead")
        # 취소 경로의 분리 통지(_notify_detached)가 동작하면 즉시 풀린다
        self.assertLess(elapsed, 2.0)

    # [12] 1 MiB 초과 출력 시 truncated: true
    async def test_ring_overflow_reports_truncated(self):
        sess = await self._one_session()
        self.assertEqual(sess.buffer._max, 1 << 20)
        await sess.write(b"head -c 1300000 /dev/zero | tr '\\0' a\n")
        ok = await wait_for(lambda: sess.buffer.end() > (1 << 20) + 4096, timeout=20.0)
        self.assertTrue(ok, f"출력이 부족하다: end={sess.buffer.end()}")
        r = await self.mgr.session_read(sess.id, since=0, max_bytes=1 << 20)
        self.assertTrue(r["truncated"])

    async def test_unknown_session_is_an_error_not_a_crash(self):
        r = await self.mgr.session_read("sess_nope")
        self.assertIn("error", r)
        r = await self.mgr.session_close("sess_nope")
        self.assertIn("error", r)

    # ---- has_pty 판별 (§4.8) -----------------------------------------

    async def test_has_pty_false_on_pipe_session(self):
        sess = await self._one_session("pipe")
        self.assertIs(sess.has_pty, False)
        self.assertIs(self.mgr.session_list()["sessions"][0]["has_pty"], False)

    async def test_has_pty_true_on_pty_session(self):
        sess = await self._one_session("pty")
        self.assertIs(sess.has_pty, True)
        self.assertIs(self.mgr.session_list()["sessions"][0]["has_pty"], True)

    async def test_has_pty_starts_unknown(self):
        # 판별 전에는 None 이다. False("PTY 없음")와 구분되어야 한다.
        from revshell_mcp.session import Session as _S

        self.assertIsNone(_S(id="x", reader=None, writer=None, peer=("h", 1)).has_pty)

    async def test_probe_failure_leaves_has_pty_none(self):
        """판별에 실패해도 세션은 유효해야 한다 — has_pty 는 None 으로 남는다."""
        await self.mgr.listener_start(self.port)
        r, w = await asyncio.open_connection("127.0.0.1", self.port)  # 셸이 없는 연결
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        sess = next(iter(self.mgr.sessions.values()))
        await asyncio.sleep(0.2)
        self.assertIsNone(sess.has_pty)
        self.assertEqual(sess.state, "active")  # 세션 자체는 멀쩡하다
        w.close()

    # ---- listener_stop (§4.7) ----------------------------------------

    async def test_listener_stop_refuses_new_connections(self):
        await self.mgr.listener_start(self.port)
        r = await self.mgr.listener_stop()
        self.assertTrue(r["stopped"])
        self.assertEqual(r["port"], self.port)
        self.assertIsNone(self.mgr.listener)
        with self.assertRaises((ConnectionRefusedError, OSError)):
            await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.port), 2.0
            )

    # 핵심 보장: 리스너를 닫아도 붙어 있는 셸은 살아 있다
    async def test_listener_stop_keeps_existing_sessions(self):
        sess = await self._one_session()
        await self.mgr.listener_stop()

        self.assertEqual(sess.state, "active")
        await sess.write(b"echo still-here\n")
        got = ""
        since = 0
        for _ in range(10):
            r = await self.mgr.session_read(sess.id, since=since, wait_ms=1000)
            got += r["data"]
            since = r["next_since"]
            if "still-here" in got:
                break
        self.assertIn("still-here", got)  # 양방향 I/O 가 그대로 산다
        self.assertEqual(self.mgr.session_list()["sessions"][0]["state"], "active")

    async def test_listener_stop_allows_port_change(self):
        await self.mgr.listener_start(self.port)
        other = free_port()
        # 닫기 전에는 다른 포트가 거부된다
        self.assertIn("error", await self.mgr.listener_start(other))
        await self.mgr.listener_stop()
        r = await self.mgr.listener_start(other)
        self.assertIn("listener_id", r)
        self.assertEqual(r["port"], other)

    async def test_listener_stop_is_idempotent(self):
        await self.mgr.listener_start(self.port)
        self.assertTrue((await self.mgr.listener_stop())["stopped"])
        r = await self.mgr.listener_stop()
        self.assertFalse(r["stopped"])
        self.assertIsNone(r["listener_id"])

    # ---- session_close (§4.6) ----------------------------------------

    # 연결만 끊고 버퍼는 남긴다
    async def test_close_kills_connection_but_keeps_buffer(self):
        sess = await self._one_session()
        await sess.write(b"echo keepme\n")
        await wait_for(lambda: b"keepme" in bytes(sess.buffer._buf))
        end = sess.buffer.end()

        r = await self.mgr.session_close(sess.id)
        self.assertEqual(r["state"], "dead")
        self.assertFalse(r["already_dead"])
        self.assertGreaterEqual(r["end_cursor"], end)
        self.assertEqual(sess.state, "dead")

        # 닫은 뒤에도 마지막 출력을 회수할 수 있어야 한다
        read = await self.mgr.session_read(sess.id, since=0)
        self.assertIn("keepme", read["data"])
        self.assertEqual(read["state"], "dead")
        self.assertIn(sess.id, self.mgr.sessions)  # 기록은 남는다

    async def test_close_is_idempotent(self):
        sess = await self._one_session()
        r1 = await self.mgr.session_close(sess.id)
        r2 = await self.mgr.session_close(sess.id)
        self.assertFalse(r1["already_dead"])
        self.assertTrue(r2["already_dead"])
        self.assertEqual(r2["state"], "dead")

    async def test_close_releases_waiting_read(self):
        sess = await self._one_session()
        cur = sess.buffer.end()
        task = asyncio.create_task(
            self.mgr.session_read(sess.id, since=cur, wait_ms=5000)
        )
        await asyncio.sleep(0.05)
        await self.mgr.session_close(sess.id)
        t0 = time.monotonic()
        r = await asyncio.wait_for(task, 3.0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(r["state"], "dead")

    # 살아있는 세션으로 상한이 찼을 때 close 가 유일한 탈출구다
    async def test_close_frees_a_slot(self):
        mgr = SessionManager(Config(max_sessions=2))
        port = free_port()
        await mgr.listener_start(port, accept=0)
        conns = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
        await wait_for(lambda: len(mgr.sessions) == 2)
        self.assertTrue(all(s.state == "active" for s in mgr.sessions.values()))

        # dead 가 하나도 없으므로 새 연결은 거부된다
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(await asyncio.wait_for(r.read(1), 2.0), b"")

        victim = next(iter(mgr.sessions))
        await mgr.session_close(victim)

        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        freed = await wait_for(lambda: victim not in mgr.sessions)
        self.assertTrue(freed, "close 한 세션이 회수되지 않아 새 연결이 거부됐다")
        self.assertEqual(len(mgr.sessions), 2)
        # 퇴출된 자리에 새 세션이 들어왔다
        fresh = [s for s in mgr.sessions.values() if s.state == "active"]
        self.assertEqual(len(fresh), 2)
        for _, cw in conns:
            cw.close()
        w.close()
        w2.close()
        await mgr.close_all()


if __name__ == "__main__":
    unittest.main()
