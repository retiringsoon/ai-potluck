#!/usr/bin/env python3
"""potluck_discord_bot.py - Discord Community Agent for ai-potluck.

Features:
1. Dual-mode Discord integration:
   - Webhook Broadcaster: Zero-dependency broadcast tool for announcements & status.
   - Interactive Bot Gateway: Full discord.py client supporting slash/prefix commands:
     • `!status`: Checks ai-potluck cluster health & ledger consistency.
     • `!rules`: View community guidelines.
     • `!verify <id>`: Cryptographically audits receipts.
     • `!mint <role> <quota>`: Admin-only invite code generation.
2. Rich Embed cards with status indicators and quick-link buttons.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Add scripts directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from potluck_community_bot import PotluckOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("discord_bot")


def send_discord_webhook(
    webhook_url: str,
    title: str,
    description: str,
    *,
    color: int = 0x5865F2,
) -> None:
    """Send a rich embed message via Discord Webhook without extra dependencies."""
    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": "ai-potluck Community Gateway"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "ai-potluck-bot/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        logger.info("Discord webhook delivered successfully.")
    except Exception as e:
        logger.error("Failed to post Discord webhook: %s", e)
        raise


def run_discord_client(token: str, ops: PotluckOps, admin_ids: list[int]) -> None:
    """Run full interactive Discord Bot using discord.py."""
    try:
        import discord  # type: ignore[import-not-found]
        from discord.ext import commands  # type: ignore[import-not-found]
    except ImportError:
        print("\n[!] 'discord.py' is not installed for full bot gateway mode.")
        print("To enable real-time interactive bot commands, install discord.py:")
        print("    uv pip install discord.py  (or pip install discord.py)\n")
        print("Alternative: You can use Webhook mode without any dependencies:")
        print("    ./scripts/potluck_discord_bot.py webhook --url <WEBHOOK_URL> --title ...\n")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    @bot.event  # type: ignore[untyped-decorator]
    async def on_ready() -> None:
        user_id = bot.user.id if bot.user else "N/A"
        logger.info("Logged in as Discord Bot: %s (ID: %s)", bot.user, user_id)
        if bot.user:
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name="ai-potluck compute pool",
            )
            await bot.change_presence(activity=activity)

    @bot.command(name="help")  # type: ignore[untyped-decorator]
    async def cmd_help(ctx: Any) -> None:
        embed = discord.Embed(
            title="👋 Welcome to ai-potluck Community!",
            description=(
                "**ai-potluck** is an open-source, cryptographically accounted "
                "AI capacity sharing network.\n\n"
                "📌 **Commands:**\n"
                "• `!status` - Check cluster health & ledger consistency\n"
                "• `!rules` - View community guidelines\n"
                "• `!verify <receipt-id>` - Cryptographically verify a receipt\n"
                "• `!request` - Request an early-access invite code"
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="GitHub",
            value="https://github.com/retiringsoon/ai-potluck",
            inline=False,
        )
        await ctx.send(embed=embed)

    @bot.command(name="rules")  # type: ignore[untyped-decorator]
    async def cmd_rules(ctx: Any) -> None:
        embed = discord.Embed(
            title="📜 Community Guidelines",
            description=(
                "1. 🛡️ **Zero Key Leakage**: Contributors run the local connector.\n"
                "2. ⚖️ **Provable Billing**: Every turn produces signed receipts.\n"
                "3. 🚫 **No Infinite Loops**: Do not run unconstrained stress agents."
            ),
            color=0xFEE75C,
        )
        await ctx.send(embed=embed)

    @bot.command(name="status")  # type: ignore[untyped-decorator]
    async def cmd_status(ctx: Any) -> None:
        health = ops.check_health()
        healthy = bool(health.get("healthy"))
        color = 0x57F287 if healthy else 0xED4245
        status_text = "🟢 Consistent & Healthy" if healthy else "🔴 Discrepancy Detected"
        detail = str(health.get("detail", health.get("error", "unknown")))[:300]

        embed = discord.Embed(
            title=f"📊 Cluster Status: {status_text}",
            description=f"```\n{detail}\n```",
            color=color,
        )
        await ctx.send(embed=embed)

    @bot.command(name="verify")  # type: ignore[untyped-decorator]
    async def cmd_verify(ctx: Any, receipt_id: str = "") -> None:
        if not receipt_id:
            await ctx.send("⚠️ Usage: `!verify <receipt-id>` (e.g. `!verify R-1`)")
            return
        res = ops.verify_receipt(receipt_id)
        all_pass = bool(res.get("all_pass"))
        color = 0x57F287 if all_pass else 0xFEE75C
        embed = discord.Embed(
            title=f"Receipt Audit: {receipt_id}",
            description=f"```json\n{json.dumps(res, indent=2)[:600]}\n```",
            color=color,
        )
        await ctx.send(embed=embed)

    @bot.command(name="mint")  # type: ignore[untyped-decorator]
    async def cmd_mint(ctx: Any, role: str = "consumer", quota: int = 500_000) -> None:

        if ctx.author.id not in admin_ids:
            msg = "⛔ **Permission Denied**: Only authorized admins can mint invite codes."
            await ctx.send(msg)
            return

        try:
            invite = ops.mint_invite(role=role, quota=quota)
            # DM the admin to avoid leaking invite to public channel
            try:
                await ctx.author.send(
                    f"🎫 **New {role.upper()} Invite Code:**\n```\n{invite}\n```"
                )
                await ctx.send(f"✅ Invite minted and sent to your DM, {ctx.author.mention}!")
            except Exception:
                await ctx.send(f"🎫 **New {role.upper()} Invite Code:**\n```\n{invite}\n```")
        except Exception as e:
            await ctx.send(f"❌ Failed to mint invite: `{e}`")

    bot.run(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="ai-potluck Discord Community Agent")
    subparsers = parser.add_subparsers(dest="subcommand")

    # 1. Webhook mode
    wh_p = subparsers.add_parser("webhook", help="Send broadcast via Discord Webhook")
    wh_p.add_argument("--url", required=True, help="Discord Webhook URL")
    wh_p.add_argument("--title", default="ai-potluck Community Update", help="Embed Title")
    wh_p.add_argument("--message", required=True, help="Embed Description / Message body")

    # 2. Interactive Bot mode
    bot_p = subparsers.add_parser("bot", help="Run interactive Discord Bot Gateway")
    bot_p.add_argument(
        "--token",
        default=os.environ.get("DISCORD_BOT_TOKEN"),
        help="Discord Bot Token (or DISCORD_BOT_TOKEN env)",
    )
    bot_p.add_argument(
        "--config",
        default="server.json",
        help="Path to server.json configuration",
    )
    bot_p.add_argument(
        "--bin",
        default="potluck-server",
        help="Path or name of potluck-server binary",
    )
    bot_p.add_argument(
        "--admin-id",
        type=int,
        action="append",
        default=[],
        help="Authorized Admin Discord User ID(s)",
    )

    args = parser.parse_args()
    if not args.subcommand:
        parser.print_help()
        sys.exit(1)

    if args.subcommand == "webhook":
        send_discord_webhook(args.url, args.title, args.message)
    elif args.subcommand == "bot":
        if not args.token:
            print("Error: Discord Bot Token required via --token or DISCORD_BOT_TOKEN env.")
            sys.exit(1)
        ops = PotluckOps(server_config=args.config, server_bin=args.bin)
        run_discord_client(token=args.token, ops=ops, admin_ids=args.admin_id)


if __name__ == "__main__":
    main()
