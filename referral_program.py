"""Compatibility façade for legacy referral_program package while migrating toward flat module layout."""
from flask import Blueprint

referral_bp = Blueprint('referral_program', __name__)

from referral_program.blockchain import *  # noqa: F401,F403

from referral_program.referral_service import *  # noqa: F401,F403
