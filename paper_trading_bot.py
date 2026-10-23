"""
=============================================================
  Paper Trading Bot v5 — On-Chain Price Engine
  
  Listens to @pfultimate in real time.
  TRADES every single detected CA — 65% TP / 20% SL.
  
  PRICE SOURCE: 4-Tier Seamless Price Engine
  ────────────────────────────────────────────────────────
  1. Bonding Curve RPC  (pre-graduation — on-chain truth)
  2. PumpSwap AMM Pool  (post-graduation — on-chain, instant)
  3. Jupiter Price API   (fallback #1)
  4. DexScreener API     (fallback #2, last resort)

  Zero graduation blind spots. When bonding curve completes,
  PumpSwap pool is read in the SAME poll cycle. No gaps.
  
  FEATURES: Milestones, Shadow TP/SL, Live Dashboard
  
  /        — P&L overview + recent trades
  /live    — real-time open trades (5s refresh)
  /trade/X — full detail + shadow states
  /json    — raw JSON dump

SETUP:
  pip install telethon aiohttp colorama python-dotenv solders

USAGE:
  python paper_trading_bot.py

ENV:
  API_ID, API_HASH, HELIUS_RPC_URL, SESSION_STRING (optional)
=============================================================
"""

import asyncio
import hmac
import aiohttp
import json
import re
import os
import shutil
import struct
import base64
import time
from datetime import datetime, timezone, timedelta
from colorama import Fore, Style, init
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from solders.pubkey import Pubkey

init(autoreset=True)

# ─────────────────────────────────────────────
#  CREDENTIALS
# ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_ID   = os.getenv("API_ID",   "")
API_HASH = os.getenv("API_HASH", "")

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
SOURCE         = "https://t.me/pfultimate"
TAKE_PROFIT    = 0.50
STOP_LOSS      = 0.15
ENTRY_SIZE     = 1.0
MAX_HOLD_HRS   = 24
LOG_FILE       = os.getenv("LOG_FILE", "paper_trades.json")
SESSION_FILE   = "paper_bot_session"
DASHBOARD_PORT = int(os.getenv("PORT", 8080))
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "")
PRICE_POLL_SEC = 5             # TP/SL checked every 5s
PATH_SAVE_SEC  = 30            # price_path written every 30s (lean JSON)

# ── Telegram auto-backup (survives host wipes / trial resets) ──
# The bot's own authenticated Telegram client DMs paper_trades.json to your
# Saved Messages. Data follows your Telegram account, not the hosting account,
# so it survives any number of Railway redeploys / trial resets.
BACKUP_HOURS  = float(os.getenv("BACKUP_HOURS", 6))   # backup interval
BACKUP_MARKER = "#pfpaperbot_backup"                  # tag to identify our backups

# Pump.fun program
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
# Initial real token reserves (for bonding progress calc)
INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000

# PumpSwap AMM program
PUMPSWAP_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMPSWAP_PROGRAM_STR = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
WSOL_MINT_BYTES = bytes(Pubkey.from_string(WSOL_MINT))
STALE_CACHE_MAX_SEC = 120  # use cached price for up to 2 minutes

# ─────────────────────────────────────────────
#  SHADOW STRATEGIES
# ─────────────────────────────────────────────
SHADOW_STRATEGIES = [
    {"name": "35/10",  "tp": 0.35, "sl": 0.10},  # tighter — explore lower end
    {"name": "50/15",  "tp": 0.50, "sl": 0.15},  # live strategy (mirrored for shadow tracking)
    {"name": "65/20",  "tp": 0.65, "sl": 0.20},
    {"name": "80/20",  "tp": 0.80, "sl": 0.20},
    {"name": "100/25", "tp": 1.00, "sl": 0.25},
    {"name": "100/30", "tp": 1.00, "sl": 0.30},
    {"name": "150/35", "tp": 1.50, "sl": 0.35},
]

# ─────────────────────────────────────────────
#  PATTERNS
# ─────────────────────────────────────────────
CA_RE      = re.compile(r"`([1-9A-HJ-NP-Za-km-z]{32,50}(?:pump|[A-Za-z0-9]{1,10}))`")
CA_LOOSE   = re.compile(r"([1-9A-HJ-NP-Za-km-z]{32,50}(?:pump)?)")
TICKER_RE  = re.compile(r"\[.+?\(([^)]{1,20})\)\]")
BONDING_RE = re.compile(r"Bonding Curve.*?([\d.]+)%")
MCAP_RE    = re.compile(r"`Cap:`\s*\**([\.\d]+)([KkMm])")
UPDATE_RE  = re.compile(r"(\d+(?:\.\d+)?)x", re.IGNORECASE)
FROM_RE    = re.compile(r"[Ff]rom.*?([\d.]+[KkMm]).*?within", re.IGNORECASE)
BONDED_RE  = re.compile(r"\$([^\s\[\](){},*]{1,20})\s+has been \*{0,2}bonded\*{0,2}")
UPDATE_TICKER_RE = re.compile(r"\$([^\s\[\](){},*]{1,20})")
UPDATE_MCAP_RE   = re.compile(r"(?:to|at|now)\s*([\d.]+)([KkMm])", re.IGNORECASE)


def is_call_card(text):
    has_ca      = bool(CA_RE.search(text)) or bool(CA_LOOSE.search(text))
    has_bonding = bool(BONDING_RE.search(text))
    has_mcap    = bool(MCAP_RE.search(text))
    has_source  = ("Juggernautnn" in text or "pf_ultimate" in text)
    if has_ca and has_bonding and has_mcap and has_source:
        return True
    if has_ca and sum([has_bonding, has_mcap, has_source]) >= 2:
        return True
    return False


def is_update_msg(text):
    return ((bool(UPDATE_RE.search(text)) and bool(FROM_RE.search(text))) or
            bool(BONDED_RE.search(text)))


def get_ca(text):
    m = CA_RE.search(text)
    if m: return m.group(1)
    m = CA_LOOSE.search(text)
    return m.group(1) if m else None


def get_ticker(text):
    m = TICKER_RE.search(text)
    return m.group(1).strip().upper() if m else "?"


def get_mcap(text):
    m = MCAP_RE.search(text)
    if not m: return None
    n = float(m.group(1))
    s = m.group(2).upper()
    return n * 1000 if s == "K" else n * 1_000_000 if s == "M" else n


def get_bonding(text):
    m = BONDING_RE.search(text)
    return float(m.group(1)) if m else None


def parse_update_msg(text):
    result = {"ticker": None, "mult": None, "mcap_now": None, "bonded": False}
    bm = BONDED_RE.search(text)
    if bm:
        result["ticker"] = bm.group(1).strip().upper()
        result["bonded"] = True
        return result
    tm = UPDATE_TICKER_RE.search(text)
    if tm:
        result["ticker"] = tm.group(1).strip().upper()
    else:
        fm = TICKER_RE.search(text)
        if fm: result["ticker"] = fm.group(1).strip().upper()
    ca = get_ca(text)
    if ca: result["ca"] = ca
    um = UPDATE_RE.search(text)
    if um: result["mult"] = float(um.group(1))
    mm = UPDATE_MCAP_RE.search(text)
    if mm:
        n = float(mm.group(1))
        s = mm.group(2).upper()
        result["mcap_now"] = n * 1000 if s == "K" else n * 1_000_000 if s == "M" else n
    return result


def now_utc():
    return datetime.now(timezone.utc)

def fmt_time(dt):
    return dt.strftime("%H:%M:%S")

def fmt_pct(pct):
    c = Fore.GREEN if pct >= 0 else Fore.RED
    return f"{c}{pct:>+.1f}%{Style.RESET_ALL}"


# ─────────────────────────────────────────────
#  GLOBAL STATE
# ─────────────────────────────────────────────
_active_cas: set = set()
_ca_to_trade_id: dict = {}
_ticker_to_trade_id: dict = {}
_live_data: dict = {}

# Cache bonding curve addresses (PDA derivation is CPU-intensive)
_curve_addr_cache: dict = {}    # mint_str -> curve_address_str

# PumpSwap caches (pool addresses & token accounts are permanent once discovered)
_pumpswap_pool_cache: dict = {}          # mint_str -> pool_address_str
_pool_token_accounts_cache: dict = {}    # mint_str -> {"base_acct": str, "quote_acct": str}
_last_price_cache: dict = {}             # mint_str -> {"price_sol": float, "source": str, "time": float}

# SOL/USD price cache (refreshed every 60s)
_sol_usd_price: dict = {"price": None, "time": 0}
SOL_USD_REFRESH_SEC = 60


async def get_sol_usd_price(session) -> float | None:
    """Fetch current SOL/USD price. Cached for 60s. Binance → CoinGecko fallback."""
    now = time.time()
    if _sol_usd_price["price"] and (now - _sol_usd_price["time"]) < SOL_USD_REFRESH_SEC:
        return _sol_usd_price["price"]
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = float(data["price"])
                _sol_usd_price["price"] = price
                _sol_usd_price["time"] = now
                return price
    except Exception:
        pass
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = data["solana"]["usd"]
                _sol_usd_price["price"] = price
                _sol_usd_price["time"] = now
                return price
    except Exception:
        pass
    return _sol_usd_price["price"]  # return stale if available


def format_mcap_usd(mcap_sol: float, sol_usd: float | None) -> str:
    """Format MCap in USD like '$31.1K' or '$1.2M'."""
    if not sol_usd or not mcap_sol:
        return "—"
    mcap_usd = mcap_sol * sol_usd
    if mcap_usd >= 1_000_000:
        return f"${mcap_usd/1_000_000:.1f}M"
    elif mcap_usd >= 1_000:
        return f"${mcap_usd/1_000:.1f}K"
    else:
        return f"${mcap_usd:.0f}"


# ─────────────────────────────────────────────
#  PRICE — Direct On-Chain Bonding Curve Reads
# ─────────────────────────────────────────────
def get_bonding_curve_address(mint_str: str) -> str:
    """Derive the bonding curve PDA for a pump.fun token."""
    if mint_str in _curve_addr_cache:
        return _curve_addr_cache[mint_str]
    mint = Pubkey.from_string(mint_str)
    curve_addr, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)],
        PUMP_PROGRAM_ID
    )
    addr = str(curve_addr)
    _curve_addr_cache[mint_str] = addr
    return addr


