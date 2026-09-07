"""Box breakout runtime smoke tests (no live A-share full market scan)."""

from __future__ import annotations

from utils import box_breakout_runtime as rt


def test_box_enabled_default():
    assert rt.enabled() is True


def test_ashare_disabled_by_default(monkeypatch):
    monkeypatch.delenv("NEXT_K_BOX_ASHARE_ENABLED", raising=False)
    assert rt.ashare_enabled() is False
    out = rt.start_scan("market")
    assert out.get("status") == "disabled"


def test_box_status_shape():
    st = rt.status()
    assert st.get("ok") is True
    assert "data_dir" in st
    assert "scanning" in st
    assert "ashare_enabled" in st
    assert isinstance(st.get("scan_log"), list)


def test_box_crypto_payload_shape():
    body = rt.get_crypto()
    assert "candidates" in body
    assert isinstance(body["candidates"], list)


def test_box_disabled_env(monkeypatch):
    monkeypatch.setenv("NEXT_K_BOX_ENABLED", "0")
    assert rt.enabled() is False
    monkeypatch.setenv("NEXT_K_BOX_ENABLED", "1")
    assert rt.enabled() is True
