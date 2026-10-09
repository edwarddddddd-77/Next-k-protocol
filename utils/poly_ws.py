"""Polymarket public activity WebSocket (filter by proxyWallet client-side)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

logger = logging.getLogger(__name__)

WS_URL = (
    os.getenv("POLY_LIVE_DATA_WS") or "wss://ws-live-data.polymarket.com"
).strip()
# Official docs historically used activity/trades; operators report orders_matched
# is the reliable channel. Allow override.
WS_ACTIVITY_TYPE = (os.getenv("POLY_WS_ACTIVITY_TYPE") or "orders_matched").strip()


OnTrade = Callable[[dict[str, Any]], Awaitable[None] | None]


class PolyActivityWs:
    """Subscribe to platform activity; invoke callback when proxy matches."""

    def __init__(
        self,
        target_wallets: set[str],
        on_trade: OnTrade,
        *,
        url: str | None = None,
    ) -> None:
        self._targets = {a.lower() for a in target_wallets if a}
        self._on_trade = on_trade
        self._url = url or WS_URL
        self._stop = asyncio.Event()
        self.connected = False
        self.messages = 0
        self.matched = 0
        self.last_error: str | None = None

    def set_targets(self, wallets: set[str]) -> None:
        self._targets = {a.lower() for a in wallets if a}

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
                # Clean exit (stop requested) — do not reconnect.
                if self._stop.is_set():
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                self.connected = False
                if self._stop.is_set():
                    break
                logger.warning("poly WS disconnect: %s (retry in %.0fs)", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 1.7)
        self.connected = False

    async def _session(self) -> None:
        async with websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self.connected = True
            self.last_error = None
            sub = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "activity",
                        "type": WS_ACTIVITY_TYPE,
                        "filters": "",
                    }
                ],
            }
            await ws.send(json.dumps(sub))
            logger.info(
                "poly WS subscribed topic=activity type=%s targets=%d",
                WS_ACTIVITY_TYPE,
                len(self._targets),
            )
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # keepalive; connection ping_interval handles protocol pings
                    continue
                await self._handle_message(raw)

    async def _handle_message(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str) or not raw.strip():
            return
        if raw in ("PONG", "pong"):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.messages += 1
        payloads = _extract_trade_payloads(msg)
        for payload in payloads:
            proxy = str(
                payload.get("proxyWallet") or payload.get("proxy_wallet") or ""
            ).strip().lower()
            if not proxy or proxy not in self._targets:
                continue
            self.matched += 1
            result = self._on_trade(payload)
            if inspect.isawaitable(result):
                await result


def _extract_trade_payloads(msg: Any) -> list[dict[str, Any]]:
    if not isinstance(msg, dict):
        return []
    # Shape A: {topic, type, payload: {...}}
    payload = msg.get("payload")
    if isinstance(payload, dict):
        if _looks_like_trade(payload):
            return [payload]
        # Nested trade field
        inner = payload.get("trade") or payload.get("data")
        if isinstance(inner, dict) and _looks_like_trade(inner):
            return [inner]
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict) and _looks_like_trade(x)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict) and _looks_like_trade(x)]
    # Shape B: bare trade object
    if _looks_like_trade(msg):
        return [msg]
    return []


def _looks_like_trade(d: dict[str, Any]) -> bool:
    if not isinstance(d, dict):
        return False
    if d.get("proxyWallet") or d.get("proxy_wallet"):
        return True
    if d.get("transactionHash") or d.get("transaction_hash"):
        return True
    return bool(d.get("asset") and d.get("side") and d.get("size") is not None)
