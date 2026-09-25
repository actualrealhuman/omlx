// Run with: node --test tests/chat_media_store.test.cjs
//
// Phase 4a: attachments must survive the round trip. Before this the template
// blanked inline base64 before persisting, so every image and file was silently
// lost on reload. These cover the codec, the IndexedDB offload/resolve path, the
// mark-and-sweep collector, and the localStorage backend's documented limitation.
//
// Store-level assertions here run against the fake. The reload case specifically
// needs real Chrome — see check 20 in tests/browser/index.html.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createMediaCodec } = require('../omlx/admin/static/js/chat_media_store.js');
const {
    createIndexedDBRecordStore,
} = require('../omlx/admin/static/js/chat_indexeddb_store.js');
const {
    createLocalStorageRecordStore,
} = require('../omlx/admin/static/js/chat_record_store.js');
const { FakeIndexedDB } = require('./helpers/fake_indexeddb.cjs');

const PNG_B64 = Buffer.from('\x89PNG\r\n\x1a\n-fake-image-bytes').toString('base64');
const OTHER_B64 = Buffer.from('a-different-image').toString('base64');
const TXT_B64 = Buffer.from('file contents here').toString('base64');

const dataUrl = (b64, mime = 'image/png') => `data:${mime};base64,${b64}`;

const imagePart = (b64 = PNG_B64) => ({
    type: 'image_url',
    image_url: { url: dataUrl(b64) },
});
const filePart = (name = 'notes.txt', b64 = TXT_B64) => ({
    type: 'file',
    file: { filename: name, mime_type: 'text/plain', data: b64 },
});

const msg = (role, content) => ({ role, content });

const chat = (id, messages, overrides = {}) => ({
    id,
    title: `Chat ${id}`,
    model: 'test-model',
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-02T00:00:00.000Z',
    messages,
    ...overrides,
});

async function makeStore() {
    const indexedDB = new FakeIndexedDB();
    const store = createIndexedDBRecordStore({ indexedDB, name: 'test-media' });
    await store.open();
    return { indexedDB, store };
}

class MemoryStorage {
    constructor() { this.map = new Map(); }
    getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
    setItem(k, v) { this.map.set(k, String(v)); }
    removeItem(k) { this.map.delete(k); }
    key(i) { return Array.from(this.map.keys())[i] ?? null; }
    get length() { return this.map.size; }
}

// ---- codec ----

test('the codec is supported when crypto.subtle is present', () => {
    const codec = createMediaCodec();
    assert.equal(codec.supported, true);
});

test('digests are content-addressed: same bytes, same digest; different bytes, different', async () => {
    const codec = createMediaCodec();
    const a = await codec.digestBase64(PNG_B64);
    const again = await codec.digestBase64(PNG_B64);
    const b = await codec.digestBase64(OTHER_B64);
    assert.equal(a, again, 'identical bytes must digest identically');
    assert.notEqual(a, b, 'different bytes must not collide');
    assert.match(a, /^sha256:[0-9a-f]{64}$/);
});

test('splitDataUrl recovers mime, encoding, and payload', () => {
    const codec = createMediaCodec();
    const parsed = codec.splitDataUrl(dataUrl(PNG_B64, 'image/jpeg'));
    assert.equal(parsed.mime, 'image/jpeg');
    assert.equal(parsed.base64, true);
    assert.equal(parsed.data, PNG_B64);
    assert.equal(codec.splitDataUrl('https://example.com/a.png'), null);
});

test('base64ByteLength matches the decoded size including padding cases', () => {
    const codec = createMediaCodec();
    for (const text of ['a', 'ab', 'abc', 'abcd', 'abcde', 'hello world']) {
        const b64 = Buffer.from(text).toString('base64');
        assert.equal(codec.base64ByteLength(b64), Buffer.byteLength(text), `for ${JSON.stringify(text)}`);
    }
});

// ---- offload ----

test('an inline image becomes a digest reference and its bytes are handed back for storage', async () => {
    const codec = createMediaCodec();
    const out = await codec.offloadMessages([msg('user', [imagePart()])]);
    const part = out.messages[0].content[0];
    assert.equal(part.image_url.url, undefined, 'the inline payload must not stay in the record');
    assert.match(part.image_url.digest, /^sha256:[0-9a-f]{64}$/);
    assert.equal(out.blobs.length, 1);
    assert.equal(out.blobs[0].data, PNG_B64);
    assert.equal(out.blobs[0].mime, 'image/png');
    assert.equal(out.blobs[0].bytes, Buffer.byteLength('\x89PNG\r\n\x1a\n-fake-image-bytes'));
});

test('a remote image URL is a reference already and is left untouched', async () => {
    const codec = createMediaCodec();
    const remote = { type: 'image_url', image_url: { url: 'https://example.com/cat.png' } };
    const out = await codec.offloadMessages([msg('user', [remote])]);
    assert.deepEqual(out.messages[0].content[0], remote);
    assert.equal(out.blobs.length, 0);
});

