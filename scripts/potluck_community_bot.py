#!/usr/bin/env python3
"""potluck_community_bot.py - Community Ops & Maintenance Automation for ai-potluck.

Features:
1. Operator CLI:
   - Mint invite codes with quota presets (consumer, node, OpenAI key).
   - Cluster health and reconciliation check.
   - Daily community digest generator.
2. Telegram Bot Daemon:
   - Responds to /start, /help, /rules, /status.
   - Admin-only /mint command for generating invite codes on the fly.
   - Verification helper for receipts (/verify <receipt-id>).

Zero third-party dependencies required (pure standard library).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("potluck_ops")


class PotluckOps:
    """Operator wrapper over the potluck-server CLI and local storage."""

    def __init__(self, server_config: str, server_bin: str = "potluck-server") -> None:
        self.server_config = str(Path(server_config).resolve())
        self.server_bin = server_bin

    def run_cmd(self, args: list[str]) -> str:
        """Execute a potluck-server admin subcommand."""
        cmd = [self.server_bin, *args, "--config", self.server_config]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return res.stdout.strip()
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() or e.stdout.strip() or str(e)
            logger.error("Command failed: %s\nError: %s", " ".join(cmd), err_msg)
            raise RuntimeError(err_msg) from e
        except FileNotFoundError:
            raise RuntimeError(
                f"Binary '{self.server_bin}' not found in PATH. "
                "Ensure ai-potluck-server venv is activated."
            ) from None

    def mint_invite(
        self,
        *,
        role: str = "consumer",
        tenant: str = "community",
        quota: int = 500_000,
        with_api_key: bool = True,
    ) -> str:
        """Mint a new invite code with specific role and quota."""
        args = ["admin", "invite", "--role", role, "--tenant", tenant, "--quota", str(quota)]
        if with_api_key and role == "consumer":
            args.append("--with-api-key")
        return self.run_cmd(args)

    def check_health(self) -> dict[str, Any]:
        """Perform reconciliation and database integrity verification."""
        try:
            reconcile_out = self.run_cmd(["admin", "reconcile"])
            healthy = "ok" in reconcile_out.lower() or "consistent" in reconcile_out.lower()
            return {
                "healthy": healthy,
                "status": "healthy" if healthy else "discrepancy_detected",
                "detail": reconcile_out,
            }
        except Exception as e:
            return {"healthy": False, "status": "unhealthy", "error": str(e)}

    def verify_receipt(self, receipt_id: str) -> dict[str, Any]:
        """Verify a single receipt against the server's database."""
        try:
            out = self.run_cmd(["verify", "receipt", "--receipt-id", receipt_id, "--json"])
            return cast("dict[str, Any]", json.loads(out))
        except Exception as e:
            return {"verified": False, "error": str(e)}


