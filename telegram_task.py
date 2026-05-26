"""Compatibility facade for legacy telegram_task package while migrating toward flat module layout."""

from telegram_task.telegram_task import telegram_task_service, init_telegram_task
from blockchain import telegram_blockchain_service

__all__ = ['telegram_task_service', 'init_telegram_task', 'telegram_blockchain_service']
