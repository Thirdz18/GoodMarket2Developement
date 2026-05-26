"""Backward-compatibility shim — imports from the new flat-file locations."""

from learn_and_earn_service import (
    init_learn_and_earn,
    quiz_manager,
    LearnEarnQuizManager,
)
from blockchain import (
    learn_blockchain_service,
    disburse_rewards,
)
from learn_earn_stream_scheduler import (
    init_learn_earn_stream_scheduler,
    get_stream_scheduler,
    LearnEarnStreamScheduler,
)

__all__ = [
    'init_learn_and_earn',
    'init_learn_earn_stream_scheduler',
    'get_stream_scheduler',
    'LearnEarnStreamScheduler',
    'quiz_manager',
    'LearnEarnQuizManager',
    'learn_blockchain_service',
    'disburse_rewards',
]
