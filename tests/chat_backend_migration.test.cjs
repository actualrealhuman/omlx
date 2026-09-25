// Run with: node --test tests/chat_backend_migration.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
    createLocalStorageRecordStore,
} = require('../omlx/admin/static/js/chat_record_store.js');
const {
    createIndexedDBRecordStore,
} = require('../omlx/admin/static/js/chat_indexeddb_store.js');
const {
    migrateRecordStores,
    backendMigrationState,
} = require('../omlx/admin/static/js/chat_history_migration.js');
const { FakeIndexedDB } = require('./helpers/fake_indexeddb.cjs');

const chat = (id, overrides = {}) => ({
    id,
    title: `Chat ${id}`,
    model: 'test-model',
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-02T00:00:00.000Z',
    messages: [{ role: 'user', content: `hello ${id}` }],
    ...overrides,
});

async function pair(localStorageRecords = []) {
    const storage = new Map();
    const localStorageLike = {
        getItem: (k) => (storage.has(k) ? storage.get(k) : null),
        setItem: (k, v) => storage.set(k, String(v)),
        removeItem: (k) => storage.delete(k),
        key: (i) => Array.from(storage.keys())[i] ?? null,
        get length() { return storage.size; },
    };
    const source = createLocalStorageRecordStore({ storage: localStorageLike });
    for (const record of localStorageRecords) await source.put(record);

    const indexedDB = new FakeIndexedDB();
    const target = createIndexedDBRecordStore({ indexedDB, name: 'migrated' });
    await target.open();

    return { storage, localStorageLike, source, target, indexedDB };
}

test('moves every record from localStorage to IndexedDB', async () => {
    const { source, target } = await pair([chat('a'), chat('b'), chat('c')]);
    const result = await migrateRecordStores({ source, target });

    assert.equal(result.ok, true);
    assert.equal(result.migrated, 3);
    assert.equal(result.skipped, 0);

    const got = await target.get('b');
    assert.equal(got.ok, true);
    assert.equal(got.record.title, 'Chat b');
});

test('the index arrives intact, with sizes and message counts', async () => {
    const two = [{ role: 'user' }, { role: 'assistant' }];
    const { source, target } = await pair([chat('a', { messages: two })]);

    await migrateRecordStores({ source, target });

    const listed = await target.listIndex();
    const entry = listed.entries.find((e) => e.id === 'a');
    assert.equal(entry.messageCount, 2);
    assert.ok(entry.bytes > 0);
});

test('the source is retained, never cleared, by migration', async () => {
    const { source, target, storage } = await pair([chat('a'), chat('b')]);
    const before = storage.size;

    const result = await migrateRecordStores({ source, target });

    assert.equal(result.retained, true);
    assert.ok(storage.size >= before);
    assert.equal((await source.get('a')).ok, true);
});

test('the backend marker is written only after verification', async () => {
    const { source, target } = await pair([chat('a')]);
    assert.equal((await backendMigrationState(target)).migrated, false);

    await migrateRecordStores({ source, target });

    const state = await backendMigrationState(target);
    assert.equal(state.migrated, true);
    assert.equal(state.info.from, 'localStorage');
    assert.equal(state.info.sourceRetained, true);
});

test('migration is idempotent', async () => {
    const { source, target } = await pair([chat('a'), chat('b')]);
    const first = await migrateRecordStores({ source, target });
    const second = await migrateRecordStores({ source, target });

    assert.equal(first.migrated, 2);
    assert.equal(second.migrated, 0);
    assert.equal(second.skipped, 2);
    assert.equal(second.ok, true);
});

test('a newer record already in the target is not overwritten', async () => {
    const { source, target } = await pair([chat('a', { title: 'OLD' })]);
    await target.put({ ...chat('a'), title: 'NEWER', updatedAt: '2026-05-05T00:00:00.000Z' });

    const result = await migrateRecordStores({ source, target });

    assert.equal(result.skipped, 1);
    assert.equal((await target.get('a')).record.title, 'NEWER');
});

test('a quota failure mid-migration leaves the marker unwritten', async () => {
    const { source, target, indexedDB } = await pair([chat('a'), chat('b'), chat('c')]);

    let puts = 0;
    const originalPut = target.put.bind(target);
    target.put = async (record, options) => {
        puts += 1;
        if (puts === 3) return { ok: false, kind: 'quota' };
        return originalPut(record, options);
    };

    const result = await migrateRecordStores({ source, target });

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'quota');
    assert.equal(result.migrated, 2);
    assert.equal((await backendMigrationState(target)).migrated, false);
    void indexedDB;
});

test('migrating between identical backends is a no-op', async () => {
    const storage = new Map();
    const localStorageLike = {
        getItem: (k) => (storage.has(k) ? storage.get(k) : null),
        setItem: (k, v) => storage.set(k, String(v)),
        removeItem: (k) => storage.delete(k),
        key: (i) => Array.from(storage.keys())[i] ?? null,
        get length() { return storage.size; },
    };
    const a = createLocalStorageRecordStore({ storage: localStorageLike });
    const b = createLocalStorageRecordStore({ storage: localStorageLike });

    const result = await migrateRecordStores({ source: a, target: b });
    assert.equal(result.ok, true);
    assert.equal(result.noop, true);
});

test('an unreadable source is reported as unavailable', async () => {
    const { target } = await pair([]);
    const source = {
        backend: 'localStorage',
        listIndex: async () => ({ ok: false, kind: 'unavailable' }),
        get: async () => ({ ok: false, kind: 'unavailable' }),
        put: async () => ({ ok: true }),
    };
    const result = await migrateRecordStores({ source, target });
    assert.equal(result.ok, false);
    assert.equal(result.kind, 'unavailable');
});

test('missing arguments are rejected', async () => {
    const { target } = await pair([]);
    assert.equal((await migrateRecordStores({ target })).ok, false);
    assert.equal((await migrateRecordStores({ source: null, target })).kind, 'invalid');
});

test('progress is reported as records land', async () => {
    const { source, target } = await pair([chat('a'), chat('b'), chat('c')]);
    const seen = [];
    await migrateRecordStores({ source, target, onProgress: (p) => seen.push(p.migrated) });
    assert.deepEqual(seen, [1, 2, 3]);
});

test('revisions are preserved across the move', async () => {
    const { source, target } = await pair([]);
    await source.put(chat('a'));
    await source.put(chat('a', { title: 'v2' }));
    assert.equal((await source.get('a')).record.rev, 2);

    await migrateRecordStores({ source, target });

    // The migrated record carries the source revision forward so a stale tab
    // in the new backend still collides rather than clobbering.
    const entry = (await target.listIndex()).entries.find((e) => e.id === 'a');
    assert.equal(entry.rev, 2);
});

test('a large corpus migrates without a count cap', async () => {
    const records = Array.from({ length: 800 }, (_, i) => chat(`c_${i}`));
    const { source, target } = await pair(records);

    const result = await migrateRecordStores({ source, target });

    assert.equal(result.ok, true);
    assert.equal(result.migrated, 800);
    assert.equal((await target.listIndex()).entries.length, 800);
});
