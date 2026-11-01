// SPDX-License-Identifier: Apache-2.0
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const js = path.join(__dirname, '../omlx_web/static/js');
const backendCursorFixture = '00000000000000000000000000000001:00000000000000000000000000000002:1';
const syntheticCursor = sequence => `${'a'.repeat(32)}:${'b'.repeat(32)}:${sequence}`;

function harness() {
    const streams = [], workers = [], downloads = [], listeners = new Map(), intervals = new Map(), timeouts = new Map();
    let nextInterval = 0, nextTimeout = 0, nextEventSequence = 0, now = 10000, monotonic = 10000;
    class TestDate extends Date { static now() { return now; } }
    const window = {t: key => key, location: new URL('http://test.invalid/admin/dashboard?tab=activity&request=first'),
        history: {pushState(_state, _title, url) { window.location = new URL(url); }, replaceState(_state, _title, url) { window.location = new URL(url); }},
        addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener(name) { listeners.delete(name); },
        dispatchEvent(event) { listeners.get(event.type)?.(event); },
    };
    const context = {window, localStorage: {getItem: () => null}, URL, URLSearchParams, AbortController, Blob, performance: {now: () => monotonic},
        Event, Date: TestDate, console,
        setTimeout(callback, delay) {const id = ++nextTimeout; timeouts.set(id, {callback, delay}); return id;},
        clearTimeout(id) {timeouts.delete(id);},
        setInterval(callback, delay) {const id = ++nextInterval; intervals.set(id, {callback, delay}); return id;},
        clearInterval(id) {intervals.delete(id);},
        requestAnimationFrame: callback => { callback(); return 1; }, cancelAnimationFrame() {},
        ResizeObserver: class { observe() {} disconnect() {} },
        EventSource: class { constructor(url) {this.url = url; this.events = {}; streams.push(this);} addEventListener(name, callback) {this.events[name] = name === 'snapshot' ? event => callback({...event, lastEventId: Object.hasOwn(event, 'lastEventId') ? event.lastEventId : syntheticCursor(++nextEventSequence)}) : callback;} close() {this.closed = true;} },
        Worker: class { constructor() {this.messages = []; workers.push(this);} postMessage(message) {this.messages.push(message);} terminate() {this.terminated = true;} },
        document: {hidden: false, addEventListener() {}, removeEventListener() {}, createElement: tag => tag === 'canvas'
            ? {getContext: () => ({measureText: () => ({width: 8})})}
            : {click() { this.clicked = true; downloads.push(this); }}},
        getComputedStyle: () => ({font: '12px monospace'}),
        fetch: async url => ({ok: true, json: async () => url.endsWith('/status') ? {health: {enabled: true}} : {models: [], sources: []}}),
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(js, 'activity.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(js, 'dashboard.js'), 'utf8'), context);
    const app = window.activityInspector();
    Object.assign(app, {mainTab: 'activity', $watch() {}, $nextTick: callback => callback(),
        $refs: {contextViewport: {clientWidth: 600, clientHeight: 440, scrollTop: 0}}, $el: {dataset: {workerUrl: '/worker.js'}}});
    return {app, context, window, streams, workers, downloads, intervals, timeouts, runTimeout(id) {const timer = timeouts.get(id); assert.ok(timer); timeouts.delete(id); timer.callback();},
        setNow(value) {now = value; monotonic = value;},
        setWallNow(value) {now = value;}, setMonotonic(value) {monotonic = value;}};
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

test('metadata cursors compare uint64 sequences exactly and ignore duplicate snapshots', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    const stream = streams.at(-1), snapshot = detail => JSON.stringify({active: [], recent: [], health: {}, detail});
    const cursor = sequence => syntheticCursor(sequence);
    stream.events.snapshot({data: snapshot({id: 'first', version: 'before-max', representations: []}), lastEventId: cursor('18446744073709551614')});
    const worker = workers.at(-1), firstCount = worker.messages.length;
    stream.events.snapshot({data: snapshot({id: 'first', version: 'max', representations: []}), lastEventId: cursor('18446744073709551615')});
    assert.equal(worker.messages.length, firstCount + 1);
    assert.equal(app.detail.version, 'max');
    const countAtMax = worker.messages.length;
    stream.events.snapshot({data: snapshot({id: 'first', version: 'duplicate', representations: []}), lastEventId: cursor('18446744073709551615')});
    stream.events.snapshot({data: snapshot({id: 'first', version: 'lower', representations: []}), lastEventId: cursor('18446744073709551614')});
    assert.equal(worker.messages.length, countAtMax);
    assert.equal(app.detail.version, 'max');
    assert.equal(app.connection, 'live');
    assert.equal(app.filtersLoading, false);
    app.activate(false);
});

test('accepts the literal cursor format emitted by the replay frame encoder', async () => {
    const {app, streams} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: 'wire', representations: []}}), lastEventId: backendCursorFixture});
    assert.equal(app.detail.version, 'wire');
    app.activate(false);
});

