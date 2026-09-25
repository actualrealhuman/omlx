// Run with: node --test tests/chat_record_store.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
    createLocalStorageRecordStore,
    parseRecord,
    RECORD_PREFIX,
    INDEX_KEY,
    META_KEY,
} = require('../omlx/admin/static/js/chat_record_store.js');

// Mirrors the Storage interface the browser provides, including key
// enumeration, and lets a test arm a failure on the next write.
class MemoryStorage {
    constructor(initial = {}) {
        this.map = new Map(Object.entries(initial));
        this.writeError = null;
        this.readError = null;
        this.setCalls = 0;
        this.removeCalls = 0;
    }

    getItem(key) {
        if (this.readError) throw this.readError;
        return this.map.has(key) ? this.map.get(key) : null;
    }

    setItem(key, value) {
        this.setCalls += 1;
        if (this.writeError) throw this.writeError;
        this.map.set(key, String(value));
    }

    removeItem(key) {
        this.removeCalls += 1;
        if (this.writeError) throw this.writeError;
        this.map.delete(key);
    }

    key(i) {
        return Array.from(this.map.keys())[i] ?? null;
    }

    get length() {
        return this.map.size;
    }

    quotaError() {
        const error = new Error('QuotaExceeded: storage is full');
        error.name = 'QuotaExceededError';
        return error;
    }
}

function makeStore(initial = {}) {
    const storage = new MemoryStorage(initial);
    return { storage, store: createLocalStorageRecordStore({ storage }) };
}

const chat = (id, overrides = {}) => ({
    id,
    title: `Chat ${id}`,
    model: 'test-model',
    createdAt: 1,
    updatedAt: 2,
    messages: [{ role: 'user', content: `hello ${id}` }],
    ...overrides,
});

test('put then get round-trips a record and stamps rev 1', async () => {
    const { store } = makeStore();
    const result = await store.put(chat('a'));

    assert.equal(result.ok, true);
    assert.equal(result.rev, 1);

    const got = await store.get('a');
    assert.equal(got.ok, true);
    assert.equal(got.record.id, 'a');
    assert.equal(got.record.rev, 1);
    assert.equal(got.record.messages.length, 1);
});

test('successive puts bump rev monotonically', async () => {
    const { store } = makeStore();
    assert.equal((await store.put(chat('a'))).rev, 1);
    assert.equal((await store.put(chat('a', { title: 'v2' }))).rev, 2);
    assert.equal((await store.put(chat('a', { title: 'v3' }))).rev, 3);

    const got = await store.get('a');
    assert.equal(got.record.title, 'v3');
    assert.equal(got.record.rev, 3);
});

test('put with a stale expectRev is refused and returns the current record', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a', { title: 'committed' }));
    await store.put(chat('a', { title: 'newer tab' }));

    const setsBefore = storage.setCalls;
    const conflict = await store.put(chat('a', { title: 'my stale edit' }), { expectRev: 1 });

    assert.equal(conflict.ok, false);
    assert.equal(conflict.kind, 'conflict');
    assert.equal(conflict.expectedRev, 1);
    assert.equal(conflict.currentRev, 2);
    assert.equal(conflict.current.title, 'newer tab');

    // The refused write must not have touched storage at all.
    assert.equal(storage.setCalls, setsBefore);
    assert.equal((await store.get('a')).record.title, 'newer tab');
});

test('put with a matching expectRev succeeds', async () => {
    const { store } = makeStore();
    await store.put(chat('a'));
    const ok = await store.put(chat('a', { title: 'mine' }), { expectRev: 1 });
    assert.equal(ok.ok, true);
    assert.equal(ok.rev, 2);
});

test('a quota failure leaves the previously committed bytes intact and returns them', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a', { title: 'keep me' }));
    const committed = storage.getItem(`${RECORD_PREFIX}a`);

    storage.writeError = storage.quotaError();
    const failed = await store.put(chat('a', { title: 'overwrite attempt' }));

    assert.equal(failed.ok, false);
    assert.equal(failed.kind, 'quota');
    assert.equal(failed.raw, committed);

    // The old record is still readable, unchanged.
    storage.writeError = null;
    const got = await store.get('a');
    assert.equal(got.record.title, 'keep me');
    assert.equal(got.record.rev, 1);
});

