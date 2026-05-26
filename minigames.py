"""Compatibility façade for legacy minigames package while migrating toward flat module layout."""

from minigames.minigames_manager import minigames_manager
from minigames.routes import minigames_bp
from minigames.blockchain import minigames_blockchain

def init_minigames(app):
    """Initialize minigames system"""
    try:
        app.register_blueprint(minigames_bp)
        return True
    except Exception as e:
        print(f"❌ Minigames initialization failed: {e}")
        return False

__all__ = ['minigames_manager', 'minigames_bp', 'minigames_blockchain', 'init_minigames']

from minigames.blockchain import *  # noqa: F401,F403

from minigames.routes import *  # noqa: F401,F403

from minigames.minigames_manager import *  # noqa: F401,F403
