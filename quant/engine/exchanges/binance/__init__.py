"""Binance USDT-M REST for HL desk (no vnpy gateway required)."""

from quant.engine.exchanges.binance import account
from quant.engine.exchanges.binance.gateway import binance_credentials_configured

__all__ = ["account", "binance_credentials_configured"]
