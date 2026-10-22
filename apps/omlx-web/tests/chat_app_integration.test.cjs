// Run with: node --test apps/omlx-web/tests/chat_app_integration.test.cjs
//
// These drive the REAL chat.html app object — extracted from the template at test
// time, not a copy — against fake storage.
//
// Why this file exists: every other test here exercises a storage module directly.
// The suite was green at the exact moment chat.html was (a) blanking every attachment
// before persisting, (b) ignoring the migration's `incomplete` refusal and serving a
// partial history, and (c) exporting from a blob frozen at migration time. All three
// were app-integration bugs and no module-level test could see them. Each test below
// is written against one of those failures.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { createChatApp, makeStorage } = require('./helpers/chat_app_harness.cjs');
const { FakeIndexedDB } = require('./helpers/fake_indexeddb.cjs');

const LEGACY_KEY = 'omlx_chat_history';
const PNG_B64 = Buffer.from('\x89PNG-fake-image-bytes').toString('base64');
const dataUrl = (b64 = PNG_B64) => `data:image/png;base64,${b64}`;

const chat = (id, messages, overrides = {}) => ({
    id,
    title: `Chat ${id}`,
    model: 'test-model',
    createdAt: 1700000000000,
    updatedAt: 1700000000000,
    messages,
    pinned: false,
    manualTitle: false,
    ...overrides,
});

const textMsg = (role, content) => ({ id: `m-${role}-${Math.random().toString(36).slice(2, 8)}`, role, content });

const imageMsg = (role, b64 = PNG_B64) => ({
    id: `m-img-${Math.random().toString(36).slice(2, 8)}`,
    role,
    content: [
        { type: 'text', text: 'look' },
        { type: 'image_url', image_url: { url: dataUrl(b64) } },
    ],
});

// Boots an app on IndexedDB, having resolved the backend the way startup does.
async function idbApp(options = {}) {
    const h = createChatApp({ indexedDB: options.indexedDB || new FakeIndexedDB(), legacy: options.legacy });
    await h.app.initChatStorage();
    return h;
}

// A second app over the same storage is the node stand-in for a page reload:
// nothing carries in memory, everything must come off disk.
function reload(h) {
    return createChatApp({ indexedDB: h.indexedDB, localStorage: h.localStorage });
}

function captureDownloads(app) {
    const out = [];
    app._downloadChatData = (data, name, mime) => { out.push({ data, name, mime }); };
    return out;
}

// Values that come back out of the vm context were built by that realm's Array, so
// assert.deepStrictEqual rejects them for prototype mismatch even when identical.
// Rebuild in the host realm before comparing structurally — note that calling
// .map() on a context array hands back a context array, so it must be a literal.
function host(value) {
    if (Array.isArray(value)) {
        const out = [];
        for (const v of value) out.push(host(v));
        return out;
    }
    return value;
}

// ---- attachments (the bug every module test missed) ----

test('an attachment survives save then reload through the real app', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    const ok = await h.app.saveChatHistory([chat('c1', [textMsg('user', 'hi'), imageMsg('user')])]);
    assert.equal(ok, true, 'saveChatHistory failed');

    const back = reload(h);
    await back.app.initChatStorage();
    const loaded = await back.app.loadChatHistory();
    assert.equal(loaded, true, 'loadChatHistory reported failure after reload');

    const stored = back.app.chatHistory.find((c) => c.id === 'c1');
    assert.ok(stored, 'the chat is gone after reload');
    const part = stored.messages[1].content[1];
    assert.equal(part.image_url.url, dataUrl(),
        'THE ATTACHMENT WAS LOST ON RELOAD — this is the bug the module tests could not see');
});

test('the hot single-record save path preserves attachments too', async () => {
    const h = await idbApp();
    const put = await h.app.saveChatRecord(chat('hot', [imageMsg('user')]));
    assert.equal(put, true, 'saveChatRecord failed');

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'hot');
    assert.ok(stored, 'chat missing after reload');
    assert.equal(stored.messages[0].content[1].image_url.url, dataUrl(),
        'the hot save path dropped the attachment');
});

test('a file part survives save and reload with its name and bytes', async () => {
    const h = await idbApp();
    const withFile = {
        id: 'f1', role: 'user',
        content: [{ type: 'file', file: { filename: 'spec.txt', mime_type: 'text/plain', data: Buffer.from('contents').toString('base64') } }],
    };
    await h.app.saveChatHistory([chat('files', [withFile])]);

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'files');
    const part = stored.messages[0].content[0];
    assert.equal(part.file.filename, 'spec.txt');
    assert.equal(Buffer.from(part.file.data, 'base64').toString(), 'contents');
});

test('no persist path in the template blanks media any more', () => {
    // The regression guard for the original defect: the template used to map
    // image_url -> {url:''} and file -> {data:''} before writing.
    const src = fs.readFileSync(path.join(__dirname, '..', 'omlx_web', 'templates', 'chat.html'), 'utf8');
    assert.equal(src.includes("image_url: { url: '' }"), false,
        'chat.html is blanking image parts again');
    assert.equal(/type:\s*'file'[\s\S]{0,120}data:\s*''/.test(src), false,
        'chat.html is blanking file parts again');
});

