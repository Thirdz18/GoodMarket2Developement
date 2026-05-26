"""Backward-compatibility shim — imports from the new flat-file locations."""

from flask import Blueprint

referral_bp = Blueprint('referral_program', __name__)

from referral_service import referral_service, ReferralService, BASE_URL
from blockchain import referral_blockchain_service

__all__ = [
    'referral_bp',
    'referral_blockchain_service',
    'referral_service',
    'ReferralService',
    'BASE_URL',
]
