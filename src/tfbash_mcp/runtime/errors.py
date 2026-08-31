"""Failures at the platform-neutral runtime boundary."""


class RuntimeBoundaryError(RuntimeError):
    """Base class for failures raised below the Shell Domain."""


class StartupHandshakeError(RuntimeBoundaryError):
    """A shell did not reach one of the bounded startup protocol phases."""

    def __init__(self, phase: str) -> None:
        if phase not in {"initial-prompt", "startup-record"}:
            raise ValueError("invalid startup handshake phase")
        self.phase = phase
        super().__init__("shell did not become ready before startup deadline")


class RuntimeConfigurationError(RuntimeBoundaryError):
    """The process-level host/runtime configuration is invalid."""


class UnsupportedShell(RuntimeBoundaryError):
    """An executable cannot satisfy the selected dialect contract."""


class DialectProtocolError(RuntimeBoundaryError):
    """Shell output violated the dialect framing protocol."""


class TransportError(RuntimeBoundaryError):
    """A PTY operation failed."""


class TransportClosed(TransportError):
    """A PTY operation targeted an already closed session."""


class ProcessControlError(RuntimeBoundaryError):
    """A semantic process-control operation could not be delivered."""


class CleanupTimeout(ProcessControlError):
    """Managed descendants could not be reaped before the deadline."""
