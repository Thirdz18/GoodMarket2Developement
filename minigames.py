"""Compatibility facade for legacy minigames package while migrating toward flat module layout."""
from minigames.minigames_manager import minigames_manager
from routes import minigames_bp
from blockchain import minigames_blockchain
from app import init_minigames

__all__ = ['minigames_manager', 'minigames_bp', 'minigames_blockchain', 'init_minigames']
