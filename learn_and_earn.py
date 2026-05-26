"""Compatibility façade for legacy learn_and_earn package while migrating toward flat module layout."""

from learn_and_earn.learn_and_earn import (
    init_learn_and_earn,
    quiz_manager,
    LearnEarnQuizManager
)
from learn_and_earn.blockchain import (
    learn_blockchain_service,
    disburse_rewards
)
from learn_and_earn.stream_scheduler import (
    init_learn_earn_stream_scheduler,
    get_stream_scheduler,
    LearnEarnStreamScheduler,
)

# Export the functions that main.py needs
def get_random_questions():
    """Get random questions for the quiz"""
    return quiz_manager.get_random_questions()

def calculate_score(answers):
    """Calculate score and rewards from quiz answers"""
    return quiz_manager.calculate_score_and_rewards(answers)

def check_user_eligibility(wallet_address):
    """Legacy function for backward compatibility"""
    return quiz_manager.check_user_eligibility(wallet_address)

__all__ = [
    'init_learn_and_earn',
    'init_learn_earn_stream_scheduler',
    'get_stream_scheduler',
    'LearnEarnStreamScheduler',
    'get_random_questions',
    'calculate_score',
    'check_user_eligibility',
    'quiz_manager',
    'LearnEarnQuizManager',
    'learn_blockchain_service',
    'disburse_rewards'
]

from learn_and_earn.blockchain import *  # noqa: F401,F403

from learn_and_earn.contract_service import *  # noqa: F401,F403

from learn_and_earn.sponsor_certificate import *  # noqa: F401,F403

from learn_and_earn.learn_and_earn import *  # noqa: F401,F403

from learn_and_earn.stream_scheduler import *  # noqa: F401,F403

from learn_and_earn.nft_service import *  # noqa: F401,F403