test('explicit reconnect resumes the channel cursor and duplicate bootstrap preserves the selected view', async () => {
    const {app, streams, workers, timeouts, runTimeout} = harness();
    await app.activate(true);
    const original = streams.at(-1), id = syntheticCursor(17);
    const snapshot = detail => JSON.stringify({active: [], recent: [], health: {}, selected_outside_filters: {id: 'keep'}, detail});
    original.events.snapshot({data: snapshot({id: 'first', version: 'one', representations: []}), lastEventId: id});
    app.pageCursor = 'older-page';
    original.onerror();
    original.events.snapshot({data: snapshot({id: 'first', version: 'native-duplicate', representations: []}), lastEventId: id});
    assert.equal(app.detail.version, 'one');
    assert.equal(app.connection, 'live');
    original.events.reconnect({});
    const timer = [...timeouts].find(([, value]) => value.delay === 3000)[0];
    runTimeout(timer);
    const resumed = streams.at(-1);
    assert.equal(new URL(resumed.url, 'http://test.invalid').searchParams.get('cursor'), id);
    resumed.events.snapshot({data: snapshot({id: 'first', version: 'duplicate', representations: []}), lastEventId: id});
    assert.equal(app.detail.version, 'one');
    assert.equal(app.selectedOutsideFilters.id, 'keep');
    assert.equal(workers.at(-1).messages.filter(message => message.type === 'sync').length, 1);
    assert.equal(app.pageCursor, 'older-page');
    app.activate(false);
});

test('malformed event IDs and JSON do not advance the applied cursor', async () => {
    const invalidIds = ['x'.repeat(64) + '1', `${'a'.repeat(32)}${'b'.repeat(32)}1`, `${'A'.repeat(32)}:${'b'.repeat(32)}:1`, `${'a'.repeat(32)}:${'b'.repeat(32)}:01`, `${'a'.repeat(32)}:${'b'.repeat(32)}:18446744073709551616`, `${'a'.repeat(32)}:${'b'.repeat(32)}:1` + 'x'.repeat(65)];
    invalidIds.push(syntheticCursor(1) + '\n', syntheticCursor(1) + '\r', syntheticCursor(1) + 'é');
    for (const invalid of invalidIds) {
        const {app, streams, workers} = harness();
        await app.activate(true);
        streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: 'invalid', representations: []}}), lastEventId: invalid});
        assert.equal(app.detail, null);
        assert.equal(workers.length, 0);
        app.activate(false);
    }
    const {app, streams} = harness();
    await app.activate(true);
    const stream = streams.at(-1), id = syntheticCursor(9);
    const snapshot = detail => JSON.stringify({active: [], recent: [], health: {}, detail});
    stream.events.snapshot({data: '{', lastEventId: id});
    assert.equal(app.detail, null);
    stream.events.snapshot({data: snapshot({id: 'first', version: 'valid', representations: []}), lastEventId: id});
    assert.equal(app.detail.version, 'valid');
    app.activate(false);
});

test('resync-required accepts a fresh baseline on the current stream', async () => {
    const {app, streams} = harness();
    await app.activate(true);
    const current = streams.at(-1), oldId = syntheticCursor(1);
    const snapshot = version => JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version, representations: []}});
    current.events.snapshot({data: snapshot('old'), lastEventId: oldId});
    current.events.resync_required({});
    assert.equal(streams.length, 1);
    current.events.snapshot({data: snapshot('new-epoch'), lastEventId: `${'c'.repeat(32)}:${'d'.repeat(32)}:1`});
    assert.equal(app.detail.version, 'new-epoch');
    app.activate(false);
});

