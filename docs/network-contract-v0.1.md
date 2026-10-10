# AI 共享平台网络契约 v0.1

> 日期：2026-10-08｜范围：D0（跨机器正常路径）+ D1（最小持久化与恢复）
> 依据：外部架构审查 §3/§6/§10 的纵切建议；本文是该建议的第 1 项交付物。
> 诚实边界：本文描述的是**已实现并已验收**的链路。未验收的部分在 §9 单独列出，不并入本文档的承诺。

## 1. 拓扑

```
消费者 CLI ──HTTPS 443（请求/查询/SSE）──▶ 中心网关
                                            ▲  │
贡献者 Connector ──主动 WSS 443 出站长连接────┘  │ OFFER/EXECUTE/ACK（既有连接上）
      │                                         │
      ├─▶ 固定 adapter ─▶ 上游模型 / 本机可控假上游
      └─▶ 本地 SQLite（execution / spool / ack 游标）

中心 ─▶ 中心 SQLite（request / attempt / event / settlement / receipt / log / outbox）
```

**中心**：HTTP 接入、平台鉴权、资源登记、调度、winner/epoch、事件缓存与重放、整数预占与结算。不向消费者发上游 Key；不接受远程任务指定任意 fetch URL 或 Shell。

**节点**：自行配置固定 adapter、上游地址与凭据；**只做出站连接**，不监听任何公网端口，不需要端口转发、UPnP 或公网 IP。本地暂停、并发与预算是最终限制，平台签名不得抬高。

**数据面**：消费者 → 中心 → 节点 → 上游，原路返回。第一版用中心中继，不做消费者直连节点。代价是中心可见明文正文，中心成为可用性、隐私与账务信任点。签名收据减少事后篡改，不消除这些信任，也不排除分叉视图（需独立见证者，未做）。

## 2. 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 存活探测，无需鉴权 |
| POST | `/v1/requests` | 创建请求（bounded），返回 `request_id`；幂等 |
| GET | `/v1/requests/{id}` | 状态：attempts、用量、输出提交状态、结算收据摘要 |
| GET | `/v1/requests/{id}/events?cursor=N` | SSE：先重放 `cursor` 之后已落盘事件，再转直播 |
| POST | `/v1/requests/{id}/cancel` | 取消（有界，不阻塞控制通道） |
| GET | `/v1/receipts/{receipt_id}` | 签名收据 + 检查点 + inclusion proof |
| GET | `/v1/checkpoint` | 最新签名检查点 |
| GET | `/v1/nodes` | 在线节点与能力（运维视图） |
| GET (Upgrade) | `/v1/node-link` | 节点 WSS 长连接 |

节点长连接是本版**唯一**的控制与数据通道；控制消息（cancel / query / lease）走独立有界发送队列，与输出分帧分离，避免慢读输出饿死控制 ACK（见 §7.8）。

## 3. 认证与凭据边界

- **消费者**：`Authorization: Bearer <consumer_token>`。令牌解析出 `{tenant_id, subject_id, role}`；中心只存令牌摘要，不存明文。
- **节点**：`Authorization: Bearer <node_token>`，在 **WSS 握手头**里发送。凭据不进 URL、不进业务日志、不进错误消息。
- **请求体不得携带身份**。`POST /v1/requests` 的身份来自令牌，不是正文。正文里的 `/v1/...` 字段一律忽略，防止用正文冒充身份。
- **consumer → payer 绑定**：授权记录声明的 `consumer_id` 必须等于令牌解析出的 `subject_id`；`payer_account_id` 必须是该 consumer 本人，或经注册表里有额度、可撤销、有有效期的 `SpendGrant` 显式授权。三个边界（创建、预占/派发、结算）都要校验，不只在结算处校验一次。
- **`gateway_authenticated`**：由平台代签的授权，作用域限平台自有账户，不在本版的多用户门禁内（见 §9）。
- 上游 Key 只在节点侧；中心与消费者**都没有**上游 Key。中心可访问上游不代表节点可访问上游——上游连接从贡献者网络发起。

## 4. 数据面时序