// ---- the two paths that actually had the defect ----
//
// saveChatHistory/saveChatRecord never blanked anything; the stripping lived in
// saveCurrentChat (send a message -> persist) and branchChat. A test that only
// covered the lower-level saves would have waved the original bug straight through.

test('saveCurrentChat — the send-a-message path — keeps attachments', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    h.app.currentModel = 'test-model';
    const msgs = [textMsg('user', 'here is a picture'), imageMsg('user')];
    await h.app.saveCurrentChat('c1', msgs, 'test-model', '');

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'c1');
    assert.ok(stored, 'the chat was not persisted by saveCurrentChat');
    assert.equal(stored.messages[1].content[1].image_url.url, dataUrl(),
        'saveCurrentChat DROPPED THE ATTACHMENT — this is the exact path a user hits when '
        + 'they send an image and reload');
});

test('saveCurrentChat keeps a file attachment on the same path', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c2';
    const withFile = {
        id: 'm1', role: 'user',
        content: [{ type: 'file', file: { filename: 'report.pdf', mime_type: 'application/pdf', data: Buffer.from('PDFDATA').toString('base64') } }],
    };
    await h.app.saveCurrentChat('c2', [withFile], 'test-model', '');

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const part = back.app.chatHistory.find((c) => c.id === 'c2').messages[0].content[0];
    assert.equal(part.file.filename, 'report.pdf');
    assert.equal(Buffer.from(part.file.data, 'base64').toString(), 'PDFDATA');
});

test('branchChat carries attachments into the branch', async () => {
    const h = await idbApp();
    const msgs = [textMsg('user', 'start'), imageMsg('user'), textMsg('assistant', 'noted')];
    h.app.currentChatId = 'orig';
    h.app.currentModel = 'test-model';
    h.app.chatHistory = [chat('orig', msgs)];
    h.app.chatSessions = { orig: { messages: msgs, model: 'test-model', systemPrompt: '', activeProfile: null, modelSettings: null, modelSettingsByModel: {} } };
    h.app.isCurrentChatStreaming = () => false;

    await h.app.branchChat(1);

    const branch = h.app.chatHistory.find((c) => c.id !== 'orig');
    assert.ok(branch, 'no branch chat was created');
    assert.equal(branch.messages[1].content[1].image_url.url, dataUrl(),
        'branchChat DROPPED THE ATTACHMENT when copying the conversation');

    // And it must survive a reload from the record store, not just sit in memory.
    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const persisted = back.app.chatHistory.find((c) => c.id === branch.id);
    assert.ok(persisted, 'the branch was not persisted');
    assert.equal(persisted.messages[1].content[1].image_url.url, dataUrl(),
        'the branch attachment did not survive reload');
});

// ---- explicit deletion through the whole-history path ----

for (const backend of ['indexedDB', 'localStorage']) {
    test(`deleteChat removes the stored record and survives reload (${backend})`, async () => {
        const h = createChatApp({ indexedDB: backend === 'indexedDB' ? new FakeIndexedDB() : null });
        await h.app.initChatStorage();
        assert.equal(await h.app.saveChatHistory([chat('delete-me', []), chat('keep-me', [])]), true);
        const orphanedStream = h.app.getStreamSession('delete-me', true);

        await h.app.deleteChat('delete-me', { skipConfirm: true });

        const back = reload(h);
        await back.app.initChatStorage();
        assert.equal(await back.app.loadChatHistory(), true);
        assert.deepEqual(host(back.app.chatHistory.map((c) => c.id)), ['keep-me']);
        assert.equal((await back.app.chatRecordStore().get('delete-me')).record, null);
        assert.ok((await back.app.chatRecordStore().get('keep-me')).record);
        assert.equal(await h.app.saveCurrentChat('delete-me', [textMsg('user', 'late stream write')]), false);
        assert.equal((await h.app.chatRecordStore().get('delete-me')).record, null,
            'a late save from the deleted chat recreated its record');
        assert.equal(orphanedStream._discardWrites, true);
        assert.equal(await h.app.saveStreamChat(orphanedStream, 'delete-me', [textMsg('assistant', 'late finalizer')]), false);
        assert.equal((await h.app.chatRecordStore().get('delete-me')).record, null,
            'a discarded stream finalizer recreated its record');
    });
}

test('clearAllHistory removes every persisted chat before reload', async () => {
    const h = createChatApp({ indexedDB: null });
    await h.app.initChatStorage();
    assert.equal(await h.app.saveChatHistory([chat('clear-a', []), chat('clear-b', [])]), true);
    h.localStorage.setItem('omlx_chat_v2:corrupt-unindexed', '{broken');
    h.context.confirm = () => true;

    await h.app.clearAllHistory();

    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true);
    const remaining = host(back.app.chatHistory.map((c) => c.id));
    assert.equal(remaining.includes('clear-a'), false);
    assert.equal(remaining.includes('clear-b'), false);
    assert.equal(remaining.includes('corrupt-unindexed'), false);
    assert.equal(h.localStorage.getItem('omlx_chat_v2:corrupt-unindexed'), null,
        'clear-all left an unindexed corrupt record behind');
    assert.equal(remaining.length, 1, 'clear-all should leave its fresh empty chat');
});

