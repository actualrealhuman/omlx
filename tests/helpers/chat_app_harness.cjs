// Loads the real chat.html app object into node so its storage logic can be
// tested without the oMLX server, a model, or a browser.
//
// Why this exists: every other test in this directory exercises a storage module
// directly. That is how the suite stayed green while chat.html was blanking every
// attachment before persisting, ignoring the migration's `incomplete` refusal, and
// exporting from a stale blob. Those were app-integration bugs, and nothing here
// could see them.
//
// How: the template's app logic is one <script> block containing a single
// `function chatApp()` factory and exactly one Jinja expression. We extract that
// block from the template at test time — so the test runs the real template, not a
// copy that can drift — substitute the Jinja, evaluate it against stubbed browser
// globals, and hand back the app object.
//
// The stubs are deliberately thin. Anything the app calls that we have not stubbed
// throws, which is the point: a test that silently no-ops past a missing dependency
// proves nothing.

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { FakeIndexedDB } = require('./fake_indexeddb.cjs');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const TEMPLATE = path.join(REPO_ROOT, 'omlx', 'admin', 'templates', 'chat.html');
const STATIC = path.join(REPO_ROOT, 'omlx', 'admin', 'static', 'js');

// Storage modules the template expects as classic-script globals.
const STORAGE_MODULES = [
    'chat_history_storage.js',
    'chat_record_store.js',
    'chat_media_store.js',
    'chat_indexeddb_store.js',
    'chat_history_migration.js',
];

// Pulls the app script block out of the template.
//
// Located by content, not by line number: a hardcoded range would silently rot the
// first time someone edits the template, and the failure would look like a broken
// test rather than a stale offset.
function extractAppBlock() {
    const html = fs.readFileSync(TEMPLATE, 'utf8');
    const blocks = [];
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
    let m;
    while ((m = re.exec(html)) !== null) blocks.push(m[1]);
    const app = blocks.find((b) => /function\s+chatApp\s*\(/.test(b));
    if (!app) {
        throw new Error('could not find the chatApp() script block in chat.html — '
            + 'the template structure changed and this harness must be updated');
    }
    // One Jinja expression injects the server API key. Null is the honest test value:
    // there is no server here, and a fake key would exercise a path that cannot happen.
    const jinjaCount = (app.match(/\{\{/g) || []).length;
    if (jinjaCount > 1) {
        throw new Error(`chatApp block now contains ${jinjaCount} Jinja expressions; `
            + 'this harness substitutes exactly one and cannot be trusted beyond that');
    }
    return app.replace(/\{\{[^}]*\}\}/g, 'null');
}

function makeStorage() {
    const map = new Map();
    const store = {
        getItem(k) { return map.has(String(k)) ? map.get(String(k)) : null; },
        setItem(k, v) { map.set(String(k), String(v)); },
        removeItem(k) { map.delete(String(k)); },
        clear() { map.clear(); },
        key(i) { return Array.from(map.keys())[i] ?? null; },
        get length() { return map.size; },
        _map: map,
    };
    return store;
}

// A DOM stub that records what the app asked for and otherwise stays out of the way.
// Anything genuinely unsupported throws, so a test cannot pass by accident.
function makeDom(localStorage) {
    const listeners = [];
    const elements = new Map();
    const mkEl = (id) => {
        const el = {
            id,
            style: {},
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            dataset: {},
            textContent: '',
            innerHTML: '',
            value: '',
            disabled: false,
            scrollTop: 0,
            scrollHeight: 0,
            clientHeight: 0,
            addEventListener() {},
            removeEventListener() {},
            setAttribute() {},
            getAttribute() { return null; },
            appendChild() {},
            insertBefore() {},
            removeChild() {},
            querySelector() { return null; },
            querySelectorAll() { return []; },
            focus() {},
            blur() {},
            click() {},
            scrollIntoView() {},
            getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
        };
        return el;
    };
    const document = {
        documentElement: { setAttribute() {}, getAttribute() { return null; }, style: {}, classList: mkEl('').classList },
        head: mkEl('head'),
        body: mkEl('body'),
        title: '',
        addEventListener(type, fn) { listeners.push({ type, fn }); },
        removeEventListener() {},
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, mkEl(id));
            return elements.get(id);
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        createElement(tag) { return mkEl(tag); },
        createTextNode(t) { return { textContent: t }; },
        _listeners: listeners,
    };
    return document;
}

function makeWindow(localStorage, document, indexedDB) {
    const win = {
        localStorage,
        sessionStorage: makeStorage(),
        indexedDB,
        document,
        innerWidth: 1280,
        innerHeight: 900,
        devicePixelRatio: 2,
        location: { href: 'http://localhost/chat', origin: 'http://localhost', pathname: '/chat', search: '', hash: '' },
        navigator: {
            userAgent: 'node-test-harness',
            language: 'en',
            onLine: true,
            clipboard: { writeText() { return Promise.resolve(); } },
            storage: {
                persisted() { return Promise.resolve(false); },
                estimate() { return Promise.resolve({ quota: 10 * 1024 * 1024 * 1024, usage: 0 }); },
            },
        },
        crypto: globalThis.crypto,
        matchMedia(query) {
            return { matches: false, media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} };
        },
        addEventListener() {},
        removeEventListener() {},
        fetch() { return Promise.reject(new Error('network is stubbed out: this harness has no server')); },
        alert() {},
        confirm() { return false; },
        prompt() { return null; },
        open() { return null; },
        scrollTo() {},
        requestAnimationFrame(fn) { return setTimeout(fn, 0); },
        cancelAnimationFrame(id) { clearTimeout(id); },
        setTimeout: (...a) => setTimeout(...a),
        clearTimeout: (...a) => clearTimeout(...a),
        setInterval: (...a) => setInterval(...a),
        clearInterval: (...a) => clearInterval(...a),
        URL: globalThis.URL,
        Blob: globalThis.Blob,
        FileReader: globalThis.FileReader || class { readAsDataURL() { this.onload?.(); } },
        btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
        atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
        TextEncoder: globalThis.TextEncoder,
        TextDecoder: globalThis.TextDecoder,
        Uint8Array: globalThis.Uint8Array,
        ArrayBuffer: globalThis.ArrayBuffer,
        // i18n: returns the key, which is what the app's own fallback logic expects
        // when a translation is absent. Tests that assert on copy must stub this.
        t: (key) => key,
        _alerts: [],
    };
    win.window = win;
    win.self = win;
    win.top = win;
    win.parent = win;
    win.globalThis = win;
    return win;
}

