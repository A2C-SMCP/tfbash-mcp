"""Shared Shell Overview Resource contract for standalone and embedded hosts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from mcp import types
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

SHELL_OVERVIEW_URI = "window://io.github.a2c-smcp.tfbash/shell-overview"
SHELL_OVERVIEW_MIME_TYPE = "text/markdown"


class ShellOverviewSource(Protocol):
    """Resource-facing operations supplied by the composed Shell service."""

    def shell_overview_markdown(self) -> str: ...

    def subscribe_overview_changes(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class ShellResourceAdapter:
    """Own the public Resource descriptor, read contract, and update events."""

    def __init__(self, source: ShellOverviewSource) -> None:
        self._source = source

    def list_resources(self) -> tuple[types.Resource, ...]:
        return (
            types.Resource(
                uri=AnyUrl(SHELL_OVERVIEW_URI),
                name="Shell Overview",
                description="Current Shell states and recent execution output.",
                mimeType=SHELL_OVERVIEW_MIME_TYPE,
                annotations=types.Annotations(
                    priority=0.8,
                    audience=["assistant"],
                ),
                _meta={"fullscreen": False},
            ),
        )

    def read_resource(self, uri: str | AnyUrl) -> types.ReadResourceResult:
        resource_uri = self.validate_resource_uri(uri)
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=resource_uri,
                    mimeType=SHELL_OVERVIEW_MIME_TYPE,
                    text=self._source.shell_overview_markdown(),
                )
            ]
        )

    def subscribe_updates(
        self,
        listener: Callable[[AnyUrl], None],
    ) -> Callable[[], None]:
        """Invoke ``listener`` synchronously on the Domain event producer thread."""

        resource_uri = AnyUrl(SHELL_OVERVIEW_URI)
        return self._source.subscribe_overview_changes(lambda: listener(resource_uri))

    @staticmethod
    def validate_resource_uri(uri: str | AnyUrl) -> AnyUrl:
        """Normalize a supported URI or raise the shared MCP error contract."""

        try:
            resource_uri = AnyUrl(uri)
        except ValueError:
            raise _unknown_resource_error() from None
        if str(resource_uri) != SHELL_OVERVIEW_URI:
            raise _unknown_resource_error()
        return resource_uri


def _unknown_resource_error() -> McpError:
    return McpError(
        types.ErrorData(
            code=types.INVALID_PARAMS,
            message="Unknown Resource URI.",
        )
    )
