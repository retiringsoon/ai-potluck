# ai-potluck 全平台社群搭建与 AI Agent 自动化运营实操指南

> 本指南用于 `ai-potluck` 项目在 **Discord、飞书 (Feishu)、Telegram (TG)、GitHub Discussions** 上的社群开通、Agent 自动化答疑与运维部署。
>
> 💰 **费用结论：全部 100% 永久免费！** 无需给任何平台付费，也不需要注册小号。

---

## 平台选型与费用一览

| 平台 | 是否收费 | 适用受众 | 自动化 Agent 接入工具 | 核心优势 |
| :--- | :---: | :--- | :--- | :--- |
| **GitHub Discussions** | **完全免费** | 全球开源开发者、异步技术探讨 | GitHub 原生，已自动开通 | 零门槛，与 Issue/PR 打通，SEO 权重高 |
| **Telegram (TG)** | **完全免费** | 海外极客、跨国节点提供者 | `scripts/potluck_community_bot.py` | 极客标配，零审核风控，无限制自由 |
| **飞书 (Feishu)** | **完全免费** | 国内开发者、小红书引流用户 | `scripts/potluck_feishu_bot.py` | 国内免翻墙扫码即进，富文本卡片交互顶级 |
| **Discord** | **完全免费** | 全球 AI / 开源项目标准聚集地 | `scripts/potluck_discord_bot.py` | 频道结构清晰，海外知名开源项目标配 |

---

## 一、GitHub Discussions（已自动化搭建完毕 ✅）

* **状态**：已通过 GitHub API 官方开启，并已发布首条官方欢迎帖。
* **访问地址**：
  * 主仓库：https://github.com/retiringsoon/ai-potluck/discussions
  * 服务端：https://github.com/retiringsoon/ai-potluck-server/discussions
  * 欢迎置顶帖：https://github.com/retiringsoon/ai-potluck/discussions/1
* **包含分类**：
  * 📢 **Announcements**：版本发布、重大更新。
  * 💬 **General**：常规极客交流。
  * 💡 **Ideas**：Feature Request、新模型接入提案。
  * 🙏 **Q&A**：使用答疑、收据核验支持。
  * 🙌 **Show and tell**：群友算力搭建分享、Cursor / Claude Code 工作流展示。

---

## 二、Telegram 极客社群（30 秒上线）

### 1. 创建群组与 Bot（完全免费）
1. 打开 Telegram，点击右上角新建群组（建议在群组设置里开启 **Topics** 开启论坛模式）。
2. 在 Telegram 里搜索官方机器人 `@BotFather`，发送 `/newbot`。
3. 按照提示输入 Bot 昵称（如 `ai-potluck Agent`）和用户名（如 `aipotluck_bot`）。
4. `@BotFather` 会立即发你一串 `HTTP API Token`（例如 `7123456789:AAH...`）。
5. 把你刚建好的 Bot 拉进你的 Telegram 群，并设置为群管理员（给予发消息权限）。

### 2. 启动自动化 Agent
```bash
cd apps/ai-potluck
export TELEGRAM_BOT_TOKEN="你的_TELEGRAM_BOT_TOKEN"

# 启动机器人服务（守护进程）
./scripts/potluck_community_bot.py tgbot --token "$TELEGRAM_BOT_TOKEN"
```
* 用户在群内发送 `/status`、`/rules`、`/verify R-1`，Bot 将毫秒级自动回复。
* 管理员可在群里私发 `/mint consumer 500000` 自动生成体验邀请码。

---

## 三、飞书免翻墙社群（国内首选，3 分钟上线）

飞书标准版对个人及 100 人以下团队永久免费，支持微信扫码直接入群。

### 1. 建立飞书社区组织与公开群（免费）
1. 用你现有的手机号登录飞书，点击头像选择 **“创建团队”**（例如命名为 `ai-potluck 开源社区`）。
2. 在新团队下新建一个群聊，在群设置里开启 **“支持微信扫码入群”**，保存群二维码备用（挂在小红书或博客上）。