test('malformed and foreign cursors get one bounded fresh-head retry', async () => {
    const {app, streams, timeouts, runTimeout} = harness();
    await app.activate(true);
    const first = streams.at(-1), payload = JSON.stringify({active: [], recent: [], health: {}, detail: null});
    first.events.snapshot({data: payload, lastEventId: `${'a'.repeat(32)}:${'b'.repeat(32)}:1`});
    first.events.snapshot({data: payload, lastEventId: `${'c'.repeat(32)}:${'d'.repeat(32)}:1`});
    assert.equal(first.closed, true);
    const timer = [...timeouts].find(([, value]) => value.delay === 3000)[0];
    runTimeout(timer);
    const retry = streams.at(-1);
    assert.notEqual(retry, first);
    assert.equal(new URL(retry.url, 'http://test.invalid').searchParams.has('cursor'), false);
    retry.events.snapshot({data: payload, lastEventId: 'malformed'});
    assert.equal(retry.closed, true);
    assert.equal([...timeouts.values()].some(value => value.delay === 3000), false);
    app.connect();
    const manual = streams.at(-1);
    assert.notEqual(manual, retry);
    manual.events.snapshot({data: payload, lastEventId: `${'3'.repeat(32)}:${'4'.repeat(32)}:1`});
    assert.equal(app.connection, 'live');
    app.activate(false);
});

test('ordinary selection, filter, control, hidden and auth transitions reset metadata resume state', async () => {
    const {app, streams, timeouts} = harness();
    await app.activate(true);
    const payload = detail => JSON.stringify({active: [], recent: [], health: {}, detail});
    streams.at(-1).events.snapshot({data: payload({id: 'first', version: 'one', representations: []}), lastEventId: syntheticCursor(1)});

    app.filtersChanged();
    let current = streams.at(-1);
    assert.equal(new URL(current.url, 'http://test.invalid').searchParams.has('cursor'), false);
    current.events.snapshot({data: payload({id: 'first', version: 'two', representations: []}), lastEventId: syntheticCursor(2)});
    const beforeSelect = streams.at(-1);
    app.select('second');
    current = streams.at(-1);
    assert.equal(new URL(current.url, 'http://test.invalid').searchParams.has('cursor'), false);
    beforeSelect.events.resync_required({});
    assert.equal(streams.at(-1), current, 'old resync event cannot replace the current selection stream');
    current.events.snapshot({data: payload({id: 'second', version: 'three', representations: []}), lastEventId: syntheticCursor(3)});
    app.mainTab = 'settings';
    current.events.snapshot({data: payload({id: 'second', version: 'hidden-race', representations: []}), lastEventId: syntheticCursor(4)});
    assert.equal(app.detail.version, 'three');
    app.mainTab = 'activity';

    app.write = async () => ({health: {}, config_version: 'next'});
    app.settingsDraft = {enabled: true, retain_images: true, max_requests: 20, max_age_days: 2, storageGiB: 1};
    await app.saveSettings();
    current = streams.at(-1);
    assert.equal(new URL(current.url, 'http://test.invalid').searchParams.has('cursor'), false);
    app.confirmRemoval('clear');
    await app.removeConfirmed();
    current = streams.at(-1);
    assert.equal(new URL(current.url, 'http://test.invalid').searchParams.has('cursor'), false);

    current.events.authorization_lost({});
    assert.equal([...timeouts.values()].some(value => value.delay === 3000), false);
    assert.equal([...streams].length, 5);
    await app.activate(false);
    await app.activate(true);
    current = streams.at(-1);
    assert.equal(new URL(current.url, 'http://test.invalid').searchParams.has('cursor'), false);
    app.activate(false);
});

