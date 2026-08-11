"""Binance USDT-M 账户 REST（vnpy 实盘）。

Supports optional per-call credentials via ``binance_creds()`` context
(for HL sub-account routing). Default = process env BINANCE_*.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import logging
import math
import os
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import requests

from binance_fapi import FAPI
from quant.common.kline_cache import norm_symbol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinanceCreds:
    api_key: str
    api_secret: str
    server: str = "REAL"

    def ok(self) -> bool:
        return bool(self.api_key and self.api_secret)


_creds_ctx: contextvars.ContextVar[Optional[BinanceCreds]] = contextvars.ContextVar(
    "binance_creds", default=None
)


@contextmanager
def binance_creds(creds: Optional[BinanceCreds]) -> Iterator[None]:
    """Temporarily use sub-account (or other) API keys for REST calls."""
    token = _creds_ctx.set(creds)
    try:
        yield
    finally:
        _creds_ctx.reset(token)


def load_creds_from_env(prefix: str = "") -> BinanceCreds:
    """prefix='' → BINANCE_*; prefix='BINANCE_SUB_O' → BINANCE_SUB_O_API_KEY etc."""
    p = (prefix or "").strip().rstrip("_")
    if p:
        key = (os.getenv(f"{p}_API_KEY") or "").strip()
        sec = (os.getenv(f"{p}_API_SECRET") or "").strip()
        server = (os.getenv(f"{p}_SERVER") or os.getenv("BINANCE_SERVER") or "REAL").strip().upper()
    else:
        key = (os.getenv("BINANCE_API_KEY") or "").strip()
        sec = (os.getenv("BINANCE_API_SECRET") or "").strip()
        server = (os.getenv("BINANCE_SERVER") or "REAL").strip().upper()
    if server not in ("REAL", "TESTNET"):
        server = "REAL"
    return BinanceCreds(api_key=key, api_secret=sec, server=server)


def _active_creds() -> BinanceCreds:
    override = _creds_ctx.get()
    if override is not None:
        return override
    return load_creds_from_env("")


def _fapi_base() -> str:
    """REAL → BINANCE_FAPI_BASE / production; TESTNET → futures testnet host."""
    if _active_creds().server == "TESTNET":
        return (
            os.getenv("BINANCE_FAPI_TESTNET") or "https://testnet.binancefuture.com"
        ).strip().rstrip("/")
    return (os.getenv("BINANCE_FAPI_BASE") or FAPI).strip().rstrip("/")


def _api_key() -> str:
    return _active_creds().api_key


def _api_secret() -> bytes:
    return _active_creds().api_secret.encode()


def _signed_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    key = _api_key()
    secret = _api_secret()
    if not key or not secret:
        raise RuntimeError("BINANCE_API_KEY/SECRET not configured")
    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(sorted(payload.items()))
    sig = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
    url = f"{_fapi_base()}{path}?{query}&signature={sig}"
    resp = requests.get(url, headers={"X-MBX-APIKEY": key}, timeout=15)
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(f"Binance {path} HTTP {resp.status_code}: {body}")
    return resp.json()


def _signed_post(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    key = _api_key()
    secret = _api_secret()
    if not key or not secret:
        raise RuntimeError("BINANCE_API_KEY/SECRET not configured")
    payload = dict(params)
    payload["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(sorted(payload.items()))
    sig = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
    url = f"{_fapi_base()}{path}?{query}&signature={sig}"
    resp = requests.post(url, headers={"X-MBX-APIKEY": key}, timeout=15)
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(f"Binance {path} HTTP {resp.status_code}: {body}")
    return resp.json()


def set_symbol_leverage(symbol: str, leverage: int) -> None:
    sym = norm_symbol(symbol)
    lev = max(1, int(leverage))
    try:
        _signed_post("/fapi/v1/leverage", {"symbol": sym, "leverage": lev})
        logger.info("[vnpy] leverage %s -> %sx", sym, lev)
    except RuntimeError as exc:
        msg = str(exc)
        if "-4028" in msg:
            return
        # Sub-accounts often capped at 5x (-4421); fall back and retry.
        if "-4421" in msg and lev > 5:
            try:
                _signed_post("/fapi/v1/leverage", {"symbol": sym, "leverage": 5})
                logger.info("[vnpy] leverage %s -> 5x (subaccount cap; wanted %sx)", sym, lev)
                return
            except RuntimeError as exc2:
                if "-4028" in str(exc2):
                    return
                raise
        raise


def fetch_usdt_available() -> float:
    """USDT-M wallet availableBalance (cross/isolated wallet free)."""
    rows = _signed_get("/fapi/v2/balance", {})
    if not isinstance(rows, list):
        return 0.0
    for row in rows:
        if str(row.get("asset") or "").upper() != "USDT":
            continue
        try:
            return max(0.0, float(row.get("availableBalance") or 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def fetch_account_equity() -> Dict[str, float]:
    """USDT-M account equity snapshot for live desk display / sizing."""
    data = _signed_get("/fapi/v2/account", {})
    if not isinstance(data, dict):
        return {"equity": 0.0, "wallet": 0.0, "upnl": 0.0, "available": 0.0}
    try:
        wallet = float(data.get("totalWalletBalance") or 0)
    except (TypeError, ValueError):
        wallet = 0.0
    try:
        upnl = float(data.get("totalUnrealizedProfit") or 0)
    except (TypeError, ValueError):
        upnl = 0.0
    try:
        avail = float(data.get("availableBalance") or 0)
    except (TypeError, ValueError):
        avail = 0.0
    return {
        "equity": round(wallet + upnl, 6),
        "wallet": round(wallet, 6),
        "upnl": round(upnl, 6),
        "available": round(avail, 6),
    }


def fetch_position_risk_rows() -> List[Dict[str, Any]]:
    rows = _signed_get("/fapi/v2/positionRisk", {})
    return rows if isinstance(rows, list) else []


def set_symbol_margin_isolated(symbol: str) -> None:
    sym = norm_symbol(symbol)
    try:
        _signed_post("/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"})
    except RuntimeError as exc:
        msg = str(exc)
        if "-4046" in msg or "-4067" in msg or "No need to change margin type" in msg:
            return
        raise


def ensure_one_way_mode() -> None:
    """账户设为单向持仓（vnpy 仅支持 one-way）。"""
    try:
        _signed_post("/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})
        logger.info("[vnpy] position mode -> one-way")
    except RuntimeError as exc:
        msg = str(exc)
        if "-4059" in msg or "No need to change" in msg:
            return
        raise


def fetch_position_amounts(symbols: List[str]) -> Dict[str, float]:
    """symbol -> 净持仓张数（多为正、空为负）。"""
    return {sym: float(snap.get("amount") or 0.0) for sym, snap in fetch_position_snapshots(symbols).items()}


def fetch_position_snapshots(symbols: List[str]) -> Dict[str, Dict[str, float]]:
    """symbol -> {amount, entry}（仅非零持仓）。"""
    want = {norm_symbol(s) for s in symbols}
    rows = _signed_get("/fapi/v2/positionRisk", {})
    out: Dict[str, Dict[str, float]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        sym = norm_symbol(str(row.get("symbol") or ""))
        if sym not in want:
            continue
        amt = float(row.get("positionAmt") or 0.0)
        if abs(amt) < 1e-12:
            continue
        out[sym] = {
            "amount": amt,
            "entry": float(row.get("entryPrice") or 0.0),
        }
    return out


def fetch_signed_position(symbol: str) -> float:
    """One-way signed base size (long +, short -)."""
    sym = norm_symbol(symbol)
    snaps = fetch_position_snapshots([sym])
    return float((snaps.get(sym) or {}).get("amount") or 0.0)


def fetch_all_signed_positions() -> Dict[str, float]:
    """All non-zero one-way signed sizes."""
    rows = _signed_get("/fapi/v2/positionRisk", {})
    out: Dict[str, float] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        sym = norm_symbol(str(row.get("symbol") or ""))
        if not sym:
            continue
        amt = float(row.get("positionAmt") or 0.0)
        if abs(amt) < 1e-12:
            continue
        out[sym] = amt
    return out


_filters_lock = threading.Lock()
_qty_step_cache: Dict[str, float] = {}
_filters_fetched_at = 0.0
_FILTERS_TTL = 3600.0


def _ensure_qty_steps(*, force: bool = False) -> None:
    global _filters_fetched_at
    now = time.time()
    with _filters_lock:
        if not force and _qty_step_cache and (now - _filters_fetched_at) < _FILTERS_TTL:
            return
    try:
        resp = requests.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("[vnpy] binance exchangeInfo failed: %s", exc)
        return
    steps: Dict[str, float] = {}
    for row in data.get("symbols") or []:
        sym = norm_symbol(str(row.get("symbol") or ""))
        if not sym:
            continue
        for f in row.get("filters") or []:
            if str(f.get("filterType") or "") == "LOT_SIZE":
                try:
                    step = float(f.get("stepSize") or 0)
                except (TypeError, ValueError):
                    step = 0.0
                if step > 0:
                    steps[sym] = step
                break
    with _filters_lock:
        _qty_step_cache.clear()
        _qty_step_cache.update(steps)
        _filters_fetched_at = time.time()
        logger.info("[vnpy] binance LOT_SIZE cached: %d", len(steps))


def round_qty(symbol: str, qty: float) -> float:
    """Floor quantity to LOT_SIZE step (never round up)."""
    sym = norm_symbol(symbol)
    _ensure_qty_steps()
    with _filters_lock:
        step = float(_qty_step_cache.get(sym) or 0.0)
    q = abs(float(qty))
    if step <= 0:
        return float(f"{q:.8f}".rstrip("0").rstrip(".") or "0")
    n = math.floor(q / step + 1e-12) * step
    # trim float noise
    prec = max(0, min(8, int(round(-math.log10(step))) if step < 1 else 0))
    return float(f"{n:.{prec}f}")


def get_order_by_client_oid(symbol: str, client_oid: str) -> Optional[Dict[str, Any]]:
    sym = norm_symbol(symbol)
    oid = str(client_oid or "").strip()
    if not oid:
        return None
    try:
        data = _signed_get(
            "/fapi/v1/order",
            {"symbol": sym, "origClientOrderId": oid},
        )
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "-2013" in msg or "order does not exist" in msg or "not exist" in msg:
            return None
        raise
    if isinstance(data, dict) and (data.get("orderId") or data.get("clientOrderId")):
        return data
    return None


def place_market_order(
    *,
    symbol: str,
    side: str,
    size: float,
    client_oid: str,
    reduce_only: bool = False,
    leverage: Optional[int] = None,
) -> Dict[str, Any]:
    """Place USDT-M market order (one-way). side: buy|sell."""
    sym = norm_symbol(symbol)
    side_u = str(side or "").strip().upper()
    if side_u not in ("BUY", "SELL"):
        side_l = str(side or "").strip().lower()
        if side_l == "buy":
            side_u = "BUY"
        elif side_l == "sell":
            side_u = "SELL"
        else:
            raise ValueError(f"invalid side: {side}")
    qty = round_qty(sym, size)
    if qty <= 0:
        raise ValueError(f"size rounds to zero: {size}")
    oid = str(client_oid or "").strip()
    if not oid:
        raise ValueError("client_oid required")
    if len(oid) > 36:
        oid = oid[:36]

    existing = get_order_by_client_oid(sym, oid)
    if existing:
        return {"deduped": True, "order": existing, "clientOid": oid, "symbol": sym}

    if leverage is not None and int(leverage) > 0 and not reduce_only:
        try:
            set_symbol_leverage(sym, int(leverage))
        except Exception as exc:
            logger.warning("[vnpy] binance set leverage skip %s: %s", sym, exc)

    qty_s = f"{qty:.8f}".rstrip("0").rstrip(".")
    params: Dict[str, Any] = {
        "symbol": sym,
        "side": side_u,
        "type": "MARKET",
        "quantity": qty_s,
        "newClientOrderId": oid,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    data = _signed_post("/fapi/v1/order", params)
    logger.info(
        "[vnpy] binance place %s %s qty=%s reduceOnly=%s oid=%s -> %s",
        sym,
        side_u,
        qty_s,
        reduce_only,
        oid,
        data.get("orderId") if isinstance(data, dict) else data,
    )
    return {
        "deduped": False,
        "order": data,
        "clientOid": oid,
        "symbol": sym,
        "size": qty_s,
        "side": side_u.lower(),
    }


def _lane_leverage(cfg) -> int:
    lev = float(getattr(cfg, "live_leverage", 0.0) or 0.0)
    if lev > 0:
        return int(lev)
    return 5


def ensure_pool_leverage(symbols: List[str], cfg) -> None:
    ensure_one_way_mode()
    lev = _lane_leverage(cfg)
    for raw in symbols:
        sym = norm_symbol(raw)
        set_symbol_margin_isolated(sym)
        set_symbol_leverage(sym, lev)
