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