test('failed chat deletion keeps its removal intent for retry', async () => {
    const h = await idbApp();
    assert.equal(await h.app.saveChatHistory([chat('retry-delete', []), chat('keep-retry', [])]), true);
    const retainedStream = h.app.getStreamSession('retry-delete', true);
    const store = h.app.chatRecordStore();
    const remove = store.remove.bind(store);
    let failOnce = true;
    store.remove = async (id) => {
        if (id === 'retry-delete' && failOnce) {
            failOnce = false;
            return { ok: false, kind: 'unavailable' };
        }
        return remove(id);
    };

    await h.app.deleteChat('retry-delete', { skipConfirm: true });
    assert.equal(h.app.chatStorageIssue?.action, 'delete');
    assert.equal(h.app._deletedChatIds.has('retry-delete'), true);
    assert.ok(h.app.chatHistory.some(c => c.id === 'retry-delete'), 'failed deletion hid the recoverable chat');
    assert.equal(retainedStream._discardWrites, undefined, 'failed deletion discarded the active stream');
    assert.equal(await h.app.saveCurrentChat('retry-delete', [textMsg('user', 'late buffered output')]), false);
    assert.equal(h.app.chatStorageIssue?.action, 'delete',
        'a late stream save cleared the pending deletion error and retry intent');
    assert.deepEqual(host(h.app.chatStorageIssue?.removedIds), ['retry-delete']);
    assert.equal(await h.app.saveChatHistory([chat('retry-delete', [], { title: 'premature import' })],
        { action: 'import' }), false, 'an import bypassed a pending deletion');
    assert.equal(h.app.chatStorageIssue?.action, 'delete');
    assert.equal(await h.app.saveChatRecord(chat('keep-retry', [], { title: 'other chat updated' })), true);
    assert.equal(h.app.chatStorageIssue?.action, 'delete',
        'an unrelated successful save cleared the pending deletion error');
    assert.deepEqual(host(h.app.chatStorageIssue?.removedIds), ['retry-delete']);
    store.remove = remove;
    await h.app.retryChatHistorySave();

    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true);
    assert.deepEqual(host(back.app.chatHistory.map((c) => c.id)), ['keep-retry']);
    assert.equal(retainedStream._discardWrites, true, 'retry did not retire the original stream');

    const restored = [...h.app.chatHistory, chat('retry-delete', [], { title: 'intentional import' })];
    assert.equal(await h.app.saveChatHistory(restored, { action: 'import' }), true);
    assert.equal((await h.app.chatRecordStore().get('retry-delete')).record.title, 'intentional import');
    assert.equal(await h.app.saveStreamChat(retainedStream, 'retry-delete',
        [textMsg('assistant', 'stale finalizer')]), false);
    assert.equal((await h.app.chatRecordStore().get('retry-delete')).record.title, 'intentional import',
        'the retired stream overwrote an intentionally imported chat with the same id');
});

test('a record write already in flight commits before the deletion', async () => {
    const h = await idbApp();
    assert.equal(await h.app.saveChatHistory([chat('race-delete', [])]), true);
    const store = h.app.chatRecordStore();
    const put = store.put.bind(store);
    let markStarted;
    const started = new Promise(resolve => { markStarted = resolve; });
    let releaseWrite;
    const gate = new Promise(resolve => { releaseWrite = resolve; });
    store.put = async (...args) => {
        const result = await put(...args);
        markStarted();
        await gate;
        return result;
    };

    const saving = h.app.saveChatRecord(chat('race-delete', [], { title: 'late update' }));
    await started;
    const deleting = h.app.deleteChat('race-delete', { skipConfirm: true });
    releaseWrite();
    assert.equal(await saving, true);
    await deleting;

    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true);
    assert.deepEqual(host(back.app.chatHistory.map((c) => c.id)), []);
});

test('overlapping deletes preserve every failed removal for retry', async () => {
    const h = await idbApp();
    assert.equal(await h.app.saveChatHistory([chat('delete-a', []), chat('delete-b', [])]), true);
    h.app.currentChatId = 'unrelated-current';
    const store = h.app.chatRecordStore();
    const remove = store.remove.bind(store);
    let markAStarted;
    let markBStarted;
    let releaseA;
    let releaseB;
    const aStarted = new Promise(resolve => { markAStarted = resolve; });
    const bStarted = new Promise(resolve => { markBStarted = resolve; });
    const aGate = new Promise(resolve => { releaseA = resolve; });
    const bGate = new Promise(resolve => { releaseB = resolve; });
    store.remove = async (id) => {
        if (id === 'delete-a') {
            markAStarted();
            await aGate;
            return { ok: false, kind: 'unavailable' };
        }
        if (id === 'delete-b') {
            markBStarted();
            await bGate;
        }
        return remove(id);
    };

    const deletingA = h.app.deleteChat('delete-a', { skipConfirm: true });
    await aStarted;
    const deletingB = h.app.deleteChat('delete-b', { skipConfirm: true });
    await bStarted;
    releaseA();
    await deletingA;
    releaseB();
    await deletingB;
    assert.deepEqual(host(h.app._pendingRemovalIds), ['delete-a']);
    assert.equal(h.app.chatStorageIssue?.action, 'delete');

    store.remove = remove;
    await h.app.retryChatHistorySave();
    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true);
    assert.deepEqual(host(back.app.chatHistory.map((c) => c.id)), []);
});

