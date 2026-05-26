"""Backward-compatibility shim — imports from the new flat-file locations."""

from telegram_task_service import telegram_task_service, init_telegram_task, TelegramTaskService
from blockchain import telegram_blockchain_service

__all__ = ['telegram_task_service', 'init_telegram_task', 'TelegramTaskService', 'telegram_blockchain_service']
