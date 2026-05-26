"""Compatibility façade for legacy reloadly package while migrating toward flat module layout."""
from reloadly.routes import reloadly_bp
from reloadly.client import reloadly_client


def init_reloadly(app):
    """Initialize Reloadly module"""
    try:
        app.register_blueprint(reloadly_bp)
        if reloadly_client.is_initialized:
            import logging
            logging.getLogger(__name__).info(
                f"✅ Reloadly module initialized ({reloadly_client.environment})"
            )
        else:
            import logging
            logging.getLogger(__name__).warning(
                "⚠️ Reloadly module loaded but API credentials not set"
            )
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"❌ Reloadly initialization failed: {e}")
        return False


__all__ = ["reloadly_bp", "reloadly_client", "init_reloadly"]

from reloadly.service import *  # noqa: F401,F403

from reloadly.routes import *  # noqa: F401,F403

from reloadly.client import *  # noqa: F401,F403
