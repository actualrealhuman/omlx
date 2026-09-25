// A small, dependency-free IndexedDB stand-in for Node tests.
//
// It is deliberately faithful about the two behaviours the storage redesign
// depends on:
//   1. a readwrite transaction buffers its writes and applies them only on
//      completion — abort() discards them, so a refused compare-and-set leaves
//      the store untouched;
//   2. requests report failure through onerror with a named error, and quota
//      exhaustion surfaces as QuotaExceededError.
//
// It is NOT a browser. Real IndexedDB semantics — upgrade blocking across tabs,
// blob storage, eviction, and the auto-commit-on-idle rule — still require
// verification in a real browser. Tests written against this fake prove the
// interface contract and the atomicity logic, nothing more.

'use strict';

class DomEvent {
    constructor(type) {
        this.type = type;
        this.target = null;
    }
}

function fireLater(target, type, extra) {
    queueMicrotask(() => {
        const event = new DomEvent(type);
        event.target = target;
        if (extra) Object.assign(event, extra);
        const handler = target[`on${type}`];
        if (typeof handler === 'function') handler(event);
    });
}

class FakeRequest {
    constructor() {
        this.result = undefined;
        this.error = null;
        this.onsuccess = null;
        this.onerror = null;
        this._errorHandlers = [];
    }

    succeed(result) {
        this.result = result;
        fireLater(this, 'success');
        return this;
    }

    fail(error) {
        this.error = error;
        queueMicrotask(() => {
            for (const handler of this._errorHandlers) {
                try { handler(error); } catch {}
            }
            if (typeof this.onerror === 'function') {
                const event = new DomEvent('error');
                event.target = this;
                this.onerror(event);
            }
        });
        return this;
    }

    // Mirrors addEventListener well enough for the transaction to observe
    // failures: in real IndexedDB a failed request errors its transaction.
    addEventListener(type, handler) {
        if (type === 'error') this._errorHandlers.push(handler);
    }
}

class FakeCursor {
    constructor(values, request) {
        this._values = values;
        this._index = 0;
        this._request = request;
    }

    get primaryKey() {
        return this._values[this._index];
    }

    get value() {
        return this._values[this._index];
    }

    // Real IDBCursor.continue() returns undefined: it re-fires `success` on the
    // originating openCursor request with the next cursor, or null once exhausted.
    // Returning a fresh request here modelled the legacy contract and let a
    // real-browser bug in cursorAll() pass 88/88. Do not restore it.
    continue() {
        this._index += 1;
        const next = this._index < this._values.length ? this : null;
        if (this._request) this._request.succeed(next);
        return undefined;
    }
}

class FakeObjectStore {
    constructor(name, keyPath) {
        this.name = name;
        this.keyPath = keyPath;
        this.data = new Map();
        this.indexes = new Map();
    }

    createIndex(name, keyPath) {
        this.indexes.set(name, keyPath);
        return { name, keyPath };
    }

    _keyOf(value) {
        return value[this.keyPath];
    }

    get(key) {
        const request = new FakeRequest();
        const found = this.data.get(key);
        request.succeed(found === undefined ? undefined : structuredCloneish(found));
        return request;
    }

    put(value, key) {
        const k = key !== undefined ? key : this._keyOf(value);
        const request = new FakeRequest();
        request.__write = { op: 'put', key: k, value: structuredCloneish(value) };
        request.succeed(k);
        return request;
    }

    add(value, key) {
        const k = key !== undefined ? key : this._keyOf(value);
        const request = new FakeRequest();
        if (this.data.has(k)) {
            const error = new Error('ConstraintError: key already exists');
            error.name = 'ConstraintError';
            request.fail(error);
            return request;
        }
        request.__write = { op: 'put', key: k, value: structuredCloneish(value) };
        request.succeed(k);
        return request;
    }

    delete(key) {
        const request = new FakeRequest();
        request.__write = { op: 'delete', key };
        request.succeed(undefined);
        return request;
    }

    clear() {
        const request = new FakeRequest();
        request.__clear = true;
        request.succeed(undefined);
        return request;
    }

    openCursor() {
        const request = new FakeRequest();
        const values = Array.from(this.data.values());
        // the cursor re-fires this same request, as a real IDBCursor does
        const cursor = new FakeCursor(values, request);
        request.succeed(values.length ? cursor : null);
        return request;
    }

