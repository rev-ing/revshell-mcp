"""리스너 1개, 세션 dict, 바인드 게이트 (§4).

이 모듈도 MCP 를 import 하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

from .session import DEFAULT_BUFFER_BYTES, Buffer, Session

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
DEFAULT_MAX_SESSIONS = 32
DEFAULT_READ_MAX_BYTES = 65536

# PTY 판별 (§4.8). `test -t 0` 은 셸 내장이라 busybox 최소 rootfs 에도 있고,
# 출력을 파싱할 필요 없이 종료코드만 보면 된다 — tty 면 0, 아니면 1.
PTY_PROBE_CMD = "test -t 0"
PTY_PROBE_TIMEOUT = 5.0

# liveness 하트비트 (§4.10). `:` 는 POSIX 셸 내장 no-op 이라 busybox ash 에도
# 있고 부작용도 출력도 없다 — 프레임 마커 두 줄만 오간다.
LIVENESS_CMD = ":"
HEARTBEAT_TIMEOUT = 10.0
DEFAULT_HEARTBEAT_SEC = 60.0
# 감시 루프 주기. 하트비트 간격과 별개다 — 재무장(§4.11)은 하트비트를 꺼도 돈다.
SUPERVISOR_TICK = 2.0


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(2)}"


@dataclass
class Config:
    allow_any_interface: bool = False  # --allow-any-interface (§4.1)
    max_sessions: int = DEFAULT_MAX_SESSIONS
    buffer_bytes: int = DEFAULT_BUFFER_BYTES
    # 이 시간 동안 한 바이트도 못 받은 유휴 세션에 liveness 프로브를 보낸다.
    # 0 이면 하트비트를 끈다 (§4.10).
    heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC


@dataclass
class Listener:
    id: str
    host: str
    port: int
    server: asyncio.AbstractServer
    # 앞으로 몇 개까지 받을 것인가. None 이면 무제한 (§4.9).
    remaining: int | None = 1


class SessionManager:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.sessions: dict[str, Session] = {}
        self.listener: Listener | None = None
        self._probes: set[asyncio.Task] = set()  # 프로브 태스크 강한 참조
        self._probing: set[str] = set()  # 프로브가 떠 있는 세션 id
        self._supervisor: asyncio.Task | None = None
        # 자동 재무장 대상 (host, port). None 이면 끔 (§4.11).
        self._rearm: tuple[str, int] | None = None

    # ---- 리스너 -------------------------------------------------------

    async def listener_start(
        self,
        port: int,
        host: str = "127.0.0.1",
        accept: int = 1,
        rearm: bool = False,
    ) -> dict:
        """바인드 게이트와 수락 쿼터 (§4.1, §4.9). accept 를 기다리지 않고 즉시 반환한다.

        accept 만큼 세션을 받으면 리스너가 스스로 닫힌다(기본 1). 0 이면 무제한.
        재시도 루프를 도는 임플란트가 붙어 있으면 무제한 리스너는 30초마다 세션을
        하나씩 쌓아 max_sessions 를 포화시키므로, 기본값을 1 로 둔다.

        rearm=True 면 세션을 잃을 때마다 리스너를 자동으로 다시 연다 (§4.11).
        쿼터가 막는 것은 '붙어 있는 동안의 폭주' 이지 '끊긴 뒤의 복구' 가 아닌데,
        그 복구가 수동이라 세션이 끊길 때마다 사람이 재무장해야 했다.
        """
        # 인증 없는 셸 핸들러를 전 인터페이스에 여는 것은 아무나 붙을 수 있다는 뜻이다.
        # host 가 자유 인자이므로 기본값이 루프백인 것만으로는 부족하다.
        if host not in LOOPBACK and not self.cfg.allow_any_interface:
            return {
                "error": f"refused: binding to a non-loopback address ({host}) "
                f"requires restarting the server with --allow-any-interface"
            }
        if accept < 0:
            return {"error": "accept must be >= 0 (0 means unlimited)"}

        if self.listener is not None:
            lsn = self.listener
            if lsn.port == port and lsn.host == host:
                # 같은 포트+host 재호출은 쿼터를 '누적'한다 — n 번 부르면 n 개를 받는다.
                # 멱등이 아니므로, 확신이 없어 다시 부르면 한 개를 더 받게 된다.
                if accept == 0 or lsn.remaining is None:
                    lsn.remaining = None
                else:
                    lsn.remaining += accept
                self._set_rearm(rearm, host, port)
                return self._listener_info()
            # host 만 다른 경우도 거부한다. 포트만 비교하면 요청한 주소가 조용히
            # 무시된 채 쿼터만 오르고, 호출자는 0.0.0.0 에 열렸다고 착각한다.
            what = "port" if lsn.port != port else "bind address"
            return {
                "error": f"already listening on {lsn.host}:{lsn.port}; "
                f"call listener_stop() first to change the {what} "
                f"(existing sessions are not affected)"
            }
        try:
            server = await asyncio.start_server(self._on_connect, host, port)
        except OSError as e:
            return {"error": f"bind failed on {host}:{port} — {e}"}
        self.listener = Listener(
            id=new_id("lsn"), host=host, port=port, server=server,
            remaining=None if accept == 0 else accept,
        )
        self._set_rearm(rearm, host, port)
        self._ensure_supervisor()
        log.info(
            "listening on %s:%d (%s, accept=%s, rearm=%s)",
            host, port, self.listener.id,
            "unlimited" if accept == 0 else accept, rearm,
        )
        return self._listener_info()

    def _set_rearm(self, rearm: bool, host: str, port: int) -> None:
        """요청이 받아들여졌을 때만 재무장 의도를 갱신한다 (§4.11).

        rearm=False 로는 지우지 않는다 — 자동 재무장 자신이 rearm=False 로
        listener_start 를 부르므로, 여기서 지우면 두 번째 상실부터 복구가 멎는다.
        끄는 경로는 listener_stop() 하나뿐이고 그건 명시적 취소다.
        """
        if rearm:
            self._rearm = (host, port)

    async def listener_stop(self) -> dict:
        """리스닝 소켓만 닫고 포트를 반납한다 (§4.7).

        **기존 세션은 건드리지 않는다.** asyncio 의 server.close() 는 리스닝 소켓만
        닫고 이미 accept 된 연결의 transport 는 그대로 두므로, "새 연결을 안 받는다"와
        "붙어 있는 셸을 끊는다"는 완전히 별개다. 세션을 끊는 건 session_close 다.

        멱등하다. 리스너가 없으면 에러 대신 stopped=False 를 돌려준다.

        자동 재무장(§4.11) 의도도 함께 취소한다. "그만 받겠다" 는 명시적 지시인데
        잠시 뒤 감시 루프가 다시 열어버리면 이 툴이 하는 말과 반대로 동작한다.
        리스너가 없어 stopped=False 인 경우에도 의도는 지운다 — 재무장 대기 중인
        상태를 끄는 방법이 달리 없기 때문이다.
        """
        self._rearm = None
        if self.listener is None:
            return {"listener_id": None, "host": None, "port": None, "stopped": False}
        lsn = self.listener
        # await 전에 먼저 비운다 — 대기 중 들어온 listener_start 가 낡은 상태를
        # 보고 "이미 대기 중" 으로 거부하면 안 된다.
        self.listener = None

        # close() 는 리스닝 소켓을 '동기적으로' 닫아 포트를 즉시 반납한다.
        #
        # wait_closed() 를 부르면 안 된다. Python 3.12 부터 이 메서드는 서버가
        # 닫히는 것뿐 아니라 **이미 accept 된 모든 연결이 끝날 때까지** 기다린다.
        # 세션이 하나라도 살아 있으면 영원히 반환하지 않으므로, 이 툴의 핵심 보장
        # ("기존 세션을 건드리지 않는다")과 정면으로 충돌하고 툴 상한도 깨진다.
        lsn.server.close()
        await asyncio.sleep(0)  # 루프가 리스닝 소켓 정리를 처리하도록 한 틱 양보
        log.info("listener %s on %s:%d 중단", lsn.id, lsn.host, lsn.port)
        return {
            "listener_id": lsn.id,
            "host": lsn.host,
            "port": lsn.port,
            "stopped": True,
        }

    def _listener_info(self) -> dict:
        assert self.listener is not None
        return {
            "listener_id": self.listener.id,
            "host": self.listener.host,
            "port": self.listener.port,
            # null 이면 무제한. 0 이 되는 순간 리스너는 이미 닫혀 있다.
            "accept_remaining": self.listener.remaining,
        }

    def _close_listener_socket(self) -> Listener | None:
        """리스닝 소켓만 동기적으로 닫는다. §4.7 의 wait_closed 함정 주석 참조."""
        lsn = self.listener
        if lsn is None:
            return None
        self.listener = None
        lsn.server.close()
        return lsn

    # ---- 연결 수락 ----------------------------------------------------

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """콜백에서 절대 오래 붙잡지 말 것. 태스크만 띄우고 즉시 반환한다 (§4.2)."""
        # 수락 쿼터 (§4.9). asyncio 는 한 번의 이벤트에서 backlog 만큼 연속으로
        # accept 할 수 있으므로, 리스너를 닫는 것만으로는 초과 수락을 막지 못한다.
        # 여기서 한 번 더 세는 것이 실제 방어선이다.
        lsn = self.listener
        if lsn is not None and lsn.remaining is not None:
            if lsn.remaining <= 0:
                writer.close()
                return
            lsn.remaining -= 1
            if lsn.remaining == 0:
                # 마지막 하나를 받았다. 같은 이벤트 루프 틱 안에서 즉시 닫는다.
                self._close_listener_socket()
                log.info("listener %s 수락 쿼터 소진 — 자동 중단", lsn.id)

        if len(self.sessions) >= self.cfg.max_sessions:
            if not self._evict_one_dead():
                writer.close()  # fd 누수 방지
                log.warning("max_sessions 초과, 살아있는 세션뿐이라 거부")
                return
        sess = Session(
            id=new_id("sess"),
            reader=reader,
            writer=writer,
            peer=writer.get_extra_info("peername") or ("?", 0),
            opened_at=time.time(),
            buffer=Buffer(self.cfg.buffer_bytes),
        )
        self.sessions[sess.id] = sess
        sess.pump_task = asyncio.create_task(sess.pump())
        self._ensure_supervisor()
        # 콜백에서 기다리지 않는다. 판별이 끝날 때까지 has_pty 는 None 이다.
        self._spawn_probe(sess, self._probe_pty(sess))
        log.info("session %s from %s", sess.id, sess.peer)

    def _spawn_probe(self, sess: Session, coro) -> None:
        """프로브를 띄우고 강한 참조를 잡는다. 세션당 동시에 하나만 띄운다.

        _probing 가드가 없으면 감시 루프가 매 tick 마다 같은 세션에 프로브를
        쌓는다. 프로브 하나가 최대 HEARTBEAT_TIMEOUT 을 쓰므로 그 사이 tick 이
        여러 번 지나가기 때문이다.
        """
        self._probing.add(sess.id)
        t = asyncio.create_task(coro)
        self._probes.add(t)
        t.add_done_callback(self._probes.discard)
        t.add_done_callback(lambda _: self._probing.discard(sess.id))

    async def _probe_pty(self, sess: Session) -> None:
        """연결 직후 1회만 PTY 여부를 판별한다 (§4.8).

        실패해도 세션은 유효해야 하므로 예외를 밖으로 내지 않는다. 판별 실패 시
        has_pty 는 None 으로 남고, 그건 "PTY 가 없다"가 아니라 "모른다"는 뜻이다.

        exec_lock 을 직접 잡고 run_framed 를 부른다. execute 를 쓰면 락 경합 시
        _busy 를 돌려받는데, 그건 판별 실패와 구분되지 않는다 (§4.10).
        """
        try:
            from . import executor  # 지연 import — executor 는 manager 를 모른다

            async with sess.exec_lock:
                r = await executor.run_framed(
                    sess, PTY_PROBE_CMD, timeout=PTY_PROBE_TIMEOUT
                )
            if r.get("complete") and r.get("exit_code") in (0, 1):
                sess.has_pty = r["exit_code"] == 0
                log.info("session %s has_pty=%s", sess.id, sess.has_pty)
        except Exception:
            log.warning("PTY 판별 실패 %s", sess.id, exc_info=True)

    # ---- 감시 루프 (§4.10, §4.11) ---------------------------------------

    def _ensure_supervisor(self) -> None:
        """감시 루프를 띄운다. 생성자가 아니라 여기서 띄우는 이유는, build(cfg) 가
        이벤트 루프가 돌기 전에 불리기 때문이다."""
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        """유휴 세션 liveness 프로브(§4.10)와 리스너 자동 재무장(§4.11).

        한 번의 예외로 감시가 통째로 멎으면 안 된다 — 그러면 half-open 이
        영원히 active 로 남는, 애초에 고치려던 상태로 조용히 되돌아간다.
        그래서 tick 안쪽만 감싼다. sleep 에서 오는 CancelledError 는 그대로 나간다.
        """
        while True:
            await asyncio.sleep(SUPERVISOR_TICK)
            try:
                await self._rearm_sweep()
                self._heartbeat_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("감시 루프 tick 실패", exc_info=True)

    def _heartbeat_sweep(self) -> None:
        """유휴 세션에 프레임 no-op 을 쏜다 (§4.10).

        exec_lock 이 잡혀 있으면 건너뛴다. 사용자 명령이 도는 중에 프로브를 쏘면
        셸 stdin 에서 그 명령 뒤에 줄을 서게 되고, 응답이 없는 이유가 '링크가
        죽어서' 인지 '앞 명령이 안 끝나서' 인지 구분할 수 없어진다.

        stale 세션도 계속 프로브한다 — 회복을 감지할 다른 경로가 없다.
        """
        idle_limit = self.cfg.heartbeat_sec
        if idle_limit <= 0:  # --heartbeat-sec 0 = 끔
            return
        now = time.time()
        for sess in list(self.sessions.values()):
            if sess.state == "dead" or sess.id in self._probing:
                continue
            if sess.exec_lock.locked():
                continue
            if now - sess.last_rx < idle_limit:
                continue
            self._spawn_probe(sess, self._probe_liveness(sess))

    async def _probe_liveness(self, sess: Session) -> None:
        """프레임 no-op 한 번. 응답이 없으면 stale 로 내린다 (§4.10).

        **stale 은 추정이지 확정이 아니다.** 링크가 half-open 이거나, 셸이
        session_write 로 받은 긴 명령에 물려 있거나 둘 중 하나다. 그래서 dead 로
        올리지 않고 아무것도 파괴하지 않는다 — 한 바이트만 와도 mark_rx 가
        active 로 되돌린다.
        """
        try:
            from . import executor

            if sess.exec_lock.locked():
                return  # tick 과 acquire 사이에 명령이 들어왔다. 정보 없음.
            async with sess.exec_lock:
                r = await executor.run_framed(
                    sess, LIVENESS_CMD, timeout=HEARTBEAT_TIMEOUT
                )
            # 성공 시 별도 처리가 없다: 응답 바이트가 pump 를 거치며 mark_rx 가
            # 이미 last_rx 를 갱신하고 stale 을 풀었다.
            if not r.get("complete") and sess.state == "active":
                sess.state = "stale"
                log.warning(
                    "session %s stale — liveness 프로브 무응답 (%.0fs 무수신)",
                    sess.id, time.time() - sess.last_rx,
                )
        except Exception:
            log.warning("liveness 프로브 실패 %s", sess.id, exc_info=True)

    async def _rearm_sweep(self) -> None:
        """세션을 잃었으면 리스너를 다시 연다 (§4.11).

        세션당 한 번만 센다(rearm_fired). 안 그러면 stale 을 오가는 세션 하나가
        재무장을 무한히 유발해 임플란트의 재시도 루프와 함께 세션을 쌓는다.
        """
        if self._rearm is None:
            return
        lost = [
            s
            for s in self.sessions.values()
            if s.state in ("dead", "stale") and not s.rearm_fired
        ]
        if not lost:
            return
        for s in lost:
            s.rearm_fired = True
        if self.listener is not None:
            return  # 이미 열려 있다 — 쿼터가 남아 있으므로 추가로 할 일이 없다
        live = sum(1 for s in self.sessions.values() if s.state == "active")
        if live + len(lost) > self.cfg.max_sessions:
            log.warning("재무장 보류 — max_sessions 여유가 없다")
            return
        host, port = self._rearm
        r = await self.listener_start(port, host, accept=len(lost))
        if "error" in r:
            # 흔한 경우는 포트가 아직 TIME_WAIT 인 것이다. 다음 tick 에 다시 온다.
            for s in lost:
                s.rearm_fired = False
            log.warning("재무장 실패, 다음 tick 에 재시도: %s", r["error"])
        else:
            log.info("세션 %d개 상실 — %s:%d 재무장", len(lost), host, port)

    def _evict_one_dead(self) -> bool:
        """상한 도달 시 가장 오래된 dead 세션을 치운다. reaper 대용 (§4.2).

        len(self.sessions) 에는 dead 세션도 포함되므로, 이게 없으면 타겟이
        max_sessions 번 끊겼다 붙는 순간부터 어떤 연결도 영원히 거부된다.
        """
        dead = [s for s in self.sessions.values() if s.state == "dead"]
        if not dead:
            return False
        victim = min(dead, key=lambda s: s.opened_at)
        del self.sessions[victim.id]
        log.info("evicted dead session %s", victim.id)
        return True

    # ---- 조회 툴 ------------------------------------------------------

    def session_list(self) -> dict:
        # 리스너 상태를 조회할 다른 수단이 없으므로 여기 함께 싣는다.
        # 자동 중단(§4.9)됐는지 확인하려면 이 필드를 봐야 한다.
        #
        # rearm 은 listener 안이 아니라 밖에 둔다. 재무장을 기다리는 동안은
        # listener 가 null 이라, 안에 넣으면 정작 알아야 할 때 사라진다 (§4.11).
        return {
            "listener": self._listener_info() if self.listener else None,
            "rearm": (
                {"host": self._rearm[0], "port": self._rearm[1]}
                if self._rearm
                else None
            ),
            "sessions": [s.summary() for s in self.sessions.values()],
        }

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def session_read(
        self,
        session_id: str,
        since: int = 0,
        wait_ms: int = 0,
        max_bytes: int = DEFAULT_READ_MAX_BYTES,
    ) -> dict:
        """§4.4 의 대기 의미를 그대로 구현한다.

        since 이후 새 데이터가 있으면 즉시 반환. 없으면 최대 wait_ms 까지 대기하고,
        그 사이 도착하면 즉시 반환. 타임아웃되면 빈 data 와 입력과 동일한
        next_since 를 반환한다(에러 아님). 세션이 죽으면 wait_ms 가 남아 있어도
        즉시 반환한다.
        """
        sess = self.sessions.get(session_id)
        if sess is None:
            return {"error": f"unknown session {session_id}"}

        buf = sess.buffer
        deadline = time.monotonic() + max(wait_ms, 0) / 1000.0
        while True:
            gen = buf.gen()  # 반드시 read '전' 에 잡는다
            data, next_since, truncated, capped = buf.read_since(since, max_bytes)
            if data or sess.state == "dead":
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await buf.wait_change(gen, remaining, also=lambda: sess.state == "dead")

        return {
            "session_id": sess.id,
            "data": data.decode("utf-8", errors="replace"),
            "next_since": next_since,
            "truncated": truncated,
            "capped": capped,
            "state": sess.state,
        }

    async def session_close(self, session_id: str) -> dict:
        """세션 연결을 끊는다 (§4.6). **기록을 지우는 게 아니라 연결만 끊는다.**

        버퍼는 그대로 남아 계속 조회할 수 있다 — dead 세션의 버퍼가 살아있는 건
        §2.2 가 이미 보장한 성질이고, 닫은 뒤에도 마지막 출력을 회수해야 하기 때문에
        중요하다. dict 에서의 제거는 기존 _evict_one_dead 에 맡긴다.

        멱등하다. 두 번 불러도 안전하고 already_dead 로 구분된다.
        """
        sess = self.sessions.get(session_id)
        if sess is None:
            return {"error": f"unknown session {session_id}"}

        already = sess.state == "dead"
        task = sess.pump_task
        if task is not None and not task.done():
            # 취소 경로(§4.3)가 dead 전이 · 분리 통지 · writer.close 를 모두 처리한다.
            # await task 대신 wait 를 쓰는 건 호출자 자신의 취소를 삼키지 않기 위해서다.
            task.cancel()
            await asyncio.wait({task}, timeout=2)
        if sess.state != "dead":  # 펌프가 없거나 취소가 안 먹은 경우의 폴백
            sess.state = "dead"
            sess._close_writer()
            await sess.buffer.bump_gen()

        log.info("closed session %s", sess.id)
        return {
            "session_id": sess.id,
            "state": sess.state,
            "already_dead": already,
            "end_cursor": sess.buffer.end(),
        }

    # ---- 종료 ---------------------------------------------------------

    async def close_all(self) -> None:
        self._rearm = None  # 종료 중에 재무장이 새 리스너를 열면 안 된다
        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None
        for probe in list(self._probes):
            probe.cancel()
        self._probes.clear()
        self._probing.clear()
        # 순서가 중요하다: 세션을 먼저 끊어야 wait_closed() 가 끝난다 (§4.7 참조).
        # 반대로 하면 3.12 의 wait_closed 가 살아있는 연결을 기다리며 멈춘다.
        for sess in list(self.sessions.values()):
            if sess.pump_task is not None and not sess.pump_task.done():
                sess.pump_task.cancel()
                try:
                    await sess.pump_task
                except (asyncio.CancelledError, Exception):
                    pass
            sess._close_writer()
        self.sessions.clear()

        if self.listener is not None:
            server = self.listener.server
            self.listener = None
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2.0)
            except (asyncio.TimeoutError, Exception):
                pass  # 종료 경로다. 매달리느니 포기한다.
