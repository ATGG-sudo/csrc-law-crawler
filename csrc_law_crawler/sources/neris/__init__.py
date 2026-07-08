"""NERIS source adapter exports."""

from __future__ import annotations

from .client import HumanLikeClient
from .parser import build_law_document, parse_law_writ_info_html

__all__ = ["HumanLikeClient", "build_law_document", "parse_law_writ_info_html"]