def parse_bonding_curve_data(data: bytes) -> dict | None:
    """
    Parse the bonding curve account data.
    Layout (after 8-byte discriminator):
      offset  8: virtualTokenReserves  (u64)
      offset 16: virtualSolReserves    (u64)
      offset 24: realTokenReserves     (u64)
      offset 32: realSolReserves       (u64)
      offset 40: tokenTotalSupply      (u64)
      offset 48: complete              (bool)
    """
    if not data or len(data) < 49:
        return None
    try:
        virtual_token = struct.unpack_from('<Q', data, 8)[0]
        virtual_sol   = struct.unpack_from('<Q', data, 16)[0]
        real_token    = struct.unpack_from('<Q', data, 24)[0]
        real_sol      = struct.unpack_from('<Q', data, 32)[0]
        token_supply  = struct.unpack_from('<Q', data, 40)[0]
        complete      = bool(data[48])
        if virtual_token == 0:
            return None
        # price_ratio: raw ratio used for multiplier calculation
        # Both entry and current use this same formula → accurate multiplier
        price_ratio = virtual_sol / virtual_token
        # price_sol: human-readable price per token in SOL
        # pump.fun tokens have 6 decimals, SOL has 9
        price_sol = (virtual_sol / 1e9) / (virtual_token / 1e6)
        # mcap_sol: approximate market cap in SOL (1B supply)
        mcap_sol = price_sol * 1e9
        # bonding progress
        if real_token >= INITIAL_REAL_TOKEN_RESERVES:
            bonding_pct = 0.0
        else:
            bonding_pct = (1 - (real_token / INITIAL_REAL_TOKEN_RESERVES)) * 100
        return {
            "price_ratio":  price_ratio,
            "price_sol":    price_sol,
            "mcap_sol":     mcap_sol,
            "bonding_pct":  bonding_pct,
            "complete":     complete,
            "virtual_sol":  virtual_sol,
            "virtual_token": virtual_token,
            "real_sol":     real_sol,
            "real_token":   real_token,
        }
    except Exception:
        return None


async def get_price_rpc(session, mint_address: str) -> dict | None:
    """
    Read price directly from the on-chain bonding curve via Helius RPC.
    Returns parsed bonding curve data or None on failure.
    This is the GROUND TRUTH — same data Photon/Axiom/Trojan read.
    """
    if not HELIUS_RPC_URL:
        return None
    curve_address = get_bonding_curve_address(mint_address)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            curve_address,
            {"encoding": "base64", "commitment": "confirmed"}
        ]
    }
    try:
        async with session.post(
            HELIUS_RPC_URL, json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            result = await r.json()
            account = result.get("result", {}).get("value")
            if not account or not account.get("data"):
                return None
            raw = base64.b64decode(account["data"][0])
            return parse_bonding_curve_data(raw)
    except Exception:
        return None


async def get_price_jupiter(session, mint_address: str) -> float | None:
    """
    Fallback for graduated tokens — Jupiter Price API.
    Returns price in SOL or None.
    """
    try:
        url = f"https://api.jup.ag/price/v2?ids={mint_address}&vsToken=So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            token_data = data.get("data", {}).get(mint_address)
            if token_data and token_data.get("price"):
                return float(token_data["price"])
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
#  PRICE — PumpSwap AMM Pool Reads (Post-Graduation)
# ─────────────────────────────────────────────
async def _rpc_call(session, method: str, params: list, timeout_sec: int = 10):
    """Generic Helius RPC call helper. Returns result or None."""
    if not HELIUS_RPC_URL:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(
            HELIUS_RPC_URL, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_sec)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data.get("result")
    except Exception:
        return None


async def _discover_pool_via_largest_accounts(session, mint_address: str) -> str | None:
    """
    Strategy A: Find PumpSwap pool by looking at who holds the most tokens.
    After graduation, the PumpSwap pool's base token account holds the largest balance.
    We read that account's owner, then check if the owner is a PumpSwap pool.
    """
    result = await _rpc_call(session, "getTokenLargestAccounts", [mint_address])
    if not result or not result.get("value"):
        return None

    accounts = result["value"][:5]  # check top 5 holders

    # Batch-read all token accounts in one call
    acct_addrs = [a["address"] for a in accounts]
    multi_result = await _rpc_call(session, "getMultipleAccounts", [
        acct_addrs, {"encoding": "base64", "commitment": "confirmed"}
    ])
    if not multi_result or not multi_result.get("value"):
        return None

    # Extract owner pubkeys from SPL token account data (owner at offset 32, 32 bytes)
    owner_addrs = []
    for acct_data in multi_result["value"]:
        if not acct_data or not acct_data.get("data"):
            continue
        try:
            raw = base64.b64decode(acct_data["data"][0])
            if len(raw) < 64:
                continue
            owner_bytes = raw[32:64]
            owner_addr = str(Pubkey.from_bytes(owner_bytes))
            owner_addrs.append(owner_addr)
        except Exception:
            continue

    if not owner_addrs:
        return None

    # Batch-read all potential pool owners in one call
    owner_result = await _rpc_call(session, "getMultipleAccounts", [
        owner_addrs, {"encoding": "base64", "commitment": "confirmed"}
    ])
    if not owner_result or not owner_result.get("value"):
        return None

    for i, owner_data in enumerate(owner_result["value"]):
        if not owner_data:
            continue
        # Check if this account is owned by the PumpSwap program
        if owner_data.get("owner") == PUMPSWAP_PROGRAM_STR:
            pool_addr = owner_addrs[i]
            return pool_addr

    return None


async def _discover_pool_via_program_accounts(session, mint_address: str) -> str | None:
    """
    Strategy B: Use getProgramAccounts with memcmp filter on base_mint.
    More expensive RPC call but guaranteed to find the pool.
    Tries multiple possible offsets for base_mint in the pool layout.
    """
    mint_bytes_b58 = mint_address  # base58 string for memcmp

    # Try known layout offsets for base_mint field
    # Variant A: disc(8) + bump(1) + index(2) + padding(2) + creator(32) = offset 45
    # Variant B: disc(8) + bump(1) + index(2) + creator(32) = offset 43
    # Variant C: disc(8) + bump(1) + index(2) + padding(5) + creator(32) = offset 48
    for offset in [43, 45, 48]:
        result = await _rpc_call(session, "getProgramAccounts", [
            PUMPSWAP_PROGRAM_STR,
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "filters": [
                    {"memcmp": {"offset": offset, "bytes": mint_bytes_b58}}
                ]
            }
        ], timeout_sec=15)

        if result and len(result) > 0:
            # Return the first matching pool
            pool_addr = result[0].get("pubkey")
            if pool_addr:
                return pool_addr

    return None


async def discover_pumpswap_pool(session, mint_address: str) -> str | None:
    """
    Find the PumpSwap AMM pool address for a graduated token.
    Tries cheap strategy first, then expensive fallback. Caches permanently.
    """
    if mint_address in _pumpswap_pool_cache:
        return _pumpswap_pool_cache[mint_address]

    # Strategy A: largest token account holders (3 RPC calls)
    pool = await _discover_pool_via_largest_accounts(session, mint_address)

    # Strategy B: getProgramAccounts (expensive, guaranteed)
    if not pool:
        pool = await _discover_pool_via_program_accounts(session, mint_address)

    if pool:
        _pumpswap_pool_cache[mint_address] = pool
    return pool


def _find_wsol_offset(raw: bytes) -> int | None:
    """Find the byte offset of WSOL mint in pool account data."""
    idx = raw.find(WSOL_MINT_BYTES)
    if idx >= 0:
        return idx
    return None


async def _read_pool_token_accounts(session, mint_address: str, pool_address: str) -> dict | None:
    """
    Read PumpSwap pool account to extract base and quote token account addresses.
    Auto-detects layout by finding WSOL mint position.
    Results cached permanently per mint.
    """
    if mint_address in _pool_token_accounts_cache:
        return _pool_token_accounts_cache[mint_address]

    result = await _rpc_call(session, "getAccountInfo", [
        pool_address, {"encoding": "base64", "commitment": "confirmed"}
    ])
    if not result or not result.get("value"):
        return None

    acct = result["value"]
    if not acct.get("data"):
        return None

    try:
        raw = base64.b64decode(acct["data"][0])
    except Exception:
        return None

    if len(raw) < 200:
        return None

    # Find WSOL mint position → that's quote_mint
    wsol_offset = _find_wsol_offset(raw)
    if wsol_offset is None:
        return None

    # Layout relative to quote_mint:
    # base_mint is 32 bytes before quote_mint
    # lp_mint is 32 bytes after quote_mint
    # pool_base_token_account is 64 bytes after quote_mint
    # pool_quote_token_account is 96 bytes after quote_mint
    base_mint_offset = wsol_offset - 32
    base_token_acct_offset = wsol_offset + 32 + 32  # skip lp_mint(32) then base_acct
    quote_token_acct_offset = base_token_acct_offset + 32

    if base_mint_offset < 8 or quote_token_acct_offset + 32 > len(raw):
        return None

    try:
        # Verify base_mint matches our token
        found_base_mint = str(Pubkey.from_bytes(raw[base_mint_offset:base_mint_offset + 32]))
        if found_base_mint != mint_address:
            # WSOL might be base_mint (token is quote) — try reversed layout
            # quote_mint(our token) would be 32 bytes AFTER wsol_offset
            alt_quote_mint_offset = wsol_offset + 32
            if alt_quote_mint_offset + 32 <= len(raw):
                found_alt = str(Pubkey.from_bytes(raw[alt_quote_mint_offset:alt_quote_mint_offset + 32]))
                if found_alt == mint_address:
                    # Reversed: WSOL is base, token is quote
                    # Adjust offsets: lp_mint is after quote_mint, then token accounts
                    base_token_acct_offset = alt_quote_mint_offset + 32 + 32
                    quote_token_acct_offset = base_token_acct_offset + 32
                    if quote_token_acct_offset + 32 > len(raw):
                        return None
                else:
                    return None
            else:
                return None

        base_acct = str(Pubkey.from_bytes(raw[base_token_acct_offset:base_token_acct_offset + 32]))
        quote_acct = str(Pubkey.from_bytes(raw[quote_token_acct_offset:quote_token_acct_offset + 32]))

        info = {"base_acct": base_acct, "quote_acct": quote_acct}
        _pool_token_accounts_cache[mint_address] = info
        return info
    except Exception:
        return None


