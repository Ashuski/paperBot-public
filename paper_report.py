"""
=============================================================
  Paper Trading Report Analyzer
  
  Reads paper_trades.json and generates:
  - Full P&L breakdown
  - Real win/loss rate
  - SL-before-TP rate (how often you'd be stopped out first)
  - Optimal SL recommendation
  - Price path statistics
  - Best/worst trades

USAGE:
  python paper_report.py
=============================================================
"""

import json
import os
import statistics
from datetime import datetime
from collections import defaultdict

try:
    from colorama import Fore, Style, init
    from tabulate import tabulate
    init(autoreset=True)
except ImportError:
    class Fore:
        GREEN=RED=YELLOW=CYAN=RESET=""
    class Style:
        RESET_ALL=""
    def tabulate(d, **k): return str(d)

LOG_FILE = "paper_trades.json"
TP = 1.00
SL = 0.20


def load():
    if not os.path.exists(LOG_FILE):
        print(f"No {LOG_FILE} found. Run paper_trading_bot.py first.")
        return None
    with open(LOG_FILE) as f:
        return json.load(f)


def analyze(log):
    trades  = log["trades"]
    closed  = [t for t in trades if t["status"] != "OPEN"]
    open_t  = [t for t in trades if t["status"] == "OPEN"]

    if not closed:
        print("No closed trades yet.")
        return

    wins      = [t for t in closed if t["status"] == "TP_HIT"]
    losses    = [t for t in closed if t["status"] in ("SL_HIT", "TIMEOUT", "DELISTED")]
    sl_bef_tp = [t for t in closed if t.get("sl_before_tp")]
    timeouts  = [t for t in closed if t["status"] == "TIMEOUT"]

    total_pnl    = sum(t.get("pnl") or 0 for t in closed)
    win_rate     = len(wins) / len(closed) * 100
    sl_bef_rate  = len(sl_bef_tp) / len(closed) * 100

    W = 65
    print(f"\n{'='*W}")
    print(f"{Fore.YELLOW}  PAPER TRADING REPORT{Style.RESET_ALL}")
    print(f"{'='*W}")
    print(f"\n  Total calls  : {len(trades)}")
    print(f"  Open trades  : {len(open_t)}")
    print(f"  Closed trades: {len(closed)}")

    print(f"\n  {Fore.GREEN}✅ TP Hit (wins)    : {len(wins)} ({win_rate:.1f}%){Style.RESET_ALL}")
    print(f"  {Fore.RED}❌ SL Hit (losses)  : {len([t for t in losses if t['status']=='SL_HIT'])} ({len([t for t in losses if t['status']=='SL_HIT'])/len(closed)*100:.1f}%){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}⏰ Timeout          : {len(timeouts)} ({len(timeouts)/len(closed)*100:.1f}%){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}⚠️  SL before TP    : {len(sl_bef_tp)} ({sl_bef_rate:.1f}%){Style.RESET_ALL}")
    print(f"     → These pumped eventually but SL would have fired first")

    pnl_c = Fore.GREEN if total_pnl >= 0 else Fore.RED
    print(f"\n  {pnl_c}Total P&L    : {total_pnl:>+.4f} SOL{Style.RESET_ALL}")
    print(f"  {pnl_c}Avg per trade: {total_pnl/len(closed):>+.4f} SOL{Style.RESET_ALL}")

    # Time to TP/SL
    tp_times = [t["minutes_to_tp"] for t in wins if t.get("minutes_to_tp")]
    sl_times = [t["minutes_to_sl"] for t in losses if t.get("minutes_to_sl")]

    if tp_times:
        print(f"\n  Avg time to TP  : {statistics.mean(tp_times):.0f}m  (median {statistics.median(tp_times):.0f}m)")
        tp_u5   = sum(1 for m in tp_times if m <= 5)
        tp_u15  = sum(1 for m in tp_times if 5 < m <= 15)
        tp_u60  = sum(1 for m in tp_times if 15 < m <= 60)
        tp_over = sum(1 for m in tp_times if m > 60)
        print(f"  TP speed: <5m={tp_u5}  5-15m={tp_u15}  15-60m={tp_u60}  60m+={tp_over}")

    if sl_times:
        print(f"  Avg time to SL  : {statistics.mean(sl_times):.0f}m  (median {statistics.median(sl_times):.0f}m)")

    # Min drop analysis — THE KEY METRIC
    print(f"\n{'-'*W}")
    print(f"{Fore.CYAN}  MIN DROP ANALYSIS (did tokens dip before pumping?){Style.RESET_ALL}")
    print(f"{'-'*W}")

    all_drops = [t.get("min_drop_pct") for t in closed if t.get("min_drop_pct") is not None]
    win_drops = [t.get("min_drop_pct") for t in wins   if t.get("min_drop_pct") is not None]

    if all_drops:
        print(f"\n  All trades — min drop stats:")
        print(f"    Average min drop : {statistics.mean(all_drops):>+.1f}%")
        print(f"    Median min drop  : {statistics.median(all_drops):>+.1f}%")
        print(f"    Worst drop       : {min(all_drops):>+.1f}%")

        print(f"\n  Of ALL trades, how many dipped X% at some point:")
        for thr in [5, 10, 15, 20, 25, 30]:
            n   = sum(1 for d in all_drops if d <= -thr)
            pct = n / len(all_drops) * 100
            print(f"    Dipped -{thr}%+  : {n:>4} ({pct:.1f}%)")

    if win_drops:
        print(f"\n  Of WINNING trades only — how many dipped before hitting TP:")
        for thr in [5, 10, 15, 20, 25, 30]:
            n   = sum(1 for d in win_drops if d <= -thr)
            pct = n / len(win_drops) * 100
            print(f"    Dipped -{thr}% before TP: {n:>4} ({pct:.1f}%) ← would be stopped out")

    # Optimal SL recommendation
    print(f"\n{'-'*W}")
    print(f"{Fore.CYAN}  OPTIMAL SL LEVEL (based on real price paths){Style.RESET_ALL}")
    print(f"{'-'*W}")
    print(f"\n  {'SL':>6}  {'Real Wins':>10}  {'Real Losses':>12}  {'Net SOL':>9}  {'Stopped Winners':>16}")
    print(f"  {'-'*60}")

    all_verified = [t for t in closed if t.get("min_drop_pct") is not None and t.get("max_gain_pct") is not None]
    if all_verified:
        for sl_test in [5, 8, 10, 12, 15, 20, 25, 30]:
            sl_dec = sl_test / 100
            # Win: max_gain >= TP AND min_drop > -SL (SL never triggered before TP)
            real_wins_n = sum(
                1 for t in all_verified
                if (t.get("max_gain_pct") or 0) >= TP * 100
                and (t.get("min_drop_pct") or 0) > -sl_test
            )
            real_loss_n = len(all_verified) - real_wins_n
            net = (real_wins_n * TP) - (real_loss_n * sl_dec)
            stopped = sum(
                1 for t in all_verified
                if (t.get("max_gain_pct") or 0) >= TP * 100
                and (t.get("min_drop_pct") or 0) <= -sl_test
            )
            marker = " ← OPTIMAL" if net == max(
                (real_wins_n2 * TP) - ((len(all_verified) - real_wins_n2) * (sl2/100))
                for sl2 in [5,8,10,12,15,20,25,30]
                for real_wins_n2 in [sum(
                    1 for t in all_verified
                    if (t.get("max_gain_pct") or 0) >= TP*100
                    and (t.get("min_drop_pct") or 0) > -sl2
                )]
            ) else ""
            print(f"  {sl_test:>5}%  {real_wins_n:>10}  {real_loss_n:>12}  {net:>+8.2f}  {stopped:>14}{Fore.GREEN if marker else ''}{marker}{Style.RESET_ALL}")

    # Best/worst trades
    print(f"\n{'-'*W}")
    print(f"{Fore.GREEN}  TOP 5 BEST TRADES{Style.RESET_ALL}")
    print(f"{'-'*W}")
    best = sorted(wins, key=lambda t: t.get("minutes_to_tp") or 999)[:5]
    rows = []
    for t in best:
        rows.append([
            f"${t['ticker']}",
            t.get("minutes_to_tp", "?"),
            f"{(t.get('min_drop_pct') or 0):>+.1f}%",
            f"+{TP*100:.0f}%",
            f"+{TP:.2f} SOL",
            "⚠️" if t.get("sl_before_tp") else "✓"
        ])
    print(tabulate(rows, headers=["Token","Mins to TP","Min Drop","Gain","P&L","Clean?"], tablefmt="rounded_outline"))

    print(f"\n{'-'*W}")
    print(f"{Fore.RED}  TOP 5 WORST TRADES{Style.RESET_ALL}")
    print(f"{'-'*W}")
    worst_sorted = sorted(losses, key=lambda t: t.get("min_drop_pct") or 0)[:5]
    rows2 = []
    for t in worst_sorted:
        rows2.append([
            f"${t['ticker']}",
            t["status"],
            f"{(t.get('min_drop_pct') or 0):>+.1f}%",
            f"{(t.get('max_gain_pct') or 0):>+.1f}%",
            f"{(t.get('pnl') or 0):>+.2f} SOL"
        ])
    print(tabulate(rows2, headers=["Token","Status","Min Drop","Max Gain","P&L"], tablefmt="rounded_outline"))

    print(f"\n{'='*W}\n")


if __name__ == "__main__":
    log = load()
    if log:
        analyze(log)