```
消费者               中心                      节点                  上游
  │  POST /v1/requests │                        │                    │
  ├────────────────────▶│ 令牌→tenant/consumer   │                    │
  │                     │ 幂等键查重·预占·建请求  │                    │
  │◀── request_id ──────┤                        │                    │
  │  GET .../events ───▶│                        │                    │
  │                     ├── attempt.offer ──────▶│ 本地策略判定        │
  │                     │◀─ attempt.accept ──────┤                    │
  │                     ├── attempt.execute ────▶│ 执行去重检查        │
  │                     │                        ├─ execute(call=1) ─▶│
  │                     │◀─ attempt.event(delta) ┤◀── delta ──────────┤
  │◀── SSE delta ───────┤  （首段语义输出提交，  │                    │
  │                     │    与 winner CAS 同事务）│                   │
  │                     │◀─ attempt.event(usage) ┤◀── final usage ────┤
  │                     │◀─ attempt.event(done) ─┤                    │
  │                     │ 结算（整数账本）→ 收据 → 日志叶子            │
  │◀── 收据 + proof ────┤                        │                    │
```

`OFFER → ACCEPT → EXECUTE` 分成三步而非一步，是因为节点需要在**本地策略**处拒绝：平台签名有效不代表该节点此刻愿意接这一单（暂停、并发已满、预算超限）。

## 5. 消息信封

所有消息是严格 JSON 文本帧，信封：

```json
{
  "schema": "aipotluck.envelope.v1",
  "type": "attempt.execute",
  "msg_id": "…",
  "sent_at": "2026-10-08T02:00:00+00:00",
  "reply_to": null,
  "payload": { }
}
```

编解码走 `canonical.loads_strict` / `canonical.canonicalize`，因此**网络边界天然拒绝**浮点、重复 key、`NaN`/`Infinity`。未知 `type` 一律拒绝并回 `protocol.error`，不做静默忽略。

消息类型：

| 方向 | type | 关键字段 |
|---|---|---|
| 节点→中心 | `node.hello` | node_id, device_id, provider_id, adapter{name,model}, policy{share_enabled,max_concurrency,budget} |
| 中心→节点 | `hello.ack` | server_time, heartbeat_interval_s, resume[]（未终态 attempt 的恢复指令） |
| 中心→节点 | `attempt.offer` | request_id, attempt_id, epoch, resource_id, model, request_digest, max_milli_credits, deadline_ms, delivery_mode |
| 节点→中心 | `attempt.accept` / `attempt.reject` | request_id, attempt_id, epoch, (reason) |
| 中心→节点 | `attempt.execute` | request_id, attempt_id, epoch |
| 中心→节点 | `attempt.cancel` | request_id, attempt_id, epoch, origin |
| 中心→节点 | `attempt.query` | request_id, attempt_id |
| 节点→中心 | `attempt.state` | request_id, attempt_id, epoch, execution_certainty, state, upstream_calls, last_seq |
| 节点→中心 | `attempt.event` | request_id, attempt_id, epoch, seq, event_type ∈ {output.delta, usage.final, attempt.failed, attempt.completed}, payload |
| 中心→节点 | `event.ack` | attempt_id, acked_seq（连续前缀） |
| 节点→中心 | `node.heartbeat` | in_flight, share_enabled, at |
| 双向 | `protocol.error` | code, detail, reply_to |

## 6. 幂等与恢复语义

四层各自独立，不能互相代替：

| 层 | 键 | 重复时的行为 |
|---|---|---|
| 请求准入 | `(tenant_id, idempotency_key)` | 同正文 → 返回同一 `request_id`，不新增 attempt / 调用 / 结算；异正文 → `409` |
| 事件序号 | `(attempt_id, seq)` | 同内容 → 幂等去重；异内容 → 协议违规，拒绝而非覆盖 |
| 执行 | `attempt_id`（节点本地表） | 已执行过的 attempt 收到重复 EXECUTE → 回 `attempt.state`，**不再调上游** |
| 结算 | `(service_unit_id, settlement_revision)` | 同内容 → 返回同一收据与日志位置；异内容 → 冲突 |

**准入见证时间**：节点签名记录里的 `reported_at` / `signed_at` 是自报的。撤销后的新消息按**平台首次见证时刻**判权，不按自报时刻——否则持有已撤销密钥者可回填旧时间戳继续结算。历史记录的**离线复核**仍按记录自身时间戳，两套规则必须分开。

**首段输出提交**：winner CAS、epoch 校验、首段语义事件落盘在**同一事务**，提交后才写 socket。这样「写完 socket 就断线」不会被误判成「用户没看到」。

**分片级原子提交**：`stream_strict` 下**每个** `output.delta` 分片的落库与「该 attempt 是否仍拥有本请求输出流」的判定在同一事务完成（store 原语 `commit_output_segment`）。首个分片执行与 §6 首段提交相同的 winner CAS；后续分片仅当提交记录仍指向该 attempt、其 epoch 未变且 attempt 仍 output-eligible 时追加。竞速失败或过期分支的分片一个字节都不会进入消费者事件流，发送方收到 `attempt.cancel`（origin=superseded）与 ACK；重放同一 `(attempt_id, seq)` 且内容不同会被整个拒绝而非覆盖。「先落库、后判定 winner」的两段式写法曾在两笔事务之间留下崩溃窗口——消费者见过文本但中心没有提交记录，重启后可能重新派发导致输出拼接——该窗口已消除。