test('clear-all retry preserves its action and retires streams after repeated failure', async () => {
    const h = await idbApp();
    assert.equal(await h.app.saveChatHistory([chat('clear-retry-a', []), chat('clear-retry-b', [])]), true);
    const orphanedStream = h.app.getStreamSession('unsaved-stream', true);
    const store = h.app.chatRecordStore();
    const remove = store.remove.bind(store);
    let failuresRemaining = 2;
    store.remove = async (id) => {
        if (id === 'clear-retry-a' && failuresRemaining > 0) {
            failuresRemaining -= 1;
            return { ok: false, kind: 'unavailable' };
        }
        return remove(id);
    };
    h.context.confirm = () => true;

    await h.app.clearAllHistory();
    assert.equal(h.app.chatStorageIssue?.action, 'clear');
    await h.app.retryChatHistorySave();
    assert.equal(h.app.chatStorageIssue?.action, 'clear', 'a failed retry changed clear into an ordinary save');

    store.remove = remove;
    await h.app.retryChatHistorySave();
    assert.equal(orphanedStream._discardWrites, true);
    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true);
    const remaining = host(back.app.chatHistory.map((c) => c.id));
    assert.equal(remaining.includes('clear-retry-a'), false);
    assert.equal(remaining.includes('clear-retry-b'), false);
});

// ---- the migration refusal (bug: app ignored it and served a partial list) ----

test('discarded stream finalizer cannot overwrite an intentional same-ID import', async () => {
    const h = await idbApp();
    assert.equal(await h.app.saveChatHistory([chat('reuse-id', [])]), true);
    const capturedSession = h.app.getChatSession('reuse-id', true);
    const capturedStream = h.app.getStreamSession('reuse-id', true);
    h.app.currentChatId = 'unrelated-current';

    await h.app.deleteChat('reuse-id', { skipConfirm: true });
    assert.equal(capturedStream._discardWrites, true);
    const replacement = chat('reuse-id', [textMsg('user', 'replacement')], {
        title: 'intentional replacement',
    });
    assert.equal(await h.app.saveChatHistory([replacement], { action: 'import' }), true);

    capturedStream.streamingContent = 'stale partial output';
    const result = h.app.commitAccumulatedStream({
        chatId: 'reuse-id',
        systemPrompt: '',
        _profile: null,
        _generationOverride: null,
        _variantUserIndex: null,
    }, capturedSession, capturedStream, 'test-model');
    assert.equal(result, null, 'discarded finalizer should not append to its captured session');
    assert.equal(capturedSession.messages.length, 0);
    await h.app._chatWriteChain['reuse-id'];
    const stored = await h.app.chatRecordStore().get('reuse-id');
    assert.equal(stored.record.title, 'intentional replacement');
    assert.equal(stored.record.messages[0].content, 'replacement');
});

test('unexpected stream failure finalizes partial text and reasoning without interruption metadata', async () => {
    const h = await idbApp();
    await h.app.saveChatHistory([chat('stream-error', [textMsg('user', 'prompt')])]);
    const session = h.app.getChatSession('stream-error', true);
    const stream = h.app.getStreamSession('stream-error', true);
    stream.targetMessageId = 'assistant-partial';
    stream._toolRoundContent = 'earlier tool-round text';
    stream.streamingContent = 'partial answer';
    stream.streamingThinking = 'partial reasoning';

    const msg = h.app.commitAccumulatedStream({
        chatId: 'stream-error', systemPrompt: '', _profile: null,
        _generationOverride: null, _variantUserIndex: 0,
    }, session, stream, 'test-model', new Error('connection lost'));
    await h.app._chatWriteChain['stream-error'];

    assert.equal(msg.content, 'earlier tool-round textpartial answer\n\nError: connection lost');
    assert.equal(msg.reasoning_content, 'partial reasoning');
    assert.equal(Object.hasOwn(msg, '_interrupted'), false);
    assert.equal(Object.hasOwn(msg, '_interruptionReason'), false);

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const saved = back.app.chatHistory.find((c) => c.id === 'stream-error');
    assert.equal(saved.messages[1].content, msg.content);
    assert.equal(saved.messages[1].reasoning_content, 'partial reasoning');
});

test('a stopped stream finalizes received output through the same guarded save path', async () => {
    const h = await idbApp();
    h.context.AbortController = AbortController;
    h.app.startStatsPolling = () => {};
    h.app.startPrefillPolling = () => {};
    h.app.stopStatsPollingIfIdle = () => {};
    h.app.setEngineStatus = () => {};
    await h.app.saveChatHistory([chat('stream-stop', [textMsg('user', 'prompt')])]);
    const encoder = new TextEncoder();
    let reads = 0;
    h.context.fetch = async () => ({
        ok: true,
        body: { getReader: () => ({
            async read() {
                reads += 1;
                if (reads === 1) {
                    return { done: false, value: encoder.encode(
                        'data: {"choices":[{"delta":{"content":"partial answer"}}]}\n\n'
                    ) };
                }
                const abort = new Error('Stopped by user');
                abort.name = 'AbortError';
                throw abort;
            },
            async cancel() {},
        }) },
    });

    await h.app.streamResponse({ chatId: 'stream-stop', model: 'test-model' });
    await h.app._chatWriteChain['stream-stop'];

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    assert.equal(back.app.chatHistory.find((c) => c.id === 'stream-stop').messages[1].content,
        'partial answer');
});

