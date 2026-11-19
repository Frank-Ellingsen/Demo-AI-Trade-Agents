"""Alpaca Execution Client (`alpaca_execution.py`).

Compatibility facade re-exporting the hardened Paper-Only `AlpacaClient` and
`build_alpaca_bracket_payload` from `src.brokers.alpaca_client`.
"""

from __future__ import annotations

from src.brokers.alpaca_client import AlpacaClient, build_alpaca_bracket_payload

__all__ = ["AlpacaClient", "build_alpaca_bracket_payload"]
