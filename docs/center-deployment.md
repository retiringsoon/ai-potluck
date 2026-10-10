# 中心部署一页纸（社区中心运营者）

> 读者：给朋友社区开一个中心的人。中心就是**一个进程** `potluck-server serve`：HTTP / WebSocket / SSE 监听、领域逻辑、账本全在里面，数据落一个 SQLite 文件，唯一第三方依赖是 `cryptography`。不需要 Postgres、Redis、容器编排、多机集群。
> 依据：《AI共享平台_发布就绪评审_v0.1》§3（中心部署一页纸）与 §7.4（中心服务器运维）。适用范围：单中心、5–50 人、每天几千次调用（§7.4 负载口径）。

## 0. 前置

- 一台**所有成员都可达**的机器：公网 VPS，或内网机器 + Tailscale（§4C）。
- **一个端口**。下文统一用 `8801`；走反代时中心只绑 `127.0.0.1:8801`，对外是 443。
- Python 3.11 或更新（`install.sh` 会自己找；不需要预装）。
- 公网 TLS 路线的可选前提：一个域名。

## 1. 安装（发布包，不是源码）

```sh
# 解开发布包（含 install.sh、两个包的 .whl、wheels/）
cd <解压目录>/
./install.sh                          # 默认装到 ~/.aipotluck（客户端 potluck）
# 中心机再把服务端包装进同一个 venv（离线时加 --no-index --find-links wheels/）：
~/.aipotluck/venv/bin/pip install ai_potluck_server-*.whl
ln -sf ~/.aipotluck/venv/bin/potluck-server ~/.aipotluck/bin/potluck-server
export PATH="$HOME/.aipotluck/bin:$PATH"
potluck version && potluck-server version
```

- `install.sh` 只创建 `$PREFIX/venv` 与 `$PREFIX/bin/potluck`（`$PREFIX` 默认 `~/.aipotluck`）；`potluck-server` 来自 `ai-potluck-server` 包，按上面装进同一个 venv 并链接到 `$PREFIX/bin`。`config.json`、`keys/` 是用户数据，脚本从不改写。
- **升级 = 解新包、再跑一次 `install.sh`**：它复用同一个 venv 做包替换，不碰 `config.json` / `keys/`，也不碰你自己放的 `center.json` / `center.db`。详见 §8。
- Windows：`install.sh` 是 POSIX sh，请用 WSL。

## 2. 目录与结算密钥

```sh
mkdir -p ~/center && chmod 700 ~/center
cd ~/center
potluck keygen --out settlement.key    # 私钥写文件（mode 0600）；stdout 第一行是配套公钥
```

`settlement.key` 的文件内容（一整行 base64 Ed25519 私钥）就是下面 `settlement_private_key_b64` 的值。把 `keygen` 打印的**公钥**连同你选的 `settlement_key_id` 另存一份——离线核验历史收据需要它（§10）。

## 3. 最小 center.json

字段名与 `ai-potluck-server` 包 `deploy.py` 的 `load_center` 逐一对上（该函数是唯一权威解析处）：

```json
{
  "host": "127.0.0.1",
  "port": 8801,
  "database_path": "/home/you/center/center.db",
  "platform_id": "platform-friends",
  "settlement_key_id": "settlement-key-1",
  "settlement_private_key_b64": "<settlement.key 文件内容，一整行>",
  "certfile": null,
  "keyfile": null,
  "credentials": [],
  "providers": [],
  "consumer_floats": []
}
```

