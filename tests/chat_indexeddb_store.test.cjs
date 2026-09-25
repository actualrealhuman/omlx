// Run with: node --test tests/chat_indexeddb_store.test.cjs
//
// These run against tests/helpers/fake_indexeddb.cjs, which models transaction
// atomicity and quota errors. They prove the interface contract and the
// compare-and-set logic. Real-browser IndexedDB behaviour still needs separate
// verification.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
    createIndexedDBRecordStore,
    isIndexedDBAvailable,
    browserStorageQuota,
} = require('../omlx/admin/static/js/chat_indexeddb_store.js');
const { FakeIndexedDB } = require('./helpers/fake_indexeddb.cjs');

async function makeStore() {
    const indexedDB = new FakeIndexedDB();
    const store = createIndexedDBRecordStore({ indexedDB, name: 'test-chat' });
    await store.open();
    return { indexedDB, store };
}

const chat = (id, overrides = {}) => ({
    id,
    title: `Chat ${id}`,
    model: 'test-model',
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-02T00:00:00.000Z',
    messages: [{ role: 'user', content: `hello ${id}` }],
    ...overrides,
});

test('opens the database with the full schema', async () => {
    const { store } = await makeStore();
    const db = (await store.open());
    for (const name of ['chats', 'chatIndex', 'meta', 'blobs']) {
        assert.ok(db.objectStoreNames.contains(name), `missing store ${name}`);
    }
    assert.equal(store.backend, 'indexedDB');
});

test('put then get round-trips and stamps rev 1', async () => {
    const { store } = await makeStore();
    const put = await store.put(chat('a'));
    assert.equal(put.ok, true);
    assert.equal(put.rev, 1);

    const got = await store.get('a');
    assert.equal(got.ok, true);
    assert.equal(got.record.title, 'Chat a');
    assert.equal(got.record.rev, 1);
});

test('successive puts bump rev monotonically', async () => {
    const { store } = await makeStore();
    assert.equal((await store.put(chat('a'))).rev, 1);
    assert.equal((await store.put(chat('a', { title: 'v2' }))).rev, 2);
    assert.equal((await store.put(chat('a', { title: 'v3' }))).rev, 3);
    assert.equal((await store.get('a')).record.rev, 3);
});

test('a stale expectRev is refused and the transaction leaves the store untouched', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', { title: 'committed' }));
    await store.put(chat('a', { title: 'other tab' }));

    const conflict = await store.put(chat('a', { title: 'my stale edit' }), { expectRev: 1 });

    assert.equal(conflict.ok, false);
    assert.equal(conflict.kind, 'conflict');
    assert.equal(conflict.expectedRev, 1);
    assert.equal(conflict.currentRev, 2);
    assert.equal(conflict.current.title, 'other tab');

    // The abort must have rolled back both the record and the index write.
    assert.equal((await store.get('a')).record.title, 'other tab');
    const listed = await store.listIndex();
    const entry = listed.entries.find((e) => e.id === 'a');
    assert.equal(entry.title, 'other tab');
    assert.equal(entry.rev, 2);
});

test('a matching expectRev commits', async () => {
    const { store } = await makeStore();
    await store.put(chat('a'));
    const ok = await store.put(chat('a', { title: 'mine' }), { expectRev: 1 });
    assert.equal(ok.ok, true);
    assert.equal(ok.rev, 2);
});

test('the record and its index commit together, with no degraded state', async () => {
    const { store } = await makeStore();
    const put = await store.put(chat('a', { messages: [{ role: 'user' }, { role: 'assistant' }] }));

    assert.equal(put.ok, true);
    assert.equal(put.indexDegraded, undefined);

    const listed = await store.listIndex();
    const entry = listed.entries.find((e) => e.id === 'a');
    assert.equal(entry.rev, 1);
    assert.equal(entry.messageCount, 2);
    assert.ok(entry.bytes > 0);
});

test('listIndex returns metadata only, never message bodies', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', { messages: [{ role: 'user', content: 'x'.repeat(5000) }] }));

    const listed = await store.listIndex();
    assert.equal(listed.ok, true);
    const entry = listed.entries.find((e) => e.id === 'a');
    assert.equal(entry.messages, undefined);
    assert.equal(entry.messageCount, 1);
});

test('quota exhaustion is reported as quota', async () => {
    const { indexedDB, store } = await makeStore();
    await store.put(chat('a'));
    indexedDB.exhaustQuota();

    const failed = await store.put(chat('b'));
    assert.equal(failed.ok, false);
    assert.equal(failed.kind, 'quota');
});

