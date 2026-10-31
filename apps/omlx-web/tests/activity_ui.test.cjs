// SPDX-License-Identifier: Apache-2.0
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const js = path.join(__dirname, '../omlx_web/static/js');

function harness() {
    const streams = [], workers = [], listeners = new Map();
    const window = {t: key => key, location: new URL('http://test.invalid/admin/dashboard?tab=activity&request=first'),
        history: {pushState(_state, _title, url) { window.location = new URL(url); }, replaceState(_state, _title, url) { window.location = new URL(url); }},
        addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener(name) { listeners.delete(name); },
        dispatchEvent(event) { listeners.get(event.type)?.(event); },
    };
    const context = {window, localStorage: {getItem: () => null}, URL, URLSearchParams, AbortController,
        Event, Date, console, setTimeout, clearTimeout, requestAnimationFrame: callback => { callback(); return 1; }, cancelAnimationFrame() {},
        ResizeObserver: class { observe() {} disconnect() {} },
        EventSource: class { constructor(url) {this.url = url; this.events = {}; streams.push(this);} addEventListener(name, callback) {this.events[name] = callback;} close() {this.closed = true;} },
        Worker: class { constructor() {this.messages = []; workers.push(this);} postMessage(message) {this.messages.push(message);} terminate() {this.terminated = true;} },
        document: {hidden: false, addEventListener() {}, removeEventListener() {}, createElement: () => ({getContext: () => ({measureText: () => ({width: 8})})})},
        getComputedStyle: () => ({font: '12px monospace'}),
        fetch: async url => ({ok: true, json: async () => url.endsWith('/status') ? {health: {enabled: true}} : {models: [], sources: []}}),
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(js, 'activity.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(js, 'dashboard.js'), 'utf8'), context);
    const app = window.activityInspector();
    Object.assign(app, {mainTab: 'activity', $watch() {}, $nextTick: callback => callback(),
        $refs: {contextViewport: {clientWidth: 600, clientHeight: 440, scrollTop: 0}}, $el: {dataset: {workerUrl: '/worker.js'}}});
    return {app, context, window, streams, workers};
}

test('inspector resynchronizes, ignores old selections and stops all work when hidden', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    assert.equal(app.selected, 'first');
    assert.ok(streams.at(-1).url.includes('request_id=first'));
    const snapshot = detail => JSON.stringify({active: [], recent: [], health: {enabled: true}, detail});
    streams.at(-1).events.snapshot({data: snapshot({id: 'first', version: '1', representations: []})});
    const oldWorker = workers.at(-1);
    assert.ok(oldWorker.messages.some(message => message.type === 'sync'));
    app.select('second');
    assert.equal(oldWorker.terminated, true);
    oldWorker.onmessage({data: {type: 'error', message: 'stale response'}});
    assert.notEqual(app.error, 'stale response');
    const current = streams.at(-1);
    current.events.snapshot({data: snapshot({id: 'second', version: '2', representations: []})});
    workers.at(-1).onmessage({data: {type: 'render', rows: [], outline: [], total: 1000, row: 200, loading: false, limited: false, search: {count: 0, marks: []}}});
    assert.equal(app.$refs.contextViewport.scrollTop, 4400);
    await app.activate(false);
    assert.equal(current.closed, true);
    assert.equal(workers.at(-1).terminated, true);
    await app.activate(true);
    assert.equal(app.selected, 'second');
    assert.equal(app.row, 200);
    await app.activate(false);
});

test('native dashboard deep links preserve modified click and browser navigation', () => {
    const {context, window} = harness();
    const dashboard = context.dashboard();
    let prevented = false;
    dashboard.openActivityRequest({metaKey: true, preventDefault() {prevented = true;}}, 'parent');
    assert.equal(prevented, false);
    dashboard.openActivityRequest({button: 0, preventDefault() {prevented = true;}}, 'parent');
    assert.equal(prevented, true);
    assert.equal(dashboard.mainTab, 'activity');
    assert.equal(window.location.searchParams.get('request'), 'parent');
    dashboard.applyTabStateFromUrl();
    assert.equal(dashboard.mainTab, 'activity');
    dashboard.setMainTab('status');
    assert.equal(window.location.searchParams.has('request'), false);
});

test('reading away from the tail disables follow and keeps a stable viewport', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: '1', representations: []}})});
    app.follow = true; app.row = 900; app.total = 1000;
    app.$refs.contextViewport.scrollTop = 2200;
    app.onScroll();
    assert.equal(app.follow, false);
    assert.equal(app.row, 100);
    assert.equal(workers.at(-1).messages.at(-1).row, 100);
    app.followOutput();
    assert.equal(app.follow, true);
    assert.equal(app.row, 1000 - app.visibleRows);
    await app.activate(false);
});

