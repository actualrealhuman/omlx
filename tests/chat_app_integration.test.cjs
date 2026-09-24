// Run with: node --test tests/chat_app_integration.test.cjs
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
    const src = fs.readFileSync(path.join(__dirname, '..', 'omlx', 'admin', 'templates', 'chat.html'), 'utf8');
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

// ---- the migration refusal (bug: app ignored it and served a partial list) ----

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

// ---- i18n ----

test('every locale carries the incomplete-migration banner the app now shows', () => {
    const dir = path.join(__dirname, '..', 'omlx', 'admin', 'i18n');
    const files = fs.readdirSync(dir).filter((f) => f.endsWith('.json'));
    assert.equal(files.length, 9, 'expected nine locale files');
    for (const f of files) {
        const dict = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
        const v = dict['chat.storage_error.incomplete'];
        assert.ok(typeof v === 'string' && v.length > 20,
            `${f} is missing a usable chat.storage_error.incomplete string`);
        // The banner must not tell the user to delete anything.
        assert.equal(/\b(delete all|clear (your )?history|erase)\b/i.test(v), false,
            `${f} tells the user to delete data — that is never the advice for this banner`);
    }
});
