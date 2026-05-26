
import requests
from datetime import datetime, timedelta, timezone
import logging
import os
import threading

# --- Additional imports for consolidated module blockchain services ---
from eth_account import Account
from config import GOODDOLLAR_CONTRACT_ADDRESS as _CONFIG_GOODDOLLAR_ADDRESS
from config import LEARN_EARN_CONTRACT_ADDRESS as _CONFIG_LEARN_EARN_ADDRESS
import asyncio
import uuid
import time as _time_module
from decimal import Decimal, ROUND_DOWN
from web3 import Web3
from web3.exceptions import TimeExhausted

logger = logging.getLogger("blockchain")

CELO_CHAIN_ID = int(os.getenv("CHAIN_ID", "42220"))
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")

GOODDOLLAR_CONTRACTS = {
    "UBI_PROXY": os.getenv("UBI_PROXY_CONTRACT", "0x43d72Ff17701B2DA814620735C39C620Ce0ea4A1"),
    "UBI_IMPLEMENTATION": "",
    "GOODDOLLAR_TOKEN": os.getenv("GOODDOLLAR_TOKEN_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"),
    "IDENTITY": os.getenv("IDENTITY_CONTRACT", "0xC361A6E67822a0EDc17D899227dd9FC50BD62F42"),
}

CUSD_CONTRACT = os.getenv("CUSD_CONTRACT", "0x765DE816845861e75A25fCA122bb6898B8B1282a")
USDT_CONTRACT = os.getenv("USDT_CONTRACT", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")

IDENTITY_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}],
        "name": "isWhitelisted",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}],
        "name": "dateAuthenticated",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}],
        "name": "lastAuthenticated",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "authenticationPeriod",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

UBI_SCHEME_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "_member", "type": "address"}],
        "name": "checkEntitlement",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "claim",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

_identity_cache: dict = {}
_identity_cache_lock = threading.Lock()
IDENTITY_CACHE_TTL = 1800  # 30 minutes — identity whitelist rarely changes

# G$ USD price cache — refreshed every 30 minutes from CoinGecko
_gd_price_cache: dict = {"price": None, "expires_at": 0}
_gd_price_lock = threading.Lock()

def _get_gd_usd_price() -> float:
    """Return the current G$ price in USD. Uses env var if set, else fetches from CoinGecko.

    Timeout is intentionally short (2s) so this never dominates the wallet balance
    critical path. On timeout/error we fall back to the last cached value (or 0)
    and shorten the cache to retry on the next request.
    """
    import time
    env_price = float(os.getenv("GD_USD_PRICE", "0"))
    if env_price > 0:
        return env_price
    with _gd_price_lock:
        if _gd_price_cache["price"] is not None and _gd_price_cache["expires_at"] > time.time():
            return _gd_price_cache["price"]
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "gooddollar", "vs_currencies": "usd"},
                timeout=2
            )
            if resp.status_code == 200:
                price = resp.json().get("gooddollar", {}).get("usd", 0) or 0
                _gd_price_cache["price"] = float(price)
                _gd_price_cache["expires_at"] = time.time() + 1800
                return float(price)
        except Exception:
            pass
        # Fallback: keep any previously cached price so we don't show $0 on a
        # transient CoinGecko hiccup. Shorten TTL so we retry sooner.
        last_price = _gd_price_cache["price"] if _gd_price_cache["price"] is not None else 0.0
        _gd_price_cache["price"] = last_price
        _gd_price_cache["expires_at"] = time.time() + 300
        return last_price

# Shared Web3 instance — reuse TCP connection instead of creating one per request
_w3_singleton = None
_w3_lock = threading.Lock()

def _get_w3():
    global _w3_singleton
    with _w3_lock:
        if _w3_singleton is None or not _w3_singleton.is_connected():
            from web3 import Web3 as _W3
            _w3_singleton = _W3(_W3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 10}))
        return _w3_singleton

# Balance cache: {wallet_lower: {"gd": {...}, "celo": {...}, "expires_at": float}}
_balance_cache: dict = {}
_balance_cache_lock = threading.Lock()
BALANCE_CACHE_TTL = 120  # 2 minutes

# Transfer history cache: {wallet_lower: {"transfers": [...], "expires_at": float}}
_transfer_history_cache: dict = {}
_transfer_history_cache_lock = threading.Lock()
TRANSFER_HISTORY_CACHE_TTL = 300  # 5 minutes

UBI_EVENT_SIGNATURES = {
    "TRANSFER": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    "UBI_CLAIMED": "0x89ed24731df6b066e4c5186901fffdba18cd9a10f07494aff900bdee260d1304",
    "UBI_CALCULATED": "0x836fa39995340265746dfe9587d9fe5c5de35b7bce778afd9b124ce1cfeafdc4",
    "UBI_CYCLE_CALCULATED": "0x83e0d535b9e84324e0a25922406398d6ff5f96d0c686204ee490e16d7670566f",
}

CUTOFF_HOURS = 24

log = logging.getLogger("blockchain")

# Shared requests session for connection pooling (reuse TCP connections)
_rpc_session = requests.Session()
_rpc_session.headers.update({"Content-Type": "application/json"})
# Increase pool size so concurrent threads don't wait for a free connection
_http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=requests.adapters.Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST"],
    ),
)
_rpc_session.mount("https://", _http_adapter)
_rpc_session.mount("http://", _http_adapter)
_session_lock = threading.Lock()

# Simple in-memory UBI verification cache
# Format: {wallet_lower: {"result": {...}, "expires_at": float}}
_ubi_cache: dict = {}
_ubi_cache_lock = threading.Lock()
UBI_CACHE_TTL = 900  # 15 minutes — reduces repeated RPC calls on re-login

# Cache for latest block number (30 second TTL)
_block_cache = {"block": 0, "expires_at": 0.0}
_block_cache_lock = threading.Lock()

# Cache for block timestamps — block timestamps are immutable, cache for 6 hours
_block_ts_cache: dict = {}
_block_ts_cache_lock = threading.Lock()
BLOCK_TS_CACHE_TTL = 21600  # 6 hours


def _get_rpc_session() -> requests.Session:
    return _rpc_session


def _get_ubi_cached(wallet_address: str):
    """Return cached UBI result or None if expired/missing."""
    key = wallet_address.lower()
    with _ubi_cache_lock:
        entry = _ubi_cache.get(key)
        if entry and entry["expires_at"] > datetime.now().timestamp():
            logger.info(f"✅ UBI cache HIT for {wallet_address[:8]}...")
            return entry["result"]
    return None


def _set_ubi_cached(wallet_address: str, result: dict):
    """Store UBI result in cache."""
    key = wallet_address.lower()
    import time
    with _ubi_cache_lock:
        _ubi_cache[key] = {
            "result": result,
            "expires_at": time.time() + UBI_CACHE_TTL
        }
        # Keep cache small — remove oldest entries if over 500 wallets
        if len(_ubi_cache) > 500:
            oldest = min(_ubi_cache, key=lambda k: _ubi_cache[k]["expires_at"])
            del _ubi_cache[oldest]


def _topic_for_address(wallet: str) -> str:
    return "0x" + ("0" * 24) + wallet.lower().replace("0x", "")


def _format_timestamp(block_number: int) -> str:
    import time as _time
    # Block timestamps are immutable — check cache first to avoid redundant RPC calls
    with _block_ts_cache_lock:
        entry = _block_ts_cache.get(block_number)
        if entry and entry["expires_at"] > _time.time():
            return entry["formatted"]

    try:
        session = _get_rpc_session()
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [hex(block_number), False],
            "id": 1
        }
        response = session.post(CELO_RPC, json=payload, timeout=8)
        result = response.json()

        if "result" in result and result["result"]:
            timestamp = int(result["result"]["timestamp"], 16)
            block_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            now = datetime.now(timezone.utc)
            diff = now - block_time

            if diff.days > 0:
                relative = f"{diff.days}d ago"
            elif diff.seconds > 3600:
                hours = diff.seconds // 3600
                relative = f"{hours}h ago"
            else:
                minutes = diff.seconds // 60
                relative = f"{minutes}m ago"

            exact_time = block_time.strftime("%b %d %Y %H:%M:%S %p (+00:00 UTC)")
            formatted = f"{relative} | {exact_time}"

            with _block_ts_cache_lock:
                _block_ts_cache[block_number] = {
                    "formatted": formatted,
                    "expires_at": _time.time() + BLOCK_TS_CACHE_TTL
                }
                # Keep cache size bounded
                if len(_block_ts_cache) > 2000:
                    oldest = min(_block_ts_cache, key=lambda k: _block_ts_cache[k]["expires_at"])
                    del _block_ts_cache[oldest]

            return formatted
    except Exception as e:
        logger.error(f"Error formatting timestamp for block {block_number}: {e}")

    return f"Block #{block_number}"


def _get_latest_block_number() -> int:
    import time
    with _block_cache_lock:
        now = time.time()
        if _block_cache["block"] > 0 and _block_cache["expires_at"] > now:
            return _block_cache["block"]

    try:
        session = _get_rpc_session()
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }
        response = session.post(CELO_RPC, json=payload, timeout=8)
        result = response.json()
        block = int(result["result"], 16)

        import time
        with _block_cache_lock:
            _block_cache["block"] = block
            _block_cache["expires_at"] = time.time() + 30  # Cache for 30 seconds

        return block
    except Exception as e:
        logger.error(f"Error getting latest block: {e}")
        return 0


def _calculate_block_range(hours_back: int) -> tuple:
    blocks_per_hour = 720
    latest_block = _get_latest_block_number()
    from_block = latest_block - (hours_back * blocks_per_hour)

    logger.info(f"Block range: {from_block} to {latest_block} (last {hours_back} hours)")
    return hex(from_block), hex(latest_block)


def _batch_rpc_call(payloads: list) -> list:
    """Send multiple JSON-RPC calls in a single HTTP request (batch RPC)."""
    try:
        session = _get_rpc_session()
        response = session.post(CELO_RPC, json=payloads, timeout=20)
        results = response.json()
        if isinstance(results, list):
            return results
        # Single error object returned (e.g. block range too large)
        if isinstance(results, dict) and "error" in results:
            logger.error(f"Batch RPC returned error: {results['error']}")
        return []
    except Exception as e:
        logger.error(f"Batch RPC error: {e}")
        return []


def _get_logs_chunked(params_template: dict, from_block_int: int, to_block_int: int,
                      chunk_size: int = 1000) -> list:
    """
    Call eth_getLogs in chunks to stay within public RPC block-range limits.
    Searches from newest to oldest; stops early on the FIRST chunk that returns
    results (suitable for "has_recent_claim" style checks).
    Returns a flat list of log entries.
    """
    session = _get_rpc_session()
    all_logs = []
    chunk_id = 0

    current_to = to_block_int
    while current_to >= from_block_int:
        current_from = max(from_block_int, current_to - chunk_size + 1)
        chunk_id += 1

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{**params_template, "fromBlock": hex(current_from), "toBlock": hex(current_to)}],
            "id": chunk_id
        }

        try:
            response = session.post(CELO_RPC, json=payload, timeout=15)
            result = response.json()

            if isinstance(result, dict):
                if "error" in result:
                    logger.error(f"eth_getLogs chunk error (blocks {current_from}-{current_to}): {result['error']}")
                elif "result" in result:
                    logs = result["result"] or []
                    all_logs.extend(logs)
                    if logs:
                        # Found something — stop searching older blocks
                        logger.info(f"Found {len(logs)} logs in chunk {current_from}-{current_to}, stopping early")
                        break
        except Exception as e:
            logger.error(f"eth_getLogs chunk exception (blocks {current_from}-{current_to}): {e}")

        current_to = current_from - 1

    return all_logs


def _get_logs_full(params_template: dict, from_block_int: int, to_block_int: int,
                   chunk_size: int = 10000, batch_size: int = 10) -> list:
    """
    Like _get_logs_chunked but scans the ENTIRE block range without early-stopping.
    Sends multiple chunk requests as a single JSON-RPC batch (up to batch_size at once)
    to minimise round-trips.  Uses a larger default chunk_size for efficiency.
    """
    # Build the list of (from, to) block ranges
    chunks = []
    current_to = to_block_int
    while current_to >= from_block_int:
        current_from = max(from_block_int, current_to - chunk_size + 1)
        chunks.append((current_from, current_to))
        current_to = current_from - 1

    session  = _get_rpc_session()
    all_logs = []

    # Send chunks in batches to keep individual HTTP requests manageable
    for batch_start in range(0, len(chunks), batch_size):
        sub_chunks = chunks[batch_start: batch_start + batch_size]
        batch_payload = [
            {
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{**params_template,
                            "fromBlock": hex(cf),
                            "toBlock":   hex(ct)}],
                "id": batch_start + idx,
            }
            for idx, (cf, ct) in enumerate(sub_chunks)
        ]
        try:
            response = session.post(CELO_RPC, json=batch_payload, timeout=20)
            results  = response.json()
            # Response may be a list (batch) or a single dict (error)
            if isinstance(results, list):
                for item in results:
                    logs = item.get("result") or []
                    all_logs.extend(logs)
            elif isinstance(results, dict) and "result" in results:
                all_logs.extend(results["result"] or [])
        except Exception as exc:
            logger.error(f"_get_logs_full batch error (chunks {batch_start}–{batch_start+len(sub_chunks)}): {exc}")

    return all_logs


def has_recent_ubi_claim(wallet_address: str) -> dict:
    # Check cache first — avoids ALL blockchain calls
    cached_result = _get_ubi_cached(wallet_address)
    if cached_result is not None:
        return cached_result

    try:
        # Search last 7 days (extended for better detection, CUTOFF_HOURS=24 defines the requirement)
        search_hours = max(CUTOFF_HOURS, 24 * 7)
        from_block, to_block = _calculate_block_range(search_hours)

        ubi_proxy_address = GOODDOLLAR_CONTRACTS["UBI_PROXY"]
        gooddollar_token = GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"]

        all_activities = []

        # --- Check for G$ transfers FROM UBI Proxy TO user ---
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": gooddollar_token,
                "topics": [
                    UBI_EVENT_SIGNATURES["TRANSFER"],
                    _topic_for_address(ubi_proxy_address),
                    _topic_for_address(wallet_address)
                ]
            }],
            "id": 1
        }

        try:
            session = _get_rpc_session()
            response = session.post(CELO_RPC, json=payload, timeout=15)
            result = response.json()

            if "error" not in result:
                logs = result.get("result", [])
                for log_entry in logs:
                    block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                    tx_hash = log_entry.get("transactionHash", "Unknown")
                    timestamp_info = _format_timestamp(block_num)
                    amount_hex = log_entry.get("data", "0x0")
                    try:
                        amount_g = int(amount_hex, 16) / (10 ** 18)
                    except Exception:
                        amount_g = 0
                    all_activities.append({
                        "contract": "UBI Proxy",
                        "contract_address": ubi_proxy_address,
                        "block": block_num,
                        "tx_hash": tx_hash,
                        "timestamp": timestamp_info,
                        "method": "UBI claim",
                        "status": "success",
                        "amount": f"{amount_g:.6f} G$",
                        "activity_type": "ubi_claim"
                    })
        except Exception as e:
            logger.error(f"Error checking UBI Proxy transfers: {e}")

        # --- Check for UBI-specific events on UBI Proxy contract ---
        for event_name, event_signature in UBI_EVENT_SIGNATURES.items():
            if event_name == "TRANSFER":
                continue

            topics = [event_signature]

            if event_name in ["UBI_CLAIMED", "CLAIM", "REWARD_CLAIMED", "UBI_DISTRIBUTED", "DAILY_UBI"]:
                topics.append(_topic_for_address(wallet_address))
            else:
                topics.append(None)

            payload = {
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": ubi_proxy_address,
                    "topics": topics
                }],
                "id": 1
            }

            try:
                session = _get_rpc_session()
                response = session.post(CELO_RPC, json=payload, timeout=15)
                result = response.json()

                if "error" not in result:
                    logs = result.get("result", [])

                    if event_name not in ["UBI_CLAIMED", "CLAIM", "REWARD_CLAIMED", "UBI_DISTRIBUTED", "DAILY_UBI"]:
                        wallet_topic = _topic_for_address(wallet_address)
                        logs = [log for log in logs if wallet_topic in log.get("topics", [])]

                    for log_entry in logs:
                        block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                        tx_hash = log_entry.get("transactionHash", "Unknown")
                        timestamp_info = _format_timestamp(block_num)
                        amount_str = "Event logged"
                        try:
                            data = log_entry.get("data", "0x")
                            if data and data != "0x":
                                amount_g = int(data, 16) / (10 ** 18)
                                amount_str = f"{amount_g:.6f} G$"
                        except Exception:
                            try:
                                log_topics = log_entry.get("topics", [])
                                if len(log_topics) > 2:
                                    amount_g = int(log_topics[2], 16) / (10 ** 18)
                                    amount_str = f"{amount_g:.6f} G$"
                            except Exception:
                                pass

                        all_activities.append({
                            "contract": "UBI Proxy",
                            "contract_address": ubi_proxy_address,
                            "block": block_num,
                            "tx_hash": tx_hash,
                            "timestamp": timestamp_info,
                            "method": event_name.lower().replace("_", " "),
                            "status": "success",
                            "amount": amount_str,
                            "activity_type": "ubi_event"
                        })
            except Exception as e:
                logger.error(f"Error checking {event_name}: {e}")

        if len(all_activities) > 0:
            all_activities.sort(key=lambda x: x["block"], reverse=True)
            latest_activity = all_activities[0]

            claims = [a for a in all_activities if a["activity_type"] == "ubi_claim"]
            events = [a for a in all_activities if a["activity_type"] == "ubi_event"]

            success_message = f"✅ UBI VERIFICATION SUCCESS!\n\n"
            success_message += f"🎯 Found {len(all_activities)} UBI activities from UBI Proxy contract\n"
            success_message += f"   💰 UBI Claims: {len(claims)}\n"
            success_message += f"   📋 Events: {len(events)}\n\n"
            success_message += f"🕐 Most Recent Activity:\n"
            success_message += f"   Contract: {latest_activity['contract']}\n"
            success_message += f"   Type: {latest_activity['method']}\n"
            success_message += f"   Amount: {latest_activity['amount']}\n"
            success_message += f"   Block: #{latest_activity['block']}\n"
            success_message += f"   Time: {latest_activity['timestamp']}\n"
            success_message += f"   Tx: {latest_activity['tx_hash'][:16]}...\n"

            if len(all_activities) > 1:
                success_message += f"\n📊 All UBI Activities (last 24 hours):\n"
                for i, activity in enumerate(all_activities[:5], 1):
                    success_message += f"   {i}. {activity['amount']} ({activity['method']}) - {activity['timestamp']}\n"
                if len(all_activities) > 5:
                    success_message += f"   ... and {len(all_activities) - 5} more activities\n"

            result = {
                "status": "success",
                "message": success_message,
                "activities": all_activities,
                "summary": {
                    "total_activities": len(all_activities),
                    "claims": len(claims),
                    "events": len(events),
                    "contracts_involved": 1,
                    "latest_activity": latest_activity
                }
            }

            _set_ubi_cached(wallet_address, result)
            return result

        else:
            result = {
                "status": "error",
                "message": "You need to claim G$ once every 24 hours to access GoodMarket.\n\nClaim G$ using:\n• MiniPay app (built into Opera Mini)\n• goodwallet.xyz\n• gooddapp.org"
            }
            import time
            with _ubi_cache_lock:
                _ubi_cache[wallet_address.lower()] = {
                    "result": result,
                    "expires_at": time.time() + 120
                }
            return result

    except Exception as e:
        logger.error(f"Exception in UBI verification: {e}")
        return {"status": "error", "message": f"⚠️ UBI verification failed: {e}"}


_GD_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
]