test('extremely tall documents stay within browser element-height limits', () => {
    const {app} = harness();
    app.total = 10000000; app.visibleRows = 30; app.row = 9000000;
    assert.equal(app.scrollHeight(), 4400000);
    const pixels = app.scrollPixels(app.row);
    assert.ok(pixels < 4400000);
    assert.equal(app.lineTop({row: app.row + 1}) - pixels, 22);
    app.$refs.contextViewport.scrollTop = pixels;
    app.row = 0;
    app.onScroll();
    assert.equal(app.row, 9000000);
});

test('live updates preserve mounted text identities and distinguish new output from reflow', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: '1', representations: []}})});
    const worker = workers.at(-1);
    const render = (total, outputBytes) => worker.onmessage({data: {type: 'render', rows: [{key: 'line', text: 'unchanged', row: 1}],
        outline: [], total, outputBytes, row: 0, loading: false, search: {count: 0, marks: []}}});
    render(1000, 0);
    const line = app.rows[0];
    render(1200, 0);
    assert.equal(app.rows[0], line);
    assert.equal(app.newOutput, false);
    render(1201, 20);
    assert.equal(app.rows[0], line);
    assert.equal(app.newOutput, true);
    await app.activate(false);
});

test('capture controls use versioned settings writes and pin deletion to the confirmed record', async () => {
    const {app, context} = harness();
    const writes = [];
    context.fetch = async (url, init = {}) => {
        if (init.method) {
            writes.push({url, ...init});
            return {ok: true, json: async () => ({health: {enabled: true}, config_version: 'new-version'})};
        }
        return {ok: true, json: async () => url.endsWith('/status')
            ? {health: {enabled: false}, settings: {enabled: false, retain_images: true, max_requests: 1000, max_age_days: 7, max_storage_bytes: 5 * 1024 ** 3}, config_version: 'old-version'}
            : {models: [], sources: []}};
    };
    await app.activate(true);
    await app.openSettings();
    assert.equal(app.settingsDraft.storageGiB, 5);
    app.settingsDraft.enabled = true; app.settingsDraft.max_requests = 10000;
    app.settingsDraft.retain_images = false;
    await app.saveSettings();
    assert.equal(writes[0].headers['X-Activity-Action'], 'settings');
    assert.equal(writes[0].headers['If-Match'], 'old-version');
    assert.equal(JSON.parse(writes[0].body).max_requests, 10000);
    assert.equal(JSON.parse(writes[0].body).retain_images, false);
    assert.equal(JSON.parse(writes[0].body).max_storage_bytes, 5 * 1024 ** 3);
    app.selected = 'confirmed'; app.confirmRemoval('delete'); app.selected = 'other';
    await app.removeConfirmed();
    assert.ok(writes[1].url.endsWith('/requests/confirmed'));
    assert.equal(writes[1].method, 'DELETE');
    assert.equal(JSON.parse(writes[1].body).confirm, 'delete');
    assert.equal(app.selected, 'other');
    app.confirmRemoval('clear');
    await app.activate(false);
    assert.equal(app.pendingAction, '');
});