test('selected active detail uses one visible receipt clock and freezes terminal duration', async () => {
    const {app, context, streams, intervals, setNow, setWallNow, setMonotonic} = harness();
    await app.activate(true);
    const snapshot = detail => JSON.stringify({active: [], recent: [], health: {enabled: true}, detail});
    streams.at(-1).events.snapshot({data: snapshot({id: 'first', received_at: 0, finished_at: null, representations: []})});
    assert.equal(intervals.size, 1);
    assert.equal(app.duration(app.detail), '10.0 s');
    const oldTick = [...intervals.values()][0].callback;
    setNow(12100); oldTick();
    assert.equal(app.duration(app.detail), '12.1 s');
    setWallNow(-900000); setMonotonic(13100); oldTick();
    assert.equal(app.duration(app.detail), '13.1 s', 'live elapsed ticks ignore wall-clock corrections');
    setNow(13100);
    context.document.hidden = true;
    app.syncObservationClock();
    assert.equal(intervals.size, 0);
    oldTick();
    assert.equal(app.observationNow, null);
    context.document.hidden = false;
    streams.at(-1).events.snapshot({data: snapshot({id: 'first', received_at: 0, finished_at: null, representations: []})});
    assert.equal(intervals.size, 1);

    app.select('second');
    assert.equal(intervals.size, 0);
    oldTick();
    assert.equal(app.observationNow, null);
    streams.at(-1).events.snapshot({data: snapshot({id: 'second', received_at: 5, finished_at: null, representations: []})});
    assert.equal(intervals.size, 1);
    const currentTick = [...intervals.values()][0].callback;
    setNow(15000); currentTick();
    assert.equal(app.duration(app.detail), '10.0 s');

    app.activate(false);
    assert.equal(intervals.size, 0);
    currentTick();
    assert.equal(app.observationNow, null);
    await app.activate(true);
    streams.at(-1).events.snapshot({data: snapshot({id: 'second', received_at: 5, finished_at: 8.25, representations: []})});
    assert.equal(intervals.size, 0);
    assert.equal(app.duration(app.detail), '3.3 s');
    await app.activate(false);
});

test('invalid and unobserved model times stay unknown and auth loss stops the clock', async () => {
    const {app, streams, intervals, setNow} = harness();
    await app.activate(true);
    const snapshot = detail => JSON.stringify({active: [], recent: [], health: {enabled: true}, detail});
    streams.at(-1).events.snapshot({data: snapshot({id: 'first', received_at: -1, finished_at: null, representations: []})});
    assert.equal(intervals.size, 0);
    assert.equal(app.duration(app.detail), '—');
    assert.equal(app.duration({received_at: NaN, finished_at: 1}), '—');
    assert.equal(app.duration({received_at: '0', finished_at: 1}), '—');
    assert.equal(app.duration({received_at: 2, finished_at: 1}), '0.0 s');
    assert.notEqual(app.time(0), '—');

    streams.at(-1).events.snapshot({data: snapshot({id: 'first', received_at: 1, finished_at: null, requested_model_id: '<default>', model_id: '<default>', model_resolution_observed: false, representations: []})});
    assert.equal(intervals.size, 1);
    assert.equal(app.modelTitle(app.detail), 'activity.default_model');
    assert.equal(app.modelRequestTitle(app.detail), 'activity.default_model');
    assert.equal(app.duration({id: 'other', received_at: 0, finished_at: null}), '—');
    const staleTick = [...intervals.values()][0].callback;
    setNow(14000);
    streams.at(-1).events.authorization_lost();
    assert.equal(intervals.size, 0);
    assert.equal(app.observationNow, null);
    staleTick();
    assert.equal(app.observationNow, null);
    await app.activate(false);
    await app.activate(true);
    streams.at(-1).events.snapshot({data: snapshot({id: 'first', received_at: 1, finished_at: null, requested_model_id: 'alias', model_id: 'canonical', model_resolution_observed: true, representations: []})});
    assert.equal(intervals.size, 1);
    assert.equal(app.modelTitle(app.detail), 'canonical');
    await app.activate(false);
});

