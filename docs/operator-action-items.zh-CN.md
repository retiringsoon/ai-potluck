# AI Potluck Operator 待办事项与架构决策手册 (Action Items & Decisions)

> 本文档记录所有需要 Operator（项目负责人）手动操作的注册与申请事项，以及需要拍板的技术架构与产品路线决策。
> 当前状态：针对 **v0.3.0-dev**，保存在 `dev` 分支，供 Operator 随时查阅与逐项推进。

---

## 目录
1. [第一部分：Operator 手动执行清单（必办账号与配置）](#第一部分operator-手动执行清单)
   - [1.1 Telegram Bot 与社区频道搭建](#11-telegram-bot-与社区频道搭建)
   - [1.2 飞书企业自建应用与事件订阅](#12-飞书企业自建应用与事件订阅)
   - [1.3 Discord 开发者后台与服务器搭建](#13-discord-开发者后台与服务器搭建)
   - [1.4 GitHub Discussions 运营初始化](#14-github-discussions-运营初始化)
   - [1.5 小红书与微信社群准备](#15-小红书与微信社群准备)
2. [第二部分：核心架构与产品战略决策清单（需决策）](#第二部分核心架构与产品战略决策清单)
   - [决策 1：长程 Agent 任务中的 Tool Calling (函数调用) 演进路线](#决策-1长程-agent-任务中的-tool-calling-函数调用演进路线)
   - [决策 2：超长任务 (504 超时后) 的异步结果取回形态](#决策-2超长任务-504-超时后的异步结果取回形态)
   - [决策 3：贡献节点多模型 (Multi-Model) 共存架构](#决策-3贡献节点多模型-multi-model-共存架构)
   - [决策 4：社区自动化多平台 Bot 统一守护进程架构](#决策-4社区自动化多平台-bot-统一守护进程架构)
   - [决策 5：早期社区信用额度与代币激励经济模型](#决策-5早期社区信用额度与代币激励经济模型)
3. [第三部分：本次长程任务支持升级落地总结（已完成）](#第三部分本次长程任务支持升级落地总结)

---

## 第一部分：Operator 手动执行清单

> **提示**：本节列出的所有平台均**完全免费**即可搭建并运营 AI Agent。你无需付费购买企业版或高级版。

### 1.1 Telegram Bot 与社区频道搭建
- [ ] **创建 Bot 账号**：
  1. 在 Telegram 中搜索官方机器人 `@BotFather`。
  2. 发送 `/newbot`，按提示输入 Bot 昵称（如 `AI Potluck Community Bot`）以及用户名（如 `aipotluck_bot` 或 `ai_potluck_helper_bot`）。
  3. 保存生成的 `HTTP API Token`（格式如 `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`）。
- [ ] **创建社群与设置管理员**：
  1. 在 Telegram 创建 Supergroup（超级群，如 `AI Potluck Community`）或 Channel。
  2. 将新创建的 Bot 拉入群组，并将其升级为 **Admin**（开启 `Delete messages`, `Pin messages`, `Invite users via link` 权限）。
  3. 关闭群组 Bot 的 Group Privacy（在 `@BotFather` 中发送 `/setprivacy` -> 选择你的 Bot -> 设为 `Disable`），这样 Bot 可以在群里接收非 `@` 命令或进行全自动答疑。
- [ ] **本地验证测试**：
  在终端中测试运行（使用我们已准备好的脚本）：
  ```bash
  export TELEGRAM_BOT_TOKEN="your_token_here"
  uv run python scripts/potluck_community_bot.py --telegram-token "$TELEGRAM_BOT_TOKEN"
  ```

---

### 1.2 飞书企业自建应用与事件订阅
- [ ] **创建自建应用**：
  1. 使用任意飞书个人/企业账号登录 [飞书开放平台](https://open.feishu.cn/)。
  2. 点击「创建自建应用」，名称填写 `AI Potluck 助手`，填写简介并上传图标。
  3. 在「凭证与基础信息」中获取 `App ID` (形如 `cli_a...`) 和 `App Secret`。
- [ ] **配置应用权限**：
  在「开发配置」->「权限管理」中开通以下必要权限：
  - `im:message`（获取与发送单聊/群聊消息）
  - `im:message.group_at_msg`（接收群聊中 @机器人的消息）
  - `im:message:send_as_bot`（以机器人身份发消息）
- [ ] **添加机器人能力并发布版本**：
  1. 在「应用功能」->「添加应用能力」中选择「机器人」。
  2. 进入「版本管理与发布」，创建版本并申请发布。个人测试租户会自动免审通过。
- [ ] **事件订阅配置（用于自动接收消息）**：
  1. 在「事件订阅」中填入公网服务器的回调请求网址（例如部署在已有服务器上的 `https://your-domain.com/webhook/feishu`，或本地测试用 Cloudflare Tunnel / frp 穿透）。
  2. 添加事件：`im.message.receive_v1`（接收消息 v2.0）。
- [ ] **本地验证测试**：
  ```bash
  export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx"
  uv run python scripts/potluck_feishu_bot.py --webhook-url "$FEISHU_WEBHOOK_URL" --test
  ```

---

### 1.3 Discord 开发者后台与服务器搭建
- [ ] **创建 Discord 服务器**：
  1. 打开 Discord，点击左侧「+」创建自己的服务器（如 `AI Potluck Community`）。
  2. 建立常用频道结构：
     - `#announcements`（官方公告）
     - `#general-chat`（社区水吧）
     - `#dev-discussion`（开发与技术交流）
     - `#node-status`（贡献节点状态看板）
     - `#agent-bot-test`（供用户与 AI 机器人交互的测试区）
- [ ] **在 Developer Portal 创建应用与 Bot**：
  1. 登录 [Discord Developer Portal](https://discord.com/developers/applications)。
  2. 点击「New Application」，命名为 `AI Potluck Bot`。
  3. 进入左侧「Bot」菜单，点击「Reset Token」并妥善保存生成的 Bot Token。
  4. **关键开关（必须打开）**：在 Bot 页面向下滚动，找到 **Privileged Gateway Intents**，开启：
     - `PRESENCE INTENT`
     - `SERVER MEMBERS INTENT`
     - `MESSAGE CONTENT INTENT`（极其重要，若不开启，Bot 无法读取用户发送的消息正文）。
- [ ] **生成 OAuth2 邀请链接并拉入服务器**：
  1. 进入左侧「OAuth2」->「URL Generator」。
  2. Scopes 勾选：`bot`, `applications.commands`。
  3. Bot Permissions 勾选：`Send Messages`, `Read Message History`, `Embed Links`, `Attach Files`, `Use Slash Commands`。
  4. 复制生成的 URL 在浏览器打开，选择刚创建的 `AI Potluck Community` 服务器授权加入。
- [ ] **本地验证测试**：
  ```bash
  export DISCORD_BOT_TOKEN="your_bot_token_here"
  uv run python scripts/potluck_discord_bot.py --discord-token "$DISCORD_BOT_TOKEN"
  ```

---

### 1.4 GitHub Discussions 运营初始化
- [ ] **确认与配置分类（Categories）**：
  GitHub Discussions 已由我们通过 GraphQL API 自动开启：
  主库链接：[https://github.com/retiringsoon/ai-potluck/discussions](https://github.com/retiringsoon/ai-potluck/discussions)
  当前已有置顶欢迎帖：`#1 Welcome to AI Potluck Discussions!`
  建议在 Repo Settings -> Discussions 中核实以下 5 个分类：
  - 📢 **Announcements**（官方发布与重大变动，仅 Maintainer 可发帖）
  - 💡 **Ideas & RFCs**（协议演进与新功能提案）
  - 🙏 **Q&A**（节点部署答疑、积分使用求助，可标记 Accepted Answer）
  - 💻 **Show and Tell**（分享你的共享节点配置、工作流集成如 Cursor/Claude Code）
  - 💬 **General**（常规自由交流）

---

### 1.5 小红书与微信社群准备
- [ ] **小红书发帖与排版**：
  - 参考已存放在本地 dev 分支的文档：[`docs/promotion-kit.zh-CN.md`](promotion-kit.zh-CN.md)。
  - 挑选 **方案 A（极客技术向）** 或 **方案 B（算力共享与省钱向）**。
  - 配图建议：
    1. 封面：终端高逼格 ASCII Banner 与 `potluck serve-openai` 启动画面截图。
    2. 图 2：多方双向签名收据（Receipt）与 Merkle 审计链示意图。
    3. 图 3：VSCode/Cursor 或 Claude Code 无缝配置截图（`localhost:8080`）。
- [ ] **微信群聊或交流号**：
  - 如需建立微信交流群，创建一个名为「AI Potluck 极客交流群」的微信群，保存群二维码。
  - 由于微信群二维码 7 天过期，建议同时准备一个微信号（或公众号后台自动回复群二维码），并更新到 promotion kit 中。

---

## 第二部分：核心架构与产品战略决策清单

### 决策 1：长程 Agent 任务中的 Tool Calling (函数调用) 演进路线
- **背景与痛点**：
  Cursor、Claude Code、Cline、AutoGen 等长程 Coding Agent 的核心依赖就是 Tool Calling。Agent 在一次长程任务中会反复发起数十个回合，每个回合都会发出 `tools` 参数，模型返回 `tool_calls`，Agent 运行工具后把带有 `tool_call_id` 的结果传回。
  我们在本次代码升级中，已经让 `ChatMessage` 和 `wire.py` 完整支持了对 `tool_calls`、`tool_call_id`、`name` 历史消息的解析与规范化签名。
- **需要拍板的权衡**：
  目前服务器网关对于下游的 `tools` 参数和流式 `tool_calls` 尚未向客户端吐出（当前只输出单一的 content 文本）。对于模型返回的 tool_calls，有两种演进路线：
  - **选项 A（推荐：协议级对齐 OpenAI 规范）**：
    在 `wire.py` 的 `output.delta` 增加 `tool_calls` 结构载荷（或新增 `tool_call.delta` 事件类型），在 `gateway_server.py` 的流式/非流式响应中，按 OpenAI 标准渲染 `message.tool_calls` 和 `finish_reason: "tool_calls"`。
    *优点*：与 Cursor、Claude Code、LangChain 100% 原生无缝兼容。
    *代价*：需要在 `connector.py` 和 `gateway_server.py` 中增加对 tool_calls 流式累加器的测试。
  - **选项 B（纯文本适配/Markdown 代码块封装）**：
    保持 wire protocol 的纯文本传输，由节点端把 tool_calls 格式化为 Markdown XML `<tool_call>...</tool_call>` 纯文本流，由客户端本地的 `serve-openai` 重新解析并转回 JSON。
    *优点*：无需修改网关和收据计费逻辑。
    *缺点*：非标准，对于非 `serve-openai` 的直接 API Key 调用者不友好。
- **Operator 建议选项**：**采纳选项 A**。在下个小版本中完整完成 `tool_calls` 在 gateway 层的流式渲染。

---

### 决策 2：超长任务 (504 超时后的异步结果取回形态)
- **背景与痛点**：
  长程任务单次推理可能耗时极长（例如 DeepSeek-R1 深度思考 200 秒，或者 Agent 单回合生成大文件）。虽然服务器端 `--deadline-ms` 允许长达数十分钟甚至 24 小时，但客户端与中间的反向代理（如 Nginx、Cloudflare）往往在 60~100 秒就会切断 HTTP 连接，返回 `504 deadline_passed`。
- **需要拍板的权衡**：
  当长程任务在后台继续运行完成并结清收据后，客户端应如何取回答案？
  - **选项 A（提供 OpenAI 兼容的异步轮询端点）**：
    在 `ai-potluck-server` 上提供只读的 `GET /openai/v1/chat/completions/{request_id}` 或 `POST /openai/v1/chat/completions/retrieve`。如果任务已完成，直接返回标准 `chat.completion` 对象；如果在运行中，返回 `202 Accepted`。
  - **选项 B（坚持平台原生 cursor 重连与收据审计）**：
    不改变 OpenAI 表面规范，要求需要极长断线重连的用户使用平台原生的 `potluck ask --resume` 或 `client.open_event_stream(request_id, cursor)`。
- **Operator 建议选项**：**采纳选项 A**。对现代 Agent 框架而言，轮询端点极大降低了网络不稳定时的重试损耗。

---

### 决策 3：贡献节点多模型 (Multi-Model) 共存架构
- **背景与痛点**：
  目前一个 Connector 进程只上报一个 upstream model。但许多算力贡献者（拥有 4090 或多卡服务器）希望同时共享一个快速模型（如 `qwen-2.5-coder-7b`）和一个大推理模型（如 `deepseek-r1`）。
- **需要拍板的权衡**：
  - **方案 A（多进程隔离模式，推荐）**：
    单个 Connector 保持极简设计（单模型、单进程、单 SQLite 库）。通过工具脚本 `potluck contribute run-all --config-dir ...` 或 systemd/PM2 一键启动 2~3 个轻量进程，每个进程使用独立的节点身份和端口。
    *优点*：进程级别故障隔离（一个模型挂了不影响其他模型）、零协议修改、架构极简。
  - **方案 B（单进程多模型路由模式）**：
    重构 Connector 内部的 `Adapter` 为字典，`node.hello` 支持上报 `models: list[str]`，内部进行并发和队列调度。
    *缺点*：增加了 Connector 内部并发调度的复杂度，可能引起多模型争抢同一物理显卡显存时的隐式死锁。
- **Operator 建议选项**：**采纳方案 A**。保持 Unix 哲学，通过轻量级多进程编排实现多模型贡献。

---

### 决策 4：社区自动化多平台 Bot 统一守护进程架构
- **背景**：
  目前在 `scripts/` 下已创建了 `potluck_community_bot.py` (Telegram/CLI), `potluck_feishu_bot.py` (飞书) 和 `potluck_discord_bot.py` (Discord)。
- **需要拍板的权衡**：
  - **方案 1（独立部署）**：三个 Bot 各自独立运行，各自配置独立的进程守护。
  - **方案 2（统一 Hub 守护）**：整合为一个 `potluck-community-daemon`，共用一个基于本地 `potluck ask` / OpenAI API 的客服问答内核与 FAQ 知识库，一次配置，三网同步广播（例如在 GitHub 发布新 Release 时，三网群组同时推送）。
- **Operator 建议选项**：初期采用**方案 1**分别跑通并观察哪个社区平台用户最活跃；活跃度明确后收敛至**方案 2**。

---

### 决策 5：早期社区信用额度与代币激励经济模型
- **需要拍板的规则**：
  1. **初始注册赠额**：新用户通过邀请码 `potluck join` 时，默认获赠多少初始额度？
     - 建议：每个邀请码赠送 **20,000 milli-credits**（相当于 20 点积分，约可跑 100~200 次标准对话）。
  2. **贡献奖励放大系数**：贡献节点每提供 1,000 tokens 算力，获得的积分比例：
     - 建议初期设置为 1:1.2（即贡献 1 份算力，可赚取 1.2 份消费额度，以鼓励早期节点接入）。
  3. **充值机制意向**：后期是否接入外部法币支付通道（Stripe / 微信支付），还是坚持纯点对点共享社区？

---

## 第三部分：本次长程任务支持升级落地总结

在当前迭代中，我们已在代码库完成以下关于**长程任务与 Agent 支持**的实质性升级，所有 2324 项单元测试已全部 100% 验证通过：

| 功能模块 | 代码修改位置 | 解决的实际问题 | 验证测试 |
|---|---|---|---|
| **推理模型参数透传** | `src/aipotluck/wire.py` (`CHAT_PARAMETER_KEYS`) | 允许 Agent 客户端指定 `reasoning_effort` ("low", "medium", "high")，用于控制 OpenAI o1/o3/o4 及新一代推理模型的长程思考深度，不再被平台网关丢弃。 | `tests/test_long_horizon_tasks.py` |
| **多回合 Tool 历史无损保留** | `src/aipotluck/wire.py` (`ChatMessage`) | 扩展 `ChatMessage` 增加了对 `tool_calls`、`tool_call_id`、`name` 字段的规范化解析与 Canonical 签名，多回合 Agent 循环中包含工具调用的历史不再被剥离损坏。 | `tests/test_long_horizon_tasks.py` & `tests/test_openai_wire.py` |
| **DeepSeek-R1 思考流提取** | `src/aipotluck/adapters.py` (`_openai_delta_text`, `_from_payload`) | 当思考模型在流式中仅输出 `reasoning_content`（`content` 为 null）或纯思考模式时，自动提取思考内容流式返回，防止长程思考被误判为“empty completion”而抛错。 | `tests/test_long_horizon_tasks.py` |
| **CLI 命令行会话亲和性** | `src/aipotluck/cli.py` (`potluck ask --session-id`) | 命令行客户端现在支持指定 `--session-id`，使脚本化长程任务可以在多个 turn 间复用同一 Session，享受服务端的聚合记录与节点的 KV Cache 亲和性。 | `tests/test_long_horizon_tasks.py` |

---

> 💡 **后续推进指南**：当你抽空完成了「第一部分」中的 Telegram/飞书/Discord 机器人创建或账号准备后，只需将对应的 Token/Secret 填入对应运行脚本即可立即上线运营！如有架构决策需要落地，随时告知继续推进。