test('an unavailable database is reported as unavailable, not as empty', async () => {
    const indexedDB = new FakeIndexedDB();
    indexedDB.rejectOpen = true;
    const store = createIndexedDBRecordStore({ indexedDB, name: 'blocked' });

    const got = await store.get('a');
    assert.equal(got.ok, false);
    assert.equal(got.kind, 'unavailable');

    const listed = await store.listIndex();
    assert.equal(listed.ok, false);
    assert.equal(listed.kind, 'unavailable');
});

test('a blocked upgrade is reported rather than hanging', async () => {
    const indexedDB = new FakeIndexedDB();
    indexedDB.openBlocked = true;
    const store = createIndexedDBRecordStore({ indexedDB, name: 'blocked2' });
    const got = await store.get('a');
    assert.equal(got.ok, false);
    assert.equal(got.kind, 'unavailable');
});

test('remove deletes the record and its index entry together', async () => {
    const { store } = await makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));

    const removed = await store.remove('a');
    assert.equal(removed.ok, true);
    assert.ok(removed.removedRaw.includes('Chat a'));
    assert.equal((await store.get('a')).record, null);

    const listed = await store.listIndex();
    assert.deepEqual(listed.entries.map((e) => e.id), ['b']);
});

test('clearAll removes everything and reports the bytes it reclaimed', async () => {
    const { store } = await makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));

    const done = await store.clearAll();
    assert.equal(done.ok, true);
    assert.equal(done.removed.length, 2);
    assert.equal((await store.listIndex()).entries.length, 0);
    assert.equal((await store.get('a')).record, null);
});

test('rebuildIndex reconstructs the index from records', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', { messages: [{ role: 'user' }] }));
    await store.put(chat('b', { messages: [{ role: 'user' }, { role: 'assistant' }] }));

    // Wipe the index out from under the store.
    const db = await store.open();
    const tx = db.transaction('chatIndex', 'readwrite');
    tx.objectStore('chatIndex').clear();
    await new Promise((resolve) => queueMicrotask(resolve));
    await new Promise((resolve) => setTimeout(resolve, 0));

    const rebuilt = await store.rebuildIndex();
    assert.equal(rebuilt.ok, true);
    assert.equal(rebuilt.count, 2);

    const listed = await store.listIndex();
    assert.deepEqual(listed.entries.map((e) => e.id).sort(), ['a', 'b']);
    const b = listed.entries.find((e) => e.id === 'b');
    assert.equal(b.messageCount, 2);
});

test('keys enumerates record ids', async () => {
    const { store } = await makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));
    const listed = await store.keys();
    assert.deepEqual(listed.ids.sort(), ['a', 'b']);
});

test('meta round-trips and patches without clobbering', async () => {
    const { store } = await makeStore();
    assert.equal((await store.getMeta()).meta, null);

    await store.setMeta({ backend: 'indexedDB', migratedFrom: 'localStorage' });
    let meta = (await store.getMeta()).meta;
    assert.equal(meta.migratedFrom, 'localStorage');
    assert.equal(meta.schemaVersion, 1);

    await store.setMeta({ backend: 'indexedDB' });
    meta = (await store.getMeta()).meta;
    assert.equal(meta.migratedFrom, 'localStorage');
});

test('totalBytes sums the index', async () => {
    const { store } = await makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));
    const total = await store.totalBytes();
    assert.equal(total.ok, true);
    assert.ok(total.bytes > 0);
});

test('put refuses a record with no id', async () => {
    const { store } = await makeStore();
    const bad = await store.put({ title: 'orphan' });
    assert.equal(bad.ok, false);
    assert.equal(bad.kind, 'invalid');
});

test('a large record round-trips without a count cap', async () => {
    const { store } = await makeStore();
    const big = chat('big', {
        messages: Array.from({ length: 4000 }, (_, i) => ({
            role: i % 2 ? 'assistant' : 'user',
            content: `m${i} ${'y'.repeat(120)}`,
        })),
    });
    assert.equal((await store.put(big)).ok, true);
    assert.equal((await store.get('big')).record.messages.length, 4000);
});

test('isIndexedDBAvailable probes and cleans up', async () => {
    const indexedDB = new FakeIndexedDB();
    assert.equal(await isIndexedDBAvailable(indexedDB), true);
    // The probe database must not linger.
    assert.deepEqual(indexedDB.databases_(), []);
});

