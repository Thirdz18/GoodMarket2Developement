"""Compatibility facade for legacy community_stories package while migrating toward flat module layout."""

from community_stories.community_stories_service import community_stories_service
from blockchain import community_stories_blockchain
from app import init_community_stories

__all__ = ['community_stories_service', 'community_stories_blockchain', 'init_community_stories']
