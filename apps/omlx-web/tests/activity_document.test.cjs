// SPDX-License-Identifier: Apache-2.0
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {ActivityDocument, submittedSections} = require('../omlx_web/static/js/activity_document.js');

function activityWorker(fetch) {
    const outputs = [];
    const context = {ActivityDocumentTools: {ActivityDocument, submittedSections},
        ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'),
        importScripts() {}, TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => outputs.push(value), fetch};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../omlx_web/static/js/activity_worker.js'), 'utf8'), context);
    return {onmessage: data => context.self.onmessage({data}), outputs};
}

async function waitForSettledWorker(worker, from = worker.outputs.length) {
    for (let attempt = 0; attempt < 100 && !worker.outputs.slice(from).some(value => value.type === 'render' && !value.loading); attempt++) {
        await new Promise(resolve => setImmediate(resolve));
    }
    assert.ok(worker.outputs.slice(from).some(value => value.type === 'render' && !value.loading), 'worker must settle after the requested operation');
    return worker.outputs.filter(value => value.type === 'render').at(-1);
}

function activityDetail(buffer, id = 'synthetic') {
    return {id, finished_at: 1, representations: [{representation: 'submitted', invocation_id: '0', bytes: buffer.length}]};
}

test('huge single messages and lines remain navigable with a bounded viewport', () => {
    const body = 'abc '.repeat(500000) + 'unique-target';
    const doc = new ActivityDocument();
    doc.setSections([{key: 'large', role: 'tool', text: body}], 80);
    assert.ok(doc.rows > 20000);
    assert.ok(doc.sections[0].checkpoints.length < doc.rows / 100);
    const found = doc.search('unique-target');
    assert.equal(found.count, 1);
    const row = doc.rowFor(found.target.key, found.target.offset);
    assert.ok(row > 20000);
    const visible = doc.window(row - 1, 1000);
    assert.ok(visible.length <= 200);
    assert.match(visible.map(line => line.text).join(''), /unique-target/);
});

test('append and width changes preserve a semantic character anchor', () => {
    const doc = new ActivityDocument();
    const input = {key: 'input', role: 'user', text: 'message '.repeat(1000)};
    const output = {key: 'output', role: 'raw_output', text: 'answer '.repeat(1000)};
    doc.setSections([input, output], 60);
    const row = doc.rowFor('output', 1200), anchor = doc.anchor(row);
    doc.setSections([{...input, text: input.text + 'more input '.repeat(1000)}, {...output, text: output.text + 'more output'}], 60);
    const next = doc.rowFor(anchor.key, anchor.offset);
    assert.equal(doc.anchor(next).offset, anchor.offset);
    assert.ok(next > row);
    doc.setSections(doc.sections, 35);
    const resized = doc.anchor(doc.rowFor(anchor.key, anchor.offset));
    assert.equal(resized.key, anchor.key);
    assert.ok(Math.abs(resized.offset - anchor.offset) < 35);
});

test('Unicode, tabs, newlines and appending at sparse boundaries keep valid rows', () => {
    const doc = new ActivityDocument();
    const text = ('世界🙂\tZ\n').repeat(1000);
    doc.setSections([{key: 'u', role: 'user', text}], 24);
    const before = doc.rows;
    doc.setSections([{key: 'u', role: 'user', text: text + 'end🙂'}], 24);
    assert.equal(doc.rows, before + 1);
    for (const line of doc.window(doc.rows - 100, 100)) {
        assert.ok(!/^[\uDC00-\uDFFF]/.test(line.text));
        assert.ok(!/[\uD800-\uDBFF]$/.test(line.text));
    }
    assert.equal(doc.search('end🙂').count, 1);
    doc.setSections([{key: 'marks', role: 'user', text: '\u0301'.repeat(100000)}], 100);
    assert.ok(doc.rows > 200);
    assert.ok(doc.window(0, 100).every(line => line.text.length <= 400));
});

test('search covers invisible content and split inputs, with bounded match metadata', () => {
    const doc = new ActivityDocument();
    doc.setSections([{key: 'one', role: 'user', text: 'a'.repeat(100000) + 'split-ne'},
        {key: 'two', role: 'assistant', text: 'x '.repeat(2000)}]);
    doc.setSections([{key: 'one', role: 'user', text: 'a'.repeat(100000) + 'split-needle'},
        {key: 'two', role: 'assistant', text: 'x '.repeat(2000)}]);
    assert.equal(doc.search('split-needle').count, 1);
    const many = doc.search('x', 1999);
    assert.equal(many.count, 2000);
    assert.equal(many.marks.length, 500);
    assert.equal(many.markersTruncated, true);
    assert.ok(many.target.offset > 3000);
});

