// Run with: node --test tests/chat_history_storage.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {
    createChatHistoryStorage,
    isQuotaError,
} = require('../omlx/admin/static/js/chat_history_storage.js');

class MemoryStorage {
    constructor(initial = null) {
        this.value = initial;
        this.error = null;
    }

    getItem() {
        return this.value;
    }

    setItem(_key, value) {
        if (this.error) throw this.error;
        this.value = value;
    }
}

test('round-trips histories larger than the former hard limit', () => {
    const storage = new MemoryStorage();
    const store = createChatHistoryStorage(storage);
    const history = Array.from({length: 1000}, (_, i) => ({id: `chat_${i}`, messages: []}));

    assert.equal(store.save(history).ok, true);
    const loaded = store.load();
    assert.equal(loaded.ok, true);
    assert.equal(loaded.history.length, 1000);
    assert.equal(loaded.history.at(-1).id, 'chat_999');
});

test('quota failure leaves the previously committed bytes unchanged', () => {
    const original = JSON.stringify([{id: 'keep-me', messages: []}]);
    const storage = new MemoryStorage(original);
    const store = createChatHistoryStorage(storage);
    const error = new Error('Storage quota exceeded');
    error.name = 'QuotaExceededError';
    storage.error = error;

    const result = store.save([{id: 'new-chat', messages: []}]);

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'quota');
    assert.equal(result.raw, original);
    assert.equal(storage.value, original);
    assert.match(result.serialized, /new-chat/);
});

test('malformed history is returned for recovery instead of becoming empty history', () => {
    const storage = new MemoryStorage('{not valid JSON');
    const result = createChatHistoryStorage(storage).load();

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'corrupt');
    assert.equal(result.raw, '{not valid JSON');
    assert.equal(storage.value, '{not valid JSON');
});

test('non-array JSON is treated as corrupt and preserved', () => {
    const raw = JSON.stringify({chats: []});
    const storage = new MemoryStorage(raw);
    const result = createChatHistoryStorage(storage).load();

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'corrupt');
    assert.equal(result.raw, raw);
});

test('serialization failure never calls storage', () => {
    const original = JSON.stringify([{id: 'keep-me'}]);
    const storage = new MemoryStorage(original);
    const circular = {};
    circular.self = circular;

    const result = createChatHistoryStorage(storage).save([circular]);

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'serialization');
    assert.equal(result.raw, original);
    assert.equal(storage.value, original);
});

test('storage read failures are reported without attempting recovery writes', () => {
    let writes = 0;
    const storage = {
        getItem() {
            throw new Error('Storage is disabled');
        },
        setItem() {
            writes += 1;
        },
    };

    const result = createChatHistoryStorage(storage).load();

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'unavailable');
    assert.equal(writes, 0);
});

test('recognizes browser-specific quota error variants', () => {
    assert.equal(isQuotaError({name: 'QuotaExceededError'}), true);
    assert.equal(isQuotaError({name: 'NS_ERROR_DOM_QUOTA_REACHED'}), true);
    assert.equal(isQuotaError({code: 22}), true);
    assert.equal(isQuotaError({code: 1014}), true);
    assert.equal(isQuotaError(new Error('Permission denied')), false);
});
