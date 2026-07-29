# SSH 역터널로 원격 콜백 받기

타겟은 인터넷 너머의 공인 서버로 콜백하는데, **작업은 다른 머신에서 하고 싶을 때** 쓰는 구성입니다.

```
[타겟]  192.168.1.112
   │ TCP 203.0.113.10:31338          ← 임플란트 설정 그대로, 수정 불필요
   ▼
[중계 서버]  공인 IP, sshd 가 0.0.0.0:31338 수신
   │ SSH 역터널
   ▼
[작업 머신]  127.0.0.1:31339          ← 여기에 리스너. MCP 클라이언트가 도는 곳
```

## 왜 이렇게 하나

MCP 서버는 **클라이언트의 자식 프로세스**로 실행됩니다. 작업 머신에서 Claude Code를 쓰고 있다면 리스너도 그 머신에서 돕니다. 그런데 타겟은 대개 NAT 뒤에 있는 작업 머신에 직접 닿을 수 없습니다.

중계 서버에서 클라이언트를 따로 띄우는 방법도 있지만, 그러면 **지금까지 쌓아온 작업 맥락이 그쪽에 없습니다.** 역터널은 세션만 작업 머신으로 끌어옵니다.

부수적인 이점:

- **`--allow-any-interface`가 필요 없습니다.** 리스너는 `127.0.0.1` 그대로입니다.
- 방화벽 규칙, WSL 네트워킹 설정을 건드리지 않습니다.
- 인터넷에 노출되는 건 **SSH가 인증하는 포트**뿐입니다.
- 임플란트의 콜백 주소를 바꾸지 않아도 됩니다.

---

## 준비

| 항목 | 확인 |
|---|---|
| 중계 서버에 SSH 접속 | 키 인증 권장. 포트가 22가 아닐 수 있음 |
| 중계 서버의 콜백 포트가 비어 있음 | `ss -tln \| grep 31338` |
| 중계 서버에서 콜백 포트가 외부로 열려 있음 | 공유기 포트포워딩 등 |

**SSH 포트가 22가 아닐 수 있습니다.** 콜백 포트만 포워딩해 두고 SSH는 다른 포트로 돌리는 구성이 흔합니다. 아래 예시는 `31337`을 씁니다.

---

## 1. 중계 서버에 `GatewayPorts` 켜기 (한 번만)

기본값이 `no`라서, 이대로 두면 `-R`이 **중계 서버의 루프백에만** 바인드되어 타겟이 붙지 못합니다.

작업 머신에서 중계 서버로 접속한 뒤:

```bash
echo 'GatewayPorts yes' | sudo tee /etc/ssh/sshd_config.d/gatewayports.conf
sudo systemctl restart ssh
```

확인:

```bash
grep -riE '^\s*GatewayPorts' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/
```

## 2. 작업 머신에서 터널 띄우기

```bash
ssh -p 31337 -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -R 0.0.0.0:31338:localhost:31339 \
    user@[중계서버 ip]
```

| 옵션 | 이유 |
|---|---|
| `-N` | 셸을 열지 않고 포워딩만 |
| `-R 0.0.0.0:31338:localhost:31339` | 중계 서버의 **모든 인터페이스** 31338 → 작업 머신 루프백 31339 |
| `ExitOnForwardFailure=yes` | `GatewayPorts`가 꺼져 있으면 **즉시 실패**. 조용히 루프백에만 붙는 사고를 막음 |
| `ServerAliveInterval=30` | 죽은 터널 감지 |

터미널을 점유하므로 별도 창에서 띄우거나 `-f`로 백그라운드에 보내세요.

**바인드 주소를 확인하세요.** 중계 서버에서:

```bash
ss -tln | grep 31338
```

```
LISTEN 0 128  0.0.0.0:31338  0.0.0.0:*      ← 정상
LISTEN 0 128  127.0.0.1:31338 0.0.0.0:*     ← GatewayPorts 가 안 켜짐
```

## 3. 리스너 시작

작업 머신의 MCP 클라이언트에서:

```
listener_start(port=31339)
```

**`host`는 기본값 `127.0.0.1` 그대로입니다.** 터널이 루프백으로 넘겨주므로 바인드 게이트를 풀 필요가 없습니다.

