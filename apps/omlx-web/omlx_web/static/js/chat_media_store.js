// SPDX-License-Identifier: Apache-2.0

// Content-addressed media codec for the per-chat record store.
//
// Phase 4a of the storage redesign (docs/decisions/0002-chat-storage-redesign.md).
//
// A message part carrying inline base64 — an `image_url` data URL or a `file`
// payload — is replaced by a reference to a digest, and the bytes live in the
// `blobs` store instead of inside the record. Reading resolves the reference back
// to the inline form, so a caller puts and gets the same message it always did.
//
// Why this exists: the template blanked these parts out before persisting, so
// every attachment was silently lost on reload. Content addressing also means the
// same image used in ten chats occupies storage once, and it gives the collector a
// way to tell a live blob from an orphan.
//
// Hashing is asynchronous, so `offloadMessages` must finish before an IndexedDB
// transaction opens. See the transaction discipline note in chat_indexeddb_store.js.
//
// Without a working `crypto.subtle` this codec reports itself unsupported and
// offloads nothing. A weak fallback hash is not acceptable here: a collision would
// silently substitute one user's image for another's, which is worse than not
// offloading at all.

(function (root) {
    'use strict';

    const DIGEST_PREFIX = 'sha256:';
    const DATA_URL_RE = /^data:([^;,]+)?((?:;[^,]*)*),([\s\S]*)$/;

    function toHex(buffer) {
        const bytes = new Uint8Array(buffer);
        let out = '';
        for (let i = 0; i < bytes.length; i += 1) {
            out += bytes[i].toString(16).padStart(2, '0');
        }
        return out;
    }

    // Byte count a base64 string represents, computed without decoding it.
    function base64ByteLength(base64) {
        const clean = String(base64 || '').replace(/\s+/g, '');
        if (!clean) return 0;
        const padding = clean.endsWith('==') ? 2 : (clean.endsWith('=') ? 1 : 0);
        return Math.max(0, Math.floor(clean.length * 3 / 4) - padding);
    }

    function splitDataUrl(url) {
        const match = DATA_URL_RE.exec(String(url || ''));
        if (!match) return null;
        const params = match[2] || '';
        return {
            mime: match[1] || 'application/octet-stream',
            base64: /;base64/i.test(params),
            data: match[3] || '',
        };
    }

    function isRef(value) {
        return !!(value && typeof value === 'object' && typeof value.digest === 'string'
            && value.digest.startsWith(DIGEST_PREFIX));
    }

    function createMediaCodec(options = {}) {
        const cryptoRoot = options.crypto || root.crypto || root.globalThis?.crypto;
        const subtle = cryptoRoot && cryptoRoot.subtle;
        const supported = !!(subtle && typeof subtle.digest === 'function');

        async function digestBase64(base64) {
            if (!supported) return null;
            const binary = atob(String(base64).replace(/\s+/g, ''));
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
            const hash = await subtle.digest('SHA-256', bytes);
            return DIGEST_PREFIX + toHex(hash);
        }

        // Walks every message's content parts. The visitor may return a replacement
        // part, or null to leave the part untouched.
        function visitParts(messages, visitor) {
            const list = Array.isArray(messages) ? messages : [];
            return Promise.all(list.map(async (msg) => {
                if (!msg || typeof msg !== 'object' || !Array.isArray(msg.content)) return msg;
                const content = await Promise.all(msg.content.map((part) => visitor(part, msg)));
                return { ...msg, content };
            }));
        }

        // Replaces inline base64 with digest references. Returns the rewritten
        // messages plus the blobs that must be written for them to resolve.
        //
        // `hasBlob(digest)` is optional; when supplied, a blob already on disk is
        // not rewritten, which keeps a re-save of a large attachment cheap.
        async function offloadMessages(messages, { hasBlob } = {}) {
            const blobs = [];
            const digests = new Set();
            let stripped = 0;
            if (!supported) {
                return { supported: false, messages, blobs, digests, stripped: 0 };
            }

            const rewritten = await visitParts(messages, async (part) => {
                if (!part || typeof part !== 'object') return part;

                if (part.type === 'image_url' && typeof part.image_url?.url === 'string') {
                    const inline = splitDataUrl(part.image_url.url);
                    // A remote http(s) URL is already a reference, not a payload.
                    if (!inline || !inline.base64) return part;
                    const digest = await digestBase64(inline.data);
                    if (!digest) return part;
                    digests.add(digest);
                    if (!hasBlob || !(await hasBlob(digest))) {
                        blobs.push({
                            digest,
                            mime: inline.mime,
                            encoding: 'base64',
                            data: inline.data,
                            bytes: base64ByteLength(inline.data),
                        });
                    }
                    return { ...part, image_url: { digest, mime: inline.mime } };
                }

                if (part.type === 'file' && typeof part.file?.data === 'string' && part.file.data) {
                    const digest = await digestBase64(part.file.data);
                    if (!digest) return part;
                    digests.add(digest);
                    if (!hasBlob || !(await hasBlob(digest))) {
                        blobs.push({
                            digest,
                            mime: part.file.mime_type || 'application/octet-stream',
                            encoding: 'base64',
                            data: part.file.data,
                            bytes: base64ByteLength(part.file.data),
                        });
                    }
                    return {
                        ...part,
                        file: {
                            filename: part.file.filename || '',
                            mime_type: part.file.mime_type || '',
                            digest,
                        },
                    };
                }

                return part;
            });

            return { supported: true, messages: rewritten, blobs, digests, stripped };
        }

        // Resolves digest references back to inline payloads.
        //
        // `getBlob(digest)` returns the stored blob or null. A reference whose blob
        // is gone is reported in `missing` and left with an empty payload rather
        // than throwing: one evicted attachment must not make a whole chat
        // unreadable. The caller decides what to say about it.
        async function resolveMessages(messages, { getBlob } = {}) {
            const missing = [];
            if (!supported || typeof getBlob !== 'function') {
                return { messages, missing };
            }

            const rewritten = await visitParts(messages, async (part, msg) => {
                if (!part || typeof part !== 'object') return part;

                if (part.type === 'image_url' && isRef(part.image_url)) {
                    const blob = await getBlob(part.image_url.digest);
                    if (!blob || typeof blob.data !== 'string') {
                        missing.push({ digest: part.image_url.digest, kind: 'image_url' });
                        return { ...part, image_url: { url: '', digest: part.image_url.digest, mediaMissing: true } };
                    }
                    const mime = blob.mime || part.image_url.mime || 'application/octet-stream';
                    return { ...part, image_url: { url: `data:${mime};base64,${blob.data}`, digest: part.image_url.digest } };
                }

                if (part.type === 'file' && isRef(part.file)) {
                    const blob = await getBlob(part.file.digest);
                    if (!blob || typeof blob.data !== 'string') {
                        missing.push({ digest: part.file.digest, kind: 'file' });
                        return {
                            ...part,
                            file: {
                                filename: part.file.filename || '',
                                mime_type: part.file.mime_type || '',
                                digest: part.file.digest,
                                mediaMissing: true,
                            },
                        };
                    }
                    return {
                        ...part,
                        file: {
                            filename: part.file.filename || blob.filename || '',
                            mime_type: part.file.mime_type || blob.mime || '',
                            data: blob.data,
                            digest: part.file.digest,
                        },
                    };
                }

                return part;
            });

            return { messages: rewritten, missing };
        }

        // Blanks inline media without hashing. Used by backends that have nowhere
        // to put the bytes, so the limitation lives in the backend rather than in
        // every caller.
        function stripMessages(messages) {
            let stripped = 0;
            const list = Array.isArray(messages) ? messages : [];
            const out = list.map((msg) => {
                if (!msg || typeof msg !== 'object' || !Array.isArray(msg.content)) return msg;
                const content = msg.content.map((part) => {
                    if (!part || typeof part !== 'object') return part;
                    if (part.type === 'image_url' && splitDataUrl(part.image_url?.url)?.base64) {
                        stripped += 1;
                        return { ...part, image_url: { url: '', mediaDropped: 'backend' } };
                    }
                    if (part.type === 'file' && part.file?.data) {
                        stripped += 1;
                        return {
                            ...part,
                            file: {
                                filename: part.file?.filename || '',
                                mime_type: part.file?.mime_type || '',
                                data: '',
                                mediaDropped: 'backend',
                            },
                        };
                    }
                    return part;
                });
                return { ...msg, content };
            });
            return { messages: out, stripped };
        }

        // Every digest referenced by a set of records. This is the mark half of
        // mark-and-sweep; the collector must not trust anything else.
        function digestsIn(messages) {
            const found = new Set();
            const list = Array.isArray(messages) ? messages : [];
            for (const msg of list) {
                if (!msg || !Array.isArray(msg.content)) continue;
                for (const part of msg.content) {
                    if (!part || typeof part !== 'object') continue;
                    if (isRef(part.image_url)) found.add(part.image_url.digest);
                    if (isRef(part.file)) found.add(part.file.digest);
                }
            }
            return found;
        }

        return {
            supported,
            digestBase64,
            offloadMessages,
            resolveMessages,
            stripMessages,
            digestsIn,
            splitDataUrl,
            base64ByteLength,
        };
    }

    root.createMediaCodec = createMediaCodec;
    root.MEDIA_DIGEST_PREFIX = DIGEST_PREFIX;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { createMediaCodec, MEDIA_DIGEST_PREFIX, splitDataUrl, base64ByteLength };
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