async def get_price_pumpswap(session, mint_address: str) -> dict | None:
    """
    Read price from PumpSwap AMM pool via Helius RPC.
    First call: discovers pool + reads token account addresses (cached).
    Steady state: 1 RPC call (getMultipleAccounts for reserves).
    """
    pool_addr = await discover_pumpswap_pool(session, mint_address)
    if not pool_addr:
        return None

    accts = await _read_pool_token_accounts(session, mint_address, pool_addr)
    if not accts:
        return None

    # Read both token account balances in a single RPC call
    result = await _rpc_call(session, "getMultipleAccounts", [
        [accts["base_acct"], accts["quote_acct"]],
        {"encoding": "base64", "commitment": "confirmed"}
    ])
    if not result or not result.get("value") or len(result["value"]) < 2:
        return None

    base_data = result["value"][0]
    quote_data = result["value"][1]
    if not base_data or not base_data.get("data") or not quote_data or not quote_data.get("data"):
        return None

    try:
        base_raw = base64.b64decode(base_data["data"][0])
        quote_raw = base64.b64decode(quote_data["data"][0])

        # SPL token account: amount is at offset 64 (u64 LE)
        if len(base_raw) < 72 or len(quote_raw) < 72:
            return None

        base_reserves = struct.unpack_from('<Q', base_raw, 64)[0]
        quote_reserves = struct.unpack_from('<Q', quote_raw, 64)[0]

        if base_reserves == 0:
            return None

        # Same decimal handling as bonding curve:
        # pump.fun tokens = 6 decimals, SOL = 9 decimals
        price_ratio = quote_reserves / base_reserves
        price_sol = (quote_reserves / 1e9) / (base_reserves / 1e6)
        mcap_sol = price_sol * 1e9  # 1B supply

        return {
            "price_ratio": price_ratio,
            "price_sol": price_sol,
            "mcap_sol": mcap_sol,
            "base_reserves": base_reserves,
            "quote_reserves": quote_reserves,
            "source": "PSWAP",
        }
    except Exception:
        return None


# ─────────────────────────────────────────────
#  PRICE — DexScreener Last-Resort Fallback
# ─────────────────────────────────────────────
async def get_price_dexscreener(session, mint_address: str) -> float | None:
    """
    Last-resort fallback. Returns price in SOL or None.
    ONLY used for price_sol — never for mcap or entry baseline.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            pairs = data.get("pairs") or []
            # Filter for SOL-quoted pairs, sort by liquidity
            sol_pairs = [p for p in pairs if p.get("quoteToken", {}).get("symbol") == "SOL"]
            if not sol_pairs:
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not sol_pairs:
                return None
            # Pick highest liquidity pair
            sol_pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            price_native = sol_pairs[0].get("priceNative")
            if price_native:
                return float(price_native)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
#  PRICE — Unified Price Engine
# ─────────────────────────────────────────────
def _update_price_cache(mint_address: str, price_sol: float, source: str):
    """Update rolling price cache for stale-data fallback."""
    _last_price_cache[mint_address] = {
        "price_sol": price_sol,
        "source": source,
        "time": time.time(),
    }


async def get_price(session, mint_address: str, entry_ratio: float,
                    entry_price_sol: float, graduated: bool) -> dict:
    """
    Unified price engine. Single call returns consistent result from best available source.
    Cascade: Bonding Curve RPC → PumpSwap RPC → Jupiter → DexScreener → Stale Cache.

    When graduation is detected (complete=true), immediately falls through to PumpSwap
    in the SAME call — no wasted poll cycles.
    """
    result = {
        "mult": None, "price_sol": None,
        "source": "NONE", "ok": False,
        "graduated": graduated, "detail": "",
    }

    # Guard against division by zero
    if entry_price_sol <= 0:
        result["detail"] = "invalid entry_price_sol"
        return result

    # ── Tier 1: Bonding Curve RPC (pre-graduation) ──
    if not graduated:
        curve = await get_price_rpc(session, mint_address)
        if curve:
            if not curve["complete"]:
                # Still on bonding curve — use ratio for multiplier
                if entry_ratio > 0:
                    mult = curve["price_ratio"] / entry_ratio
                else:
                    mult = curve["price_sol"] / entry_price_sol
                result.update({
                    "mult": mult, "price_sol": curve["price_sol"],
                    "source": "RPC", "ok": True, "graduated": False,
                    "detail": f"bonding {curve['bonding_pct']:.1f}%",
                })
                _update_price_cache(mint_address, curve["price_sol"], "RPC")
                return result
            else:
                # Just graduated — fall through to PumpSwap immediately
                graduated = True
                result["graduated"] = True
                # Pass the last bonding curve mult so monitor_trade can recalibrate
                # entry_price_sol. Bonding curve uses virtual reserves (inflated by ~30 SOL
                # virtual subsidy), but PumpSwap uses only real reserves → ~7% price gap.
                if entry_ratio > 0:
                    result["last_bc_mult"] = curve["price_ratio"] / entry_ratio
                else:
                    result["last_bc_mult"] = curve["price_sol"] / entry_price_sol

    # ── Tier 2: PumpSwap AMM Pool RPC (post-graduation) ──
    pswap = await get_price_pumpswap(session, mint_address)
    if pswap:
        mult = pswap["price_sol"] / entry_price_sol
        result.update({
            "mult": mult, "price_sol": pswap["price_sol"],
            "source": "PSWAP", "ok": True, "graduated": True,
            "detail": f"pool reserves b={pswap['base_reserves']} q={pswap['quote_reserves']}",
        })
        _update_price_cache(mint_address, pswap["price_sol"], "PSWAP")
        return result

    # ── Tier 3: Jupiter Price API ──
    jup = await get_price_jupiter(session, mint_address)
    if jup:
        mult = jup / entry_price_sol
        result.update({
            "mult": mult, "price_sol": jup,
            "source": "JUP", "ok": True, "graduated": True,
            "detail": "jupiter api",
        })
        _update_price_cache(mint_address, jup, "JUP")
        return result

    # ── Tier 4: DexScreener ──
    dex = await get_price_dexscreener(session, mint_address)
    if dex:
        mult = dex / entry_price_sol
        result.update({
            "mult": mult, "price_sol": dex,
            "source": "DEX", "ok": True, "graduated": True,
            "detail": "dexscreener fallback",
        })
        _update_price_cache(mint_address, dex, "DEX")
        return result

    # ── Tier 5: Stale cache (emergency, <2min old) ──
    cached = _last_price_cache.get(mint_address)
    if cached and (time.time() - cached["time"]) < STALE_CACHE_MAX_SEC:
        mult = cached["price_sol"] / entry_price_sol
        result.update({
            "mult": mult, "price_sol": cached["price_sol"],
            "source": "CACHE", "ok": True, "graduated": graduated,
            "detail": f"stale {int(time.time() - cached['time'])}s from {cached['source']}",
        })
        return result

    # ── All sources failed ──
    result["detail"] = "all sources failed"
    return result


# ─────────────────────────────────────────────
#  SHADOW STRATEGY EVALUATOR
# ─────────────────────────────────────────────
def init_shadows(trade: dict):
    trade["shadows"] = {}
    for strat in SHADOW_STRATEGIES:
        trade["shadows"][strat["name"]] = {
            "tp": strat["tp"], "sl": strat["sl"],
            "status": "OPEN", "exit_mult": None, "exit_time": None,
            "pnl": None, "sl_before_tp": None, "sl_touched": False,
        }


def update_shadows(trade: dict, mult: float, current_time: datetime):
    for name, s in trade.get("shadows", {}).items():
        if s["status"] != "OPEN": continue
        tp_mult = 1 + s["tp"]
        sl_mult = 1 - s["sl"]
        if mult <= sl_mult and not s["sl_touched"]:
            s["sl_touched"] = True
        if mult >= tp_mult:
            s.update(status="TP_HIT", exit_mult=round(mult,4),
                     exit_time=current_time.isoformat(),
                     pnl=round(ENTRY_SIZE * s["tp"], 4),
                     sl_before_tp=s["sl_touched"])
        elif mult <= sl_mult:
            s.update(status="SL_HIT", exit_mult=round(mult,4),
                     exit_time=current_time.isoformat(),
                     pnl=round(-ENTRY_SIZE * s["sl"], 4),
                     sl_before_tp=False)


def timeout_shadows(trade: dict, mult: float, current_time: datetime):
    for name, s in trade.get("shadows", {}).items():
        if s["status"] != "OPEN": continue
        pct = (mult - 1) * 100
        s.update(status="TIMEOUT", exit_mult=round(mult,4),
                 exit_time=current_time.isoformat(),
                 pnl=round(ENTRY_SIZE * (pct / 100), 4))


def update_shadows_from_milestone(trade: dict, mult: float, ts: str):
    for name, s in trade.get("shadows", {}).items():
        if s["status"] != "OPEN": continue
        tp_mult = 1 + s["tp"]
        if mult >= tp_mult:
            s.update(status="TP_HIT", exit_mult=round(mult,4),
                     exit_time=ts, pnl=round(ENTRY_SIZE * s["tp"], 4),
                     sl_before_tp=s["sl_touched"])


# ─────────────────────────────────────────────
#  TRADE LOG
# ─────────────────────────────────────────────
def load_log() -> dict:
    if os.path.exists(LOG_FILE):
        try:
            content = open(LOG_FILE).read().strip()
            if not content: return {"trades": [], "summary": {}}
            return json.loads(content)
        except Exception:
            backup = LOG_FILE + ".corrupted"
            shutil.copy(LOG_FILE, backup)
            print(f"{Fore.RED}Log corrupted, backed up to {backup}{Style.RESET_ALL}")
            return {"trades": [], "summary": {}}
    return {"trades": [], "summary": {}}


def save_log(log: dict):
    tmp = LOG_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(log, f, indent=2, default=str)
        os.replace(tmp, LOG_FILE)
    except Exception as e:
        print(f"{Fore.RED}Save error: {e}{Style.RESET_ALL}")


def _trade_count(path: str) -> int:
    """Count trades in a JSON log file. Returns 0 on any error."""
    try:
        data = json.loads(open(path).read() or "{}")
        return len(data.get("trades", []))
    except Exception:
        return 0


async def backup_to_telegram(client, log: dict):
    """DM paper_trades.json to Saved Messages. Data survives host wipes."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        n = len(log.get("trades", []))
        caption = f"{BACKUP_MARKER}\n📦 {n} trades · {now_utc().isoformat()[:19]}Z"
        await client.send_file("me", LOG_FILE, caption=caption, force_document=True)
        print(f"  {Fore.GREEN}📦 Backed up {n} trades to Telegram Saved Messages{Style.RESET_ALL}")
    except Exception as e:
        print(f"  {Fore.RED}Backup failed: {e}{Style.RESET_ALL}")


