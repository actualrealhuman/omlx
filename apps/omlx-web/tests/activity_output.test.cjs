// SPDX-License-Identifier: Apache-2.0
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {ApiOutput} = require('../omlx_web/static/js/activity_output.js');
const event = data => `data: ${JSON.stringify(data)}\r\n\r\n`;
function parse(endpoint, wire, stride = 13, terminal = true) {
    const view = new ApiOutput('/v1/' + endpoint);
    for (let i = 0; i < wire.length; i += stride) view.feed(wire.slice(i, i + stride));
    view.finish(terminal); return view;
}

test('Chat JSON preserves separate reasoning, answer, choices and tool arguments inertly', () => {
    const view = parse('chat/completions', ' \n' + JSON.stringify({choices: [
        {index: 0, message: {reasoning_content: 'consider 世界🙂', content: 'answer', tool_calls: [{id: 'call-1', function: {name: 'lookup', arguments: '{"value":"<script>text</script>"}'}}]}},
        {index: 1, message: {content: 'alternative'}},
    ]}));
    assert.equal(view.status(), 'complete');
    assert.deepEqual(view.sections().map(s => s.role), ['reasoning', 'assistant', 'tool_calls', 'assistant']);
    assert.match(view.sections()[2].text, /lookup\ncall-1\n\{"value":"<script>text<\/script>"\}/);
    assert.equal(view.sections()[3].text, 'alternative');
});

test('Chat SSE joins fragmented tool arguments and keeps tools and choices independent', () => {
    const wire = ': keepalive\r\n\r\n' + [
        {choices: [{index: 0, delta: {role: 'assistant'}}]},
        {choices: [{index: 0, delta: {reasoning_content: 'think '}}]},
        {choices: [{index: 0, delta: {content: 'answer'}}]},
        {choices: [{index: 0, delta: {tool_calls: [{index: 0, id: 'a', function: {name: 'lo', arguments: '{"x":'}}, {index: 1, id: 'b', function: {name: 'other', arguments: '{}'}}]}}]},
        {choices: [{index: 1, delta: {content: 'second'}}]},
        {choices: [{index: 0, delta: {tool_calls: [{index: 0, function: {name: 'okup', arguments: '1}'}}]}}]},
        {choices: [], usage: {completion_tokens: 1}},
    ].map(event).join('') + 'data: [DONE]\r\n\r\n';
    const view = parse('chat/completions', wire, 1);
    assert.equal(view.status(), 'complete');
    const tools = view.sections().filter(s => s.role === 'tool_calls');
    assert.equal(tools[0].text, 'lookup\na\n{"x":1}');
    assert.equal(tools[1].text, 'other\nb\n{}');
    assert.equal(view.sections().filter(s => s.role === 'assistant').map(s => s.text).join('|'), 'answer|second');
});

test('Completions uses actual choice text and never splits model thinking markers', () => {
    for (const wire of [JSON.stringify({choices: [{index: 0, text: '<think>literal</think>answer'}]}), event({choices: [{index: 0, text: '<think>literal</think>answer'}]}) + 'data: [DONE]\n\n']) {
        const view = parse('completions', wire);
        assert.equal(view.status(), 'complete');
        assert.equal(view.sections()[0].role, 'assistant');
        assert.equal(view.sections()[0].text, '<think>literal</think>answer');
    }
});

test('Messages JSON and SSE preserve block order and replace initial empty tool input', () => {
    const complete = {type: 'message', content: [{type: 'thinking', thinking: 'reason'}, {type: 'text', text: 'answer'}, {type: 'tool_use', name: 'lookup', id: 'c', input: {x: 1}}]};
    const json = parse('messages', JSON.stringify(complete));
    assert.deepEqual(json.sections().map(s => s.role), ['reasoning', 'assistant', 'tool_calls']);
    const frames = [
        {type: 'message_start', message: {content: []}},
        {type: 'content_block_start', index: 0, content_block: {type: 'thinking', thinking: ''}},
        {type: 'content_block_delta', index: 0, delta: {type: 'thinking_delta', thinking: 'reason'}},
        {type: 'content_block_delta', index: 0, delta: {type: 'signature_delta', signature: 'evidence-only'}},
        {type: 'content_block_stop', index: 0},
        {type: 'content_block_start', index: 1, content_block: {type: 'text', text: ''}},
        {type: 'content_block_delta', index: 1, delta: {type: 'text_delta', text: 'answer'}},
        {type: 'content_block_start', index: 2, content_block: {type: 'tool_use', name: 'lookup', id: 'c', input: {}}},
        {type: 'content_block_delta', index: 2, delta: {type: 'input_json_delta', partial_json: '{"x":'}},
        {type: 'content_block_delta', index: 2, delta: {type: 'input_json_delta', partial_json: '1}'}},
        {type: 'message_stop'},
    ];
    const stream = parse('messages', frames.map(event).join(''));
    assert.equal(stream.status(), 'complete');
    assert.equal(stream.sections()[2].text, 'lookup\nc\n{"x":1}');
    assert.equal(stream.sections()[0].text, json.sections()[0].text);
});