def get_gooddollar_balance(wallet_address: str, include_price: bool = True) -> dict:
    """Fetch on-chain G$ balance for a wallet, with optional USD price enrichment.

    Set ``include_price=False`` to skip the (potentially slow) CoinGecko price
    fetch on the critical path. Callers can fetch the price separately in
    parallel via ``_get_gd_usd_price()`` and merge the result with
    ``enrich_gd_balance_with_price()``.
    """
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("gd") and entry["expires_at"] > time.time():
            logger.debug(f"💰 GD balance cache HIT for {wallet_address[:8]}...")
            cached = entry["gd"]
            cached_has_price = cached.get("gd_usd_price") is not None
            # Serve cached entry unless the caller needs the embedded price
            # but the cached entry was populated without one (e.g. by
            # ``wallet_balances`` which fetches the price separately).
            if (not include_price) or cached_has_price:
                return cached
    try:
        from web3 import Web3
        w3 = _get_w3()
        gooddollar_token = GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(gooddollar_token),
            abi=_GD_ERC20_ABI
        )
        wallet_checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = contract.functions.balanceOf(wallet_checksum).call()
        balance_g = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_g),
            "balance_formatted": f"{balance_g:,.6f} G$",
            "wallet": wallet_address,
            "contract": gooddollar_token,
        }
        if include_price:
            gd_usd_price = _get_gd_usd_price()
            usd_value = balance_g * gd_usd_price if gd_usd_price > 0 else None
            result["gd_usd_price"] = gd_usd_price
            result["usd_value"] = usd_value if usd_value is not None else 0
            if usd_value is not None:
                result["usd_formatted"] = f"≈ ${usd_value:.4f} USD"
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "gd": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"GD balance check error: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error loading balance"}


def enrich_gd_balance_with_price(gd_result: dict, gd_usd_price: float) -> dict:
    """Return a copy of a GD balance dict with USD price/value fields populated.

    Designed to be paired with ``get_gooddollar_balance(include_price=False)``
    so the price fetch can run in parallel with the balance fetch instead of
    serialised after it.
    """
    if not gd_result or not gd_result.get("success"):
        return gd_result
    enriched = dict(gd_result)
    balance_g = float(enriched.get("balance") or 0)
    usd_value = balance_g * gd_usd_price if gd_usd_price > 0 else None
    enriched["gd_usd_price"] = gd_usd_price
    enriched["usd_value"] = usd_value if usd_value is not None else 0
    if usd_value is not None:
        enriched["usd_formatted"] = f"≈ ${usd_value:.4f} USD"
    return enriched


def is_identity_verified(wallet_address: str) -> dict:
    """Check if a wallet is face-verified on the GoodDollar Identity contract."""
    try:
        from web3 import Web3 as _Web3
        checksum = _Web3.to_checksum_address(wallet_address)
        key = checksum.lower()

        with _identity_cache_lock:
            entry = _identity_cache.get(key)
            if entry and entry["expires_at"] > datetime.now().timestamp():
                logger.debug(f"🪪 Identity cache HIT for {wallet_address[:8]}...")
                return entry["result"]

        w3 = _get_w3()
        contract = w3.eth.contract(
            address=_Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["IDENTITY"]),
            abi=IDENTITY_ABI
        )
        verified = contract.functions.isWhitelisted(checksum).call()
        result = {"verified": verified}

        with _identity_cache_lock:
            _identity_cache[key] = {
                "result": result,
                "expires_at": datetime.now().timestamp() + IDENTITY_CACHE_TTL
            }

        status = "✅ verified" if verified else "❌ not verified"
        logger.info(f"🪪 Identity check for {wallet_address[:8]}...: {status}")
        return result

    except Exception as e:
        logger.error(f"Identity check error for {wallet_address}: {e}")
        return {"verified": False, "error": str(e)}


_fv_expiry_cache: dict = {}
_fv_expiry_cache_lock = threading.Lock()
FV_EXPIRY_CACHE_TTL = 300  # 5 minutes — expiry rarely changes but we want fresh enough countdowns


def get_identity_expiry(wallet_address: str) -> dict:
    """Return face-verification expiry data for a wallet.

    Reads `isWhitelisted`, `dateAuthenticated`, and `authenticationPeriod` from
    the GoodDollar Identity contract. Each call is wrapped independently because
    `dateAuthenticated` / `authenticationPeriod` can revert on some identity
    contract deployments — in that case we still return whitelist status with
    `expiry_available=False` so the UI can show "Verified" without a countdown.
    """
    import time
    from web3 import Web3 as _Web3

    key = (wallet_address or "").lower()

    def _empty(error: str | None = None) -> dict:
        return {
            "success": False,
            "error": error,
            "wallet": key,
            "verified": False,
            "expired": False,
            "ever_verified": False,
            "is_whitelisted": False,
            "expiry_available": False,
            "date_authenticated": 0,
            "date_authenticated_iso": None,
            "authentication_period_days": 0,
            "expires_at": 0,
            "expires_at_iso": None,
            "seconds_remaining": 0,
            "days_remaining": 0,
        }

    try:
        checksum = _Web3.to_checksum_address(wallet_address)
    except Exception as e:
        return _empty(f"invalid_address: {e}")

    with _fv_expiry_cache_lock:
        entry = _fv_expiry_cache.get(key)
        if entry and entry["expires_cache_at"] > time.time():
            return entry["result"]

    try:
        w3 = _get_w3()
        contract = w3.eth.contract(
            address=_Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["IDENTITY"]),
            abi=IDENTITY_ABI
        )
    except Exception as e:
        logger.error(f"FV expiry contract init failed for {wallet_address}: {e}")
        return _empty(str(e))

    # 1) isWhitelisted — required; if this fails, we genuinely can't answer.
    try:
        is_whitelisted = bool(contract.functions.isWhitelisted(checksum).call())
    except Exception as e:
        logger.error(f"FV expiry isWhitelisted failed for {wallet_address}: {e}")
        return _empty(str(e))

    # 2) lastAuthenticated (primary) falling back to dateAuthenticated (older
    #    deployments). On the current Celo Identity contract,
    #    dateAuthenticated() reverts while lastAuthenticated() returns the real
    #    unix timestamp.
    date_auth = 0
    try:
        date_auth = int(contract.functions.lastAuthenticated(checksum).call())
    except Exception as primary_err:
        logger.debug(f"FV expiry lastAuthenticated unavailable for {wallet_address[:8]}…: {primary_err}")
        try:
            date_auth = int(contract.functions.dateAuthenticated(checksum).call())
        except Exception as fv_err:
            logger.debug(f"FV expiry dateAuthenticated also unavailable: {fv_err}")

    # 3) authenticationPeriod — also known to revert on current Celo deployment.
    auth_period_days = 0
    try:
        auth_period_days = int(contract.functions.authenticationPeriod().call())
    except Exception as fv_err:
        logger.debug(f"FV expiry authenticationPeriod unavailable: {fv_err}")

    now = int(time.time())
    ever_verified = date_auth > 0
    expiry_available = ever_verified and auth_period_days > 0
    expires_at = (date_auth + auth_period_days * 86400) if expiry_available else 0
    seconds_remaining = max(0, expires_at - now) if expires_at > 0 else 0
    days_remaining = seconds_remaining // 86400 if seconds_remaining > 0 else 0
    expired = expiry_available and now > expires_at

    # Without reliable expiry data we trust on-chain whitelist status.
    if expiry_available:
        effective_verified = is_whitelisted and not expired
    else:
        effective_verified = is_whitelisted

    result = {
        "success": True,
        "wallet": key,
        "is_whitelisted": is_whitelisted,
        "ever_verified": ever_verified,
        "verified": effective_verified,
        "expired": bool(expired),
        "expiry_available": expiry_available,
        "date_authenticated": date_auth,
        "date_authenticated_iso": (
            datetime.fromtimestamp(date_auth, tz=timezone.utc).isoformat()
            if ever_verified else None
        ),
        "authentication_period_days": auth_period_days,
        "expires_at": expires_at,
        "expires_at_iso": (
            datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
            if expires_at > 0 else None
        ),
        "seconds_remaining": int(seconds_remaining),
        "days_remaining": int(days_remaining),
    }

    with _fv_expiry_cache_lock:
        _fv_expiry_cache[key] = {
            "result": result,
            "expires_cache_at": time.time() + FV_EXPIRY_CACHE_TTL,
        }

    logger.info(
        f"🪪 FV expiry for {wallet_address[:8]}…: "
        f"whitelisted={is_whitelisted}, expiry_available={expiry_available}, "
        f"expired={expired}, days_remaining={days_remaining}"
    )
    return result


def invalidate_fv_expiry_cache(wallet_address: str | None = None) -> None:
    """Drop cached FV expiry data (for a wallet, or all if None)."""
    with _fv_expiry_cache_lock:
        if wallet_address:
            _fv_expiry_cache.pop(wallet_address.lower(), None)
        else:
            _fv_expiry_cache.clear()


_entitlement_cache: dict = {}
_entitlement_cache_lock = threading.Lock()
ENTITLEMENT_CACHE_TTL = 180  # 3 minutes — short enough to reflect claim/status changes

def get_ubi_claim_calldata() -> str:
    """Return encoded calldata for UBIScheme claim()."""
    return "0x4e71d92d"


def get_ubi_entitlement(wallet_address: str) -> dict:
    """Check how much G$ the wallet can claim right now from the UBIScheme contract."""
    import time
    key = wallet_address.lower()
    with _entitlement_cache_lock:
        entry = _entitlement_cache.get(key)
        if entry and entry["expires_at"] > time.time():
            logger.debug(f"⚡ UBI entitlement cache HIT for {wallet_address[:8]}...")
            return entry["result"]

    try:
        from web3 import Web3
        w3 = _get_w3()
        wallet_checksum = Web3.to_checksum_address(wallet_address)

        identity_contract = w3.eth.contract(
            address=Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["IDENTITY"]),
            abi=IDENTITY_ABI
        )
        is_verified = identity_contract.functions.isWhitelisted(wallet_checksum).call()

        # Compute entitlement even for unverified users so the UI can show the
        # pending claim amount before Face Verification is completed.
        entitlement_wei = 0
        entitlement_g = 0.0
        try:
            ubi_contract = w3.eth.contract(
                address=Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["UBI_PROXY"]),
                abi=UBI_SCHEME_ABI
            )
            entitlement_wei = ubi_contract.functions.checkEntitlement(wallet_checksum).call()
            entitlement_g = entitlement_wei / (10 ** 18)
        except Exception as entitlement_err:
            logger.warning(
                f"⚠️ Could not fetch entitlement for {wallet_address[:8]}... before FV check: {entitlement_err}"
            )

        if not is_verified:
            result = {
                "success": True,
                "wallet": key,
                "is_verified": False,
                "can_claim": False,
                "entitlement": float(entitlement_g),
                "entitlement_formatted": f"{entitlement_g:.2f}",
                "reason": "not_verified"
            }
            with _entitlement_cache_lock:
                _entitlement_cache[key] = {"result": result, "expires_at": time.time() + ENTITLEMENT_CACHE_TTL}
            return result

        # Whitelisted — check if Face Verification has lapsed (still in whitelist but auth expired).
        # On the current Celo Identity contract `dateAuthenticated` reverts, so we try
        # `lastAuthenticated` first (the working function) and fall back to `dateAuthenticated`
        # for older deployments. `authenticationPeriod` is also wrapped separately.
        fv_lapsed = False
        date_auth = 0
        try:
            date_auth = identity_contract.functions.lastAuthenticated(wallet_checksum).call()
        except Exception:
            try:
                date_auth = identity_contract.functions.dateAuthenticated(wallet_checksum).call()
            except Exception as fv_err:
                logger.debug(f"FV lapse: no auth-date fn available for {wallet_address[:8]}…: {fv_err}")

        auth_period = 0
        try:
            auth_period = identity_contract.functions.authenticationPeriod().call()
        except Exception as ap_err:
            logger.debug(f"FV lapse: authenticationPeriod unavailable: {ap_err}")

        if date_auth > 0 and auth_period > 0:
            auth_expires_at = date_auth + auth_period * 86400
            if time.time() > auth_expires_at:
                fv_lapsed = True

        if fv_lapsed:
            result = {
                "success": True,
                "wallet": key,
                "is_verified": False,
                "can_claim": False,
                "entitlement": float(entitlement_g),
                "entitlement_formatted": f"{entitlement_g:.2f}",
                "reason": "re_verification_needed"
            }
            with _entitlement_cache_lock:
                _entitlement_cache[key] = {"result": result, "expires_at": time.time() + ENTITLEMENT_CACHE_TTL}
            return result

        result = {
            "success": True,
            "wallet": key,
            "is_verified": True,
            "entitlement": float(entitlement_g),
            "entitlement_formatted": f"{entitlement_g:.2f}",
            "can_claim": entitlement_g > 0,
            "claim_calldata": get_ubi_claim_calldata(),
            "ubi_contract": GOODDOLLAR_CONTRACTS["UBI_PROXY"]
        }
        with _entitlement_cache_lock:
            _entitlement_cache[key] = {"result": result, "expires_at": time.time() + ENTITLEMENT_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"UBI entitlement check error for {wallet_address[:8]}...: {e}")
        return {"success": False, "error": str(e), "entitlement": 0, "can_claim": False, "is_verified": None}


def invalidate_entitlement_cache(wallet_address: str):
    """Call this after a successful UBI claim to force fresh data on next check."""
    key = wallet_address.lower()
    with _entitlement_cache_lock:
        _entitlement_cache.pop(key, None)