test('isIndexedDBAvailable returns false when open is rejected', async () => {
    const indexedDB = new FakeIndexedDB();
    indexedDB.rejectOpen = true;
    assert.equal(await isIndexedDBAvailable(indexedDB), false);
});

test('isIndexedDBAvailable returns false without an indexedDB object', async () => {
    assert.equal(await isIndexedDBAvailable(null), false);
    assert.equal(await isIndexedDBAvailable(undefined), false);
});

test('opening twice reuses one connection', async () => {
    const indexedDB = new FakeIndexedDB();
    const store = createIndexedDBRecordStore({ indexedDB, name: 'reuse' });
    await store.open();
    await store.open();
    await store.put(chat('a'));
    assert.equal(indexedDB.openCount, 1);
});

test('close releases the connection and reopen works', async () => {
    const indexedDB = new FakeIndexedDB();
    const store = createIndexedDBRecordStore({ indexedDB, name: 'reopen' });
    await store.open();
    await store.put(chat('a'));
    await store.close();
    await store.open();
    assert.equal((await store.get('a')).record.title, 'Chat a');
});

// ---- reactive state ----

// Alpine wraps component state in deeply reactive Proxies, so a chat taken from
// the live list is a Proxy at every level. IndexedDB writes run the structured
// clone algorithm, which rejects a Proxy outright with DataCloneError, and the
// backend used to surface that as "storage unavailable" — a browser-wide claim
// made because of one field's shape. The backend now normalises before writing.
//
// Faithful to Alpine in the two ways that matter here: proxies are cached per
// target, so a cycle reads back as the same proxy each time; and only plain
// objects and arrays are wrapped, leaving Date and friends untouched.
function reactiveTree(value, cache) {
    const seen = cache || new WeakMap();
    if (value === null || typeof value !== 'object') return value;
    const tag = Object.prototype.toString.call(value);
    if (tag !== '[object Object]' && !Array.isArray(value)) return value;
    if (seen.has(value)) return seen.get(value);
    const copy = Array.isArray(value) ? value.slice() : Object.assign({}, value);
    const proxy = new Proxy(copy, {
        get(target, key, receiver) {
            return reactiveTree(Reflect.get(target, key, receiver), seen);
        },
    });
    seen.set(value, proxy);
    return proxy;
}

test('a reactive Proxy record from component state is writable', async () => {
    const { store } = await makeStore();
    const rec = reactiveTree(chat('reactive', {
        messages: [
            { role: 'user', content: 'string content' },
            { role: 'assistant', content: [{ type: 'text', text: 'parted content' }] },
        ],
        modelSettingsByModel: { 'm1': { temperature: 0.3 } },
    }));

    const put = await store.put(rec);
    assert.equal(put.ok, true, `put must not fail on reactive state: ${put.kind} ${put.error}`);

    const got = await store.get('reactive');
    assert.equal(got.ok, true);
    assert.equal(got.record.messages[0].content, 'string content');
    assert.deepEqual(got.record.messages[1].content, [{ type: 'text', text: 'parted content' }]);
    assert.equal(got.record.modelSettingsByModel.m1.temperature, 0.3);
});

test('a reactive Proxy record with a nested Proxy messages array is writable', async () => {
    const { store } = await makeStore();
    // The exact shape that reached the store before the fix: a plain top level
    // with a Proxy still wrapped around the nested array.
    const messages = reactiveTree([{ role: 'user', content: 'nested' }]);
    const put = await store.put({ id: 'nested', messages });
    assert.equal(put.ok, true, `nested reactive write failed: ${put.kind} ${put.error}`);
    assert.equal((await store.get('nested')).record.messages[0].content, 'nested');
});

test('a record holding a function reports serialization, not unavailable', async () => {
    const { store } = await makeStore();
    const rec = chat('fn');
    rec.unclonable = function nope() { return 'nope'; };
    const put = await store.put(rec);
    assert.equal(put.ok, false);
    assert.equal(put.kind, 'serialization',
        'an uncloneable field must not be reported as the browser having no storage');
});

test('a circular record still round-trips, as IndexedDB allows it', async () => {
    const { store } = await makeStore();
    const rec = chat('loop');
    rec.self = rec;
    const put = await store.put(rec);
    assert.equal(put.ok, true, `circular record rejected: ${put.kind} ${put.error}`);
    const got = await store.get('loop');
    assert.equal(got.ok, true);
    assert.equal(got.record.self.id, 'loop');
});

