# ai-potluck 全平台宣发与推广文案指南

> 本文档用于 `ai-potluck` 项目在 GitHub、Hacker News、Twitter/X、Reddit、小红书以及极客社群的宣发与推广。各平台文案可直接复制使用。

---

## 一、GitHub 仓库物料与定位

### 1. 仓库基础信息
* **Repository Name**: `ai-potluck`
* **Short Description / Bio**:
  > `Pool AI capacity, not credentials. A peer-contributed, cryptographically verifiable AI compute sharing network with drop-in Claude Code & Cursor support.`
* **Topics (Tags)**:
  `ai-sharing`, `cryptography`, `ed25519`, `merkle-tree`, `claude-code`, `cursor`, `llm-gateway`, `self-hosted`, `decentralized`, `privacy-first`

### 2. 徽标 (Badges)
```markdown
[![ci](https://github.com/retiringsoon/ai-potluck/actions/workflows/ci.yml/badge.svg)](https://github.com/retiringsoon/ai-potluck/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)
[![Zero-Dependency Core](https://img.shields.io/badge/Dependencies-Standard%20Lib%20%2B%20Crypto-success.svg)](pyproject.toml)
```

---

## 二、海外极客圈宣发（Hacker News / Reddit / X）

### 1. Hacker News: Show HN
**Title:** Show HN: ai-potluck – Pool AI capacity, not credentials (cryptographically verifiable sharing)

**Body:**
```markdown
Hey HN,

Communities and dev teams informally share AI accounts all the time — pasting an API key into a Telegram/WeChat chat, passing around a team subscription, or relying on "I'll Venmo you later."

It works until the first billing cycle ends: somebody runs an unbounded coding agent loop, nobody can prove who spent what, the person sharing carries 100% of the financial and account-ban risk, and every dispute turns into "trust me."

We built **ai-potluck** (MIT licensed) to turn this informal handshake into cryptographic infrastructure:

1. **Contributors pool capacity, never credentials**: You run a lightweight local connector on your own machine. Your upstream API keys never leave your device (stored locally under file mode 0600). The connector makes outbound connections only (NAT-friendly); you can stop sharing anytime with Ctrl+C.
2. **Every charge is cryptographically provable**: Every request produces an Ed25519 dual-signed receipt (device signature + platform signature) anchored into an append-only Merkle transparency log. Anyone can verify charges offline (`potluck verify receipt`) — interrupted work is reconciled cleanly, never guessed.
3. **Drop-in for Claude Code & Cursor**: `potluck serve-openai` spins up a local loopback signing proxy (`127.0.0.1:8787`). Plug the pool into Claude Code, Cursor, Codex, or the OpenAI SDK without writing a single line of integration code.
4. **Zero heavyweight dependencies**: Written in pure Python (standard library + `cryptography` only). SQLite WAL backend with deterministic integer arithmetic (milli-credits).

Source: https://github.com/retiringsoon/ai-potluck
Server repo: https://github.com/retiringsoon/ai-potluck-server

We'd love your feedback, code critique, and early thoughts!
```

### 2. Reddit: r/selfhosted & r/LocalLLaMA
**Title:** [Project] ai-potluck – Share surplus LLM quota & self-hosted vLLM with friends without handing over your API keys

**Body:**
```markdown
Hi everyone,

If you host local models (vLLM/Ollama on an RTX 4090/A100) or have surplus enterprise API credits, sharing them with friends or colleagues usually involves either setting up a complex reverse proxy or literally giving away your API keys.

I created **ai-potluck**, an open-source, peer-to-peer capacity-sharing network:
* Upstream keys stay 100% on the contributor's machine.
* Every query is billed in deterministic milli-credits with an Ed25519-signed receipt and Merkle log entry.
* Loopback proxy (`potluck serve-openai`) makes it a drop-in replacement for Cursor and Claude Code.

Check it out: https://github.com/retiringsoon/ai-potluck
Feedback is warmly appreciated!
```

---

## 三、小红书爆款文案（两套风格）

### 方案 A：痛点引发共鸣型（适合大众开发者、学生、独立黑客）

* **封面大字标题**：`朋友找我借 API Key？别当冤大头了！我写了个开源神器`
* **封面副标题**：`密钥不离身 | 加密可对账 | 无缝接 Claude Code & Cursor`
* **配图建议**：
  * **图 1**：痛点对比图。左边是微信聊天记录截图（“兄弟借个 Key 跑点东西，回头转你”+ 账单翻车），右边是终端里清爽优雅的 `potluck ask` 绿色输出和成本收据。
  * **图 2**：架构草图。突出“中心服务器管账，Key 永远留在本地电脑”的安全感。
  * **图 3**：Cursor / Claude Code 中配置 `http://127.0.0.1:8787/v1` 的实测实跑图。
