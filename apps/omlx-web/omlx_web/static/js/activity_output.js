// SPDX-License-Identifier: Apache-2.0
// Readable projection of captured API bytes. Never parses model control syntax.
(function (root) {
    'use strict';
    const MAX_FRAME = 1024 * 1024, MAX_JSON = 16 * 1024 * 1024;
    const MAX_TEXT = 16 * 1024 * 1024, MAX_SECTIONS = 4096;
    const endpoints = {'/v1/completions': 'completion', '/v1/chat/completions': 'chat', '/v1/messages': 'messages', '/v1/responses': 'responses'};
    const index = value => Number.isInteger(value) && value >= 0 && value < 4096 ? value : null;
    const short = value => typeof value === 'string' ? value.slice(0, 256) : '';
    class ApiOutput {
        constructor(endpoint) {
            this.family = endpoints[endpoint]; this.parts = new Map(); this.blocks = new Map();
            this.pending = ''; this.format = ''; this.frame = []; this.frameSize = 0;
            this.textSize = 0; this.notes = new Set(); this.seen = false;
            this.done = false; this.failed = false; this.halted = false; this.sequence = null;
        }
        limit() { this.notes.add('projection_limit'); this.halted = true; this.pending = ''; this.frame = []; }
        parse(text) {
            let quoted = false, escaped = false, depth = 0, nodes = 0;
            for (const character of text) {
                if (quoted) {
                    if (escaped) escaped = false;
                    else if (character === '\\') escaped = true;
                    else if (character === '"') quoted = false;
                } else if (character === '"') quoted = true;
                else if ('{[,:'.includes(character)) {
                    nodes++;
                    if (character === '{' || character === '[') depth++;
                    if (nodes > 32768 || depth > 64) { this.limit(); return null; }
                } else if (character === '}' || character === ']') depth--;
            }
            return JSON.parse(text);
        }
        put(key, role, value, append = false, metadata = {}) {
            if (value == null) return;
            if (typeof value !== 'string') { this.unknown(); return; }
            let part = this.parts.get(key);
            if (!part) {
                if (this.parts.size >= MAX_SECTIONS) { this.limit(); return; }
                part = {key, role, text: '', ...metadata}; this.parts.set(key, part);
            }
            if (!append) this.textSize -= part.text.length;
            const available = Math.max(0, MAX_TEXT - this.textSize);
            let text = value.slice(0, available);
            if (text.length && /[\uD800-\uDBFF]/.test(text.at(-1)) && text.length < value.length) text = text.slice(0, -1);
            part.text = (append ? part.text : '') + text; this.textSize += text.length;
            Object.assign(part, metadata);
            if (text.length < value.length) this.limit();
        }
        tool(key, item, append = false) {
            const fn = item.function || item;
            const previous = this.parts.get(key);
            if ([fn.name, item.call_id || item.id, item.namespace].some(value => typeof value === 'string' && value.length > 256)
                || (append && typeof fn.name === 'string' && (previous?.name?.length || 0) + fn.name.length > 256)) this.notes.add('metadata_truncated');
            const metadata = {name: typeof fn.name === 'string' ? short((append ? previous?.name || '' : '') + fn.name) : previous?.name || '',
                id: short(item.call_id || item.id) || previous?.id || '', namespace: short(item.namespace) || previous?.namespace || ''};
            const value = fn.arguments ?? item.input;
            this.put(key, 'tool_calls', typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2), append, metadata);
        }
        error(value) {
            this.failed = true; this.put('error', 'output_error', typeof value === 'string' ? value : JSON.stringify(value));
        }
        unknown() { this.notes.add('unmapped_output'); }
        array(value) {
            if (value == null) return [];
            if (!Array.isArray(value)) { this.unknown(); return []; }
            if (value.length > MAX_SECTIONS) this.limit();
            return value.slice(0, MAX_SECTIONS);
        }
        openai(data, streaming) {
            if (!Array.isArray(data.choices)) { this.unknown(); return; }
            if (data.choices.length > MAX_SECTIONS) this.limit();
            for (const choice of data.choices.slice(0, MAX_SECTIONS)) {
                const n = index(choice.index);
                if (n === null) { this.unknown(); continue; }
                const prefix = `choice-${n}`;
                if (this.family === 'completion') this.put(prefix, 'assistant', choice.text, streaming);
                else {
                    const message = streaming ? choice.delta : choice.message;
                    if (!message || typeof message !== 'object') continue;
                    this.put(`${prefix}-reasoning`, 'reasoning', message.reasoning_content, streaming);
                    if (typeof message.content === 'string') this.put(`${prefix}-answer`, 'assistant', message.content, streaming);
                    else if (Array.isArray(message.content)) this.array(message.content).forEach((part, i) => {
                        if (typeof part.text === 'string') this.put(`${prefix}-answer-${i}`, 'assistant', part.text, streaming);
                        else this.unknown();
                    });
                    else if (message.content != null) this.unknown();
                    if (Array.isArray(message.tool_calls)) this.array(message.tool_calls).forEach((call, i) => {
                        const toolIndex = streaming ? index(call.index) : i;
                        if (toolIndex === null) this.unknown();
                        else this.tool(`${prefix}-tool-${toolIndex}`, call, streaming);
                    });
                    if (message.function_call) this.tool(`${prefix}-legacy-tool`, message.function_call, streaming);
                    if (message.refusal) this.put(`${prefix}-refusal`, 'output_refusal', message.refusal, streaming);
                }
            }
        }
        anthropicBlock(n, contentBlock) {
            if (n === null || !contentBlock || typeof contentBlock !== 'object') { this.unknown(); return; }
            this.blocks.set(n, contentBlock.type);
            const key = `block-${n}`;
            if (contentBlock.type === 'text') this.put(key, 'assistant', contentBlock.text);
            else if (contentBlock.type === 'thinking') this.put(key, 'reasoning', contentBlock.thinking);
            else if (contentBlock.type === 'tool_use') this.tool(key, contentBlock);
            else this.unknown();
        }
        anthropic(data, streaming) {
            if (!streaming) {
                if (!Array.isArray(data.content)) { this.unknown(); return; }
                this.array(data.content).forEach((block, i) => this.anthropicBlock(i, block));
                return;
            }
            const n = index(data.index), key = `block-${n}`;
            if (data.type === 'content_block_start') this.anthropicBlock(n, data.content_block);
            else if (data.type === 'content_block_delta') {
                if (n === null || !this.blocks.has(n)) { this.notes.add('missing_block_start'); return; }
                const delta = data.delta || {};
                if (delta.type === 'text_delta' && this.blocks.get(n) === 'text') this.put(key, 'assistant', delta.text, true);
                else if (delta.type === 'thinking_delta' && this.blocks.get(n) === 'thinking') this.put(key, 'reasoning', delta.thinking, true);
                else if (delta.type === 'input_json_delta' && this.blocks.get(n) === 'tool_use') {
                    const part = this.parts.get(key);
                    this.put(key, 'tool_calls', delta.partial_json, !!part?.argumentsStarted, {argumentsStarted: true});
                } else if (delta.type !== 'signature_delta') this.unknown();
            } else if (data.type === 'message_stop') this.done = true;
            else if (!['message_start', 'message_delta', 'content_block_stop', 'ping'].includes(data.type)) this.unknown();
        }
        responseItem(n, item) {
            if (n === null || !item || typeof item !== 'object') { this.unknown(); return; }
            if (item.type === 'function_call') this.tool(`item-${n}-tool`, item);
            else if (item.type === 'reasoning') {
                for (const [i, part] of this.array(item.summary).entries()) {
                    if (typeof part.text === 'string') this.put(`item-${n}-reasoning-${i}`, 'reasoning', part.text);
                    else this.unknown();
                }
            } else if (item.type === 'message') {
                for (const [i, part] of this.array(item.content).entries()) {
                    if (part.type === 'output_text') this.put(`item-${n}-answer-${i}`, 'assistant', part.text);
                    else if (part.type === 'refusal') this.put(`item-${n}-refusal-${i}`, 'output_refusal', part.refusal);
                    else this.unknown();
                }
            } else this.unknown();
        }
        responseSnapshot(response, replace = false) {
            if (!Array.isArray(response?.output)) { this.unknown(); return; }
            if (replace) { this.parts.clear(); this.textSize = 0; }
            if (response.output.length > MAX_SECTIONS) this.limit();
            response.output.slice(0, MAX_SECTIONS).forEach((item, i) => this.responseItem(i, item));
            if (response.error) this.error(response.error);
        }
        responses(data, streaming) {
            if (!streaming) { this.responseSnapshot(data); return; }
            const type = data.type, n = index(data.output_index);
            if (['response.completed', 'response.incomplete', 'response.failed'].includes(type)) {
                // Completed/incomplete snapshots are authoritative replacements,
                // not extra deltas. Failure can still leave useful partial text.
                this.responseSnapshot(data.response, type !== 'response.failed');
                this.failed ||= type === 'response.failed'; this.done = true; return;
            }
            if (['response.created', 'response.in_progress'].includes(type)) return;
            if (type === 'response.output_item.added' || type === 'response.output_item.done') this.responseItem(n, data.item);
            else if (n === null) this.unknown();
            else if (type === 'response.output_text.delta' || type === 'response.output_text.done') {
                const i = index(data.content_index);
                if (i === null) this.unknown();
                else this.put(`item-${n}-answer-${i}`, 'assistant', type.endsWith('.delta') ? data.delta : data.text, type.endsWith('.delta'));
            } else if (type === 'response.reasoning_summary_text.delta' || type === 'response.reasoning_summary_text.done') {
                const i = index(data.summary_index);
                if (i === null) this.unknown();
                else this.put(`item-${n}-reasoning-${i}`, 'reasoning', type.endsWith('.delta') ? data.delta : data.text, type.endsWith('.delta'));
            } else if (type === 'response.function_call_arguments.delta' || type === 'response.function_call_arguments.done') {
                this.put(`item-${n}-tool`, 'tool_calls', type.endsWith('.delta') ? data.delta : data.arguments, type.endsWith('.delta'));
            } else if (type === 'response.content_part.done' || type === 'response.reasoning_summary_part.done') {
                const reasoning = type.includes('reasoning'), i = index(reasoning ? data.summary_index : data.content_index);
                if (i === null) this.unknown();
                else this.put(`item-${n}-${reasoning ? 'reasoning' : 'answer'}-${i}`, reasoning ? 'reasoning' : 'assistant', data.part?.text);
            } else if (!['response.content_part.added', 'response.reasoning_summary_part.added'].includes(type)) this.unknown();
        }
        accept(data, streaming) {
            if (!data || typeof data !== 'object' || Array.isArray(data)) { this.unknown(); return; }
            this.seen = true;
            if (data.error) { this.error(data.error); return; }
            if (streaming && this.family === 'responses' && Number.isSafeInteger(data.sequence_number)) {
                if (this.sequence !== null && data.sequence_number <= this.sequence) { this.notes.add('duplicate_or_reordered_event'); return; }
                if (this.sequence !== null && data.sequence_number !== this.sequence + 1) this.notes.add('event_sequence_gap');
                this.sequence = data.sequence_number;
            }
            if (this.family === 'messages') this.anthropic(data, streaming);
            else if (this.family === 'responses') this.responses(data, streaming);
            else this.openai(data, streaming);
        }
        dispatch() {
            if (!this.frame.length) return;
            const text = this.frame.join('\n'); this.frame = []; this.frameSize = 0;
            if (text === '[DONE]') { this.done = true; return; }
            try { const data = this.parse(text); if (!this.halted) this.accept(data, true); }
            catch (_) { this.notes.add('invalid_event'); }
        }
        feed(fragment) {
            if (!this.family || this.halted || !fragment) return;
            if (fragment.length > MAX_JSON) { this.limit(); return; }
            if (this.format === 'json_done') {
                if (fragment.trim()) this.notes.add('extra_json_data');
                return;
            }
            this.pending += fragment;
            if (!this.format) {
                const beginning = this.pending.trimStart();
                if (!beginning) { if (this.pending.length > MAX_FRAME) this.limit(); return; }
                this.format = beginning.startsWith('{') ? 'json' : 'sse';
            }
            if (this.pending.length > (this.format === 'json' ? MAX_JSON : MAX_FRAME + fragment.length)) { this.limit(); return; }
            if (this.format === 'json') return;
            let newline;
            while (!this.halted && (newline = this.pending.indexOf('\n')) >= 0) {
                let line = this.pending.slice(0, newline); this.pending = this.pending.slice(newline + 1);
                if (line.endsWith('\r')) line = line.slice(0, -1);
                if (line.length > MAX_FRAME) { this.limit(); return; }
                if (!line) this.dispatch();
                else if (line.startsWith('data:')) {
                    const data = line.slice(line[5] === ' ' ? 6 : 5);
                    this.frameSize += data.length;
                    if (this.frameSize > MAX_FRAME) { this.limit(); return; }
                    this.frame.push(data);
                }
            }
            if (this.pending.length > MAX_FRAME) this.limit();
        }
        finish(terminal = false) {
            if (this.format === 'json' && !this.halted) {
                if (this.pending.trimEnd().endsWith('}')) {
                    try {
                        const data = this.parse(this.pending);
                        if (this.halted) return;
                        this.accept(data, false);
                        this.done = true; this.format = 'json_done'; this.pending = '';
                    } catch (_) { if (terminal) this.notes.add('invalid_json'); }
                } else if (terminal) this.notes.add('incomplete_json');
            }
            this.terminal = terminal;
        }
        status() {
            if (!this.family) return 'unavailable';
            if (this.halted) return 'limited';
            if (this.failed) return 'failed';
            if (this.notes.size) return 'partial';
            if (this.done) return 'complete';
            return this.terminal ? 'partial' : this.seen ? 'streaming' : 'waiting';
        }
        sections() {
            return [...this.parts.values()].filter(part => part.text || part.name || part.id).map(part => ({
                ...part, text: part.role === 'tool_calls' ? [part.namespace, part.name, part.id, part.text].filter(Boolean).join('\n') : part.text,
                group: part.key.match(/^(choice|block|item)-(\d+)/)?.slice(1),
            }));
        }
    }
    const api = {ApiOutput}; root.ActivityOutputTools = api;
    if (typeof module !== 'undefined') module.exports = api;
})(typeof self !== 'undefined' ? self : globalThis);
