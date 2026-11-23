"""Structured CLI errors and cancellation exceptions."""

from __future__ import annotations

from typing import Any


class CliError(RuntimeError):
    """Domain, usage, or external-tool failure carrying a structured error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fix: str = "",
        exit_code: int = 1,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.exit_code = exit_code
        self.details = details


class SelectionCancelled(RuntimeError):
    """Interactive selection was cancelled by the user."""

    exit_code = 130