test('semantic messages preserve roles/tools and keep media inert', () => {
    const sections = submittedSections(JSON.stringify({model: 'example', system: 'instructions', messages: [
        {role: 'user', content: [{type: 'text', text: '<script>inert</script>'}, {type: 'image_url', image_url: {url: 'data:image/png;base64,AAAA'}}]},
        {role: 'assistant', content: '', tool_calls: [{id: 'call-1', function: {name: 'example'}}]},
        {role: 'tool', content: 'result'},
    ], tools: [{name: 'example'}]}));
    assert.deepEqual(sections.map(s => s.role), ['system', 'user', 'tool_calls', 'tool', 'tools', 'options']);
    assert.match(sections[1].text, /<script>inert<\/script>/);
    assert.ok(!sections.some(s => s.text.includes('AAAA')));
    assert.equal(submittedSections('{partial')[0].role, 'unparsed');
});

test('actual worker incrementally decodes split UTF-8 and searches retained content', async () => {
    const outputs = [];
    const submitted = JSON.stringify({messages: [{role: 'user', content: '世界🙂needle'}]});
    const buffers = {submitted: Buffer.from(submitted), raw_output: Buffer.from('answer 世界🙂')};
    const context = {ActivityDocumentTools: {ActivityDocument, submittedSections},
        importScripts() {}, TextDecoder, URLSearchParams, Blob, self: {},
        ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'),
        postMessage: data => outputs.push(data),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams;
            const bytes = buffers[params.get('representation')];
            const offset = Number(params.get('offset'));
            return {ok: true, arrayBuffer: async () => Uint8Array.from(bytes.subarray(offset, offset + 3)).buffer};
        },
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../omlx_web/static/js/activity_worker.js'), 'utf8'), context);
    context.self.onmessage({data: {type: 'sync', detail: {id: 'synthetic', representations: Object.entries(buffers).map(([representation, data]) => ({representation, bytes: data.length, invocation_id: representation === 'submitted' ? '0' : '1'}))}}});
    for (let attempt = 0; attempt < 100 && !outputs.some(o => o.type === 'render' && !o.loading); attempt++) await new Promise(resolve => setImmediate(resolve));
    const last = outputs.filter(o => o.type === 'render').at(-1);
    assert.equal(last.loading, false);
    assert.ok(!last.rows.some(row => row.text.includes('\uFFFD')));
    assert.match(last.rows.map(row => row.text).join(''), /世界🙂needle/);
    context.self.onmessage({data: {type: 'search', query: 'needle', match: -1}});
    assert.equal(outputs.at(-1).search.count, 1);
    assert.equal(outputs.at(-1).search.match, 0);
    context.self.onmessage({data: {type: 'mode', mode: 'submitted_json'}});
    const jsonRows = outputs.at(-1).rows.map(row => row.text).join('');
    assert.match(jsonRows, /"messages"/);
    assert.ok(!jsonRows.includes('answer 世界'));
});

test('actual worker reveals a far-tail submitted match entered before content arrives', async () => {
    let release;
    const delayed = new Promise(resolve => { release = resolve; });
    const body = Buffer.from(JSON.stringify({messages: [{role: 'user', content: 'ordinary\n'.repeat(90) + 'late-target'}]}));
    const worker = activityWorker(async () => delayed);
    worker.onmessage({type: 'viewport', row: 0, count: 20, columns: 40, follow: false});
    worker.onmessage({type: 'sync', detail: activityDetail(body)});
    worker.onmessage({type: 'search', query: 'late-target', match: -1});
    assert.equal(worker.outputs.at(-1).loading, true);
    assert.equal(worker.outputs.at(-1).search.count, 0);
    release({ok: true, arrayBuffer: async () => body});
    const view = await waitForSettledWorker(worker);
    assert.equal(view.search.count, 1);
    assert.equal(view.search.match, 0, 'normalize requested ordinal only against the completed index');
    assert.ok(view.row > 80);
    assert.equal(view.row, view.search.marks[0]);
    assert.ok(view.rows.some(line => line.text.includes('late-target')));
});