## 4. 확인

```
session_list()
→ {"listener": {...}, "sessions": [{"id": "sess_b3cb", "state": "active", "has_pty": true, ...}]}
```

```
session_exec(session_id="sess_b3cb", cmd="id; hostname; uname -srm")
```

---

## 알아둘 점

### `peer`가 항상 `127.0.0.1`이 됩니다

모든 연결이 터널을 통과하므로 세션의 출발지 IP를 볼 수 없습니다.

```json
"peer": "127.0.0.1:46314"
```

**타겟이 여럿이면 `peer`로 구분할 수 없습니다.** `hostname`이나 MAC으로 식별하세요. 실제 출발지는 중계 서버에서 확인합니다.

```bash
ss -tn state established | grep 31338
# 203.0.113.10:31338   198.51.100.7:56432
```

### 재시도 루프와 수락 쿼터

임플란트가 재시도 루프를 돌면 리스너를 열어둔 만큼 세션이 쌓입니다. `listener_start`의 `accept` 기본값이 1인 이유입니다.

```
listener_start(31339)            # 1개 받고 자동 중단
listener_start(31339, accept=3)  # 3개
```

리스너를 닫아둔 상태에서 타겟이 접속하면, 중계 서버의 sshd가 연결은 받지만 작업 머신으로 넘기지 못해 **즉시 끊깁니다.** 타겟 쪽에도 프로세스가 쌓이지 않습니다.

### 터널이 끊기면

세션도 함께 끊깁니다. 터널을 다시 띄우고 `listener_start`를 하면 타겟이 재시도 주기 안에 다시 붙습니다.

장시간 작업이면 `autossh`를 쓰거나 systemd user 유닛으로 관리하세요.

```bash
autossh -M 0 -p 31337 -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -R 0.0.0.0:31338:localhost:31339 \
    user@203.0.113.10
```

---

## 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| `ssh: connect to host ... port 22: Connection timed out` | SSH 포트가 22가 아니거나 미포워딩 | `-p <포트>` 지정 |
| 터널이 즉시 종료됨 | `GatewayPorts` 꺼짐 (`ExitOnForwardFailure`가 잡아냄) | 1단계 수행 |
| 중계 서버에 `127.0.0.1:31338`로만 바인드 | 같은 원인, `ExitOnForwardFailure` 미지정 | 1단계 + 옵션 추가 |
| `Warning: remote port forwarding failed` | 중계 서버의 31338을 다른 프로세스가 점유 | `ss -tlnp \| grep 31338` 로 확인 후 정리 |
| 타겟은 접속하는데 세션이 안 생김 | 작업 머신에 리스너가 없거나 쿼터 소진 | `session_list()`의 `listener` 확인 |
| 세션이 붙었다 바로 끊김 | 리스너가 닫힌 상태 | `listener_start` 재호출 |

---

## 대안

| 방법 | 장점 | 단점 |
|---|---|---|
| **SSH 역터널** | 작업 맥락 유지, 설정 변경 최소, 되돌리기 쉬움 | 중계 서버 SSH 접근 필요, 터널 유지 관리 |
| 중계 서버에서 클라이언트 실행 | 가장 단순 | **작업 맥락이 그쪽에 갇힘**, 인증 없는 핸들러 노출 |
| 타겟과 같은 LAN에 작업 머신 배치 | 인터넷 노출 없음 | 물리적 제약, WSL이면 NAT 우회 설정 필요 |

### WSL2 사용자

WSL은 기본적으로 NAT 뒤에 있어, WSL 안에서 `0.0.0.0`에 바인드해도 LAN에서 보이지 않습니다. **역터널을 쓰면 이 문제가 아예 발생하지 않습니다** — 리스너가 루프백이면 되니까요.

같은 LAN 방식으로 가야 한다면 `.wslconfig`에 `networkingMode=mirrored`를 넣고 `wsl --shutdown` 해야 하며, **Hyper-V 전용 방화벽 계층**이 별도로 인바운드를 막고 있으므로 그것도 함께 열어야 합니다.

```powershell
Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore | Select Name, DefaultInboundAction
# DefaultInboundAction 이 Block 이면 인바운드가 막힙니다
```
