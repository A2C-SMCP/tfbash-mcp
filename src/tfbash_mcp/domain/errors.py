"""Platform-neutral domain failures."""


class DomainError(RuntimeError):
    """Base class for expected Shell Domain failures."""


class CapacityExceeded(DomainError):
    """A configured resource capacity has been exhausted."""


class ShellNotFound(DomainError):
    """The requested shell is not registered."""


class ShellBusy(DomainError):
    """The shell already owns an active execution."""


class ShellClosing(DomainError):
    """The shell has crossed its close admission fence."""


class ShellUnavailable(DomainError):
    """The shell is rebuilding or is in an error state."""


class ExecutionNotFound(DomainError):
    """The execution is unknown, expired, or belongs to another shell."""


class ExecutionNotActive(DomainError):
    """The execution no longer accepts input or control operations."""


class InvalidCursor(DomainError):
    """The cursor is beyond output or splits a UTF-8 code point."""


class InvalidTransition(DomainError):
    """A requested state transition violates the domain state machine."""