test('actual worker resolves a loading search after the complete request index settles', async () => {
    let release;
    const delayed = new Promise(resolve => { release = resolve; });
    const outputs = [];
    const submitted = Buffer.from(JSON.stringify({messages: [{role: 'user', content: 'needle'}]}));
    const raw = Buffer.from('needle from output');
    const context = {ActivityDocumentTools: {ActivityDocument, submittedSections},
        ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'),
        importScripts() {}, TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => outputs.push(value),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams, offset = Number(params.get('offset'));
            const bytes = params.get('representation') === 'submitted' ? submitted : raw;
            if (params.get('representation') === 'submitted') return delayed;
            return {ok: true, arrayBuffer: async () => bytes.subarray(offset)};
        }};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../omlx_web/static/js/activity_worker.js'), 'utf8'), context);
    context.self.onmessage({data: {type: 'viewport', row: 0, count: 20, columns: 40, follow: false}});
    context.self.onmessage({data: {type: 'sync', detail: {id: 'delayed', representations: [
        {representation: 'raw_output', invocation_id: '1', bytes: raw.length},
        {representation: 'submitted', invocation_id: '0', bytes: submitted.length},
    ]}}});
    await new Promise(resolve => setImmediate(resolve));
    context.self.onmessage({data: {type: 'search', query: 'needle', match: 0}});
    // Layout feedback that reports the same row must leave the intent pending.
    context.self.onmessage({data: {type: 'viewport', row: 0, count: 18, columns: 24, follow: false}});
    release({ok: true, arrayBuffer: async () => submitted});
    let view = await waitForSettledWorker({outputs});
    assert.equal(view.search.count, 2, JSON.stringify({keys: view.outline.map(section => section.key), current: view.current, search: view.search, rows: view.rows.slice(0, 3)}));
    assert.equal(view.search.match, 0);
    assert.match(view.current.key, /^submitted:/, 'match zero follows final input-before-output order');
    assert.equal(view.row, view.search.marks[0]);

    // A completed intent is one-shot: later manual navigation survives live updates.
    const manualRow = view.outline.find(section => section.role === 'raw_output').row + 1;
    assert.notEqual(manualRow, view.search.marks[0]);
    context.self.onmessage({data: {type: 'jump', row: manualRow}});
    const appendedRaw = Buffer.from('needle from output needle later');
    context.fetch = async url => {
        const params = new URL(url, 'http://test.invalid').searchParams;
        const bytes = params.get('representation') === 'submitted' ? submitted : appendedRaw;
        return {ok: true, arrayBuffer: async () => bytes.subarray(Number(params.get('offset')))};
    };
    const beforeUpdate = outputs.length;
    context.self.onmessage({data: {type: 'sync', detail: {id: 'delayed', representations: [
        {representation: 'raw_output', invocation_id: '1', bytes: appendedRaw.length},
        {representation: 'submitted', invocation_id: '0', bytes: submitted.length},
    ]}}});
    view = await waitForSettledWorker({outputs}, beforeUpdate);
    assert.equal(view.row, manualRow, 'manual navigation survives later live updates');
});

test('actual worker cancels loading search intent on clear, mode change, navigation, and Follow', async () => {
    const cases = [
        ['query clear', worker => worker.onmessage({type: 'search', query: '', match: 0})],
        ['mode change', worker => worker.onmessage({type: 'mode', mode: 'model'})],
        ['manual navigation', worker => worker.onmessage({type: 'jump', row: 5})],
        ['scroll movement', worker => worker.onmessage({type: 'viewport', row: 5, count: 20, columns: 40, follow: false})],
        ['explicit Follow', worker => worker.onmessage({type: 'viewport', row: 0, count: 20, columns: 40, follow: true, revealOutline: true})],
    ];
    for (const [name, cancel] of cases) {
        let release;
        const delayed = new Promise(resolve => { release = resolve; });
        const body = Buffer.from(JSON.stringify({messages: [{role: 'user', content: 'late-target'},
            {role: 'assistant', content: 'trailing '.repeat(30)}]}));
        const worker = activityWorker(async () => delayed);
        worker.onmessage({type: 'viewport', row: 0, count: 20, columns: 40, follow: false});
        worker.onmessage({type: 'sync', detail: activityDetail(body, name)});
        await new Promise(resolve => setImmediate(resolve));
        worker.onmessage({type: 'search', query: 'late-target', match: 0});
        cancel(worker);
        release({ok: true, arrayBuffer: async () => body});
        const view = await waitForSettledWorker(worker);
        assert.ok(view, `${name}: worker settled`);
        assert.notEqual(view.row, view.search.marks[0], `${name}: canceled query must not move to its match`);
    }
});

