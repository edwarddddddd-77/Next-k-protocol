"""Wangge proxy must keep Protocol APIs (incl. HL desk) on FastAPI."""

from __future__ import annotations

from routers.wangge_proxy import _keep_on_protocol


def test_keep_binance_and_hl_short():
    assert _keep_on_protocol("/api/binance/health") is True
    assert _keep_on_protocol("/api/hl-short/paper") is True
    assert _keep_on_protocol("/api/hl-short/copy/status") is True
    assert _keep_on_protocol("/api/clawby-quant/status") is True
    assert _keep_on_protocol("/docs") is True


def test_proxy_other_paths():
    assert _keep_on_protocol("/") is False
    assert _keep_on_protocol("/api/overview") is False
    assert _keep_on_protocol("/index.html") is False
