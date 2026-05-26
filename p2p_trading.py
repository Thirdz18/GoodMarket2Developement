"""Compatibility facade for legacy p2p_trading package while migrating toward flat module layout."""

from p2p_trading.contract import get_contract
from p2p_trading.escrow_service import escrow_service
from p2p_trading.indexer import get_indexer
from routes import p2p_bp
from app import init_p2p_trading

__all__ = [
    "p2p_bp",
    "init_p2p_trading",
    "escrow_service",
    "get_contract",
    "get_indexer",
]