```sh
chmod 600 center.json
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `host` / `port` | str / int | 监听地址与端口。走反代用 `127.0.0.1`；进程自带 TLS 用 `0.0.0.0` + 443 |
| `database_path` | str | SQLite 文件路径。**写绝对路径**：相对路径按进程 CWD 解析，`serve` 与 `admin` 在不同目录执行会开到两个不同的库 |
| `platform_id` | str | 本中心标识，写进签名检查点 |
| `settlement_key_id` | str | 结算密钥的 key id，收据与检查点用它作 `signer_key_id` |
| `settlement_private_key_b64` | str | 结算私钥。启动时即校验，非法直接拒绝启动（fail-closed） |
| `certfile` / `keyfile` | str 或 null | 进程自带 TLS：两个都填 PEM 路径，或**都为 null**（明文 HTTP）。只给其中一个会被拒 |
| `credentials` | 数组 | 预置 token 凭据。**留空 = 邀请制动态注册** |
| `providers` | 数组 | 预置节点设备公钥 + 演示额度。留空，节点走 `join --node` 动态注册 |
| `consumer_floats` | 数组 | 预铸演示额度。留空 |

- 三个数组都可以是 `[]`，这就是邀请制的含义：成员不写进配置，靠 `admin invite` + `join` 动态注册（§7）。重启时中心会把这些动态身份从库里重放回注册表，所以邀请加入的人重启后照样能认证。
- 文件按严格 JSON 解析：不允许浮点数、重复键或 `NaN`。

## 4. 让成员可达 + TLS

中心**只有一个监听端口**：消费者走 `https://<host>:<port>`，节点走 `wss://<host>:<port>/v1/node-link`，SSE 走 `https://<host>:<port>/v1/requests/{id}/events`——全在这一个 socket 上。谁终结 TLS，决定你选哪条路线。

### 4A 进程自带证书（`center.json` 的 `certfile`/`keyfile`）

签发并放置证书（以 Let's Encrypt 为例）：

```sh
sudo certbot certonly --standalone -d center.example.com
# 得到 /etc/letsencrypt/live/center.example.com/fullchain.pem 与 privkey.pem
```

```json
"host": "0.0.0.0", "port": 443,
"certfile": "/etc/letsencrypt/live/center.example.com/fullchain.pem",
"keyfile":  "/etc/letsencrypt/live/center.example.com/privkey.pem",
```

要点：

- 两个字段**要么都给、要么都为 null**（`load_center` 强制）。填了以后中心在同一个端口上直接说 TLS；启动日志会打印 `https://…` 与 `wss://…`，`/v1/node-link` 的 WebSocket 升级也在同一个端口上，不需要第二个端口。
- 运行 `serve` 的用户必须能读私钥文件（Let's Encrypt 的 `live/` 目录默认 root-only：要么以 root 运行，要么用 `--deploy-hook` 把证书复制到服务用户可读且 mode 0600 的位置）。
- 证书换新后 **TLS 只在进程启动时读入，必须重启**：
  ```sh
  certbot renew --deploy-hook "systemctl restart potluck-center"
  ```
- 自签证书不可用：客户端（节点与消费者）默认按系统信任链校验证书与主机名，自签会被拒。
- 走这条路，成员地址里直接写公网名：节点 `center_url` 用 `wss://center.example.com/v1/node-link`，消费者 `center_url` 用 `https://center.example.com`。

### 4B 反向代理终结 TLS（公网 VPS 推荐，可顺带限流）

中心保持明文，只绑回环；证书、续期、限流都交给反代：

```json
"host": "127.0.0.1", "port": 8801,
"certfile": null, "keyfile": null,
```

**Caddy（最小可用，自动 HTTPS）**

```
# Caddyfile
center.example.com {
    reverse_proxy 127.0.0.1:8801 {
        flush_interval -1
    }
}
```

- `center.example.com` 一行即自动申请/续期 HTTPS 证书。
- Caddy v2 默认透传 WebSocket 升级，`/v1/node-link` 不需要额外配置。
- `flush_interval -1` 关掉响应缓冲：**SSE 长连接必须关缓冲**，否则事件会被攒着不推。中心自己每 15s 发一个注释心跳防中间设备掐空闲连接，关掉缓冲后这个心跳才能生效。
- 启动：`caddy run --config Caddyfile`，或用 Caddy 发行包自带的 systemd unit。

**nginx（关键配置点）**

```nginx
# 顶部：WebSocket 升级需要一个 map
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name center.example.com;
    ssl_certificate     /etc/letsencrypt/live/center.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/center.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8801;
        proxy_http_version 1.1;                        # WebSocket 必须 1.1
        proxy_set_header Upgrade $http_upgrade;        # 透传升级
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        proxy_buffering off;                           # SSE 必须关缓冲
        proxy_read_timeout 3600s;                      # 长连接/SSE 不被掐断
        proxy_send_timeout 3600s;
    }
}
```

