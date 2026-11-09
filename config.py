"""config.py — operational wiring: mode, credentials, schedule, costs, network, paths.

Trading rules are deliberately NOT here. They live beside the code that enforces them, as the
framework's code map says: the engine constants in strategy.py, the protection shell
(RISK_PER_TRADE, MAX_OPEN_POSITIONS, …) in paper_bot.py.

Values come from environment variables, optionally loaded from a local `.env` (see .env.example).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


_load_dotenv(ROOT / ".env")


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw.strip())
    except ValueError as exc:
        raise SystemExit(f"config: {name}={raw!r} is not a valid {cast.__name__}") from exc


def _csv(name: str) -> set[str]:
    return {s.strip().upper() for s in os.environ.get(name, "").split(",") if s.strip()}


# --- mode ----------------------------------------------------------------------------------------
# paper: live Bitget public data + local simulated fills (no keys). DEFAULT.
# demo:  Bitget demo trading (paptrading header) with demo API keys.
# live:  real orders. Refused unless LIVE_CONFIRM matches AND the edge gate passes (framework:
#        "nothing is enabled for live orders until a signal proves positive").
TRADING_MODE = _env("TRADING_MODE", "paper").lower()
LIVE_CONFIRM = _env("LIVE_CONFIRM", "")
LIVE_CONFIRM_PHRASE = "I-ACCEPT-LIVE-RISK"
EDGE_MIN_TRADES = _env("EDGE_MIN_TRADES", 30, int)
EDGE_MIN_PROFIT_FACTOR = _env("EDGE_MIN_PROFIT_FACTOR", 1.2, float)

ACCOUNT_SIZE = _env("ACCOUNT_SIZE", 600.0, float)
ENGINE = "v5 sweep-reversal · C2/C3 closure"
EXCHANGE = "Bitget USDT-M Futures"

# --- Bitget ----------------------------------------------------------------------------------------
BITGET_BASE_URL = _env("BITGET_BASE_URL", "https://api.bitget.com")
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN = "USDT"
BITGET_API_KEY = _env("BITGET_API_KEY", "")
BITGET_API_SECRET = _env("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = _env("BITGET_API_PASSPHRASE", "")
# Older demo accounts use productType SUSDT-FUTURES with S-prefixed symbols (SBTCSUSDT).
BITGET_DEMO_LEGACY = _env("BITGET_DEMO_LEGACY", False, bool)
LEVERAGE = _env("LEVERAGE", 10, int)
MARGIN_MODE = _env("MARGIN_MODE", "isolated")
# Trailing-stop callback for Bitget moving_plan orders, in percent (framework: 1% behind the peak).
BITGET_TRAIL_RANGE_RATE = _env("BITGET_TRAIL_RANGE_RATE", "1")

# --- costs (Bitget USDT-M VIP0; market orders are taker) -----------------------------------------
TAKER_FEE = _env("TAKER_FEE", 0.0006, float)
PAPER_SLIPPAGE = _env("PAPER_SLIPPAGE", 0.0002, float)

# --- network ---------------------------------------------------------------------------------------
HTTP_TIMEOUT = _env("HTTP_TIMEOUT", 15.0, float)
HTTP_RPS = _env("HTTP_RPS", 12.0, float)            # Bitget public market data allows ~20 req/s/IP
HTTP_WORKERS = _env("HTTP_WORKERS", 8, int)

# --- universe ----------------------------------------------------------------------------------------
MOVERS_N = 20                                        # Routine 1: top 20 movers + 20 losers
MIN_VOLUME_USD = 1_000_000.0                         # Routine 1: 24h volume ≥ $1M
# The framework scans ALL coins for signals; this optional floor (default off) exists only for operators.
UNIVERSE_MIN_VOLUME_USD = _env("UNIVERSE_MIN_VOLUME_USD", 0.0, float)

# TradFi / RWA exclusion (crypto only). Bitget marks RWA contracts with isRwa=YES when present;
# this list is the belt-and-braces fallback. Extend with TRADFI_EXTRA, carve out with TRADFI_ALLOW.
TRADFI_BASES = frozenset({
    # metals, energy, commodities, gold-backed tokens
    "XAU", "XAG", "XPT", "XPD", "XAUT", "PAXG", "XCU", "COPPER", "WTI", "BRENT", "USOIL", "UKOIL",
    "OIL", "NATGAS", "NGAS",
    # indices / ETFs
    "SPY", "QQQ", "IWM", "DIA", "US500", "US30", "NAS100", "USTEC", "SPX500", "DJI", "NDX", "HK50",
    "JP225", "GER40", "UK100", "VIX",
    # FX
    "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNH", "EURUSD", "GBPUSD", "USDJPY",
    # equities (Bitget stock futures + tokenized stocks)
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "GOOG", "META", "NFLX", "AMD", "INTC", "COIN",
    "MSTR", "HOOD", "CRCL", "PLTR", "BABA", "ORCL", "AVGO", "SMCI", "MU", "TSM", "UBER", "SHOP",
    "PYPL", "DIS", "JPM", "GME", "AMC", "IBM", "CSCO", "QCOM", "ADBE", "CRM", "NKE", "WMT", "COST",
    "MCD", "XOM", "CVX", "GS", "BAC", "SNAP", "RBLX", "ABNB", "RIVN", "LCID", "NIO", "BIDU", "PDD",
    "SOFI", "CVNA", "APP", "SNOW", "NET", "CRWD", "PANW", "MRVL", "ASML", "ARM", "SBUX", "LLY",
    "UNH", "JNJ", "PFE", "MRNA", "BRK", "BRKB", "CIRCLE", "GLXY", "FIG", "BMNR", "SBET", "RDDT",
})
# Tokenized-stock tickers (TSLAX, TSLAON): excluded only when the stem is a known TradFi base,
# so crypto tickers that merely end in these letters (TON, APEX) stay in the universe.
TRADFI_SUFFIXES = ("XSTOCK", "ON", "X")
TRADFI_EXTRA = _csv("TRADFI_EXTRA")
TRADFI_ALLOW = _csv("TRADFI_ALLOW")

# --- schedule (MYT) ------------------------------------------------------------------------------
MARKET_DAY_START = "07:00"                           # "market day from 07:00 MYT"
REPORT_TIMES = ("07:00", "19:00")                    # daily report + PDF
SCAN_EVERY_MIN = 240                                 # scan + research routines + signals: every 4h
SCAN_OFFSET = _env("SCAN_OFFSET", "00:02")           # grid anchor: 2 min after each Bitget 4H close
TRADE_EVERY_MIN = 30                                 # trade: every 30 minutes
TRADE_OFFSET_S = 30
MANAGE_EVERY_MIN = 5                                 # manage: ~every 5 minutes
MANAGE_OFFSET_S = 10
SIGNALS_MAX_AGE_H = _env("SIGNALS_MAX_AGE_H", 8.0, float)   # trade ignores a stale signals_live.json

# --- notifications -------------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
GMAIL_USER = _env("GMAIL_USER", "")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD", "")
GMAIL_TO = _env("GMAIL_TO", "")
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _env("SMTP_PORT", 465, int)

# --- paths / UI ----------------------------------------------------------------------------------
BOT_HOME = Path(_env("BOT_HOME", str(ROOT)))
DASHBOARD_HOST = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _env("DASHBOARD_PORT", 8787, int)
