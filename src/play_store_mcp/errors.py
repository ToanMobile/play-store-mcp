"""Shared exception types.

Split out of ``client`` so that low-level helpers (``credentials``) can raise
the canonical error without importing the 6k-line client module and creating
an import cycle. ``play_store_mcp.client`` re-exports ``PlayStoreClientError``,
so ``from play_store_mcp.client import PlayStoreClientError`` keeps working.
"""

from __future__ import annotations


class PlayStoreClientError(Exception):
    """Base exception for Play Store client errors."""