async def restore_from_telegram(client):
    """On startup, pull latest backup from Saved Messages if it has more trades
    than the local file (handles fresh deploy with empty/no local data)."""
    try:
        local_n = _trade_count(LOG_FILE) if os.path.exists(LOG_FILE) else 0

        # iter_messages is newest-first — first matching backup is the latest
        latest = None
        async for msg in client.iter_messages("me", limit=100):
            if msg.document and BACKUP_MARKER in (msg.message or ""):
                latest = msg
                break

        if not latest:
            print(f"  {Fore.YELLOW}No Telegram backup found (first run?){Style.RESET_ALL}")
            return

        tmp = LOG_FILE + ".restore"
        await latest.download_media(file=tmp)
        backup_n = _trade_count(tmp)

        if backup_n > local_n:
            os.replace(tmp, LOG_FILE)
            print(f"  {Fore.GREEN}♻ Restored {backup_n} trades from Telegram backup "
                  f"(local had {local_n}){Style.RESET_ALL}")
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            print(f"  {Fore.CYAN}Local data ({local_n}) ≥ backup ({backup_n}), "
                  f"keeping local{Style.RESET_ALL}")
    except Exception as e:
        print(f"  {Fore.RED}Restore failed: {e}{Style.RESET_ALL}")


def update_summary(log: dict):
    trades  = log["trades"]
    real    = [t for t in trades if t.get("mode") == "TRADE"]
    closed  = [t for t in real if t.get("status") not in ("OPEN",)]
    wins    = [t for t in closed if t.get("status") == "TP_HIT"]
    losses  = [t for t in closed if t.get("status") in ("SL_HIT","TIMEOUT")]
    sl_btp  = [t for t in closed if t.get("sl_before_tp")]
    pnl     = sum(t.get("pnl") or 0 for t in closed)
    open_c  = len([t for t in real if t.get("status") == "OPEN"])
    skipped = len([t for t in trades if t.get("mode") == "SKIPPED"])
    ms_cnt  = sum(len(t.get("milestones",[])) for t in real)
    peaked  = len([t for t in real if t.get("peak_mult_channel")])
    bonded  = sum(1 for t in real for m in t.get("milestones",[]) if m.get("bonded"))

    shadow_summary = {}
    for strat in SHADOW_STRATEGIES:
        sn = strat["name"]
        sc = [t.get("shadows",{}).get(sn,{}) for t in real
              if t.get("shadows",{}).get(sn,{}).get("status","OPEN") != "OPEN"]
        sw = [s for s in sc if s.get("status") == "TP_HIT"]
        sp = sum(s.get("pnl") or 0 for s in sc)
        shadow_summary[sn] = {
            "closed": len(sc), "wins": len(sw),
            "win_rate": f"{len(sw)/max(len(sc),1)*100:.1f}%",
            "pnl": round(sp,4), "avg_pnl": round(sp/max(len(sc),1),4),
            "sl_before_tp": sum(1 for s in sc if s.get("sl_before_tp")),
        }

    log["summary"] = {
        "total_calls": len(real), "open": open_c, "closed": len(closed),
        "skipped": skipped, "wins": len(wins), "losses": len(losses),
        "win_rate": f"{len(wins)/max(len(closed),1)*100:.1f}%",
        "sl_before_tp": len(sl_btp),
        "total_pnl_sol": round(pnl,4),
        "avg_pnl": round(pnl/max(len(closed),1),4),
        "milestones_total": ms_cnt, "tokens_with_peak": peaked,
        "tokens_bonded": bonded,
        "shadow_strategies": shadow_summary,
        "last_updated": now_utc().isoformat(),
    }


def print_stats(log: dict):
    s   = log.get("summary", {})
    pnl = s.get("total_pnl_sol", 0)
    c   = Fore.GREEN if pnl >= 0 else Fore.RED
    print(f"\n  {'─'*65}")
    print(f"  {Fore.CYAN}TRADES{Style.RESET_ALL} | "
          f"Total:{s.get('total_calls',0)} "
          f"Open:{s.get('open',0)} "
          f"Closed:{s.get('closed',0)} "
          f"Skip:{s.get('skipped',0)}")
    print(f"  {Fore.GREEN}Wins:{s.get('wins',0)}{Style.RESET_ALL} "
          f"{Fore.RED}Losses:{s.get('losses',0)}{Style.RESET_ALL} "
          f"WR:{s.get('win_rate','0%')} "
          f"{Fore.YELLOW}SLbTP:{s.get('sl_before_tp',0)}{Style.RESET_ALL}")
    print(f"  P&L: {c}{pnl:>+.4f} SOL{Style.RESET_ALL}  "
          f"Avg:{s.get('avg_pnl',0):+.4f}  "
          f"{Fore.MAGENTA}MS:{s.get('milestones_total',0)}{Style.RESET_ALL}")
    print(f"  {'─'*65}")


# ─────────────────────────────────────────────
#  TRADE MONITOR — Direct RPC price reads
# ─────────────────────────────────────────────
async def monitor_trade(trade_id, ca, ticker, entry_ratio, entry_price_sol,
                        entry_time, session, log, already_graduated=False, entry_source="RPC"):
    tp_mult  = 1 + TAKE_PROFIT
    sl_mult  = 1 - STOP_LOSS
    max_time = entry_time + timedelta(hours=MAX_HOLD_HRS)

    price_path     = []
    min_mult       = 1.0
    max_mult       = 1.0
    sl_hit_time    = None
    tp_hit_time    = None
    no_data_cnt    = 0
    last_path_save = 0
    graduated      = already_graduated

    trade = next((t for t in log["trades"] if t["id"] == trade_id), None)

    source_labels = {"RPC": "Helius RPC (on-chain)", "PSWAP": "PumpSwap AMM (on-chain)",
                     "JUP": "Jupiter API", "DEX": "DexScreener ⚠ (stale risk)"}
    source_display = source_labels.get(entry_source, entry_source)
    print(f"\n  {Fore.CYAN}[{fmt_time(entry_time)}] ENTERED ${ticker}{Style.RESET_ALL}")
    print(f"  Entry price: {entry_price_sol:.10f} SOL/token")
    print(f"  TP: ×{tp_mult:.2f} (+{int(TAKE_PROFIT*100)}%)  "
          f"SL: ×{sl_mult:.2f} (-{int(STOP_LOSS*100)}%)")
    print(f"  {Fore.GREEN}Price source: {source_display}{Style.RESET_ALL}")

    try:
        while True:
            await asyncio.sleep(PRICE_POLL_SEC)
            current_time = now_utc()
            epoch_now    = current_time.timestamp()

            # ── Timeout ──
            if current_time > max_time:
                price = await get_price(session, ca, entry_ratio, entry_price_sol, graduated)
                mult = price["mult"] if price["ok"] else 1.0
                pct = (mult - 1) * 100
                if trade:
                    timeout_shadows(trade, mult, current_time)
                    trade.update({
                        "status": "TIMEOUT", "exit_time": current_time.isoformat(),
                        "exit_mult": round(mult,4), "exit_reason": "24h timeout",
                        "pnl": round(ENTRY_SIZE*(pct/100),4),
                        "pct_change": round(pct,2),
                        "min_drop_pct": round((min_mult-1)*100,2),
                        "max_gain_pct": round((max_mult-1)*100,2),
                        "sl_before_tp": sl_hit_time is not None and tp_hit_time is None,
                        "price_path": price_path,
                    })
                    save_log(log)
                print(f"  {Fore.YELLOW}TIMEOUT ${ticker}  {pct:+.1f}%{Style.RESET_ALL}")
                return

            # ── Get price via unified engine ──
            price = await get_price(session, ca, entry_ratio, entry_price_sol, graduated)
            mult   = price["mult"]
            source = price["source"]

            # Graduation transition (logged once)
            if price["graduated"] and not graduated:
                graduated = True

                # ── Recalibrate entry_price_sol ──
                # Bonding curve virtual reserves include ~30 SOL subsidy, making the
                # virtual price ~7% higher than PumpSwap's real-reserve price.
                # Without recalibration, mult drops ~7% instantly at graduation = fake SL.
                # Fix: set new entry_price_sol so mult is continuous across the transition.
                last_bc_mult = price.get("last_bc_mult")
                if last_bc_mult and price.get("price_sol") and last_bc_mult > 0:
                    old_entry = entry_price_sol
                    entry_price_sol = price["price_sol"] / last_bc_mult
                    print(f"  {Fore.CYAN}🔧 Recalibrated entry: {old_entry:.10f} → {entry_price_sol:.10f} SOL "
                          f"(virtual→real reserve adjustment){Style.RESET_ALL}")
                    if trade:
                        trade["entry_price_sol_calibrated"] = entry_price_sol
                    # Recompute mult with calibrated entry
                    mult = price["price_sol"] / entry_price_sol

                if trade:
                    trade["graduated"] = True
                    trade["graduated_at"] = current_time.isoformat()
                print(f"  {Fore.MAGENTA}🎓 ${ticker} GRADUATED — now using {source}{Style.RESET_ALL}")

            if not price["ok"]:
                no_data_cnt += 1
                held = (current_time - entry_time).total_seconds() / 60
                _live_data[trade_id] = {
                    "ticker": ticker, "ca": ca[:12],
                    "mult": None, "pct": None, "source": "NONE",
                    "rpc_ok": False, "held_min": round(held,1),
                    "entry_time": entry_time.isoformat(),
                    "graduated": graduated,
                    "shadows": {n: s["status"] for n,s in trade.get("shadows",{}).items()} if trade else {},
                }
                if no_data_cnt % 12 == 1:
                    print(f"  [{fmt_time(current_time)}] ${ticker} — no price (×{no_data_cnt}) [{price['detail']}]")
                continue

            no_data_cnt = 0
            pct      = (mult - 1) * 100
            min_mult = min(min_mult, mult)
            max_mult = max(max_mult, mult)

            if mult <= sl_mult and sl_hit_time is None:
                sl_hit_time = current_time
            if mult >= tp_mult and tp_hit_time is None:
                tp_hit_time = current_time

            # ── Update shadows ──
            if trade:
                update_shadows(trade, mult, current_time)

            # ── Push to _live_data (powers /live page) ──
            held = (current_time - entry_time).total_seconds() / 60
            _live_data[trade_id] = {
                "ticker": ticker, "ca": ca[:12],
                "mult": round(mult, 4), "pct": round(pct, 2),
                "source": source, "rpc_ok": True,
                "held_min": round(held, 1),
                "entry_time": entry_time.isoformat(),
                "min_drop": round((min_mult-1)*100, 2),
                "max_gain": round((max_mult-1)*100, 2),
                "graduated": graduated,
                "shadows": {n: s["status"] for n,s in trade.get("shadows",{}).items()} if trade else {},
            }

            # ── Log price_path every PATH_SAVE_SEC ──
            if epoch_now - last_path_save >= PATH_SAVE_SEC:
                price_path.append({
                    "time": current_time.isoformat(),
                    "mult": round(mult,4), "pct": round(pct,2),
                    "source": source,
                })
                last_path_save = epoch_now

            # Console
            near_tp = pct >= TAKE_PROFIT * 100 * 0.8
            near_sl = pct <= -STOP_LOSS  * 100 * 0.8
            grad_tag = " 🎓" if graduated else ""
            print(f"  [{fmt_time(current_time)}] ${ticker:<10} {fmt_pct(pct)} [{source}]{grad_tag}"
                  f"{'  → NEAR TP' if near_tp else ''}"
                  f"{'  → NEAR SL' if near_sl else ''}")

            # ── TP HIT ──
            if mult >= tp_mult:
                sl_first = sl_hit_time is not None and (
                    tp_hit_time is None or sl_hit_time < tp_hit_time)
                mins = (current_time - entry_time).total_seconds() / 60
                pnl  = ENTRY_SIZE * TAKE_PROFIT
                if trade:
                    trade.update({
                        "status": "TP_HIT", "exit_time": current_time.isoformat(),
                        "exit_mult": round(mult,4),
                        "exit_reason": f"TP hit in {mins:.0f}m",
                        "pnl": round(pnl,4),
                        "pct_change": TAKE_PROFIT * 100,
                        "minutes_to_tp": round(mins,1),
                        "min_drop_pct": round((min_mult-1)*100,2),
                        "max_gain_pct": round((max_mult-1)*100,2),
                        "sl_before_tp": sl_first, "price_path": price_path,
                    })
                    save_log(log)
                tag = f" {Fore.YELLOW}(SL first){Style.RESET_ALL}" if sl_first else ""
                print(f"\n  {Fore.GREEN}TP ✅ ${ticker} +{pnl:.2f} SOL ({mins:.0f}m){Style.RESET_ALL}{tag}")
                return

            # ── SL HIT ──
            if mult <= sl_mult:
                mins = (current_time - entry_time).total_seconds() / 60
                pnl  = -ENTRY_SIZE * STOP_LOSS
                if trade:
                    trade.update({
                        "status": "SL_HIT", "exit_time": current_time.isoformat(),
                        "exit_mult": round(mult,4),
                        "exit_reason": f"SL hit in {mins:.0f}m",
                        "pnl": round(pnl,4),
                        "pct_change": -STOP_LOSS * 100,
                        "minutes_to_sl": round(mins,1),
                        "min_drop_pct": round((min_mult-1)*100,2),
                        "max_gain_pct": round((max_mult-1)*100,2),
                        "sl_before_tp": False, "price_path": price_path,
                    })
                    save_log(log)
                print(f"\n  {Fore.RED}SL ❌ ${ticker} {pnl:.2f} SOL ({mins:.0f}m){Style.RESET_ALL}")
                return

            # Periodic JSON save
            if len(price_path) % 10 == 0 and trade and len(price_path) > 0:
                trade["price_path"]   = price_path
                trade["max_gain_pct"] = round((max_mult-1)*100,2)
                trade["min_drop_pct"] = round((min_mult-1)*100,2)
                update_summary(log)
                save_log(log)

    finally:
        _active_cas.discard(ca)
        _live_data.pop(trade_id, None)


