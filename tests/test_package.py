from importlib import metadata
from pathlib import Path

from tfbash_mcp import __version__
from tfbash_mcp.server import server


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_server_is_configured() -> None:
    assert server.name == "tfbash-mcp"


def test_distribution_does_not_depend_on_the_complete_ide4ai_runtime() -> None:
    requirements = metadata.requires("tfbash-mcp") or []
    assert all("ide4ai" not in requirement.casefold() for requirement in requirements)


def test_ide4ai_derivation_notice_retains_baseline_and_mit_terms() -> None:
    notice = (Path(__file__).parents[1] / "NOTICE").read_text()
    assert "20ece038e66e13885e77503e217b23766e60dc86" in notice
    assert "Copyright (c) 2025 JQQ and A2C-SMCP/ide4ai contributors" in notice
    assert "Permission is hereby granted, free of charge" in notice