    count() {
        const request = new FakeRequest();
        request.succeed(this.data.size);
        return request;
    }
}

function structuredCloneish(value) {
    // No JSON fallback. A real store runs the structured clone algorithm and
    // raises DataCloneError on anything it cannot take — a Proxy among them. The
    // fallback that used to sit here silently accepted those values, which is how
    // a reactive-state write bug reached a release with every node test green.
    return structuredClone(value);
}

class FakeTransaction {
    constructor(db, storeNames, mode) {
        this.db = db;
        this.mode = mode;
        this.storeNames = storeNames;
        this.aborted = false;
        this.done = false;
        this.error = null;
        this.oncomplete = null;
        this.onabort = null;
        this.onerror = null;
        this._pending = [];
        this._cleared = new Set();
        this._outstanding = 0;
        this._timer = null;

        // A real transaction stays alive while requests are pending and commits
        // once the event loop has nothing left queued for it. Callers issue their
        // requests across microtasks, so settling on a microtask would commit
        // before any of them were recorded — a macrotask is the faithful choice.
        this._arm();
    }

    _arm() {
        if (this._timer) clearTimeout(this._timer);
        this._timer = setTimeout(() => this._settle(), 0);
    }

    objectStore(name) {
        if (!this.storeNames.includes(name)) {
            throw new Error(`NotFoundError: no store "${name}" in this transaction`);
        }
        // Transaction-scoped proxy: writes are buffered here rather than applied
        // to the store, so abort() can discard them and the store is never
        // mutated globally by opening a transaction.
        const underlying = this.db._stores.get(name);
        const tx = this;
        return {
            name,
            createIndex: (n, kp) => underlying.createIndex(n, kp),
            openCursor: () => {
                tx._arm();
                return underlying.openCursor();
            },
            count: () => {
                tx._arm();
                return underlying.count();
            },
            get: (key) => {
                tx._arm();
                return underlying.get(key);
            },
            put: (value, key) => {
                const request = underlying.put(value, key);
                if (request.__write) request.__storeName = name;
                tx.record(request);
                return request;
            },
            add: (value, key) => {
                const request = underlying.add(value, key);
                if (request.__write) request.__storeName = name;
                tx.record(request);
                return request;
            },
            delete: (key) => {
                const request = underlying.delete(key);
                request.__storeName = name;
                tx.record(request);
                return request;
            },
            clear: () => {
                const request = underlying.clear();
                request.__storeName = name;
                tx.record(request);
                return request;
            },
        };
    }

    record(request) {
        if (this.aborted) {
            const error = new Error('TransactionInactiveError: transaction is aborted');
            error.name = 'TransactionInactiveError';
            request.fail(error);
            return;
        }
        if (this.mode !== 'readwrite') {
            const error = new Error('ReadOnlyError: transaction is readonly');
            error.name = 'ReadOnlyError';
            request.fail(error);
            return;
        }
        this._arm();
        // In real IndexedDB a failed request errors and aborts its transaction,
        // discarding everything that transaction had written.
        request.addEventListener('error', (error) => {
            if (this.done) return;
            this.error = error;
            this.aborted = true;
            this._pending = [];
            this._cleared.clear();
            if (this._timer) clearTimeout(this._timer);
            fireLater(this, 'abort');
        });
        if (request.__clear) {
            this._cleared.add(request.__storeName);
            return;
        }
        if (request.__write) {
            this._pending.push({ store: request.__storeName, ...request.__write });
        }
    }

    abort() {
        if (this.done) return;
        this.aborted = true;
        this._pending = [];
        this._cleared.clear();
        if (this._timer) clearTimeout(this._timer);
        const error = new Error('AbortError: transaction was aborted');
        error.name = 'AbortError';
        this.error = error;
        fireLater(this, 'abort');
    }

    _settle() {
        if (this.aborted || this.done) return;
        for (const storeName of this._cleared) {
            this.db._stores.get(storeName).data.clear();
        }
        for (const write of this._pending) {
            const store = this.db._stores.get(write.store);
            if (write.op === 'put') store.data.set(write.key, write.value);
            else if (write.op === 'delete') store.data.delete(write.key);
        }
        this.done = true;
        this._pending = [];
        this._cleared.clear();
        fireLater(this, 'complete');
    }
}