关键点逐条：

1. `proxy_http_version 1.1` + `Upgrade`/`Connection` 透传——少一个 `/v1/node-link` 就升级失败，节点永远连不上。
2. `proxy_buffering off`（或由上游发 `X-Accel-Buffering: no`）——否则 `/v1/requests/{id}/events` 的 SSE 不流式。
3. `proxy_read_timeout` 拉长——节点长连接与 SSE 都是长连接，默认 60s 会被掐。
4. 服务端**没有任何速率限制**（§11），公网请在这里补一层：`limit_req_zone` 对 `POST /v1/join` 与 `POST /v1/requests` 限速。要让限流按真实客户端 IP 生效，还需要 `set_real_ip_from <反代地址>; real_ip_header X-Forwarded-For;`。
5. 走这条路，成员地址用公开名（`https://center.example.com` / `wss://center.example.com/v1/node-link`），后端端口不对外。

### 4C Tailscale（不想在公网暴露时）

最省事的一种"成员可达"：

```sh
# 中心机与每位成员各装 Tailscale、加入同一 tailnet
# 中心仍只绑回环，由 tailscale 转发
tailscale serve --bg 8801          # 在 tailnet 名上提供 HTTPS，转发到本机 127.0.0.1:8801（需较新的 tailscale）
```

- `center.json` 保持 `host: "127.0.0.1"`、`certfile/keyfile: null`。
- 成员地址用 tailnet 名字（`https://<机器>.<tailnet>.ts.net`）；Tailscale 自带证书，客户端信任链能过。
- 也可以用 `tailscale cert <机器>.<tailnet>.ts.net` 拿到真实证书，改走 §4A。
- 这条路天然只对 tailnet 成员开放，不需要公网端口、不需要自己管证书；代价是每位成员都要装 Tailscale。

## 5. systemd unit（Linux）

```ini
# /etc/systemd/system/potluck-center.service
[Unit]
Description=AI Share center gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=potluck
Group=potluck
WorkingDirectory=/home/aipotluck/center
ExecStart=/home/aipotluck/.aipotluck/bin/potluck-server serve --config /home/aipotluck/center/center.json
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now potluck-center
journalctl -u potluck-center -f
```

要点：`potluck-server serve` 前台阻塞直到 SIGTERM，配 `Type=simple`；`Restart=always` 覆盖崩溃与 OOM；`WorkingDirectory` 与 `center.json` 里的 `database_path` 都指向同一目录（用绝对路径最稳）；`UMask=0077` 让进程写出的 `center.db` 及其 `-wal`/`-shm` 只对本用户可读。

## 6. launchd（macOS）

```xml
<!-- ~/Library/LaunchAgents/com.example.potluck-center.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.potluck-center</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.aipotluck/bin/potluck</string>
    <string>serve</string>
    <string>--config</string>
    <string>/Users/you/center/center.json</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/you/center</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/you/center/center.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/center/center.err.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.potluck-center.plist
launchctl kickstart -k gui/$(id -u)/com.example.potluck-center
launchctl bootout   gui/$(id -u)/com.example.potluck-center
```

要点：`KeepAlive` 等价于 systemd 的 `Restart=always`；`WorkingDirectory` 必须是 `center.json` 与库文件所在目录；macOS 请在"节能"里关掉自动休眠，或用 `caffeinate` 包住（休眠会断所有连接）。

## 7. 邀请与日常运营

**`admin invite` 必须在中心机上执行**：它不走网络，而是直接打开 `center.json` 里 `database_path` 指向的那一个 SQLite 文件（`cli.py` 的 `_load_center_store` → `CenterStore.open(database_path)`）。换句话说，谁能执行它，谁就拥有中心机的文件系统访问权；它**没有远程版本**，这也是设计上的安全边界。

```sh
ssh center
potluck-server admin invite --config ~/center/center.json --role consumer --tenant friends
potluck-server admin invite --config ~/center/center.json --role node            # 邀请一个贡献节点
```

