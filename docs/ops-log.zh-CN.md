# 运维记录（内部，仅 dev 分支）

append-only：新条目加在顶部，不改旧文。

## 2026-10-13 · 管理页上线 + v0.3.0 + 邀请码自动发 key

- **生产部署**：JP `potluck-server.service` 现以 `--admin-ui 9802` 运行；
  管理页仅绑 127.0.0.1（非回环拒绝启动），查看方式
  `ssh -L 9802:127.0.0.1:9802 aliyun-jp` 后开 http://127.0.0.1:9802。
  内容：consumer/contributor 排名、今日与 7 天 requests/tokens/mC、
  tokens/小时吞吐、队列深度、会话聚合、最近收据，10s 自动刷新。
- **版本**：v0.3.0 已发布（双仓）。package version 统一 0.3.0。
- **邀请码自动发 key**：`admin invite --with-api-key` 铸造的邀请码，
  join 时中心当场签发 OpenAI 面 API key（明文只在 join 输出出现一次）。
  兑换前邀请码零花钱能力；key 与身份可独立撤销。
- **当前 5 个测试用户邀请码**（带自动 key，有效期 2026-11-11）已交付
  `~/Downloads/potluck-invites.txt`；更早 5 个旧码（无自动 key）已标注替代，
  未用完勿再分发，自然过期。
- **deadline 政策**：OpenAI 面默认预算 30 分钟（原 100s），
  `--deadline-ms` 可至 24h 硬上限；签名路径由客户端 `--timeout` 决定。
- **术语**：center → server 全量统一（含中文"中心服务器"）；
  旧配置键 `center_url` 已拒绝并指路 `server_url`。
- **配置 fail-loud**：server.json / node.json 未知字段拒绝（报错列出
  未知名与合法集合，不引用值）——拼错 certfile 不再可能静默明文监听。
- **测试套件**：server 496→493（删 3 个重复用例，均有变异 kill 验证）；
  10 处盲 sleep 改条件等待；启用 pytest-xdist（并行 9.4s，串行 69.7s）。
- **遗留观察**：`tests/test_reconcile.py` 的对账不变量测试在 CI 出现过一次
  失败（慢环境），已加逐表 diff 诊断；复发时日志会直接给出"哪张表哪行"。
  截至本条未见复发。

## 2026-10-12 · 生产切新栈 + v0.2.0/v0.2.1

- JP 生产切换到 ai-potluck-server 新栈（数据目录
  `/root/potluck-server-data`，systemd 单元 `potluck-server.service`）。
- OpenAI 兼容端点上线：`/openai/v1/chat/completions`（流式+缓冲）、
  `/openai/v1/models`、`admin api-key`（额度/撤销/限流/并发上限）。
- 生产实测抓到两个 bug 并修复：升级库的 request_inputs 迁移
  （表重建）、不可派发请求的终态路径（僵尸行）。
- v0.2.0 / v0.2.1 双仓发布；术语 center→server 随 v0.2.1。
