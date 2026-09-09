import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from optiprofiler_evolve.observability.live_views import LiveViews


class LiveViewsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.views = LiveViews(Path(directory.name), Mock())
        self.views.events = Mock()

    def test_io_failures_are_reported_with_tracebacks(self):
        with patch(
            "optiprofiler_evolve.observability.live_views.project_public_events",
            side_effect=OSError("unreadable ledger"),
        ):
            with patch("optiprofiler_evolve.observability.live_views.logger") as logger:
                self.views.refresh()
        self.views.emit.assert_called_once_with(
            "public_view_refresh_failed", "failed", data={"error": "OSError: unreadable ledger"},
        )
        logger.exception.assert_called_once()

    def test_programming_errors_are_not_converted_to_io_failures(self):
        with patch(
            "optiprofiler_evolve.observability.live_views.project_public_events",
            side_effect=AttributeError("bug"),
        ):
            with self.assertRaisesRegex(AttributeError, "bug"):
                self.views.refresh()
        self.views.emit.assert_not_called()

    def test_background_failures_keep_the_traceback(self):
        with patch.object(self.views, "_stop") as stop:
            stop.wait.side_effect = [False, True]
            with patch.object(self.views, "refresh_if_due", side_effect=AttributeError("bug")):
                with patch("optiprofiler_evolve.observability.live_views.logger") as logger:
                    self.views._loop()
        logger.exception.assert_called_once_with("Background view refresh failed")

    def test_stop_joins_the_background_thread(self):
        self.views.start(Mock())
        self.addCleanup(self.views.stop)
        thread = self.views.thread
        self.views.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.views.thread)
