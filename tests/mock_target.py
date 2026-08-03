"""테스트용 콜백 클라이언트 (§9).

실제 타겟 없이 전 기능을 검증할 수 있어야 한다. 두 모드를 지원한다:

- 파이프 모드 — `sh` + subprocess.PIPE. 에코도 PS1 도 없다. 기본 검증용.
- PTY 모드   — pty.openpty() 로 에코와 PS1 을 재현. 프레이밍 방식이
               strip_echo 없이 동작한다는 주장을 증명하는 것이 목적이다.

둘 다 stdlib 만으로 된다.
"""

from __future__ import annotations

import asyncio
import os
import pty
import signal
import termios

RELAY_CHUNK = 4096


class MockTarget:
    def __init__(
        self,
        port: int,
        mode: str = "pipe",
        host: str = "127.0.0.1",
        shell: str = "/bin/sh",
        ps1: str = "mock$ ",
    ) -> None:
        self.port, self.mode, self.host, self.shell, self.ps1 = (
            port,
            mode,
            host,
            shell,
            ps1,
        )
        self.proc: asyncio.subprocess.Process | None = None
        self._sock_r: asyncio.StreamReader | None = None
        self._sock_w: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._pty_reader: asyncio.StreamReader | None = None
        self._pty_wt = None
        self._pty_rt = None

    # ---- 수명 ---------------------------------------------------------

    async def start(self) -> "MockTarget":
        self._sock_r, self._sock_w = await asyncio.open_connection(self.host, self.port)
        if self.mode == "pipe":
            await self._start_pipe()
        elif self.mode == "pty":
            await self._start_pty()
        else:
            raise ValueError(f"unknown mode {self.mode}")
        return self

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self.kill_shell()
        for tr in (self._pty_rt, self._pty_wt):
            if tr is not None:
                try:
                    tr.close()
                except Exception:
                    pass
        if self._sock_w is not None:
            try:
                self._sock_w.close()
            except Exception:
                pass

    async def kill_shell(self) -> None:
        """셸만 죽여 '연결 중 끊김' 을 만든다."""
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except (asyncio.TimeoutError, Exception):
                pass
            # subprocess transport 를 명시적으로 닫지 않으면 루프 종료 후
            # __del__ 에서 ResourceWarning 이 쏟아진다
            tr = getattr(self.proc, "_transport", None)
            if tr is not None:
                try:
                    tr.close()
                except Exception:
                    pass

    async def drop(self) -> None:
        """소켓만 끊는다."""
        if self._sock_w is not None:
            self._sock_w.close()

    async def freeze(self) -> None:
        """**half-open 을 재현한다** (§4.10).

        소켓은 ESTABLISHED 그대로 두고 릴레이만 멈춘다. 서버 입장에서는 보내면
        보내지고 read 는 영원히 안 끝나는, 실제 half-open 과 구별할 수 없는 상태가
        된다. drop() 처럼 FIN 을 보내면 pump 가 EOF 를 보고 dead 로 전이해버려
        정작 잡으려는 실패 양상이 사라진다 — 그 차이가 이 메서드의 존재 이유다.

        중간 홉(SSH 역터널, NAT)이 조용히 사라진 경우가 여기 해당한다.
        """
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        # 셸도 멈춘다. 살아 있으면 프로브에 답해버려 stale 이 안 잡힌다.
        await self.kill_shell()

    # ---- 파이프 모드 --------------------------------------------------

    def _env(self) -> dict:
        env = dict(os.environ)
        env.update(PS1=self.ps1, TERM="dumb", LC_ALL="C.UTF-8", LANG="C.UTF-8")
        env.pop("ENV", None)
        return env

    async def _start_pipe(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.shell,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._env(),
        )
        self._tasks = [
            asyncio.create_task(self._pump(self.proc.stdout.read, self._to_socket)),
            asyncio.create_task(self._pump(self._sock_r.read, self._to_pipe)),
        ]

    async def _to_pipe(self, data: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    # ---- PTY 모드 -----------------------------------------------------

    async def _start_pty(self) -> None:
        master, slave = pty.openpty()
        # 에코와 ONLCR(\n -> \r\n) 을 그대로 살린다. 프레이밍이 이걸 견디는지가
        # 이 모드의 존재 이유다.
        attrs = termios.tcgetattr(slave)
        attrs[3] |= termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        self.proc = await asyncio.create_subprocess_exec(
            self.shell,
            "-i",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env=self._env(),
        )
        os.close(slave)

        loop = asyncio.get_running_loop()
        self._pty_reader = asyncio.StreamReader()
        self._pty_rt, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._pty_reader),
            os.fdopen(os.dup(master), "rb", 0),
        )
        self._pty_wt, _ = await loop.connect_write_pipe(
            asyncio.Protocol, os.fdopen(master, "wb", 0)
        )
        self._tasks = [
            asyncio.create_task(self._pump(self._pty_reader.read, self._to_socket)),
            asyncio.create_task(self._pump(self._sock_r.read, self._to_pty)),
        ]

    async def _to_pty(self, data: bytes) -> None:
        assert self._pty_wt is not None
        self._pty_wt.write(data)

    # ---- 공통 릴레이 --------------------------------------------------

    async def _to_socket(self, data: bytes) -> None:
        assert self._sock_w is not None
        self._sock_w.write(data)
        await self._sock_w.drain()

    async def _pump(self, read, sink) -> None:
        try:
            while True:
                data = await read(RELAY_CHUNK)
                if not data:
                    break
                await sink(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        # 한쪽이 EOF 면 반대편도 닫아 세션이 dead 로 전이하게 한다
        if self._sock_w is not None:
            try:
                self._sock_w.close()
            except Exception:
                pass


async def spawn(port: int, mode: str = "pipe", **kw) -> MockTarget:
    return await MockTarget(port, mode, **kw).start()


def free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


__all__ = ["MockTarget", "spawn", "free_port", "signal"]
