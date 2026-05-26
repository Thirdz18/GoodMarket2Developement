"""Backward-compatibility shim — imports from the new flat-file locations."""

from minigames_manager import minigames_manager, MinigamesManager
from blockchain import minigames_blockchain

__all__ = ['minigames_manager', 'MinigamesManager', 'minigames_blockchain']
