from __future__ import annotations

from collections.abc import Callable

import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from tfbash_mcp.resource_adapter import SHELL_OVERVIEW_URI, ShellResourceAdapter


class FakeOverviewSource:
    def __init__(self) -> None:
        self.listeners: set[Callable[[], None]] = set()

    def shell_overview_markdown(self) -> str:
        return "# Shell Overview\n\nshared contents"

    def subscribe_overview_changes(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)


def test_resource_adapter_owns_descriptor_and_read_contract() -> None:
    adapter = ShellResourceAdapter(FakeOverviewSource())

    first = adapter.list_resources()[0]
    second = adapter.list_resources()[0]
    assert first is not second
    assert str(first.uri) == SHELL_OVERVIEW_URI
    assert first.name == "Shell Overview"
    assert first.description == "Current Shell states and recent execution output."
    assert first.mimeType == "text/markdown"
    assert first.annotations == types.Annotations(priority=0.8, audience=["assistant"])
    assert first.meta == {"fullscreen": False}

    result = adapter.read_resource(first.uri)
    assert result == types.ReadResourceResult(
        contents=[
            types.TextResourceContents(
                uri=AnyUrl(SHELL_OVERVIEW_URI),
                mimeType="text/markdown",
                text="# Shell Overview\n\nshared contents",
            )
        ]
    )


def test_resource_adapter_rejects_unknown_uri_consistently() -> None:
    adapter = ShellResourceAdapter(FakeOverviewSource())

    assert adapter.read_resource(SHELL_OVERVIEW_URI).contents
    assert adapter.read_resource(AnyUrl(SHELL_OVERVIEW_URI)).contents

    for invalid_uri in ("not a uri", "window://io.github.a2c-smcp.tfbash/unknown"):
        with pytest.raises(McpError) as caught:
            adapter.read_resource(invalid_uri)

        assert caught.value.error.code == types.INVALID_PARAMS
        assert caught.value.error.message == "Unknown Resource URI."


def test_resource_adapter_maps_domain_changes_to_resource_uri() -> None:
    source = FakeOverviewSource()
    adapter = ShellResourceAdapter(source)
    updates: list[str] = []

    unsubscribe = adapter.subscribe_updates(lambda uri: updates.append(str(uri)))
    for listener in tuple(source.listeners):
        listener()
    assert updates == [SHELL_OVERVIEW_URI]

    unsubscribe()
    unsubscribe()
    assert not source.listeners
