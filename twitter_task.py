"""Compatibility facade for legacy twitter_task package while migrating toward flat module layout."""

from twitter_task.twitter_task import twitter_task_service, init_twitter_task
from blockchain import twitter_blockchain_service

__all__ = ['twitter_task_service', 'init_twitter_task', 'twitter_blockchain_service']
