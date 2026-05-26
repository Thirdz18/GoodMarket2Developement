"""Discourse Task module — re-exports for backward compatibility."""

from .discourse_task import discourse_task_service, init_discourse_task
from blockchain import discourse_blockchain_service

__all__ = ['discourse_task_service', 'init_discourse_task', 'discourse_blockchain_service']