test('scheduler pause labels stay scoped to the selected invocation while siblings generate', () => {
    const {app, window} = harness();
    const translations = require('../omlx_web/i18n/en.json');
    window.t = key => translations[key] || key;
    app.detail = {invocations: [
        {id: '1', state: 'prefilling', currently_paused: true, pause_count: 2, last_pause_reason: 'prefill_eviction'},
        {id: '2', state: 'generating', currently_paused: false, pause_count: 0},
    ]};
    app.invocationId = '1';
    assert.ok(app.invocationPauseSummary().includes('2 pauses'));
    assert.ok(app.invocationLabel('1').includes(translations['activity.invocation.paused']));
    assert.ok(!app.invocationLabel('2').includes(translations['activity.invocation.paused']));
    app.invocationId = '2';
    assert.equal(app.invocationPauseSummary(), translations['activity.invocation.no_pauses']);
    assert.equal(app.detail.invocations[0].currently_paused, true);
    assert.equal(app.detail.invocations[1].state, 'generating');
});

test('observation-ended and terminal pause summaries do not claim a current engine pause', () => {
    const {app, window} = harness();
    const translations = require('../omlx_web/i18n/en.json');
    window.t = key => translations[key] || key;
    app.invocationId = '1';
    app.invocationDetail = {id: '1', state: 'prefilling', currently_paused: true};
    app.detail = {invocations: [{id: '1', state: 'observation_ended', currently_paused: null,
        last_observed_paused: true, pause_count: 1, last_pause_reason: 'batch_reset'}]};
    let parts = app.invocationPauseSummary().split(' · ');
    assert.ok(parts.includes(translations['activity.invocation.last_observed_paused']));
    assert.ok(!parts.includes(translations['activity.invocation.paused']));
    app.detail.invocations[0] = {id: '1', state: 'completed', currently_paused: true,
        pause_count: 1, last_pause_reason: '<untrusted-reason>'};
    parts = app.invocationPauseSummary().split(' · ');
    assert.ok(!parts.includes(translations['activity.invocation.paused']));
    assert.ok(!parts.join(' ').includes('<untrusted-reason>'));
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

test('filters reconnect the complete query, clear old population and retain selected detail', async () => {
    const {app, streams} = harness();
    await app.activate(true);
    app.active = [{id: 'old-active'}]; app.recent = [{id: 'old-recent'}];
    app.activityActiveCount = 4; app.selected = 'first';
    app.scopeFilter = 'recent'; app.modelFilter = 'model-a'; app.sourceFilter = 'source-a';
    app.stateFilter = 'completed'; app.receivedFrom = '2026-10-31T10:00'; app.receivedUntil = '2026-10-31T11:00';
    app.filtersChanged();
    const stream = streams.at(-1), query = new URL(stream.url, 'http://test.invalid').searchParams;
    assert.equal(query.get('request_id'), 'first');
    assert.equal(query.get('scope'), 'recent');
    assert.equal(query.get('model_id'), 'model-a');
    assert.equal(query.get('source_id'), 'source-a');
    assert.equal(query.get('state'), 'completed');
    assert.ok(Number.isFinite(Number(query.get('since'))));
    assert.ok(Number.isFinite(Number(query.get('until'))));
    assert.equal(app.selected, 'first');
    assert.equal(app.active.length + app.recent.length, 0);
    assert.equal(app.activityActiveCount, null);
    assert.equal(app.filtersLoading, true);
    assert.deepEqual(Array.from(app.visibleGroups(), group => group.scope), ['recent']);
    stream.events.snapshot({data: JSON.stringify({active: [], recent: [], active_more: false, recent_more: false,
        detail: {id: 'first', version: '1', representations: []}, selected_outside_filters: true, health: {}})});
    assert.equal(app.selectedOutsideFilters, true);
    assert.equal(app.filtersLoading, false);
    assert.equal(app.activityActiveCount, null);
    const opened = streams.length;
    app.receivedFrom = '2026-10-31T12:00'; app.receivedUntil = '2026-10-31T11:00';
    app.filtersChanged();
    assert.equal(streams.length, opened);
    assert.equal(app.selected, 'first');
    assert.ok(app.filterError);
    assert.equal(stream.closed, true);
    app.select('second');
    assert.equal(streams.length, opened);
    assert.equal(app.selected, 'second');
    await app.activate(false);
});

test('selection reconnect preserves same-filter browse pages and cursor', async () => {
    const {app, streams} = harness();
    await app.activate(true);
    app.active = [{id: 'active'}]; app.recent = [{id: 'recent'}];
    app.older = [{id: 'older'}]; app.pageCursor = 'next-cursor';
    app.select('another');
    assert.equal(app.active[0].id, 'active');
    assert.equal(app.recent[0].id, 'recent');
    assert.equal(app.older[0].id, 'older');
    assert.equal(app.pageCursor, 'next-cursor');
    assert.ok(new URL(streams.at(-1).url, 'http://test.invalid').searchParams.has('request_id'));
    await app.activate(false);
});

test('page reads carry every filter and A-B-A stale responses cannot repopulate or clear newer paging', async () => {
    const {app, context, streams} = harness();
    await app.activate(true);
    const pending = [];
    context.fetch = (url, options) => new Promise(resolve => pending.push({url, options, resolve}));
    app.modelFilter = 'A'; app.stateFilter = 'error'; app.scopeFilter = 'all';
    app.receivedFrom = '2026-10-31T10:00'; app.receivedUntil = '2026-10-31T12:00';
    app.pageCursor = 'cursor-a';
    const first = app.more('recent');
    const q1 = new URL(pending[0].url, 'http://test.invalid').searchParams;
    assert.deepEqual([...['scope', 'state', 'model_id'].map(key => q1.get(key))], ['recent', 'error', 'A']);
    assert.equal(q1.get('cursor'), 'cursor-a'); assert.ok(q1.has('since') && q1.has('until'));
    app.modelFilter = 'B'; app.filtersChanged();
    app.pageCursor = 'cursor-b';
    const second = app.more('recent');
    pending[0].resolve({ok: true, json: async () => ({requests: [{id: 'stale-a'}], next_cursor: 'stale-next'})});
    await first;
    assert.equal(app.paging, true);
    app.modelFilter = 'A'; app.filtersChanged();
    pending[1].resolve({ok: true, json: async () => ({requests: [{id: 'stale-b'}], next_cursor: 'stale-next'})});
    await second;
    assert.equal(app.older.length, 0);
    assert.equal(app.pageCursor, '');
    assert.equal(app.paging, false);
    assert.equal(app.active.length + app.recent.length, 0);
    assert.ok(streams.length >= 3);
    await app.activate(false);
});

test('page controls do not read after auth loss, hiding or a stale group click', async () => {
    const {app, streams, context} = harness();
    await app.activate(true);
    const reads = [];
    context.fetch = async url => { reads.push(url); return {ok: true, json: async () => ({requests: []})}; };
    app.scopeFilter = 'active';
    await app.more('recent');
    assert.equal(reads.length, 0);
    streams.at(-1).events.authorization_lost();
    await app.more('active');
    assert.equal(reads.length, 0);
    await app.activate(false);
    await app.more('active');
    assert.equal(reads.length, 0);
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

test('outline paging is a worker command and invocation replacement clears prior outline position', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: 'first', version: '1', representations: []}})});
    app.outlineOffset = 128; app.outlineTotal = 4100; app.outlinePageSize = 128;
    app.pageOutline(1);
    assert.equal(workers.at(-1).messages.at(-1).type, 'outline-page');
    assert.equal(workers.at(-1).messages.at(-1).offset, 256);
    app.detail = {invocations: [{id: '1', state: 'completed'}], representations: []};
    app.chooseInvocation('1');
    assert.equal(app.outlineOffset, 0);
    assert.equal(app.outlineTotal, 0);
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
    assert.equal(app.authorizationLost, true);
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
    app.chooseInvocation('1');
    const exportContext = app.beginExport();
    const pending = app.saveExport({type: 'export', action: 'copy', requestToken: exportContext.requestToken,
        blob: {text: () => new Promise(done => {resolve = done;})}});
    app.chooseInvocation('2');
    resolve('old generation'); await pending;
    assert.deepEqual(copied, []);
    assert.equal(app.exporting, false);
    await app.activate(false);
});