# ─────────────────────────────────────────────
#  CHANNEL MILESTONE MATCHING
# ─────────────────────────────────────────────
def match_update_to_trade(log, parsed):
    trades = log.get("trades", [])
    if not trades: return None
    ca = parsed.get("ca")
    if ca:
        tid = _ca_to_trade_id.get(ca)
        if tid: return next((t for t in trades if t["id"] == tid), None)
    ticker = parsed.get("ticker")
    if ticker:
        tid = _ticker_to_trade_id.get(ticker)
        if tid: return next((t for t in trades if t["id"] == tid), None)
        for t in reversed(trades):
            if t.get("ticker","").upper() == ticker: return t
    return None


def attach_milestone(trade, parsed, msg_time):
    if "milestones" not in trade: trade["milestones"] = []
    milestone = {"time": msg_time}
    if parsed.get("mult"): milestone["mult"] = parsed["mult"]
    if parsed.get("mcap_now"): milestone["mcap"] = parsed["mcap_now"]
    if parsed.get("bonded"): milestone["bonded"] = True
    existing = {m.get("mult") for m in trade["milestones"]}
    if parsed.get("mult") and parsed["mult"] in existing: return False
    trade["milestones"].append(milestone)
    if parsed.get("mult"):
        pk = trade.get("peak_mult_channel")
        if pk is None or parsed["mult"] > pk:
            trade["peak_mult_channel"] = parsed["mult"]
            trade["peak_time_channel"] = msg_time
        update_shadows_from_milestone(trade, parsed["mult"], msg_time)
    return True


# ─────────────────────────────────────────────
#  SHARED CSS
# ─────────────────────────────────────────────
CSS = """*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a1a;color:#e0e0e0;font-family:monospace;padding:20px}
a{color:#378ADD;text-decoration:none} a:hover{text-decoration:underline}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.card{background:#242424;border:1px solid #333;border-radius:8px;padding:12px}
.lbl{font-size:10px;color:#666;text-transform:uppercase;margin-bottom:4px}
.val{font-size:20px;font-weight:bold}
.sub{font-size:11px;color:#555;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#555;font-size:11px;padding:5px 8px;text-align:left;border-bottom:1px solid #2a2a2a}
td{padding:5px 8px;border-bottom:1px solid #222}
h2{font-size:12px;color:#888;text-transform:uppercase;margin:16px 0 8px;border-bottom:1px solid #333;padding-bottom:4px}
.tag{display:inline-block;font-size:9px;padding:2px 6px;border-radius:4px;background:#2a2a2a;color:#888;margin-left:8px}
nav{margin-bottom:16px;font-size:12px;color:#555}
nav a{margin-right:16px}
.g{color:#1D9E75} .r{color:#E24B4A} .y{color:#BA7517} .m{color:#A855F7}
.rpc-on{color:#1D9E75;font-weight:bold} .rpc-off{color:#E24B4A;font-weight:bold}
.src-cache{color:#BA7517;font-weight:bold}"""

NAV = '<nav><a href="/">Overview</a> <a href="/live">Live Monitor</a> </nav>'


