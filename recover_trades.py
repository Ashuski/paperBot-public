"""
=============================================================
  Trade Recovery Module
  
  Run this BEFORE starting the bot after being offline.
  It checks DexScreener for any open trades and tries to
  figure out what happened while the bot was offline.
  
  For each open trade it fetches:
  - Current price vs entry price
  - Whether TP or SL was likely hit
  - Marks them appropriately in paper_trades.json

USAGE:
  python recover_trades.py
  Then run paper_trading_bot.py as normal
=============================================================
"""

import asyncio
import aiohttp
import json
import os
from datetime import datetime, timezone, timedelta
from colorama import Fore, Style, init

init(autoreset=True)

LOG_FILE   = "paper_trades.json"
TP         = 1.00
SL         = 0.20


def load_log():
    if not os.path.exists(LOG_FILE):
        print(f"{Fore.RED}No {LOG_FILE} found.{Style.RESET_ALL}")
        return None
    with open(LOG_FILE) as f:
        return json.load(f)


def save_log(log):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


async def get_token_data(session, ca):
    """Get current price + 24h price change data"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data  = await r.json()
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs:
                return None
            pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            return {
                "price":       float(pair.get("priceUsd") or 0),
                "liquidity":   float((pair.get("liquidity") or {}).get("usd") or 0),
                "change_24h":  float((pair.get("priceChange") or {}).get("h24") or 0),
                "change_6h":   float((pair.get("priceChange") or {}).get("h6") or 0),
                "change_1h":   float((pair.get("priceChange") or {}).get("h1") or 0),
                "volume_24h":  float((pair.get("volume") or {}).get("h24") or 0),
                "pair_url":    pair.get("url", ""),
            }
    except Exception as e:
        return None


async def recover():
    log = load_log()
    if not log:
        return

    open_trades = [t for t in log["trades"] if t["status"] == "OPEN"]

    if not open_trades:
        print(f"{Fore.GREEN}No open trades to recover.{Style.RESET_ALL}")
        return

    print(f"\n{Fore.YELLOW}╔══════════════════════════════════════════╗")
    print(f"║         Trade Recovery Tool             ║")
    print(f"╚══════════════════════════════════════════╝{Style.RESET_ALL}")
    print(f"\n{Fore.CYAN}Found {len(open_trades)} open trades to check...{Style.RESET_ALL}\n")

    recovered_tp  = 0
    recovered_sl  = 0
    still_open    = 0
    dead          = 0

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        for trade in open_trades:
            ca           = trade["ca"]
            ticker       = trade["ticker"]
            entry_price  = trade["entry_price"]
            tp_price     = entry_price * (1 + TP)
            sl_price     = entry_price * (1 - SL)
            entry_time   = datetime.fromisoformat(trade["called_at"])
            now          = datetime.now(timezone.utc)
            hours_open   = (now - entry_time).total_seconds() / 3600

            print(f"  Checking ${ticker:<12} (open for {hours_open:.1f}h)...")

            token_data = await get_token_data(session, ca)

            if not token_data or token_data["price"] == 0:
                # Token is dead — assume SL hit
                trade["status"]      = "SL_HIT"
                trade["exit_price"]  = sl_price
                trade["exit_time"]   = now.isoformat()
                trade["exit_reason"] = "Token dead/delisted while bot was offline (SL assumed)"
                trade["pnl"]         = round(-1.0 * SL, 4)
                trade["pct_change"]  = -SL * 100
                trade["recovered"]   = True
                trade["recovery_note"] = "Token not found on DexScreener after restart"
                dead += 1
                print(f"    {Fore.RED}❌ DEAD — SL assumed (-{SL*100:.0f}%){Style.RESET_ALL}")

            else:
                current_price = token_data["price"]
                current_pct   = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                change_24h    = token_data["change_24h"]
                change_6h     = token_data["change_6h"]
                liq           = token_data["liquidity"]

                # Estimate what happened using price change windows
                # If current price is above TP → TP was hit at some point
                # If current price is far below entry → SL was hit
                # The 24h/6h/1h changes give us hints about the journey

                if current_price >= tp_price:
                    # Currently above TP — definitely hit TP
                    trade["status"]        = "TP_HIT"
                    trade["exit_price"]    = tp_price
                    trade["exit_time"]     = now.isoformat()
                    trade["exit_reason"]   = f"TP confirmed — current price {current_pct:+.1f}% above entry"
                    trade["pnl"]           = round(1.0 * TP, 4)
                    trade["pct_change"]    = TP * 100
                    trade["recovered"]     = True
                    trade["recovery_note"] = f"Current price ${current_price:.8f} ({current_pct:+.1f}%)"
                    recovered_tp += 1
                    print(f"    {Fore.GREEN}✅ TP HIT — still above TP price (+{TP*100:.0f}% confirmed){Style.RESET_ALL}")

                elif current_price <= sl_price:
                    # Currently below SL — SL was hit
                    trade["status"]        = "SL_HIT"
                    trade["exit_price"]    = sl_price
                    trade["exit_time"]     = now.isoformat()
                    trade["exit_reason"]   = f"SL confirmed — current price {current_pct:+.1f}% below entry"
                    trade["pnl"]           = round(-1.0 * SL, 4)
                    trade["pct_change"]    = -SL * 100
                    trade["recovered"]     = True
                    trade["recovery_note"] = f"Current price ${current_price:.8f} ({current_pct:+.1f}%)"
                    recovered_sl += 1
                    print(f"    {Fore.RED}❌ SL HIT — below SL price ({current_pct:+.1f}%){Style.RESET_ALL}")

                else:
                    # Price is between SL and TP — ambiguous
                    # Use 24h/6h high as proxy
                    # If 24h high was >= TP% → likely hit TP at some point
                    # This is an estimate — we note it as UNCERTAIN

                    # Best estimate: if change_24h shows it went above TP at peak
                    # DexScreener change_24h is current vs 24h ago, not the high
                    # So we can only flag as uncertain

                    if hours_open > 24:
                        # Been open more than 24h — max hold exceeded, close it
                        trade["status"]        = "TIMEOUT"
                        trade["exit_price"]    = current_price
                        trade["exit_time"]     = now.isoformat()
                        trade["exit_reason"]   = f"Max hold time exceeded — closed at {current_pct:+.1f}%"
                        trade["pnl"]           = round(1.0 * (current_pct / 100), 4)
                        trade["pct_change"]    = round(current_pct, 2)
                        trade["recovered"]     = True
                        trade["recovery_note"] = f"24h timeout. Current: ${current_price:.8f} ({current_pct:+.1f}%)"
                        print(f"    {Fore.YELLOW}⏰ TIMEOUT — {hours_open:.0f}h open, closed at {current_pct:+.1f}%{Style.RESET_ALL}")
                    else:
                        # Still within hold window, price between SL and TP
                        # Leave open — bot will pick it up and continue monitoring
                        trade["recovery_note"] = (
                            f"Price between SL/TP at restart: {current_pct:+.1f}%. "
                            f"Bot will resume monitoring. "
                            f"24h change: {change_24h:+.1f}%, 6h: {change_6h:+.1f}%"
                        )
                        still_open += 1
                        print(f"    {Fore.CYAN}🔄 STILL OPEN — resuming monitor "
                              f"(current: {current_pct:+.1f}%, liq: ${liq:,.0f}){Style.RESET_ALL}")

            await asyncio.sleep(0.5)

    # Update summary
    closed  = [t for t in log["trades"] if t["status"] != "OPEN"]
    wins    = [t for t in closed if t["status"] == "TP_HIT"]
    losses  = [t for t in closed if t["status"] in ("SL_HIT", "TIMEOUT")]
    total_pnl = sum(t.get("pnl") or 0 for t in closed)

    log["summary"] = {
        "total_calls":       len(log["trades"]),
        "open":              len([t for t in log["trades"] if t["status"] == "OPEN"]),
        "closed":            len(closed),
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate":          f"{len(wins)/max(len(closed),1)*100:.1f}%",
        "total_pnl_sol":     round(total_pnl, 4),
        "avg_pnl_per_trade": round(total_pnl / max(len(closed), 1), 4),
        "last_updated":      datetime.now(timezone.utc).isoformat(),
    }

    save_log(log)

    print(f"\n{'='*55}")
    print(f"{Fore.YELLOW}  RECOVERY SUMMARY{Style.RESET_ALL}")
    print(f"{'='*55}")
    print(f"  {Fore.GREEN}TP recovered  : {recovered_tp}{Style.RESET_ALL}")
    print(f"  {Fore.RED}SL recovered  : {recovered_sl}{Style.RESET_ALL}")
    print(f"  {Fore.RED}Dead tokens   : {dead}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}Still open    : {still_open} (bot will resume){Style.RESET_ALL}")
    print(f"\n  Total P&L so far: {Fore.GREEN if total_pnl >= 0 else Fore.RED}{total_pnl:>+.4f} SOL{Style.RESET_ALL}")
    print(f"  Win rate        : {log['summary']['win_rate']}")
    print(f"\n  {Fore.GREEN}✅ Recovery complete — now run paper_trading_bot.py{Style.RESET_ALL}\n")


if __name__ == "__main__":
    asyncio.run(recover())
