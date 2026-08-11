"""Minimal symbol helpers for HL desk executors (no kline cache I/O)."""

from __future__ import annotations


def norm_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    return s if s.endswith("USDT") else s + "USDT"