test('actual worker clears a no-match loading intent when the sync pass ends', async () => {
    let release;
    const delayed = new Promise(resolve => { release = resolve; });
    let current = Buffer.from('ordinary\n'.repeat(30));
    const worker = activityWorker(async url => {
        const params = new URL(url, 'http://test.invalid').searchParams;
        if (Number(params.get('offset')) === 0) return delayed;
        return {ok: true, arrayBuffer: async () => current.subarray(Number(params.get('offset')))};
    });
    worker.onmessage({type: 'viewport', row: 0, count: 20, columns: 40, follow: false});
    const detail = (bytes, id = 'synthetic') => ({id, finished_at: 1, representations: [{representation: 'raw_output', invocation_id: '1', bytes}]});
    worker.onmessage({type: 'sync', detail: detail(current.length)});
    await new Promise(resolve => setImmediate(resolve));
    worker.onmessage({type: 'search', query: 'not-present', match: 0});
    release({ok: true, arrayBuffer: async () => current});
    let view = await waitForSettledWorker(worker);
    assert.equal(view.search.count, 0);
    assert.equal(view.row, 1);

    current = Buffer.concat([current, Buffer.from(' not-present')]);
    const beforeUpdate = worker.outputs.length;
    worker.onmessage({type: 'sync', detail: detail(current.length)});
    view = await waitForSettledWorker(worker, beforeUpdate);
    assert.equal(view.search.count, 1);
    assert.equal(view.row, 1, 'a loading query completed without a match must not jump on a later sync');
    assert.notEqual(view.row, view.search.marks[0]);
});

test('actual worker drops pending search intent when content loading fails', async () => {
    let reject;
    const delayed = new Promise((_, fail) => { reject = fail; });
    const raw = Buffer.from('needle');
    const worker = activityWorker(async url => {
        const params = new URL(url, 'http://test.invalid').searchParams;
        if (params.get('representation') === 'submitted') return delayed;
        return {ok: true, arrayBuffer: async () => raw};
    });
    worker.onmessage({type: 'viewport', row: 0, count: 20, columns: 40, follow: false});
    worker.onmessage({type: 'sync', detail: {id: 'failed', representations: [
        {representation: 'raw_output', invocation_id: '1', bytes: raw.length},
        {representation: 'submitted', invocation_id: '0', bytes: 10},
    ]}});
    await new Promise(resolve => setImmediate(resolve));
    worker.onmessage({type: 'search', query: 'needle', match: 0});
    reject(new Error('synthetic content failure'));
    for (let attempt = 0; attempt < 100 && !worker.outputs.some(value => value.type === 'error'); attempt++) await new Promise(resolve => setImmediate(resolve));
    worker.onmessage({type: 'viewport', row: 0, count: 20, columns: 40, follow: false});
    const view = worker.outputs.filter(value => value.type === 'render').at(-1);
    assert.equal(view.search.count, 1);
    assert.notEqual(view.row, view.search.marks[0], 'a later render must not apply the failed load’s pending search');
});

