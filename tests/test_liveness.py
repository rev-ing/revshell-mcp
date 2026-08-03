"""§4.10 liveness · §4.11 자동 재무장 · §5.1 백그라운드 실행.

실제 운용에서 나온 실패 양상을 그대로 재현하는 것이 목적이다:
half-open TCP 에서 session_list 가 active 를 유지하고, session_exec 은
"아직 실행 중일 수 있으니 계속 읽어보라"는 힌트를 돌려주어 호출자를
타임아웃 루프에 가두었다.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from revshell_mcp import executor, manager
from revshell_mcp.executor import _FrameScanner, _timeout_hint
from revshell_mcp.manager import Config, SessionManager
from revshell_mcp.session import Session

from .mock_target import MockTarget, free_port
from .test_manager import wait_for


class _Base(unittest.IsolatedAsyncioTestCase):
    """감시 루프 상수를 테스트 속도에 맞춰 줄인다. 반드시 원복할 것 —
    모듈 전역이라 안 되돌리면 뒤따르는 테스트 파일까지 영향을 받는다."""

    heartbeat_sec = 0.0
    rearm_ok = False

    async def asyncSetUp(self):
        self._saved = (manager.SUPERVISOR_TICK, manager.HEARTBEAT_TIMEOUT)
        manager.SUPERVISOR_TICK = 0.05
        manager.HEARTBEAT_TIMEOUT = 0.4
        self.port = free_port()
        self.mgr = SessionManager(Config(heartbeat_sec=self.heartbeat_sec))
        self.targets: list[MockTarget] = []

    async def asyncTearDown(self):
        for t in self.targets:
            await t.stop()
        await self.mgr.close_all()
        manager.SUPERVISOR_TICK, manager.HEARTBEAT_TIMEOUT = self._saved

    async def _target(self, mode="pipe") -> MockTarget:
        t = await MockTarget(self.port, mode).start()
        self.targets.append(t)
        return t

    async def _session(self, **kw) -> Session:
        await self.mgr.listener_start(self.port, accept=0, **kw)
        await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        sess = next(iter(self.mgr.sessions.values()))
        await wait_for(lambda: sess.has_pty is not None or sess.state == "dead")
        return sess


# ---------------------------------------------------------------------------
# §4.10 last_rx 와 stale
# ---------------------------------------------------------------------------


class LastRxTest(_Base):
    async def test_summary_exposes_last_rx_sec(self):
        sess = await self._session()
        row = sess.summary()
        self.assertIn("last_rx_sec", row)
        # PTY 판별 프로브가 방금 응답했으므로 last_rx 는 age 보다 훨씬 작다
        self.assertLess(row["last_rx_sec"], row["age_sec"] + 0.001)

    async def test_last_rx_tracks_reception_not_creation(self):
        sess = await self._session()
        sess.last_rx = time.time() - 3600  # 한 시간 전에 마지막 수신
        self.assertGreater(sess.summary()["last_rx_sec"], 3599)
        await executor.execute(sess, "echo hi", timeout=5)
        # 응답이 왔으므로 pump 의 mark_rx 가 갱신했다
        self.assertLess(sess.summary()["last_rx_sec"], 5)

    async def test_mark_rx_clears_stale(self):
        sess = await self._session()
        sess.state = "stale"
        sess.mark_rx()
        # stale 은 추정일 뿐이므로 바이트 한 번에 뒤집힌다 — 회복을 프로브 성공에만
        # 의존시키면 늦게 도착한 출력에 대응하지 못한다
        self.assertEqual(sess.state, "active")

    async def test_mark_rx_does_not_resurrect_dead(self):
        sess = await self._session()
        sess.state = "dead"
        sess.mark_rx()
        self.assertEqual(sess.state, "dead")


class HeartbeatTest(_Base):
    heartbeat_sec = 0.15

    async def test_half_open_session_becomes_stale(self):
        """관측된 실패 양상 그 자체 — 소켓은 ESTAB 인데 응답이 없다."""
        sess = await self._session()
        await self.targets[0].freeze()
        ok = await wait_for(lambda: sess.state == "stale", timeout=6)
        self.assertTrue(ok, "half-open 세션이 stale 로 안 넘어갔다")
        # dead 로 올리지 않는다 — 소켓은 실제로 열려 있고 회복 가능성이 있다
        self.assertEqual(sess.state, "stale")
        row = sess.summary()
        self.assertEqual(row["state"], "stale")
        self.assertGreater(row["last_rx_sec"], 0.1)

    async def test_session_list_reports_stale(self):
        sess = await self._session()
        await self.targets[0].freeze()
        await wait_for(lambda: sess.state == "stale", timeout=6)
        rows = self.mgr.session_list()["sessions"]
        self.assertEqual([r["state"] for r in rows], ["stale"])

    async def test_live_session_never_goes_stale(self):
        """오탐 방어. 살아있는 세션에 프로브가 여러 번 도는 동안 active 여야 한다."""
        sess = await self._session()
        await asyncio.sleep(0.5)
        self.assertEqual(sess.state, "active")
        self.assertGreater(sess.buffer.end(), 0)

    async def test_heartbeat_skips_busy_session(self):
        """실행 중인 명령에 프로브를 끼워 넣으면 프레임이 오염되고 stale 오판이 난다."""
        sess = await self._session()
        task = asyncio.create_task(executor.execute(sess, "sleep 0.8", timeout=6))
        await asyncio.sleep(0.45)  # 하트비트 주기(0.15s)를 여러 번 넘긴다
        self.assertEqual(sess.state, "active")
        r = await task
        self.assertTrue(r["complete"], r)
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["output"].strip(), "")

    async def test_dead_session_is_not_probed(self):
        sess = await self._session()
        await self.targets[0].drop()
        await wait_for(lambda: sess.state == "dead", timeout=5)
        await asyncio.sleep(0.4)
        self.assertEqual(sess.state, "dead")  # stale 로 덮어쓰지 않는다


class HeartbeatDisabledTest(_Base):
    heartbeat_sec = 0.0

    async def test_no_probe_when_disabled(self):
        sess = await self._session()
        await self.targets[0].freeze()
        await asyncio.sleep(0.4)
        # --heartbeat-sec 0 이면 판정하지 않는다. 예전 동작 그대로다.
        self.assertEqual(sess.state, "active")


# ---------------------------------------------------------------------------
# §4.10 힌트 — 죽은 세션에 "계속 폴링하라"고 하지 않는다
# ---------------------------------------------------------------------------


class _NullWriter:
    def close(self):
        pass


def _fake_session(state="active", idle=0.0) -> Session:
    s = Session(
        id="sess_hint", reader=None, writer=_NullWriter(), peer=("127.0.0.1", 1)
    )
    s.state = state
    s.last_rx = time.time() - idle
    return s


class TimeoutHintTest(unittest.TestCase):
    def _hint(self, state="active", idle=0.0, s_at=-1, truncated=False, cmd="ls"):
        sc = _FrameScanner(b"__S0__", b"__E0__")
        sc.s_at = s_at
        return _timeout_hint(_fake_session(state, idle), cmd, sc, truncated)

    def test_stale_session_says_half_open(self):
        h = self._hint(state="stale", idle=300)
        self.assertIn("half-open", h)
        self.assertIn("session_close", h)
        # 예전 힌트가 호출자를 가둔 문구다. 남아 있으면 안 된다.
        self.assertNotIn("may still be running", h)

    def test_long_silence_without_start_marker_is_suspicious(self):
        h = self._hint(idle=executor.LIVENESS_SUSPECT + 10, s_at=-1)
        self.assertIn("wedged", h)
        self.assertNotIn("may still be running", h)

    def test_fresh_session_still_gets_the_ordinary_hint(self):
        # 오탐 방어: 방금 응답한 세션의 느린 명령은 여전히 '진행 중' 이다
        h = self._hint(idle=1.0, s_at=0)
        self.assertIn("may still be running", h)

    def test_started_command_on_quiet_session_is_not_flagged(self):
        # 시작 마커가 왔다 = 셸이 payload 를 처리했다. 링크는 살아 있다.
        h = self._hint(idle=executor.LIVENESS_SUSPECT + 10, s_at=0)
        self.assertIn("may still be running", h)

    def test_truncation_still_wins(self):
        h = self._hint(idle=999, truncated=True, s_at=-1)
        self.assertIn("too large", h)

    def test_interactive_hint_survives(self):
        self.assertIn("interactive", self._hint(cmd="vim /etc/hosts", s_at=0))


# ---------------------------------------------------------------------------
# §4.11 자동 재무장
# ---------------------------------------------------------------------------


class RearmTest(_Base):
    heartbeat_sec = 0.0  # 재무장은 하트비트와 독립적으로 돌아야 한다

    async def test_listener_reopens_after_session_dies(self):
        await self.mgr.listener_start(self.port, accept=1, rearm=True)
        t = await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        # accept=1 을 채웠으므로 리스너는 스스로 닫혔다 (§4.9)
        await wait_for(lambda: self.mgr.listener is None)

        await t.drop()
        await wait_for(lambda: self.mgr.listener is not None, timeout=5)
        self.assertIsNotNone(self.mgr.listener, "세션 상실 후 재무장되지 않았다")

        # 실제로 다시 붙을 수 있어야 의미가 있다
        await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 2, timeout=5)
        self.assertEqual(len(self.mgr.sessions), 2)

    async def test_rearm_fires_once_per_session(self):
        """세션 하나가 재무장을 반복 유발하면 임플란트 재시도 루프와 함께 폭주한다."""
        await self.mgr.listener_start(self.port, accept=1, rearm=True)
        t = await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        await t.drop()
        await wait_for(lambda: self.mgr.listener is not None, timeout=5)
        remaining = self.mgr.listener.remaining
        await asyncio.sleep(0.4)  # 여러 tick 을 흘려보낸다
        self.assertEqual(self.mgr.listener.remaining, remaining)

    async def test_rearm_covers_session_close(self):
        sess = await self._session(rearm=True)
        await self.mgr.listener_stop()  # 재무장 의도까지 꺼진다
        await self.mgr.listener_start(self.port, accept=1, rearm=True)
        await wait_for(lambda: self.mgr.listener is not None)
        await self.mgr.session_close(sess.id)
        await wait_for(lambda: self.mgr.listener is not None, timeout=5)
        self.assertIsNotNone(self.mgr.listener)

    async def test_listener_stop_cancels_rearm(self):
        await self.mgr.listener_start(self.port, accept=1, rearm=True)
        t = await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        await self.mgr.listener_stop()
        self.assertIsNone(self.mgr.session_list()["rearm"])

        await t.drop()
        await asyncio.sleep(0.4)
        # 명시적으로 닫았는데 감시 루프가 다시 열면 툴이 하는 말과 반대가 된다
        self.assertIsNone(self.mgr.listener)

    async def test_no_rearm_without_the_flag(self):
        await self.mgr.listener_start(self.port, accept=1)
        t = await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        await t.drop()
        await asyncio.sleep(0.4)
        self.assertIsNone(self.mgr.listener)

    async def test_session_list_exposes_rearm_intent(self):
        await self.mgr.listener_start(self.port, accept=1, rearm=True)
        r = self.mgr.session_list()
        # 재무장을 기다리는 동안은 listener 가 null 이므로 밖에 있어야 보인다
        self.assertEqual(r["rearm"], {"host": "127.0.0.1", "port": self.port})

    async def test_auto_rearm_does_not_clear_the_intent(self):
        """자동 재무장은 rearm=False 로 listener_start 를 부른다. 그게 의도를
        지우면 두 번째 상실부터 복구가 멎는다."""
        await self.mgr.listener_start(self.port, accept=1, rearm=True)
        t1 = await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 1)
        await t1.drop()
        await wait_for(lambda: self.mgr.listener is not None, timeout=5)

        t2 = await self._target()
        await wait_for(lambda: len(self.mgr.sessions) == 2, timeout=5)
        await wait_for(lambda: self.mgr.listener is None, timeout=5)
        await t2.drop()
        await wait_for(lambda: self.mgr.listener is not None, timeout=5)
        self.assertIsNotNone(self.mgr.listener, "두 번째 상실에서 재무장이 멎었다")


# ---------------------------------------------------------------------------
# §5.1 백그라운드 실행
# ---------------------------------------------------------------------------


class BackgroundExecTest(_Base):
    heartbeat_sec = 0.0  # 프로브와의 락 경합으로 flaky 해지지 않게 끈다

    async def asyncTearDown(self):
        # 띄워둔 백그라운드 잡을 거둔다. 살려두면 셸의 stdout 파이프를 계속
        # 붙들어 mock target 종료가 매번 kill_shell 의 2초 상한까지 기다린다.
        for sess in list(self.mgr.sessions.values()):
            if sess.state == "active":
                try:
                    await executor.execute(sess, "kill $! 2>/dev/null; true", timeout=3)
                except Exception:
                    pass
        await super().asyncTearDown()

    async def test_trailing_ampersand_returns_at_once(self):
        sess = await self._session()
        t0 = time.monotonic()
        r = await executor.execute(sess, "sleep 3 &", timeout=6)
        elapsed = time.monotonic() - t0
        self.assertTrue(r["complete"], r)
        self.assertLess(elapsed, 3, "백그라운드인데 명령이 끝나기를 기다렸다")
        # POSIX 는 비동기 리스트의 종료 상태를 0 으로 정의한다. 그 0 은 결과가
        # 아니라 기동 사실일 뿐이므로 성공으로 오독되지 않게 지운다.
        self.assertIsNone(r["exit_code"])
        self.assertIn("background", r["hint"])

    async def test_shell_is_not_blocked_afterwards(self):
        sess = await self._session()
        await executor.execute(sess, "sleep 3 &", timeout=6)
        r = await executor.execute(sess, "echo alive", timeout=6)
        self.assertTrue(r["complete"], r)
        self.assertEqual(r["output"].strip(), "alive")
        self.assertEqual(r["exit_code"], 0)

    async def test_pid_is_recoverable(self):
        """힌트가 안내하는 회수 경로가 실제로 동작하는지 확인한다."""
        sess = await self._session()
        await executor.execute(sess, "sleep 3 &", timeout=6)
        r = await executor.execute(sess, "echo $!", timeout=6)
        self.assertTrue(r["complete"], r)
        self.assertGreater(int(r["output"].strip()), 0)

    async def test_redirected_background_job_keeps_output_out_of_band(self):
        sess = await self._session()
        r = await executor.execute(
            sess, "echo hello >/tmp/revshell_bg_test 2>&1 &", timeout=6
        )
        self.assertTrue(r["complete"], r)
        self.assertEqual(r["output"].strip(), "")
        r2 = await executor.execute(
            sess, "sleep 0.2; cat /tmp/revshell_bg_test", timeout=6
        )
        self.assertEqual(r2["output"].strip(), "hello")
        await executor.execute(sess, "rm -f /tmp/revshell_bg_test", timeout=6)

    async def test_ampersand_does_not_break_framing(self):
        """`&` 뒤에 `;` 를 붙이면 parse error 라 마커가 아예 안 나온다.
        조립이 틀리면 이 테스트가 타임아웃으로 잡는다."""
        sess = await self._session()
        r = await executor.execute(sess, "sleep 3 &", timeout=6)
        self.assertTrue(r["complete"], r)
        self.assertNotIn("__S", r["output"])
        self.assertNotIn("__E", r["output"])
        self.assertNotIn("syntax", r["output"].lower())


if __name__ == "__main__":
    unittest.main()