test('Responses done items and terminal snapshots replace deltas without duplicated answers or tools', () => {
    const output = [{type: 'reasoning', summary: [{type: 'summary_text', text: 'reason'}]},
        {type: 'message', content: [{type: 'output_text', text: 'clean answer'}]},
        {type: 'function_call', name: 'lookup', call_id: 'c', namespace: 'tools', arguments: '{"x":1}'}];
    const frames = [
        {type: 'response.created'},
        {type: 'response.reasoning_summary_text.delta', output_index: 0, summary_index: 0, delta: 'reason'},
        {type: 'response.output_item.done', output_index: 0, item: output[0]},
        {type: 'response.output_text.delta', output_index: 1, content_index: 0, delta: 'unclean answer'},
        {type: 'response.output_text.done', output_index: 1, content_index: 0, text: 'clean answer'},
        {type: 'response.content_part.done', output_index: 1, content_index: 0, part: output[1].content[0]},
        {type: 'response.output_item.done', output_index: 1, item: output[1]},
        {type: 'response.output_item.added', output_index: 2, item: {...output[2], arguments: ''}},
        {type: 'response.function_call_arguments.delta', output_index: 2, delta: '{"x":1}'},
        {type: 'response.function_call_arguments.done', output_index: 2, arguments: '{"x":1}'},
        {type: 'response.output_item.done', output_index: 2, item: output[2]},
        {type: 'response.completed', response: {output}},
    ].map((data, i) => ({...data, sequence_number: i}));
    const stream = parse('responses', frames.map(event).join(''), 3);
    assert.equal(stream.status(), 'complete');
    assert.deepEqual(stream.sections().map(s => s.text), ['reason', 'clean answer', 'tools\nlookup\nc\n{"x":1}']);
    assert.deepEqual(stream.sections().map(s => s.text), parse('responses', JSON.stringify({output})).sections().map(s => s.text));
});

test('incomplete/failed Responses retain honest terminal status and partial output', () => {
    const output = [{type: 'message', content: [{type: 'output_text', text: 'partial'}]}];
    const incomplete = parse('responses', event({type: 'response.incomplete', response: {status: 'incomplete', output}}));
    assert.equal(incomplete.status(), 'complete'); // protocol terminal, not successful inference
    const failed = parse('responses', event({type: 'response.output_text.delta', output_index: 0, content_index: 0, delta: 'partial'}) +
        event({type: 'response.failed', response: {output: [], error: {message: 'synthetic failure'}}}));
    assert.equal(failed.status(), 'failed');
    assert.equal(failed.sections()[0].text, 'partial');
    assert.match(failed.sections()[1].text, /synthetic failure/);
});

test('interruption, missing block start, malformed and unknown envelopes remain visibly partial', () => {
    const partial = parse('chat/completions', event({choices: [{index: 0, delta: {content: 'kept'}}]}));
    assert.equal(partial.status(), 'partial');
    assert.equal(partial.sections()[0].text, 'kept');
    assert.equal(parse('messages', event({type: 'content_block_delta', index: 0, delta: {type: 'text_delta', text: 'unknown'}})).status(), 'partial');
    assert.equal(parse('chat/completions', 'data: {invalid}\n\ndata: [DONE]\n\n').status(), 'partial');
    assert.equal(parse('messages', JSON.stringify({content: [{type: 'future_block'}]})).status(), 'partial');
    assert.equal(parse('chat/completions', '{"choices":[').status(), 'partial');
    assert.equal(parse('chat/completions', 'data: {"choices":[]}').status(), 'partial');
    assert.equal(parse('unknown', '{}').status(), 'unavailable');
    const named = parse('chat/completions', JSON.stringify({choices: [{index: 0, message: {tool_calls: [{function: {name: 'x'.repeat(300), arguments: '{}'}}]}}]}));
    assert.equal(named.status(), 'partial');
    assert.equal(named.sections()[0].name.length, 256);
});

test('duplicate/reordered/gapped Responses sequence cannot silently duplicate content', () => {
    const delta = {type: 'response.output_text.delta', output_index: 0, content_index: 0, delta: 'once', sequence_number: 1};
    const view = parse('responses', event(delta) + event(delta) + event({...delta, sequence_number: 3, delta: ' twice'}));
    assert.equal(view.status(), 'partial');
    assert.equal(view.sections()[0].text, 'once twice');
    assert.ok(view.notes.has('duplicate_or_reordered_event'));
    assert.ok(view.notes.has('event_sequence_gap'));
});

test('frame, structure and output budgets stop projection while retaining explicit status', () => {
    assert.equal(parse('chat/completions', event({choices: [{index: 0, delta: {content: 'x'.repeat(1024 * 1024)}}]}), 65536).status(), 'limited');
    const deep = '{"choices":' + '['.repeat(65) + '0' + ']'.repeat(65) + '}';
    assert.equal(parse('chat/completions', deep).status(), 'limited');
    const nodes = JSON.stringify({choices: Array.from({length: 9000}, (_, index) => ({index, text: 'x'}))});
    assert.equal(parse('completions', nodes, 65536).status(), 'limited');
    const blocks = JSON.stringify({content: Array.from({length: 4097}, () => ({type: 'text', text: ''}))});
    assert.equal(parse('messages', blocks, 65536).status(), 'limited');
    const view = new ApiOutput('/v1/chat/completions');
    for (let i = 0; i < 18 && !view.halted; i++) view.feed(event({choices: [{index: 0, delta: {content: 'x'.repeat(1000000)}}]}));
    assert.equal(view.status(), 'limited');
    assert.ok(view.textSize <= 16 * 1024 * 1024);
});