test('actual worker keeps a searched section anchor across width revisions', async () => {
    const outputs = [], submitted = Buffer.from(JSON.stringify({messages: [{role: 'assistant', content: 'word '.repeat(1200) + 'ACTIVITY_STRESS_TURN_END'}]}));
    const context = {ActivityDocumentTools: {ActivityDocument, submittedSections}, ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'),
        importScripts() {}, TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => outputs.push(value),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams, offset = Number(params.get('offset'));
            return {ok: true, arrayBuffer: async () => submitted.subarray(offset, offset + 65536)};
        }};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../omlx_web/static/js/activity_worker.js'), 'utf8'), context);
    context.self.onmessage({data: {type: 'viewport', row: 0, count: 45, columns: 80, follow: false, layoutRevision: 0}});
    context.self.onmessage({data: {type: 'sync', detail: {id: 'synthetic', finished_at: 1, representations: [
        {representation: 'submitted', invocation_id: '0', bytes: submitted.length},
    ]}}});
    for (let attempt = 0; attempt < 100 && !outputs.some(value => value.type === 'render' && !value.loading && value.outlineTotal); attempt++) await new Promise(resolve => setImmediate(resolve));
    context.self.onmessage({data: {type: 'search', query: 'ACTIVITY_STRESS_TURN_END', match: 0}});
    const before = outputs.at(-1);
    assert.equal(before.search.count, 1, JSON.stringify({loading: before.loading, current: before.current, rows: before.rows.slice(0, 2)}));
    const key = before.current.key;
    assert.equal(key, 'submitted:0:message-0');
    context.self.onmessage({data: {type: 'viewport', row: before.row, count: 45, columns: 24, follow: false, layoutRevision: 1}});
    const after = outputs.at(-1);
    assert.equal(after.layoutRevision, 1);
    assert.equal(after.search.count, 1);
    assert.equal(after.current.key, key);
    assert.equal(after.row, after.search.marks[0]);
    assert.notEqual(after.row, before.row);
    context.self.onmessage({data: {type: 'viewport', row: 0, count: 55, columns: 24, follow: true, layoutRevision: 1}});
    const tail = outputs.at(-1);
    assert.equal(tail.row, tail.total - 35);
    context.self.onmessage({data: {type: 'viewport', row: tail.row, count: 60, columns: 24, follow: true, layoutRevision: 1}});
    assert.equal(outputs.at(-1).row, tail.total - 40, 'height-only resize keeps follow at the actual tail');
    context.self.onmessage({data: {type: 'viewport', row: 10, count: 60, columns: 24, follow: false, layoutRevision: 1}});
    assert.equal(outputs.at(-1).row, 10, 'manual scrolling still selects the requested row');
});