test('worker copies one complete stored section and leaves full export unchanged', async () => {
    const content = JSON.stringify({messages: [
        {role: 'user', content: 'first message'},
        {role: 'assistant', content: 'second message outside the mounted window'},
    ]});
    const output = [];
    const reader = {ActivityDocumentTools: require('../omlx_web/static/js/activity_document.js'),
        ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'), importScripts() {},
        TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => output.push(value),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams;
            const bytes = Buffer.from(content).subarray(Number(params.get('offset')), Number(params.get('offset')) + Number(params.get('limit')));
            return {ok: true, arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)};
        },
    };
    vm.createContext(reader);
    vm.runInContext(fs.readFileSync(path.join(js, 'activity_worker.js'), 'utf8'), reader);
    reader.self.onmessage({data: {type: 'mode', mode: 'submitted'}});
    reader.self.onmessage({data: {type: 'viewport', row: 0, count: 2, columns: 100, follow: false}});
    reader.self.onmessage({data: {type: 'sync', detail: {id: 'first', endpoint: 'chat', representations: [
        {representation: 'submitted', invocation_id: '0', bytes: Buffer.byteLength(content)},
    ]}, labels: {user: 'user', assistant: 'assistant'}}});
    for (let attempt = 0; attempt < 100 && !output.some(item => item.type === 'render' && !item.loading && item.total > 0); attempt++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(output.findLast(item => item.type === 'render').mode, 'submitted');
    reader.self.onmessage({data: {type: 'viewport', row: 0, count: 100, columns: 100, follow: false}});
    const secondHeader = output.at(-1).rows.find(row => row.header && row.text.startsWith('assistant'));
    const secondKey = secondHeader.sectionKey;
    assert.ok(secondKey);
    reader.self.onmessage({data: {type: 'viewport', row: 0, count: 2, columns: 100, follow: false}});
    assert.ok(!output.findLast(item => item.type === 'render').rows.some(row => row.sectionKey === secondKey));
    reader.self.onmessage({data: {type: 'export-section', sectionKey: secondKey, requestToken: 7}});
    const single = await output.at(-1).blob.text();
    assert.ok(single.includes('assistant · 2'));
    assert.ok(single.includes('second message outside the mounted window'));
    assert.ok(!single.includes('first message'));
    reader.self.onmessage({data: {type: 'export-section', sectionKey: 'stale-section', requestToken: 8}});
    assert.equal(output.at(-1).type, 'export_error');
    assert.equal(output.at(-1).requestToken, 8);
    assert.equal(output.at(-1).reason, 'section_unavailable');
    reader.self.onmessage({data: {type: 'export', action: 'copy', requestToken: 9}});
    const full = await output.at(-1).blob.text();
    assert.ok(full.includes('first message') && full.includes('second message outside the mounted window'));
});