test('an unexpected stream error with no output remains visible and persists', async () => {
    const h = await idbApp();
    h.context.AbortController = AbortController;
    h.app.startStatsPolling = () => {};
    h.app.startPrefillPolling = () => {};
    h.app.stopStatsPollingIfIdle = () => {};
    h.app.setEngineStatus = () => {};
    h.context.fetch = async () => { throw new Error('connection lost'); };
    await h.app.saveChatHistory([chat('empty-stream-error', [textMsg('user', 'prompt')])]);

    await h.app.streamResponse({ chatId: 'empty-stream-error', model: 'test-model' });
    await h.app._chatWriteChain['empty-stream-error'];

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const saved = back.app.chatHistory.find((c) => c.id === 'empty-stream-error');
    assert.equal(saved.messages.at(-1).role, 'assistant');
    assert.equal(saved.messages.at(-1).content, 'Error: connection lost');
});

test('an un-migratable legacy chat stops the load instead of vanishing from it', async () => {
    const legacy = [
        { id: 'ok-1', title: 'kept', messages: [] },
        { title: 'NO ID — cannot become a record' },
        { id: 'ok-2', title: 'kept too', messages: [] },
    ];
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), legacy });
    await h.app.initChatStorage();

    const loaded = await h.app.loadChatHistory();

    assert.equal(loaded, false, 'load must fail: serving the migrated subset would hide the refused chat');
    assert.equal(h.app.chatStorageIssue?.kind, 'incomplete',
        'the app must surface the refusal, not swallow it');
    assert.ok(!h.app.chatMutationAllowed(), 'writes must be blocked while the migration is unresolved');

    // The decisive assertion: the partial list must NOT be what the user is shown.
    const shown = h.app.chatHistory || [];
    assert.equal(shown.length, 0,
        `THE APP SERVED A PARTIAL HISTORY (${shown.length} chats) WHILE ONE WAS REFUSED — `
        + 'the user would never know a chat was missing');
    assert.ok(h.app.chatStorageIssue.raw, 'the retained legacy value must be offered for recovery');
    assert.ok(String(h.app.chatStorageIssue.raw).includes('NO ID'),
        'the recovery payload must contain the refused chat');
});

test('the refusal does not destroy the legacy data', async () => {
    const legacy = [{ id: 'ok-1', title: 'kept', messages: [] }, { title: 'orphan' }];
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), legacy });
    await h.app.initChatStorage();
    await h.app.loadChatHistory();

    const retained = JSON.parse(h.localStorage.getItem(LEGACY_KEY));
    assert.equal(retained.length, 2, 'the legacy blob must be retained whole');
    assert.ok(retained.some((e) => e.title === 'orphan'), 'the refused chat was deleted from the legacy blob');
});

test('a clean migration still loads normally', async () => {
    const legacy = [{ id: 'a', title: 'A', messages: [] }, { id: 'b', title: 'B', messages: [] }];
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), legacy });
    await h.app.initChatStorage();
    const loaded = await h.app.loadChatHistory();
    assert.equal(loaded, true);
    assert.equal(h.app.chatStorageIssue, null, 'no issue should be raised for a clean migration');
    assert.deepEqual(host(h.app.chatHistory.map((c) => c.id).sort()), ['a', 'b']);
});

// ---- export (bug: read a blob frozen at migration time) ----

test('export contains a chat created after the migration', async () => {
    const legacy = [{ id: 'old-1', title: 'Old one', messages: [textMsg('user', 'from legacy')] }];
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), legacy });
    await h.app.initChatStorage();
    assert.equal(await h.app.loadChatHistory(), true);

    // A chat created after migration lives only in the record store.
    await h.app.saveChatHistory([
        ...h.app.chatHistory,
        chat('brand-new', [textMsg('user', 'written after migration')]),
    ]);

    const dl = captureDownloads(h.app);
    await h.app.downloadChats();

    assert.equal(dl.length, 1, 'no export was produced');
    const exported = JSON.parse(dl[0].data);
    const ids = exported.map((c) => c.id).sort();
    assert.ok(ids.includes('brand-new'),
        `EXPORT IS STALE — the post-migration chat is absent (exported: ${JSON.stringify(ids)}). `
        + 'This is the bug where downloadChats() read the legacy blob.');
    assert.ok(ids.includes('old-1'), 'the migrated chat should still be exported');
});

test('export carries attachments, not blanked parts', async () => {
    const h = await idbApp();
    await h.app.saveChatHistory([chat('withimg', [imageMsg('user')])]);
    const dl = captureDownloads(h.app);
    await h.app.downloadChats();
    assert.equal(dl.length, 1);
    const exported = JSON.parse(dl[0].data);
    const part = exported[0].messages[0].content[1];
    assert.equal(part.image_url.url, dataUrl(),
        'the export dropped the attachment bytes — an archive that cannot restore them is not a backup');
});

