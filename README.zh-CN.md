# ai-potluck

给社区拼 AI 额度的信任层。贡献者在自己机器上跑 Connector 共享闲置
模型调用能力，上游凭据不出本机；消费者按用量付 credit；每笔消耗都有签名收据 +
透明日志，可离线核验。

本仓库是客户端：`potluck` 命令、Connector，以及协议与验证核心。模块划分只有一条
规则——协议与验证原语放在 ai-potluck，客户端无需信任中心即可核验收据；中心记账与
网关放在伴生仓库 [`ai-potluck-server`](../ai-potluck-server)（`potluck-server`），
它依赖本包，并从 ai-potluck 导入验证原语来做自己的账。

*[English](README.md)*

## 支持环境

首批只构建、测试并支持下列组合：

| 目标 id | 系统 / 架构 | CPython |
|---|---|---|
| `macos_arm64` | macOS 11 及以上，Apple Silicon（arm64） | 3.11 / 3.12 / 3.13 |
| `linux_x86_64` | Linux x86_64，glibc 2.17 及以上 | 3.11 / 3.12 / 3.13 |
| `linux_arm64` | Linux arm64，glibc 2.17 及以上 | 3.11 / 3.12 / 3.13 |

本批明确**不支持**：

- Windows 原生 —— 请用 WSL2；安装脚本是 POSIX `sh`。
- Intel（x86_64）Mac。
- 低于 3.11 或高于 3.13 的 CPython。`pyproject.toml` 写的是 `>=3.11`，那说明的是
  代码**能跑**的范围；上表说明的是发布**已验证**的范围，`install.sh` 会拒绝表外
  的版本。
- PyPy、GraalPy 等非 CPython 解释器。

这张表就在 `scripts/support_matrix.py` 里，`install.sh` 在创建任何文件**之前**先查
它；`tests/test_release_support_matrix.py` 会在本节与那张表不一致时失败。不在支持
范围内的机器会得到矩阵和具体的下一步，而不是一个装了一半的目录。

## 安装

### 一条命令（推荐）

