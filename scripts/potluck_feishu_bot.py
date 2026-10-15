#!/usr/bin/env python3
# ruff: noqa: RUF001
"""potluck_feishu_bot.py - Feishu / Lark Community Agent for ai-potluck.

Features:
1. Zero third-party dependencies: built with Python standard library.
2. Webhook Event Server:
   - Responds to URL verification challenge automatically.
   - Listens to message events (`im.message.receive_v1`).
   - Handles @mention in groups and 1-on-1 direct messages.
3. Interactive Actions:
   - `/status`: Checks ai-potluck cluster health & ledger consistency.
   - `/rules`: Returns community rules and safety guidelines.
   - `/verify <id>`: Offline receipt cryptographic verification.
   - `/mint`: Admin-authorized invite code issuance.
   - Rich interactive cards (CardKit) formatting.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Add scripts directory to sys.path so potluck_community_bot is directly importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
from potluck_community_bot import PotluckOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("feishu_bot")

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """Lightweight client for Feishu / Lark Open API."""

    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str = ""
        self._token_expires_at: float = 0.0

    def get_tenant_access_token(self) -> str:
        """Fetch or refresh tenant_access_token."""
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token

        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps(
            {"app_id": self.app_id, "app_secret": self.app_secret}
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu auth failed: {data}")
            self._token = str(data["tenant_access_token"])
            # Cache token (usually valid for 2 hours; refresh 5 min early)
            expire_in = int(data.get("expire", 7200))
            self._token_expires_at = now + expire_in - 300
            return self._token
        except Exception as e:
            logger.error("Failed to fetch Feishu token: %s", e)
            raise

    def send_card(
        self,
        receive_id_type: str,
        receive_id: str,
        card_content: dict[str, Any],
    ) -> None:
        """Send an interactive card message."""
        token = self.get_tenant_access_token()
        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
        body = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_content),
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            logger.error("Failed to send Feishu message: %s", e)


def build_card(title: str, text: str, *, status: str = "blue") -> dict[str, Any]:
    """Build a standard Feishu Interactive Card."""
    colors = {
        "green": "green",
        "red": "red",
        "blue": "blue",
        "orange": "orange",
    }
    header_color = colors.get(status, "blue")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": header_color,
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": text},
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "GitHub 源码"},
                        "type": "default",
                        "url": "https://github.com/retiringsoon/ai-potluck",
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "社区讨论"},
                        "type": "primary",
                        "url": "https://github.com/retiringsoon/ai-potluck/discussions",
                    },
                ],
            },
        ],
    }


def make_handler(
    feishu: FeishuClient,
    ops: PotluckOps,
    admin_open_ids: list[str],
) -> type[http.server.BaseHTTPRequestHandler]:
    """Factory creating the Feishu webhook HTTP handler."""

    class FeishuWebhookHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Quieter access logs
            pass

        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)

            try:
                data = json.loads(raw_body.decode("utf-8"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            # 1. Handle Feishu URL Verification challenge
            if "challenge" in data:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"challenge": data["challenge"]}).encode("utf-8")
                )
                return

            # 2. Acknowledge event promptly (Feishu requires response within 3s)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

            # 3. Process event
            event = data.get("event", {})
            header = data.get("header", {})
            if header.get("event_type") != "im.message.receive_v1":
                return

            message = event.get("message", {})
            sender = event.get("sender", {}).get("sender_id", {})
            chat_id = message.get("chat_id")
            open_id = sender.get("open_id")

            if message.get("message_type") != "text":
                return

            try:
                content_obj = json.loads(message.get("content", "{}"))
                text = str(content_obj.get("text", "")).strip()
            except Exception:
                return

            # Strip @bot tags if present
            clean_text = " ".join(
                [w for w in text.split() if not w.startswith("@_user_")]
            ).strip()

            target_id = chat_id if message.get("chat_type") == "group" else open_id
            id_type = "chat_id" if message.get("chat_type") == "group" else "open_id"

            if not target_id:
                return

            self._dispatch_reply(clean_text, id_type, target_id, open_id)

        def _dispatch_reply(
            self,
            text: str,
            id_type: str,
            target_id: str,
            sender_id: str | None,
        ) -> None:
            if not text or text.lower() in ("/start", "/help", "帮助", "菜单"):
                card = build_card(
                    "👋 欢迎来到 ai-potluck 极客交流圈",
                    (
                        "**ai-potluck** 是一个开源的密码学防赖账 AI 额度共享网络。\n\n"
                        "💡 **常用指令：**\n"
                        "• `/status` - 巡检集群健康与对账状态\n"
                        "• `/rules` - 查看社群协作规则\n"
                        "• `/verify <收据ID>` - 离线验算收据真伪\n"
                        "• `/request` - 申请早期公测体验邀请码"
                    ),
                    status="blue",
                )
                feishu.send_card(id_type, target_id, card)

            elif text.startswith("/rules"):
                card = build_card(
                    "📜 ai-potluck 社群守则",
                    (
                        "1. 🛡️ **私钥不离身**：贡献算力一律运行本地 Connector，"
                        "严禁在群里明文发 Key。\n"
                        "2. ⚖️ **按量扣费有凭据**：每次提问均带双签收据与 Merkle 日志。\n"
                        "3. 🚫 **禁止恶意压测**：测试节点仅供体验与开发调试，勿挂无限循环 Agent。"
                    ),
                    status="orange",
                )
                feishu.send_card(id_type, target_id, card)

            elif text.startswith("/status"):
                health = ops.check_health()
                is_healthy = bool(health.get("healthy"))
                state_str = "🟢 正常 (Consistent)" if is_healthy else "🔴 异常 (Discrepancy)"
                detail = health.get("detail", health.get("error", "unknown"))[:200]
                card = build_card(
                    f"📊 集群状态：{state_str}",
                    f"**记账完整性：** `{health.get('status')}`\n\n```\n{detail}\n```",
                    status="green" if is_healthy else "red",
                )
                feishu.send_card(id_type, target_id, card)

            elif text.startswith("/verify"):
                parts = text.split()
                if len(parts) < 2:
                    card = build_card(
                        "⚠️ 指令格式错误",
                        "用法：`/verify <receipt-id>` (例如 `/verify R-1`)",
                        status="red",
                    )
                    feishu.send_card(id_type, target_id, card)
                    return
                receipt_id = parts[1]
                res = ops.verify_receipt(receipt_id)
                card = build_card(
                    f"收据核验：{receipt_id}",
                    f"```json\n{json.dumps(res, indent=2)[:500]}\n```",
                    status="green" if res.get("all_pass") else "orange",
                )
                feishu.send_card(id_type, target_id, card)

            elif text.startswith("/mint"):
                # Admin-only check
                if sender_id not in admin_open_ids:
                    card = build_card(
                        "⛔ 权限不足",
                        "只有管理员才可在群内签发邀请码。",
                        status="red",
                    )
                    feishu.send_card(id_type, target_id, card)
                    return
                parts = text.split()
                role = "consumer"
                if len(parts) >= 2 and parts[1] in ("consumer", "node"):
                    role = parts[1]
                quota = 500_000
                if len(parts) >= 3 and parts[2].isdigit():
                    quota = int(parts[2])
                try:
                    invite = ops.mint_invite(role=role, quota=quota)
                    card = build_card(
                        f"🎫 新 {role.upper()} 邀请码已签发",
                        f"```\n{invite}\n```\n⚠️ **仅显示一次，请及时私发给成员。**",
                        status="green",
                    )
                    feishu.send_card(id_type, target_id, card)
                except Exception as ex:
                    card = build_card("❌ 发码失败", f"错误详情：`{ex}`", status="red")
                    feishu.send_card(id_type, target_id, card)

    return FeishuWebhookHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="ai-potluck Feishu Community Agent")
    parser.add_argument(
        "--port",
        type=int,
        default=8088,
        help="Webhook listening port (default: 8088)",
    )
    parser.add_argument(
        "--app-id",
        default=os.environ.get("FEISHU_APP_ID"),
        help="Feishu App ID",
    )
    parser.add_argument(
        "--app-secret",
        default=os.environ.get("FEISHU_APP_SECRET"),
        help="Feishu App Secret",
    )
    parser.add_argument(
        "--config",
        default="server.json",
        help="Path to potluck server.json",
    )
    parser.add_argument(
        "--bin",
        default="potluck-server",
        help="Path to potluck-server binary",
    )
    parser.add_argument(
        "--admin-open-id",
        action="append",
        default=[],
        help="Feishu open_id allowed to mint",
    )

    args = parser.parse_args()
    if not args.app_id or not args.app_secret:
        print("Error: Feishu App ID & Secret required via CLI or FEISHU_APP_ID env.")
        sys.exit(1)

    feishu = FeishuClient(app_id=args.app_id, app_secret=args.app_secret)
    ops = PotluckOps(server_config=args.config, server_bin=args.bin)
    handler_cls = make_handler(feishu, ops, args.admin_open_id)

    server = http.server.HTTPServer(("0.0.0.0", args.port), handler_cls)
    logger.info("Feishu Community Bot Webhook listening on port %d...", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Feishu bot...")
        server.server_close()


if __name__ == "__main__":
    main()
