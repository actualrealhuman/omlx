// SPDX-License-Identifier: Apache-2.0
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {ActivityDocument, submittedSections} = require('../omlx_web/static/js/activity_document.js');

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
