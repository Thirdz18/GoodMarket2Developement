"""Primary Flask application module.

This module exposes `app` for WSGI/ASGI servers AND centralises the
initialisation of every feature module (blueprint registration, background
workers, etc.).  All `init_*` functions that were previously scattered across
each module's ``__init__.py`` are now defined here so that the startup
sequence lives in one place.
"""

from main import app  # noqa: F401  — creates the Flask app & middleware

import logging

logger = logging.getLogger(__name__)

# =========================================================================
# Consolidated init functions (moved from each module's __init__.py)
# =========================================================================

# --- Routes (main) -------------------------------------------------------
from routes import routes  # main Blueprint, already registered in main.py

# --- Telegram Task --------------------------------------------------------

def init_telegram_task(app):
    """Initialize Telegram Task module"""
    try:
        from telegram_task.telegram_task import TelegramTaskService
        from routes import routes as _routes_bp  # noqa: F811
        return True
    except Exception as e:
        logger.error(f"Telegram Task initialization failed: {e}")
        return False


# --- Twitter Task ---------------------------------------------------------

def init_twitter_task(app):
    """Initialize Twitter Task module"""
    try:
        from twitter_task.twitter_task import TwitterTaskService
        return True
    except Exception as e:
        logger.error(f"Twitter Task initialization failed: {e}")
        return False


# --- Discourse Task -------------------------------------------------------

def init_discourse_task(app):
    """Initialize Discourse Task module"""
    try:
        from discourse_task.discourse_task import DiscourseTaskService
        return True
    except Exception as e:
        logger.error(f"Discourse Task initialization failed: {e}")
        return False


# --- Minigames ------------------------------------------------------------

def init_minigames(app):
    """Initialize Minigames system"""
    try:
        from routes import minigames_bp
        app.register_blueprint(minigames_bp)
        return True
    except Exception as e:
        logger.error(f"Minigames initialization failed: {e}")
        return False


# --- Jumble ---------------------------------------------------------------

def init_jumble(app):
    """Initialize Jumble Words system"""
    try:
        from routes import jumble_bp
        app.register_blueprint(jumble_bp)
        return True
    except Exception as e:
        logger.error(f"Jumble initialization failed: {e}")
        return False


# --- Price Prediction -----------------------------------------------------

def init_price_prediction(app):
    """Initialize Price Prediction system"""
    from routes import price_prediction_bp
    app.register_blueprint(price_prediction_bp)


# --- Savings --------------------------------------------------------------

def init_savings(app):
    """Initialize G$ Savings module"""
    try:
        from routes import savings_bp
        app.register_blueprint(savings_bp)
        logger.info("G$ Savings module initialized")
        return True
    except Exception as e:
        logger.error(f"G$ Savings initialization failed: {e}")
        return False


# --- Reloadly -------------------------------------------------------------

def init_reloadly(app):
    """Initialize Reloadly module"""
    try:
        from routes import reloadly_bp
        from reloadly.client import reloadly_client

        app.register_blueprint(reloadly_bp)
        if reloadly_client.is_initialized:
            logger.info(
                f"Reloadly module initialized ({reloadly_client.environment})"
            )
        else:
            logger.warning(
                "Reloadly module loaded but API credentials not set"
            )
        return True
    except Exception as e:
        logger.error(f"Reloadly initialization failed: {e}")
        return False


# --- Community Stories ----------------------------------------------------

def init_community_stories(app):
    """Initialize Community Stories module"""
    try:
        from routes import community_stories_bp
        app.register_blueprint(community_stories_bp, url_prefix='/community-stories')
        logger.info("Community Stories module initialized")
        return True
    except Exception as e:
        logger.error(f"Community Stories initialization failed: {e}")
        return False


# --- Learn & Earn ---------------------------------------------------------

def init_learn_and_earn(app):
    """Initialize Learn & Earn system"""
    try:
        from learn_and_earn.learn_and_earn import init_learn_and_earn as _original_init
        return _original_init(app)
    except Exception as e:
        logger.error(f"Learn & Earn initialization failed: {e}")
        return False


def init_learn_earn_stream_scheduler(app):
    """Initialize Learn & Earn stream scheduler"""
    try:
        from learn_and_earn.stream_scheduler import init_learn_earn_stream_scheduler as _original_init
        return _original_init(app)
    except Exception as e:
        logger.error(f"Learn & Earn stream scheduler initialization failed: {e}")
        return False


# --- P2P Trading ----------------------------------------------------------

def init_p2p_trading(app):
    """Initialize P2P Trading system.

    Registers the p2p blueprint and optionally starts the background
    indexer (opt-in via P2P_INDEXER_ENABLED env var).
    """
    try:
        import os
        from routes import p2p_bp
        app.register_blueprint(p2p_bp, url_prefix="/p2p")
        if os.getenv("P2P_INDEXER_ENABLED", "").lower() in ("1", "true", "yes"):
            from p2p_trading.indexer import get_indexer
            get_indexer().start()
        return True
    except Exception as e:
        logger.error(f"P2P Trading initialization failed: {e}")
        return False


