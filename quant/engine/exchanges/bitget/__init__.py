"""Bitget USDT-M REST for HL desk (no vnpy gateway required)."""

from quant.engine.exchanges.bitget import account
from quant.engine.exchanges.bitget.gateway import bitget_credentials_configured

__all__ = ["account", "bitget_credentials_configured"]
