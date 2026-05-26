"""Compatibility façade for legacy twitter_task package while migrating toward flat module layout."""

from twitter_task.twitter_task import twitter_task_service, init_twitter_task

__all__ = ['twitter_task_service', 'init_twitter_task']

from twitter_task.blockchain import *  # noqa: F401,F403

from twitter_task.twitter_task import *  # noqa: F401,F403