test('stale rendered section keys cannot export after a view change', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {
        id: 'first', version: 'v1', representations: [],
    }})});
    const worker = workers.at(-1);
    worker.onmessage({data: {type: 'render', rows: [{key: 'heading', sectionKey: 'submitted:0:message-0', header: true, text: 'user'}],
        outline: [], total: 1, row: 0, loading: false, limited: false, search: {count: 0, marks: []}}});
    const old = app.rows[0].copyContext;
    assert.equal(old.view, 'submitted');
    app.mode = 'model'; app.changeMode();
    const before = worker.messages.length;
    app.exportSection('submitted:0:message-0', old);
    assert.equal(worker.messages.length, before);
    assert.equal(app.exportError, 'activity.selection_changed');
    await app.activate(false);
});

test('delayed old-mode renders cannot replace the current view rows', async () => {
    const {app, streams, workers} = harness();
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {
        id: 'first', version: 'v1', representations: [],
    }})});
    const worker = workers.at(-1);
    worker.onmessage({data: {type: 'render', mode: 'model', rows: [{key: 'old', text: 'old mode'}], outline: [], total: 1, row: 0, loading: false}});
    assert.equal(app.rows.length, 0);
    worker.onmessage({data: {type: 'render', mode: 'submitted', rows: [{key: 'current', text: 'current mode'}], outline: [], total: 1, row: 0, loading: false}});
    assert.equal(app.rows[0].key, 'current');
    await app.activate(false);
});

