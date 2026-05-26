"""Compatibility façade for legacy discourse_task package while migrating toward flat module layout."""

from discourse_task.discourse_task import discourse_task_service, init_discourse_task

__all__ = ['discourse_task_service', 'init_discourse_task']

from discourse_task.blockchain import *  # noqa: F401,F403

from discourse_task.discourse_task import *  # noqa: F401,F403
