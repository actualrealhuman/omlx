// SPDX-License-Identifier: Apache-2.0

(function (root) {
    'use strict';

    function isQuotaError(error) {
        return error?.name === 'QuotaExceededError'
            || error?.name === 'NS_ERROR_DOM_QUOTA_REACHED'
            || error?.code === 22
            || error?.code === 1014
            || /quota/i.test(String(error?.message || ''));
    }

    function createChatHistoryStorage(storage, key = 'omlx_chat_history') {
        function readRaw() {
            try {
                return { ok: true, raw: storage.getItem(key) };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        return {
            load() {
                const read = readRaw();
                if (!read.ok) return read;
                if (read.raw == null) {
                    return { ok: true, history: [], raw: null };
                }
                try {
                    const history = JSON.parse(read.raw);
                    if (!Array.isArray(history)) {
                        return {
                            ok: false,
                            kind: 'corrupt',
                            raw: read.raw,
                            error: new TypeError('Chat history is not an array'),
                        };
                    }
                    return { ok: true, history, raw: read.raw };
                } catch (error) {
                    return { ok: false, kind: 'corrupt', raw: read.raw, error };
                }
            },

            save(history) {
                let serialized;
                try {
                    serialized = JSON.stringify(history);
                } catch (error) {
                    const previous = readRaw();
                    return {
                        ok: false,
                        kind: 'serialization',
                        error,
                        raw: previous.ok ? previous.raw : null,
                    };
                }

                try {
                    storage.setItem(key, serialized);
                    return { ok: true, serialized };
                } catch (error) {
                    const previous = readRaw();
                    return {
                        ok: false,
                        kind: isQuotaError(error) ? 'quota' : 'unavailable',
                        error,
                        serialized,
                        raw: previous.ok ? previous.raw : null,
                    };
                }
            },

            readRaw,
        };
    }

    root.createChatHistoryStorage = createChatHistoryStorage;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { createChatHistoryStorage, isQuotaError };
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
