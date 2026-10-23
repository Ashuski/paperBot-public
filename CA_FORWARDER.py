"""
=============================================================
  Phase 1 — Pumpfun Ultimate Alert Forwarder Bot
  
  Monitors @pfultimate for new token calls.
  When a call card is detected, extracts CA + ticker
  and sends a clean message to your private group
  so Phanes bot can pick it up instantly.

  Patterns validated against 4,734 real channel messages.
=============================================================

SETUP:
  pip install telethon colorama

USAGE:
  python phase1_forwarder.py
  Enter credentials when prompted, then leave it running 24/7.
"""

import asyncio
import re
import logging
from datetime import datetime, timezone
from colorama import Fore, Style, init
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import os

init(autoreset=True)

# ─────────────────────────────────────────────
#  CONFIG  ← Set via env vars (.env locally, Variables on Railway)
# ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_ID      = os.getenv("API_ID", "")
API_HASH    = os.getenv("API_HASH", "")
SOURCE      = os.getenv("SOURCE", "https://t.me/pfultimate")
DESTINATION = int(os.getenv("DESTINATION", "0"))

# ─────────────────────────────────────────────
#  PATTERNS (validated against real channel data)
# ─────────────────────────────────────────────

# CA in backticks in msg.text: `5rYNmBx...pump`
# Catches both pump.fun (ends in pump) and regular Solana CAs
CA_BACKTICK  = re.compile(r"`([1-9A-HJ-NP-Za-km-z]{32,50}(?:pump|[A-Za-z0-9]{1,10}))`")

# Ticker from title line: [Token Name (TICKER)]
TITLE_TICKER = re.compile(r"\[.+?\(([^)]{1,20})\)\]")

# Is this a call card? Must have CA + bonding curve + cap + copyright
BONDING_RE   = re.compile(r"Bonding Curve.*?([\d.]+)%")
MCAP_RE      = re.compile(r"`Cap:`\s*\**([\.\d]+)([KkMm])")

# Is this an update message? (skip these)
MULTIPLIER_RE = re.compile(r"(\d+(?:\.\d+)?)x", re.IGNORECASE)
FROM_TO_RE    = re.compile(r"[Ff]rom.*?[\d.]+[KkMm].*?within", re.IGNORECASE)
BONDED_RE     = re.compile(r"\$([A-Z0-9 ]{1,20})\s+has been \*{0,2}bonded\*{0,2}")


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def is_call_card(text: str) -> bool:
    """Detect initial call cards — the ones we want to forward."""
    return (
        bool(CA_BACKTICK.search(text)) and
        bool(BONDING_RE.search(text))  and
        bool(MCAP_RE.search(text))     and
        ("Juggernautnn" in text or "pf_ultimate" in text or "pfultimate" in text)
    )


def is_update_message(text: str) -> bool:
    """Detect update/bonded messages — skip these."""
    has_mult    = bool(MULTIPLIER_RE.search(text))
    has_from_to = bool(FROM_TO_RE.search(text))
    has_bonded  = bool(BONDED_RE.search(text))
    return (has_mult and has_from_to) or has_bonded


def extract_ca(text: str):
    m = CA_BACKTICK.search(text)
    return m.group(1) if m else None


def extract_ticker(text: str):
    m = TITLE_TICKER.search(text)
    if m:
        return m.group(1).strip().upper()
    return None


def extract_mcap(text: str):
    m = MCAP_RE.search(text)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == "K":
        return val * 1_000
    if suffix == "M":
        return val * 1_000_000
    return val


def extract_bonding(text: str):
    m = BONDING_RE.search(text)
    return float(m.group(1)) if m else None


def build_forward_message(ca: str, ticker: str, mcap, bonding, source_name: str) -> str:
    mcap_str    = f"${mcap:,.0f}" if mcap else "N/A"
    bonding_str = f"{bonding:.1f}%" if bonding else "N/A"
    ticker_str  = f"${ticker}" if ticker else "Unknown"
    return (
        f"🚨 *New Call — {ticker_str}*\n"
        f"{'─' * 30}\n"
        f"📋 *CA:* `{ca}`\n"
        f"📊 *MCap:* {mcap_str} | *BC:* {bonding_str}\n"
        f"{'─' * 30}\n"
        f"🔗 [Pump.fun](https://pump.fun/{ca}) · "
        f"[DexScreener](https://dexscreener.com/solana/{ca}) · "
        f"[Birdeye](https://birdeye.so/token/{ca})\n"
        f"📡 _{source_name}_  🕐 _{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )


# ─────────────────────────────────────────────
#  MAIN BOT
# ─────────────────────────────────────────────
async def main():
    print(f"\n{Fore.YELLOW}╔══════════════════════════════════════════╗")
    print(f"║   🤖  Phase 1 — Call Forwarder Bot      ║")
    print(f"╚══════════════════════════════════════════╝{Style.RESET_ALL}\n")

    api_id      = API_ID
    api_hash    = API_HASH
    source      = SOURCE
    destination = DESTINATION

    session_str = os.getenv("SESSION_STRING", "")
    if session_str:
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        print(f"  {Fore.GREEN}Using StringSession (cloud mode){Style.RESET_ALL}")
    else:
        client = TelegramClient("phase1_session", int(api_id), api_hash)
    await client.start()

    source_entity = await client.get_entity(source)
    source_name   = getattr(source_entity, "title", source)

    try:
        dest_entity = await client.get_entity(destination)
        dest_name   = getattr(dest_entity, "title", str(destination))
    except Exception as e:
        print(f"{Fore.RED}❌ Could not resolve group: {e}{Style.RESET_ALL}")
        return

    print(f"\n{Fore.GREEN}✅ Monitoring  : {source_name}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}✅ Forwarding  : {dest_name}{Style.RESET_ALL}")
    print(f"\n{Fore.CYAN}👂 Listening for new calls... (Ctrl+C to stop){Style.RESET_ALL}\n")

    # Track forwarded CAs to avoid duplicates within the session
    forwarded_cas = set()
    call_count    = 0

    @client.on(events.NewMessage(chats=source_entity))
    async def handler(event):
        nonlocal call_count
        text = event.message.text or ""
        if not text:
            return

        # Skip update/bonded messages — only want call cards
        if is_update_message(text):
            return

        # Must be a valid call card
        if not is_call_card(text):
            return

        ca = extract_ca(text)
        if not ca:
            return

        # Skip duplicates
        if ca in forwarded_cas:
            print(f"{Fore.YELLOW}[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"⚠️  Duplicate skipped: {ca[:12]}...{Style.RESET_ALL}")
            return

        forwarded_cas.add(ca)
        call_count += 1

        ticker  = extract_ticker(text)
        mcap    = extract_mcap(text)
        bonding = extract_bonding(text)
        msg     = build_forward_message(ca, ticker, mcap, bonding, source_name)

        await client.send_message(destination, msg, parse_mode="markdown")

        ticker_display = f"${ticker}" if ticker else "?"
        mcap_display   = f"${mcap:,.0f}" if mcap else "N/A"
        print(
            f"{Fore.GREEN}[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
            f"#{call_count} ✅ Forwarded: {ticker_display} | "
            f"MCap: {mcap_display} | {ca[:16]}...{Style.RESET_ALL}"
        )

    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Bot stopped.{Style.RESET_ALL}")