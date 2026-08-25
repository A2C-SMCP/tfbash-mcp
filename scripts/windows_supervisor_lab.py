#!/usr/bin/env python3
"""Run the Windows supervisor SSH smoke from macOS."""

from __future__ import annotations

from experiments.windows_supervisor_native.lab import main

if __name__ == "__main__":
    raise SystemExit(main())