test('a file part is offloaded with its filename preserved', async () => {
    const codec = createMediaCodec();
    const out = await codec.offloadMessages([msg('user', [filePart('report.pdf', TXT_B64)])]);
    const part = out.messages[0].content[0];
    assert.equal(part.file.data, undefined);
    assert.equal(part.file.filename, 'report.pdf');
    assert.match(part.file.digest, /^sha256:/);
    assert.equal(out.blobs[0].data, TXT_B64);
});

test('text-only and string-content messages pass through unchanged', async () => {
    const codec = createMediaCodec();
    const out = await codec.offloadMessages([msg('user', 'plain text'), msg('assistant', [{ type: 'text', text: 'hi' }])]);
    assert.equal(out.messages[0].content, 'plain text');
    assert.deepEqual(out.messages[1].content, [{ type: 'text', text: 'hi' }]);
    assert.equal(out.blobs.length, 0);
});

test('digestsIn finds every reference across messages', async () => {
    const codec = createMediaCodec();
    const out = await codec.offloadMessages([
        msg('user', [imagePart(PNG_B64), filePart()]),
        msg('assistant', [{ type: 'text', text: 'no media' }]),
        msg('user', [imagePart(OTHER_B64)]),
    ]);
    const found = codec.digestsIn(out.messages);
    assert.equal(found.size, 3);
});

// ---- IndexedDB round trip ----

test('an image survives put then get with its bytes intact', async () => {
    const { store } = await makeStore();
    const put = await store.put(chat('a', [msg('user', [imagePart()])]));
    assert.equal(put.ok, true);

    const got = await store.get('a');
    assert.equal(got.ok, true);
    assert.equal(got.record.messages[0].content[0].image_url.url, dataUrl(PNG_B64));
    assert.deepEqual(got.mediaMissing, []);
});

test('a file survives put then get with filename, mime, and bytes intact', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [filePart('spec.txt')])]));
    const got = await store.get('a');
    const part = got.record.messages[0].content[0];
    assert.equal(part.file.filename, 'spec.txt');
    assert.equal(part.file.mime_type, 'text/plain');
    assert.equal(part.file.data, TXT_B64);
});

test('the record on disk holds a reference, not the bytes', async () => {
    const { indexedDB, store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart()])]));
    const raw = await store.getBlob(await (async () => {
        const out = await createMediaCodec().offloadMessages([msg('user', [imagePart()])]);
        return out.messages[0].content[0].image_url.digest;
    })());
    assert.equal(raw.ok, true);
    assert.equal(raw.blob.data, PNG_B64);

    // The stored record must not carry the inline payload.
    const db = await store.open();
    const stored = await new Promise((resolve, reject) => {
        const req = db.transaction('chats', 'readonly').objectStore('chats').get('a');
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
    assert.equal(stored.messages[0].content[0].image_url.url, undefined);
    assert.ok(!JSON.stringify(stored).includes(PNG_B64), 'raw base64 leaked into the record');
});

test('the same image used in two chats is stored once', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart()])]));
    const second = await store.put(chat('b', [msg('user', [imagePart()])]));
    assert.equal(second.blobsWritten, 0, 'the shared blob must not be written twice');
    const listed = await store.listBlobs();
    assert.equal(listed.blobs.length, 1);
});

test('a chat mixing text, image, and remote links keeps all of them', async () => {
    const { store } = await makeStore();
    const parts = [
        { type: 'text', text: 'look at this' },
        imagePart(PNG_B64),
        { type: 'image_url', image_url: { url: 'https://example.com/remote.png' } },
        filePart('a.txt'),
    ];
    await store.put(chat('mixed', [msg('user', parts)]));
    const got = await store.get('mixed');
    const back = got.record.messages[0].content;
    assert.equal(back[0].text, 'look at this');
    assert.equal(back[1].image_url.url, dataUrl(PNG_B64));
    assert.equal(back[2].image_url.url, 'https://example.com/remote.png');
    assert.equal(back[3].file.data, TXT_B64);
});

test('a multi-megabyte attachment round-trips', async () => {
    const { store } = await makeStore();
    const big = Buffer.alloc(2 * 1024 * 1024, 0x41).toString('base64');
    await store.put(chat('big', [msg('user', [imagePart(big)])]));
    const got = await store.get('big');
    assert.equal(got.record.messages[0].content[0].image_url.url, dataUrl(big));
    const listed = await store.listBlobs();
    assert.equal(listed.blobs[0].bytes, 2 * 1024 * 1024);
});

// ---- collector ----

test('the collector keeps referenced blobs and removes orphans', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart(PNG_B64)])]));
    await store.put(chat('b', [msg('user', [imagePart(OTHER_B64)])]));

    // An orphan: written by a save whose record never committed.
    const codec = createMediaCodec();
    const orphan = await codec.offloadMessages([msg('user', [imagePart(Buffer.from('orphaned').toString('base64'))])]);
    const orphanDigest = orphan.messages[0].content[0].image_url.digest;
    const db = await store.open();
    await new Promise((resolve, reject) => {
        const tx = db.transaction('blobs', 'readwrite');
        tx.objectStore('blobs').put({ ...orphan.blobs[0], createdAt: new Date().toISOString() });
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
    });

    const swept = await store.collectBlobs();
    assert.equal(swept.ok, true);
    assert.deepEqual(swept.removed, [orphanDigest]);
    assert.equal(swept.kept, 2);
    assert.equal(swept.referenced, 2);

    // Both live attachments still resolve.
    assert.equal((await store.get('a')).record.messages[0].content[0].image_url.url, dataUrl(PNG_B64));
    assert.equal((await store.get('b')).record.messages[0].content[0].image_url.url, dataUrl(OTHER_B64));
});

