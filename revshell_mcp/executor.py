"""프레이밍 센티널, 명령 실행, 입력 검증 (§5).

셸은 명령이 끝났다고 알려주지 않는다. 출력을 양끝에서 마커로 감싸 완료 신호를
인공적으로 만들어낸다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time

from .session import Session

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 65536

# exec 폴링 1회당 버퍼에서 읽어오는 상한. 링버퍼 상한과 '같다는 사실에 의존하지 말 것'
# (§5.2-3). 테스트는 이 값을 낮춰 read_capped 연속 참 상황을 인위적으로 만든다.
EXEC_READ_MAX = 1 << 20

_STALE_MARKER = re.compile(rb"__[SE][0-9a-f]{12}__")

# ---------------------------------------------------------------------------
# §5.1 입력 검증
# ---------------------------------------------------------------------------

# "\\" 는 여기 없다 — 마스킹 후에는 'x' 가 되어 걸리지 않으므로 스캐너가 직접 잡는다.
_TRAILING = ("&&", "||", ">>", ";", "|", "&", ">", "<")

_WORD_START = " \t;|&("

# 모델이 소비하는 문자열은 영어로 (§2.3). 주석·README 는 한글.
_E_DANGLING = "command ends with a dangling escape (\\)"
_E_COMMENT = "an unquoted comment (#) would swallow the terminating marker"
_E_UNBALANCED = "unbalanced quotes"


def _scan_shell(c: str) -> tuple[str | None, str | None]:
    """1패스로 따옴표 불균형·주석·매달린 이스케이프를 검출하고,
    따옴표/이스케이프 구간을 'x' 로 마스킹한 문자열을 함께 돌려준다.

    마스크는 원문과 길이가 1:1 로 유지된다 — 주석 판정이 원문 인덱스와
    어긋나지 않으려면 이 성질이 필요하다.
    """
    state: str | None = None  # None | "'" | '"'
    out: list[str] = []
    i, n = 0, len(c)
    while i < n:
        ch = c[i]
        if state == "'":  # 작은따옴표 안엔 이스케이프가 없다
            out.append("x")
            if ch == "'":
                state = None
        elif state == '"':
            out.append("x")
            if ch == "\\":  # 큰따옴표 안에선 \ 가 다음 문자를 먹는다
                if i + 1 >= n:
                    return _E_DANGLING, None
                out.append("x")
                i += 1
            elif ch == '"':
                state = None
        else:
            if ch == "\\":
                if i + 1 >= n:
                    return _E_DANGLING, None
                out.append("x")
                out.append("x")
                i += 1
            elif ch in "'\"":
                out.append("x")
                state = ch
            elif ch == "#" and (not out or out[-1] in _WORD_START):
                # 판정은 원문이 아니라 마스크 기준이다. echo a\ #b 의 이스케이프된
                # 공백은 단어 구분자가 아니므로 원문 기준으로 보면 오판한다.
                return _E_COMMENT, None
            else:
                out.append(ch)
        i += 1
    if state:
        return _E_UNBALANCED, None
    return None, "".join(out)


# 오탐이 게이트 자체보다 위험하다 (§5.8). rm -rf /tmp/build 까지 막으면 LLM 이
# confirm=True 를 습관적으로 붙이게 되고, 그 순간 게이트는 장식이 된다.
# 이건 오타·폭주 방지지 악의적 우회 차단이 아니다 — 조각 전송으로 우회 가능하다.
DESTRUCTIVE = [
    r"rm\s+-rf\s+/(\s|$|\*)",  # 루트만. /tmp/build 는 통과시킨다
    r"mkfs\.",
    r"dd\s+.*of=/dev/",
    r"\bshutdown\b|\breboot\b",
    r":\(\)\{.*\};:",
]
_DESTRUCTIVE_RE = [re.compile(p) for p in DESTRUCTIVE]


def _check_destructive(c: str, confirm: bool) -> str | None:
    if confirm:
        return None
    for rx in _DESTRUCTIVE_RE:
        if rx.search(c):
            return (
                f"refused: matches a destructive pattern ({rx.pattern}). "
                f"If this is intended, call again with confirm=true."
            )
    return None


def _validate_all(cmd: str, confirm: bool) -> str | None:
    c = cmd.strip()  # strip 전에 검사하면 'ls &  ' 같은 입력이 샌다
    if not c:
        return "empty command"
    if "\n" in c or "\r" in c:
        return "newlines are not allowed; send multi-line input with session_write"
    err, bare = _scan_shell(c)  # ← 트레일링 검사보다 반드시 먼저
    if err:
        return err
    assert bare is not None
    if bare.endswith(_TRAILING):
        return (
            "command ends with a shell operator. To background a job, send "
            "`nohup cmd >/tmp/log 2>&1 &` with session_write and read the log."
        )
    return _check_destructive(c, confirm)


# ---------------------------------------------------------------------------
# §5.3 증분 스캐너
# ---------------------------------------------------------------------------


class _FrameScanner:
    """매 폴링마다 frame 전체를 find 하면 O(n^2) 이다. 마지막 탐색 위치를 들고
    다니되 마커 길이 -1 만큼만 겹쳐 재검사한다."""

    def __init__(self, s_mark: bytes, e_mark: bytes) -> None:
        self.s, self.e = s_mark, e_mark
        self.s_at = -1
        self.e_at = -1
        self.scan_from = 0

    def _shift(self, n: int) -> None:
        """프레임 앞/가운데에서 n 바이트가 사라졌을 때 오프셋 보정."""
        self.scan_from = max(0, self.scan_from - n)
        if self.e_at >= 0:
            # 현재 흐름에서는 compact 의 0단계 때문에 도달 불가능하지만 가드로 남긴다.
            # 0단계를 완화하는 순간 body 슬라이스와 rc 가 어긋난다. 단위 테스트로 커버.
            self.e_at -= n

    def feed(self, frame) -> tuple[bytes, int | None] | None:
        ov = max(len(self.s), len(self.e)) - 1
        base = max(0, self.scan_from - ov)
        if self.s_at < 0:
            k = frame.find(self.s, base)
            if k >= 0:
                self.s_at = k
        if self.s_at >= 0 and self.e_at < 0:
            k = frame.find(self.e, max(self.s_at + len(self.s), base))
            if k >= 0:
                self.e_at = k
        self.scan_from = len(frame)
        if self.e_at < 0:
            return None
        nl = frame.find(b"\n", self.e_at + len(self.e))
        if nl < 0:
            # rc 가 아직 도착 안 함 — 계속 폴링 (§5.4). 개행 없이 파싱하면
            # 두 자리 종료코드(12)의 앞 한 글자만 읽는 사고가 난다.
            return None
        body = bytes(frame[self.s_at + len(self.s) : self.e_at]).lstrip(b"\r\n")
        rc = _parse_rc(frame[self.e_at + len(self.e) : nl])
        return _drop_stale_markers(body), rc

    def compact(self, frame: bytearray, max_bytes: int) -> bool:
        """프레임 생애주기 (§5.5). **반드시 feed 이후에 호출한다.**

        각 단계는 직전 feed 의 스캔 결과에 의존한다. 순서가 반대면 첫 반복에서
        프레임이 한 번도 스캔되지 않은 채 1단계가 돌아 방금 도착한 S 마커를 지운다.

        반환값은 '3단계가 돌았는가' = capped 여부. 1·2단계가 버리는 건 에코·프롬프트
        잔재이므로 사용자 데이터 손실이 아니다.
        """
        ov = max(len(self.s), len(self.e)) - 1
        if self.e_at >= 0:  # 0단계: 개행만 기다리는 중, 압축할 이유가 없다
            return False
        if self.s_at < 0:  # 1단계: 스캔했는데 S 없음 = 전부 쓰레기
            if len(frame) > ov:
                n = len(frame) - ov
                del frame[:n]
                self._shift(n)
            return False
        if self.s_at > 0:  # 2단계: 프레임 앞의 에코 폐기
            n = self.s_at
            del frame[:n]
            self._shift(n)
            self.s_at = 0  # ← _shift 는 s_at 을 건드리지 않는다. 빠뜨리기 쉽다.
            return False
        if len(frame) <= max_bytes:  # 3단계
            return False
        head = max_bytes // 2  # tail_keep = max_bytes - head
        n = len(frame) - max_bytes
        del frame[head : head + n]
        self._shift(n)
        return True


def _parse_rc(raw) -> int | None:
    try:
        return int(bytes(raw).strip())
    except ValueError:
        return None


def _drop_stale_markers(body: bytes) -> bytes:
    """타임아웃된 이전 exec 의 마커가 뒤늦게 도착해 현재 프레임 안쪽에 떨어질 수 있다."""
    return _STALE_MARKER.sub(b"", body)


# ---------------------------------------------------------------------------
# 반환 스키마 (§2.2) — 모든 경로가 같은 필드 집합을 채운다
# ---------------------------------------------------------------------------


def _base(sess: Session) -> dict:
    return {
        "session_id": sess.id,
        "output": "",
        "exit_code": None,
        "next_since": None,
        "complete": False,
        "truncated": False,
        "capped": False,
        "hint": None,
    }


def _cap_body(body: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """반환 직전의 상한 (§5.5).

    compact 의 3단계만으로는 부족하다. 프레임이 한 번의 read 에 통째로 도착하면
    feed 가 compact 보다 먼저 완성시키므로(§5.2-1 의 필수 순서) 압축이 한 번도
    돌지 않은 채 EXEC_READ_MAX(1 MiB) 크기의 output 이 나갈 수 있다. 로컬 타겟에
    빠른 명령이면 흔히 벌어지는 일이지 이론적 케이스가 아니다.

    compact 3단계와 같은 규칙(가운데를 버리고 앞뒤를 남긴다)을 쓴다. 잘렸다는
    표시는 별도 마커가 아니라 capped 필드다 — 마커를 끼우면 바이트 회계가 어긋난다.
    """
    if len(body) <= max_bytes:
        return body, False
    head = max_bytes // 2
    tail = max_bytes - head
    return body[:head] + body[-tail:], True


def _rejected(sess: Session, reason: str) -> dict:
    r = _base(sess)
    r["hint"] = reason  # next_since 는 null — 아무것도 읽지 않았다 (§2.1)
    return r


def _busy(sess: Session) -> dict:
    r = _base(sess)
    r["hint"] = (
        "another command is running on this session, so this one has NOT started "
        "yet. This is not a failure: check progress with session_read, then retry "
        "the same call."
    )
    return r


def _ok(sess, body: bytes, rc, pos, truncated, capped, max_bytes) -> dict:
    body, hit = _cap_body(body, max_bytes)
    r = _base(sess)
    r.update(
        output=body.decode("utf-8", errors="replace"),
        exit_code=rc,
        next_since=pos,
        complete=True,
        truncated=truncated,
        capped=capped or hit,
    )
    return r


def _incomplete(sess, frame, scanner, pos, truncated, capped, hint, max_bytes) -> dict:
    # 이미 수집한 부분 출력을 버리면 LLM 이 불필요하게 한 번 더 왕복한다 (§5.2-4).
    if scanner.s_at >= 0:
        partial = bytes(frame[scanner.s_at + len(scanner.s) :]).lstrip(b"\r\n")
    else:
        partial = bytes(frame)
    partial, hit = _cap_body(_drop_stale_markers(partial), max_bytes)
    r = _base(sess)
    r.update(
        output=partial.decode("utf-8", errors="replace"),
        next_since=pos,
        truncated=truncated,
        capped=capped or hit,
        hint=hint,
    )
    return r


_INTERACTIVE = re.compile(r"\b(vim?|vi|nano|top|htop|less|more|man|su|ssh|ftp)\b")
_STDIN_EATER = re.compile(r"^(cat|read|python3?|sh|bash|sort|wc|grep|head|tail)\s*$")


def _timeout_hint(cmd: str, scanner: _FrameScanner, truncated: bool) -> str:
    c = cmd.strip()
    if truncated and scanner.s_at < 0:
        return (
            "output was too large and the start of the frame was lost. Reduce the "
            "output (`| head`) or redirect to a file and read it in pieces."
        )
    if _INTERACTIVE.search(c):
        return (
            "this looks like an interactive program, so the sentinel will never "
            "arrive. Drive it with session_write if the session has a PTY. "
            "Without a PTY such programs cannot be controlled at all — use "
            "session_close and reconnect."
        )
    if _STDIN_EATER.match(c):
        # \x03 / \x04 를 시그널·EOF 로 바꿔주는 건 tty 라인 디시플린이다.
        # PTY 없는 파이프 세션에서는 그냥 바이트 한 개로 전달되어 아무 효과가 없다.
        return (
            "the command may have consumed stdin, including the line carrying the "
            "terminating marker. If the session has a PTY, session_write of EOF "
            "(`\\x04`) may free it. Without a PTY those control bytes are delivered "
            "literally and will not help — use session_close and reconnect."
        )
    return (
        "the command may still be running. This is not a failure: continue with "
        "session_read(since=next_since)."
    )


# ---------------------------------------------------------------------------
# §5.2 실행 흐름
# ---------------------------------------------------------------------------


async def execute(
    sess: Session,
    cmd: str,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    confirm: bool = False,
) -> dict:
    err = _validate_all(cmd, confirm)
    if err:
        return _rejected(sess, err)

    # 비어있는 Lock.acquire() 는 yield 하지 않으므로 이 검사와 acquire 사이에
    # 다른 코루틴이 끼어들 수 없다. TOCTOU 처럼 보이지만 안전하다. 고치지 말 것.
    if sess.exec_lock.locked():
        return _busy(sess)

    async with sess.exec_lock:
        token = secrets.token_hex(6)
        s_mark = f"__S{token}__".encode()
        e_mark = f"__E{token}__".encode()
        # 마커 분할 트릭을 빼지 말 것: sh 는 인접 따옴표 문자열을 이어붙이지만
        # PTY 에코로 되돌아오는 원문에는 따옴표가 살아있어 마커와 일치하지 않는다.
        payload = (
            f"printf '__S''{token}''__\\n'; {cmd.strip()}; "
            f"printf '__E''{token}''__%d\\n' $?\n"
        ).encode()

        pos = sess.buffer.end()  # 전송 직전 고정
        frame = bytearray()
        scanner = _FrameScanner(s_mark, e_mark)
        truncated = capped = False
        await sess.write(payload)

        deadline = time.monotonic() + timeout
        while True:
            gen = sess.buffer.gen()  # 반드시 read '전' 에 잡는다
            chunk, pos, trunc, read_capped = sess.buffer.read_since(pos, EXEC_READ_MAX)
            truncated |= trunc
            if chunk:
                frame.extend(chunk)

            # ── 순서 엄수: feed 먼저, compact 나중 (§5.5) ──
            done = scanner.feed(frame)
            if done is not None:
                body, rc = done
                return _ok(sess, body, rc, pos, truncated, capped, max_bytes)
            capped |= scanner.compact(frame, max_bytes)

            if sess.state == "dead":
                return _incomplete(
                    sess, frame, scanner, pos, truncated, capped,
                    "the session closed before the command finished", max_bytes,
                )
            if (remaining := deadline - time.monotonic()) <= 0:
                break  # ← read_capped 분기보다 반드시 위 (§5.2-3)
            if read_capped:
                # 미읽은 데이터가 남았다. 대기 금지. 다만 이 경로 전체가 sync 라
                # 양보하지 않으면 펌프가 굶는다.
                await asyncio.sleep(0)
                continue
            await sess.buffer.wait_change(
                gen, remaining, also=lambda: sess.state == "dead"
            )

    return _incomplete(
        sess,
        frame,
        scanner,
        pos,
        truncated,
        capped,
        _timeout_hint(cmd, scanner, truncated),
        max_bytes,
    )


async def session_write(
    sess: Session, data: str, newline: bool = True, confirm: bool = False
) -> dict:
    """원시 전송 (§2). 락을 잡지 않지만 exec 진행 중이면 warnings 에 플래그를 세운다."""
    reason = _check_destructive(data, confirm)
    if reason:
        return {
            "session_id": sess.id,
            "bytes_written": 0,
            "write_stalled": sess.write_stalled,
            "warnings": [reason],
        }
    warnings: list[str] = []
    if sess.exec_lock.locked():
        warnings.append("wrote directly to a session with a session_exec in flight")
    if sess.state == "dead":
        warnings.append("session is dead; the bytes will not reach the target")
    raw = data.encode() + (b"\n" if newline else b"")
    n = await sess.write(raw)
    if sess.write_stalled:
        warnings.append("drain timed out; bytes are queued and may not be delivered")
    return {
        "session_id": sess.id,
        "bytes_written": n,
        "write_stalled": sess.write_stalled,
        "warnings": warnings,
    }