def get_celo_balance(wallet_address: str) -> dict:
    """Get native CELO balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("celo") and entry["expires_at"] > time.time():
            logger.debug(f"⚡ CELO balance cache HIT for {wallet_address[:8]}...")
            return entry["celo"]
    try:
        from web3 import Web3
        w3 = _get_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(checksum)
        balance_celo = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_celo),
            "balance_wei": str(balance_wei),
            "balance_formatted": f"{balance_celo:.6f} CELO",
            "wallet": wallet_address,
        }
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "celo": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"CELO balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def get_cusd_balance(wallet_address: str) -> dict:
    """Get cUSD (Celo Dollar) ERC-20 balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("cusd") and entry["expires_at"] > time.time():
            logger.debug(f"💵 cUSD balance cache HIT for {wallet_address[:8]}...")
            return entry["cusd"]
    try:
        from web3 import Web3
        w3 = _get_w3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CUSD_CONTRACT),
            abi=_GD_ERC20_ABI
        )
        wallet_checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = contract.functions.balanceOf(wallet_checksum).call()
        balance_cusd = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_cusd),
            "balance_formatted": f"{balance_cusd:.6f} cUSD",
            "wallet": wallet_address,
            "contract": CUSD_CONTRACT,
        }
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "cusd": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"cUSD balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def get_usdt_balance(wallet_address: str) -> dict:
    """Get USDT (Tether on Celo) ERC-20 balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("usdt") and entry["expires_at"] > time.time():
            logger.debug(f"💵 USDT balance cache HIT for {wallet_address[:8]}...")
            return entry["usdt"]
    try:
        from web3 import Web3
        w3 = _get_w3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDT_CONTRACT),
            abi=_GD_ERC20_ABI
        )
        wallet_checksum = Web3.to_checksum_address(wallet_address)
        balance_raw = contract.functions.balanceOf(wallet_checksum).call()
        balance_usdt = balance_raw / (10 ** 6)
        result = {
            "success": True,
            "balance": float(balance_usdt),
            "balance_formatted": f"{balance_usdt:.6f} USDT",
            "wallet": wallet_address,
            "contract": USDT_CONTRACT,
        }
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "usdt": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"USDT balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def prepare_usdt_transfer_data(to_address: str, amount_usdt: float) -> dict:
    """
    Return the ABI-encoded calldata for a USDT ERC-20 transfer(address,uint256) call.
    USDT on Celo uses 6 decimals.
    """
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        token_addr = Web3.to_checksum_address(USDT_CONTRACT)
        to_checksum = Web3.to_checksum_address(to_address)
        amount_raw = int(amount_usdt * (10 ** 6))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_raw])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token_addr,
            "data": data,
            "value": "0x0",
            "chain_id": CELO_CHAIN_ID,
            "token": "USDT",
            "recipient": to_checksum,
            "amount": amount_usdt,
        }
    except Exception as e:
        logger.error(f"prepare_usdt_transfer_data error: {e}")
        return {"success": False, "error": str(e)}


def prepare_cusd_transfer_data(to_address: str, amount_cusd: float) -> dict:
    """
    Return the ABI-encoded calldata for a cUSD ERC-20 transfer(address,uint256) call.
    """
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        token_addr = Web3.to_checksum_address(CUSD_CONTRACT)
        to_checksum = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_cusd * (10 ** 18))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_wei])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token_addr,
            "data": data,
            "value": "0x0",
            "chain_id": CELO_CHAIN_ID,
            "token": "cUSD",
            "recipient": to_checksum,
            "amount": amount_cusd,
        }
    except Exception as e:
        logger.error(f"prepare_cusd_transfer_data error: {e}")
        return {"success": False, "error": str(e)}


def get_wallet_transfer_history(wallet_address: str, limit: int = 30) -> list:
    """
    Get G$ (ERC-20) transfer events involving this wallet (both sent and received).
    Returns a list of transfer dicts sorted newest-first. Cached for 5 minutes.
    """
    import time
    key = wallet_address.lower()
    with _transfer_history_cache_lock:
        entry = _transfer_history_cache.get(key)
        if entry and entry["expires_at"] > time.time():
            logger.debug(f"📜 Transfer history cache HIT for {wallet_address[:8]}...")
            return entry["transfers"][:limit]
    try:
        gooddollar_token = GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"]
        search_hours = 24 * 7  # 7 days instead of 30 — much faster
        from_block_hex, to_block_hex = _calculate_block_range(search_hours)
        from_block_int = int(from_block_hex, 16)
        to_block_int = int(to_block_hex, 16)

        wallet_topic = _topic_for_address(wallet_address)
        TRANSFER_SIG = UBI_EVENT_SIGNATURES["TRANSFER"]
        all_logs = []

        params_sent = {
            "address": gooddollar_token,
            "topics": [TRANSFER_SIG, wallet_topic, None],
        }
        params_received = {
            "address": gooddollar_token,
            "topics": [TRANSFER_SIG, None, wallet_topic],
        }

        for params in [params_sent, params_received]:
            logs = _get_logs_chunked(params, from_block_int, to_block_int, chunk_size=2000)
            all_logs.extend(logs)

        transfers = []
        wallet_lower = wallet_address.lower()

        for log_entry in all_logs:
            topics = log_entry.get("topics", [])
            if len(topics) < 3:
                continue
            from_addr = "0x" + topics[1][-40:]
            to_addr = "0x" + topics[2][-40:]
            direction = "sent" if from_addr.lower() == wallet_lower else "received"
            block_num = int(log_entry.get("blockNumber", "0x0"), 16)
            tx_hash = log_entry.get("transactionHash", "")
            try:
                amount_g = int(log_entry.get("data", "0x0"), 16) / (10 ** 18)
            except Exception:
                amount_g = 0
            transfers.append({
                "token": "G$",
                "direction": direction,
                "from": from_addr,
                "to": to_addr,
                "amount": float(amount_g),
                "amount_formatted": f"{amount_g:.4f} G$",
                "block": block_num,
                "tx_hash": tx_hash,
                "timestamp": _format_timestamp(block_num),
                "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash}",
            })

        transfers.sort(key=lambda x: x["block"], reverse=True)
        import time
        with _transfer_history_cache_lock:
            _transfer_history_cache[key] = {
                "transfers": transfers,
                "expires_at": time.time() + TRANSFER_HISTORY_CACHE_TTL,
            }
        return transfers[:limit]

    except Exception as e:
        logger.error(f"Transfer history error for {wallet_address}: {e}")
        return []


# Known contract addresses used for transaction classification
_UNISWAP_ROUTER_CELO = "0x5615cdab10dc425a742d643d949a7f474c01abc4"

# Separate cache for comprehensive multi-token tx history
_tx_history_cache: dict = {}
_tx_history_cache_lock = threading.Lock()
TX_HISTORY_CACHE_TTL = 300  # 5 minutes


def get_comprehensive_tx_history(wallet_address: str, limit: int = 50, force: bool = False) -> list:
    """
    Return classified transaction history across G$, cUSD, and USDT for the last 14 days.

    Each item has:
      tx_type  – 'claim' | 'swap' | 'savings_deposit' | 'savings_withdraw' |
                 'transfer_sent' | 'transfer_received'
      label    – human-readable description
      token    – 'G$' | 'cUSD' | 'USDT'

    Key improvements over the old single-token version:
    • Fetches G$, cUSD, USDT ERC-20 transfer events (all sends & receives)
    • Uses _get_logs_full so the ENTIRE date window is scanned, not just the
      first block-chunk that happens to have activity
    • Swap detection: batch-fetches transaction details in a single round-trip
      and checks tx.to == Uniswap Router (pools emit transfers, not the router)
    • Separate 5-minute cache to avoid re-scanning on every tab switch
    """
    import os as _os, time as _time

    cache_key = wallet_address.lower()
    if not force:
        with _tx_history_cache_lock:
            entry = _tx_history_cache.get(cache_key)
            if entry and entry["expires_at"] > _time.time():
                logger.debug(f"[tx-history] cache HIT for {wallet_address[:8]}…")
                return entry["txs"][:limit]
    else:
        logger.info(f"[tx-history] force refresh for {wallet_address[:8]}…")

    savings_addr = _os.getenv("SAVINGS_CONTRACT_ADDRESS", "").lower()
    ubi_proxy    = GOODDOLLAR_CONTRACTS["UBI_PROXY"].lower()

    TOKENS = [
        {"symbol": "G$",   "address": GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"], "decimals": 18},
        {"symbol": "cUSD", "address": CUSD_CONTRACT,  "decimals": 18},
        {"symbol": "USDT", "address": USDT_CONTRACT,  "decimals": 6},
    ]
    TRANSFER_SIG = UBI_EVENT_SIGNATURES["TRANSFER"]

    # 14-day window; 720 blocks/hour on Celo
    search_hours   = 24 * 14
    from_block_hex, to_block_hex = _calculate_block_range(search_hours)
    from_block_int = int(from_block_hex, 16)
    to_block_int   = int(to_block_hex,   16)

    wallet_topic = _topic_for_address(wallet_address)
    wallet_lower = wallet_address.lower()

    now_ts = _time.time()
    raw_transfers = []

    for token in TOKENS:
        # Fetch sent events (wallet is the `from`)
        sent_params = {
            "address": token["address"],
            "topics": [TRANSFER_SIG, wallet_topic, None],
        }
        # Fetch received events (wallet is the `to`)
        recv_params = {
            "address": token["address"],
            "topics": [TRANSFER_SIG, None, wallet_topic],
        }

        for params in [sent_params, recv_params]:
            logs = _get_logs_full(params, from_block_int, to_block_int, chunk_size=10000)
            for log_entry in logs:
                tlist = log_entry.get("topics", [])
                if len(tlist) < 3:
                    continue
                from_addr = "0x" + tlist[1][-40:]
                to_addr   = "0x" + tlist[2][-40:]
                block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                tx_hash   = log_entry.get("transactionHash", "")
                try:
                    amount = int(log_entry.get("data", "0x0"), 16) / (10 ** token["decimals"])
                except Exception:
                    amount = 0

                direction = "sent" if from_addr.lower() == wallet_lower else "received"
                # Format nicely: 2 decimals for cUSD/USDT, 4 for G$
                decimals_display = 2 if token["symbol"] in ("cUSD", "USDT") else 4
                # Approximate Unix timestamp from block number (Celo ~5 sec/block)
                approx_ts = now_ts - (to_block_int - block_num) * 5
                raw_transfers.append({
                    "network":          "celo",
                    "token":            token["symbol"],
                    "direction":        direction,
                    "from":             from_addr,
                    "to":               to_addr,
                    "amount":           float(amount),
                    "amount_formatted": f"{amount:.{decimals_display}f} {token['symbol']}",
                    "block":            block_num,
                    "tx_hash":          tx_hash,
                    "timestamp":        _format_timestamp(block_num),
                    "explorer_url":     f"https://celoscan.io/tx/{tx_hash}",
                    "_sort_ts":         approx_ts,
                })

    # Deduplicate — same event can appear in both sent and received queries
    seen: set = set()
    unique_transfers = []
    for tx in raw_transfers:
        key = (tx["tx_hash"], tx["token"], tx["from"].lower(), tx["to"].lower())
        if key not in seen:
            seen.add(key)
            unique_transfers.append(tx)

    # Sort newest-first by approximate Unix timestamp
    unique_transfers.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    # Trim before expensive swap-detection batch call (Celo only)
    unique_transfers = unique_transfers[:min(limit * 3, 150)]

    # ── Batch-fetch tx details to detect Uniswap Router calls (swaps) ────────
    # Only Celo transactions need this check; XDC G$ txs are pre-classified.
    celo_hashes = list({tx["tx_hash"] for tx in unique_transfers
                        if tx.get("tx_hash") and tx.get("network") == "celo"})
    swap_hashes: set = set()

    if celo_hashes:
        batch_payload = [
            {"jsonrpc": "2.0", "method": "eth_getTransactionByHash",
             "params": [h], "id": idx}
            for idx, h in enumerate(celo_hashes)
        ]
        try:
            session = _get_rpc_session()
            resp = session.post(CELO_RPC, json=batch_payload, timeout=25)
            batch_results = resp.json()
            if isinstance(batch_results, list):
                for item in batch_results:
                    tx_data = item.get("result") or {}
                    if (tx_data.get("to") or "").lower() == _UNISWAP_ROUTER_CELO:
                        h = (tx_data.get("hash") or "").lower()
                        if h:
                            swap_hashes.add(h)
        except Exception as exc:
            logger.warning(f"[tx-history] batch tx-hash fetch failed: {exc}")
    # ─────────────────────────────────────────────────────────────────────────

    # Classify each Celo transfer; XDC transfers already have tx_type set
    celo_classified = []
    for tx in unique_transfers:
        if tx.get("network") == "xdc":
            # Already classified by get_xdc_gd_transfer_history; just keep it
            celo_classified.append(tx)
            continue

        from_addr     = (tx.get("from")    or "").lower()
        to_addr       = (tx.get("to")      or "").lower()
        tx_hash_lower = (tx.get("tx_hash") or "").lower()
        token         = tx.get("token", "G$")

        if tx_hash_lower in swap_hashes:
            tx_type = "swap"
            label   = f"Token Swap ({token})"
        elif from_addr == ubi_proxy:
            tx_type = "claim"
            label   = "G$ UBI Claim"
        elif savings_addr and to_addr == savings_addr:
            tx_type = "savings_deposit"
            label   = "Savings Deposit"
        elif savings_addr and from_addr == savings_addr:
            tx_type = "savings_withdraw"
            label   = "Savings Withdrawal"
        elif tx.get("direction") == "sent":
            tx_type = "transfer_sent"
            label   = f"Sent {token}"
        else:
            tx_type = "transfer_received"
            label   = f"Received {token}"

        celo_classified.append({**tx, "tx_type": tx_type, "label": label})

    # ── Merge in XDC G$ transfer history ─────────────────────────────────────
    try:
        xdc_txs = get_xdc_gd_transfer_history(wallet_address, limit=50)
        # _sort_ts is already set as approx Unix ts by get_xdc_gd_transfer_history;
        # fall back to 0 only if missing (shouldn't happen)
        for xtx in xdc_txs:
            if "_sort_ts" not in xtx:
                xtx["_sort_ts"] = 0.0
        celo_classified.extend(xdc_txs)
    except Exception as xdc_err:
        logger.warning(f"[tx-history] XDC G$ fetch failed (non-fatal): {xdc_err}")
    # ─────────────────────────────────────────────────────────────────────────

    # Re-sort after merging and strip internal sort key
    celo_classified.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    result = []
    for tx in celo_classified:
        clean = {k: v for k, v in tx.items() if k != "_sort_ts"}
        result.append(clean)

    # Cache and return
    with _tx_history_cache_lock:
        _tx_history_cache[cache_key] = {
            "txs":        result,
            "expires_at": _time.time() + TX_HISTORY_CACHE_TTL,
        }

    return result[:limit]


def prepare_gd_transfer_data(to_address: str, amount_gd: float) -> dict:
    """
    Return the ABI-encoded calldata for an ERC-20 transfer(address,uint256) call.
    This is sent to the frontend so the user can sign it with their own wallet.
    """
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        token_addr = Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"])
        to_checksum = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_gd * (10 ** 18))
        # Encode transfer(address,uint256) calldata manually — works on all web3.py versions
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_wei])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token_addr,
            "data": data,
            "value": "0x0",
            "chain_id": CELO_CHAIN_ID,
            "token": "G$",
            "recipient": to_checksum,
            "amount": amount_gd,
        }
    except Exception as e:
        logger.error(f"prepare_gd_transfer_data error: {e}")
        return {"success": False, "error": str(e)}


def check_ubi_entitlement(wallet_address: str) -> dict:
    """
    Check how much G$ a wallet can claim right now from the UBI pool.
    Returns claimable amount in G$ (human-readable) and raw wei value.
    Also returns the encoded claim() calldata so the frontend can send the tx.
    """
    try:
        w3 = _get_w3()
        checksum_wallet = w3.to_checksum_address(wallet_address)
        ubi_proxy = w3.eth.contract(
            address=w3.to_checksum_address(GOODDOLLAR_CONTRACTS["UBI_PROXY"]),
            abi=UBI_SCHEME_ABI
        )

        entitlement_wei = ubi_proxy.functions.checkEntitlement(checksum_wallet).call()
        entitlement_gd = entitlement_wei / (10 ** 18)

        # Encode claim() calldata: keccak256("claim()")[0:4] = 0x4e71d92d
        claim_selector = w3.keccak(text="claim()")[:4].hex()
        claim_calldata = "0x" + claim_selector

        whitelisted = is_identity_verified(wallet_address)

        return {
            "success": True,
            "wallet": wallet_address,
            "claimable_wei": str(entitlement_wei),
            "claimable_gd": round(entitlement_gd, 4),
            "can_claim": entitlement_wei > 0,
            "whitelisted": whitelisted,
            "ubi_contract": GOODDOLLAR_CONTRACTS["UBI_PROXY"],
            "claim_calldata": claim_calldata,
            "chain_id": CELO_CHAIN_ID,
        }
    except Exception as e:
        logger.error(f"check_ubi_entitlement error for {wallet_address}: {e}")
        return {
            "success": False,
            "error": str(e),
            "can_claim": False,
            "claimable_gd": 0,
        }



# ============================
# XDC Network Integration
# ============================

XDC_CHAIN_ID = int(os.getenv("XDC_CHAIN_ID", "50"))
XDC_RPC = os.getenv("XDC_RPC_URL", "https://erpc.xinfin.network")

# Common XDC-ecosystem tokens (mainnet)
XUSDT_CONTRACT = os.getenv("XUSDT_CONTRACT", "0xD4B5f10D61916Bd6E0860144a91Ac658dE8a1437")
SRX_CONTRACT   = os.getenv("SRX_CONTRACT",   "0x17B217490e1c17dD9d41E5c8f3fF7DE1bd38eE60")

# GoodDollar contracts on XDC Network (production-xdc)
XDC_GD_TOKEN    = os.getenv("XDC_GD_TOKEN",   "0xEC2136843a983885AebF2feB3931F73A8eBEe50c")
XDC_UBI_SCHEME  = os.getenv("XDC_UBI_SCHEME", "0x22867567E2D80f2049200E25C6F31CB6Ec2F0faf")
XDC_IDENTITY    = os.getenv("XDC_IDENTITY",   "0x27a4a02C9ed591E1a86e2e5D05870292c34622C9")
XDC_GD_DECIMALS = 18  # G$ on XDC uses 18 decimal places.

# GoodDollar on Fuse mainnet. Defaults come from GoodDollar docs; the decimals
# value is still read from-chain when possible so deployments can override safely.
FUSE_CHAIN_ID = int(os.getenv("FUSE_CHAIN_ID", "122"))
FUSE_RPC = os.getenv("FUSE_RPC_URL", "https://rpc.fuse.io")
FUSE_GD_TOKEN = os.getenv("FUSE_GD_TOKEN", "0x495d133B938596C9984d462F007B676bDc57eCEC")
FUSE_GD_DECIMALS = int(os.getenv("FUSE_GD_DECIMALS", "2"))
FUSE_UBI_SCHEME = os.getenv("FUSE_UBI_SCHEME", "0xd253A5203817225e9768C05E5996d642fb96bA86")

_xdc_w3_singleton = None
_xdc_w3_lock = threading.Lock()
_fuse_w3_singleton = None
_fuse_w3_lock = threading.Lock()

_xdc_balance_cache: dict = {}
_xdc_balance_cache_lock = threading.Lock()
XDC_BALANCE_CACHE_TTL = 120  # 2 minutes

_fuse_balance_cache: dict = {}
_fuse_balance_cache_lock = threading.Lock()
FUSE_BALANCE_CACHE_TTL = 120  # 2 minutes


def _get_xdc_w3():
    global _xdc_w3_singleton
    with _xdc_w3_lock:
        if _xdc_w3_singleton is None or not _xdc_w3_singleton.is_connected():
            from web3 import Web3 as _W3
            _xdc_w3_singleton = _W3(_W3.HTTPProvider(XDC_RPC, request_kwargs={"timeout": 10}))
        return _xdc_w3_singleton


def _get_fuse_w3():
    global _fuse_w3_singleton
    with _fuse_w3_lock:
        if _fuse_w3_singleton is None or not _fuse_w3_singleton.is_connected():
            from web3 import Web3 as _W3
            _fuse_w3_singleton = _W3(_W3.HTTPProvider(FUSE_RPC, request_kwargs={"timeout": 10}))
        return _fuse_w3_singleton


def _normalize_xdc_address(address: str) -> str:
    """Convert xdc... prefix to 0x... so web3.py can handle it."""
    if address.lower().startswith("xdc"):
        return "0x" + address[3:]
    return address


def get_xdc_balance(wallet_address: str) -> dict:
    """Get native XDC balance for a wallet address (cached 2 minutes)."""
    import time
    norm = _normalize_xdc_address(wallet_address)
    key = norm.lower()
    with _xdc_balance_cache_lock:
        entry = _xdc_balance_cache.get(key)
        if entry and entry.get("xdc") and entry["expires_at"] > time.time():
            return entry["xdc"]
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        checksum = Web3.to_checksum_address(norm)
        balance_wei = w3.eth.get_balance(checksum)
        balance_xdc = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_xdc),
            "balance_wei": str(balance_wei),
            "balance_formatted": f"{balance_xdc:.6f} XDC",
            "wallet": wallet_address,
        }
        with _xdc_balance_cache_lock:
            existing = _xdc_balance_cache.get(key, {})
            _xdc_balance_cache[key] = {**existing, "xdc": result, "expires_at": time.time() + XDC_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"XDC balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


_XDC_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]


def _get_xdc_token_balance(wallet_address: str, token_contract: str, cache_key: str) -> dict:
    """Generic XDC ERC-20 balance fetch with caching."""
    import time
    norm = _normalize_xdc_address(wallet_address)
    key = norm.lower()
    with _xdc_balance_cache_lock:
        entry = _xdc_balance_cache.get(key)
        if entry and entry.get(cache_key) and entry["expires_at"] > time.time():
            return entry[cache_key]
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_contract),
            abi=_XDC_ERC20_ABI
        )
        checksum = Web3.to_checksum_address(norm)
        balance_raw = contract.functions.balanceOf(checksum).call()
        try:
            decimals = contract.functions.decimals().call()
        except Exception:
            decimals = 6
        balance = balance_raw / (10 ** decimals)
        result = {
            "success": True,
            "balance": float(balance),
            "balance_formatted": f"{balance:.6f}",
            "wallet": wallet_address,
            "contract": token_contract,
        }
        with _xdc_balance_cache_lock:
            existing = _xdc_balance_cache.get(key, {})
            _xdc_balance_cache[key] = {**existing, cache_key: result, "expires_at": time.time() + XDC_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"XDC token {cache_key} balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def get_xusdt_balance(wallet_address: str) -> dict:
    return _get_xdc_token_balance(wallet_address, XUSDT_CONTRACT, "xusdt")


def get_fuse_balance(wallet_address: str) -> dict:
    """Get native FUSE balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _fuse_balance_cache_lock:
        entry = _fuse_balance_cache.get(key)
        if entry and entry.get("fuse") and entry["expires_at"] > time.time():
            return entry["fuse"]
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(checksum)
        balance_fuse = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_fuse),
            "balance_wei": str(balance_wei),
            "balance_formatted": f"{balance_fuse:.6f} FUSE",
            "wallet": wallet_address,
            "network": "fuse",
        }
        with _fuse_balance_cache_lock:
            existing = _fuse_balance_cache.get(key, {})
            _fuse_balance_cache[key] = {**existing, "fuse": result, "expires_at": time.time() + FUSE_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"FUSE balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def _get_fuse_token_decimals(token_contract: str) -> int:
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        contract = w3.eth.contract(address=Web3.to_checksum_address(token_contract), abi=_XDC_ERC20_ABI)
        return int(contract.functions.decimals().call())
    except Exception:
        return FUSE_GD_DECIMALS


def get_fuse_gd_balance(wallet_address: str) -> dict:
    """Get G$ (GoodDollar) balance on Fuse Network."""
    import time
    key = wallet_address.lower()
    with _fuse_balance_cache_lock:
        entry = _fuse_balance_cache.get(key)
        if entry and entry.get("gd") and entry["expires_at"] > time.time():
            return entry["gd"]
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        token = Web3.to_checksum_address(FUSE_GD_TOKEN)
        contract = w3.eth.contract(address=token, abi=_XDC_ERC20_ABI)
        balance_raw = contract.functions.balanceOf(checksum).call()
        decimals = _get_fuse_token_decimals(FUSE_GD_TOKEN)
        balance = balance_raw / (10 ** decimals)
        result = {
            "success": True,
            "balance": float(balance),
            "balance_raw": str(balance_raw),
            "decimals": decimals,
            "token": "G$",
            "network": "fuse",
            "wallet": wallet_address,
            "contract": FUSE_GD_TOKEN,
        }
        with _fuse_balance_cache_lock:
            existing = _fuse_balance_cache.get(key, {})
            _fuse_balance_cache[key] = {**existing, "gd": result, "expires_at": time.time() + FUSE_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"get_fuse_gd_balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0.0}


def check_fuse_ubi_entitlement(wallet_address: str) -> dict:
    """Check how much G$ the wallet can claim on Fuse Network via UBIScheme.checkEntitlement()."""
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        ubi_addr = Web3.to_checksum_address(FUSE_UBI_SCHEME)
        abi = [
            {"inputs":[],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
            {"inputs":[{"name":"_account","type":"address"}],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        ]
        contract = w3.eth.contract(address=ubi_addr, abi=abi)
        try:
            raw = contract.functions.checkEntitlement(checksum).call()
        except Exception:
            raw = contract.functions.checkEntitlement().call({'from': checksum})
        claimable = raw / (10 ** FUSE_GD_DECIMALS)
        return {
            "success": True,
            "claimable": float(claimable),
            "claimable_raw": str(raw),
            "can_claim": claimable > 0,
            "network": "fuse",
            "ubi_contract": FUSE_UBI_SCHEME,
            "chain_id": FUSE_CHAIN_ID,
        }
    except Exception as e:
        logger.error(f"check_fuse_ubi_entitlement error: {e}")
        return {"success": False, "error": str(e), "claimable": 0.0, "can_claim": False, "network": "fuse"}


def prepare_fuse_gd_send_data(to_address: str, amount: float) -> dict:
    """Prepare Fuse G$ ERC-20 transfer calldata."""
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        to_checksum = Web3.to_checksum_address(to_address)
        token = Web3.to_checksum_address(FUSE_GD_TOKEN)
        decimals = _get_fuse_token_decimals(FUSE_GD_TOKEN)
        amount_raw = int(amount * (10 ** decimals))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_raw])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token,
            "data": data,
            "value": "0x0",
            "chain_id": FUSE_CHAIN_ID,
            "token": "FUSE_GD",
            "recipient": to_checksum,
            "amount": amount,
            "decimals": decimals,
        }
    except Exception as e:
        logger.error(f"prepare_fuse_gd_send_data error: {e}")
        return {"success": False, "error": str(e)}


def prepare_fuse_send_data(to_address: str, amount_fuse: float) -> dict:
    """Prepare native FUSE send transaction parameters."""
    try:
        from web3 import Web3
        to_checksum = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_fuse * (10 ** 18))
        return {
            "success": True,
            "to": to_checksum,
            "data": "0x",
            "value": hex(amount_wei),
            "chain_id": FUSE_CHAIN_ID,
            "token": "FUSE",
            "recipient": to_checksum,
            "amount": amount_fuse,
        }
    except Exception as e:
        logger.error(f"prepare_fuse_send_data error: {e}")
        return {"success": False, "error": str(e)}


