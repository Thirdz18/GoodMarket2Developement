"""Minigames module — re-exports for backward compatibility."""

from .minigames_manager import minigames_manager
from blockchain import minigames_blockchain

__all__ = ['minigames_manager', 'minigames_blockchain']
