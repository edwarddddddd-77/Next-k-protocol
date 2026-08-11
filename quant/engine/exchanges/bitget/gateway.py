"""Slim Bitget gateway helpers for HL desk (REST-only; no vnpy)."""

from __future__ import annotations

import os

GATEWAY_NAME = "BITGET"


def bitget_credentials_configured() -> bool:
    return bool(
        (os.getenv("BITGET_API_KEY") or "").strip()
        and (os.getenv("BITGET_API_SECRET") or "").strip()
        and (os.getenv("BITGET_PASSPHRASE") or os.getenv("BITGET_API_PASSPHRASE") or "").strip()
    )


def bitget_connect_setting() -> dict:
    server = (os.getenv("BITGET_SERVER") or "REAL").strip().upper()
    if server not in ("REAL", "DEMO"):
        server = "REAL"
    proxy_port = int((os.getenv("BITGET_PROXY_PORT") or "0").strip() or 0)
    return {
        "API Key": (os.getenv("BITGET_API_KEY") or "").strip(),
        "Secret Key": (os.getenv("BITGET_API_SECRET") or "").strip(),
        "Passphrase": (
            os.getenv("BITGET_PASSPHRASE") or os.getenv("BITGET_API_PASSPHRASE") or ""
        ).strip(),
        "Server": server,
        "Proxy Host": (os.getenv("BITGET_PROXY_HOST") or "").strip(),
        "Proxy Port": proxy_port,
        "Kline Stream": "False",
    }


__all__ = [
    "GATEWAY_NAME",
    "bitget_connect_setting",
    "bitget_credentials_configured",
]