- 选项默认值：`--role consumer`（或 `node`）、`--tenant auto`（每位加入者一个服务器派生的私有租户）、`--quota 20000`（首次加入铸入的 milli-credit）、`--expires-in 7d`、`--max-uses 1`。
- 邀请码只在 stdout 打印一次，库里只存摘要；丢了就重新开一个。
- `admin invite-list`（脱敏列表）、`admin revoke-key <key_id>`（撤销，立即在运行中的中心生效，无需重启）。
- `serve` 正在运行时也可以执行这些命令：SQLite 用 WAL + 5s busy_timeout，读写互不阻塞。
- 把码给朋友：消费者 `potluck join --center https://center.example.com --invite <码>`；节点 `potluck join --center … --invite <码> --node`（`--node` 会顺带跑贡献者向导）。

## 8. 升级

1. 备份（§9）：`center.json` + `center.db` 在线备份。
2. 解新发布包，重跑 `./install.sh --prefix ~/.aipotluck`，中心机再用同一 venv 的 pip 替换 `ai-potluck-server` 包——配置与密钥都不动。
3. 重启服务：`sudo systemctl restart potluck-center`（或 `launchctl kickstart -k …`）。
4. 起来后看日志首屏（绑定的 URL、provisioned provider 列表）与 `curl` 健康检查（§11）。

回退：换成上一个发布包再跑一次 `install.sh`。库不需要手工迁移：打开时 `CenterStore` 会自动补齐旧库缺的列（`ai-potluck-server` 的 `persistence.py` 中 `_migrate`），已有数据不动；但仍然先按 §9 备份再升级。

## 9. 备份与恢复

**运行中不要直接 `cp center.db`。** 库是 WAL 模式，活库的写入可能还在 `center.db-wal` 里，单拷主文件会得到一个回退到某个旧检查点、甚至不自洽的副本。用 SQLite 的在线备份：

```sh
# 手动/演练
sqlite3 /home/you/center/center.db ".backup '/home/you/backup/center-$(date +%F-%H%M).db'"
```

```cron
# crontab -e：每天 03:15 在线备份，保留 14 天
15 3 * * * /usr/bin/sqlite3 /home/you/center/center.db ".backup '/home/you/backup/center-$(date +\%F).db'" && find /home/you/backup -name 'center-*.db' -mtime +14 -delete
```

（等价写法 `VACUUM INTO '…'` 也可以；两者都是对活库做一致性快照。）

另外**离机备份 `center.json`**：它含结算私钥（§10）。用加密盘或密钥管理工具保存，不要进 git、不要进共享目录。

恢复演练（建议每月一次）：

```sh
sudo systemctl stop potluck-center
mv ~/center/center.db ~/center/center.db.broken            # 保留现场
rm -f ~/center/center.db-wal ~/center/center.db-shm        # 旧库的 WAL/SHM 不能留给新库
sqlite3 ~/backup/center-2026-10-09.db ".backup '$HOME/center/center.db'"   # 或直接 cp 备份文件（它已静止）
# 如需改动配置：恢复对应的 center.json（私钥必须与库中历史收据的 signer_key_id 一致）
sudo systemctl start potluck-center
curl -s http://127.0.0.1:8801/healthz                      # {"status":"ok"}
potluck-server admin invite-list --config ~/center/center.json     # 能读、邀请仍在
# 若在 credentials 里预置了 consumer token（空数组的邀请制部署可跳过这行）：
curl -s http://127.0.0.1:8801/v1/checkpoint -H "Authorization: Bearer <consumer token>"   # tree_size 与备份时间点相符
```

恢复后确认：邀请可读、`GET /v1/checkpoint` 的 `tree_size`/`root_hash` 与备份时间点一致（需一个消费者角色凭据，见 §11）、老收据仍能取到（`GET /v1/receipts/{id}`）。

## 10. 结算私钥与文件权限