test('worker pages the complete outline and keeps aggregate rail and stable-key navigation bounded', async () => {
    const outputs = [], totalMessages = 4105;
    let submitted = JSON.stringify({messages: Array.from({length: totalMessages}, (_, i) => ({role: i % 2 ? 'assistant' : 'user',
        content: i === 0 ? `large ${'x'.repeat(2 * 1024 * 1024)}` : `message-${i}`}))});
    let output = 'tail output';
    const buffers = () => ({submitted: Buffer.from(submitted), raw_output: Buffer.from(output)});
    const context = {ActivityDocumentTools: {ActivityDocument, submittedSections}, ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'),
        importScripts() {}, TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => outputs.push(value),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams, data = buffers()[params.get('representation')];
            const offset = Number(params.get('offset'));
            return {ok: true, arrayBuffer: async () => Uint8Array.from(data.subarray(offset, offset + 65536)).buffer};
        }};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../omlx_web/static/js/activity_worker.js'), 'utf8'), context);
    context.self.onmessage({data: {type: 'viewport', row: 0, count: 45, columns: 80, follow: false}});
    const sync = () => context.self.onmessage({data: {type: 'sync', detail: {id: 'synthetic', endpoint: '/v1/chat/completions',
        representations: Object.entries(buffers()).map(([representation, data]) => ({representation, bytes: data.length, invocation_id: representation === 'submitted' ? '0' : '1'}))}}});
    const waitForRender = async () => {
        for (let attempt = 0; attempt < 100 && !outputs.some(value => value.type === 'render' && !value.loading && value.outlineTotal > 0); attempt++) await new Promise(resolve => setImmediate(resolve));
        return outputs.filter(value => value.type === 'render').at(-1);
    };
    sync();
    let view = await waitForRender();
    assert.equal(view.outlineTotal, totalMessages + 1); // The raw output section is included after submitted input.
    assert.ok(view.outline.length <= 128);
    assert.ok(view.rail.length <= 256);
    assert.ok(view.outline.every(item => !('text' in item) && !('checkpoints' in item) && item.label.length <= 256));
    assert.ok(view.rail.every(item => item.preview.length <= 128));
    assert.ok(view.rail.some(item => item.role === 'mixed'));
    assert.ok(Buffer.byteLength(JSON.stringify({outline: view.outline, rail: view.rail})) < 100000);
    assert.equal(view.rail[0].row, 0);
    assert.equal(view.rail.at(-1).row + view.rail.at(-1).rows, view.total);
    const beforeOutlineRow = view.row;
    context.self.onmessage({data: {type: 'outline-page', offset: 128}});
    view = outputs.at(-1);
    assert.equal(view.outlineOffset, 128);
    assert.equal(view.row, beforeOutlineRow, 'changing outline pages must not move the document');
    assert.equal(view.outline[0].key, 'submitted:0:message-128');
    output += ' appended while the outline page is browsed';
    outputs.length = 0;
    sync(); view = await waitForRender();
    assert.equal(view.outlineOffset, 128, 'live sync must preserve the manually browsed outline page');
    assert.equal(view.row, beforeOutlineRow, 'live sync must preserve the selected document row');
    context.self.onmessage({data: {type: 'outline-page', offset: 999999}});
    view = outputs.at(-1);
    assert.equal(view.outlineOffset, Math.floor((totalMessages - 1) / 128) * 128);
    context.self.onmessage({data: {type: 'jump', row: 0}});
    assert.equal(outputs.at(-1).outlineOffset, 0, 'rail jumps reveal the current outline page');
    context.self.onmessage({data: {type: 'viewport', row: view.total - 1, count: 45, columns: 80, follow: false}});
    assert.equal(outputs.at(-1).outlineOffset, Math.floor((totalMessages - 1) / 128) * 128,
        'ordinary scrolling reveals the current outline page without enabling follow');
    context.self.onmessage({data: {type: 'outline-page', offset: 128}});
    context.self.onmessage({data: {type: 'viewport', row: view.total - 1, count: 45, columns: 80, follow: true, revealOutline: true}});
    assert.equal(outputs.at(-1).outlineOffset, Math.floor((totalMessages - 1) / 128) * 128,
        'explicit Follow reveals the current outline page even when the viewport has not moved');
    assert.equal(view.outlineHasNext, false);
    const tailKey = `submitted:0:message-${totalMessages - 1}`;
    context.self.onmessage({data: {type: 'jump-section', key: tailKey}});
    view = outputs.at(-1);
    assert.equal(view.current.key, tailKey);
    assert.equal(view.current.ordinal, totalMessages);
    assert.equal(view.outlineOffset, Math.floor((totalMessages - 1) / 128) * 128);
    context.self.onmessage({data: {type: 'jump-section', key: 'submitted:0:message-127'}});
    context.self.onmessage({data: {type: 'navigate', direction: 1}});
    assert.equal(outputs.at(-1).current.ordinal, 129);
    assert.equal(outputs.at(-1).outlineOffset, 128);
    context.self.onmessage({data: {type: 'navigate', direction: -1}});
    assert.equal(outputs.at(-1).current.ordinal, 128);
    context.self.onmessage({data: {type: 'jump-section', key: tailKey}});
    const tailRow = view.current.row;
    context.self.onmessage({data: {type: 'navigate', direction: -1}});
    assert.equal(outputs.at(-1).current.ordinal, totalMessages - 1);
    context.self.onmessage({data: {type: 'navigate', direction: 1}});
    assert.equal(outputs.at(-1).current.ordinal, totalMessages);

    // Output append and a width reflow change the live row mapping. Stable keys still resolve the tail.
    output += ' more appended output';
    outputs.length = 0;
    sync(); view = await waitForRender();
    assert.equal(view.current.key, tailKey);
    assert.equal(view.current.row, tailRow);
    outputs.length = 0;
    context.self.onmessage({data: {type: 'viewport', row: view.row, count: 45, columns: 24, follow: false}});
    view = outputs.at(-1);
    assert.notEqual(view.current.row, tailRow);
    assert.equal(view.current.key, tailKey);
    context.self.onmessage({data: {type: 'jump-section', key: tailKey}});
    assert.equal(outputs.at(-1).row, outputs.at(-1).current.row);
    assert.equal(outputs.at(-1).current.ordinal, totalMessages);

    // Mode replacement resets the page and ordinal to the start of the new index.
    context.self.onmessage({data: {type: 'mode', mode: 'model'}});
    assert.equal(outputs.at(-1).outlineOffset, 0);
    assert.equal(outputs.at(-1).outlineTotal, 1);
    assert.equal(outputs.at(-1).current.ordinal, 1);
});

test('normalized attachment references remain visible and plain documents stay readable', () => {
    const sections = submittedSections(JSON.stringify({messages: [{role: 'user', content: [
        {type: 'image_url', image_url: {url: {_omlx_activity_attachment: {id: 'attachment-1', status: 'not_retained'}}}},
        {type: 'document', source: {type: 'text', data: 'useful document text'}},
    ]}]}), {attachment_not_retained: 'not retained'});
    assert.match(sections[0].text, /attachment-1: not retained/);
    assert.match(sections[0].text, /useful document text/);
});

