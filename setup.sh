#!/usr/bin/env bash
# revshell-mcp 설치 스크립트
#
#   ./setup.sh            요구사항 확인 + 가상환경 생성 + 설치 + 스모크 체크
#   ./setup.sh --test     위 + 전체 테스트 실행
#   ./setup.sh --force    기존 .venv 를 지우고 새로 만든다
#   ./setup.sh --register 설치 후 Claude Code 에 자동 등록 (claude CLI 필요)
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
MCP_NAME="revshell"
ROOT="$(pwd)"
RUN_TESTS=0
FORCE=0
REGISTER=0
for arg in "$@"; do
    case "$arg" in
        --test)     RUN_TESTS=1 ;;
        --force)    FORCE=1 ;;
        --register) REGISTER=1 ;;
        -h|--help)
            sed -n '2,7p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "알 수 없는 옵션: $arg (--help 참조)" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then
    R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
    R=""; G=""; Y=""; B=""; N=""
fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$1" >&2; shift; for l in "$@"; do printf '    %s\n' "$l" >&2; done; exit 1; }
step() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

# ---------------------------------------------------------------- 요구사항
step "1/4 요구사항 확인"

PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    found=$(python3 -V 2>&1 || echo "python3 없음")
    die "Python 3.11 이상이 필요합니다 (현재: $found)" \
        "Debian/Ubuntu : sudo apt install python3.11 python3.11-venv" \
        "Raspberry Pi  : Bookworm 이상을 쓰거나 pyenv 로 설치하세요" \
        "(Bullseye 의 3.9 로는 동작하지 않습니다)"
fi
ok "$PY ($("$PY" -V 2>&1))"

if ! "$PY" -c 'import ensurepip' 2>/dev/null; then
    die "venv 모듈을 쓸 수 없습니다" \
        "Debian/Ubuntu : sudo apt install $PY-venv"
fi
ok "venv 사용 가능"

command -v git >/dev/null 2>&1 && ok "git $(git --version | awk '{print $3}')" \
                               || warn "git 없음 (설치에는 지장 없습니다)"

# 패키지 디렉토리 안에 만들어진 가상환경은 헷갈리기 쉽다
if [ -d "revshell_mcp/.venv" ]; then
    warn "revshell_mcp/.venv 가 있습니다. 패키지 디렉토리 안이라 혼동을 부릅니다."
    warn "이 스크립트는 저장소 루트에 $VENV 를 만듭니다. 정리: rm -rf revshell_mcp/.venv"
fi

# ---------------------------------------------------------------- 가상환경
step "2/4 가상환경"

if [ "$FORCE" = 1 ] && [ -d "$VENV" ]; then
    rm -rf "$VENV"; ok "기존 $VENV 삭제"
fi

if [ -x "$VENV/bin/python" ]; then
    ok "$VENV 재사용 ($("$VENV/bin/python" -V 2>&1))"
else
    "$PY" -m venv "$VENV" || die "가상환경 생성 실패"
    ok "$VENV 생성"
fi

# ---------------------------------------------------------------- 설치
step "3/4 설치"

"$VENV/bin/python" -m pip install --upgrade pip -q 2>/dev/null || warn "pip 업그레이드 생략"
if ! "$VENV/bin/python" -m pip install -e . -q; then
    die "설치 실패" "네트워크 연결과 pyproject.toml 을 확인하세요"
fi
ok "revshell-mcp + 의존성 설치 완료"
ok "mcp $("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("mcp"))' 2>/dev/null || echo '?')"

# ---------------------------------------------------------------- 검증
step "4/4 검증"

"$VENV/bin/python" - <<'EOF' || exit 1
import asyncio, sys

_c = sys.stdout.isatty()
G = "\033[32m" if _c else ""
R = "\033[31m" if _c else ""
N = "\033[0m" if _c else ""

try:
    from revshell_mcp.manager import Config
    from revshell_mcp.server import build
except Exception as e:
    print(f"  {R}✗{N} import 실패: {e}", file=sys.stderr); sys.exit(1)

