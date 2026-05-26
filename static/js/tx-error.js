/**
 * GMTxError — shared transaction error formatter for GoodMarket.
 *
 * Converts raw ethers / RPC / WalletConnect errors into concise,
 * user-friendly messages. All templates guard usage behind
 * `window.GMTxError && GMTxError.format` so this is safe to load lazily.
 */
(function () {
    "use strict";

    var USER_REJECTION_PATTERNS = [
        /user rejected/i,
        /user denied/i,
        /user disapproved/i,
        /ACTION_REJECTED/i,
        /rejected/i,
        /cancelled/i,
        /canceled/i,
        /denied by user/i,
    ];

    function _extractMessage(err) {
        if (!err) return "Unknown error";
        if (typeof err === "string") return err;
        return (
            err.shortMessage ||
            err.reason ||
            err.message ||
            (err.data && err.data.message) ||
            (err.error && err.error.message) ||
            String(err)
        );
    }

    function isUserRejection(err) {
        var raw = _extractMessage(err).toLowerCase();
        var code = err && err.code;
        if (code === 4001 || code === "ACTION_REJECTED") return true;
        return USER_REJECTION_PATTERNS.some(function (re) {
            return re.test(raw);
        });
    }

    function format(err) {
        if (!err) return "Transaction failed";
        var raw = _extractMessage(err);

        if (isUserRejection(err)) {
            return "Transaction was rejected in your wallet.";
        }

        if (/insufficient funds/i.test(raw) || /code -32000/i.test(raw)) {
            return "Insufficient funds for gas. Please add CELO to your wallet or wait for gas top-up.";
        }

        if (/nonce too low/i.test(raw)) {
            return "Transaction nonce conflict. Please try again.";
        }

        if (/execution reverted/i.test(raw)) {
            var revertMatch = raw.match(/reason[:\s]*["']?([^"']+)/i);
            if (revertMatch) return "Contract reverted: " + revertMatch[1].trim();
            return "Transaction reverted by the contract. You may have already claimed or the conditions are not met.";
        }

        if (/replacement transaction underpriced/i.test(raw)) {
            return "A previous transaction is still pending. Please wait or increase gas.";
        }

        if (/could not coalesce/i.test(raw)) {
            return "Wallet communication error. Please try again.";
        }

        if (/<html/i.test(raw) || /<!doctype/i.test(raw)) {
            return "Network is temporarily busy (non-JSON RPC response). Please retry in 30–60 seconds.";
        }

        // Strip verbose ethers.js wrapping
        var cleaned = raw
            .replace(/^Error:\s*/i, "")
            .replace(/\(action="[^"]*",\s*reason="[^"]*".*\)/g, "")
            .trim();
        if (cleaned.length > 200) cleaned = cleaned.substring(0, 200) + "…";
        return cleaned || "Transaction failed";
    }

    window.GMTxError = {
        format: format,
        isUserRejection: isUserRejection,
    };
})();
