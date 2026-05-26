"""Backward-compatibility shim — imports from the new flat-file locations."""

from p2p_contract import get_contract
from p2p_escrow_service import escrow_service
from p2p_indexer import get_indexer

__all__ = [
    "escrow_service",
    "get_contract",
    "get_indexer",
]
