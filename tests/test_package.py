from tfbash_mcp import __version__
from tfbash_mcp.server import server


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_server_is_configured() -> None:
    assert server.name == "tfbash-mcp"
