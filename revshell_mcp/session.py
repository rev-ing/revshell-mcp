"""Buffer 와 Session.

이 모듈은 MCP 를 import 하지 않는다 (§0). 나중에 CLI 나 웹 UI 를 같은 코어 위에
얹을 수 있어야 한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_BUFFER_BYTES = 1 << 20
DRAIN_TIMEOUT = 5.0
READ_CHUNK = 4096

# fire-and-forget 태스크의 강한 참조 (§4.3).
# 이 집합을 빼면 태스크가 GC 되어 재현되지 않는 간헐적 미통지가 된다.
_PENDING: set[asyncio.Task] = set()


def _utf8_safe_end(b: bytes) -> int:
    """미완성 멀티바이트 꼬리를 제외한 길이. 잘린 꼬리는 다음 read 에서 이어진다 (§3.3)."""
    n = len(b)
    for back in range(1, min(4, n) + 1):
        c = b[n - back]
        if c & 0b1100_0000 == 0b1000_0000:
            continue  # continuation byte, 계속 되짚음
        if c & 0b1000_0000 == 0:
            need = 1
        elif c & 0b1110_0000 == 0b1100_0000:
            need = 2
        elif c & 0b1111_0000 == 0b1110_0000:
            need = 3
        elif c & 0b1111_1000 == 0b1111_0000:
            need = 4
        else:
            return n  # 깨진 바이트, errors="replace" 에 맡김
        return n if back >= need else n - back
    return n


def _skip_orphan_continuations(b, i: int) -> int:
    """앞쪽 절단 후 남은 고아 continuation byte 를 건너뛴다 (§3.3)."""
    while i < len(b) and (b[i] & 0b1100_0000) == 0b1000_0000:
        i += 1
    return i


class Buffer:
    """링버퍼 + 절대 커서 (§3).

    절대 오프셋을 반환하므로 서버가 클라이언트별 읽기 위치를 기억할 필요가 없다.
    버퍼가 잘려나가도 커서 비교만으로 누락을 감지한다.
    """

    def __init__(self, max_bytes: int = DEFAULT_BUFFER_BYTES) -> None:
        self._buf = bytearray()
        self._cursor = 0  # _buf[0] 의 절대 오프셋
        self._max = max_bytes
        self._gen = 0
        # Condition 이 자체 락을 갖고 있으므로 별도의 _lock 을 두지 말 것 (§3.1).
        self._cond = asyncio.Condition()

    # ---- sync 조회 ----------------------------------------------------

    def end(self) -> int:
        return self._cursor + len(self._buf)

    def gen(self) -> int:
        return self._gen

    def buffered(self) -> int:
        return len(self._buf)

    def read_since(self, offset: int, max_bytes: int) -> tuple[bytes, int, bool, bool]:
        """returns (data, next_since, truncated, capped).

        이 메서드가 sync 인 것은 의도된 설계다 (§3.1). 내부에 await 가 없으므로
        단일 이벤트 루프에서 append 와 인터리브될 수 없고, 따라서 락 없이
        호출해도 안전하다. **async 로 바꾸지 말 것** — 데드락이 난다.

        cap · UTF-8 경계 · next_since 는 서로 물려 있어 한 함수에서 계산한다 (§3.2).
        """
        truncated = offset < self._cursor
        start = min(max(offset, self._cursor) - self._cursor, len(self._buf))
        capped = len(self._buf) - start > max_bytes

        take = bytes(self._buf[start : start + max_bytes])  # 슬라이스 1회
        take = take[: _utf8_safe_end(take)]  # 유효 경계까지 뒤로 물림

        # next_since 는 '실제 반환한 바이트 수' 로부터 계산한다. 버퍼 끝(end())을
        # 반환하면 cap 과 경계 보정분이 조용히 유실된다 (§3.2).
        next_since = self._cursor + start + len(take)
        return take, next_since, truncated, capped

    # ---- async 변경/대기 ----------------------------------------------

    async def append(self, data: bytes) -> None:
        async with self._cond:
            self._buf.extend(data)
            if len(self._buf) > self._max:
                drop = _skip_orphan_continuations(self._buf, len(self._buf) - self._max)
                del self._buf[:drop]
                self._cursor += drop
            self._gen += 1
            self._cond.notify_all()

    async def bump_gen(self) -> None:
        """데이터 없이 세대만 올린다. 상태 전이 통지용 (§4.3)."""
        async with self._cond:
            self._gen += 1
            self._cond.notify_all()

    async def wait_change(
        self, last_gen: int, timeout: float, also: Callable[[], bool] | None = None
    ) -> int:
        """세대 변화 대기. **타임아웃은 정상 경로이며 예외를 던지지 않는다** (§3.4).

        `also` 는 조기 기상을 만들지 못한다 — Condition.wait_for 의 술어는 notify 가
        와야 재평가되기 때문이다. "다른 이유로 깨어났을 때 함께 확인되는 조건" 일 뿐이며,
        세션 종료를 실제로 조기 감지시키는 것은 §4.3 의 통지다.
        """

        def ready() -> bool:
            return self._gen != last_gen or (also is not None and also())

        if timeout <= 0:
            return self._gen
        try:
            async with self._cond:
                await asyncio.wait_for(self._cond.wait_for(ready), timeout)
        except asyncio.TimeoutError:
            pass
        return self._gen


@dataclass
class Session:
    id: str  # "sess_a3f9"
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: tuple[str, int]
    opened_at: float = field(default_factory=time.time)  # §4.2 퇴출 정렬, §2.2 age_sec
    state: str = "active"  # "active" | "dead"
    buffer: Buffer = field(default_factory=Buffer)
    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # §2.2 busy
    write_stalled: bool = False  # §4.5
    pump_task: asyncio.Task | None = None
    # None = 아직 모름/판별 실패. 판별 실패해도 세션은 유효해야 한다 (§4.8).
    has_pty: bool | None = None

    # ---- I/O ----------------------------------------------------------

    async def write(self, data: bytes) -> int:
        """쓰기는 반드시 타임아웃 (§4.5).

        타겟이 안 읽으면 drain() 이 영원히 안 끝나 툴 상한을 깬다. 타임아웃돼도
        바이트는 writer 버퍼에 남아 나중에 전송되므로 깨끗한 실패가 아니다 —
        반드시 플래그로 노출해야 LLM 이 "보냈는데 반응 없음" 을 오해하지 않는다.
        """
        try:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), DRAIN_TIMEOUT)
            self.write_stalled = False  # 성공 시 반드시 해제
        except asyncio.TimeoutError:
            self.write_stalled = True
        except (ConnectionResetError, BrokenPipeError, OSError):
            self.write_stalled = True
            log.warning("write 실패 %s", self.id, exc_info=True)
        return len(data)

    async def pump(self) -> None:
        """소켓을 빨아들여 버퍼에 넣는 일만 한다 (§4.3).

        ConnectionResetError/BrokenPipeError 만 잡으면 안 된다. OSError 나
        예상 밖 예외가 나면 태스크가 예외로 죽고 꼬리의 상태 전이에 도달하지 못해,
        세션이 영원히 active 로 남는다.
        """
        try:
            while True:
                data = await self.reader.read(READ_CHUNK)
                if not data:
                    break
                await self.buffer.append(data)
        # ---- 취소 경로: await 금지, 반드시 re-raise ----
        except asyncio.CancelledError:
            self.state = "dead"
            self._notify_detached()
            self._close_writer()
            raise
        # ---- 그 외 모든 예외를 삼킨다 ----
        except Exception:
            log.warning("pump 종료 %s", self.id, exc_info=True)
        self.state = "dead"
        await self.buffer.bump_gen()
        self._close_writer()

    def _notify_detached(self) -> None:
        """취소된 태스크는 await 할 수 없으므로 별도 태스크로 통지한다 (§4.3).

        새 태스크는 취소 대상이 아니다. _PENDING 강한 참조를 빼지 말 것.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 루프 종료 중
            return
        t = loop.create_task(self.buffer.bump_gen())
        _PENDING.add(t)
        t.add_done_callback(_PENDING.discard)

    def _close_writer(self) -> None:
        try:
            self.writer.close()
        except Exception:  # pragma: no cover - 이미 닫힌 소켓
            pass

    # ---- 조회 ---------------------------------------------------------

    def summary(self) -> dict:
        """session_list 의 한 행 (§2.2)."""
        return {
            "id": self.id,
            "peer": f"{self.peer[0]}:{self.peer[1]}" if self.peer else None,
            "state": self.state,
            "age_sec": round(time.time() - self.opened_at, 1),
            "end_cursor": self.buffer.end(),
            "buffered_bytes": self.buffer.buffered(),
            "busy": self.exec_lock.locked(),
            "write_stalled": self.write_stalled,
            "has_pty": self.has_pty,
        }