test('a quota failure is reported as quota, not as a generic failure', async () => {
    const { store, storage } = makeStore();
    const firefox = new Error('NS_ERROR_DOM_QUOTA_REACHED');
    firefox.name = 'NS_ERROR_DOM_QUOTA_REACHED';
    storage.writeError = firefox;
    const failed = await store.put(chat('a'));
    assert.equal(failed.kind, 'quota');
});

test('a non-quota write failure is reported as unavailable', async () => {
    const { store, storage } = makeStore();
    storage.writeError = new Error('disk gone');
    const failed = await store.put(chat('a'));
    assert.equal(failed.ok, false);
    assert.equal(failed.kind, 'unavailable');
});

test('a corrupt stored record is refused, preserved, and never overwritten', async () => {
    const raw = '{not json at all';
    const { store, storage } = makeStore({ [`${RECORD_PREFIX}a`]: raw });

    const got = await store.get('a');
    assert.equal(got.ok, false);
    assert.equal(got.kind, 'corrupt');
    assert.equal(got.raw, raw);

    const put = await store.put(chat('a', { title: 'replacement' }));
    assert.equal(put.ok, false);
    assert.equal(put.kind, 'corrupt');
    assert.equal(put.raw, raw);
    assert.equal(storage.getItem(`${RECORD_PREFIX}a`), raw);
});

test('a non-object stored record is treated as corrupt, not as absent', async () => {
    const raw = JSON.stringify(['surprise', 'array']);
    const { store, storage } = makeStore({ [`${RECORD_PREFIX}a`]: raw });
    const put = await store.put(chat('a'));
    assert.equal(put.ok, false);
    assert.equal(put.kind, 'corrupt');
    assert.equal(storage.getItem(`${RECORD_PREFIX}a`), raw);
});

test('parseRecord distinguishes absent from corrupt', () => {
    assert.deepEqual(parseRecord(null), { ok: true, record: null });
    assert.equal(parseRecord('nonsense').ok, false);
    assert.equal(parseRecord('nonsense').kind, 'corrupt');
    assert.equal(parseRecord('[]').kind, 'corrupt');
    assert.equal(parseRecord('{"id":"x"}').ok, true);
});

test('put writes only the one record, not the whole corpus', async () => {
    const { store, storage } = makeStore();
    for (const id of ['a', 'b', 'c', 'd']) await store.put(chat(id));

    const before = storage.setCalls;
    await store.put(chat('c', { title: 'edited' }));

    // record + index = two writes, independent of corpus size
    assert.equal(storage.setCalls - before, 2);
});

test('the index tracks every record with size and message count', async () => {
    const { store } = makeStore();
    await store.put(chat('a', { messages: [{ role: 'user' }, { role: 'assistant' }] }));
    await store.put(chat('b'));

    const listed = await store.listIndex();
    assert.equal(listed.ok, true);
    const byId = Object.fromEntries(listed.entries.map(e => [e.id, e]));

    assert.equal(byId.a.messageCount, 2);
    assert.equal(byId.b.messageCount, 1);
    assert.equal(byId.a.rev, 1);
    assert.ok(byId.a.bytes > 0);
    assert.ok(byId.a.bytes < JSON.stringify(chat('a')).length + 200);
});

test('totalBytes sums the index without reading records', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));

    const readsBefore = storage.map.size;
    const total = await store.totalBytes();
    assert.equal(total.ok, true);
    assert.ok(total.bytes > 0);
    assert.equal(storage.map.size, readsBefore);
});

test('a corrupt index does not lose a record that already committed', async () => {
    const { store, storage } = makeStore({ [INDEX_KEY]: 'broken{index' });
    const result = await store.put(chat('a', { title: 'committed' }));

    // The record write succeeded; only the index is unusable.
    assert.equal(result.ok, true);
    assert.equal(result.indexDegraded, true);
    assert.equal((await store.get('a')).record.title, 'committed');
});

test('rebuildIndex recovers the index from records and reports corrupt ones', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));
    storage.setItem(`${RECORD_PREFIX}bad`, '}{not json');
    storage.setItem(INDEX_KEY, 'gone');

    const rebuilt = await store.rebuildIndex();
    assert.equal(rebuilt.ok, true);
    assert.equal(rebuilt.count, 2);
    assert.deepEqual(rebuilt.corrupt.map(c => c.id), ['bad']);

    const listed = await store.listIndex();
    const ids = listed.entries.map(e => e.id).sort();
    assert.deepEqual(ids, ['a', 'b']);
    // The corrupt record is still on disk, untouched.
    assert.equal(storage.getItem(`${RECORD_PREFIX}bad`), '}{not json');
});