test('export of an empty history alerts rather than writing an empty archive', async () => {
    const h = await idbApp();
    const dl = captureDownloads(h.app);
    const alerts = [];
    h.window.alert = (m) => alerts.push(m);
    await h.app.downloadChats();
    assert.equal(dl.length, 0, 'an empty archive was written');
    assert.equal(alerts.length, 1, 'the user was not told there is nothing to export');
});

// ---- backend selection ----

test('the app selects IndexedDB when it is available', async () => {
    const h = await idbApp();
    assert.equal(h.app.chatStorageBackend, 'indexedDB');
    assert.equal(h.app.chatRecordStore().supportsBlobs, true);
});

test('the app falls back to localStorage and still keeps working', async () => {
    const h = createChatApp({ indexedDB: null });
    await h.app.initChatStorage();
    assert.equal(h.app.chatStorageBackend, 'localStorage');
    assert.equal(h.app.chatRecordStore().supportsBlobs, false);

    // On this backend media genuinely cannot be kept — but it must be reported,
    // not silently dropped.
    const ok = await h.app.saveChatHistory([chat('c1', [imageMsg('user')])]);
    assert.equal(ok, true, 'a text-plus-image chat must still save on localStorage');
    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'c1');
    assert.ok(stored, 'the chat itself must survive even when media cannot');
    assert.equal(stored.messages[0].content[1].image_url.mediaDropped, 'backend',
        'the dropped attachment must be marked, not silently blanked');
});

// ---- revision safety through the app ----

test('a stale save through the app is refused and the stored attachment survives', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    await h.app.saveChatHistory([chat('c1', [imageMsg('user', PNG_B64)])]);
    const rev = h.app.recordRevFor('c1');
    assert.ok(Number.isInteger(rev));

    // Another tab wins the race.
    const other = await idbApp({ indexedDB: h.indexedDB });
    other.app.rememberRecordRev('c1', rev);
    await other.app.saveChatRecord(chat('c1', [textMsg('user', 'tab B')]), { action: 'chat' });

    // This tab still holds the old revision and must be refused.
    const ok = await h.app.saveChatHistory([chat('c1', [textMsg('user', 'tab A stale')])]);
    assert.equal(ok, false, 'a stale whole-history save must not succeed');
    assert.equal(h.app.chatStorageIssue?.kind, 'conflict');

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'c1');
    assert.equal(stored.messages[0].content, 'tab B', 'the stale write overwrote the newer one');
});

// ---- self-conflict: the banner that blamed a tab that was never open ----

// Real IndexedDB queues a readwrite transaction behind any earlier one touching the
// same store, so its reads see that transaction's committed writes. The fake did not
// do this: it let two overlapping transactions both read the pre-state, which made
// every same-tab compare-and-set look fine. The suite stayed green for the whole time
// a variant switch in Chrome raised "Chat history was not saved".
test('the fake serialises overlapping readwrite puts, as Chrome does', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    assert.equal(await h.app.saveChatHistory([chat('c1', [textMsg('user', 'seed')])]), true);
    const rev = h.app.recordRevFor('c1');

    const store = h.app.chatRecordStore();
    const [a, b] = await Promise.all([
        store.put(chat('c1', [textMsg('user', 'A')]), { expectRev: rev }),
        store.put(chat('c1', [textMsg('user', 'B')]), { expectRev: rev }),
    ]);
    const refused = [a, b].find((r) => !r.ok);
    assert.ok(refused,
        'the fake let two overlapping writes through — it is more forgiving than '
        + 'Chrome again, and nothing built on it can see the self-conflict bug');
    assert.equal(refused.kind, 'conflict');
});

test('two overlapping saves for one chat both commit instead of self-conflicting', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    await h.app.saveChatHistory([chat('c1', [textMsg('user', 'seed')])]);

    const results = await Promise.all([
        h.app.saveCurrentChat('c1'),
        h.app.saveCurrentChat('c1'),
    ]);

    assert.deepEqual(host(results), [true, true],
        'a save must never be refused because of another save from this same tab');
    assert.equal(h.app.chatStorageIssue, null,
        'no banner: nothing else wrote, so "changed in another tab" would be a lie');
});

test('switching response variants persists the choice and raises no banner', async () => {
    // The reported flow: re-roll, then click another response tab.
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    h.app.chatHistory = [];
    const session = h.app.getChatSession('c1', true);
    const user = { id: 'u1', role: 'user', content: 'hello' };
    const v1 = { id: 'v1', role: 'assistant', content: 'first', model: 'm1', meta: { modelId: 'm1' } };
    const v2 = { id: 'v2', role: 'assistant', content: 'second', model: 'm2', meta: { modelId: 'm2' } };
    user._activeVariantId = 'v1';
    session.messages = [user, v1, v2];
    session.model = 'm1';
    h.app.messages = session.messages;
    await h.app.saveCurrentChat('c1', session.messages, 'm1', '');

    await h.app.selectVariant(0, 'v2');

    assert.equal(h.app.chatStorageIssue, null,
        'selectVariant fired two writes for one chat; the second was always refused');

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'c1');
    assert.equal(stored.messages[0]._activeVariantId, 'v2',
        'THE VARIANT SWITCH WAS LOST ON RELOAD');
});