需要 [uv](https://docs.astral.sh/uv/) 和上表中的一个 CPython。在 checkout 里：

```bash
git clone <repo-url> && cd ai-potluck
./install.sh
```

`install.sh` 先按上表检查本机，然后在 `~/.aipotluck` 下建一个自包含虚拟环境，把包
装进去，打印版本号和下一步命令。脚本提示时把 `~/.aipotluck/bin` 加进 `PATH`。依赖从
包索引解析，所以这一步需要联网；失败时原样打印 uv 的输出并非零退出，不会留下一个
半装的 prefix。

常用变体：

```bash
./install.sh --python 3.11        # 指定 CPython 小版本
./install.sh --prefix ~/tools/potluck
./install.sh --preflight          # 只跑兼容性检查，不安装
./install.sh --uninstall          # 删除 venv 与启动器，保留 config/keys
./install.sh --source "ai-potluck @ git+https://HOST/REPO@v0.1.0"
```

升级就是再跑一次同一条命令：虚拟环境整体重建（里面没有用户数据），而
`~/.aipotluck/config.json` 与 `~/.aipotluck/keys`——你的身份与连接配置——永远不动。

### 源码（开发）

```bash
cd ai-potluck
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
.venv/bin/potluck version
```

## 5 分钟消费者

找运营者要一个邀请码，然后：

```bash
potluck join --center http://host:port --invite CODE
potluck ask "你的问题" --stream
```

`join` 在本机生成 Ed25519 密钥（0600），在中心兑换邀请码，把身份存到
`~/.aipotluck`——之后的命令都不需要再配置。`ask --stream` 逐段显示回答。真实输出
如下（回答文本与数字为示意）：

```
potluck: looking for available nodes
potluck: generating
<the answer, printed as it is generated>
potluck: done
potluck: cost 38 milli-credit (38 tokens, receipt R-1)
```

费用行列出具 tokens 数与记录这笔费用的收据。不加 `--stream` 时为缓冲模式：先显示
`waiting for generation...`，再一次性打印完整回答——不会冒充流式。加 `--json` 输出单个机器可读
文档。

## 5 分钟贡献者

你共享的是被允许转售的额度——见下方[供给合规](#供给合规)。找运营者要一个
`node` 邀请码，然后：

```bash
potluck join --node --center http://host:port --invite CODE \
  --upstream-endpoint https://api.example/v1/chat/completions \
  --upstream-model MODEL \
  --upstream-key-file ./key

potluck share run       # 开始共享（前台运行；Ctrl-C 停止）
potluck share pause     # 停止接新任务；在途任务正常结束
potluck share resume    # 重新接单
potluck share status    # 脱敏的本机视图
```

`--upstream-endpoint` 必须是**含 path 的完整 URL**，即上游接受 chat-completions
`POST` 的具体地址（例如 `https://api.example/v1/chat/completions`），不能只填
base URL——connector 会直接 POST 到你给的地址。

本机行为：

- 上游 key 只存在本机 `node.json`（0600），不上传、不打印。
- 向导自检只做 TCP 连通性探测（上游与中心），不发送凭据；加
  `--probe-upstream-live` 才会向上游发一次真实、计费的请求。
- 密钥从文件或环境变量（`AIPOTLUCK_UPSTREAM_KEY`）读取，不走命令行参数——命令行
  参数对同机所有进程可见。

## 运行自己的中心

中心是单进程 + 内嵌 SQLite，不需要外部数据库。中心随伴生仓库
`ai-potluck-server` 发布，它依赖本包。部署（TLS、守护进程、隧道）见
[docs/center-deployment.md](docs/center-deployment.md)。

```bash
uv pip install ai-potluck-server   # 或：-e <ai-potluck-server checkout>
potluck-server serve --config center.json
```

邀请码必须在中心机上签发（那里有 store 文件）：

```bash
potluck-server admin invite --config center.json --role consumer --tenant friends
potluck-server admin invite --config center.json --role node --tenant friends
```

邀请码只显示一次，中心只存摘要。

## 信任模型

- 一笔结算产生一张收据：基于贡献设备签名的用量报告生成，并追加进 Merkle 透明
  日志——两者都可离线核验。
- 当前所有用量记录的证据等级都是 `node_reported`：签名只证明**谁报的**，不证明
  上游真消耗了这些 token。
- 贡献者在本机执行推理，能看到所有流经它的 prompt。
- 租户是“读收据”的信任组：`GET /v1/receipts/{id}` 对该收据所属租户内的任何消费者
  凭据都放行（含 postings），所以账目必须互相隔离的消费者要邀请进不同租户。节点
  自己的派发历史作用域更紧——按凭据绑定的设备。

## 核对自己的记录

三个只读命令，让每一侧都能确认其他侧没有悄悄丢失或篡改记录。它们都不写任何
数据、也不冻结任何账户：只报出差异、指名具体记录，处置由你决定。三个命令也都
不建库：`database_path` 指向的库不存在时报错并退出 1——空库一行记录都没有，否则
一个整体丢失的库会被读成“无差异”。

```bash
# 消费者——用 `join` 保存的身份经 HTTP 核验一张收据（配置目录默认 ~/.aipotluck）
potluck verify receipt --receipt-id R-1
potluck verify receipt --receipt-id R-1 --json

# 运营者——对中心自己的数据库做同一套核验；中心还存有签名的用量报告，
# 所以五个判定都能评估
potluck-server verify receipt --config center.json --receipt-id R-1

# 贡献者——把 node.db 与中心派发给本节点的 attempt 对账
# （节点配置默认 ~/.aipotluck/node.json）
potluck share reconcile

# 运营者——把 attempts、settlements、receipts、透明日志叶子与账本重放日志
# 双向交叉核对。--config 必填：这个命令读的是中心自己的数据库
potluck-server admin reconcile --config center.json
```

`verify receipt` 分开报告五个判定——`signature_valid`、`metering_provenance`、
`allocation_valid`、`ledger_included`、`account_history_complete`——因为它们会各自
独立失败，而一个笼统的绿灯会被误读成“上游真的消耗了这些 token”的证明。
`all_pass` 只是这五个判定的汇总展示，不是第六个判定。`unknown` 表示“两边都没有
证据”，不是矛盾，因此不会让命令失败：消费者侧的 `metering_provenance` 永远是
`unknown`，因为消费者手上没有签名的用量报告。两个对账命令在记录一致时退出 0，
存在任何差异时退出 1。

消费者经 HTTP 核验时，平台结算公钥来自提供收据的同一个中心——这能发现日志丢失
或乱序，但发现不了“中心重写历史后重新签名”。把 `GET /v1/checkpoint` 的
`public_key_b64` 带外固定下来，才能封住这个缺口。

## 供给合规

本项目只支持 L1 白名单供给：自有 API key、自部署模型、或合同允许你转售的企业
额度。共享个人包月订阅通常违反上游服务条款；本项目默认不支持，用户自负其责。

## 当前限制

- 邀请制加入。
- 无充值、无提现：credit 由运营者铸造，无法兑现。
- 单中心、单实例——无联邦、无高可用。
- 只支持[支持环境](#支持环境)里列出的组合；原生不支持 Windows，请用 WSL2。

## 开发

```bash
cd ai-potluck
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"

.venv/bin/python -m pytest tests/ -q --basetemp=.pytest_basetemp
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
```

端到端发布门禁——真实安装 + 默认 join/ask 路径 + 真实中心——在
`ai-potluck-server` 仓库的 `scripts/release_gate.sh`，因为它同时驱动两个包。

只有在会拦截 pytest 在系统临时目录建目录的沙箱里才需要 `--basetemp`；普通环境
不用带。`mypy` 按 `pyproject.toml` 的配置跑 `--strict`，必须保持 0 警告。

## 文档

- [方案说明与早期用户征集（RFC）](docs/PROPOSAL.zh-CN.md)
- [路线图](docs/ROADMAP.md)
- [Proposal & early-user asks (English)](docs/PROPOSAL.md)
- [网络契约 v0.1](docs/network-contract-v0.1.md)
- [中心部署](docs/center-deployment.md)
