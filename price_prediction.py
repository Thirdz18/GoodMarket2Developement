"""Compatibility facade for legacy price_prediction package while migrating toward flat module layout."""

from routes import price_prediction_bp
from price_prediction.price_prediction_service import price_prediction_service
from app import init_price_prediction

__all__ = ['price_prediction_bp', 'price_prediction_service', 'init_price_prediction']