test('image previews load one admin URL lazily and invalidate on selection, expiry and hidden tabs', async () => {
    const {app, streams} = harness();
    await app.activate(true);
    const detail = {id: 'first', version: 'images', representations: [], attachments: [
        {id: 'attachment-1', status: 'not_retained', reason: 'attachment_queue_limit'},
        {id: 'attachment-2', status: 'retained', mime_type: 'image/png'},
        {id: 'attachment-3', status: 'retained', mime_type: 'image/jpeg'},
    ]};
    const snapshot = value => ({data: JSON.stringify({active: [], recent: [], health: {}, detail: value})});
    streams.at(-1).events.snapshot(snapshot(detail));
    assert.equal(app.imageUrl('attachment-2'), null);
    app.toggleAttachments();
    assert.equal(app.previewId, 'attachment-2');
    assert.equal(app.imageUrl(app.previewId), '/admin/api/activity/requests/first/attachments/attachment-2');
    assert.equal(app.imageUrl('https://example.invalid/image'), null);
    assert.equal(app.imageUrl('attachment-1'), null);
    assert.equal(app.imageReason(detail.attachments[0]), 'activity.image_reason.attachment_queue_limit');
    app.previewId = 'attachment-3';
    app.imageFailed({target: {src: 'http://test.invalid/admin/api/activity/requests/first/attachments/attachment-2'}});
    assert.equal(app.previewFailed, false);
    app.imageFailed({target: {src: 'http://test.invalid' + app.imageUrl(app.previewId)}});
    assert.equal(app.previewFailed, true);
    app.toggleAttachments();
    assert.equal(app.previewId, '');
    app.toggleAttachments();
    streams.at(-1).events.authorization_lost();
    assert.equal(app.imageUrl('attachment-2'), null);
    app.toggleAttachments();
    streams.at(-1).events.snapshot(snapshot(null));
    assert.equal(app.attachmentsOpen, false);
    app.select('second');
    assert.equal(app.previewId, '');
    await app.activate(false);
    assert.equal(app.imageUrl('attachment-2'), null);
});

test('coverage labels keep dispatch identity separate from retained evidence and unknown states', () => {
    const {app} = harness();
    app.detail = {coverage: {backend_kinds: ['distributed_proxy', 'local_text'], evidence: {
        submitted: 'retained', prepared_text: 'not_observed', prepared_tokens: 'retained',
        producer_output: 'observed_not_retained', api_wire: 'retained', images: 'future_state',
    }}};
    assert.equal(app.backendLabel(), 'activity.backend.distributed_proxy + activity.backend.local_text');
    const rows = app.coverageRows();
    assert.equal(rows.length, 6);
    assert.equal(rows.find(item => item.key === 'prepared_text').state, 'not_observed');
    assert.equal(rows.find(item => item.key === 'producer_output').state, 'observed_not_retained');
    assert.equal(rows.find(item => item.key === 'images').state, 'not_observed');
    app.detail.coverage.backend_kinds = ['arbitrary-backend-string'];
    assert.equal(app.backendLabel(), 'activity.backend.unknown');
    app.detail = null;
    assert.equal(app.backendLabel(), 'activity.backend.unresolved');
    app.coverageOpen = true; app.select('next');
    assert.equal(app.coverageOpen, false);
});

test('failed settings writes retain the draft and do not blindly retry', async () => {
    const {app, context} = harness();
    let calls = 0;
    context.fetch = async () => { calls++; return {ok: false, status: 409, json: async () => ({detail: 'reload before saving'})}; };
    app.settingsOpen = true; app.settingsDraft.max_requests = 42;
    await app.saveSettings();
    assert.equal(calls, 1);
    assert.equal(app.settingsOpen, true);
    assert.equal(app.settingsDraft.max_requests, 42);
    assert.equal(app.error, 'reload before saving');
    assert.equal(app.controlBusy, false);
});

