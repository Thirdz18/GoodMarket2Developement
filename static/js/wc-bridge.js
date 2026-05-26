/**
 * GMWalletConnect — shared WalletConnect bridge for GoodMarket pages.
 *
 * Provides an EIP-1193-compatible provider for users that logged in via
 * WalletConnect (QR / deep-link) and have no injected `window.ethereum`.
 *
 * Strategy:
 *  1. Try restoring an in-browser SignClient session left by homepage login.
 *  2. Try the Node.js sidecar service (`/api/wc-*` proxy routes).
 *  3. Fall back to creating a fresh in-browser SignClient session.
 *
 * Public API consumed by wallet.html, dashboard.html, learn_and_earn.html,
 * savings.html, swap.html, send-link.html, reloadly.html, xdc_wallet.html:
 *
 *   GMWalletConnect.configure(opts)
 *   GMWalletConnect.isPreferred()   → boolean
 *   GMWalletConnect.getProvider()   → Promise<EIP1193Provider>
 *   GMWalletConnect.isConnected()   → boolean
 *   GMWalletConnect.connect()       → Promise<address>
 *   GMWalletConnect.bridgeRequest(method, params) → Promise<any>
 */
(function () {
    "use strict";

    // ── Internal state ────────────────────────────────────────
    var _cfg = {
        walletAddress: "",
        loginMethod: "",
        projectId: "",
        sidecarEnabled: true,
        dappName: "GoodMarket",
        dappDescription: "Claim and manage GoodDollar on Celo",
        assetVersion: "",
        showQr: null,
        hideQr: null,
    };

    var _mode = null;           // "sidecar" | "browser"
    var _sidecarSessionId = null;
    var _browserSession = null;
    var _signClient = null;
    var _sdkLoading = null;
    var _wcAddress = null;
    var _providerInstance = null;

    var WC_LOGIN_METHODS = ["walletconnect", "manual", "manual_address"];

    // ── Helpers ───────────────────────────────────────────────

    function _rpcError(message, code, data) {
        var err = new Error(message);
        err.code = code || -32603;
        if (data !== undefined) err.data = data;
        return err;
    }

    function _delay(ms) {
        return new Promise(function (r) { setTimeout(r, ms); });
    }

    function _isMobileBrowser() {
        try {
            return /android|iphone|ipad|ipod|mobile/i.test(navigator.userAgent || "");
        } catch (_) { return false; }
    }

    function _wakeWalletApp(session) {
        try {
            if (!session || !_isMobileBrowser()) return;
            var meta = session.peer && session.peer.metadata;
            var redirect = meta && meta.redirect;
            if (!redirect) return;
            var href = redirect.native || redirect.universal;
            if (!href) return;
            var link = document.createElement("a");
            link.href = href;
            link.style.display = "none";
            link.target = "_self";
            link.rel = "noopener noreferrer";
            document.body.appendChild(link);
            link.click();
            setTimeout(function () { try { link.remove(); } catch (_) {} }, 100);
        } catch (_) { /* no-op */ }
    }

    // ── WalletConnect SDK loader ──────────────────────────────

    function _loadScript(src) {
        return new Promise(function (resolve, reject) {
            var s = document.createElement("script");
            s.src = src;
            s.async = true;
            s.onload = resolve;
            s.onerror = reject;
            document.head.appendChild(s);
        });
    }

    function _loadSdk() {
        if (_sdkLoading) return _sdkLoading;
        _sdkLoading = new Promise(function (resolve, reject) {
            // Check if already available globally
            var SC = (window["@walletconnect/sign-client"] || {}).SignClient;
            if (SC) { resolve(SC); return; }

            var unpkgSrc = "https://unpkg.com/@walletconnect/sign-client@2/dist/index.umd.js";
            var cdnSrc = "https://cdn.jsdelivr.net/npm/@walletconnect/sign-client@2/dist/index.umd.js";
            _loadScript(unpkgSrc).then(function () {
                var SC2 = (window["@walletconnect/sign-client"] || {}).SignClient;
                SC2 ? resolve(SC2) : reject(new Error("WalletConnect SDK unavailable"));
            }).catch(function () {
                _loadScript(cdnSrc).then(function () {
                    var SC2 = (window["@walletconnect/sign-client"] || {}).SignClient;
                    SC2 ? resolve(SC2) : reject(new Error("WalletConnect SDK unavailable"));
                }).catch(function () {
                    reject(new Error("WalletConnect SDK unavailable"));
                });
            });
        });
        return _sdkLoading;
    }

    function _getClient() {
        if (_signClient) return Promise.resolve(_signClient);
        if (!_cfg.projectId) {
            return Promise.reject(new Error("WALLETCONNECT_PROJECT_ID is not configured"));
        }
        return _loadSdk().then(function (SignClient) {
            return SignClient.init({
                projectId: _cfg.projectId,
                metadata: {
                    name: _cfg.dappName || "GoodMarket",
                    description: _cfg.dappDescription || "Claim and manage GoodDollar on Celo",
                    url: window.location.origin,
                    icons: [
                        window.location.origin +
                        "/static/icons/icon-192x192.png" +
                        (_cfg.assetVersion ? "?v=" + _cfg.assetVersion : ""),
                    ],
                },
            });
        }).then(function (client) {
            _signClient = client;
            window._wcSignClient = client;
            _tryRestoreBrowserSession();
            return client;
        });
    }

    function _tryRestoreBrowserSession() {
        try {
            if (_browserSession) return;
            if (!_signClient || !_signClient.session || !_signClient.session.getAll) return;
            var sessions = _signClient.session.getAll();
            if (!sessions || !sessions.length) return;
            var restored = sessions[sessions.length - 1];
            _browserSession = restored;
            _mode = "browser";
            if (window._wcSession === undefined || window._wcSession === null) {
                try { window._wcSession = restored; } catch (_) {}
            }
            var ns = restored.namespaces || {};
            Object.keys(ns).some(function (key) {
                var accts = (ns[key] && ns[key].accounts) || [];
                if (accts.length) {
                    _wcAddress = String(accts[0]).split(":").pop();
                    return true;
                }
                return false;
            });
        } catch (_) { /* no-op */ }
    }

    // ── Sidecar connection ────────────────────────────────────

    async function _sidecarConnect() {
        var uriData;
        try {
            var uriResp = await fetch("/api/wc-uri");
            if (!uriResp.ok) throw new Error("sidecar HTTP " + uriResp.status);
            uriData = await uriResp.json();
        } catch (_) {
            var e = new Error("sidecar-unavailable");
            e._sidecarUnavailable = true;
            throw e;
        }
        if (!uriData || !uriData.success || !uriData.id || !uriData.uri) {
            var e2 = new Error("sidecar-unavailable");
            e2._sidecarUnavailable = true;
            throw e2;
        }
        _sidecarSessionId = uriData.id;
        _mode = "sidecar";

        if (typeof _cfg.showQr === "function") {
            try { _cfg.showQr(uriData.uri); } catch (_) {}
        }

        for (var i = 0; i < 60; i++) {
            await _delay(2000);
            var stResp = await fetch("/api/wc-session/" + encodeURIComponent(_sidecarSessionId));
            var stData = await stResp.json();
            if (!stResp.ok || !stData.success) continue;
            if (stData.status === "approved" && stData.address) {
                _wcAddress = stData.address;
                if (typeof _cfg.hideQr === "function") {
                    try { _cfg.hideQr(); } catch (_) {}
                }
                return _wcAddress;
            }
            if (stData.status === "rejected") {
                throw _rpcError("WalletConnect request was rejected.", 4001);
            }
        }
        throw _rpcError("WalletConnect approval timed out.", -32603);
    }

    // ── Browser (in-page SignClient) connection ───────────────

    async function _browserConnect() {
        var client = await _getClient();
        if (_browserSession && _wcAddress) {
            _mode = "browser";
            return _wcAddress;
        }
        var result = await client.connect({
            requiredNamespaces: {},
            optionalNamespaces: {
                eip155: {
                    methods: [
                        "eth_accounts",
                        "eth_sendTransaction",
                        "eth_getTransactionReceipt",
                        "eth_chainId",
                        "personal_sign",
                        "eth_sign",
                    ],
                    chains: ["eip155:42220"],
                    events: ["chainChanged", "accountsChanged"],
                },
            },
        });
        _mode = "browser";
        if (result.uri) {
            if (typeof _cfg.showQr === "function") {
                try { _cfg.showQr(result.uri); } catch (_) {}
            }
        }
        _browserSession = await result.approval();
        try { window._wcSession = _browserSession; } catch (_) {}
        var ns = _browserSession.namespaces || {};
        Object.keys(ns).some(function (key) {
            var accts = (ns[key] && ns[key].accounts) || [];
            if (accts.length) {
                _wcAddress = String(accts[0]).split(":").pop();
                return true;
            }
            return false;
        });
        if (typeof _cfg.hideQr === "function") {
            try { _cfg.hideQr(); } catch (_) {}
        }
        if (!_wcAddress) throw _rpcError("No accounts returned from wallet", -32603);
        return _wcAddress;
    }

    // ── Combined connect ──────────────────────────────────────

    async function _doConnect() {
        if (_wcAddress) return _wcAddress;

        // 1. Try restoring an existing browser session
        try {
            await _getClient();
            if (_browserSession && _wcAddress) {
                _mode = "browser";
                return _wcAddress;
            }
        } catch (restoreErr) {
            if (restoreErr && restoreErr.code === 4001) throw restoreErr;
            console.warn("[wc-bridge] session restore failed:", restoreErr);
        }

        // 2. Try sidecar
        if (_cfg.sidecarEnabled) {
            try {
                return await _sidecarConnect();
            } catch (sErr) {
                if (!sErr || !sErr._sidecarUnavailable) throw sErr;
                console.warn("[wc-bridge] sidecar unavailable; using browser fallback.");
            }
        }

        // 3. In-browser SignClient
        return await _browserConnect();
    }

    // ── Bridge request (EIP-1193 style) ───────────────────────

    async function _bridgeRequest(method, params) {
        if (method === "eth_accounts" || method === "eth_requestAccounts") {
            if (!_wcAddress) await _doConnect();
            return _wcAddress ? [_wcAddress] : [];
        }

        if (method === "eth_chainId") return "0xa4ec";     // Celo
        if (method === "net_version") return "42220";

        if (method === "wallet_switchEthereumChain" || method === "wallet_addEthereumChain") {
            return null;
        }

        if (method === "eth_sendTransaction") {
            if (!_wcAddress) await _doConnect();
            var txParams = (params && params[0]) ? params[0] : {};

            if (_mode === "sidecar" && _sidecarSessionId) {
                var txResp = await fetch(
                    "/api/wc-tx/" + encodeURIComponent(_sidecarSessionId),
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(txParams),
                    }
                );
                var txData = await txResp.json();
                if (!txResp.ok || !txData.success || txData.error || !txData.txHash) {
                    var rawErr = (txData && txData.error) ? String(txData.error) : "WalletConnect transaction failed";
                    var isReject = /user rejected|user denied|user disapproved|rejected|cancelled|canceled/i.test(rawErr);
                    throw _rpcError(rawErr, isReject ? 4001 : -32603);
                }
                return txData.txHash;
            }

            if (!_browserSession) {
                throw _rpcError("WalletConnect browser session is not active.", -32603);
            }
            var client = await _getClient();
            var reqPromise = client.request({
                topic: _browserSession.topic,
                chainId: "eip155:42220",
                request: { method: "eth_sendTransaction", params: [txParams] },
            });
            _wakeWalletApp(_browserSession);
            var txHash;
            try {
                txHash = await reqPromise;
            } catch (wcErr) {
                var code = (wcErr && typeof wcErr.code === "number") ? wcErr.code : -32603;
                var msg = (wcErr && wcErr.message) ? String(wcErr.message) : "WalletConnect transaction failed";
                throw _rpcError(msg, code);
            }
            if (!txHash) throw _rpcError("WalletConnect transaction failed", -32603);
            return txHash;
        }

        if (method === "personal_sign" || method === "eth_sign") {
            if (!_wcAddress) await _doConnect();

            if (_mode === "sidecar" && _sidecarSessionId) {
                var signParams = params || [];
                var message = signParams[0] || "";
                var address = signParams[1] || _wcAddress;
                if (method === "personal_sign" && message && /^0x[0-9a-f]/i.test(String(address)) && !/^0x[0-9a-f]{40}$/i.test(String(message))) {
                    // personal_sign(message, address) — already correct order
                } else if (method === "personal_sign" && /^0x[0-9a-f]{40}$/i.test(String(message))) {
                    // personal_sign(address, message) — swap
                    var tmp = message;
                    message = address;
                    address = tmp;
                }
                var signResp = await fetch(
                    "/api/wc-sign/" + encodeURIComponent(_sidecarSessionId),
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ message: message, address: address }),
                    }
                );
                var signData = await signResp.json();
                if (!signResp.ok || !signData.success || signData.error) {
                    throw _rpcError(
                        (signData && signData.error) || "WalletConnect signing failed",
                        4001
                    );
                }
                return signData.signature || signData.result;
            }

            if (!_browserSession) {
                throw _rpcError("WalletConnect browser session is not active.", -32603);
            }
            var signClient = await _getClient();
            var signReqPromise = signClient.request({
                topic: _browserSession.topic,
                chainId: "eip155:42220",
                request: { method: method, params: params },
            });
            _wakeWalletApp(_browserSession);
            try {
                return await signReqPromise;
            } catch (signErr) {
                var sCode = (signErr && typeof signErr.code === "number") ? signErr.code : -32603;
                var sMsg = (signErr && signErr.message) ? String(signErr.message) : "WalletConnect signing failed";
                throw _rpcError(sMsg, sCode);
            }
        }

        // For any other RPC call, forward to the Celo public RPC
        try {
            var rpcResp = await fetch("https://forno.celo.org", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: method, params: params || [] }),
            });
            var rpcData = await rpcResp.json();
            if (rpcData.error) throw _rpcError(rpcData.error.message || "RPC error", rpcData.error.code);
            return rpcData.result;
        } catch (rpcErr) {
            if (rpcErr && rpcErr.code) throw rpcErr;
            throw _rpcError(String(rpcErr), -32603);
        }
    }

    // ── EIP-1193 provider wrapper ─────────────────────────────

    function _buildProvider() {
        if (_providerInstance) return _providerInstance;
        _providerInstance = {
            isWalletConnect: true,
            request: function (args) {
                var method = args && args.method;
                var params = args && args.params;
                return _bridgeRequest(method, params);
            },
        };
        return _providerInstance;
    }

    // ── Public API ────────────────────────────────────────────

    window.GMWalletConnect = {
        configure: function (opts) {
            if (!opts) return;
            if (opts.walletAddress !== undefined) _cfg.walletAddress = opts.walletAddress;
            if (opts.loginMethod !== undefined) _cfg.loginMethod = opts.loginMethod;
            if (opts.projectId !== undefined) _cfg.projectId = opts.projectId;
            if (opts.sidecarEnabled !== undefined) _cfg.sidecarEnabled = !!opts.sidecarEnabled;
            if (opts.dappName !== undefined) _cfg.dappName = opts.dappName;
            if (opts.dappDescription !== undefined) _cfg.dappDescription = opts.dappDescription;
            if (opts.assetVersion !== undefined) _cfg.assetVersion = opts.assetVersion;
            if (typeof opts.showQr === "function") _cfg.showQr = opts.showQr;
            if (typeof opts.hideQr === "function") _cfg.hideQr = opts.hideQr;

            // Pre-set _wcAddress from the server-rendered wallet if the user
            // logged in via WalletConnect so getProvider() can return
            // accounts immediately without an extra connect() round-trip.
            if (
                _cfg.walletAddress &&
                WC_LOGIN_METHODS.indexOf((_cfg.loginMethod || "").toLowerCase()) !== -1
            ) {
                _wcAddress = _cfg.walletAddress;
            }

            // Warm up the SignClient on configure() so any existing session
            // from the homepage login is restored before the user clicks
            // Claim / Send / Sign.
            if (_cfg.projectId && !_signClient) {
                setTimeout(function () {
                    _getClient().catch(function () { /* no-op */ });
                }, 800);
            }
        },

        isPreferred: function () {
            var login = (_cfg.loginMethod || "").toLowerCase();
            if (WC_LOGIN_METHODS.indexOf(login) === -1) return false;
            // Prefer WalletConnect bridge when there is no injected provider
            if (typeof window.ethereum !== "undefined" && window.ethereum) {
                // Exception: if ethereum is shimmed but not a real provider
                // (e.g., a proxy that does not support request()), still
                // prefer WC.
                if (typeof window.ethereum.request === "function") return false;
            }
            return true;
        },

        getProvider: function () {
            // Synchronous — returns the cached EIP-1193 provider immediately.
            // The provider's request() is async and will connect on first use.
            return _buildProvider();
        },

        isConnected: function () {
            return !!_wcAddress;
        },

        connect: function () {
            return _doConnect();
        },

        bridgeRequest: function (method, params) {
            return _bridgeRequest(method, params);
        },
    };
})();
