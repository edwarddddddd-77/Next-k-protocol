"""Background supervisor: Polymarket WS + Data API poll → paper copy."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool) -> bool:
    import os

    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class PolyCopySupervisor:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown = False
        self._ws: Any = None
        self._status: dict[str, Any] = {
            "running": False,
            "mode": "paper",
            "started_at": None,
            "wallets": [],
            "last_fill_at": None,
            "fills_seen": 0,
            "fills_copied": 0,
            "poll_cycles": 0,
            "error": None,
            "ws": {},
        }
        self._lock = threading.Lock()

    @property
    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._status)
        try:
            from utils.poly_clob import status as clob_status
            from utils.poly_paper_copy import slim_paper_for_api

            out["paper"] = slim_paper_for_api()
            out["clob"] = clob_status()
            out["mode"] = "live" if clob_status().get("enabled") else "paper"
        except Exception as exc:
            out["paper_error"] = str(exc)
        if self._ws is not None:
            out["ws"] = {
                "connected": bool(getattr(self._ws, "connected", False)),
                "messages": int(getattr(self._ws, "messages", 0) or 0),
                "matched": int(getattr(self._ws, "matched", 0) or 0),
                "last_error": getattr(self._ws, "last_error", None),
            }
        return out

    def should_start(self) -> bool:
        return _env_bool("POLY_COPY_ENABLED", False)

    def start(self) -> None:
        if not self.should_start():
            logger.info("poly copy supervisor disabled (POLY_COPY_ENABLED=0)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._shutdown = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name="poly-copy-supervisor",
            daemon=True,
        )
        self._thread.start()
        logger.info("poly copy supervisor thread started")

    def stop(self) -> None:
        self._shutdown = True
        ws = self._ws
        if ws is not None:
            try:
                ws.request_stop()
            except Exception:
                pass
        loop = self._loop
        if loop and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._async_stop(), loop)
            try:
                fut.result(timeout=10)
            except Exception as exc:
                logger.warning("poly copy stop: %s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        with self._lock:
            self._status["running"] = False

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as exc:
            logger.exception("poly copy supervisor crashed: %s", exc)
            with self._lock:
                self._status["running"] = False
                self._status["error"] = str(exc)

    async def _async_stop(self) -> None:
        self._shutdown = True
        if self._ws is not None:
            self._ws.request_stop()

    async def _wait_for_wallets(self) -> list[dict[str, Any]]:
        """Block until watchlist/env has at least one target (or shutdown)."""
        from utils.poly_config import load_watchlist
        from utils.poly_paper_copy import ensure_bots_from_watchlist

        warned = False
        while not self._shutdown:
            ensure_bots_from_watchlist()
            wallets = load_watchlist()
            if wallets:
                return wallets
            if not warned:
                logger.warning(
                    "poly copy: no target wallet — set poly_watchlist.json address "
                    "or POLY_TARGET_WALLET (retrying)"
                )
                warned = True
                with self._lock:
                    self._status["running"] = False
                    self._status["error"] = "no_target_wallet"
            await self._sleep_interruptible(10)
        return []

    async def _async_main(self) -> None:
        self._loop = asyncio.get_running_loop()
        from utils.poly_config import load_watchlist
        from utils.poly_paper_copy import (
            ensure_bots_from_watchlist,
            refresh_leader_equity,
            set_baseline_if_needed,
        )
        from utils.poly_ws import PolyActivityWs

        wallets = await self._wait_for_wallets()
        if not wallets:
            return

        for w in wallets:
            set_baseline_if_needed(w["id"])
            try:
                await asyncio.to_thread(refresh_leader_equity, w["id"], True)
            except Exception as exc:
                logger.warning("poly leader equity failed %s: %s", w["id"], exc)

        targets = {w["address"] for w in wallets}
        with self._lock:
            self._status.update(
                {
                    "running": True,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "wallets": [
                        {"id": w["id"], "address": w["address"]} for w in wallets
                    ],
                    "error": None,
                }
            )

        async def on_trade(payload: dict[str, Any]) -> None:
            await self._handle_trade(payload, source="ws")

        self._ws = PolyActivityWs(targets, on_trade)
        ws_task = asyncio.create_task(self._ws.run(), name="poly-activity-ws")
        poll_task = asyncio.create_task(self._poll_loop(), name="poly-activity-poll")

        try:
            while not self._shutdown:
                await asyncio.sleep(15)
                try:
                    ensure_bots_from_watchlist()
                    wallets = load_watchlist()
                    for w in wallets:
                        set_baseline_if_needed(w["id"])
                    new_targets = {w["address"] for w in wallets}
                    if new_targets != targets:
                        targets = new_targets
                        self._ws.set_targets(targets)
                        with self._lock:
                            self._status["wallets"] = [
                                {"id": w["id"], "address": w["address"]}
                                for w in wallets
                            ]
                except Exception as exc:
                    logger.warning("poly watchlist refresh: %s", exc)
        finally:
            self._shutdown = True
            if self._ws is not None:
                self._ws.request_stop()
            for t in (ws_task, poll_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            with self._lock:
                self._status["running"] = False

    async def _handle_trade(self, payload: dict[str, Any], *, source: str) -> None:
        from utils.poly_paper_copy import ingest_trade, refresh_leader_equity

        proxy = str(
            payload.get("proxyWallet") or payload.get("proxy_wallet") or ""
        ).strip().lower()
        try:
            from utils.poly_config import load_watchlist

            for w in load_watchlist():
                if w["address"] == proxy:
                    await asyncio.to_thread(refresh_leader_equity, w["id"], False)
                    break
        except Exception:
            pass

        result = await asyncio.to_thread(ingest_trade, payload, source=source)
        reason = result.get("reason")
        # Poll re-reads recent history; duplicates must not inflate counters.
        if reason in ("duplicate", "normalize_failed", "no_proxy", "no_bot"):
            return
        with self._lock:
            self._status["fills_seen"] = int(self._status.get("fills_seen") or 0) + 1
            if result.get("applied"):
                self._status["fills_copied"] = int(
                    self._status.get("fills_copied") or 0
                ) + len(result["applied"])
                self._status["last_fill_at"] = datetime.now(timezone.utc).isoformat()

    async def _poll_loop(self) -> None:
        from utils.poly_config import load_watchlist
        from utils.poly_data_api import fetch_activity_trades
        from utils.poly_paper_copy import (
            bot_baseline_ts,
            paper_config,
            refresh_leader_equity,
            set_baseline_if_needed,
        )

        while not self._shutdown:
            poll_sec = float(paper_config().get("poll_sec") or 4)
            try:
                for w in load_watchlist():
                    if self._shutdown:
                        break
                    set_baseline_if_needed(w["id"])
                    await asyncio.to_thread(refresh_leader_equity, w["id"], False)
                    baseline = bot_baseline_ts(w["id"])
                    # Strictly after baseline (copy_current=false); avoids
                    # replaying the cutoff second as before_baseline skips.
                    start = (int(baseline) + 1) if baseline is not None else None
                    rows = await asyncio.to_thread(
                        fetch_activity_trades,
                        w["address"],
                        limit=50,
                        start=start,
                    )
                    for row in reversed(rows):
                        await self._handle_trade(row, source="poll")
                with self._lock:
                    self._status["poll_cycles"] = (
                        int(self._status.get("poll_cycles") or 0) + 1
                    )
                    # Clear transient poll errors after a successful cycle.
                    if self._status.get("error") not in (None, "no_target_wallet"):
                        self._status["error"] = None
            except Exception as exc:
                logger.warning("poly poll cycle failed: %s", exc)
                with self._lock:
                    self._status["error"] = str(exc)
            await self._sleep_interruptible(poll_sec)

    async def _sleep_interruptible(self, seconds: float) -> None:
        end = asyncio.get_running_loop().time() + max(0.0, seconds)
        while not self._shutdown:
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.5, remaining))


poly_copy_supervisor = PolyCopySupervisor()