### 2. 创建飞书 Bot 应用
1. 访问 [飞书开放平台 (open.feishu.cn)](https://open.feishu.cn/)，登录后点击 **“创建企业自建应用”**。
2. 应用名称填写 `ai-potluck Assistant`，上传一个机器人头像。
3. 在左侧菜单 **“凭证与基础信息”** 获取：
   * `App ID` (例如 `cli_a1b2c3...`)
   * `App Secret` (例如 `XYZ123...`)
4. 在左侧菜单 **“添加应用能力”** -> 开启 **“机器人”**。
5. 在左侧菜单 **“权限管理”** -> 开通以下权限：
   * `im:message`（获取与发送单聊/群聊消息）
   * `im:message:send_as_bot`（以应用身份发消息）
6. 在左侧菜单 **“事件订阅”**：
   * 请求网址填入你的服务器地址（如 `http://<你的公网IP或域名>:8088/`）。
   * 添加事件：`im.message.receive_v1`（接收消息）。
7. 点击 **“版本管理与发布”**，申请发布（自建应用自己点同意即可秒级生效）。

### 3. 启动飞书 Agent 服务
```bash
cd apps/ai-potluck
export FEISHU_APP_ID="cli_..."
export FEISHU_APP_SECRET="..."

# 启动 Webhook 服务（默认监听 8088 端口）
./scripts/potluck_feishu_bot.py --app-id "$FEISHU_APP_ID" --app-secret "$FEISHU_APP_SECRET" --port 8088
```
* 群友在飞书群里 `@ai-potluck Assistant` 或直接提问 `/status`、`/rules`，Bot 将以精美的飞书交互式卡片（CardKit）回复，并附带直达 GitHub 的按钮！

---

## 四、Discord 海外极客社群（2 分钟上线）

### 1. 创建 Discord 服务器（完全免费）
1. 打开 Discord，点击左侧栏最下方的 `+`（添加服务器） -> **“亲自创建”** -> **“供我和我的朋友使用”**。
2. 服务器名称填写 `ai-potluck`，上传图标。
3. 推荐建立几个默认频道：
   * 📢 `#announcements`
   * 💬 `#general`
   * 🤖 `#bot-playground`
   * 💡 `#feature-requests`

### 2. 创建 Discord Bot
1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)。
2. 点击右上角 **“New Application”**，起名 `ai-potluck-agent`。
3. 点击左侧 **“Bot”** 选项卡：
   * 点击 **“Reset Token”** 获取 `Bot Token` 并复制保存。
   * 下拉勾选 **Privileged Gateway Intents** 中的 `MESSAGE CONTENT INTENT`。
4. 点击左侧 **“OAuth2” -> “URL Generator”**：
   * Scopes 勾选：`bot`、`applications.commands`。
   * Bot Permissions 勾选：`Send Messages`、`Embed Links`、`Read Messages/View Channels`。
   * 复制底部生成的链接，在浏览器打开，将 Bot 邀请进你刚才建的 `ai-potluck` 服务器。

### 3. 启动 Discord Agent
```bash
cd apps/ai-potluck
export DISCORD_BOT_TOKEN="你的_DISCORD_BOT_TOKEN"

# 1. 运行完整实时互动机器人（需要 uv pip install discord.py）
./scripts/potluck_discord_bot.py bot --token "$DISCORD_BOT_TOKEN"

# 2. 或者：免依赖纯 Webhook 广播模式（零三方依赖，只需在频道设置里建个 Webhook）
./scripts/potluck_discord_bot.py webhook --url "<DISCORD_WEBHOOK_URL>" \
  --title "ai-potluck 每日状态简报" \
  --message "集群运转正常，账本与 Merkle 日志校验通过！"
```

---

## 五、日常运营快速指令速查

```bash
# 1. 给小红书新用户签发一个带 50 万 milli-credit 的体验号：
./scripts/potluck_community_bot.py mint --role consumer --quota 500000

# 2. 给自建 4090 或拥有 API 的极客签发一个贡献节点码：
./scripts/potluck_community_bot.py mint --role node

# 3. 检查当前账本与 Merkle 树是否完好（日常巡检）：
./scripts/potluck_community_bot.py health

# 4. 生成今日社群 Markdown 简报（复制到群里）：
./scripts/potluck_community_bot.py digest
```