test('cleared or expired selections invalidate already queued worker responses', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    const stream = streams.at(-1);
    stream.events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: '1', representations: []}})});
    const old = workers.at(-1);
    stream.events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: null})});
    assert.equal(old.terminated, true);
    assert.equal(app.missing, true);
    old.onmessage({data: {type: 'error', message: 'stale worker'}});
    assert.notEqual(app.error, 'stale worker');
    assert.equal(app.rows.length, 0);
    await app.activate(false);
});

test('invocation selection isolates worker evidence and restores the entire-request position', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    const detail = {id: 'first', version: 'v1', invocations: [{id: '1', state: 'completed'}, {id: '2', state: 'generating'}], representations: [
        {invocation_id: '0', representation: 'submitted'}, {invocation_id: '0', representation: 'api_response'},
        {invocation_id: '1', representation: 'raw_output'}, {invocation_id: '2', representation: 'model_input'}, {invocation_id: '2', representation: 'raw_output'},
    ]};
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], detail, health: {}})});
    app.row = 155; app.follow = false;
    const original = workers.at(-1);
    app.chooseInvocation('2');
    assert.equal(original.terminated, true);
    assert.equal(app.mode, 'model');
    assert.equal(app.invocationId, '2');
    const sync = workers.at(-1).messages.find(message => message.type === 'sync');
    assert.deepEqual(Array.from(sync.detail.representations, item => item.invocation_id), ['2', '2']);
    assert.equal(app.invocationState(), 'activity.state.generating');
    // Feed the controller's exact filtered payload through the real reader worker.
    const workerOutputs = [], reads = [];
    const payloads = {model_input: 'prepared-two', raw_output: 'answer-two'};
    sync.detail.representations.forEach(item => { item.bytes = Buffer.byteLength(payloads[item.representation]); });
    const reader = {ActivityDocumentTools: require('../omlx_web/static/js/activity_document.js'),
        ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'), importScripts() {},
        TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => workerOutputs.push(value),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams;
            reads.push(params.get('invocation_id'));
            const data = Uint8Array.from(Buffer.from(payloads[params.get('representation')]));
            return {ok: true, arrayBuffer: async () => data.buffer};
        },
    };
    vm.createContext(reader);
    vm.runInContext(fs.readFileSync(path.join(js, 'activity_worker.js'), 'utf8'), reader);
    reader.self.onmessage({data: {type: 'mode', mode: 'model'}}); workerOutputs.length = 0;
    reader.self.onmessage({data: sync});
    for (let attempt = 0; attempt < 100 && !workerOutputs.some(item => item.type === 'render' && !item.loading); attempt++) await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(reads, ['2', '2']);
    reader.self.onmessage({data: {type: 'export'}});
    const exported = await workerOutputs.at(-1).blob.text();
    assert.ok(exported.includes('prepared-two') && exported.includes('answer-two'));
    assert.ok(!exported.includes('submitted') && !exported.includes('api_response'));
    app.chooseInvocation('1');
    assert.equal(app.invocationState(), 'activity.state.completed');
    app.chooseInvocation('');
    assert.equal(app.row, 155); assert.equal(app.mode, 'submitted'); assert.equal(app.follow, false);
    assert.equal(workers.at(-1).messages.find(message => message.type === 'sync').detail.representations.length, 5);
    app.chooseInvocation('2');
    await app.activate(false);
    await app.activate(true);
    assert.equal(app.invocationId, '2');
    assert.equal(app.invocationOpen, false);
    app.mode = 'api_response'; app.changeMode();
    assert.equal(app.invocationId, '');
    assert.equal(app.mode, 'api_response');
    await app.activate(false);
});