test('a refused single-record save stays recoverable and imports', async () => {
    const h = await idbApp();
    h.app.currentChatId = 'c1';
    await h.app.saveChatHistory([chat('c1', [textMsg('user', 'saved')])]);

    // Force the guard to refuse by claiming a revision that is not on disk.
    h.app.rememberRecordRev('c1', h.app.recordRevFor('c1') + 5);
    const ok = await h.app.saveChatRecord(
        chat('c1', [textMsg('user', 'unsaved edit')], { title: 'edited' }),
        { action: 'chat' });
    assert.equal(ok, false);
    assert.equal(h.app.chatStorageIssue?.kind, 'conflict');

    const pending = h.app.pendingChatHistory();
    assert.ok(Array.isArray(pending), 'pendingChatHistory must always hand back a list');
    assert.ok(pending.some((c) => c.title === 'edited'),
        'the refused chat is the only copy of that edit; dropping it here loses it '
        + 'from the base of every later save');

    const downloads = captureDownloads(h.app);
    h.app.downloadPendingChatHistory();
    const file = JSON.parse(downloads[0].data);
    assert.ok(Array.isArray(file),
        'the recovery file must be a list — importChats rejects a bare object, so '
        + 'an unimportable backup is no backup');
    assert.ok(file.some((c) => c.title === 'edited'),
        'the recovery download must contain the chat that failed to save');
});

// ---- i18n ----

// Key parity, not a locale count. A hardcoded count broke the day upstream added
// cs.json: it failed for the wrong reason (ten files, not nine) while staying silent
// on the reason that mattered (the new locale had none of our strings). Parity fails
// for the reason that matters and survives a locale being added on either side.
test('every locale is in key parity with en.json', () => {
    const dir = path.join(__dirname, '..', 'omlx_web', 'i18n');
    const files = fs.readdirSync(dir).filter((f) => f.endsWith('.json'));
    assert.ok(files.includes('en.json'), 'en.json is the reference and must exist');
    const enKeys = Object.keys(JSON.parse(fs.readFileSync(path.join(dir, 'en.json'), 'utf8'))).sort();

    for (const f of files) {
        const keys = Object.keys(JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'))).sort();
        const missing = enKeys.filter((k) => !keys.includes(k));
        const extra = keys.filter((k) => !enKeys.includes(k));
        assert.deepEqual(missing, [],
            `${f} is missing ${missing.length} key(s) that en.json has — users of that `
            + `locale see raw dotted keys. First few: ${missing.slice(0, 6).join(', ')}`);
        assert.deepEqual(extra, [],
            `${f} carries ${extra.length} key(s) en.json does not. First few: ${extra.slice(0, 6).join(', ')}`);
    }
});

// No length thresholds here. They cannot work across scripts: zh's "聊天记录未保存" is
// a complete 7-character sentence, and fr's "Télécharger les discussions non
// enregistrées" is a correct 44-character button. Any absolute bound is wrong for some
// locale, and a threshold that a correct string violates gets the guard removed rather
// than the string fixed. These checks are script-independent instead.
const NON_LATIN = ['ja', 'ko', 'zh', 'zh-TW', 'ru'];

test('the storage strings are real copy, not placeholders', () => {
    const dir = path.join(__dirname, '..', 'omlx_web', 'i18n');
    const en = JSON.parse(fs.readFileSync(path.join(dir, 'en.json'), 'utf8'));
    const keys = Object.keys(en).filter((k) => k.startsWith('chat.storage_error') || k === 'chat.multi_tab_notice');
    assert.ok(keys.length >= 15, `expected the full storage-safety family, found ${keys.length}`);

    for (const file of fs.readdirSync(dir).filter((x) => x.endsWith('.json'))) {
        const locale = file.replace(/\.json$/, '');
        const dict = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));

        for (const k of keys) {
            const v = dict[k];
            assert.ok(typeof v === 'string' && v.trim().length > 0,
                `${file} · ${k} has no copy`);
            assert.equal(v.trim(), v, `${file} · ${k} has stray whitespace`);
            assert.equal(v.includes('storage_error.'), false,
                `${file} · ${k} holds a dotted key where it should hold copy`);

            // Untranslated English left in place is the failure a length check misses.
            if (locale !== 'en') {
                assert.notEqual(v, en[k],
                    `${file} · ${k} is still the English string — not translated`);
            }
            if (NON_LATIN.includes(locale)) {
                assert.ok(/[^\x00-\x7f]/.test(v),
                    `${file} · ${k} is pure ASCII in a non-Latin locale — untranslated`);
            }
        }
    }
});

// English patterns only, and deliberately so. Running these against cs/ja/ko would
// match nothing and report green — a guard that cannot fail is worse than none.
test('the English storage warnings never tell the user to delete anything', () => {
    const en = JSON.parse(fs.readFileSync(
        path.join(__dirname, '..', 'omlx_web', 'i18n', 'en.json'), 'utf8'));
    for (const [k, v] of Object.entries(en)) {
        if (!k.startsWith('chat.storage_error') && k !== 'chat.multi_tab_notice') continue;
        assert.equal(/\b(delete all|clear (your )?history|erase|wipe|drop)\b/i.test(v), false,
            `${k} tells the user to delete data — never the advice for a storage warning`);
    }
});