**重启语义**：

- 中心重启 → 从 SQLite 重建请求、事件流、收据、Merkle 日志。消费者按游标恢复，未重做 attempt。
- 节点重启 → `executions` 表里仍为 `SUBMITTING` 的执行标记为 `UNKNOWN`（不确定是否已产生上游消耗），上报后中心的用量保持 unknown，**不写 0、不无限重试**。
- `hello.ack` 的 `resume[]` 告诉节点哪些 attempt 仍未终态；节点据此重发 spool，而不是重新执行。

## 7. 故障分类与处置

| # | 场景 | 处置 |
|---|---|---|
| 7.1 | 节点位于 NAT 后，仅出站 | 节点主动 WSS；中心不反向连节点 |
| 7.2 | 上游调用成功但链路断 | 节点 spool 补传；**不能把链路断开当执行未发生** |
| 7.3 | SUBMITTING 期间节点被 kill | 该执行标 `UNKNOWN`，中心记为待核对组件，不并入用户账单 |
| 7.4 | A 首段输出前失败 | 最多一次受限 retry（受 `max_attempts` 与「已提交输出」守卫）；换 B 合法 |
| 7.5 | A 已提交输出后断线 | 恢复 A 或显式失败；**不得拼接 B**；A 迟到的 usage 仍保留 |
| 7.6 | final usage 丢失 | 输出成功与用量 unknown 相互独立 |
| 7.7 | 重复结算 ACK | 返回同一收据与同一日志位置，不新增叶子 |
| 7.8 | 慢消费者 | 各连接有界发送队列；超出即阻塞该连接/断开，**不得**阻塞控制通道或拖慢结算 |
| 7.9 | 节点暂停 | 新准入 = 0；在途 attempt 按正常路径结束 |
| 7.10 | 凭据缺失或错误 | fail-closed，退出码非 0，且不回显凭据 |

## 8. 代理与网络实测项

先支持**贡献者显式配置**的固定中心代理（`HTTPS_PROXY` 语义），不从远程 prompt 派生代理参数，不转发私有环境变量。若目标网络阻断 WSS Upgrade，再增加 HTTPS 任务领取 + 批量事件上传的回退；第一版**不同时造两套传输实现**。

部署前必须实测（本版未自动化的部分，列为已知缺口）：

- 中心可达性与有效 TLS 证书链信任
- 443 出站、企业代理 `CONNECT`/`Upgrade` 是否放行
- 空闲断开与心跳间隔的实际容忍度
- 机器休眠后的重连行为
- 节点到上游（或本机模型服务）的可达性

WebSocket ping 只说明连接层活性，**不能**替代 attempt 执行状态与应用层 ACK。LAN / VPN / 同机演示不得当作公网共享验收。

## 9. 已知限制与未做

- **未做**：消费者直连节点（ICE/STUN/TURN）、区域 relay、TEE/zk 来源证明、独立见证者排除分叉视图、真实付费上游 adapter 验收、提现、奖励系统。
- **第二贡献节点**：回环真实 socket 上的双节点闭环（失败换节点 §6-7a、提交后不拼接 §6-7b、暂停节点零派发 §6-8）已由 `tests/test_end_to_end_network.py` 以真实 TCP + 真实 connector 验收；跨机器双节点部署验收按 `docs/ai-share-cross-machine-live-test.md` 执行，属部署侧步骤，不在本仓库测试覆盖内。
- **`gateway_authenticated`**：平台代签授权的作用域限制未在本版实现为独立门禁；该模式的签名密钥是平台密钥而非消费者可持有，因而不在「陌生多用户」攻击面内，但也不构成多用户安全承诺。
- **上游适配器**：本版只提供固定 `HttpUpstreamAdapter`（显式配置的单一端点 + 单一凭据）与 `FakeUpstreamAdapter`（可控假上游）。不宣称任何供应商条款合规性。
- **中心存储**：用 SQLite，仅中心内部访问。审查建议的部署形态是 PostgreSQL；本版通过 store 接口隔离，替换实现不触碰分账原语。单写者假设成立前不宣称水平扩展。
- **分叉视图**：平台仍被信任。初版不宣传 trustless。
