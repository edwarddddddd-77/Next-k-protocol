"""HL coin → Binance USDT-M symbol (BTC → BTCUSDT)."""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

from binance_fapi import FAPI
from quant.common.kline_cache import norm_symbol

logger = logging.getLogger(__name__)

_contracts_lock = threading.Lock()
_contracts_cache: set[str] | None = None
_contracts_fetched_at = 0.0
_CONTRACTS_TTL_SEC = 3600.0

_BUILTIN_ALIASES: dict[str, str] = {
    "kFLOKI": "FLOKI",
    "KFLOKI": "FLOKI",
}


def hl_base_ticker(coin: str) -> str | None:
    raw = str(coin or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return None
    if ":" in raw:
        # xyz:TSLA / xyz:MU — skip stock perps on Binance USDT-M by default
        prefix, rest = raw.split(":", 1)
        if prefix.lower() in ("xyz", "flx", "vntl"):
            return None
        raw = rest
    base = raw.strip().upper()
    return _BUILTIN_ALIASES.get(base, base) or None


def verify_symbols_enabled() -> bool:
    raw = (os.getenv("HL_BINANCE_VERIFY_SYMBOLS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def binance_contract_set(*, force: bool = False) -> set[str] | None:
    global _contracts_cache, _contracts_fetched_at
    now = time.time()
    with _contracts_lock:
        if (
            not force
            and _contracts_cache is not None
            and (now - _contracts_fetched_at) < _CONTRACTS_TTL_SEC
        ):
            return _contracts_cache
    try:
        resp = requests.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=20)
        resp.raise_for_status()
        data = resp.json()
        out = {
            norm_symbol(str(r.get("symbol") or ""))
            for r in (data.get("symbols") or [])
            if str(r.get("contractType") or "").upper() == "PERPETUAL"
            and str(r.get("quoteAsset") or "").upper() == "USDT"
            and str(r.get("status") or "").upper() == "TRADING"
        }
        out = {s for s in out if s}
        with _contracts_lock:
            _contracts_cache = out
            _contracts_fetched_at = time.time()
        logger.info("binance USDT-M perpetual cached: %d", len(out))
        return out
    except Exception as exc:
        logger.warning("binance exchangeInfo failed: %s", exc)
        with _contracts_lock:
            return _contracts_cache


def resolve_binance_symbol(coin: str) -> tuple[str | None, str | None]:
    base = hl_base_ticker(coin)
    if not base:
        return None, "empty_or_filtered"
    sym = norm_symbol(f"{base}USDT")
    if not verify_symbols_enabled():
        return sym, None
    known = binance_contract_set()
    if known is None:
        return sym, None  # soft allow if listing fetch failed
    if sym not in known:
        return None, "not_on_binance"
    return sym, None


def map_hl_coin_to_binance(coin: str) -> str | None:
    sym, _ = resolve_binance_symbol(coin)
    return sym
