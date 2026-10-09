"""Polymarket public Data API (no leader keys required)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DATA_API = (os.getenv("POLY_DATA_API") or "https://data-api.polymarket.com").rstrip("/")
_TIMEOUT = float(os.getenv("POLY_HTTP_TIMEOUT_SEC") or 20)


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{DATA_API}{path}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()


def fetch_activity_trades(
    user: str,
    *,
    limit: int = 100,
    start: int | None = None,
) -> list[dict[str, Any]]:
    """Recent TRADE activity for a proxy wallet, newest first."""
    params: dict[str, Any] = {
        "user": user,
        "type": "TRADE",
        "limit": max(1, min(int(limit), 500)),
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }
    if start is not None and int(start) > 0:
        params["start"] = int(start)
    data = _get("/activity", params)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def fetch_positions(user: str, *, limit: int = 100) -> list[dict[str, Any]]:
    params = {
        "user": user,
        "sizeThreshold": 0,
        "limit": max(1, min(int(limit), 500)),
        "sortBy": "CURRENT",
        "sortDirection": "DESC",
    }
    data = _get("/positions", params)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def fetch_portfolio_value(user: str) -> float:
    """Leader marked portfolio value in USDC (positions)."""
    try:
        data = _get("/value", {"user": user})
    except Exception as exc:
        logger.warning("poly /value failed user=%s: %s", user[:10], exc)
        return 0.0
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, dict):
            try:
                return max(0.0, float(row.get("value") or 0))
            except (TypeError, ValueError):
                return 0.0
    if isinstance(data, dict):
        inner = data.get("data") if "data" in data else data
        if isinstance(inner, dict):
            try:
                return max(0.0, float(inner.get("value") or 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def normalize_trade(raw: dict[str, Any], *, source: str = "data_api") -> dict[str, Any] | None:
    """Normalize activity / WS trade into a common fill shape."""
    if not isinstance(raw, dict):
        return None
    side = str(raw.get("side") or "").strip().upper()
    if side not in ("BUY", "SELL"):
        return None
    try:
        size = float(raw.get("size") or 0)
        price = float(raw.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or price <= 0:
        return None
    asset = str(raw.get("asset") or "").strip()
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    outcome = str(raw.get("outcome") or "").strip() or "Unknown"
    tx = str(raw.get("transactionHash") or raw.get("transaction_hash") or "").strip()
    ts_raw = raw.get("timestamp") or raw.get("ts") or 0
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        ts = 0
    # WS sometimes delivers ms; Data API uses seconds.
    if ts > 10_000_000_000:
        ts = ts // 1000
    proxy = str(raw.get("proxyWallet") or raw.get("proxy_wallet") or "").strip().lower()
    usdc = raw.get("usdcSize")
    try:
        usdc_size = float(usdc) if usdc is not None else size * price
    except (TypeError, ValueError):
        usdc_size = size * price
    # Prefer tx+asset+side (open-source style). Fall back when hash missing.
    if tx:
        dedupe = "|".join([tx.lower(), asset or condition_id or "noasset", side])
    else:
        dedupe = "|".join(
            [
                "notx",
                asset or condition_id or "noasset",
                side,
                f"{size:.8g}",
                f"{price:.8g}",
                str(ts),
            ]
        )
    return {
        "dedupe_key": dedupe,
        "transaction_hash": tx,
        "proxy_wallet": proxy,
        "side": side,
        "size": size,
        "price": price,
        "usdc_size": usdc_size,
        "asset": asset,
        "condition_id": condition_id,
        "outcome": outcome,
        "outcome_index": raw.get("outcomeIndex", raw.get("outcome_index")),
        "title": str(raw.get("title") or ""),
        "slug": str(raw.get("slug") or ""),
        "event_slug": str(raw.get("eventSlug") or raw.get("event_slug") or ""),
        "timestamp": ts,
        "source": source,
        "raw": raw,
    }
