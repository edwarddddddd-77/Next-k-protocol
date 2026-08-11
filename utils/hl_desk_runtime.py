"""HL 映仓台 runtime: copy supervisor + weekly candidate pool."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_scheduler: Any = None
_scheduler_lock = threading.Lock()


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
    """Start WS copy supervisor and optional weekly candidates job."""
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

    _start_weekly_candidates(app)


def stop_hl_desk(app: Any | None = None) -> None:
    try:
        from utils.hl_copy_supervisor import hl_copy_supervisor

        hl_copy_supervisor.stop()
    except Exception as exc:
        logger.warning("HL copy supervisor shutdown skipped: %s", exc)

    global _scheduler
    with _scheduler_lock:
        sch = _scheduler
        _scheduler = None
    if sch is not None:
        try:
            sch.shutdown(wait=False)
        except Exception as exc:
            logger.warning("HL desk scheduler shutdown skipped: %s", exc)
    if app is not None:
        setattr(getattr(app, "state", app), "hl_desk_scheduler", None)


def _start_weekly_candidates(app: Any | None) -> None:
    raw = (os.getenv("HL_DESK_WEEKLY_SCHEDULER") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        logger.info("HL desk weekly candidates scheduler off")
        return
    try:
        import pytz
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as exc:
        logger.warning("HL desk weekly scheduler unavailable: %s", exc)
        return

    def _job() -> None:
        try:
            from utils.hl_desk_candidates import build_candidate_pool

            logger.info("HL desk candidates weekly build starting…")
            board = build_candidate_pool()
            logger.info(
                "HL desk candidates weekly done: ready=%s watch=%s bound=%s",
                board.get("ready_count"),
                board.get("watch_count"),
                board.get("bound_count"),
            )
        except Exception:
            logger.exception("HL desk candidates weekly failed")

    tz = pytz.timezone("Asia/Shanghai")
    sch = BackgroundScheduler(timezone=tz)
    sch.add_job(
        _job,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=30,
        id="hl_desk_candidates_weekly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sch.start()
    global _scheduler
    with _scheduler_lock:
        _scheduler = sch
    if app is not None:
        setattr(getattr(app, "state", app), "hl_desk_scheduler", sch)
    logger.info("HL desk weekly candidates scheduler started (Mon 09:30 Asia/Shanghai)")