test('actual worker projects long API output, keeps raw views separate and exports all selected text', async () => {
    const outputs = [], response = text => `data: ${JSON.stringify(text)}\n\n`;
    const body = '世界🙂 text '.repeat(100000) + 'far-tail-match';
    const wire = response({choices: [{index: 0, delta: {reasoning_content: 'reason'}}]}) +
        Array.from({length: Math.ceil(body.length / 200000)}, (_, i) => response({choices: [{index: 0, delta: {content: body.slice(i * 200000, (i + 1) * 200000)}}]})).join('');
    const buffers = {api_response: Buffer.from(wire), raw_output: Buffer.from('<think>producer layer</think>'),
        submitted: Buffer.from(JSON.stringify({messages: [{role: 'user', content: 'input'}]}))};
    const context = {ActivityDocumentTools: {ActivityDocument, submittedSections}, ActivityOutputTools: require('../omlx_web/static/js/activity_output.js'),
        importScripts() {}, TextDecoder, URLSearchParams, Blob, self: {}, postMessage: value => outputs.push(value),
        fetch: async url => {
            const params = new URL(url, 'http://test.invalid').searchParams, offset = Number(params.get('offset'));
            return {ok: true, arrayBuffer: async () => Uint8Array.from(buffers[params.get('representation')].subarray(offset, offset + 65537)).buffer};
        }};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../omlx_web/static/js/activity_worker.js'), 'utf8'), context);
    const detail = {id: 'synthetic', endpoint: '/v1/chat/completions', finished_at: null,
        representations: Object.entries(buffers).map(([representation, data]) => ({representation, bytes: data.length, invocation_id: '0'}))};
    context.self.onmessage({data: {type: 'sync', detail}});
    for (let attempt = 0; attempt < 100 && !outputs.some(value => value.type === 'render' && !value.loading); attempt++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(outputs.at(-1).projectionState, 'streaming');
    assert.equal(outputs.at(-1).projectionIncomplete, true);
    assert.ok(outputs.at(-1).rows.length <= 200);
    context.self.onmessage({data: {type: 'search', query: 'far-tail-match', match: 0}});
    assert.equal(outputs.at(-1).search.count, 1);
    const anchor = outputs.at(-1).current.key;
    context.self.onmessage({data: {type: 'export'}});
    assert.equal(outputs.at(-1).incomplete, true);
    buffers.api_response = Buffer.from(wire + 'data: [DONE]\n\n');
    detail.finished_at = 1;
    detail.representations.find(item => item.representation === 'api_response').bytes = buffers.api_response.length;
    outputs.length = 0;
    context.self.onmessage({data: {type: 'sync', detail}});
    for (let attempt = 0; attempt < 100 && !outputs.some(value => value.type === 'render' && !value.loading); attempt++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(outputs.at(-1).projectionState, 'complete');
    assert.equal(outputs.at(-1).projectionIncomplete, false);
    assert.equal(outputs.at(-1).current.key, anchor);
    context.self.onmessage({data: {type: 'export'}});
    let text = await outputs.at(-1).blob.text();
    assert.ok(text.includes(body));
    assert.ok(!text.includes('<think>producer layer</think>'));
    assert.ok(text.indexOf('input') < text.indexOf(body));
    assert.equal(outputs.at(-1).incomplete, false);
    context.self.onmessage({data: {type: 'mode', mode: 'parsed_output'}});
    context.self.onmessage({data: {type: 'export'}});
    text = await outputs.at(-1).blob.text();
    assert.ok(text.includes(body) && !text.includes('user\ninput'));
    context.self.onmessage({data: {type: 'mode', mode: 'model'}});
    context.self.onmessage({data: {type: 'export'}});
    assert.ok((await outputs.at(-1).blob.text()).includes('<think>producer layer</think>'));
    context.self.onmessage({data: {type: 'mode', mode: 'api_response'}});
    context.self.onmessage({data: {type: 'export'}});
    assert.ok((await outputs.at(-1).blob.text()).includes(wire));
    context.self.onmessage({data: {type: 'mode', mode: 'submitted_json'}});
    context.self.onmessage({data: {type: 'export'}});
    assert.ok(!(await outputs.at(-1).blob.text()).includes('far-tail-match'));
});