def get_xdc_transfer_history(wallet_address: str, limit: int = 30) -> list:
    """
    Fetch recent XDC transfer events using XDCScan API (block explorer).
    Falls back to empty list if the API is unavailable.
    """
    import requests as _req
    norm = _normalize_xdc_address(wallet_address)
    try:
        api_url = "https://xdc.blocksscan.io/api"
        params = {
            "module": "account",
            "action": "txlist",
            "address": norm,
            "sort": "desc",
            "limit": limit,
        }
        resp = _req.get(api_url, params=params, timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        txs = data.get("result", [])
        if not isinstance(txs, list):
            return []
        transfers = []
        for tx in txs[:limit]:
            try:
                value_wei = int(tx.get("value", "0"))
                value_xdc = value_wei / (10 ** 18)
                from_addr = tx.get("from", "")
                to_addr   = tx.get("to", "")
                direction = "sent" if from_addr.lower() == norm.lower() else "received"
                transfers.append({
                    "direction": direction,
                    "from":      from_addr,
                    "to":        to_addr,
                    "amount":    value_xdc,
                    "amount_formatted": f"{value_xdc:.6f} XDC",
                    "tx_hash":   tx.get("hash", ""),
                    "block":     int(tx.get("blockNumber", 0)),
                    "timestamp": tx.get("timeStamp", ""),
                    "explorer_url": f"https://explorer.xinfin.network/txs/{tx.get('hash', '')}",
                })
            except Exception:
                continue
        return transfers
    except Exception as e:
        logger.error(f"XDC transfer history error for {wallet_address}: {e}")
        return []


def get_xdc_gd_transfer_history(wallet_address: str, limit: int = 50) -> list:
    """
    Fetch XDC G$ (ERC-20) Transfer events directly from the XDC RPC via eth_getLogs.
    This is the authoritative source — it reads directly from the blockchain so the
    token amounts are always correct (blocksscan's tokentx returns native XDC value=0).

    Classifies each event as 'claim' (from UBI Scheme), 'transfer_sent', or
    'transfer_received'. Falls back to empty list on any error.
    """
    import time as _time
    TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    norm        = _normalize_xdc_address(wallet_address)
    wallet_lower = norm.lower()
    # 32-byte zero-padded topic for the wallet address
    wallet_topic = "0x" + "0" * 24 + wallet_lower[2:]
    xdc_ubi_lower = XDC_UBI_SCHEME.lower()

    try:
        session = _get_rpc_session()

        # Current XDC block
        blk_resp = session.post(XDC_RPC,
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 0},
            timeout=8)
        to_block_int = int(blk_resp.json()["result"], 16)

        # 14-day window — XDC ≈ 2 sec/block → 43 200 blocks/day
        days_back      = 14
        blocks_per_day = 43_200
        from_block_int = to_block_int - days_back * blocks_per_day

        # Chunk + batch parameters
        chunk_size = 50_000   # 50 k blocks ≈ 28 h on XDC  →  ≈ 13 chunks for 14 days
        batch_size = 5        # 5 chunks per HTTP request   →  ≈ 3 HTTP calls per direction

        raw_logs: list = []
        for wallet_pos in (1, 2):           # 1 = wallet is sender, 2 = wallet is receiver
            topics = [TRANSFER_SIG, None, None]
            topics[wallet_pos] = wallet_topic
            params_tpl = {"address": XDC_GD_TOKEN, "topics": topics}

            chunks = []
            cur_to = to_block_int
            while cur_to >= from_block_int:
                cur_from = max(from_block_int, cur_to - chunk_size + 1)
                chunks.append((cur_from, cur_to))
                cur_to = cur_from - 1

            for batch_start in range(0, len(chunks), batch_size):
                sub = chunks[batch_start: batch_start + batch_size]
                payload = [
                    {"jsonrpc": "2.0", "method": "eth_getLogs",
                     "params": [{**params_tpl, "fromBlock": hex(cf), "toBlock": hex(ct)}],
                     "id": batch_start + i}
                    for i, (cf, ct) in enumerate(sub)
                ]
                try:
                    r = session.post(XDC_RPC, json=payload, timeout=25)
                    results = r.json()
                    if isinstance(results, list):
                        for item in results:
                            raw_logs.extend(item.get("result") or [])
                    elif isinstance(results, dict) and "result" in results:
                        raw_logs.extend(results.get("result") or [])
                except Exception as be:
                    logger.warning(f"[xdc-gd-logs] batch error: {be}")

        # Deduplicate by (txHash, logIndex)
        seen: set = set()
        unique_logs = []
        for log in raw_logs:
            key = (log.get("transactionHash", ""), log.get("logIndex", ""))
            if key not in seen:
                seen.add(key)
                unique_logs.append(log)

        # Sort newest-first, trim
        unique_logs.sort(key=lambda x: int(x.get("blockNumber", "0x0"), 16), reverse=True)
        unique_logs = unique_logs[:limit]

        now_ts = _time.time()
        transfers = []
        for log in unique_logs:
            try:
                tlist = log.get("topics", [])
                if len(tlist) < 3:
                    continue
                from_addr = "0x" + tlist[1][-40:]
                to_addr   = "0x" + tlist[2][-40:]
                block_num = int(log.get("blockNumber", "0x0"), 16)
                tx_hash   = log.get("transactionHash", "")
                try:
                    amount = int(log.get("data", "0x0"), 16) / (10 ** XDC_GD_DECIMALS)
                except Exception:
                    amount = 0.0

                direction = "sent" if from_addr.lower() == wallet_lower else "received"

                if from_addr.lower() == xdc_ubi_lower:
                    tx_type = "claim"
                    label   = "G$ UBI Claim (XDC)"
                elif direction == "sent":
                    tx_type = "transfer_sent"
                    label   = "Sent XDC G$"
                else:
                    tx_type = "transfer_received"
                    label   = "Received XDC G$"

                # Approximate Unix timestamp from block offset (XDC ≈ 2 sec/block)
                approx_ts = now_ts - (to_block_int - block_num) * 2

                transfers.append({
                    "network":          "xdc",
                    "token":            "XDC G$",
                    "direction":        direction,
                    "from":             from_addr,
                    "to":               to_addr,
                    "amount":           float(amount),
                    "amount_formatted": f"{amount:.4f} G$",
                    "block":            block_num,
                    "tx_hash":          tx_hash,
                    "timestamp":        str(int(approx_ts)),   # Unix int as string for frontend
                    "explorer_url":     f"https://xdcscan.io/tx/{tx_hash}",
                    "tx_type":          tx_type,
                    "label":            label,
                    "_sort_ts":         approx_ts,
                })
            except Exception:
                continue

        logger.info(f"[xdc-gd-history] {wallet_address[:8]}… → {len(transfers)} G$ transfers (14d)")
        return transfers

    except Exception as e:
        logger.error(f"XDC G$ transfer history error for {wallet_address}: {e}")
        return []


def prepare_xdc_send_data(to_address: str, amount_xdc: float) -> dict:
    """Prepare native XDC send transaction parameters."""
    try:
        from web3 import Web3
        norm_to = _normalize_xdc_address(to_address)
        to_checksum = Web3.to_checksum_address(norm_to)
        amount_wei = int(amount_xdc * (10 ** 18))
        return {
            "success": True,
            "to": to_checksum,
            "data": "0x",
            "value": hex(amount_wei),
            "chain_id": XDC_CHAIN_ID,
            "token": "XDC",
            "recipient": to_checksum,
            "amount": amount_xdc,
        }
    except Exception as e:
        logger.error(f"prepare_xdc_send_data error: {e}")
        return {"success": False, "error": str(e)}


def prepare_xdc_token_send_data(to_address: str, amount: float, token_contract: str, decimals: int = 6) -> dict:
    """Prepare XDC ERC-20 token transfer calldata."""
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        norm_to = _normalize_xdc_address(to_address)
        to_checksum = Web3.to_checksum_address(norm_to)
        amount_raw = int(amount * (10 ** decimals))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_raw])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": Web3.to_checksum_address(token_contract),
            "data": data,
            "value": "0x0",
            "chain_id": XDC_CHAIN_ID,
            "token": "XUSDT",
            "recipient": to_checksum,
            "amount": amount,
        }
    except Exception as e:
        logger.error(f"prepare_xdc_token_send_data error: {e}")
        return {"success": False, "error": str(e)}