// ---- reactive state ----
// Alpine hands the save path a Proxy tree. IndexedDB's structured clone rejects a
// Proxy outright, so before the backend normalised, every save from live component
// state failed and the UI said "Browser storage is unavailable". Nothing above this
// line could see it: the module tests all passed plain objects.

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

test('saveChatRecord accepts a reactive chat, the shape Alpine hands it', async () => {
    const h = await idbApp();
    const ok = await h.app.saveChatRecord(
        reactiveTree(chat('rx', [textMsg('user', 'hello from reactive state')])),
        { action: 'chat' });
    assert.equal(ok, true, 'a reactive chat must save, not be reported as a storage failure');
    assert.equal(h.app.chatStorageIssue, null, 'no banner may be raised');

    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true);
    const stored = back.app.chatHistory.find((c) => c.id === 'rx');
    assert.ok(stored, 'the reactive chat must come back off disk');
    assert.equal(stored.messages[0].content, 'hello from reactive state');
});

test('saveChatRecord accepts a reactive chat carrying an inline attachment', async () => {
    const h = await idbApp();
    const ok = await h.app.saveChatRecord(
        reactiveTree(chat('rx-img', [textMsg('user', 'look'), imageMsg('user')])),
        { action: 'chat' });
    assert.equal(ok, true, `reactive attachment save failed: ${JSON.stringify(h.app.chatStorageIssue)}`);
    assert.equal(h.app.chatStorageIssue, null);

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'rx-img');
    assert.ok(stored);
    const url = stored.messages[1].content[1].image_url.url;
    assert.equal(url, dataUrl(), 'the attachment must survive byte-identical');
});

test('saveChatHistory accepts a reactive history array', async () => {
    const h = await idbApp();
    const list = reactiveTree([chat('r1', [textMsg('user', 'a')]), chat('r2', [textMsg('user', 'b')])]);
    const ok = await h.app.saveChatHistory(list, { action: 'save' });
    assert.equal(ok, true, 'a reactive history must save');
    assert.equal(h.app.chatStorageIssue, null);

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    assert.deepEqual(host(back.app.chatHistory.map((c) => c.id).sort()), ['r1', 'r2']);
});

// ---- plain-HTTP origin ----
// The supported deployment is ordinary HTTP on a trusted LAN or Tailscale address.
// Chrome then reports isSecureContext false and withholds navigator.storage and
// crypto.subtle, while IndexedDB works normally. Serving over HTTPS is not an
// acceptable fix, so this pins the behaviour that makes it unnecessary.

test('a plain-HTTP origin still selects IndexedDB and saves chats', async () => {
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), insecure: true });
    assert.equal(h.window.isSecureContext, false);
    assert.equal(h.window.navigator.storage, undefined, 'navigator.storage must be absent');
    assert.equal(h.window.crypto.subtle, undefined, 'crypto.subtle must be absent');

    await h.app.initChatStorage();
    assert.equal(h.app.chatStorageBackend, 'indexedDB',
        'IndexedDB must still be chosen when navigator.storage is missing');
    assert.equal(h.app.chatStorageIssue, null);

    const ok = await h.app.saveChatRecord(
        reactiveTree(chat('lan', [textMsg('user', 'over the LAN'), imageMsg('user')])),
        { action: 'chat' });
    assert.equal(ok, true, 'saving must work over plain HTTP');
    assert.equal(h.app.chatStorageIssue, null);

    const back = reload(h);
    await back.app.initChatStorage();
    assert.equal(await back.app.loadChatHistory(), true, 'history must load over plain HTTP');
    const stored = back.app.chatHistory.find((c) => c.id === 'lan');
    assert.ok(stored, 'the chat must survive');
    assert.equal(stored.messages[0].content, 'over the LAN');
});

test('without crypto.subtle attachments are kept inline, not dropped', async () => {
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), insecure: true });
    await h.app.initChatStorage();
    const store = h.app.chatRecordStore();
    assert.equal(store.media.supported, false,
        'content addressing needs crypto.subtle, which an insecure origin does not have');

    const b64 = PNG_B64;
    const ok = await h.app.saveChatRecord(chat('inline', [imageMsg('user', b64)]), { action: 'chat' });
    assert.equal(ok, true);
    assert.equal(h.app.chatStorageIssue, null);

    const back = reload(h);
    await back.app.initChatStorage();
    await back.app.loadChatHistory();
    const stored = back.app.chatHistory.find((c) => c.id === 'inline');
    assert.ok(stored, 'the chat must not be lost with its attachment');
    const part = stored.messages[0].content[1];
    assert.equal(part.image_url.url, dataUrl(b64),
        'the attachment must be held inline and come back byte-identical');
    assert.equal(part.image_url.mediaDropped, undefined,
        'an inline attachment must not be reported as dropped');
});

test('the app never consults navigator.storage on the save path', async () => {
    const h = createChatApp({ indexedDB: new FakeIndexedDB(), insecure: true });
    // navigator.storage is absent, so any unguarded read would throw. If the save
    // path completes, nothing on it reached for it.
    await h.app.initChatStorage();
    const ok = await h.app.saveChatHistory([chat('nosm', [textMsg('user', 'x')])]);
    assert.equal(ok, true);
    const listed = await h.app.chatRecordStore().listIndex();
    assert.equal(listed.ok, true);
    assert.equal(h.app.chatStorageIssue, null);
});