// Minimal stand-ins for the vendored libraries the template touches at load time.
function makeLibStubs() {
    const marked = function (src) { return `<p>${String(src ?? '')}</p>`; };
    marked.use = () => {};
    marked.parse = (src) => marked(src);
    marked.setOptions = () => {};
    const hljs = {
        getLanguage: () => null,
        highlight: (o) => ({ value: String(o?.code ?? '') }),
        highlightAll: () => {},
        registerLanguage: () => {},
    };
    const DOMPurify = { sanitize: (s) => String(s ?? ''), addHook: () => {} };
    const markedHighlight = { markedHighlight: () => ({}) };
    return { marked, markedHighlight, hljs, DOMPurify, katex: { render: () => {}, renderToString: () => '' } };
}

// Boots the real template app object against fresh fake storage.
//
// `options.indexedDB` lets a test pass a specific fake (or null to force the
// localStorage fallback). `options.legacy` seeds the legacy whole-history blob so a
// migration can be exercised end to end.
function createChatApp(options = {}) {
    const localStorage = options.localStorage || makeStorage();
    const fakeIdb = options.indexedDB === null ? null : (options.indexedDB || new FakeIndexedDB());
    const document = makeDom(localStorage);
    const window = makeWindow(localStorage, document, fakeIdb);

    const context = vm.createContext(window);
    const libs = makeLibStubs();
    for (const [k, v] of Object.entries(libs)) context[k] = v;
    // The template reads these bare names as well as through window.
    context.localStorage = localStorage;
    context.indexedDB = fakeIdb;
    context.sessionStorage = window.sessionStorage;
    context.document = document;
    context.navigator = window.navigator;
    context.location = window.location;
    context.matchMedia = window.matchMedia;
    context.crypto = globalThis.crypto;
    context.t = window.t;
    context.alert = (m) => { window._alerts.push(m); };
    context.confirm = () => false;
    context.btoa = window.btoa;
    context.atob = window.atob;
    context.fetch = window.fetch;
    context.requestAnimationFrame = window.requestAnimationFrame;
    context.cancelAnimationFrame = window.cancelAnimationFrame;
    context.URL.createObjectURL = () => 'blob:stub';
    context.URL.revokeObjectURL = () => {};
    context.console = console;

    // Load the storage modules the same way the template does: classic scripts
    // publishing onto the global object.
    for (const file of STORAGE_MODULES) {
        const src = fs.readFileSync(path.join(STATIC, file), 'utf8');
        vm.runInContext(src, context, { filename: file });
    }

    const appBlock = extractAppBlock();
    vm.runInContext(appBlock, context, { filename: 'chat.html:app' });

    const chatApp = vm.runInContext('typeof chatApp === "function" ? chatApp : null', context);
    if (!chatApp) throw new Error('chatApp() is not defined after evaluating the template block');

    const app = chatApp();

    // Alpine supplies these in the browser. Without them a completed call keeps a
    // microtask alive that throws after the test has returned, which node reports as
    // an unhandledRejection attributed to whichever test happened to finish last —
    // a confusing failure that has nothing to do with the assertion being made.
    app.$nextTick = (fn) => {
        try { if (typeof fn === 'function') fn(); } catch (e) { /* mirror Alpine */ }
        return Promise.resolve();
    };
    app.$refs = {};
    app.$el = document.body;

    // Loaders that require the oMLX server. Stubbed to no-ops rather than left to
    // fail, so a test cannot accidentally depend on network data that does not exist
    // here, and so the console stays readable enough to spot a real problem.
    for (const name of ['loadAdminModelDefaults', 'loadModelCapabilities', 'fetchAdminModelsList',
        'ensureSessionModelSettings', 'refreshQuotaEstimate', 'loadProviderQuota']) {
        if (typeof app[name] === 'function') app[name] = async () => {};
    }

    if (options.legacy !== undefined) {
        localStorage.setItem('omlx_chat_history', JSON.stringify(options.legacy));
    }

    return {
        app,
        window,
        document,
        localStorage,
        sessionStorage: window.sessionStorage,
        indexedDB: fakeIdb,
        context,
        alerts: window._alerts,
    };
}

module.exports = {
    createChatApp,
    makeStorage,
    extractAppBlock,
    TEMPLATE,
    STATIC,
};