test('a reactive circular record round-trips', async () => {
    const { store } = await makeStore();
    const plain = chat('rxloop');
    plain.self = plain;
    const rec = reactiveTree(plain);
    const put = await store.put(rec);
    assert.equal(put.ok, true, `reactive circular record rejected: ${put.kind} ${put.error}`);
    assert.equal((await store.get('rxloop')).record.self.id, 'rxloop');
});

test('a date reached through reactive state is stored as a date', async () => {
    const { store } = await makeStore();
    const when = new Date('2026-03-04T05:06:07.000Z');
    // Alpine does not proxy Date, so the date arrives nested inside a Proxy.
    const put = await store.put(reactiveTree({ id: 'rxdate', at: when, messages: [] }));
    assert.equal(put.ok, true, `put failed: ${put.kind} ${put.error}`);
    const got = await store.get('rxdate');
    assert.ok(got.record.at instanceof Date, 'the Date must survive as a Date, not a string');
    assert.equal(got.record.at.toISOString(), when.toISOString());
});

test('a reactive record is writable through the compare-and-set path too', async () => {
    const { store } = await makeStore();
    await store.put(chat('a', { title: 'v1' }));
    const put = await store.put(reactiveTree(chat('a', { title: 'v2' })), { expectRev: 1 });
    assert.equal(put.ok, true);
    assert.equal(put.rev, 2);
    assert.equal((await store.get('a')).record.title, 'v2');
});

// ---- capability detection ----

test('isIndexedDBAvailable falls back to globalThis.indexedDB', async () => {
    const indexedDB = new FakeIndexedDB();
    globalThis.indexedDB = indexedDB;
    try {
        assert.equal(await isIndexedDBAvailable(), true);
    } finally {
        delete globalThis.indexedDB;
    }
});

test('isIndexedDBAvailable ignores navigator.storage and reports IndexedDB on its own', async () => {
    // A plain-HTTP LAN origin looks like this: no navigator.storage, no secure
    // context, working IndexedDB. The probe must answer from IndexedDB alone.
    const hadNavigator = Object.hasOwn(globalThis, 'navigator');
    const savedNavigator = globalThis.navigator;
    try {
        if (hadNavigator) {
            try { delete globalThis.navigator; } catch { globalThis.navigator = undefined; }
        }
        assert.equal(globalThis.navigator && globalThis.navigator.storage, undefined);
        const indexedDB = new FakeIndexedDB();
        assert.equal(await isIndexedDBAvailable(indexedDB), true);
    } finally {
        if (hadNavigator) globalThis.navigator = savedNavigator;
    }
});

test('browserStorageQuota degrades instead of throwing when navigator.storage is absent', async () => {
    const hadNavigator = Object.hasOwn(globalThis, 'navigator');
    const savedNavigator = globalThis.navigator;
    try {
        if (hadNavigator) {
            try { delete globalThis.navigator; } catch { globalThis.navigator = undefined; }
        }
        const q = await browserStorageQuota();
        assert.equal(q.ok, true);
        assert.equal(q.available, false);
        assert.equal(q.quota, null);
        assert.equal(q.usage, null);
        assert.equal(q.persisted, null);
    } finally {
        if (hadNavigator) globalThis.navigator = savedNavigator;
    }
});

test('browserStorageQuota reports the estimate when navigator.storage is present', async () => {
    const savedNavigator = globalThis.navigator;
    const fake = {
        estimate: async () => ({ quota: 12345, usage: 678 }),
        persisted: async () => true,
    };
    try {
        Object.defineProperty(globalThis, 'navigator', { value: { storage: fake }, configurable: true, writable: true });
        const q = await browserStorageQuota();
        assert.equal(q.ok, true);
        assert.equal(q.available, true);
        assert.equal(q.quota, 12345);
        assert.equal(q.usage, 678);
        assert.equal(q.persisted, true);
    } finally {
        if (savedNavigator === undefined) delete globalThis.navigator;
        else Object.defineProperty(globalThis, 'navigator', { value: savedNavigator, configurable: true, writable: true });
    }
});

test('browserStorageQuota reports a failed estimate without throwing', async () => {
    const savedNavigator = globalThis.navigator;
    try {
        Object.defineProperty(globalThis, 'navigator', {
            value: { storage: { estimate: async () => { throw new Error('blocked'); } } },
            configurable: true,
            writable: true,
        });
        const q = await browserStorageQuota();
        assert.equal(q.ok, false);
        assert.equal(q.available, false);
    } finally {
        if (savedNavigator === undefined) delete globalThis.navigator;
        else Object.defineProperty(globalThis, 'navigator', { value: savedNavigator, configurable: true, writable: true });
    }
});
