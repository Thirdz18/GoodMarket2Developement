"""Trustless P2P escrow module — re-exports for backward compatibility."""

from .contract import get_contract
from .escrow_service import escrow_service
from .indexer import get_indexer

__all__ = [
    "escrow_service",
    "get_contract",
    "get_indexer",
]
