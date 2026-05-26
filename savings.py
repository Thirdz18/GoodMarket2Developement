"""Compatibility facade for legacy savings package while migrating toward flat module layout."""

from routes import savings_bp
from app import init_savings

__all__ = ["savings_bp", "init_savings"]
