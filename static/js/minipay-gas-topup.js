/**
 * MPGasTopUp — MiniPay gas pre-flight helper for GoodMarket.
 *
 * MiniPay uses CIP-64 fee abstraction and pays gas in stablecoins
 * (cUSD/USDT/USDC), not in native CELO. This module detects MiniPay,
 * checks stablecoin balances, and ensures the wallet has enough gas
 * before claim/send/swap transactions.
 *
 * No-op when running outside MiniPay.
 */
(function () {
    "use strict";

    var _forceMiniPay = false;

    // Stablecoin contract addresses on Celo mainnet
    var CUSD  = "0x765DE816845861e75A25fCA122bb6898B8B1282a";
    var USDT  = "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e";
    var USDC  = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C";
    // Min stablecoin balance considered sufficient for gas (~0.01 USD)
    var MIN_STABLE_WEI = 10000000000000000n; // 0.01e18

    function isMiniPay() {
        if (_forceMiniPay) return true;
        if (typeof window.ethereum !== "undefined" && window.ethereum && window.ethereum.isMiniPay) return true;
        if (typeof window.ethereum !== "undefined" && window.ethereum && window.ethereum.providers) {
            if (window.ethereum.providers.some(function (p) { return p && p.isMiniPay; })) return true;
        }
        if (typeof navigator !== "undefined" && /minipay/i.test(navigator.userAgent || "")) return true;
        return false;
    }

    function setMiniPayDetected() {
        _forceMiniPay = true;
    }

    // Read on-chain ERC-20 balance via eth_call (balanceOf)
    async function _erc20Balance(rpc, token, wallet) {
        var data = "0x70a08231" + wallet.replace("0x", "").toLowerCase().padStart(64, "0");
        var resp = await fetch(rpc, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                jsonrpc: "2.0", id: 1,
                method: "eth_call",
                params: [{ to: token, data: data }, "latest"],
            }),
        });
        var json = await resp.json();
        if (json.error || !json.result) return 0n;
        return BigInt(json.result);
    }

    async function _celoBalance(rpc, wallet) {
        var resp = await fetch(rpc, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                jsonrpc: "2.0", id: 1,
                method: "eth_getBalance",
                params: [wallet, "latest"],
            }),
        });
        var json = await resp.json();
        if (json.error || !json.result) return 0n;
        return BigInt(json.result);
    }

    async function getBalances(wallet) {
        var rpc = "https://forno.celo.org";
        var results = await Promise.all([
            _celoBalance(rpc, wallet),
            _erc20Balance(rpc, CUSD, wallet),
            _erc20Balance(rpc, USDT, wallet),
            _erc20Balance(rpc, USDC, wallet),
        ]);
        return {
            celo: results[0],
            cusd: results[1],
            usdt: results[2],
            usdc: results[3],
        };
    }

    function hasStablecoinGasBalance(balances) {
        if (!balances) return false;
        return (
            (balances.cusd && balances.cusd >= MIN_STABLE_WEI) ||
            (balances.usdt && balances.usdt >= MIN_STABLE_WEI) ||
            (balances.usdc && balances.usdc >= MIN_STABLE_WEI)
        );
    }

    /**
     * Ensure the wallet has enough stablecoin gas for a MiniPay tx.
     * Returns { proceed: true/false, ... }.
     */
    async function ensureToppedUp(wallet, opts) {
        if (!isMiniPay()) return { proceed: true };

        opts = opts || {};
        try {
            var balances = await getBalances(wallet);
            if (hasStablecoinGasBalance(balances)) {
                return { proceed: true };
            }

            // Check if user has enough CELO to swap
            var CELO_SWAP_MIN = 90000000000000000n; // 0.09 CELO
            var hasCeloToSwap = balances.celo && balances.celo > CELO_SWAP_MIN;

            if (!hasCeloToSwap) {
                // Try the backend gas faucet
                try {
                    var faucetResp = await fetch("/api/faucet/gas", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ wallet: wallet }),
                    });
                    var faucetData = await faucetResp.json();
                    if (faucetData && faucetData.success) {
                        // Re-check balances after faucet
                        await new Promise(function (r) { setTimeout(r, 3000); });
                        balances = await getBalances(wallet);
                        if (hasStablecoinGasBalance(balances)) {
                            return { proceed: true };
                        }
                    }
                    if (faucetData && faucetData.cooldown) {
                        return {
                            proceed: false,
                            cooldown: true,
                            cooldownSeconds: faucetData.cooldown_seconds || 0,
                        };
                    }
                } catch (_faucetErr) {
                    console.warn("[MPGasTopUp] faucet call failed:", _faucetErr);
                }

                return {
                    proceed: false,
                    insufficientGas: true,
                    error: "Wallet has insufficient stablecoin gas and not enough CELO to swap.",
                };
            }

            // User has CELO but no stablecoin — if stableGasOnly or skipAutoSwap,
            // just try faucet and return
            if (opts.stableGasOnly || opts.skipAutoSwap) {
                try {
                    var fResp = await fetch("/api/faucet/gas", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ wallet: wallet }),
                    });
                    var fData = await fResp.json();
                    if (fData && fData.success) {
                        await new Promise(function (r) { setTimeout(r, 3000); });
                        return { proceed: true };
                    }
                } catch (_) {}
                return { proceed: true };
            }

            // Auto-swap CELO -> cUSD for gas
            try {
                if (!window.ethereum) return { proceed: false, error: "No wallet provider for auto-swap." };
                var provider = window.ethereum;

                // Prepare minimal CELO -> cUSD swap via backend
                var swapResp = await fetch("/api/wallet/prepare-send", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        token: "CELO",
                        to: wallet,
                        amount: "0.05",
                        action: "gas_swap",
                    }),
                });
                var swapData = await swapResp.json();
                if (swapData && swapData.success && swapData.to && swapData.data) {
                    var txHash = await provider.request({
                        method: "eth_sendTransaction",
                        params: [{
                            from: wallet,
                            to: swapData.to,
                            data: swapData.data,
                            value: swapData.value || "0x0",
                        }],
                    });
                    if (txHash) {
                        await new Promise(function (r) { setTimeout(r, 5000); });
                        return { proceed: true, swapped: true };
                    }
                }
            } catch (swapErr) {
                var isReject = /reject|denied|cancel/i.test(String(swapErr && swapErr.message));
                if (isReject) return { proceed: false, cancelled: true };
                console.warn("[MPGasTopUp] auto-swap failed:", swapErr);
            }

            // Fallback: still try to proceed
            return { proceed: true };
        } catch (outerErr) {
            console.warn("[MPGasTopUp] ensureToppedUp error:", outerErr);
            throw outerErr;
        }
    }

    window.MPGasTopUp = {
        isMiniPay: isMiniPay,
        setMiniPayDetected: setMiniPayDetected,
        getBalances: getBalances,
        hasStablecoinGasBalance: hasStablecoinGasBalance,
        ensureToppedUp: ensureToppedUp,
    };
})();
