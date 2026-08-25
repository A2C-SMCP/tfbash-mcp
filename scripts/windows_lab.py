#!/usr/bin/env python3
"""SSH-first controller for the disposable Windows Phase 0 lab."""

import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    module = import_module("experiments.windows_phase0.lab")
    cli = cast(Callable[[], int], module.cli)
    raise SystemExit(cli())
