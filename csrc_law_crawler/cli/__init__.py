"""Unified command-line entrypoint for the crawler."""

from __future__ import annotations


def main() -> int:
    from .main import main as dispatch

    return dispatch()

__all__ = ["main"]
