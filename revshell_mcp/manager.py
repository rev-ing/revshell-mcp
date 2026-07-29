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


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(2)}"


@dataclass
class Config:
    allow_any_interface: bool = False  # --allow-any-interface (§4.1)
    max_sessions: int = DEFAULT_MAX_SESSIONS
    buffer_bytes: int = DEFAULT_BUFFER_BYTES


@dataclass
class Listener:
    id: str
    host: str
    port: int
    server: asyncio.AbstractServer


class SessionManager:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.sessions: dict[str, Session] = {}
        self.listener: Listener | None = None
        self._probes: set[asyncio.Task] = set()  # PTY 판별 태스크 강한 참조

    # ---- 리스너 -------------------------------------------------------

    async def listener_start(self, port: int, host: str = "127.0.0.1") -> dict:
        """바인드 게이트와 멱등성 (§4.1). accept 를 기다리지 않고 즉시 반환한다."""
        # 인증 없는 셸 핸들러를 전 인터페이스에 여는 것은 아무나 붙을 수 있다는 뜻이다.
        # host 가 자유 인자이므로 기본값이 루프백인 것만으로는 부족하다.
        if host not in LOOPBACK and not self.cfg.allow_any_interface:
            return {
                "error": f"refused: binding to a non-loopback address ({host}) "
                f"requires restarting the server with --allow-any-interface"
            }
        if self.listener is not None:
            if self.listener.port == port:
                return self._listener_info()  # 멱등
            return {
                "error": f"already listening on :{self.listener.port}; "
                f"call listener_stop() first to move to another port "
                f"(existing sessions are not affected)"
            }
        try:
            server = await asyncio.start_server(self._on_connect, host, port)
        except OSError as e:
            return {"error": f"bind failed on {host}:{port} — {e}"}
        self.listener = Listener(id=new_id("lsn"), host=host, port=port, server=server)
        log.info("listening on %s:%d (%s)", host, port, self.listener.id)
        return self._listener_info()

    async def listener_stop(self) -> dict:
        """리스닝 소켓만 닫고 포트를 반납한다 (§4.7).

        **기존 세션은 건드리지 않는다.** asyncio 의 server.close() 는 리스닝 소켓만
        닫고 이미 accept 된 연결의 transport 는 그대로 두므로, "새 연결을 안 받는다"와
        "붙어 있는 셸을 끊는다"는 완전히 별개다. 세션을 끊는 건 session_close 다.

        멱등하다. 리스너가 없으면 에러 대신 stopped=False 를 돌려준다.
        """
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
        }

    # ---- 연결 수락 ----------------------------------------------------

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """콜백에서 절대 오래 붙잡지 말 것. 태스크만 띄우고 즉시 반환한다 (§4.2)."""
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
        # 콜백에서 기다리지 않는다. 판별이 끝날 때까지 has_pty 는 None 이다.
        probe = asyncio.create_task(self._probe_pty(sess))
        self._probes.add(probe)
        probe.add_done_callback(self._probes.discard)
        log.info("session %s from %s", sess.id, sess.peer)

    async def _probe_pty(self, sess: Session) -> None:
        """연결 직후 1회만 PTY 여부를 판별한다 (§4.8).

        실패해도 세션은 유효해야 하므로 예외를 밖으로 내지 않는다. 판별 실패 시
        has_pty 는 None 으로 남고, 그건 "PTY 가 없다"가 아니라 "모른다"는 뜻이다.

        exec_lock 을 잡으므로 판별이 끝나기 전에 session_exec 이 들어오면 busy 로
        즉시 반환된다. 창은 짧고 busy hint 가 재시도를 안내하므로 그대로 둔다.
        """
        try:
            from . import executor  # 지연 import — executor 는 manager 를 모른다

            r = await executor.execute(sess, PTY_PROBE_CMD, timeout=PTY_PROBE_TIMEOUT)
            if r.get("complete") and r.get("exit_code") in (0, 1):
                sess.has_pty = r["exit_code"] == 0
                log.info("session %s has_pty=%s", sess.id, sess.has_pty)
        except Exception:
            log.warning("PTY 판별 실패 %s", sess.id, exc_info=True)

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
        return {"sessions": [s.summary() for s in self.sessions.values()]}

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
        for probe in list(self._probes):
            probe.cancel()
        self._probes.clear()
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
