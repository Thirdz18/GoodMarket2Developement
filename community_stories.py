"""Compatibility façade for legacy community_stories package while migrating toward flat module layout."""

from flask import Flask
from community_stories.routes import community_stories_bp
from community_stories.community_stories_service import community_stories_service
import logging

logger = logging.getLogger(__name__)

def init_community_stories(app: Flask):
    """Initialize Community Stories module"""
    try:
        # Register blueprint
        app.register_blueprint(community_stories_bp, url_prefix='/community-stories')
        
        logger.info("✅ Community Stories module initialized")
        return True
    except Exception as e:
        logger.error(f"❌ Community Stories initialization failed: {e}")
        return False

from community_stories.blockchain import *  # noqa: F401,F403

from community_stories.routes import *  # noqa: F401,F403

from community_stories.community_stories_service import *  # noqa: F401,F403
