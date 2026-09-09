"""Control-flow failures shared by phases and invocation services."""


class RunCancelled(RuntimeError):
    """The controller requested cooperative cancellation."""


class IntegrityReviewerUnavailable(RuntimeError):
    """Strict reviewer availability could not be satisfied."""
