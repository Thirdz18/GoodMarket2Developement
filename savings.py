"""Compatibility façade for legacy savings package while migrating toward flat module layout."""
from savings.routes import savings_bp


def init_savings(app):
    """Initialize G$ Savings module"""
    try:
        app.register_blueprint(savings_bp)
        import logging
        logging.getLogger(__name__).info("✅ G$ Savings module initialized")
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"❌ G$ Savings initialization failed: {e}")
        return False


__all__ = ["savings_bp", "init_savings"]

from savings.blockchain import *  # noqa: F401,F403

from savings.routes import *  # noqa: F401,F403