test('keys enumerates only record keys', async () => {
    const { store, storage } = makeStore({ unrelated: 'x', [INDEX_KEY]: '{}' });
    await store.put(chat('a'));
    await store.put(chat('b'));

    const listed = await store.keys();
    assert.deepEqual(listed.ids.sort(), ['a', 'b']);
    assert.ok(!listed.ids.includes(INDEX_KEY));
    assert.ok(!storage.map.has('unrelated') === false);
});

test('remove deletes the record and drops it from the index', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));

    const removed = await store.remove('a');
    assert.equal(removed.ok, true);
    assert.ok(removed.removedRaw);
    assert.equal(storage.getItem(`${RECORD_PREFIX}a`), null);

    const listed = await store.listIndex();
    assert.deepEqual(listed.entries.map(e => e.id), ['b']);
});

test('clearAll removes every record and retains their bytes for the caller', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));

    const done = await store.clearAll();
    assert.equal(done.ok, true);
    assert.equal(done.removed.length, 2);
    assert.equal(storage.getItem(`${RECORD_PREFIX}a`), null);
    assert.equal(storage.getItem(`${RECORD_PREFIX}b`), null);
    assert.equal(storage.getItem(INDEX_KEY), null);
});

test('clearAll stops at the first failure and reports what it already removed', async () => {
    const { store, storage } = makeStore();
    await store.put(chat('a'));
    await store.put(chat('b'));

    const seen = [];
    const originalRemove = storage.removeItem.bind(storage);
    storage.removeItem = (key) => {
        if (key === `${RECORD_PREFIX}b`) throw storage.quotaError();
        seen.push(key);
        return originalRemove(key);
    };

    const failed = await store.clearAll();
    assert.equal(failed.ok, false);
    assert.equal(failed.kind, 'quota');
    assert.equal(failed.removed.length, 1);
});

test('meta records schema version and survives a patch', async () => {
    const { store } = makeStore();
    assert.equal((await store.getMeta()).meta, null);

    await store.setMeta({ backend: 'localStorage', migratedFrom: 'legacy' });
    const meta = (await store.getMeta()).meta;
    assert.equal(meta.backend, 'localStorage');
    assert.equal(meta.migratedFrom, 'legacy');
    assert.equal(meta.schemaVersion, 2);

    await store.setMeta({ backend: 'localStorage' });
    const patched = (await store.getMeta()).meta;
    assert.equal(patched.migratedFrom, 'legacy');
});

test('a corrupt meta value is reported rather than silently treated as fresh', async () => {
    const { store } = makeStore({ [META_KEY]: 'not-json' });
    const meta = await store.getMeta();
    assert.equal(meta.ok, false);
    assert.equal(meta.kind, 'corrupt');
});

test('unavailable storage surfaces as unavailable, not as empty', async () => {
    const storage = new MemoryStorage();
    storage.readError = new Error('SecurityError: storage disabled');
    const store = createLocalStorageRecordStore({ storage });

    const got = await store.get('a');
    assert.equal(got.ok, false);
    assert.equal(got.kind, 'unavailable');

    const listed = await store.listIndex();
    assert.equal(listed.ok, false);
    assert.equal(listed.kind, 'unavailable');
});

test('put refuses a record without an id', async () => {
    const { store } = makeStore();
    const bad = await store.put({ title: 'no id' });
    assert.equal(bad.ok, false);
    assert.equal(bad.kind, 'invalid');
});

test('a record larger than the former whole-history cap round-trips', async () => {
    const { store } = makeStore();
    const big = chat('big', {
        messages: Array.from({ length: 5000 }, (_, i) => ({
            role: i % 2 ? 'assistant' : 'user',
            content: `message ${i} ${'x'.repeat(200)}`,
        })),
    });

    const put = await store.put(big);
    assert.equal(put.ok, true);

    const got = await store.get('big');
    assert.equal(got.record.messages.length, 5000);
    assert.equal(got.record.messages.at(-1).content.startsWith('message 4999'), true);
});
