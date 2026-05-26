"""Backward-compatibility shim — imports from the new flat-file locations."""

from community_stories_service import community_stories_service
from blockchain import community_stories_blockchain

__all__ = ['community_stories_service', 'community_stories_blockchain']