- `chmod 600 center.json`、`chmod 700 ~/center`；整盘加密或至少把 `center.json` 放在加密卷上。
- **泄漏的影响**：拿到私钥的人可以用这个 `settlement_key_id` 签出以假乱真的收据与检查点——这是信任根，按"密钥已失陷"处理：用 `keygen` 生成新密钥、换一个新 `settlement_key_id`、重启，并在社区里说明旧 key_id 作废。
- **丢失的影响**：历史收据仍然可核验（只要你还留着 `keygen` 打印的**公钥**与 `settlement_key_id`：收据里带 `signer_key_id`，核验只用公钥，例如 `potluck verify --public-key …`），但**新收据再也签不出来**——`serve` 启动时会拒绝启动（`_provision_settlement_key` fail-closed），中心就此停摆。所以私钥要备份，且备份本身要当机密保管。
- 想彻底不留私钥在盘上，就给 `center.json` 用 0600 + 加密卷，并把这个文件排除在任何同步、备份到云端明文的流程之外。

## 11. 监控、负载与暴露警告

- 健康检查：`GET /healthz`（无需鉴权）返回 `{"status":"ok"}`。systemd/launchd 之外，再加一个外部探针（`curl -fsS` + 告警）。
- 运维视图：`GET /v1/nodes`（消费者角色凭据：consumer token 或 `AIPotluck-Sig` 签名）给出在线节点与能力；`GET /v1/checkpoint` 给出最新签名检查点（可用作"账本还在长"的指标）。
- **不要裸暴露到公网。** 服务端是手写的 HTTP/WebSocket/SSE 栈，**没有任何速率限制或 DoS 防护**：`POST /v1/join`、`POST /v1/requests` 都接受无上限的请求速率（邀请码本身 192 bit，爆破不可行，但恶意刷请求会烧 CPU）。公网部署必须二选一：
  1. 前面套反代，终结 TLS 并在反代上限流（§4B）；或
  2. 只在 Tailscale 之类的私有网络里可达（§4C）。
- 负载口径（§7.4）：每连接一个线程、SSE 队列有界（消费者 64 / 节点 512）、SQLite 单写者，5–50 人/每天几千次调用绰绰有余。**不要为这个量级引入 Postgres**；真到天花板（内存全量重建账本 / Merkle 日志百万级叶子、单进程 GIL）还差几个数量级。

## 12. 机器规格（以阿里云 ECS 为例）

| 量级 | 规格 | 说明 |
|---|---|---|
| 3–5 人试用 | 1C1G ~ 2C2G 共享型（如 ecs.e/t6，¥50–100/月） | 进程常驻 RSS 很低；SQLite 每笔结算只写几行 |
| 20–50 人、每日几百次调用 | **2C2G ~ 2C4G + 3–5Mbps 固定带宽或按量付费**（¥100–200/月） | 推荐档；CPU 大部分时间空闲，流式中继是 IO 型 |
| 磁盘 | 40GB ESSD 够用数年 | 每笔结算落库几 KB；另留 WAL 与备份空间 |

### 大型圈层（~100 人 / 数百并发长流）：先看三个真瓶颈，再谈机器

这个量级（如 100 人 × 5 个并发 agent 会话，每会话 1 小时）**超出了当前实现的已验证包络**，且瓶颈顺序与直觉不同：

1. **供给侧（最先到顶）**：500 路并发流 = 500 个同时的上游调用。默认节点上限是并发 1 / 执行 2——意味着要么 250–500 台贡献机，要么调高上限；而上游组织的并发/TPM 限速才是真正的稀缺资源。**平台的稀缺品是上游并发，从来不是中心算力**。
2. **中心写路径**：正确性设计（网络契约 §6 块级原子发布）使得**每个输出增量都是独立 fsync 事务**（WAL + 默认 synchronous=FULL 单写者）。500 流 ≈ 每秒数千个持久事务，超过单写者 fsync 上限（云盘约数百到 ~2000 tx/s）。解决办法是工程活：同流增量合批提交（保原子性）、`synchronous=NORMAL`（崩溃窗口需重审，非免费开关）、本地 NVMe（fsync 快 2–5×），或 ADR001 预留的传输层重写。堆 CPU 不解决 fsync 单写者。
3. **并发会话结构**：500 个消费者 SSE 会话 + 每会话一线程在 GIL 下开始抖动；增量还会逐条落 events 表（长会话可达 GB/天级），备份体积要重算。

