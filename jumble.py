"""Backward-compatibility shim — imports from the new flat-file locations."""

from jumble_service import jumble_service
from blockchain import jumble_blockchain

__all__ = ['jumble_service', 'jumble_blockchain']
