"""env_loader should tolerate quoted KEY=\"value\" lines."""

from __future__ import annotations

import os

from env_loader import load_env_oi


def test_load_env_oi_strips_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.oi"
    env_file.write_text(
        'BINANCE_API_KEY="abc123"\n'
        "BINANCE_API_SECRET='sec456'\n"
        "PLAIN=ok\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("PLAIN", raising=False)

    assert load_env_oi(tmp_path) == env_file
    assert os.environ["BINANCE_API_KEY"] == "abc123"
    assert os.environ["BINANCE_API_SECRET"] == "sec456"
    assert os.environ["PLAIN"] == "ok"
