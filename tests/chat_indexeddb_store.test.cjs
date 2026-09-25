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
