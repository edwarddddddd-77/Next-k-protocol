"""Minimal Binance Futures public helpers for HL desk symbol map / marks."""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

FAPI = (os.getenv("BINANCE_FAPI_BASE") or "https://fapi.binance.com").strip().rstrip("/")


def fetch_mark_price(symbol: str) -> Optional[float]:
    try:
        resp = requests.get(
            f"{FAPI}/fapi/v1/ticker/price",
            params={"symbol": str(symbol or "").strip().upper()},
            timeout=15,
        )
        resp.raise_for_status()
        data: Any = resp.json()
        px = float(data.get("price"))
        return px if px > 0 else None
    except Exception:
        return None