test('invocation options load one bounded admin read only on expansion and ignore stale selections', async () => {
    const {app, streams, context} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {
        id: 'first', version: '1', representations: [], invocations: [{id: '1'}, {id: '2'}],
    }})});
    const pending = [];
    context.fetch = (url, options) => new Promise(resolve => pending.push({url, options, resolve}));
    app.chooseInvocation('1');
    assert.equal(pending.length, 0);
    const first = app.toggleInvocationOptions();
    assert.ok(pending[0].url.endsWith('/requests/first/invocations/1'));
    app.chooseInvocation('2');
    assert.equal(pending[0].options.signal.aborted, true);
    const second = app.toggleInvocationOptions();
    pending[0].resolve({ok: true, json: async () => ({id: '1', admission_options: {seed: 111}})});
    await first;
    assert.equal(app.invocationDetail, null);
    pending[1].resolve({ok: true, json: async () => ({id: '2', admission_options: {seed: 222}})});
    await second;
    assert.equal(app.invocationDetail.id, '2');
    assert.ok(app.invocationOptions().includes('222'));
    await app.toggleInvocationOptions();
    assert.equal(app.invocationDetail, null);
    const third = app.toggleInvocationOptions();
    await app.activate(false);
    assert.equal(pending[2].options.signal.aborted, true);
    pending[2].resolve({ok: true, json: async () => ({id: '2', admission_options: {seed: 333}})});
    await third;
    assert.equal(app.invocationDetail, null);
});

test('invocation read failures retry explicitly and clear on authorization loss or removed history', async () => {
    const {app, streams, context} = harness();
    await app.activate(true);
    const snapshot = detail => ({data: JSON.stringify({active: [], recent: [], health: {}, detail})});
    streams.at(-1).events.snapshot(snapshot({id: 'first', version: '1', representations: [], invocations: [{id: '1', generated_tokens: 0}]}));
    app.chooseInvocation('1');
    context.fetch = async () => ({ok: false, status: 404});
    await app.toggleInvocationOptions();
    assert.ok(app.invocationError.includes('404'));
    context.fetch = async () => ({ok: true, json: async () => ({id: '1', admission_options: {seed: 42}})});
    await app.loadInvocation();
    assert.equal(app.invocationError, '');
    assert.equal(app.invocationSummary().generated_tokens, 0);
    streams.at(-1).events.authorization_lost();
    assert.equal(app.invocationDetail, null); assert.equal(app.invocationOpen, false);
    app.connect();
    streams.at(-1).events.snapshot(snapshot(null));
    assert.equal(app.invocationDetail, null); assert.equal(app.missing, true);
    await app.activate(false);
});

test('invocation choices include retained evidence with missing summaries and reject invalid IDs', () => {
    const {app} = harness();
    app.detail = {invocations: [{id: '2'}, {id: '<script>'}, {id: '257'}], representations: [
        {invocation_id: '0'}, {invocation_id: '1'}, {invocation_id: '2'}, {invocation_id: '01'},
    ]};
    assert.deepEqual(Array.from(app.invocationChoices()), ['1', '2']);
    assert.equal(app.invocationState('1'), 'activity.invocation.unknown');
    app.chooseInvocation('999');
    assert.equal(app.invocationId, '');
});

test('switching invocation invalidates a deferred clipboard export', async () => {
    const {app, streams, context} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: '1', representations: [], invocations: [{id: '1'}, {id: '2'}]}})});
    const copied = []; let resolve;
    context.navigator = {clipboard: {writeText: async text => copied.push(text)}};
    app.chooseInvocation('1'); app.exporting = true;
    const pending = app.saveExport({action: 'copy', blob: {text: () => new Promise(done => {resolve = done;})}});
    app.chooseInvocation('2');
    resolve('old generation'); await pending;
    assert.deepEqual(copied, []);
    assert.equal(app.exporting, false);
    await app.activate(false);
});

test('deferred scroll restoration from an older worker cannot move the new invocation viewport', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: '1', representations: [], invocations: [{id: '1'}]}})});
    const callbacks = []; app.$nextTick = callback => callbacks.push(callback);
    workers.at(-1).onmessage({data: {type: 'render', rows: [], outline: [], total: 1000, row: 200, loading: false, search: {count: 0}}});
    app.chooseInvocation('1');
    callbacks.forEach(callback => callback());
    assert.equal(app.$refs.contextViewport.scrollTop, 0);
    await app.activate(false);
});
