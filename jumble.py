"""Compatibility façade for legacy jumble package while migrating toward flat module layout."""
from jumble.blockchain import jumble_blockchain
from jumble.jumble_service import jumble_service
from jumble.routes import jumble_bp


def init_jumble(app):
    try:
        app.register_blueprint(jumble_bp)
        return True
    except Exception as e:
        print(f"❌ Jumble initialization failed: {e}")
        return False


__all__ = ['jumble_blockchain', 'jumble_service', 'jumble_bp', 'init_jumble']

from jumble.blockchain import *  # noqa: F401,F403

from jumble.routes import *  # noqa: F401,F403

from jumble.jumble_service import *  # noqa: F401,F403
