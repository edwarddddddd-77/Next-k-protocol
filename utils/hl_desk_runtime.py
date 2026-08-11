"""HL 映仓台 runtime: copy supervisor only (no candidate pool)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def desk_enabled() -> bool:
    raw = (os.getenv("HL_DESK_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def ensure_hl_data_dir() -> Path:
    """Make sure DATA_DIR (or fallback) exists before HL JSON/SQLite writes."""
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        root = Path(raw).expanduser()
    else:
        for candidate in (Path("/app/data"), Path("/data")):
            if candidate.is_dir():
                root = candidate
                break
        else:
            root = Path(__file__).resolve().parents[1]
    root.mkdir(parents=True, exist_ok=True)
    return root


def start_hl_desk(app: Any | None = None) -> None:
    """Start WS copy supervisor."""
    if not desk_enabled():
        logger.info("HL desk disabled (HL_DESK_ENABLED=0)")
        return

    try:
        data_dir = ensure_hl_data_dir()
        logger.info("HL desk data dir: %s", data_dir)
    except Exception as exc:
        logger.warning("HL desk data dir ensure failed: %s", exc)

    try:
        from utils.hl_copy_supervisor import hl_copy_supervisor

        hl_copy_supervisor.start()
        logger.info("HL copy supervisor started")
    except Exception as exc:
        logger.warning("HL copy supervisor startup skipped: %s", exc)


def stop_hl_desk(app: Any | None = None) -> None:
    try:
        from utils.hl_copy_supervisor import hl_copy_supervisor

        hl_copy_supervisor.stop()
    except Exception as exc:
        logger.warning("HL copy supervisor shutdown skipped: %s", exc)