test('deleting the last chat that referenced a blob makes it collectable', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart()])]));
    assert.equal((await store.listBlobs()).blobs.length, 1);

    await store.remove('a');
    // remove() must not touch blobs directly.
    assert.equal((await store.listBlobs()).blobs.length, 1, 'remove deleted a blob without a sweep');

    const swept = await store.collectBlobs();
    assert.equal(swept.removed.length, 1);
    assert.equal((await store.listBlobs()).blobs.length, 0);
});

test('a blob referenced by only one of several chats survives', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart(PNG_B64)])]));
    await store.put(chat('b', [msg('user', [imagePart(PNG_B64)])]));

    await store.remove('a');
    const swept = await store.collectBlobs();
    assert.deepEqual(swept.removed, [], 'a blob still referenced by chat b was deleted');
    assert.equal((await store.get('b')).record.messages[0].content[0].image_url.url, dataUrl(PNG_B64));
});

test('collecting with no blobs at all is a clean no-op', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', 'text only')]));
    const swept = await store.collectBlobs();
    assert.equal(swept.ok, true);
    assert.deepEqual(swept.removed, []);
    assert.equal(swept.bytesFreed, 0);
});

test('a missing blob is reported and the chat text still reads', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [{ type: 'text', text: 'the conversation' }, imagePart()])]));
    const db = await store.open();
    const listed = await store.listBlobs();
    const digest = listed.blobs[0].digest;
    await new Promise((resolve, reject) => {
        const tx = db.transaction('blobs', 'readwrite');
        tx.objectStore('blobs').delete(digest);
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
    });

    const got = await store.get('a');
    assert.equal(got.ok, true, 'a lost attachment must not make the chat unreadable');
    assert.equal(got.record.messages[0].content[0].text, 'the conversation');
    assert.equal(got.record.messages[0].content[1].image_url.mediaMissing, true);
    assert.equal(got.mediaMissing.length, 1);
    assert.equal(got.mediaMissing[0].digest, digest);
});

// ---- localStorage backend ----

test('the localStorage backend reports that it cannot hold blobs', () => {
    const store = createLocalStorageRecordStore({ storage: new MemoryStorage(), prefix: 't:', indexKey: 'i', metaKey: 'm' });
    assert.equal(store.supportsBlobs, false);
});

test('localStorage blanks inline media and reports how much it dropped', async () => {
    const storage = new MemoryStorage();
    const store = createLocalStorageRecordStore({ storage, prefix: 't:', indexKey: 'i', metaKey: 'm' });
    const put = await store.put(chat('a', [msg('user', [imagePart(), filePart()])]));
    assert.equal(put.ok, true);
    assert.equal(put.mediaStripped, 2);
    assert.ok(!storage.getItem('t:a').includes(PNG_B64), 'base64 was persisted to localStorage');
});

test('localStorage keeps a remote image URL', async () => {
    const storage = new MemoryStorage();
    const store = createLocalStorageRecordStore({ storage, prefix: 't:', indexKey: 'i', metaKey: 'm' });
    const remote = { type: 'image_url', image_url: { url: 'https://example.com/x.png' } };
    const put = await store.put(chat('a', [msg('user', [remote])]));
    assert.equal(put.mediaStripped, 0);
    assert.ok(storage.getItem('t:a').includes('https://example.com/x.png'));
});

test('a text-only chat costs nothing on either backend', async () => {
    const { store } = await makeStore();
    const put = await store.put(chat('a', [msg('user', 'no media here')]));
    assert.equal(put.ok, true);
    assert.equal(put.blobsWritten, 0);
    assert.equal((await store.listBlobs()).blobs.length, 0);
});

// ---- revision safety is preserved alongside media ----

test('a refused write does not leave a dangling record reference', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart(PNG_B64)])]));
    const stale = await store.put(chat('a', [msg('user', [imagePart(OTHER_B64)])]), { expectRev: 99 });
    assert.equal(stale.ok, false);
    assert.equal(stale.kind, 'conflict');

    // The committed record still resolves to the original attachment.
    const got = await store.get('a');
    assert.equal(got.record.messages[0].content[0].image_url.url, dataUrl(PNG_B64));
});

test('clearAll leaves blobs for the collector rather than deleting media directly', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', [msg('user', [imagePart()])]));
    const cleared = await store.clearAll();
    assert.equal(cleared.ok, true);
    assert.equal((await store.listBlobs()).blobs.length, 1, 'clearAll deleted media without a sweep');
    const swept = await store.collectBlobs();
    assert.equal(swept.removed.length, 1);
});
