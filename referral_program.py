"""Compatibility facade for legacy referral_program package while migrating toward flat module layout."""

from referral_program import referral_bp
from blockchain import referral_blockchain_service
from referral_program.referral_service import referral_service, ReferralService, BASE_URL

__all__ = [
    'referral_bp',
    'referral_blockchain_service',
    'referral_service',
    'ReferralService',
    'BASE_URL',
]