tools = asyncio.run(build(Config()).list_tools())
# description 이 비면 LLM 이 사용법을 전혀 읽지 못한다. 조용히 지나가면 안 된다.
missing = [t.name for t in tools if not (t.description or "").strip()]
if missing:
    print(f"  {R}✗{N} description 이 빈 툴: {missing}", file=sys.stderr); sys.exit(1)
print(f"  {G}✓{N} 툴 {len(tools)}개 등록: {', '.join(t.name for t in tools)}")
EOF

[ -x "$VENV/bin/revshell-mcp" ] && ok "실행 파일: $VENV/bin/revshell-mcp" \
                               || die "콘솔 스크립트가 생성되지 않았습니다"

BIN="$ROOT/$VENV/bin/revshell-mcp"

if [ "$REGISTER" = 1 ]; then
    step "MCP 등록"
    command -v claude >/dev/null 2>&1 || die \
        "claude CLI 를 찾을 수 없습니다" \
        "다른 MCP 클라이언트를 쓰신다면 --register 없이 실행하고" \
        "아래에 출력되는 경로를 직접 등록하세요."
    # get 은 없는 이름에도 rc=0 을 돌려주므로 출력으로 판별한다
    if claude mcp get "$MCP_NAME" 2>&1 | grep -q "^${MCP_NAME}:"; then
        if claude mcp remove "$MCP_NAME" >/dev/null 2>&1; then
            ok "기존 '$MCP_NAME' 등록 제거 (경로 갱신을 위해)"
        else
            warn "기존 '$MCP_NAME' 제거 실패 — 등록이 겹칠 수 있습니다"
        fi
    fi
    if claude mcp add "$MCP_NAME" -s user -- "$BIN" >/dev/null 2>&1; then
        ok "등록 완료: $MCP_NAME -> $BIN"
        warn "적용하려면 Claude Code 를 재시작해야 합니다"
    else
        die "등록 실패" "수동 등록: claude mcp add $MCP_NAME -s user -- $BIN"
    fi
fi

if [ "$RUN_TESTS" = 1 ]; then
    step "테스트"
    "$VENV/bin/python" -W ignore::ResourceWarning \
        -m unittest discover -s tests -t . -p 'test_*.py' 2>&1 | tail -4
fi

# ---------------------------------------------------------------- 안내
printf '\n%s설치 완료%s\n' "$B" "$N"

if [ "$REGISTER" = 1 ]; then
    cat <<EOF

'$MCP_NAME' 등록까지 마쳤습니다. ${B}Claude Code 를 재시작${N}하면 툴 7개가 보입니다.
확인: claude mcp get $MCP_NAME
EOF
else
    cat <<EOF

MCP 클라이언트에 등록하세요. (--register 를 주면 자동으로 합니다)

  ${B}Claude Code${N}
    claude mcp add $MCP_NAME -s user -- $BIN

  ${B}그 외 클라이언트${N} (JSON)
    "$MCP_NAME": { "command": "$BIN" }

등록 후 클라이언트를 재시작하면 툴 7개가 보입니다.
EOF
fi

cat <<EOF

${B}원격 타겟의 콜백을 받으려면${N}

  리스너는 기본적으로 127.0.0.1 에만 바인드합니다. 다른 호스트에서 콜백을
  받으려면 ${B}등록 시${N} --allow-any-interface 를 붙여야 합니다.
  런타임에 켤 수 없으므로, 빠뜨렸다면 지우고 다시 등록해야 합니다.

    claude mcp add $MCP_NAME -s user -- $BIN --allow-any-interface

  0.0.0.0 뿐 아니라 ${B}특정 LAN IP(192.168.x.x 등)도 이 플래그 없이는 거부${N}됩니다.
  인증 없는 셸 핸들러가 열리는 것이므로 교전 범위가 확실할 때만 쓰세요.
  루프백만으로 충분한 경우(SSH 역터널 등)에는 붙이지 마세요.
EOF

printf '\n전체 테스트는 ./setup.sh --test 로 돌릴 수 있습니다.\n'
