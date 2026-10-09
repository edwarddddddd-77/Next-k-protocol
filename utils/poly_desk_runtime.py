"""Polymarket copy desk runtime: supervisor start/stop."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def desk_enabled() -> bool:
    raw = (os.getenv("POLY_DESK_ENABLED") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def ensure_poly_data_dir() -> Path:
    from utils.poly_config import resolve_data_dir

    return resolve_data_dir()


def start_poly_desk(app: Any | None = None) -> None:
    if not desk_enabled():
        logger.info("poly desk disabled (POLY_DESK_ENABLED=0)")
        return
    try:
        data_dir = ensure_poly_data_dir()
        logger.info("poly desk data dir: %s", data_dir)
    except Exception as exc:
        logger.warning("poly desk data dir ensure failed: %s", exc)

    try:
        from utils.poly_paper_copy import ensure_bots_from_watchlist

        ensure_bots_from_watchlist()
    except Exception as exc:
        logger.warning("poly paper init skipped: %s", exc)

    try:
        from utils.poly_copy_supervisor import poly_copy_supervisor

        poly_copy_supervisor.start()
        logger.info("poly copy supervisor start requested")
    except Exception as exc:
        logger.warning("poly copy supervisor startup skipped: %s", exc)


def stop_poly_desk(app: Any | None = None) -> None:
    try:
        from utils.poly_copy_supervisor import poly_copy_supervisor

        poly_copy_supervisor.stop()
    except Exception as exc:
        logger.warning("poly copy supervisor shutdown skipped: %s", exc)
