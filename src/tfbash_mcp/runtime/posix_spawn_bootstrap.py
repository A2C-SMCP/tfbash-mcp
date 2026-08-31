"""Single-threaded child bootstrap for the macOS ``posix_spawn`` PTY path."""

from __future__ import annotations

import errno
import fcntl
import os
import sys
import termios
from collections.abc import Sequence
from typing import NoReturn

_STANDARD_INPUT = 0


def configure_terminal() -> None:
    """Claim standard input as the controlling terminal and foreground this group."""

    try:
        os.tcgetpgrp(_STANDARD_INPUT)
    except OSError as error:
        if error.errno != errno.ENOTTY:
            raise
        fcntl.ioctl(_STANDARD_INPUT, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(_STANDARD_INPUT, os.getpgrp())


def main(arguments: Sequence[str] | None = None) -> NoReturn:
    """Prepare the spawned PTY session, then replace this helper with the shell."""

    launch_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(launch_arguments) < 2:
        raise SystemExit("working directory and executable are required")
    working_directory, executable, *executable_arguments = launch_arguments
    configure_terminal()
    os.chdir(working_directory)
    os.execve(
        executable,
        [executable, *executable_arguments],
        dict(os.environ),
    )


if __name__ == "__main__":
    main()
