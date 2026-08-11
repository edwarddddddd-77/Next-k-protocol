"""Slim Binance gateway helpers for HL desk (REST-only; no vnpy)."""

from __future__ import annotations

import os

GATEWAY_NAME = "BINANCE_LINEAR"


def binance_credentials_configured() -> bool:
    return bool(
        (os.getenv("BINANCE_API_KEY") or "").strip()
        and (os.getenv("BINANCE_API_SECRET") or "").strip()
    )


__all__ = ["GATEWAY_NAME", "binance_credentials_configured"]
