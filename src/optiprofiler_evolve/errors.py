"""Control-flow failures shared by phases and invocation services."""


class RunCancelled(RuntimeError):
    """The controller requested cooperative cancellation."""
