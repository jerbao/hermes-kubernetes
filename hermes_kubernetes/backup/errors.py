"""Typed recoverable backup errors that preserve safe public messages."""


class SnapshotRetryError(RuntimeError):
    """A source changed during capture; retry the complete snapshot once."""