**零代码的横向扩展方案**：架构天然按社区自治（全部状态在单中心内），100 人 = 4 个社区 × 25 人 = 4 个 2C4G 中心。把社区当分片单元，比单体扩容便宜且不碰正确性设计。

在该量级运行前需要：真实负载压测（伪造 N 节点 + N 消费者 SSE 流）、增量合批评估、带宽成本核算（文本中继平均 1–2Mbps，但按量计费下长会话累计不可忽略）。

带宽口径：中心只中继**文本增量**，一条 LLM 回答通常几 KB；100 路并发流也就几十 KB/s，1Mbps 都能跑，但新连接建立与轮询有突发，建议 3–5Mbps 或按量。备份到 OSS 加一条 cron 即可（§9）。

地域与合规要点：

- **中心在大陆节点完全可以**——架构上中心不调用任何上游（出站只有对成员的响应），需要访问 OpenAI/Anthropic 的是贡献者自己的机器，各自解决各自的网络；
- 绑域名对外提供 HTTP 服务（80/443）按大陆规定需 ICP 备案；用香港/海外地域、或纯 `IP:端口`、或 Tailscale 内网则不涉及，按你的成员分布选；
- 安全组只需放行：中心端口（建议 443）+ SSH；
- 这是单点：进程挂了社区停摆（账务无损，重启自动恢复），`/healthz` 外部探针 + 每日备份是最低配置。

## 13. 企业网络下的可达性（贡献者/消费者侧）

线协议形态决定被拦截的概率。三段连接的画像：

| 连接 | 形态 | 与普通流量的差异 |
|---|---|---|
| 消费者 → 中心 | 标准 HTTPS REST + SSE 长连接 | 几乎无差异：POST JSON + text/event-stream，任意 Web API 都长这样 |
| 贡献者 → 中心 | 出站 WSS（HTTP/1.1 Upgrade 后转 WebSocket，15s 心跳保活 + 退避重连） | 同 Slack/Teams/VS Code 隧道一类长连接，主流企业出口普遍放行 |
| 贡献者 → 上游 | 标准 HTTPS 到知名 API 域名 | 与任何 AI 应用使用无差异 |

真正会触发拦截的是三件事，都有标准解法：

1. **非标准端口**（如 `:8801`）：出口防火墙最常拦的就是非常见端口。**用 443 + 反代或进程自带证书**，与普通网站不可区分；
2. **自签证书**：TLS 握手失败比被墙更像被墙。公网部署用 Let's Encrypt 或云厂商证书；
3. **企业 TLS 深度检测（装了公司根证书做 MITM）**：这时内容对安全网关可见，未知 API + 自定义 Authorization 头 + SSE 流可能触发 DLP 审查而非拦截。这属于公司策略问题：要么找 IT 把域名加白，要么走 Tailscale（同样需要批准）。**不要未经批准在公司网里跑贡献节点**——那不是技术问题，是政策问题；自家网络无此顾虑。

残余风险（诚实口径）：Python TLS 栈的指纹（JA3 类）与自定义路径理论上可被主动指纹识别，但常规企业出口按域名/端口/证书策略拦截，不做主动指纹筛查。SSE 在个别老代理上会被缓冲，消费者可退回缓冲模式（不带 `--stream`）。

### 大陆不备案的可行姿势

ICP 备案绑定的是「域名 + 大陆服务器上的 web 服务」，裸 IP 访问不触发备案，但拿不到正规 CA 证书（自签=客户端不信任），公网明文不可取。两条正解：

1. **私网化（推荐）**：Tailscale/WireGuard，中心无任何公网端口——无备案问题、攻击面最小、本来就是私有社区的最佳姿势；隧道内明文 HTTP 也可接受（仍建议中心开 TLS）。
2. **香港/海外地域 + 域名 + Let's Encrypt**：无备案要求，443 全开，任何人可直接连接。延迟：大陆到香港约 30–60ms、到日本约 50–120ms——流式文本与 agent 任务对此完全无感（LLM 出 token 的节奏远慢于这个数）。代价：跨境带宽单价更高、晚高峰质量看运营商线路。注意中心不需要访问任何上游（架构如此），所以海外中心不存在上游可达性问题。