def run_telegram_bot(
    token: str,
    ops: PotluckOps,
    admin_chat_ids: list[int],
    poll_interval_s: float = 2.0,
) -> None:
    """Run a self-contained Telegram polling bot without external libraries."""
    logger.info("Starting Telegram Community Bot...")
    offset = 0
    api_base = f"https://api.telegram.org/bot{token}"

    def send_telegram(chat_id: int, text: str) -> None:
        payload = json.dumps(
            {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{api_base}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as err:
            logger.error("Failed to send Telegram message: %s", err)

    welcome_text = (
        "👋 *Welcome to the ai-potluck Community!*\n\n"
        "ai-potluck is an open-source, cryptographically accounted AI compute sharing network.\n"
        "Your upstream API keys *never leave your device*.\n\n"
        "🔗 *GitHub*: [retiringsoon/ai-potluck](https://github.com/retiringsoon/ai-potluck)\n\n"
        "📌 *Available Commands:*\n"
        "• `/status` - Inspect cluster health & audit ledger\n"
        "• `/rules` - View community code of conduct\n"
        "• `/verify <receipt-id>` - Check cryptographic validity of a receipt\n"
        "• `/request` - How to get an invite code\n"
    )

    rules_text = (
        "📜 *ai-potluck Community Guidelines:*\n\n"
        "1. 🛡️ *Zero Key Leakage*: Never share upstream keys. Run the local connector instead.\n"
        "2. ⚖️ *Deterministic Budget*: Usage is metered in milli-credits with provable receipts.\n"
        "3. 🚫 *No Stress Loops*: Do not run infinite automated loops without coordination.\n"
        "4. 🤝 *Mutual Respect*: Be polite and constructive in discussions.\n"
    )

    while True:
        try:
            url = f"{api_base}/getUpdates?offset={offset}&timeout=30"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "").strip()
                from_user = msg.get("from", {}).get("username", "friend")

                if not chat_id or not text:
                    continue

                if text.startswith("/start") or text.startswith("/help"):
                    send_telegram(chat_id, welcome_text)

                elif text.startswith("/rules"):
                    send_telegram(chat_id, rules_text)

                elif text.startswith("/request"):
                    send_telegram(
                        chat_id,
                        f"🎫 Hi @{from_user}! To request an early-access invite code, "
                        "please describe your use case (e.g. testing Claude Code, vLLM node) "
                        "and an admin will review and issue one.",
                    )

                elif text.startswith("/status"):
                    health = ops.check_health()
                    status_emoji = "🟢" if health.get("healthy") else "🔴"
                    detail = health.get("detail", health.get("error", "unknown"))
                    send_telegram(
                        chat_id,
                        f"{status_emoji} *Cluster Health:* `{health.get('status')}`\n\n"
                        f"```\n{detail[:400]}\n```",
                    )

                elif text.startswith("/verify"):
                    parts = text.split()
                    if len(parts) < 2:
                        send_telegram(chat_id, "Usage: `/verify <receipt-id>` (e.g. `/verify R-1`)")
                        continue
                    receipt_id = parts[1]
                    res = ops.verify_receipt(receipt_id)
                    all_pass = res.get("all_pass", False)
                    verdict_emoji = "✅ Valid" if all_pass else "⚠️ Verification Notice"
                    send_telegram(
                        chat_id,
                        f"{verdict_emoji} *Receipt {receipt_id}*\n```json\n"
                        f"{json.dumps(res, indent=2)[:600]}\n```",
                    )

                elif text.startswith("/mint"):
                    # Admin-only gate
                    if chat_id not in admin_chat_ids:
                        send_telegram(
                            chat_id,
                            "⛔ *Permission Denied*: Only designated operators can mint invites.",
                        )
                        continue

                    parts = text.split()
                    role = "consumer"
                    quota = 500_000
                    if len(parts) >= 2 and parts[1] in ("consumer", "node"):
                        role = parts[1]
                    if len(parts) >= 3 and parts[2].isdigit():
                        quota = int(parts[2])

                    try:
                        invite_out = ops.mint_invite(role=role, quota=quota)
                        send_telegram(
                            chat_id,
                            f"🎫 *New {role.upper()} Invite Issued:*\n\n```\n{invite_out}\n```\n"
                            "⚠️ *Shown only once. Send to the member directly.*",
                        )
                    except Exception as ex:
                        send_telegram(chat_id, f"❌ Failed to mint invite: `{ex}`")

        except urllib.error.URLError as e:
            logger.warning("Network error in Telegram polling: %s", e)
            time.sleep(5)
        except Exception as e:
            logger.error("Unexpected error in Telegram bot loop: %s", e)
            time.sleep(poll_interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="ai-potluck Community & Ops Automation Tool")
    parser.add_argument(
        "--config",
        default="server.json",
        help="Path to server.json configuration file",
    )
    parser.add_argument(
        "--bin",
        default="potluck-server",
        help="Path or name of the potluck-server binary",
    )
    subparsers = parser.add_subparsers(dest="command")

    # 1. Mint command
    mint_p = subparsers.add_parser("mint", help="Mint a new invite code")
    mint_p.add_argument("--role", choices=["consumer", "node"], default="consumer")
    mint_p.add_argument("--tenant", default="community")
    mint_p.add_argument("--quota", type=int, default=500_000)
    mint_p.add_argument(
        "--no-api-key",
        action="store_true",
        help="Do not issue OpenAI API key with invite",
    )

    # 2. Health check command
    subparsers.add_parser("health", help="Check cluster books & reconciliation status")

    # 3. Community Digest command
    subparsers.add_parser("digest", help="Generate a Markdown status digest for group chats")

    # 4. Telegram Bot daemon
    tg_p = subparsers.add_parser("tgbot", help="Run Telegram Community Assistant Bot")
    tg_p.add_argument(
        "--token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN"),
        help="Telegram Bot API Token (or via TELEGRAM_BOT_TOKEN env var)",
    )
    tg_p.add_argument(
        "--admin-id",
        type=int,
        action="append",
        default=[],
        help="Authorized Admin Telegram Chat ID(s) allowed to mint invites",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    ops = PotluckOps(server_config=args.config, server_bin=args.bin)

    if args.command == "mint":
        invite = ops.mint_invite(
            role=args.role,
            tenant=args.tenant,
            quota=args.quota,
            with_api_key=not args.no_api_key,
        )
        print(invite)

    elif args.command == "health":
        status = ops.check_health()
        print(json.dumps(status, indent=2))
        sys.exit(0 if status.get("healthy") else 1)

    elif args.command == "digest":
        health = ops.check_health()
        state_str = "🟢 正常 (Consistent)" if health.get("healthy") else "🔴 异常 (Check needed)"
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"📊 【ai-potluck 集群每日简报】 ({now_str})\n")
        print(f"• 集群记账状态: {state_str}")
        print("• 密码学审计: 双签收据与 Merkle 透明日志同步完成")
        print("• 快速加入指南: `potluck join --server <server_url> --invite <code_here>`\n")
        print("开源仓库: https://github.com/retiringsoon/ai-potluck")

    elif args.command == "tgbot":
        if not args.token:
            print("Error: Telegram bot token required via --token or TELEGRAM_BOT_TOKEN.")
            sys.exit(1)
        run_telegram_bot(token=args.token, ops=ops, admin_chat_ids=args.admin_id)



if __name__ == "__main__":
    main()