# ─────────────────────────────────────────────
#  WEB DASHBOARD
# ─────────────────────────────────────────────
async def start_dashboard():
    from aiohttp import web

    async def page_overview(request):
        log = load_log()
        trades = log.get("trades", [])
        s   = log.get("summary", {})
        pnl = s.get("total_pnl_sol", 0)
        pc  = "#1D9E75" if pnl >= 0 else "#E24B4A"

        real   = [t for t in trades if t.get("mode") == "TRADE"]
        recent = sorted(real, key=lambda x: x.get("called_at",""), reverse=True)[:30]
        rows   = ""
        for t in recent:
            st  = t.get("status","?")
            col = "#1D9E75" if st=="TP_HIT" else "#E24B4A" if st in ("SL_HIT","TIMEOUT") else "#BA7517"
            ico = "✅" if st=="TP_HIT" else "❌" if st in ("SL_HIT","TIMEOUT") else "🔄"
            p   = t.get("pnl") or 0
            mg  = t.get("max_gain_pct")
            md  = t.get("min_drop_pct")
            pk  = t.get("peak_mult_channel")
            ms  = len(t.get("milestones",[]))
            tid = t.get("id","")
            rows += (f"<tr><td>{ico} <a href='/trade/{tid}'>${t.get('ticker','?')}</a></td>"
                     f"<td style='color:{col}'>{st}</td>"
                     f"<td>{t.get('called_at','')[:16].replace('T',' ')}</td>"
                     f"<td style='color:{col}'>{p:+.2f}</td>"
                     f"<td>{f'+{mg:.1f}%' if mg else '-'}</td>"
                     f"<td>{f'{md:.1f}%' if md else '-'}</td>"
                     f"<td class='g'>{f'{pk:.1f}x' if pk else '-'}</td>"
                     f"<td style='color:#555'>{ms}</td></tr>")

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15"><title>Paper Bot v5</title>
<style>{CSS}</style></head><body>{NAV}
<h1 style="color:#1D9E75;font-size:16px">Paper Bot v5 — On-Chain Engine
<span class="tag">{int(TAKE_PROFIT*100)}/{int(STOP_LOSS*100)} · Helius RPC + PumpSwap</span></h1>
<div style="color:#444;font-size:11px;margin:4px 0 16px">
HB:{log.get('last_heartbeat','?')[:19]} · Mon:{log.get('active_monitors',0)} ·
MS:{s.get('milestones_total',0)}</div>
<div class="grid">
<div class="card"><div class="lbl">P&L</div>
<div class="val" style="color:{pc}">{pnl:+.4f}</div>
<div class="sub">SOL</div></div>
<div class="card"><div class="lbl">Win rate</div>
<div class="val g">{s.get('win_rate','0%')}</div>
<div class="sub">{s.get('wins',0)}W / {s.get('losses',0)}L</div></div>
<div class="card"><div class="lbl">Trades</div>
<div class="val">{s.get('total_calls',0)}</div>
<div class="sub">{s.get('open',0)} open · {s.get('skipped',0)} skip</div></div>
<div class="card"><div class="lbl">Avg P&L</div>
<div class="val" style="color:{pc}">{s.get('avg_pnl',0):+.4f}</div></div>
<div class="card"><div class="lbl">SL before TP</div>
<div class="val y">{s.get('sl_before_tp',0)}</div></div>
</div>
<h2>Recent trades (click token to inspect)</h2>
<table><tr><th>Token</th><th>Status</th><th>Called</th>
<th>P&L</th><th>Max↑</th><th>Max↓</th><th>Peak</th><th>MS</th></tr>
{rows or "<tr><td colspan='8' style='color:#444;padding:20px'>No trades yet</td></tr>"}
</table></body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def page_live(request):
        n = len(_live_data)
        rows = ""
        for tid, d in sorted(_live_data.items(), key=lambda x: x[1].get("held_min",0)):
            rpc_ok = d.get("rpc_ok", False)
            src    = d.get("source", "?")
            if src in ("RPC", "PSWAP"):
                src_cls = "rpc-on"
                src_txt = f"{src} ✓"
            elif src in ("JUP", "DEX"):
                src_cls = "y"
                src_txt = f"{src} ✓"
            elif src == "CACHE":
                src_cls = "src-cache"
                src_txt = "STALE ⚠"
            else:
                src_cls = "rpc-off"
                src_txt = "DOWN ✗"
            grad    = " 🎓" if d.get("graduated") else ""

            if d["mult"] is not None:
                pct = d["pct"]
                pct_col = "g" if pct >= 0 else "r"
                mult_s  = f"×{d['mult']:.4f}"
                pct_s   = f"{pct:+.1f}%"
                tp_dist = (TAKE_PROFIT * 100) - pct
                sl_dist = pct - (-STOP_LOSS * 100)
                bar_pct = max(0, min(100, (pct + STOP_LOSS*100) / ((TAKE_PROFIT+STOP_LOSS)*100) * 100))
                bar = (f"<div style='background:#333;height:6px;border-radius:3px;margin-top:4px'>"
                       f"<div style='background:{'#1D9E75' if pct>=0 else '#E24B4A'};height:6px;"
                       f"border-radius:3px;width:{bar_pct:.0f}%'></div></div>")
            else:
                pct_col = "y"; mult_s = "—"; pct_s = "waiting…"
                bar = ""; tp_dist = sl_dist = 0

            sh = d.get("shadows", {})
            sh_parts = []
            for sname in ["50/15","65/20","80/20","100/25","100/30","150/35"]:
                st = sh.get(sname, "?")
                if st == "OPEN":    sh_parts.append(f"<span style='color:#BA7517'>{sname}:⏳</span>")
                elif st == "TP_HIT": sh_parts.append(f"<span class='g'>{sname}:✅</span>")
                elif st == "SL_HIT": sh_parts.append(f"<span class='r'>{sname}:❌</span>")
                else:               sh_parts.append(f"<span style='color:#555'>{sname}:{st[:3]}</span>")

            rows += f"""<tr>
<td><a href="/trade/{tid}">${d['ticker']}</a>{grad}<br>
<span style="color:#555;font-size:10px">{d['ca']}…</span></td>
<td><span class="{src_cls}">{src_txt}</span></td>
<td class="{pct_col}" style="font-size:16px;font-weight:bold">{mult_s}<br>
<span style="font-size:12px">{pct_s}</span>{bar}</td>
<td style="font-size:10px">TP in: <span class="g">{tp_dist:+.1f}%</span><br>
SL in: <span class="r">{sl_dist:.1f}%</span></td>
<td>{d.get('held_min',0):.0f}m<br>
<span style="color:#555;font-size:10px">↑{d.get('max_gain',0):+.1f}% ↓{d.get('min_drop',0):.1f}%</span></td>
<td style="font-size:10px">{'  '.join(sh_parts)}</td>
</tr>"""

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5"><title>LIVE — {n} open</title>
<style>{CSS}
tr td{{vertical-align:top;padding:8px}}
</style></head><body>{NAV}
<h1 style="color:#BA7517;font-size:16px">🔴 LIVE MONITOR
<span class="tag">{n} open · RPC+PumpSwap · {PRICE_POLL_SEC}s poll</span></h1>
<div style="color:#444;font-size:11px;margin:4px 0 16px">
Active CAs: {len(_active_cas)} · PDA cache: {len(_curve_addr_cache)} · Pool cache: {len(_pumpswap_pool_cache)}</div>
{f'''<table>
<tr><th>Token</th><th>Source</th><th>Price</th><th>Distance</th><th>Held</th><th>Shadows</th></tr>
{rows}
</table>''' if rows else '<div style="color:#555;padding:40px;text-align:center">No open trades. Waiting for next call…</div>'}
</body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def page_trade(request):
        tid   = request.match_info.get("tid", "")
        log   = load_log()
        trade = next((t for t in log["trades"] if t["id"] == tid), None)
        if not trade:
            return web.Response(text=f"Trade {tid} not found", status=404)

        st      = trade.get("status","?")
        is_open = st == "OPEN"
        live    = _live_data.get(tid, {})

        info_rows = ""
        display_fields = [
            "id","mode","ticker","ca","called_at","hour_utc","entered_at",
            "entry_price_sol","entry_mcap_sol","entry_mcap_usd","sol_usd_at_entry","entry_bonding_pct",
            "entry_source","mcap","bonding_curve","graduated",
            "status","exit_time","exit_mult","exit_reason","pnl","pct_change",
            "minutes_to_tp","minutes_to_sl","min_drop_pct","max_gain_pct",
            "sl_before_tp","peak_mult_channel","peak_time_channel",
        ]
        for k in display_fields:
            v = trade.get(k)
            if v is None: continue
            if k == "entry_price_sol":
                display = f"{v:.10f} SOL"
            elif k == "entry_mcap_sol":
                display = f"{v:,.2f} SOL"
            elif k == "entry_mcap_usd":
                if v >= 1_000_000: display = f"${v/1_000_000:,.2f}M"
                elif v >= 1_000: display = f"${v/1_000:,.1f}K"
                else: display = f"${v:,.0f}"
            elif k == "sol_usd_at_entry":
                display = f"${v:,.2f}"
            elif k == "entry_bonding_pct":
                display = f"{v:.1f}%"
            elif k == "mcap":
                if v >= 1_000_000: display = f"${v/1_000_000:,.2f}M"
                elif v >= 1_000: display = f"${v/1_000:,.1f}K"
                else: display = f"${v:,.0f}"
            elif k == "pnl":
                col = "#1D9E75" if v >= 0 else "#E24B4A"
                display = f"<span style='color:{col};font-weight:bold'>{v:+.4f} SOL</span>"
            elif k == "pct_change":
                col = "#1D9E75" if v >= 0 else "#E24B4A"
                display = f"<span style='color:{col}'>{v:+.1f}%</span>"
            elif k in ("min_drop_pct","max_gain_pct"):
                col = "#1D9E75" if v >= 0 else "#E24B4A"
                display = f"<span style='color:{col}'>{v:+.1f}%</span>"
            elif k == "exit_mult": display = f"×{v:.4f}"
            elif k == "peak_mult_channel":
                display = f"<span style='color:#1D9E75;font-weight:bold'>{v:.1f}x</span>"
            elif k == "bonding_curve": display = f"{v:.1f}%"
            elif k in ("minutes_to_tp","minutes_to_sl"): display = f"{v:.1f} min"
            elif k == "graduated": display = "🎓 Yes — price via PumpSwap AMM" if v else "No — bonding curve"
            elif k in ("called_at","entered_at","exit_time","peak_time_channel"):
                display = str(v)[:19].replace("T"," ")
            else: display = str(v)
            labels = {
                "id":"Trade ID", "mode":"Mode", "ticker":"Ticker", "ca":"Contract Address",
                "called_at":"Called At", "hour_utc":"Hour (UTC)", "entered_at":"Entered At",
                "entry_price_sol":"Entry Price (SOL)", "entry_mcap_sol":"Entry MCap (SOL)",
                "entry_mcap_usd":"Entry MCap (USD)", "sol_usd_at_entry":"SOL/USD at Entry",
                "entry_bonding_pct":"Entry Bonding %",
                "entry_source":"Price Source", "mcap":"Market Cap (channel)",
                "bonding_curve":"Bonding Curve (channel)", "graduated":"Graduated",
                "status":"Status", "exit_time":"Exit Time", "exit_mult":"Exit Multiplier",
                "exit_reason":"Exit Reason", "pnl":"P&L", "pct_change":"Price Change",
                "minutes_to_tp":"Time to TP", "minutes_to_sl":"Time to SL",
                "min_drop_pct":"Max Drop", "max_gain_pct":"Max Gain",
                "sl_before_tp":"SL Before TP", "peak_mult_channel":"Peak (Channel)",
                "peak_time_channel":"Peak Time (Channel)",
            }
            label = labels.get(k, k)
            info_rows += f"<tr><td style='color:#666'>{label}</td><td>{display}</td></tr>"

        if is_open and live:
            info_rows += f"<tr><td style='color:#BA7517'>LIVE mult</td><td style='font-size:16px;font-weight:bold'>×{live.get('mult','?')} ({live.get('pct','?')}%)</td></tr>"
            info_rows += f"<tr><td style='color:#BA7517'>LIVE source</td><td>{live.get('source','?')}</td></tr>"
            info_rows += f"<tr><td style='color:#BA7517'>LIVE held</td><td>{live.get('held_min',0):.1f}m</td></tr>"

        shadow_rows = ""
        for sn in ["50/15","65/20","80/20","100/25","100/30","150/35"]:
            s = trade.get("shadows",{}).get(sn,{})
            if not s: continue
            sst = s.get("status","?")
            col = "#1D9E75" if sst=="TP_HIT" else "#E24B4A" if sst in ("SL_HIT","TIMEOUT") else "#BA7517"
            ico = "✅" if sst=="TP_HIT" else "❌" if sst=="SL_HIT" else "⏳" if sst=="OPEN" else "⏰"
            live_tag = " ◀ LIVE" if sn == f"{int(TAKE_PROFIT*100)}/{int(STOP_LOSS*100)}" else ""
            shadow_rows += (f"<tr><td>{sn}{live_tag}</td>"
                           f"<td style='color:{col}'>{ico} {sst}</td>"
                           f"<td>TP:{s.get('tp',0)*100:.0f}% SL:{s.get('sl',0)*100:.0f}%</td>"
                           f"<td>{s.get('exit_mult','—')}</td>"
                           f"<td>{s.get('pnl','—')}</td>"
                           f"<td>{'Yes' if s.get('sl_touched') else 'No'}</td>"
                           f"<td>{'Yes' if s.get('sl_before_tp') else 'No' if s.get('sl_before_tp') is not None else '—'}</td>"
                           f"<td style='font-size:10px'>{s.get('exit_time','—')[:19] if s.get('exit_time') else '—'}</td></tr>")

        ms_rows = ""
        for m in trade.get("milestones", []):
            ms_rows += (f"<tr><td>{m.get('time','')[:19]}</td>"
                       f"<td class='g'>{m.get('mult','—')}x</td>"
                       f"<td>{'${:,.0f}'.format(m['mcap']) if m.get('mcap') else '—'}</td>"
                       f"<td>{'🔗 Yes' if m.get('bonded') else ''}</td></tr>")

        pp = trade.get("price_path", [])[-30:]
        pp_rows = ""
        for p in pp:
            pcol = "g" if p.get("pct",0) >= 0 else "r"
            pp_rows += (f"<tr><td>{p.get('time','')[:19]}</td>"
                       f"<td>×{p.get('mult','?')}</td>"
                       f"<td class='{pcol}'>{p.get('pct',0):+.1f}%</td>"
                       f"<td>{p.get('source','?')}</td></tr>")

        refresh = '<meta http-equiv="refresh" content="5">' if is_open else ''

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
{refresh}<title>{tid} — ${trade.get('ticker','?')}</title>
<style>{CSS}</style></head><body>{NAV}
<h1 style="color:{'#BA7517' if is_open else '#1D9E75' if st=='TP_HIT' else '#E24B4A'};font-size:16px">
{'🔴 ' if is_open else ''}{tid} — ${trade.get('ticker','?')}
<span class="tag">{st}</span></h1>
<h2>Trade Info</h2>
<table>{info_rows}</table>
<h2>Shadow Strategies (all 6)</h2>
<table><tr><th>Strategy</th><th>Status</th><th>Levels</th><th>Exit ×</th>
<th>P&L</th><th>SL touched?</th><th>SL→TP?</th><th>Exit time</th></tr>
{shadow_rows or "<tr><td colspan='8' style='color:#555'>No shadow data</td></tr>"}
</table>
<h2>Milestones from channel ({len(trade.get('milestones',[]))})</h2>
<table><tr><th>Time</th><th>Mult</th><th>MCap</th><th>Bonded</th></tr>
{ms_rows or "<tr><td colspan='4' style='color:#555'>No milestones yet</td></tr>"}
</table>
<h2>Price path (last 30 · {PATH_SAVE_SEC}s interval)</h2>
<table><tr><th>Time</th><th>Mult</th><th>Change</th><th>Source</th></tr>
{pp_rows or "<tr><td colspan='4' style='color:#555'>No price data yet</td></tr>"}
</table></body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def json_page(request):
        txt = open(LOG_FILE).read() if os.path.exists(LOG_FILE) else "{}"
        return web.Response(text=txt, content_type="application/json")

    # Keep the real dashboard private even if its URL is shared publicly.
    # Browser HTTP Basic Authentication (username: viewer). Configure
    # DASHBOARD_PASSWORD in Render's environment; missing password fails closed.
    @web.middleware
    async def require_dashboard_password(request, handler):
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        expected = ("Basic " + base64.b64encode(("viewer:" + password).encode()).decode()) if password else ""
        provided = request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(provided, expected):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="PaperBot Dashboard"'})
        return await handler(request)

    app = web.Application(middlewares=[require_dashboard_password])
    app.router.add_get("/", page_overview)
    app.router.add_get("/live", page_live)
    app.router.add_get("/trade/{tid}", page_trade)
     # /json deliberately disabled on internet-facing service
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT).start()
    print(f"{Fore.GREEN}✅ Dashboard: http://0.0.0.0:{DASHBOARD_PORT}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}  /       — overview + P&L{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}  /live   — real-time open trades{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}  /trade/ — per-trade detail{Style.RESET_ALL}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    strat_list = ", ".join(s["name"] for s in SHADOW_STRATEGIES)
    print(f"\n{Fore.YELLOW}╔══════════════════════════════════════════════╗")
    print(f"║  Paper Trading Bot v5 — On-Chain Engine     ║")
    print(f"║  {int(TAKE_PROFIT*100)}/{int(STOP_LOSS*100)} live + shadows: {strat_list:>20} ║")
    print(f"╚══════════════════════════════════════════════╝{Style.RESET_ALL}\n")

    if not HELIUS_RPC_URL:
        print(f"{Fore.RED}ERROR: HELIUS_RPC_URL not set in .env{Style.RESET_ALL}")
        print(f"  Get your free key at https://helius.dev")
        print(f"  Add to .env: HELIUS_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY")
        return

    api_id   = API_ID   or input("Telegram API ID   : ").strip()
    api_hash = API_HASH or input("Telegram API Hash : ").strip()

    # ── Connect Telegram client FIRST (needed to restore backup) ──
    session_str = os.getenv("SESSION_STRING", "")
    if session_str:
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        print(f"  {Fore.GREEN}Using StringSession (cloud mode){Style.RESET_ALL}")
    else:
        client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
    await client.start()

    # ── Restore trade data from Telegram backup if local is empty/stale ──
    await restore_from_telegram(client)

    log = load_log()
    print(f"  {Fore.CYAN}Loaded {len(log['trades'])} trades from {LOG_FILE}{Style.RESET_ALL}")

    # ── Clean up stuck OPEN trades from previous crashes ──
    stuck = [t for t in log["trades"] if t.get("status") == "OPEN"]
    if stuck:
        now_str = now_utc().isoformat()
        for t in stuck:
            t["status"] = "TIMEOUT"
            t["exit_reason"] = "closed on restart"
            t["exit_time"] = now_str
            t["pnl"] = 0
        update_summary(log)
        save_log(log)
        print(f"  {Fore.YELLOW}Closed {len(stuck)} stuck OPEN trades from previous run{Style.RESET_ALL}")

    for t in log["trades"]:
        if t.get("mode") not in ("TRADE","SKIPPED"): continue
        tid = t["id"]; ca = t.get("ca"); tk = t.get("ticker","").upper()
        if ca: _ca_to_trade_id[ca] = tid
        if tk and tk != "?": _ticker_to_trade_id[tk] = tid
        if t.get("status") == "OPEN": _active_cas.add(ca)

    if _active_cas:
        print(f"  {Fore.YELLOW}{len(_active_cas)} open trades from previous run{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{len(_ca_to_trade_id)} CA lookups, "
          f"{len(_ticker_to_trade_id)} ticker lookups{Style.RESET_ALL}")

    source_entity = await client.get_entity(SOURCE)
    connector    = aiohttp.TCPConnector(limit=50)
    http_session = aiohttp.ClientSession(connector=connector)
    trade_counter = len(log["trades"])
    active_tasks = set()

    # Test RPC connection
    print(f"  {Fore.CYAN}Testing Helius RPC...{Style.RESET_ALL}", end=" ")
    try:
        test_payload = {"jsonrpc":"2.0","id":1,"method":"getHealth"}
        async with http_session.post(HELIUS_RPC_URL, json=test_payload,
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                print(f"{Fore.GREEN}OK ✅{Style.RESET_ALL}")
            else:
                print(f"{Fore.RED}HTTP {r.status} ⚠{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}FAILED: {e}{Style.RESET_ALL}")

    # Test PumpSwap pool reader against a known graduated token
    test_mint = os.getenv("TEST_GRADUATED_MINT", "")
    if test_mint:
        print(f"  {Fore.CYAN}Testing PumpSwap reader...{Style.RESET_ALL}", end=" ")
        try:
            pool = await discover_pumpswap_pool(http_session, test_mint)
            if pool:
                pswap = await get_price_pumpswap(http_session, test_mint)
                if pswap:
                    jup = await get_price_jupiter(http_session, test_mint)
                    jup_s = f" (Jupiter: {jup:.10f})" if jup else ""
                    print(f"{Fore.GREEN}OK ✅ Pool: {pool[:12]}… Price: {pswap['price_sol']:.10f} SOL{jup_s}{Style.RESET_ALL}")
                else:
                    print(f"{Fore.YELLOW}Pool found ({pool[:12]}…) but price read failed ⚠{Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}No pool found for test mint ⚠{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}FAILED: {e}{Style.RESET_ALL}")

    # Warm up SOL/USD price
    sol_usd = await get_sol_usd_price(http_session)
    sol_usd_s = f"${sol_usd:,.2f}" if sol_usd else "unavailable"

    print(f"\n  {Fore.GREEN}✅ {getattr(source_entity,'title',SOURCE)}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}✅ {int(TAKE_PROFIT*100)}%TP / {int(STOP_LOSS*100)}%SL / {ENTRY_SIZE}SOL{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}✅ Price: RPC → PumpSwap → Jupiter → DexScreener{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}✅ SOL/USD: {sol_usd_s}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}✅ Shadows: {strat_list}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}✅ Milestones: ON{Style.RESET_ALL}")
    print(f"\n  {Fore.CYAN}Listening... (Ctrl+C){Style.RESET_ALL}\n")
    print_stats(log)

    @client.on(events.NewMessage(chats=source_entity))
    async def handler(event):
        nonlocal trade_counter
        text = event.message.text or ""
        if not text: return

        msg_time = event.message.date
        if msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)

        # ── MILESTONE ──
        if is_update_msg(text):
            parsed = parse_update_msg(text)
            trade = match_update_to_trade(log, parsed)
            if trade:
                added = attach_milestone(trade, parsed, msg_time.isoformat())
                if added:
                    tk = trade.get("ticker","?")
                    ms = f"{parsed['mult']}x" if parsed.get("mult") else "bonded"
                    st = trade.get("status","?")
                    post = " (post-exit)" if st != "OPEN" else ""
                    print(f"  {Fore.MAGENTA}📡 ${tk} → {ms} [{st}{post}]{Style.RESET_ALL}")
                    save_log(log)
            else:
                tk = parsed.get("ticker") or "?"
                ms = f"{parsed.get('mult','')}x" if parsed.get("mult") else "bonded"
                print(f"  {Fore.YELLOW}📡 UNMATCHED ${tk} → {ms}{Style.RESET_ALL}")
            return

        # ── NEW CALL ──
        if not is_call_card(text): return
        ca = get_ca(text)
        if not ca:
            print(f"  {Fore.YELLOW}Call detected, CA extraction failed{Style.RESET_ALL}")
            return
        if ca in _active_cas:
            print(f"  {Fore.YELLOW}${get_ticker(text)} already active{Style.RESET_ALL}")
            return

        call_time = msg_time
        ticker    = get_ticker(text)
        mcap      = get_mcap(text)
        bonding   = get_bonding(text)

        # ── Fetch entry price from on-chain ──
        print(f"\n  {Fore.CYAN}New: ${ticker} ({ca[:12]}…) — reading on-chain...{Style.RESET_ALL}")

        # 2 quick RPC attempts only — channel posts tokens already minutes/hours old,
        # so if bonding curve is missing it's closed (graduated), not timing lag.
        # Don't waste time retrying — move straight to PumpSwap/Jupiter.
        curve = None
        for attempt in range(2):
            curve = await get_price_rpc(http_session, ca)
            if curve:
                break
            if attempt == 0:
                await asyncio.sleep(2)

        # ── Determine entry price source ──
        already_graduated = False
        entry_source = "RPC"
        entry_ratio = None
        entry_price_sol = None
        entry_mcap_sol = None
        entry_bond_pct = None

        if curve and not curve["complete"]:
            # Normal bonding curve entry (pre-graduation)
            entry_ratio     = curve["price_ratio"]
            entry_price_sol = curve["price_sol"]
            entry_mcap_sol  = curve["mcap_sol"]
            entry_bond_pct  = curve["bonding_pct"]
            entry_source    = "RPC"

        else:
            # Post-graduation entry — two cases:
            # a) curve["complete"] is True (account still open, graduated)
            # b) curve is None (account closed, rent reclaimed post-graduation)
            already_graduated = True
            reason = "curve complete" if (curve and curve["complete"]) else "no curve data"
            print(f"  {Fore.MAGENTA}🎓 ${ticker} graduated ({reason}) — reading PumpSwap...{Style.RESET_ALL}")

            # IMPORTANT: DexScreener is NOT used for post-graduation entry.
            # DexScreener caches the old bonding curve price for minutes after graduation.
            # Using it as entry while monitoring via PSWAP (live AMM price) causes fake
            # multipliers (seen as phantom +100% TPs). Only on-chain sources are safe for entry.
            #
            # Tier 1: PumpSwap pool — try up to 3 times with 3s gaps (pool may be initializing)
            pswap = None
            for attempt in range(3):
                pswap = await get_price_pumpswap(http_session, ca)
                if pswap:
                    break
                if attempt < 2:
                    print(f"  {Fore.YELLOW}PumpSwap attempt {attempt+1}/3 — waiting 3s...{Style.RESET_ALL}")
                    await asyncio.sleep(3)

            if pswap:
                entry_ratio     = pswap["price_ratio"]
                entry_price_sol = pswap["price_sol"]
                entry_mcap_sol  = pswap["mcap_sol"]
                entry_bond_pct  = 100.0
                entry_source    = "PSWAP"
            else:
                # Tier 2: Jupiter — try up to 2 times with 5s gap
                jup = None
                for attempt in range(2):
                    jup = await get_price_jupiter(http_session, ca)
                    if jup:
                        break
                    if attempt < 1:
                        print(f"  {Fore.YELLOW}Jupiter attempt 1/2 — waiting 5s...{Style.RESET_ALL}")
                        await asyncio.sleep(5)

                if jup:
                    entry_price_sol = jup
                    entry_ratio     = jup
                    entry_mcap_sol  = jup * 1e9
                    entry_bond_pct  = 100.0
                    entry_source    = "JUP"
                # No DexScreener — stale prices cause source mismatch with live PSWAP monitoring

        # ── Skip if no entry price available ──
        if not entry_price_sol or entry_price_sol <= 0:
            reason = "no_rpc_data" if not curve else "graduated_no_price"
            print(f"  {Fore.RED}${ticker} SKIPPED ({reason}){Style.RESET_ALL}")
            trade_counter += 1
            sid = f"skip_{trade_counter:04d}"
            log["trades"].append({
                "id": sid, "mode": "SKIPPED", "ticker": ticker, "ca": ca,
                "called_at": call_time.isoformat(), "hour_utc": call_time.hour,
                "reason": reason, "status": "SKIPPED",
                "mcap": mcap, "bonding_curve": bonding, "pnl": 0,
                "milestones": [], "peak_mult_channel": None,
            })
            _ca_to_trade_id[ca] = sid
            if ticker != "?": _ticker_to_trade_id[ticker] = sid
            save_log(log)
            return

        _active_cas.add(ca)
        trade_counter += 1
        tid = f"trade_{trade_counter:04d}"
        entry_time = now_utc()

        record = {
            "id": tid, "mode": "TRADE", "ticker": ticker, "ca": ca,
            "called_at": call_time.isoformat(), "hour_utc": call_time.hour,
            "entered_at": entry_time.isoformat(),
            "entry_price_sol": entry_price_sol,
            "entry_ratio": entry_ratio,
            "entry_mcap_sol": entry_mcap_sol,
            "entry_bonding_pct": entry_bond_pct,
            "entry_size": ENTRY_SIZE, "entry_source": entry_source,
            "mcap": mcap, "bonding_curve": bonding,
            "graduated": already_graduated,
            "status": "OPEN", "pnl": None,
            "min_drop_pct": None, "max_gain_pct": None,
            "sl_before_tp": None, "minutes_to_tp": None, "minutes_to_sl": None,
            "price_path": [], "milestones": [],
            "peak_mult_channel": None, "peak_time_channel": None,
        }
        if already_graduated:
            record["graduated_at"] = entry_time.isoformat()

        # Fetch SOL/USD for MCap display
        sol_usd = await get_sol_usd_price(http_session)
        entry_mcap_usd_s = format_mcap_usd(entry_mcap_sol, sol_usd)
        if sol_usd and entry_mcap_sol:
            record["entry_mcap_usd"] = round(entry_mcap_sol * sol_usd, 2)
            record["sol_usd_at_entry"] = round(sol_usd, 2)
            tp_mcap_usd = format_mcap_usd(entry_mcap_sol * (1 + TAKE_PROFIT), sol_usd)
            sl_mcap_usd = format_mcap_usd(entry_mcap_sol * (1 - STOP_LOSS), sol_usd)
        else:
            tp_mcap_usd = "—"
            sl_mcap_usd = "—"

        init_shadows(record)
        log["trades"].append(record)
        _ca_to_trade_id[ca] = tid
        if ticker != "?": _ticker_to_trade_id[ticker] = tid
        update_summary(log); save_log(log)

        mcap_s = f"${mcap:,.0f}" if mcap else "N/A"
        bc_s = f"BC:{entry_bond_pct:.1f}%" if entry_bond_pct < 100 else "GRADUATED"
        print(f"  {Fore.GREEN}#{trade_counter} ${ticker} Called:{mcap_s} "
              f"Entry:{entry_mcap_usd_s} {bc_s} [{entry_source}]{Style.RESET_ALL}")
        print(f"  TP target: {tp_mcap_usd}  SL target: {sl_mcap_usd}  (SOL=${sol_usd:.0f})" if sol_usd else "")

        task = asyncio.create_task(
            monitor_trade(tid, ca, ticker, entry_ratio, entry_price_sol,
                          entry_time, http_session, log,
                          already_graduated=already_graduated,
                          entry_source=entry_source))
        active_tasks.add(task)

        def on_done(t, tid_=tid):
            active_tasks.discard(t)
            if not t.cancelled() and t.exception():
                print(f"  {Fore.RED}Error [{tid_}]: {t.exception()}{Style.RESET_ALL}")
            update_summary(log); save_log(log); print_stats(log)
        task.add_done_callback(on_done)

    async def heartbeat():
        while True:
            await asyncio.sleep(300)
            log["last_heartbeat"]  = now_utc().isoformat()
            log["active_monitors"] = len(active_tasks)
            update_summary(log); save_log(log)
            print(f"  {Fore.CYAN}[{fmt_time(now_utc())}] ♥ {len(active_tasks)} mon / "
                  f"{len(_active_cas)} CAs / {len(_ca_to_trade_id)} tracked{Style.RESET_ALL}")
            print_stats(log)

    async def backup_loop():
        while True:
            await asyncio.sleep(BACKUP_HOURS * 3600)
            update_summary(log); save_log(log)
            await backup_to_telegram(client, log)

    asyncio.create_task(heartbeat())
    asyncio.create_task(backup_loop())

    # Backup once on startup so a fresh deploy immediately has a restore point
    await backup_to_telegram(client, log)

    try:
        await client.run_until_disconnected()
    except Exception as e:
        print(f"\n{Fore.RED}Connection lost: {e}{Style.RESET_ALL}")
    finally:
        await http_session.close()
        update_summary(log); save_log(log); print_stats(log)
        # Final backup on shutdown — capture latest data before host wipes it
        try:
            await backup_to_telegram(client, log)
        except Exception:
            pass


async def run_forever():
    while True:
        try:
            await main()
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Stopped.{Style.RESET_ALL}"); break
        except Exception as e:
            print(f"{Fore.RED}Crashed: {e}{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}Restarting in 30s…{Style.RESET_ALL}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    async def start_all():
        await start_dashboard()
        await run_forever()
    try:
        asyncio.run(start_all())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Stopped.{Style.RESET_ALL}")