"""Compatibility facade for legacy reloadly package while migrating toward flat module layout."""
from routes import reloadly_bp
from reloadly.client import reloadly_client
from reloadly.service import get_user_orders
from app import init_reloadly

__all__ = ["reloadly_bp", "reloadly_client", "init_reloadly", "get_user_orders"]
