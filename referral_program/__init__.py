"""Referral Program module — re-exports for backward compatibility."""

from flask import Blueprint

referral_bp = Blueprint('referral_program', __name__)

from blockchain import referral_blockchain_service
from .referral_service import referral_service, ReferralService, BASE_URL

__all__ = [
    'referral_bp',
    'referral_blockchain_service',
    'referral_service',
    'ReferralService',
    'BASE_URL',
]
