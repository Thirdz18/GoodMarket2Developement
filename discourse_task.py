"""Backward-compatibility shim — imports from the new flat-file locations."""

from discourse_task_service import discourse_task_service, init_discourse_task, DiscourseTaskService
from blockchain import discourse_blockchain_service

__all__ = ['discourse_task_service', 'init_discourse_task', 'DiscourseTaskService', 'discourse_blockchain_service']