test('request ID/link copying is canonical and deferred fallback is fenced after selection change', async () => {
    const {app, streams, context, window, downloads} = harness();
    const requestId = 'req-full-id-0123456789';
    window.location = new URL(`http://test.invalid/admin/dashboard?tab=activity&request=${requestId}&other=keep#fragment`);
    await app.activate(true);
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {id: requestId, version: 'v1', representations: []}})});
    const copied = [];
    context.navigator = {clipboard: {writeText: async text => copied.push(text)}};
    await app.copyRequestInfo('id');
    await app.copyRequestInfo('link');
    assert.deepEqual(copied, [requestId, `http://test.invalid/admin/dashboard?tab=activity&request=${requestId}`]);
    assert.equal(app.exportContext, null);

    context.navigator.clipboard.writeText = async () => { throw new Error('clipboard denied'); };
    await app.copyRequestInfo('id');
    assert.equal(downloads.at(-1).download, `activity-request-id-${requestId}.txt`);
    assert.equal(app.exporting, false);

    let rejectClipboard;
    context.navigator.clipboard.writeText = () => new Promise((_resolve, reject) => { rejectClipboard = reject; });
    const pending = app.copyRequestInfo('id');
    app.select('other');
    rejectClipboard(new Error('permission denied'));
    await pending;
    assert.equal(app.exporting, false);
    assert.equal(app.exportError, '');
    await app.activate(false);
});

test('pending clipboard rejection cannot fall back after hide, auth loss, deletion, view or invocation change', async () => {
    const changes = [
        async app => app.select('other'),
        async app => { app.mode = 'model'; app.changeMode(); },
        async app => app.chooseInvocation('1'),
        async (app, streams) => streams.at(-1).events.authorization_lost(),
        async (app, streams) => streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: null})}),
        async app => app.activate(false),
    ];
    for (const change of changes) {
        const {app, streams, context, downloads} = harness();
        await app.activate(true);
        streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {
            id: 'first', version: 'v1', representations: [], invocations: [{id: '1'}],
        }})});
        let rejectClipboard, writes = 0;
        context.navigator = {clipboard: {writeText: () => { writes++; return new Promise((_resolve, reject) => { rejectClipboard = reject; }); }}};
        const pending = app.copyRequestInfo('id');
        await change(app, streams);
        rejectClipboard(new Error('clipboard denied'));
        await pending;
        assert.equal(writes, 1);
        assert.equal(downloads.length, 0);
        assert.equal(app.exporting, false);
        await app.activate(false);
    }
});

test('auth loss blocks new copy actions until authenticated activation and metadata updates do not cancel copies', async () => {
    const {app, streams, workers, context, downloads} = harness();
    await app.activate(true);
    const first = {id: 'first', version: 'v1', representations: []};
    streams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: first})});
    const worker = workers.at(-1), messageCount = worker.messages.length;
    streams.at(-1).events.authorization_lost();
    context.navigator = {clipboard: {writeText: async () => { throw new Error('unexpected clipboard call'); }}};
    app.exportView('copy');
    await app.copyRequestInfo('id');
    assert.equal(app.authorizationLost, true);
    assert.equal(worker.messages.length, messageCount);
    assert.equal(downloads.length, 0);

    await app.activate(false);
    await app.activate(true);
    assert.equal(app.authorizationLost, false);
    await app.activate(false);

    const {app: live, streams: liveStreams, context: liveContext, downloads: liveDownloads} = harness();
    await live.activate(true);
    liveStreams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: first})});
    liveContext.URL.createObjectURL = () => 'blob:test';
    let finishText, copied = [];
    liveContext.navigator = {clipboard: {writeText: async text => { copied.push(text); throw new Error('clipboard denied'); }}};
    const contextBefore = live.beginExport();
    const pending = live.saveExport({type: 'export', action: 'copy', requestToken: contextBefore.requestToken,
        blob: {text: () => new Promise(resolve => { finishText = resolve; })}});
    liveStreams.at(-1).events.snapshot({data: JSON.stringify({active: [], recent: [], health: {}, detail: {...first, version: 'v2'}})});
    finishText('stable retained view');
    await pending;
    assert.deepEqual(copied, ['stable retained view']);
    assert.equal(liveDownloads.length, 1);
    assert.equal(live.exporting, false);
    await live.activate(false);
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
