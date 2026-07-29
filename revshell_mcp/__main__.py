"""엔트리포인트, CLI 플래그.

로그는 stderr 로만 나간다. stdout 은 stdio transport 가 점유하므로 여기에
한 글자라도 흘리면 프로토콜이 깨진다 (§0).
"""

from __future__ import annotations

import argparse
import logging
import sys

from .manager import DEFAULT_MAX_SESSIONS, Config
from .session import DEFAULT_BUFFER_BYTES


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="revshell-mcp")
    ap.add_argument(
        "--allow-any-interface",
        action="store_true",
        help="루프백 외 바인드를 허용한다. 인증 없는 핸들러가 노출되므로 "
        "교전 범위가 확실할 때만 쓸 것.",
    )
    ap.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    ap.add_argument("--buffer-bytes", type=int, default=DEFAULT_BUFFER_BYTES)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,  # stdout 은 절대 쓰지 않는다
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = Config(
        allow_any_interface=args.allow_any_interface,
        max_sessions=args.max_sessions,
        buffer_bytes=args.buffer_bytes,
    )

    try:
        from .server import build
    except ModuleNotFoundError as e:  # pragma: no cover
        print(f"MCP SDK 가 필요하다: pip install mcp  ({e})", file=sys.stderr)
        return 1

    build(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