* **正文文案**：
```text
不知道大家有没有遇到过这种尴尬事：
朋友或者同事想测个新模型、跑个 Agent 任务，找你借 API Key。
借吧？怕对方放进死循环脚本一夜刷爆信用卡，或者不小心把 Key 传到了 GitHub；
不借吧？低头不见抬头见，又抹不开面子……

为了彻底终结这种“口头拼车”的风险，我和搭档做了一个开源小工具——【ai-potluck】！

它的逻辑特别有意思，就像吃“百家饭”（Potluck）：
每个人带一道菜，或者大家一起尝尝鲜。

✨ 为什么说它是极客的理想方案？
1️⃣【密钥永远不出本机】：你想共享闲置额度或自建显卡，只在自己电脑上挂一个小后台。你的上游 API Key 严格存放在本地 0600 权限文件里，中心服务器和使用者从头到尾碰不到你的 Key！你随时 Ctrl-C 就能断开。
2️⃣【每一分钱都有加密收据】：朋友每次提问，系统生成 Ed25519 双签收据并记入只增不改的 Merkle 透明日志。谁花了多少毫厘币一目了然，还能离线对账（`potluck verify receipt`），再也没有糊涂账。
3️⃣【无缝接入 Claude Code & Cursor】：运行 `potluck serve-openai`，本地直接起一个回环代理，环境变量一设，你的开发工具就直接接入了朋友共享的算力池！

不搞虚头巴脑的币，不搞中心化资金托管，纯粹是一套优雅的密码学共享工具。

代码已全开源在 GitHub！想进内测交流群或者申请体验额度的小伙伴，欢迎在评论区留言~ 🚀

#开源项目 #程序员日常 #独立开发 #ClaudeCode #Cursor #人工智能 #Python #后端开发
```

---

### 方案 B：硬核极客技术流（适合架构师、技术大牛、开源发烧友）

* **封面大字标题**：`我给 AI 拼车写了一套密码学收据协议（开源）`
* **封面副标题**：`零凭据泄露 · Ed25519双签 · Merkle透明日志`
* **配图建议**：
  * **图 1**：清晰规范的时序流程图（客户端签名 → 中心验签预占 → 节点本地调用上游 → 签名回传收据）。
  * **图 2**：`potluck-server` Admin UI 实时监控面板（活跃节点、排队深度、收据清单）。
  * **图 3**：终端执行 `potluck verify receipt` 打印 5 个维度离线验证全绿的终端高光图。
* **正文文案**：
```text
传统的 AI 网关（如 OneAPI / NewAPI）本质上是“管理员全托管”架构：
所有成员必须把上游 API Key 托付给管理员，一旦管理员机器被黑或者中心数据库泄露，所有 Key 全部阵亡；而且管理员拥有任意修改账本的生杀大权。

我们从密码学的第一性原理出发，写了【ai-potluck】：
一套专注于多方零信任算力协同的开源协议。

🔑 核心设计亮点：
▪️ 零凭据穿透（Zero-Knowledge Routing）：贡献节点主动向外建立 WebSocket，上游 API Key 仅存本地（0600 权限）。节点在本机请求供应商，中心只转发加密文本增量。
▪️ 双向 Ed25519 签名与 Merkle 审计：消费端请求逐笔签名授权；节点回传经硬件密钥签名的用量；中心写入类似 Certificate Transparency 的只增不改 Merkle 日志。
▪️ 离线确定性对账：所有记账均使用确定性整数（milli-credit），拒绝浮点漂移。任何一方怀疑账目，直接本地执行 `verify receipt` 离线校验五维证明。
▪️ 极客纯度：核心库除 cryptography 外零三方依赖，完全依靠 Python 标准库构建，SQLite WAL 高性能驱动。

无论是实验室共享 A100，还是开发者小圈子拼车 Claude 3.5 Sonnet，都不用再靠人情信用。

代码已在 GitHub 开源：retiringsoon/ai-potluck
欢迎技术同行 Star 与交流！

#开源架构 #密码学 #系统设计 #架构设计 #Python #分布式系统 #网络协议
```

---

## 四、社群转化与评论区互动话术

### 1. 评论区置顶引导
> 感谢大家关注！`ai-potluck` 纯开源、无任何代币与金融炒作，初衷是让朋友和团队安全共享算力。
> 
> 💡 想体验的小伙伴：
> 1. 欢迎 Star 我们的 GitHub：`retiringsoon/ai-potluck`
> 2. 想进开发者尝鲜群 / 获取公测邀请码的朋友，可以在评论区留言“上车”，我私信发你群链接和体验 Invite Code！

### 2. 常见评论回复模板
* **Q: 这和 OneAPI / NewAPI 有什么区别？**
  * **A**: OneAPI 是中心化代理，你的 API Key 必须上传到服务器；而 ai-potluck 的核心是“Key 绝不出本机”，上游调用是在你自己的电脑上完成的，且每一笔账目都有密码学签名和防篡改日志，防赖账、防爆卡。
* **Q: 既然节点在本机跑，那我把电脑关了怎么办？**
  * **A**: 节点断开连接后，中心会自动剔除离线节点，后续请求会自动路由给其他在线节点或优雅排队，绝不会卡死请求，也不会扣除消费者的额度。
* **Q: 支持 Windows 吗？**
  * **A**: 目前推荐通过 WSL2 运行，体验和 Linux/macOS 完全一致，原生 Windows 启动脚本已在 Roadmap 中。