class FakeIndexedDBDatabase {
    constructor(name, version) {
        this.name = name;
        this.version = version;
        this.objectStoreNames = {
            _stores: new Map(),
            contains(name) {
                return this._stores.has(name);
            },
            get length() {
                return this._stores.size;
            },
            item(i) {
                return Array.from(this._stores.keys())[i] ?? null;
            },
        };
        this._stores = this.objectStoreNames._stores;
        this.closed = false;
        this.onversionchange = null;
    }

    createObjectStore(name, options = {}) {
        if (this._stores.has(name)) {
            throw new Error(`ConstraintError: store "${name}" already exists`);
        }
        const store = new FakeObjectStore(name, options.keyPath || 'id');
        this._stores.set(name, store);
        this.objectStoreNames._stores = this._stores;
        return store;
    }

    deleteObjectStore(name) {
        this._stores.delete(name);
    }

    transaction(storeNames, mode = 'readonly') {
        if (this.closed) throw new Error('InvalidStateError: database is closed');
        const names = Array.isArray(storeNames) ? storeNames : [storeNames];
        for (const name of names) {
            if (!this._stores.has(name)) {
                const error = new Error(`NotFoundError: no store "${name}"`);
                error.name = 'NotFoundError';
                throw error;
            }
        }
        const tx = new FakeTransaction(this, names, mode);
        return tx;
    }

    close() {
        this.closed = true;
    }
}

class FakeIndexedDB {
    constructor() {
        this.databases = new Map();
        // Test hooks.
        this.quotaExceeded = false;
        this.openBlocked = false;
        this.rejectOpen = false;
        this.openCount = 0;
    }

    open(name, version = 1) {
        this.openCount += 1;
        const request = new FakeRequest();

        if (this.rejectOpen) {
            const error = new Error('SecurityError: access denied');
            error.name = 'SecurityError';
            request.fail(error);
            return request;
        }
        if (this.openBlocked) {
            queueMicrotask(() => {
                if (typeof request.onblocked === 'function') request.onblocked(new DomEvent('blocked'));
            });
            return request;
        }

        let entry = this.databases.get(name);
        const isNew = !entry;
        if (isNew) {
            entry = { db: new FakeIndexedDBDatabase(name, version), version };
            this.databases.set(name, entry);
        } else if (entry.db.closed) {
            // Reopening after close yields a fresh connection over the same data.
            const reopened = new FakeIndexedDBDatabase(name, entry.version);
            for (const [storeName, store] of entry.db._stores) {
                reopened._stores.set(storeName, store);
            }
            entry.db = reopened;
        }
        if (version > entry.version) entry.version = version;
        entry.db.version = entry.version;

        // Handlers are assigned by the caller *after* open() returns, exactly as
        // in real IndexedDB, so both the check and the dispatch must be deferred.
        queueMicrotask(() => {
            if (isNew && typeof request.onupgradeneeded === 'function') {
                const event = new DomEvent('upgradeneeded');
                event.oldVersion = 0;
                event.newVersion = version;
                event.target = request;
                request.result = entry.db;
                try {
                    request.onupgradeneeded(event);
                } catch (error) {
                    request.error = error;
                    if (typeof request.onerror === 'function') request.onerror(new DomEvent('error'));
                    return;
                }
            }
            request.result = entry.db;
            if (typeof request.onsuccess === 'function') request.onsuccess(new DomEvent('success'));
        });
        return request;
    }

    deleteDatabase(name) {
        this.databases.delete(name);
        const request = new FakeRequest();
        request.succeed(undefined);
        return request;
    }

    databases_() {
        return Array.from(this.databases.keys());
    }

    // Force every subsequent write to fail as if the origin were out of space.
    exhaustQuota() {
        this.quotaExceeded = true;
        for (const entry of this.databases.values()) {
            for (const store of entry.db._stores.values()) {
                store.put = (value, key) => {
                    const request = new FakeRequest();
                    const error = new Error('QuotaExceededError: storage full');
                    error.name = 'QuotaExceededError';
                    // Use fail() so both onerror and the transaction's
                    // addEventListener handlers observe the failure.
                    request.fail(error);
                    return request;
                };
            }
        }
    }

    restoreQuota() {
        this.quotaExceeded = false;
    }
}

module.exports = { FakeIndexedDB, FakeRequest };
