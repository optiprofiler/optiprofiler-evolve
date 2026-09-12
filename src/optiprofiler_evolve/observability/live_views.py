"""Live projections of the authoritative event ledger."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from optiprofiler_evolve.observability.events import EventWriter
from optiprofiler_evolve.observability.projections import project_public_events
from optiprofiler_evolve.observability.views.owner import render_owner_views
from optiprofiler_evolve.observability.views.public import (
    materialize_public_bundle,
    render_final_report,
    render_public_report,
    render_status,
)

logger = logging.getLogger(__name__)


class LiveViews:
    def __init__(self, run_dir: Path, emit: Callable[..., None]) -> None:
        self.run_dir = run_dir
        self.emit = emit
        self.events: EventWriter | None = None
        self._render_lock = threading.Lock()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.poll_seconds = 2.0
        self.due_seconds = 4.0
        self._last_refresh = 0.0

    def start(self, events: EventWriter) -> None:
        self.events = events
        self._stop.clear()
        self.thread = threading.Thread(
            target=self._loop,
            name="ope-status-refresher",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None and self.thread.ident is not None:
            # Bounded: a wedged renderer or an unresponsive filesystem must not
            # hold up run completion. The thread is a daemon, so abandoning it is
            # safe and the run still returns its result.
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning(
                    "Background view refresh did not stop within 10s; abandoning it."
                )
        self.thread = None

    def _loop(self) -> None:
        # Keep views current during long worker invocations.
        while not self._stop.wait(self.poll_seconds):
            try:
                self.refresh_if_due(interval_seconds=self.due_seconds)
            except Exception:
                logger.exception("Background view refresh failed")

    def refresh(self, *, include_report: bool = False) -> None:
        if self.events is None:
            return
        with self._render_lock:
            if self.events is None:
                return
            self.events.flush()
            try:
                public_events = self.run_dir / "public_events.jsonl"
                project_public_events(self.run_dir / "events.jsonl", public_events)
                state = render_status(public_events, self.run_dir / "public_status.html")
                render_public_report(state, self.run_dir / "PUBLIC_REPORT.md")
                if include_report:
                    render_final_report(public_events, self.run_dir / "report.html")
                materialize_public_bundle(self.run_dir)
                render_owner_views(
                    self.run_dir / "events.jsonl",
                    self.run_dir,
                    final=include_report,
                )
            except Exception as exc:
                logger.exception("View refresh failed")
                self.emit(
                    "public_view_refresh_failed",
                    "failed",
                    data={"error": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                self._last_refresh = time.monotonic()

    def refresh_if_due(self, interval_seconds: float = 1.0) -> None:
        if time.monotonic() - self._last_refresh >= interval_seconds:
            self.refresh()
