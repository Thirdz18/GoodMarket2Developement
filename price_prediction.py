"""Compatibility façade for legacy price_prediction package while migrating toward flat module layout."""
from price_prediction.routes import price_prediction_bp
from price_prediction.price_prediction_service import price_prediction_service

def init_price_prediction(app):
    app.register_blueprint(price_prediction_bp)

from price_prediction.routes import *  # noqa: F401,F403

from price_prediction.price_prediction_service import *  # noqa: F401,F403