# --- Referral Program (no init function, just blueprint) ------------------


# =========================================================================
# Module initialisation — execute all init_* functions
# =========================================================================
# The calls below were previously scattered through main.py (lines 562-714).
# Keeping them here means app.py is the single source of truth for
# "what gets initialised and how".

# Initialize Telegram Task
if not init_telegram_task(app):
    logger.warning("⚠️ Telegram Task initialization failed")
else:
    logger.info("✅ Telegram Task initialized successfully")

# Initialize Twitter Task
if not init_twitter_task(app):
    logger.warning("⚠️ Twitter Task initialization failed")
else:
    logger.info("✅ Twitter Task initialized successfully")

# Initialize Discourse Task
if not init_discourse_task(app):
    logger.warning("⚠️ Discourse Task initialization failed")
else:
    logger.info("✅ Discourse Task initialized successfully")

# Initialize News Feed
from news_feed import init_news_feed, news_feed_service
init_news_feed(app)

# Initialize Minigames System
logger.info("🎮 Initializing Minigames system...")
if init_minigames(app):
    logger.info("✅ Minigames system initialized")
else:
    logger.error("❌ Minigames initialization failed")

# Register Telegram bot blueprint
from telegram_bot import telegram_bot
app.register_blueprint(telegram_bot)
logger.info("✅ Telegram bot blueprint registered")

# Initialize G$ Savings
logger.info("💰 Initializing G$ Savings system...")
if init_savings(app):
    logger.info("✅ G$ Savings initialized")
else:
    logger.error("❌ G$ Savings initialization failed")

# Initialize Reloadly Store
logger.info("🛒 Initializing Reloadly Store...")
if init_reloadly(app):
    logger.info("✅ Reloadly Store initialized")
else:
    logger.error("❌ Reloadly Store initialization failed")

# Initialize Jumble Words System
logger.info("🔤 Initializing Jumble Words system...")
if init_jumble(app):
    logger.info("✅ Jumble Words system initialized")
else:
    logger.error("❌ Jumble Words initialization failed")

# Initialize Price Prediction System
logger.info("📈 Initializing Price Prediction system...")
try:
    init_price_prediction(app)
    logger.info("✅ Price Prediction system initialized")
except Exception as e:
    logger.error(f"❌ Price Prediction initialization failed: {e}")

# Initialize Community Stories System
logger.info("🌟 Initializing Community Stories system...")
if init_community_stories(app):
    logger.info("✅ Community Stories system initialized")
else:
    logger.error("❌ Community Stories initialization failed")

# Initialize Reward Configuration Service
logger.info("💰 Initializing Reward Configuration Service...")
try:
    from reward_config_service import reward_config_service
    logger.info("✅ Reward Configuration Service initialized")
except Exception as e:
    logger.error(f"❌ Reward Configuration Service initialization failed: {e}")

# Initialize Referral Program system
logger.info("🎁 Initializing Referral Program system...")
try:
    from referral_program import referral_bp
    app.register_blueprint(referral_bp)
    logger.info("✅ Referral Program system ready")
except Exception as e:
    logger.warning(f"⚠️ Referral Program initialization failed: {e}")

# Initialize Learn & Earn System
logger.info("🎓 Initializing Learn & Earn system...")
if init_learn_and_earn(app):
    logger.info("✅ Learn & Earn system initialized")
else:
    logger.error("❌ Learn & Earn initialization failed")

# Spawn Learn & Earn stream worker
try:
    if init_learn_earn_stream_scheduler(app):
        logger.info("✅ Learn & Earn stream scheduler started")
    else:
        logger.info("ℹ️ Learn & Earn stream scheduler not started (disabled or instant mode)")
except Exception as e:
    logger.error(f"❌ Learn & Earn stream scheduler initialization failed: {e}")

# Initialize trustless P2P Trading
logger.info("🤝 Initializing P2P Trading system...")
try:
    init_p2p_trading(app)
    logger.info("✅ P2P Trading system initialized")
except Exception as e:
    logger.error(f"❌ P2P Trading initialization failed: {e}")

# Initialize GoodMarket claim reconciler
logger.info("🧮 Initializing GoodMarket claim reconciler...")
try:
    from goodmarket_claim_reconciler import init_goodmarket_claim_reconciler
    if init_goodmarket_claim_reconciler(app):
        logger.info("✅ GoodMarket claim reconciler started")
    else:
        logger.info("ℹ️ GoodMarket claim reconciler not started (disabled)")
except Exception as e:
    logger.error(f"❌ GoodMarket claim reconciler initialization failed: {e}")

# Initialize GoodMarket attribution backfill
logger.info("🏷️ Initializing GoodMarket attribution backfill...")
try:
    from goodmarket_attribution_backfill import init_attribution_backfill
    if init_attribution_backfill(app):
        logger.info("✅ GoodMarket attribution backfill scheduled")
    else:
        logger.info("ℹ️ GoodMarket attribution backfill not scheduled (disabled or already ran)")
except Exception as e:
    logger.error(f"❌ GoodMarket attribution backfill initialization failed: {e}")
