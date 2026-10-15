"""Polymarket public Data API (no leader keys required)."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DATA_API = (os.getenv("POLY_DATA_API") or "https://data-api.polymarket.com").rstrip("/")
GAMMA_API = (os.getenv("POLY_GAMMA_API") or "https://gamma-api.polymarket.com").rstrip("/")
_TIMEOUT = float(os.getenv("POLY_HTTP_TIMEOUT_SEC") or 20)

# asset (token id) -> {title, outcome, condition_id, slug}
_token_meta_cache: dict[str, dict[str, str]] = {}
_token_meta_lock = threading.Lock()


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


def fetch_positions(user: str, *, limit: int = 500) -> list[dict[str, Any]]:
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


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return val if isinstance(val, list) else []
    return []


def resolve_token_meta(asset: str) -> dict[str, str] | None:
    """Map CLOB token id → title / Yes|No / conditionId (Gamma). Cached."""
    token = str(asset or "").strip()
    if not token:
        return None
    with _token_meta_lock:
        hit = _token_meta_cache.get(token)
        if hit is not None:
            return dict(hit) if hit else None
    try:
        url = f"{GAMMA_API}/markets"
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params={"clob_token_ids": token})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("poly gamma meta failed asset=%s…: %s", token[:12], exc)
        with _token_meta_lock:
            _token_meta_cache[token] = {}
        return None
    row = None
    if isinstance(data, list) and data:
        row = data[0] if isinstance(data[0], dict) else None
    elif isinstance(data, dict):
        row = data
    if not row:
        with _token_meta_lock:
            _token_meta_cache[token] = {}
        return None
    token_ids = [str(x) for x in _parse_json_list(row.get("clobTokenIds"))]
    outcomes = [str(x) for x in _parse_json_list(row.get("outcomes"))]
    outcome = ""
    try:
        idx = token_ids.index(token)
        if 0 <= idx < len(outcomes):
            outcome = outcomes[idx]
    except ValueError:
        pass
    meta = {
        "title": str(row.get("question") or row.get("title") or "").strip(),
        "slug": str(row.get("slug") or "").strip(),
        "condition_id": str(row.get("conditionId") or row.get("condition_id") or "").strip(),
        "outcome": outcome.strip(),
    }
    with _token_meta_lock:
        _token_meta_cache[token] = dict(meta)
    return meta


def _meta_missing(outcome: str, title: str, condition_id: str) -> bool:
    oc = (outcome or "").strip()
    return (not oc or oc == "Unknown") or not (title or "").strip() or not (condition_id or "").strip()


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
    title = str(raw.get("title") or raw.get("question") or "").strip()
    slug = str(raw.get("slug") or "").strip()
    # WS orders_matched often omits title/outcome — resolve via Gamma token id.
    if asset and _meta_missing(outcome, title, condition_id):
        meta = resolve_token_meta(asset)
        if meta:
            if not title:
                title = meta.get("title") or ""
            if not slug:
                slug = meta.get("slug") or ""
            if not condition_id:
                condition_id = meta.get("condition_id") or ""
            if (not outcome or outcome == "Unknown") and meta.get("outcome"):
                outcome = meta["outcome"]
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
        "title": title,
        "slug": slug,
        "event_slug": str(raw.get("eventSlug") or raw.get("event_slug") or ""),
        "timestamp": ts,
        "source": source,
        "raw": raw,
    }
