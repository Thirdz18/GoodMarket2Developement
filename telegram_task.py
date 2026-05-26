"""Compatibility façade for legacy telegram_task package while migrating toward flat module layout."""

from telegram_task.telegram_task import telegram_task_service, init_telegram_task

__all__ = ['telegram_task_service', 'init_telegram_task']

from telegram_task.blockchain import *  # noqa: F401,F403

from telegram_task.telegram_task import *  # noqa: F401,F403