def get_xdc_gd_balance(wallet_address: str) -> dict:
    """Get G$ (GoodDollar) balance on XDC Network. G$ uses 2 decimals."""
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        norm = _normalize_xdc_address(wallet_address)
        checksum = Web3.to_checksum_address(norm)
        token = Web3.to_checksum_address(XDC_GD_TOKEN)
        abi = [{"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        contract = w3.eth.contract(address=token, abi=abi)
        raw = contract.functions.balanceOf(checksum).call()
        balance = raw / (10 ** XDC_GD_DECIMALS)
        return {"success": True, "balance": float(balance), "balance_raw": str(raw), "token": "G$", "network": "xdc"}
    except Exception as e:
        logger.error(f"get_xdc_gd_balance error: {e}")
        return {"success": False, "error": str(e), "balance": 0.0}


def check_xdc_ubi_entitlement(wallet_address: str) -> dict:
    """Check how much G$ the wallet can claim on XDC Network via UBIScheme.checkEntitlement()."""
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        norm = _normalize_xdc_address(wallet_address)
        checksum = Web3.to_checksum_address(norm)
        ubi_addr = Web3.to_checksum_address(XDC_UBI_SCHEME)
        abi = [
            {"inputs":[],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
            {"inputs":[{"name":"_account","type":"address"}],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        ]
        contract = w3.eth.contract(address=ubi_addr, abi=abi)
        try:
            raw = contract.functions.checkEntitlement(checksum).call()
        except Exception:
            raw = contract.functions.checkEntitlement().call({'from': checksum})
        claimable = raw / (10 ** XDC_GD_DECIMALS)
        return {
            "success": True,
            "claimable": float(claimable),
            "claimable_raw": str(raw),
            "can_claim": claimable > 0,
            "network": "xdc",
        }
    except Exception as e:
        logger.error(f"check_xdc_ubi_entitlement error: {e}")
        return {"success": False, "error": str(e), "claimable": 0.0, "can_claim": False}


def is_xdc_identity_whitelisted(wallet_address: str) -> dict:
    """Check if wallet is whitelisted on XDC GoodDollar Identity contract."""
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        norm = _normalize_xdc_address(wallet_address)
        checksum = Web3.to_checksum_address(norm)
        id_addr = Web3.to_checksum_address(XDC_IDENTITY)
        abi = [{"inputs":[{"name":"_user","type":"address"}],"name":"isWhitelisted","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"}]
        contract = w3.eth.contract(address=id_addr, abi=abi)
        whitelisted = contract.functions.isWhitelisted(checksum).call()
        return {"success": True, "whitelisted": whitelisted, "network": "xdc"}
    except Exception as e:
        logger.error(f"is_xdc_identity_whitelisted error: {e}")
        return {"success": False, "error": str(e), "whitelisted": False}



# =========================================================================
# CONSOLIDATED MODULE BLOCKCHAIN SERVICES
# =========================================================================
# The following classes and instances were originally in separate module
# directories (e.g. telegram_task/blockchain.py, minigames/blockchain.py).
# They have been consolidated here for a flat-file organization.
# =========================================================================


# =========================================================================
# Shared ERC-20 ABI (used by Telegram, Twitter, Community Stories, etc.)
# =========================================================================

_GD_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


# =========================================================================
# Telegram Task Blockchain Service (from telegram_task/blockchain.py)
# =========================================================================



logger = logging.getLogger(__name__)




class TelegramTaskBlockchain:
    """Telegram Task Disbursement via direct DAILYTASK_KEY G$ transfer."""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Telegram Task")
        else:
            logger.error("❌ Failed to connect to Celo network")

        logger.info("📱 Telegram Task Blockchain Service initialized (DAILYTASK_KEY direct-transfer mode)")

    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_telegram_reward(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """
        Disburse Telegram Task reward as a direct G$ ERC-20 transfer signed by
        DAILYTASK_KEY. No on-chain reward contract is involved.

        Args:
            wallet_address: Recipient wallet address
            amount: Amount in G$ to send
            task_id: Unique task/submission ID (used only for logging)

        Returns:
            dict: Result with success status, tx_hash, or error
        """
        try:
            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"📱 Telegram reward disbursement: {amount} G$ to {masked_wallet} | task_id={task_id}")

            dailytask_key = os.getenv('DAILYTASK_KEY')
            if not dailytask_key:
                logger.error("❌ DAILYTASK_KEY not configured")
                return {"success": False, "error": "DAILYTASK_KEY not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            try:
                if not dailytask_key.startswith('0x'):
                    dailytask_key = '0x' + dailytask_key
                dailytask_account = Account.from_key(dailytask_key)
            except Exception as key_error:
                logger.error(f"❌ DAILYTASK_KEY is invalid: {key_error}")
                return {"success": False, "error": "DAILYTASK_KEY invalid"}

            gd_address = _CONFIG_GOODDOLLAR_ADDRESS
            if not gd_address:
                logger.error("❌ GOODDOLLAR_CONTRACT_ADDRESS not configured")
                return {"success": False, "error": "G$ token address not configured"}

            try:
                gd_contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(gd_address),
                    abi=_GD_ERC20_ABI,
                )
            except Exception as contract_error:
                logger.error(f"❌ Failed to load G$ token contract: {contract_error}")
                return {"success": False, "error": "Failed to load G$ token contract"}

            reward_amount_wei = int(round(float(amount) * (10 ** 18)))

            # Pre-flight: verify the sender wallet has enough CELO for gas and
            # enough G$ to cover the transfer. Surfacing this clearly helps ops
            # respond quickly when the wallet needs a top-up.
            try:
                celo_balance = self.w3.eth.get_balance(dailytask_account.address)
                min_celo_required_wei = int(0.005 * (10 ** 18))  # 0.005 CELO floor
                if celo_balance < min_celo_required_wei:
                    logger.error(
                        f"❌ DAILYTASK_KEY wallet has insufficient CELO for gas: "
                        f"{celo_balance / 10**18} CELO. Please top up {dailytask_account.address}."
                    )
                    return {
                        "success": False,
                        "error": "DAILYTASK_KEY wallet needs CELO for gas",
                        "error_type": "insufficient_gas",
                    }
            except Exception as gas_check_err:
                logger.error(f"❌ Failed to check DAILYTASK_KEY wallet CELO balance: {gas_check_err}")
                return {
                    "success": False,
                    "error": "Failed to check DAILYTASK_KEY wallet gas",
                    "error_type": "gas_check_failed",
                }

            try:
                gd_balance = gd_contract.functions.balanceOf(dailytask_account.address).call()
                if gd_balance < reward_amount_wei:
                    logger.error(
                        f"❌ DAILYTASK_KEY wallet has insufficient G$: "
                        f"{gd_balance / 10**18} G$ < {reward_amount_wei / 10**18} G$. "
                        f"Please top up {dailytask_account.address}."
                    )
                    return {
                        "success": False,
                        "error": "DAILYTASK_KEY wallet has insufficient G$",
                        "error_type": "insufficient_balance",
                    }
            except Exception as balance_error:
                logger.error(f"❌ Failed to read DAILYTASK_KEY wallet G$ balance: {balance_error}")
                return {
                    "success": False,
                    "error": "Failed to read DAILYTASK_KEY wallet G$ balance",
                    "error_type": "balance_check_failed",
                }

            try:
                nonce = self.w3.eth.get_transaction_count(dailytask_account.address)
                gas_price = int(self.w3.eth.gas_price * 1.2)
                # Estimate gas dynamically instead of hardcoding a fixed limit.
                # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
                # safety buffer on top of the estimate, and fall back to a
                # conservative ceiling only if estimation fails.
                try:
                    estimated_gas = gd_contract.functions.transfer(
                        Web3.to_checksum_address(wallet_address),
                        int(reward_amount_wei),
                    ).estimate_gas({'from': dailytask_account.address})
                    gas_limit = int(estimated_gas * 1.3)
                    logger.info(
                        f"⛽ Telegram reward gas estimate: {estimated_gas} "
                        f"(using limit: {gas_limit})"
                    )
                except Exception as estimate_error:
                    logger.warning(
                        f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                    )
                    gas_limit = 250000
                tx = gd_contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    int(reward_amount_wei),
                ).build_transaction({
                    'chainId': self.chain_id,
                    'gas': gas_limit,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': dailytask_account.address,
                })
            except Exception as build_error:
                logger.error(f"❌ Failed to build transfer tx: {build_error}")
                return {"success": False, "error": "Failed to build transaction"}

            try:
                signed_tx = self.w3.eth.account.sign_transaction(tx, dailytask_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                tx_hash_hex = tx_hash.hex()
                if not tx_hash_hex.startswith('0x'):
                    tx_hash_hex = '0x' + tx_hash_hex
                logger.info(f"📤 Telegram reward transfer sent: {tx_hash_hex}")
            except Exception as send_error:
                logger.error(f"❌ Failed to send transfer tx: {send_error}")
                return {"success": False, "error": f"Failed to send transaction: {str(send_error)}"}

            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            except Exception as receipt_error:
                logger.error(f"❌ Transfer receipt timeout: {receipt_error}")
                return {
                    "success": False,
                    "error": "Transaction timeout",
                    "tx_hash": tx_hash_hex,
                }

            if receipt.status == 1:
                logger.info(
                    f"✅ Telegram reward disbursed: {reward_amount_wei / 10**18} G$ "
                    f"to {masked_wallet} | tx={tx_hash_hex}"
                )
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": reward_amount_wei / 10**18,
                    "recipient": wallet_address,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                }

            logger.error(f"❌ Telegram reward transfer reverted on-chain | tx={tx_hash_hex}")
            return {
                "success": False,
                "error": "Transfer reverted on-chain",
                "error_type": "reverted",
                "tx_hash": tx_hash_hex,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
            }

        except Exception as e:
            logger.error(f"❌ Telegram reward disbursement error: {e}")
            import traceback
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            return {"success": False, "error": str(e)}

    def disburse_telegram_reward_sync(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Synchronous wrapper for disburse_telegram_reward"""
        import concurrent.futures

        try:
            try:
                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(self._run_in_new_loop, wallet_address, amount, task_id)
                    return future.result()
            except RuntimeError:
                return asyncio.run(self.disburse_telegram_reward(wallet_address, amount, task_id))
        except Exception as e:
            logger.error(f"❌ Sync disbursement wrapper error: {e}")
            return {"success": False, "error": str(e)}

    def _run_in_new_loop(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Helper to run async function in a new loop in a separate thread"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.disburse_telegram_reward(wallet_address, amount, task_id))
        finally:
            loop.close()


# Global instance
telegram_blockchain_service = TelegramTaskBlockchain()


# =========================================================================
# Twitter Task Blockchain Service (from twitter_task/blockchain.py)
# =========================================================================



logger = logging.getLogger(__name__)




class TwitterTaskBlockchain:
    """Twitter Task Disbursement via direct DAILYTASK_KEY G$ transfer."""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Twitter Task")
        else:
            logger.error("❌ Failed to connect to Celo network")

        logger.info("🐦 Twitter Task Blockchain Service initialized (DAILYTASK_KEY direct-transfer mode)")

    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_twitter_reward(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """
        Disburse Twitter Task reward as a direct G$ ERC-20 transfer signed by
        DAILYTASK_KEY. No on-chain reward contract is involved.

        Args:
            wallet_address: Recipient wallet address
            amount: Amount in G$ to send
            task_id: Unique task/submission ID (used only for logging)

        Returns:
            dict: Result with success status, tx_hash, or error
        """
        try:
            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"🐦 Twitter reward disbursement: {amount} G$ to {masked_wallet} | task_id={task_id}")

            dailytask_key = os.getenv('DAILYTASK_KEY')
            if not dailytask_key:
                logger.error("❌ DAILYTASK_KEY not configured")
                return {"success": False, "error": "DAILYTASK_KEY not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            try:
                if not dailytask_key.startswith('0x'):
                    dailytask_key = '0x' + dailytask_key
                dailytask_account = Account.from_key(dailytask_key)
            except Exception as key_error:
                logger.error(f"❌ DAILYTASK_KEY is invalid: {key_error}")
                return {"success": False, "error": "DAILYTASK_KEY invalid"}

            gd_address = _CONFIG_GOODDOLLAR_ADDRESS
            if not gd_address:
                logger.error("❌ GOODDOLLAR_CONTRACT_ADDRESS not configured")
                return {"success": False, "error": "G$ token address not configured"}

            try:
                gd_contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(gd_address),
                    abi=_GD_ERC20_ABI,
                )
            except Exception as contract_error:
                logger.error(f"❌ Failed to load G$ token contract: {contract_error}")
                return {"success": False, "error": "Failed to load G$ token contract"}

            reward_amount_wei = int(round(float(amount) * (10 ** 18)))

            # Pre-flight: verify the sender wallet has enough CELO for gas and
            # enough G$ to cover the transfer. Surfacing this clearly helps ops
            # respond quickly when the wallet needs a top-up.
            try:
                celo_balance = self.w3.eth.get_balance(dailytask_account.address)
                min_celo_required_wei = int(0.005 * (10 ** 18))  # 0.005 CELO floor
                if celo_balance < min_celo_required_wei:
                    logger.error(
                        f"❌ DAILYTASK_KEY wallet has insufficient CELO for gas: "
                        f"{celo_balance / 10**18} CELO. Please top up {dailytask_account.address}."
                    )
                    return {
                        "success": False,
                        "error": "DAILYTASK_KEY wallet needs CELO for gas",
                        "error_type": "insufficient_gas",
                    }
            except Exception as gas_check_err:
                logger.error(f"❌ Failed to check DAILYTASK_KEY wallet CELO balance: {gas_check_err}")
                return {
                    "success": False,
                    "error": "Failed to check DAILYTASK_KEY wallet gas",
                    "error_type": "gas_check_failed",
                }

            try:
                gd_balance = gd_contract.functions.balanceOf(dailytask_account.address).call()
                if gd_balance < reward_amount_wei:
                    logger.error(
                        f"❌ DAILYTASK_KEY wallet has insufficient G$: "
                        f"{gd_balance / 10**18} G$ < {reward_amount_wei / 10**18} G$. "
                        f"Please top up {dailytask_account.address}."
                    )
                    return {
                        "success": False,
                        "error": "DAILYTASK_KEY wallet has insufficient G$",
                        "error_type": "insufficient_balance",
                    }
            except Exception as balance_error:
                logger.error(f"❌ Failed to read DAILYTASK_KEY wallet G$ balance: {balance_error}")
                return {
                    "success": False,
                    "error": "Failed to read DAILYTASK_KEY wallet G$ balance",
                    "error_type": "balance_check_failed",
                }

            try:
                nonce = self.w3.eth.get_transaction_count(dailytask_account.address)
                gas_price = int(self.w3.eth.gas_price * 1.2)
                # Estimate gas dynamically instead of hardcoding a fixed limit.
                # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
                # safety buffer on top of the estimate, and fall back to a
                # conservative ceiling only if estimation fails.
                try:
                    estimated_gas = gd_contract.functions.transfer(
                        Web3.to_checksum_address(wallet_address),
                        int(reward_amount_wei),
                    ).estimate_gas({'from': dailytask_account.address})
                    gas_limit = int(estimated_gas * 1.3)
                    logger.info(
                        f"⛽ Twitter reward gas estimate: {estimated_gas} "
                        f"(using limit: {gas_limit})"
                    )
                except Exception as estimate_error:
                    logger.warning(
                        f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                    )
                    gas_limit = 250000
                tx = gd_contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    int(reward_amount_wei),
                ).build_transaction({
                    'chainId': self.chain_id,
                    'gas': gas_limit,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': dailytask_account.address,
                })
            except Exception as build_error:
                logger.error(f"❌ Failed to build transfer tx: {build_error}")
                return {"success": False, "error": "Failed to build transaction"}

            try:
                signed_tx = self.w3.eth.account.sign_transaction(tx, dailytask_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                tx_hash_hex = tx_hash.hex()
                if not tx_hash_hex.startswith('0x'):
                    tx_hash_hex = '0x' + tx_hash_hex
                logger.info(f"📤 Twitter reward transfer sent: {tx_hash_hex}")
            except Exception as send_error:
                logger.error(f"❌ Failed to send transfer tx: {send_error}")
                return {"success": False, "error": f"Failed to send transaction: {str(send_error)}"}

            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            except Exception as receipt_error:
                logger.error(f"❌ Transfer receipt timeout: {receipt_error}")
                return {
                    "success": False,
                    "error": "Transaction timeout",
                    "tx_hash": tx_hash_hex,
                }

            if receipt.status == 1:
                logger.info(
                    f"✅ Twitter reward disbursed: {reward_amount_wei / 10**18} G$ "
                    f"to {masked_wallet} | tx={tx_hash_hex}"
                )
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": reward_amount_wei / 10**18,
                    "recipient": wallet_address,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                }

            logger.error(f"❌ Twitter reward transfer reverted on-chain | tx={tx_hash_hex}")
            return {
                "success": False,
                "error": "Transfer reverted on-chain",
                "error_type": "reverted",
                "tx_hash": tx_hash_hex,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
            }

        except Exception as e:
            logger.error(f"❌ Twitter reward disbursement error: {e}")
            import traceback
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            return {"success": False, "error": str(e)}

    def disburse_twitter_reward_sync(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Synchronous wrapper for async disbursement"""
        import concurrent.futures

        try:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(self._run_in_new_loop, wallet_address, amount, task_id)
                return future.result()
        except RuntimeError:
            return asyncio.run(self.disburse_twitter_reward(wallet_address, amount, task_id))
        except Exception as e:
            logger.error(f"❌ Sync disbursement wrapper error: {e}")
            return {"success": False, "error": str(e)}

    def _run_in_new_loop(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Helper to run async function in a new loop in a separate thread"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.disburse_twitter_reward(wallet_address, amount, task_id))
        finally:
            loop.close()


# Global instance
twitter_blockchain_service = TwitterTaskBlockchain()


# =========================================================================
# Discourse Task Blockchain Service (from discourse_task/blockchain.py)
# =========================================================================



logger = logging.getLogger(__name__)

class DiscourseTaskBlockchain:
    """Discourse Task Direct Private Key Disbursement"""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_contract = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')

        # Discourse_Task key
        self.task_key = os.getenv('DISCOURSE_TASK_KEY')

        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Discourse Task")
        else:
            logger.error("❌ Failed to connect to Celo network for Discourse Task")

        logger.info("💬 Discourse Task Blockchain Service initialized")

    def mask_wallet_address(self, wallet_address: str) -> str:
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_discourse_reward(self, wallet_address: str, amount: float) -> dict:
        """Disburse Discourse Task rewards via direct private key transfer"""
        try:
            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"💬 Discourse Task reward disbursement: {amount} G$ to {masked_wallet}")

            if not self.task_key:
                logger.error("❌ DISCOURSE_TASK_KEY not configured")
                return {"success": False, "error": "Discourse_Task key not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            try:
                key = self.task_key if self.task_key.startswith('0x') else '0x' + self.task_key
                task_account = Account.from_key(key)
                logger.info(f"🔑 Using Discourse Task account: {self.mask_wallet_address(task_account.address)}")
            except Exception as key_error:
                logger.error(f"❌ Failed to load DISCOURSE_TASK_KEY: {key_error}")
                return {"success": False, "error": "Key loading error"}

            erc20_abi = [
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]

            try:
                contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.gooddollar_contract),
                    abi=erc20_abi
                )
            except Exception as contract_error:
                logger.error(f"❌ Failed to instantiate GoodDollar contract: {contract_error}")
                return {"success": False, "error": "Contract instantiation error"}

            amount_wei = int(amount * (10 ** 18))

            try:
                nonce = self.w3.eth.get_transaction_count(task_account.address)
                gas_price = int(self.w3.eth.gas_price * 1.2)
            except Exception as network_error:
                logger.error(f"❌ Failed to get network info: {network_error}")
                return {"success": False, "error": "Network error"}

            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).estimate_gas({'from': task_account.address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Discourse Task gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            try:
                transfer_txn = contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).build_transaction({
                    'chainId': self.chain_id,
                    'gas': gas_limit,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': task_account.address
                })
            except Exception as build_error:
                logger.error(f"❌ Failed to build transaction: {build_error}")
                return {"success": False, "error": "Transaction build error"}

            try:
                signed_txn = self.w3.eth.account.sign_transaction(transfer_txn, self.task_key)
            except Exception as sign_error:
                logger.error(f"❌ Failed to sign transaction: {sign_error}")
                return {"success": False, "error": "Transaction signing error"}

            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
                tx_hash_hex = tx_hash.hex()
                if not tx_hash_hex.startswith('0x'):
                    tx_hash_hex = '0x' + tx_hash_hex
                logger.info(f"🔗 Discourse Task transaction sent: {tx_hash_hex}")
            except Exception as send_error:
                logger.error(f"❌ Failed to send transaction: {send_error}")
                return {"success": False, "error": "Transaction send error"}

            try:
                logger.info(f"⏳ Waiting for confirmation: {tx_hash_hex}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            except Exception as receipt_error:
                logger.error(f"❌ Receipt fetch error: {receipt_error}")
                return {
                    "success": False,
                    "error": "Receipt fetch error",
                    "tx_hash": tx_hash_hex,
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }

            if receipt.status == 1:
                logger.info(f"✅ Discourse Task reward sent to {masked_wallet}. TX: {tx_hash_hex}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }
            else:
                logger.error(f"❌ Discourse Task transaction failed on-chain. TX: {tx_hash_hex}")
                return {
                    "success": False,
                    "error": "Transaction failed on-chain",
                    "tx_hash": tx_hash_hex,
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }

        except Exception as e:
            logger.error(f"❌ Discourse Task disbursement error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def disburse_discourse_reward_sync(self, wallet_address: str, amount: float) -> dict:
        """Synchronous wrapper"""
        import concurrent.futures
        try:
            try:
                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(self._run_in_new_loop, wallet_address, amount)
                    return future.result()
            except RuntimeError:
                return asyncio.run(self.disburse_discourse_reward(wallet_address, amount))
        except Exception as e:
            logger.error(f"❌ Sync wrapper error: {e}")
            return {"success": False, "error": str(e)}

    def _run_in_new_loop(self, wallet_address: str, amount: float) -> dict:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.disburse_discourse_reward(wallet_address, amount))
        finally:
            loop.close()

# Global instance
discourse_blockchain_service = DiscourseTaskBlockchain()


# =========================================================================
# Minigames Blockchain Service (from minigames/blockchain.py)
# =========================================================================


logger = logging.getLogger(__name__)

class MinigamesBlockchainService:
    """Minigames Blockchain Service for G$ Rewards using Direct Private Key"""

    def __init__(self):
        # Network configuration
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_contract = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')

        # MERCHANT_ADDRESS for deposits (users send G$ here)
        merchant_address = os.getenv('MERCHANT_ADDRESS')
        if merchant_address:
            try:
                self.merchant_address = Web3.to_checksum_address(merchant_address)
                logger.info(f"✅ MERCHANT_ADDRESS configured: {self.merchant_address}")
            except Exception as e:
                logger.error(f"❌ Error loading MERCHANT_ADDRESS: {e}")
                self.merchant_address = None
        else:
            self.merchant_address = None
            logger.warning("⚠️ MERCHANT_ADDRESS not configured")

        # GAMES_KEY for withdrawals (sending winnings to users)
        games_key = os.getenv('GAMES_KEY')
        if games_key:
            try:
                if not games_key.startswith('0x'):
                    games_key = '0x' + games_key
                self.games_account = Account.from_key(games_key)
                self.games_key_address = self.games_account.address
                logger.info(f"✅ GAMES_KEY configured: {self.games_key_address}")
            except Exception as e:
                logger.error(f"❌ Error loading GAMES_KEY: {e}")
                self.games_account = None
                self.games_key_address = None
        else:
            self.games_account = None
            self.games_key_address = None
            logger.warning("⚠️ GAMES_KEY not configured")


        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Minigames")
        else:
            logger.error("❌ Failed to connect to Celo network")

        # GoodDollar token contract
        self.gooddollar_token = Web3.to_checksum_address(self.gooddollar_contract)

        # ERC20 ABI for transfers
        self.erc20_abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]

        self.token_contract = self.w3.eth.contract(
            address=self.gooddollar_token,
            abi=self.erc20_abi
        )

        # Transfer event signature
        self.TRANSFER_EVENT_SIGNATURE = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

        logger.info(f"🎮 Minigames Blockchain Service initialized")
        logger.info(f"   MERCHANT address (deposits): {self.merchant_address}")
        logger.info(f"   GAMES_KEY address (withdrawals): {self.games_key_address}")
        logger.info(f"   GoodDollar token: {self.gooddollar_token}")


    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def verify_deposit_to_merchant(self, wallet_address: str, amount: float, tx_hash: str) -> dict:
        """Verify that user deposited G$ to MERCHANT_ADDRESS"""
        try:
            logger.info(f"🔍 Verifying deposit: {amount} G$ from {self.mask_wallet_address(wallet_address)}")

            if not self.w3.is_connected():
                return {"success": False, "error": "Blockchain connection failed"}

            if not self.merchant_address:
                logger.error("❌ MERCHANT_ADDRESS not configured.")
                return {"success": False, "error": "MERCHANT_ADDRESS not configured"}

            # Get transaction receipt
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)

            if not receipt or receipt.status != 1:
                return {"success": False, "error": "Transaction not found or failed"}

            # Check transfer logs for the specific token contract
            for log in receipt.logs:
                if log['address'].lower() == self.gooddollar_token.lower():
                    # Check if it's a Transfer event
                    if len(log['topics']) >= 3:
                        # Topics: [event_signature, from_address, to_address]
                        to_address = '0x' + log['topics'][2].hex()[-40:]

                        if to_address.lower() == self.merchant_address.lower():
                            # Verify amount
                            amount_wei = int(log['data'].hex(), 16)
                            amount_g = amount_wei / (10 ** 18)

                            if abs(amount_g - amount) < 0.01:  # Allow small variance
                                logger.info(f"✅ Deposit verified: {amount} G$ to MERCHANT_ADDRESS")
                                return {"success": True, "verified": True, "amount": amount_g, "tx_hash": tx_hash}

            return {"success": False, "error": "Transfer to MERCHANT_ADDRESS not found in transaction"}

        except Exception as e:
            logger.error(f"❌ Error verifying deposit: {e}")
            return {"success": False, "error": str(e)}

    async def check_pending_deposits(self, wallet_address: str, expected_amount: float = None) -> dict:
        """
        Automatically check for pending deposits to MERCHANT_ADDRESS from a wallet
        Similar to P2P trading's automatic deposit verification
        """
        try:
            logger.info(f"🔍 AUTO-VERIFY: Checking deposits from {self.mask_wallet_address(wallet_address)} to MERCHANT_ADDRESS")

            if not self.w3.is_connected():
                return {'success': False, 'error': 'Blockchain connection failed', 'deposits_found': []}

            if not self.merchant_address:
                logger.error("❌ MERCHANT_ADDRESS not configured.")
                return {'success': False, 'error': 'MERCHANT_ADDRESS not configured', 'deposits_found': []}

            # Calculate block range (last 24 hours)
            latest_block = self.w3.eth.block_number
            # Assuming Celo block time is around 5 seconds, 720 blocks per hour
            blocks_per_hour = 720
            # Look back for 24 hours
            hours_to_check = 24
            from_block = max(0, latest_block - (hours_to_check * blocks_per_hour))


            logger.info(f"📊 Scanning blocks {from_block} to {latest_block} (last {hours_to_check} hours)")

            # Convert addresses to topic format for logs
            # Topic[0] is the event signature
            # Topic[1] is the indexed parameter 'from' (sender)
            # Topic[2] is the indexed parameter 'to' (recipient)
            from_topic = '0x' + '0' * 24 + wallet_address.lower().replace('0x', '')
            to_topic = '0x' + '0' * 24 + self.merchant_address.lower().replace('0x', '')

            # Query Transfer events: FROM user TO MERCHANT_ADDRESS
            filter_params = {
                'fromBlock': hex(from_block),
                'toBlock': 'latest',
                'address': self.gooddollar_token,
                'topics': [
                    self.TRANSFER_EVENT_SIGNATURE,
                    from_topic,  # FROM: user wallet
                    to_topic     # TO: MERCHANT_ADDRESS
                ]
            }

            logs = self.w3.eth.get_logs(filter_params)
            logger.info(f"📋 Found {len(logs)} G$ transfers from {self.mask_wallet_address(wallet_address)} to MERCHANT_ADDRESS")

            deposits = []
            for log in logs:
                try:
                    # Parse amount from the event data
                    amount_wei = int(log['data'].hex(), 16)
                    amount_g = amount_wei / (10 ** 18)

                    # Get block timestamp for context
                    block = self.w3.eth.get_block(log['blockNumber'])
                    timestamp = datetime.fromtimestamp(block['timestamp'])

                    tx_hash = log['transactionHash'].hex()

                    deposit_info = {
                        'tx_hash': tx_hash,
                        'amount': amount_g,
                        'block_number': log['blockNumber'],
                        'timestamp': timestamp.isoformat(),
                        'from': wallet_address,
                        'to': self.merchant_address
                    }

                    # If an expected amount is specified, check if the deposit matches
                    if expected_amount is not None:
                        if abs(amount_g - expected_amount) < 0.01:  # Allow small rounding difference
                            deposits.append(deposit_info)
                            logger.info(f"✅ Matching deposit: {amount_g} G$ (TX: {tx_hash[:16]}...)")
                    else:
                        # If no specific amount is expected, add all found deposits
                        deposits.append(deposit_info)
                        logger.info(f"📦 Deposit found: {amount_g} G$ (TX: {tx_hash[:16]}...)")

                except Exception as parse_error:
                    logger.error(f"❌ Error parsing log entry: {parse_error}")
                    # Continue to the next log entry even if one fails
                    continue

            if len(deposits) > 0:
                logger.info(f"✅ Successfully found {len(deposits)} deposit(s) from {self.mask_wallet_address(wallet_address)}.")
                # Return the list of deposits, count, and the most recent one
                return {
                    'success': True,
                    'deposits_found': deposits,
                    'total_deposits': len(deposits),
                    'latest_deposit': deposits[0] if deposits else None
                }
            else:
                logger.info(f"⏳ No matching deposits found from {self.mask_wallet_address(wallet_address)} to MERCHANT_ADDRESS in the last {hours_to_check} hours.")
                return {
                    'success': True,
                    'deposits_found': [],
                    'total_deposits': 0,
                    'latest_deposit': None
                }

        except Exception as e:
            logger.error(f"❌ An unexpected error occurred while checking pending deposits: {e}")
            # Return error and an empty list of deposits
            return {'success': False, 'error': str(e), 'deposits_found': []}


    async def disburse_from_games_key(self, wallet_address: str, amount: float, session_id: str) -> dict:
        """Disburse winnings from GAMES_KEY to player"""
        try:
            logger.info(f"💸 Disbursing winnings: {amount} G$ to {self.mask_wallet_address(wallet_address)}")

            if not self.games_key_address:
                logger.error("❌ GAMES_KEY not configured")
                return {"success": False, "error": "Games wallet not configured"}

            if not self.w3.is_connected():
                return {"success": False, "error": "Blockchain connection failed"}

            recipient_checksum = Web3.to_checksum_address(wallet_address)
            amount_wei = int(amount * (10 ** 18))

            # Build transfer transaction
            nonce = self.w3.eth.get_transaction_count(self.games_key_address)
            gas_price = int(self.w3.eth.gas_price * 1.2)  # Add 20% buffer

            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = self.token_contract.functions.transfer(
                    recipient_checksum,
                    amount_wei
                ).estimate_gas({'from': self.games_key_address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Withdrawal gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            transaction = self.token_contract.functions.transfer(
                recipient_checksum,
                amount_wei
            ).build_transaction({
                'from': self.games_key_address,
                'nonce': nonce,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            })

            # Sign and send
            signed_txn = self.w3.eth.account.sign_transaction(
                transaction,
                private_key=self.games_account.key if self.games_account else None
            )

            logger.info("📡 Sending withdrawal transaction from GAMES_KEY...")
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                logger.info(f"✅ Withdrawal successful: {amount} G$ - TX: {tx_hash_hex}")

                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "recipient": wallet_address,
                    "message": f"Successfully withdrew {amount} G$!",
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }
            else:
                logger.error(f"❌ Withdrawal failed on-chain: {tx_hash_hex}")
                return {"success": False, "error": "Transaction failed on blockchain", "tx_hash": tx_hash_hex}

        except Exception as e:
            import traceback
            logger.error(f"❌ Withdrawal error: {e}")
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            
            # Check for insufficient funds error
            error_msg = str(e).lower()
            if "insufficient funds" in error_msg:
                logger.error(f"❌ GAMES_KEY wallet needs CELO for gas fees!")
                return {
                    "success": False, 
                    "error": "Withdrawal system temporarily unavailable. Please try again later or contact support.",
                    "error_type": "insufficient_gas",
                    "balance_safe": True
                }
            
            return {"success": False, "error": "Withdrawal failed. Please try again later."}

    async def disburse_game_reward(self, wallet_address: str, amount: float, game_type: str, session_id: str) -> dict:
        """
        Disburse game reward to player via direct private key transfer using GAMES_KEY

        Args:
            wallet_address: Recipient wallet address
            amount: Amount in G$ to disburse
            game_type: Type of game (for logging)
            session_id: Game session ID

        Returns:
            Dict with success status, transaction hash, and details
        """
        try:
            logger.info(f"🎮 Minigame reward disbursement: {amount} G$ to {self.mask_wallet_address(wallet_address)}")

            if not self.games_key_address:
                logger.error("❌ GAMES_KEY not configured for minigames rewards")
                return {"success": False, "error": "Minigames wallet not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            # Convert amount to Wei (18 decimals for G$)
            amount_wei = int(amount * (10 ** 18))

            # Get nonce and gas price for the transaction
            nonce = self.w3.eth.get_transaction_count(self.games_key_address)
            gas_price = int(self.w3.eth.gas_price * 1.2)  # Add 20% buffer for gas price

            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = self.token_contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).estimate_gas({'from': self.games_key_address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Minigame reward gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            # Build the transaction using the token contract's transfer function
            transaction = self.token_contract.functions.transfer(
                Web3.to_checksum_address(wallet_address),
                amount_wei
            ).build_transaction({
                'from': self.games_key_address,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
                'chainId': self.chain_id
            })

            # Sign the transaction with the GAMES_KEY private key
            signed_txn = self.w3.eth.account.sign_transaction(
                transaction,
                private_key=self.games_account.key if self.games_account else None
            )

            # Send the signed transaction to the network
            logger.info("📡 Sending minigame reward transaction...")
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            # Ensure tx_hash starts with '0x'
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"🔗 Transaction sent: {tx_hash_hex}")

            # Wait for the transaction to be confirmed on the blockchain
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                # Transaction successful
                logger.info(f"✅ Minigame reward successfully disbursed: {amount} G$ - TX: {tx_hash_hex}")
                explorer_url = f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                logger.info(f"🔗 Explorer: {explorer_url}")
                logger.info(f"⛽ Gas used: {receipt.gasUsed}")
                logger.info(f"🧾 Block: {receipt.blockNumber}")

                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "game_type": game_type,
                    "session_id": session_id,
                    "recipient": wallet_address,
                    "message": f"Successfully disbursed {amount} G$ minigame reward!",
                    "timestamp": datetime.now().isoformat(),
                    "explorer_url": explorer_url,
                    "blockchain_confirmed": True
                }
            else:
                # Transaction failed on the blockchain
                logger.error(f"❌ Minigame transaction failed on-chain: {tx_hash_hex}")
                return {
                    "success": False,
                    "error": "Transaction failed on blockchain",
                    "tx_hash": tx_hash_hex
                }

        except Exception as e:
            # Log any exceptions during the disbursement process
            import traceback
            logger.error(f"❌ Minigame reward disbursement error: {e}")
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            return {"success": False, "error": str(e)}

# Global instance for the service
minigames_blockchain = MinigamesBlockchainService()


# =========================================================================
# Jumble Blockchain Service (alias of minigames_blockchain)
# =========================================================================

jumble_blockchain = minigames_blockchain


# =========================================================================
# Referral Program Blockchain Service (from referral_program/blockchain.py)
# =========================================================================


logger = logging.getLogger(__name__)

_REFERRAL_ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]


class ReferralBlockchain:
    """Handles G$ disbursement for the referral program using REFERRAL_KEY."""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_token = os.getenv(
            'GOODDOLLAR_TOKEN_CONTRACT',
            '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A'
        )
        self.referral_key = os.getenv('REFERRAL_KEY')
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("Referral blockchain service connected to Celo network")
        else:
            logger.error("Referral blockchain service failed to connect to Celo network")

        if not self.referral_key:
            logger.error("REFERRAL_KEY environment variable not set")

    def _mask(self, addr):
        if not addr or len(addr) < 10:
            return addr
        return addr[:6] + "..." + addr[-4:]

    def get_referral_wallet_balance(self):
        """Return the G$ balance of the REFERRAL_KEY wallet."""
        if not self.referral_key:
            return {"success": False, "error": "REFERRAL_KEY not configured", "balance": 0}
        try:
            key = self.referral_key if self.referral_key.startswith('0x') else '0x' + self.referral_key
            account = Account.from_key(key)
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_token),
                abi=_REFERRAL_ERC20_ABI
            )
            balance_wei = contract.functions.balanceOf(account.address).call()
            balance_g = balance_wei / (10 ** 18)
            return {
                "success": True,
                "balance": balance_g,
                "balance_wei": balance_wei,
                "wallet": account.address
            }
        except Exception as e:
            logger.error(f"Failed to get referral wallet balance: {e}")
            return {"success": False, "error": str(e), "balance": 0}

    def disburse_referral_reward(self, wallet_address: str, amount: float, reward_type: str) -> dict:
        """
        Transfer G$ from REFERRAL_KEY wallet to recipient.
        Returns {"success": True, "tx_hash": "..."} on success.
        Returns {"success": False, "pending": True, "error": "insufficient_balance"} when out of funds.
        """
        try:
            if not self.referral_key:
                logger.error("REFERRAL_KEY not configured")
                return {"success": False, "pending": True, "error": "REFERRAL_KEY not configured"}

            key = self.referral_key if self.referral_key.startswith('0x') else '0x' + self.referral_key
            try:
                referral_account = Account.from_key(key)
            except Exception as key_err:
                logger.error(f"Invalid REFERRAL_KEY: {key_err}")
                return {"success": False, "pending": False, "error": "Invalid REFERRAL_KEY"}

            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_token),
                abi=_REFERRAL_ERC20_ABI
            )

            amount_wei = int(amount * (10 ** 18))

            balance_wei = contract.functions.balanceOf(referral_account.address).call()
            balance_g = balance_wei / (10 ** 18)
            logger.info(
                f"Referral wallet balance: {balance_g:.2f} G$ | "
                f"Required: {amount:.2f} G$ ({reward_type} for {self._mask(wallet_address)})"
            )

            if balance_wei < amount_wei:
                logger.warning(
                    f"Insufficient REFERRAL_KEY balance: {balance_g:.2f} G$ < {amount:.2f} G$. "
                    f"Marking as pending_disbursed."
                )
                return {
                    "success": False,
                    "pending": True,
                    "error": "insufficient_balance",
                    "balance_available": balance_g,
                    "balance_required": amount
                }

            nonce = self.w3.eth.get_transaction_count(referral_account.address)
            gas_price = int(self.w3.eth.gas_price * 1.2)

            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).estimate_gas({'from': referral_account.address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"Referral reward gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            txn = contract.functions.transfer(
                Web3.to_checksum_address(wallet_address),
                amount_wei
            ).build_transaction({
                'chainId': self.chain_id,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
                'from': referral_account.address
            })

            signed_txn = self.w3.eth.account.sign_transaction(txn, key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"Referral reward TX sent: {tx_hash_hex}")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status == 1:
                logger.info(
                    f"Referral {reward_type} reward of {amount} G$ sent to "
                    f"{self._mask(wallet_address)} | TX: {tx_hash_hex}"
                )
                return {
                    "success": True,
                    "pending": False,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "recipient": wallet_address,
                    "reward_type": reward_type
                }
            else:
                logger.error(f"Referral reward TX failed on-chain: {tx_hash_hex}")
                return {"success": False, "pending": False, "error": "Transaction failed on-chain", "tx_hash": tx_hash_hex}

        except Exception as e:
            logger.error(f"Referral reward disbursement error for {reward_type}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"success": False, "pending": False, "error": str(e)}

    def disburse_referral_reward_sync(self, wallet_address: str, amount: float, reward_type: str) -> dict:
        """Synchronous wrapper (runs in current thread)."""
        return self.disburse_referral_reward(wallet_address, amount, reward_type)


referral_blockchain_service = ReferralBlockchain()


# =========================================================================
# Learn & Earn Blockchain Service (from learn_and_earn/blockchain.py)
# =========================================================================


logger = logging.getLogger(__name__)


class LearnBlockchainService:
    """Learn & Earn Smart Contract Disbursement Service
    
    Uses the deployed LearnAndEarnRewards smart contract for secure G$ disbursements.
    Falls back to direct transfer only if contract is not configured.
    """

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 2

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_address = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
        self.contract_address = _CONFIG_LEARN_EARN_ADDRESS or None
        self._wallet_key = os.getenv('LEARN_WALLET_PRIVATE_KEY')
        self.tx_receipt_timeout = int(os.getenv('TX_RECEIPT_TIMEOUT', '300'))

        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url, request_kwargs={'timeout': 30}))
        self.contract = None
        self.owner_account = None

        if self.w3.is_connected():
            logger.info("Connected to Celo network for Learn & Earn")
        else:
            logger.error("Failed to connect to Celo network")

        self._initialize()

    def _initialize(self):
        """Initialize contract and wallet"""
        try:
            if self.contract_address:
                self.contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.contract_address),
                    abi=self._get_contract_abi()
                )
                logger.info(f"Learn & Earn Contract loaded: {self.contract_address[:10]}...")
            else:
                logger.warning("Learn & Earn contract not configured")

            if self._wallet_key:
                key = self._wallet_key if self._wallet_key.startswith('0x') else '0x' + self._wallet_key
                self.owner_account = Account.from_key(key)
                logger.info("Learn & Earn wallet configured")
            else:
                logger.warning("Learn & Earn wallet not configured")

        except Exception as e:
            logger.error(f"Initialization error: {type(e).__name__}")

    @property
    def is_configured(self) -> bool:
        """Check if the service is properly configured (without exposing private key)"""
        return self.owner_account is not None

    def _get_contract_abi(self):
        """Get minimal ABI for contract interactions"""
        return [
            {"inputs": [], "name": "getContractBalance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "quizId", "type": "string"}], "name": "disburseReward", "outputs": [{"type": "bytes32"}], "stateMutability": "nonpayable", "type": "function"},
            {"inputs": [{"name": "recipient", "type": "address"}, {"name": "quizId", "type": "string"}], "name": "isQuizRewardClaimed", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "maxDisbursementAmount", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "minDisbursementAmount", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
        ]

    def _get_erc20_abi(self):
        """Get ERC20 ABI for balance checks"""
        return [
            {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
        ]

    def _generate_quiz_id(self, wallet_address: str, quiz_result_summary: dict = None) -> str:
        """Generate a unique, deterministic quiz ID using wallet + timestamp + uuid"""
        timestamp = int(datetime.now().timestamp())
        unique_part = uuid.uuid4().hex[:8]
        short_wallet = wallet_address[-8:].lower()
        return f"quiz_{short_wallet}_{timestamp}_{unique_part}"

    def _safe_amount_wei(self, amount: float) -> int:
        """Convert G$ amount to wei safely, avoiding floating point precision issues.
        
        Uses Decimal for precise conversion to prevent amounts like 1000.0000000000001
        from exceeding the contract's maxDisbursementAmount.
        """
        d_amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        amount_wei = int(d_amount * Decimal('1000000000000000000'))

        try:
            if self.contract:
                max_wei = self.contract.functions.maxDisbursementAmount().call()
                min_wei = self.contract.functions.minDisbursementAmount().call()

                if amount_wei > max_wei:
                    logger.warning(f"Amount {amount_wei} exceeds max {max_wei}, capping to max")
                    amount_wei = max_wei
                elif amount_wei < min_wei:
                    logger.warning(f"Amount {amount_wei} below min {min_wei}, raising to min")
                    amount_wei = min_wei
        except Exception as e:
            logger.warning(f"Could not check disbursement limits: {e}")

        return amount_wei

    async def get_contract_balance(self) -> float:
        """Get the G$ balance of the Learn & Earn contract"""
        try:
            if not self.contract:
                logger.error("Contract not configured")
                return 0.0

            balance_wei = self.contract.functions.getContractBalance().call()
            balance_g = balance_wei / (10 ** 18)
            logger.info(f"Contract balance: {balance_g:.2f} G$")
            return balance_g

        except Exception as e:
            logger.error(f"Error getting contract balance: {type(e).__name__}: {e}")
            return 0.0

    async def get_learn_wallet_balance(self) -> float:
        """Get the G$ balance of the Learn wallet (for legacy compatibility)"""
        try:
            if self.contract:
                return await self.get_contract_balance()

            if not self.owner_account:
                return 0.0

            erc20 = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_address),
                abi=self._get_erc20_abi()
            )
            balance_wei = erc20.functions.balanceOf(self.owner_account.address).call()
            return balance_wei / (10 ** 18)

        except Exception as e:
            logger.error(f"Error getting balance: {type(e).__name__}: {e}")
            return 0.0

    async def send_g_reward(self, wallet_address: str, amount: float, quiz_result_summary: dict = None) -> dict:
        """Send G$ rewards - uses smart contract with unique quiz ID"""
        try:
            quiz_id = self._generate_quiz_id(wallet_address, quiz_result_summary)
            logger.info(f"Generated unique quiz_id: {quiz_id}")
            return await self.disburse_quiz_reward(wallet_address, amount, quiz_id)

        except Exception as e:
            logger.error(f"Error sending reward: {type(e).__name__}: {e}")
            return {"success": False, "error": "Failed to send reward"}

    async def disburse_quiz_reward(self, wallet_address: str, amount: float, quiz_id: str) -> dict:
        """Send G$ rewards via smart contract with retry logic"""
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                result = await self._attempt_disburse(wallet_address, amount, quiz_id, attempt)

                if result.get('success'):
                    return result

                if result.get('permanent_failure'):
                    return result

                last_error = result.get('error', 'Unknown error')
                logger.warning(f"Attempt {attempt}/{self.MAX_RETRIES} failed: {last_error}")

                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAY_BASE ** attempt
                    logger.info(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

            except Exception as e:
                last_error = str(e)
                logger.error(f"Attempt {attempt}/{self.MAX_RETRIES} exception: {type(e).__name__}: {e}")

                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAY_BASE ** attempt
                    await asyncio.sleep(delay)

        error_msg = self._sanitize_error(last_error or "Failed after all retries")
        return {"success": False, "error": error_msg}

    async def _attempt_direct_transfer(self, wallet_address: str, amount: float, attempt: int) -> dict:
        """Direct ERC20 transfer fallback when smart contract is not configured"""
        logger.info(f"Direct ERC20 transfer attempt {attempt}: {amount} G$ to {wallet_address[:10]}...")
        try:
            erc20_abi = [
                {"inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
                {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            ]
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_address),
                abi=erc20_abi
            )

            amount_wei = int(Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_DOWN) * Decimal('1000000000000000000'))

            balance_wei = token.functions.balanceOf(self.owner_account.address).call()
            balance_g = balance_wei / (10 ** 18)
            logger.info(f"Learn wallet balance: {balance_g:.4f} G$, need: {amount} G$")

            if balance_wei < amount_wei:
                return {"success": False, "error": "Rewards pool is currently depleted. Please try again later.", "permanent_failure": True}

            nonce = self.w3.eth.get_transaction_count(self.owner_account.address, 'pending')
            gas_price = int(self.w3.eth.gas_price * 1.2)
            if attempt > 1:
                gas_price = int(gas_price * (1 + (attempt * 0.1)))

            try:
                estimated_gas = token.functions.transfer(
                    Web3.to_checksum_address(wallet_address), amount_wei
                ).estimate_gas({'from': self.owner_account.address})
                gas_limit = int(estimated_gas * 1.3)
            except Exception:
                gas_limit = 100000

            txn = token.functions.transfer(
                Web3.to_checksum_address(wallet_address), amount_wei
            ).build_transaction({
                'chainId': self.chain_id,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
            })

            signed_txn = self.w3.eth.account.sign_transaction(txn, self._wallet_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"Direct transfer sent: {tx_hash_hex}")
            receipt = self._wait_for_receipt(tx_hash)

            if receipt.status == 1:
                logger.info(f"Direct transfer success: {amount} G$ → {wallet_address[:10]} TX: {tx_hash_hex}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                    "gas_used": receipt.gasUsed,
                    "block_number": receipt.blockNumber,
                    "method": "direct_transfer"
                }
            else:
                logger.error(f"Direct transfer REVERTED: {tx_hash_hex}")
                return {"success": False, "error": "Transaction reverted. Please try again."}

        except Exception as e:
            logger.error(f"Direct transfer error: {type(e).__name__}: {e}")
            return {"success": False, "error": self._sanitize_error(str(e))}

    async def _attempt_disburse(self, wallet_address: str, amount: float, quiz_id: str, attempt: int) -> dict:
        """Single attempt to disburse reward"""
        logger.info(f"Quiz reward attempt {attempt}: {amount} G$ to {wallet_address[:10]}...")

        if not self.owner_account:
            return {"success": False, "error": "Reward system not configured. Please contact support.", "permanent_failure": True}

        if not self._wallet_key:
            return {"success": False, "error": "Reward system not configured. Please contact support.", "permanent_failure": True}

        if not self.contract:
            logger.warning("Smart contract not configured — using direct ERC20 transfer fallback")
            return await self._attempt_direct_transfer(wallet_address, amount, attempt)

        try:
            is_paused = self.contract.functions.paused().call()
            if is_paused:
                return {"success": False, "error": "Reward system is temporarily paused. Please try again later.", "permanent_failure": True}
        except Exception as e:
            logger.warning(f"Paused check failed: {e}")

        balance = await self.get_contract_balance()
        if balance < amount:
            logger.warning(f"Insufficient contract balance: {balance:.2f} G$ < {amount} G$ — falling back to direct ERC20 transfer")
            return await self._attempt_direct_transfer(wallet_address, amount, attempt)

        try:
            already_claimed = self.contract.functions.isQuizRewardClaimed(
                Web3.to_checksum_address(wallet_address),
                quiz_id
            ).call()
            if already_claimed:
                return {"success": False, "error": "Reward already claimed for this quiz.", "permanent_failure": True}
        except Exception as e:
            logger.warning(f"Claim check failed: {e}")

        amount_wei = self._safe_amount_wei(amount)
        logger.info(f"Amount: {amount} G$ = {amount_wei} wei (safe conversion)")

        nonce = self.w3.eth.get_transaction_count(self.owner_account.address, 'pending')
        gas_price = int(self.w3.eth.gas_price * 1.2)

        if attempt > 1:
            gas_price = int(gas_price * (1 + (attempt * 0.1)))
            logger.info(f"Bumped gas price for retry attempt {attempt}")

        try:
            estimated_gas = self.contract.functions.disburseReward(
                Web3.to_checksum_address(wallet_address),
                amount_wei,
                quiz_id
            ).estimate_gas({'from': self.owner_account.address})
            gas_limit = int(estimated_gas * 1.3)
            logger.info(f"Estimated gas: {estimated_gas}, using limit: {gas_limit}")
        except Exception as gas_err:
            logger.warning(f"Gas estimation failed ({gas_err}), using default 500000")
            gas_limit = 500000

        txn = self.contract.functions.disburseReward(
            Web3.to_checksum_address(wallet_address),
            amount_wei,
            quiz_id
        ).build_transaction({
            'chainId': self.chain_id,
            'gas': gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
        })

        signed_txn = self.w3.eth.account.sign_transaction(txn, self._wallet_key)

        logger.info(f"Sending reward transaction (nonce={nonce}, gas={gas_limit}, gasPrice={gas_price})...")
        tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        tx_hash_hex = tx_hash.hex()
        if not tx_hash_hex.startswith('0x'):
            tx_hash_hex = '0x' + tx_hash_hex

        logger.info(f"Transaction sent: {tx_hash_hex}")

        receipt = self._wait_for_receipt(tx_hash)

        if receipt.status == 1:
            logger.info(f"Reward sent successfully: {amount} G$ - TX: {tx_hash_hex} - Block: {receipt.blockNumber} - Gas: {receipt.gasUsed}")
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "amount": amount,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                "gas_used": receipt.gasUsed,
                "block_number": receipt.blockNumber
            }
        else:
            revert_reason = "Unknown"
            try:
                self.w3.eth.call({
                    'to': txn['to'],
                    'from': self.owner_account.address,
                    'data': txn['data'],
                    'value': txn.get('value', 0),
                }, receipt.blockNumber
                )
            except Exception as revert_err:
                revert_reason = str(revert_err)

            logger.error(f"Transaction REVERTED: {tx_hash_hex} - Block: {receipt.blockNumber} - Gas: {receipt.gasUsed}")
            logger.error(f"Revert reason: {revert_reason}")
            logger.error(f"Revert details - Wallet: {wallet_address[:10]}, Amount: {amount} G$ ({amount_wei} wei), QuizID: {quiz_id}")
            return {
                "success": False,
                "error": f"Transaction reverted: {revert_reason}",
                "tx_hash": tx_hash_hex,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}"
            }


    def _build_cfa_abi(self):
        return [
            {
                "inputs": [
                    {"internalType": "address", "name": "token", "type": "address"},
                    {"internalType": "address", "name": "receiver", "type": "address"},
                    {"internalType": "int96", "name": "flowRate", "type": "int96"},
                    {"internalType": "bytes", "name": "ctx", "type": "bytes"}
                ],
                "name": "createFlow",
                "outputs": [{"internalType": "bytes", "name": "newCtx", "type": "bytes"}],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "address", "name": "token", "type": "address"},
                    {"internalType": "address", "name": "sender", "type": "address"},
                    {"internalType": "address", "name": "receiver", "type": "address"},
                    {"internalType": "bytes", "name": "ctx", "type": "bytes"}
                ],
                "name": "deleteFlow",
                "outputs": [{"internalType": "bytes", "name": "newCtx", "type": "bytes"}],
                "stateMutability": "nonpayable",
                "type": "function"
            }
        ]

    async def start_reward_stream(self, receiver_wallet: str, flow_rate_wei: int) -> dict:
        host = os.getenv('SUPERFLUID_HOST_ADDRESS')
        cfa = os.getenv('SUPERFLUID_CFA_V1_ADDRESS')
        token = os.getenv('LEARN_EARN_STREAM_TOKEN_ADDRESS') or os.getenv('GOODDOLLAR_SUPERTOKEN_ADDRESS') or self.gooddollar_address
        if not all([host, cfa, token]):
            return {"success": False, "error": "Superfluid env not configured"}
        if not self.owner_account or not self._wallet_key:
            return {"success": False, "error": "Wallet not configured"}
        try:
            cfa_contract = self.w3.eth.contract(address=Web3.to_checksum_address(cfa), abi=self._build_cfa_abi())
            call_data = cfa_contract.encode_abi('createFlow', args=[
                Web3.to_checksum_address(token),
                Web3.to_checksum_address(receiver_wallet),
                int(flow_rate_wei),
                b''
            ])
            host_abi = [{"inputs":[{"internalType":"address","name":"agreementClass","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"},{"internalType":"bytes","name":"userData","type":"bytes"}],"name":"callAgreement","outputs":[{"internalType":"bytes","name":"returnedData","type":"bytes"}],"stateMutability":"nonpayable","type":"function"}]
            host_contract = self.w3.eth.contract(address=Web3.to_checksum_address(host), abi=host_abi)
            nonce = self.w3.eth.get_transaction_count(self.owner_account.address, 'pending')
            tx = host_contract.functions.callAgreement(
                Web3.to_checksum_address(cfa),
                call_data,
                b''
            ).build_transaction({
                'chainId': self.chain_id,
                'gas': 800000,
                'gasPrice': int(self.w3.eth.gas_price * 1.2),
                'nonce': nonce,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self._wallet_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._wait_for_receipt(tx_hash)
            txh = tx_hash.hex()
            if receipt.status != 1:
                return {"success": False, "error": "createFlow reverted", "tx_hash": txh}
            return {"success": True, "tx_hash": txh, "explorer_url": f"https://celoscan.io/tx/{txh}"}
        except Exception as e:
            logger.error(f"start_reward_stream error: {e}")
            return {"success": False, "error": self._sanitize_error(str(e))}

    async def stop_reward_stream(self, receiver_wallet: str) -> dict:
        host = os.getenv('SUPERFLUID_HOST_ADDRESS')
        cfa = os.getenv('SUPERFLUID_CFA_V1_ADDRESS')
        token = os.getenv('LEARN_EARN_STREAM_TOKEN_ADDRESS') or os.getenv('GOODDOLLAR_SUPERTOKEN_ADDRESS') or self.gooddollar_address
        if not all([host, cfa, token]):
            return {"success": False, "error": "Superfluid env not configured"}
        if not self.owner_account or not self._wallet_key:
            return {"success": False, "error": "Wallet not configured"}
        try:
            cfa_contract = self.w3.eth.contract(address=Web3.to_checksum_address(cfa), abi=self._build_cfa_abi())
            call_data = cfa_contract.encode_abi('deleteFlow', args=[
                Web3.to_checksum_address(token),
                Web3.to_checksum_address(self.owner_account.address),
                Web3.to_checksum_address(receiver_wallet),
                b''
            ])
            host_abi = [{"inputs":[{"internalType":"address","name":"agreementClass","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"},{"internalType":"bytes","name":"userData","type":"bytes"}],"name":"callAgreement","outputs":[{"internalType":"bytes","name":"returnedData","type":"bytes"}],"stateMutability":"nonpayable","type":"function"}]
            host_contract = self.w3.eth.contract(address=Web3.to_checksum_address(host), abi=host_abi)
            nonce = self.w3.eth.get_transaction_count(self.owner_account.address, 'pending')
            tx = host_contract.functions.callAgreement(Web3.to_checksum_address(cfa), call_data, b'').build_transaction({
                'chainId': self.chain_id,
                'gas': 800000,
                'gasPrice': int(self.w3.eth.gas_price * 1.2),
                'nonce': nonce,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self._wallet_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._wait_for_receipt(tx_hash)
            txh = tx_hash.hex()
            if receipt.status != 1:
                return {"success": False, "error": "deleteFlow reverted", "tx_hash": txh}
            return {"success": True, "tx_hash": txh, "explorer_url": f"https://celoscan.io/tx/{txh}"}
        except Exception as e:
            logger.error(f"stop_reward_stream error: {e}")
            return {"success": False, "error": self._sanitize_error(str(e))}

    def _wait_for_receipt(self, tx_hash):
        """
        Wait for transaction receipt with configurable timeout and manual fallback polling.
        Prevents frequent 120s false-failure during temporary Celo congestion.
        """
        try:
            return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.tx_receipt_timeout)
        except TimeExhausted:
            tx_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
            logger.warning(
                f"Receipt timeout after {self.tx_receipt_timeout}s for tx {tx_hex}. "
                "Polling manually for final status..."
            )
            manual_poll_seconds = 60
            deadline = time.time() + manual_poll_seconds
            while time.time() < deadline:
                try:
                    receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                    if receipt:
                        return receipt
                except Exception:
                    pass
                time.sleep(3)
            raise TimeExhausted(
                f"Transaction not mined after {self.tx_receipt_timeout + manual_poll_seconds}s total wait"
            )

    def _sanitize_error(self, error_msg: str) -> str:
        """Remove sensitive info from error messages shown to users"""
        error_lower = error_msg.lower()
        if 'private' in error_lower or 'key' in error_lower:
            return "Configuration error"
        elif 'insufficient' in error_lower:
            return "Rewards pool is currently depleted"
        elif 'nonce' in error_lower:
            return "Transaction conflict, please try again."
        elif 'already processed' in error_lower or 'already claimed' in error_lower:
            return "Reward already claimed for this quiz."
        elif 'timeout' in error_lower or 'timed out' in error_lower:
            return "Network timeout. Please try again."
        elif 'revert' in error_lower or 'execution reverted' in error_lower:
            return "Transaction was rejected by the contract. Please try again."
        else:
            return "Failed to process reward. Please try again."


learn_blockchain_service = LearnBlockchainService()


def disburse_rewards(wallet_address, amount, score):
    """Legacy function for backward compatibility"""
    quiz_id = learn_blockchain_service._generate_quiz_id(wallet_address)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    return loop.run_until_complete(
        learn_blockchain_service.disburse_quiz_reward(wallet_address, amount, quiz_id)
    )


# =========================================================================
# Savings Blockchain Service (from savings/blockchain.py)
# =========================================================================

"""
G$ Savings blockchain service.
All on-chain reads. Withdrawals and deposits happen directly from the user's wallet (frontend).

Contract mechanics (v5 — multi-token, slot-based, custom-duration bonuses):
  - Tokens accepted: G$, CELO, cUSD, USDT (Tether on Celo, 6 decimals).
  - One slot per (user, token, lockDays). Top-ups inherit the slot's
    original unlocksAt (no lock extension).
  - Lock duration: ANY integer day from 1 to 360 (inclusive). No fixed
    preset durations — the user types a custom number of days.
  - Per-token min/max (using each token's NATIVE decimals):
      G$:   1,000        – 10,000,000   (18 decimals)
      CELO: 1            – 100,000      (18 decimals)
      cUSD: 1            – 1,000,000    (18 decimals)
      USDT: 1            – 1,000,000    ( 6 decimals)
  - Per-duration bonus structure (always paid in G$, regardless of
    deposit token; internal contract ratio 1 G$ ≡ 0.001 CELO ≡ 0.001 cUSD ≡
    0.001 USDT):
      1..29-day   → 30 G$        if amount ≥ per-token MIN.
      30..360-day → (lockDays * 500 / 30) G$ if amount ≥ per-token
                     "100k G$ equivalent" (G$ 100,000 / CELO 100 /
                     cUSD 100 / USDT 100). 30d → 500 G$, 60d → 1,000 G$,
                     ..., 360d → 6,000 G$.
      ≥300-day with amount ≥ per-token "1M G$ equivalent"
         (G$ 1,000,000 / CELO 1,000 / cUSD 1,000 / USDT 1,000) REPLACES
         the mid-tier value with a flat 20,000 G$ loyalty bonus.
  - Bonus only paid if reward pool has sufficient G$ (optional / trustless).
  - No owner, no pause, no early withdrawal.

Legacy contracts (read-only):
  - v4 (multi-token, fixed durations [1, 30, ..., 365]). Was the live
    contract before v5; users with active v4 saves can still see and
    withdraw them via the legacy v4 panel.
  - v2 (single-token G$ only, deposit-id based). Frozen permanently.
"""

logger = logging.getLogger(__name__)

SAVINGS_CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
SAVINGS_CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
SAVINGS_CONTRACT_ADDRESS = os.getenv('SAVINGS_CONTRACT_ADDRESS', '')
GD_TOKEN_ADDRESS = os.getenv('GOODDOLLAR_CONTRACT_ADDRESS', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
CELO_TOKEN_ADDRESS = os.getenv('CELO_TOKEN_ADDRESS', '0x471EcE3750Da237f93B8E339c536989b8978a438')
CUSD_TOKEN_ADDRESS = os.getenv('CUSD_TOKEN_ADDRESS', '0x765DE816845861e75A25fCA122bb6898B8B1282a')
# Tether (USD₮) on Celo — 6-decimal ERC-20, not the 18-decimal pattern.
USDT_TOKEN_ADDRESS = os.getenv('USDT_TOKEN_ADDRESS', '0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e')

# Legacy v4 contract — multi-token (G$/CELO/cUSD) savings vault that used
# fixed preset durations. Kept read-only so users with active v4 slots can
# still see and withdraw them in the UI after the v5 redeploy.
LEGACY_V4_CONTRACT_ADDRESS = os.getenv(
    'LEGACY_V4_CONTRACT_ADDRESS',
    '0x78d2a6Dd976337d3bEaFA0c30df6a0fDE949a618',
)

# Legacy v2 contract — frozen-in-place forever, read-only support so users with
# old (single-token, deposit-id-based) saves can still see and withdraw them.
LEGACY_V2_CONTRACT_ADDRESS = '0xF3cca43F5C108d3dEf01Ff1E138866aC1ed00e9c'

# Map of supported tokens, used by the frontend / API to label slots.
# USDT uses 6 decimals; all others are 18. Anywhere we convert raw on-chain
# amounts to human-readable values we must scale by the token's own decimals
# (Web3.from_wei(_, 'ether') would over-divide a USDT balance by 1e12).
SUPPORTED_TOKENS = {
    GD_TOKEN_ADDRESS.lower():   {"symbol": "G$",   "decimals": 18},
    CELO_TOKEN_ADDRESS.lower(): {"symbol": "CELO", "decimals": 18},
    CUSD_TOKEN_ADDRESS.lower(): {"symbol": "cUSD", "decimals": 18},
    USDT_TOKEN_ADDRESS.lower(): {"symbol": "USDT", "decimals":  6},
}


def _token_meta(addr):
    if not addr:
        return {"symbol": "?", "decimals": 18}
    return SUPPORTED_TOKENS.get(addr.lower(), {"symbol": "?", "decimals": 18})


def _raw_to_human(raw, decimals):
    """Scale a raw on-chain integer amount to its human-readable float using
    the token's native decimals. Returns 0.0 on any conversion error so the
    UI never crashes on a malformed value."""
    try:
        d = int(decimals) if decimals is not None else 18
        if d < 0:
            d = 18
        return float(int(raw)) / float(10 ** d)
    except Exception:
        return 0.0


# Common slot-detail tuple shared between v4 and v5 ABIs.
_USER_ACTIVE_SLOTS_OUT = [
    {"internalType": "address[]", "name": "tokens",         "type": "address[]"},
    {"internalType": "uint256[]", "name": "lockDays_",      "type": "uint256[]"},
    {"internalType": "uint256[]", "name": "amounts",        "type": "uint256[]"},
    {"internalType": "uint256[]", "name": "unlocksAts",     "type": "uint256[]"},
    {"internalType": "bool[]",    "name": "areUnlocked",    "type": "bool[]"},
    {"internalType": "bool[]",    "name": "bonusClaimed",   "type": "bool[]"},
    {"internalType": "uint256[]", "name": "pendingBonuses", "type": "uint256[]"},
]

SAVINGS_ABI = [
    # ── Constructor (v5 — 4-token registry) ─────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "_gd",        "type": "address"},
            {"internalType": "address", "name": "_celoToken", "type": "address"},
            {"internalType": "address", "name": "_cusd",      "type": "address"},
            {"internalType": "address", "name": "_usdt",      "type": "address"},
        ],
        "stateMutability": "nonpayable",
        "type": "constructor",
    },
    # ── Write functions ──────────────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "amount",   "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "depositSavings",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "fundRewardPool",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # ── View: slot details ───────────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "user",     "type": "address"},
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "getSlot",
        "outputs": [
            {"internalType": "uint256", "name": "amount",         "type": "uint256"},
            {"internalType": "uint256", "name": "firstDepositAt", "type": "uint256"},
            {"internalType": "uint256", "name": "unlocksAt",      "type": "uint256"},
            {"internalType": "bool",    "name": "bonusClaimed",   "type": "bool"},
            {"internalType": "bool",    "name": "isUnlocked",     "type": "bool"},
            {"internalType": "uint256", "name": "pendingBonus",   "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserSlotRefs",
        "outputs": [
            {
                "components": [
                    {"internalType": "address", "name": "token",    "type": "address"},
                    {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
                ],
                "internalType": "struct GDSavings.SlotRef[]",
                "name": "",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserActiveSlots",
        "outputs": _USER_ACTIVE_SLOTS_OUT,
        "stateMutability": "view",
        "type": "function",
    },
    # ── View: contract stats (v5 — USDT added) ──────────────────────────
    {
        "inputs": [],
        "name": "getContractStats",
        "outputs": [
            {"internalType": "uint256", "name": "totalLockedGd",       "type": "uint256"},
            {"internalType": "uint256", "name": "totalLockedCelo",     "type": "uint256"},
            {"internalType": "uint256", "name": "totalLockedCusd",     "type": "uint256"},
            {"internalType": "uint256", "name": "totalLockedUsdt",     "type": "uint256"},
            {"internalType": "uint256", "name": "rewardPoolBalance",   "type": "uint256"},
            {"internalType": "uint256", "name": "contractGdBalance",   "type": "uint256"},
            {"internalType": "uint256", "name": "contractCeloBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "contractCusdBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "contractUsdtBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "slotsOpenedTotal",    "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # ── View: bonus calculator ───────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "amount",   "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "getBonusAmount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "getMinMax",
        "outputs": [
            {"internalType": "uint256", "name": "minA", "type": "uint256"},
            {"internalType": "uint256", "name": "maxA", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "isAllowedToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    # v5: continuous duration range, not a fixed [1, 30, ..., 365] preset list.
    {
        "inputs": [],
        "name": "getDurationRange",
        "outputs": [
            {"internalType": "uint256", "name": "minDays", "type": "uint256"},
            {"internalType": "uint256", "name": "maxDays", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getTokens",
        "outputs": [
            {"internalType": "address", "name": "gdAddr",   "type": "address"},
            {"internalType": "address", "name": "celoAddr", "type": "address"},
            {"internalType": "address", "name": "cusdAddr", "type": "address"},
            {"internalType": "address", "name": "usdtAddr", "type": "address"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # ── View: state vars ─────────────────────────────────────────────────
    {"inputs": [], "name": "rewardPool",
     "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSlotsOpened",
     "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "gd",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "celoToken",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "cusd",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "usdt",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

# Legacy v4 ABI — only the read functions used by the legacy v4 panel.
# v4 used fixed [1, 30, 60, ..., 365] durations and 3 tokens (G$/CELO/cUSD).
# Withdrawals from v4 are signed directly by the user's wallet on the
# frontend using the matching JS ABI, so we only need the reads here.
LEGACY_V4_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserActiveSlots",
        "outputs": _USER_ACTIVE_SLOTS_OUT,
        "stateMutability": "view",
        "type": "function",
    },
]

# Legacy v2 ABI — only the read functions we need to list a user's old deposits.
# Withdrawals from the v2 contract are signed by the user's wallet on the
# frontend (using the same v2 ABI hardcoded in templates/savings.html), so this
# backend-side ABI does not need to include the `withdraw(uint256)` mutation.
LEGACY_V2_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserDepositIds",
        "outputs": [{"internalType": "uint256[]", "name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "depositId", "type": "uint256"}],
        "name": "getDeposit",
        "outputs": [
            {"internalType": "address", "name": "owner_",        "type": "address"},
            {"internalType": "uint256", "name": "amount",        "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays",      "type": "uint256"},
            {"internalType": "uint256", "name": "depositedAt",   "type": "uint256"},
            {"internalType": "uint256", "name": "unlocksAt",     "type": "uint256"},
            {"internalType": "bool",    "name": "withdrawn",     "type": "bool"},
            {"internalType": "bool",    "name": "bonusClaimed",  "type": "bool"},
            {"internalType": "bool",    "name": "isUnlocked",    "type": "bool"},
            {"internalType": "bool",    "name": "bonusEligible", "type": "bool"},
            {"internalType": "uint256", "name": "pendingBonus",  "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

_SAVINGS_ERC20_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def savings_get_w3():
    return Web3(Web3.HTTPProvider(SAVINGS_CELO_RPC_URL))


def get_savings_contract(w3):
    if not SAVINGS_CONTRACT_ADDRESS:
        raise ValueError("SAVINGS_CONTRACT_ADDRESS not set")
    return w3.eth.contract(
        address=Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS),
        abi=SAVINGS_ABI,
    )


def get_erc20_contract(w3, token_address):
    return w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=_SAVINGS_ERC20_ABI,
    )


def get_gd_contract(w3):
    """Backwards-compatible helper for callers that only need the G$ token."""
    return get_erc20_contract(w3, GD_TOKEN_ADDRESS)


def get_contract_stats():
    """Return high-level stats about the v5 savings vault.

    USDT uses 6 decimals so we must scale its raw values with the token's
    own decimals — calling Web3.from_wei(_, 'ether') on a USDT amount would
    under-report it by a factor of 10¹².
    """
    try:
        w3 = savings_get_w3()
        contract = get_savings_contract(w3)
        s = contract.functions.getContractStats().call()
        (
            total_locked_gd_raw,
            total_locked_celo_raw,
            total_locked_cusd_raw,
            total_locked_usdt_raw,
            reward_pool_raw,
            contract_gd_raw,
            contract_celo_raw,
            contract_cusd_raw,
            contract_usdt_raw,
            slots_opened,
        ) = s
        usdt_decimals = _token_meta(USDT_TOKEN_ADDRESS)["decimals"]
        return {
            "total_locked_gd":       str(total_locked_gd_raw),
            "total_locked_gd_h":     _raw_to_human(total_locked_gd_raw,   18),
            "total_locked_celo":     str(total_locked_celo_raw),
            "total_locked_celo_h":   _raw_to_human(total_locked_celo_raw, 18),
            "total_locked_cusd":     str(total_locked_cusd_raw),
            "total_locked_cusd_h":   _raw_to_human(total_locked_cusd_raw, 18),
            "total_locked_usdt":     str(total_locked_usdt_raw),
            "total_locked_usdt_h":   _raw_to_human(total_locked_usdt_raw, usdt_decimals),
            "reward_pool":           str(reward_pool_raw),
            "reward_pool_gd":        _raw_to_human(reward_pool_raw, 18),
            "contract_gd_balance":   str(contract_gd_raw),
            "contract_celo_balance": str(contract_celo_raw),
            "contract_cusd_balance": str(contract_cusd_raw),
            "contract_usdt_balance": str(contract_usdt_raw),
            "total_slots_opened":    slots_opened,
            "contract_address":      SAVINGS_CONTRACT_ADDRESS,
            "tokens": {
                "gd":   GD_TOKEN_ADDRESS,
                "celo": CELO_TOKEN_ADDRESS,
                "cusd": CUSD_TOKEN_ADDRESS,
                "usdt": USDT_TOKEN_ADDRESS,
            },
        }
    except Exception as e:
        logger.error(f"get_contract_stats error: {e}")
        return None


def _normalize_active_slots(raw_slots):
    """Shared helper for v4 + v5 getUserActiveSlots() responses."""
    (
        tokens,
        lock_days_list,
        amounts,
        unlocks_ats,
        are_unlocked,
        bonus_claimeds,
        pending_bonuses,
    ) = raw_slots

    result = []
    for i in range(len(tokens)):
        token_addr = tokens[i]
        meta = _token_meta(token_addr)
        decimals = meta["decimals"]
        result.append({
            "token":             token_addr,
            "token_symbol":      meta["symbol"],
            "token_decimals":    decimals,
            "lock_days":         int(lock_days_list[i]),
            "amount":            str(amounts[i]),
            "amount_h":          _raw_to_human(amounts[i], decimals),
            "unlocks_at":        int(unlocks_ats[i]),
            "is_unlocked":       bool(are_unlocked[i]),
            "bonus_claimed":     bool(bonus_claimeds[i]),
            "pending_bonus":     str(pending_bonuses[i]),
            # Pending bonus is always paid in G$ (18-decimal) on both v4
            # and v5, regardless of the deposit token.
            "pending_bonus_gd":  _raw_to_human(pending_bonuses[i], 18),
        })
    return result


def get_user_deposits(wallet_address):
    """Return all active slots for a given wallet address.

    Each entry represents one (token, lockDays) slot with its current
    aggregated `amount` and the slot's `unlocks_at` (which never moves
    after the first deposit, even if the user tops up later).
    """
    try:
        w3 = savings_get_w3()
        contract = get_savings_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        raw_slots = contract.functions.getUserActiveSlots(addr).call()
        return _normalize_active_slots(raw_slots)
    except Exception as e:
        logger.error(f"get_user_deposits error: {e}")
        return []


def get_token_allowance(wallet_address, token_address):
    """Check how much `token_address` the user has approved for the savings contract."""
    try:
        w3 = savings_get_w3()
        token = get_erc20_contract(w3, token_address)
        addr = Web3.to_checksum_address(wallet_address)
        savings_addr = Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS)
        return token.functions.allowance(addr, savings_addr).call()
    except Exception as e:
        logger.error(f"get_token_allowance({token_address}) error: {e}")
        return 0


def get_gd_allowance(wallet_address):
    """Backwards-compatible: G$ allowance for the savings contract."""
    return get_token_allowance(wallet_address, GD_TOKEN_ADDRESS)


def get_user_token_balances(wallet_address):
    """Return the user's balances + savings-vault allowances for all
    supported tokens, scaled by each token's own decimals."""
    try:
        w3 = savings_get_w3()
        addr = Web3.to_checksum_address(wallet_address)
        out = {}
        token_map = (
            ("gd",   GD_TOKEN_ADDRESS),
            ("celo", CELO_TOKEN_ADDRESS),
            ("cusd", CUSD_TOKEN_ADDRESS),
            ("usdt", USDT_TOKEN_ADDRESS),
        )
        for key, token_addr in token_map:
            decimals = _token_meta(token_addr)["decimals"]
            try:
                token = get_erc20_contract(w3, token_addr)
                bal = token.functions.balanceOf(addr).call()
                allowance = (
                    token.functions.allowance(
                        addr, Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS)
                    ).call()
                    if SAVINGS_CONTRACT_ADDRESS
                    else 0
                )
                out[key] = {
                    "address":     token_addr,
                    "decimals":    decimals,
                    "balance":     str(bal),
                    "balance_h":   _raw_to_human(bal, decimals),
                    "allowance":   str(allowance),
                    "allowance_h": _raw_to_human(allowance, decimals),
                }
            except Exception as inner:
                logger.warning(f"balance fetch failed for {key}: {inner}")
                out[key] = {
                    "address":     token_addr,
                    "decimals":    decimals,
                    "balance":     "0",
                    "balance_h":   0.0,
                    "allowance":   "0",
                    "allowance_h": 0.0,
                }
        return out
    except Exception as e:
        logger.error(f"get_user_token_balances error: {e}")
        return {}


def get_legacy_contract(w3):
    """The frozen v2 contract (single-token, deposit-id based). Read-only here."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(LEGACY_V2_CONTRACT_ADDRESS),
        abi=LEGACY_V2_ABI,
    )


def get_legacy_v4_contract(w3):
    """The v4 multi-token savings contract — read-only after the v5 redeploy.
    Users with active v4 slots can still withdraw them from the frontend."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(LEGACY_V4_CONTRACT_ADDRESS),
        abi=LEGACY_V4_ABI,
    )


def get_user_legacy_v4_deposits(wallet_address):
    """Return all active v4 slots for the given wallet.

    Same shape as `get_user_deposits` (active-only), so the frontend can
    reuse the same row-rendering logic for the legacy v4 panel. Returns an
    empty list if the wallet never opened a v4 slot or the contract call
    fails (e.g. v4 contract address not configured).
    """
    if not LEGACY_V4_CONTRACT_ADDRESS:
        return []
    try:
        w3 = savings_get_w3()
        contract = get_legacy_v4_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        raw_slots = contract.functions.getUserActiveSlots(addr).call()
        return _normalize_active_slots(raw_slots)
    except Exception as e:
        logger.error(f"get_user_legacy_v4_deposits error: {e}")
        return []


def get_user_legacy_deposits(wallet_address):
    """Return all v2 deposits (old contract) for the given wallet.

    Each entry uses the v2 schema: id, amount (G$ wei), lock_days,
    deposited_at, unlocks_at, withdrawn, bonus_claimed, is_unlocked,
    bonus_eligible, pending_bonus_gd. The frontend renders these in a
    separate, collapsible "Legacy Saves" panel; users can withdraw them
    by signing `withdraw(depositId)` directly to the v2 contract.
    """
    try:
        w3 = savings_get_w3()
        legacy = get_legacy_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        ids = legacy.functions.getUserDepositIds(addr).call()
        result = []
        for dep_id in ids:
            try:
                (
                    _owner,
                    amount_raw,
                    lock_days,
                    deposited_at,
                    unlocks_at,
                    withdrawn,
                    bonus_claimed,
                    is_unlocked,
                    bonus_eligible,
                    pending_bonus_raw,
                ) = legacy.functions.getDeposit(int(dep_id)).call()
            except Exception as inner:
                logger.warning(f"legacy getDeposit({dep_id}) failed: {inner}")
                continue
            result.append({
                "id":               int(dep_id),
                "amount":           str(amount_raw),
                "amount_gd":        float(Web3.from_wei(amount_raw, 'ether')),
                "lock_days":        int(lock_days),
                "deposited_at":     int(deposited_at),
                "unlocks_at":       int(unlocks_at),
                "withdrawn":        bool(withdrawn),
                "bonus_claimed":    bool(bonus_claimed),
                "is_unlocked":      bool(is_unlocked),
                "bonus_eligible":   bool(bonus_eligible),
                "pending_bonus":    str(pending_bonus_raw),
                "pending_bonus_gd": float(Web3.from_wei(pending_bonus_raw, 'ether')),
            })
        return result
    except Exception as e:
        logger.error(f"get_user_legacy_deposits error: {e}")
        return []


# =========================================================================
# Community Stories Blockchain Service (from community_stories/blockchain.py)
# =========================================================================



logger = logging.getLogger(__name__)

def _decode_revert_reason(data: bytes) -> str:
    """Decode revert reason from raw bytes returned by eth_call"""
    try:
        if not data or data == b'':
            return "No revert reason returned"
        if data[:4] == bytes.fromhex('08c379a0'):
            reason = data[4:]
            length = int.from_bytes(reason[32:64], 'big')
            return reason[64:64 + length].decode('utf-8', errors='replace')
        if data[:4] == bytes.fromhex('4e487b71'):
            code = int.from_bytes(data[4:], 'big')
            return f"Panic code {code}"
        return f"Unknown revert data: {data.hex()[:64]}"
    except Exception as e:
        return f"Could not decode revert: {str(e)}"

class CommunityStoriesBlockchain:
    def __init__(self):
        # Blockchain configuration
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_contract = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
        
        # Community Stories wallet key
        self.community_key = os.getenv('COMMUNITY_KEY')
        
        # Debug logging
        logger.info(f"🔍 Checking COMMUNITY_KEY configuration...")
        if self.community_key:
            logger.info(f"✅ COMMUNITY_KEY found (length: {len(self.community_key)})")
        else:
            logger.error("❌ COMMUNITY_KEY not found in environment variables")
        
        # Initialize Web3
        try:
            self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))
            if not self.w3.is_connected():
                logger.error("❌ Failed to connect to Celo network")
                self.enabled = False
            else:
                logger.info("✅ Connected to Celo network for Community Stories")
                self.enabled = True
        except Exception as e:
            logger.error(f"❌ Web3 initialization error: {e}")
            self.enabled = False
        
        # Load community wallet
        if self.community_key and self.enabled:
            try:
                if not self.community_key.startswith('0x'):
                    self.community_key = '0x' + self.community_key
                self.community_account = Account.from_key(self.community_key)
                logger.info(f"✅ Community Stories wallet loaded: {self.community_account.address[:8]}...")
                logger.info(f"💰 Ready to disburse Community Stories rewards!")
            except Exception as e:
                logger.error(f"❌ Error loading community wallet: {e}")
                logger.error(f"🔍 Please check if COMMUNITY_KEY is a valid private key")
                self.enabled = False
        else:
            if not self.community_key:
                logger.error("❌ COMMUNITY_KEY not configured in Secrets")
                logger.error("🔑 Please add COMMUNITY_KEY in Replit Secrets")
            self.enabled = False
    
    async def disburse_reward(self, recipient_wallet: str, amount: float, submission_id: str) -> dict:
        """Disburse Community Stories reward to user"""
        if not self.enabled:
            logger.error(f"❌ Community Stories blockchain service not enabled")
            logger.error(f"🔍 Check COMMUNITY_KEY in Secrets")
            return {
                'success': False,
                'error': 'Community Stories blockchain service not enabled',
                'error_type': 'service_disabled'
            }
        
        try:
            logger.info(f"💰 Disbursing {amount} G$ to {recipient_wallet[:8]}... for submission {submission_id}")
            
            # Check CELO balance for gas
            celo_balance = self.w3.eth.get_balance(self.community_account.address)
            celo_balance_formatted = celo_balance / (10 ** 18)
            min_celo_required = 0.01  # 0.01 CELO minimum
            
            if celo_balance_formatted < min_celo_required:
                logger.error(f"❌ Insufficient CELO for gas: {celo_balance_formatted} CELO < {min_celo_required} CELO")
                return {
                    'success': False,
                    'error': f'Community wallet needs CELO for gas. Current: {celo_balance_formatted:.4f} CELO. Please fund {self.community_account.address} with at least 0.01 CELO.',
                    'error_type': 'insufficient_gas'
                }
            
            # Validate recipient wallet
            if not recipient_wallet or not recipient_wallet.startswith('0x'):
                logger.error(f"❌ Invalid recipient wallet: {recipient_wallet}")
                return {
                    'success': False,
                    'error': 'Invalid recipient wallet address',
                    'error_type': 'invalid_wallet'
                }
            
            # Complete ERC20 ABI (balanceOf + transfer)
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function"
                },
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]
            
            # Create contract instance
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_contract),
                abi=erc20_abi
            )
            
            # Convert amount to wei (18 decimals)
            amount_wei = int(amount * (10 ** 18))
            
            # Check balance
            balance = contract.functions.balanceOf(self.community_account.address).call()
            if balance < amount_wei:
                logger.error(f"❌ Insufficient balance: {balance / (10**18)} G$ < {amount} G$")
                return {
                    'success': False,
                    'error': 'Insufficient balance in community wallet',
                    'error_type': 'insufficient_balance'
                }
            
            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = contract.functions.transfer(
                    Web3.to_checksum_address(recipient_wallet),
                    amount_wei
                ).estimate_gas({'from': self.community_account.address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Community Stories gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            # Build transaction
            tx = contract.functions.transfer(
                Web3.to_checksum_address(recipient_wallet),
                amount_wei
            ).build_transaction({
                'from': self.community_account.address,
                'gas': gas_limit,
                'gasPrice': self.w3.eth.gas_price,
                'nonce': self.w3.eth.get_transaction_count(self.community_account.address),
                'chainId': self.chain_id
            })
            
            # Sign transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.community_key)
            
            # Send transaction
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            
            logger.info(f"✅ Transaction sent: {tx_hash_hex}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt.status == 1:
                logger.info(f"✅ Community Stories reward disbursed successfully!")
                return {
                    'success': True,
                    'tx_hash': tx_hash_hex,
                    'amount': amount,
                    'recipient': recipient_wallet,
                    'explorer_url': f'https://explorer.celo.org/mainnet/tx/{tx_hash_hex}'
                }
            else:
                # Try to decode exact revert reason via eth_call simulation
                revert_reason = "Unknown"
                try:
                    call_data = contract.functions.transfer(
                        Web3.to_checksum_address(recipient_wallet),
                        amount_wei
                    ).build_transaction({
                        'from': self.community_account.address,
                        'gas': 250000,
                        'gasPrice': self.w3.eth.gas_price,
                        'nonce': self.w3.eth.get_transaction_count(self.community_account.address),
                        'chainId': self.chain_id
                    })
                    self.w3.eth.call(call_data, receipt.blockNumber)
                except Exception as call_err:
                    if hasattr(call_err, 'data') and call_err.data:
                        raw = call_err.data
                        if isinstance(raw, str):
                            raw = bytes.fromhex(raw.replace('0x', ''))
                        revert_reason = _decode_revert_reason(raw)
                    else:
                        revert_reason = str(call_err)

                reason_lower = revert_reason.lower()
                if any(k in reason_lower for k in ['balance', 'insufficient', 'funds']):
                    error_type = "insufficient_balance"
                elif any(k in reason_lower for k in ['access', 'owner', 'authorized']):
                    error_type = "access_denied"
                else:
                    error_type = "contract_revert"

                logger.error(f"❌ Community Stories transaction failed [{error_type}]: {revert_reason} | TX: {tx_hash_hex}")
                return {
                    'success': False,
                    'error': f"Transaction failed: {revert_reason}",
                    'error_type': error_type,
                    'revert_reason': revert_reason,
                    'tx_hash': tx_hash_hex,
                    'explorer_url': f'https://explorer.celo.org/mainnet/tx/{tx_hash_hex}'
                }
                
        except Exception as e:
            logger.error(f"❌ Disbursement error: {e}")
            error_msg = str(e)
            
            # Check for specific error types
            if 'insufficient funds' in error_msg.lower():
                return {
                    'success': False,
                    'error': f'Insufficient CELO for gas fees. Please fund Community wallet {self.community_account.address} with at least 0.01 CELO.',
                    'error_type': 'insufficient_gas'
                }
            
            return {
                'success': False,
                'error': error_msg,
                'error_type': 'blockchain_error'
            }

# Global instance
community_stories_blockchain = CommunityStoriesBlockchain()

if __name__ == "__main__":
    test_wallet = "0xFf00A683f7bD77665754A65F2B82fdEFc4371a50"
    result = has_recent_ubi_claim(test_wallet)
    print(result["message"])
