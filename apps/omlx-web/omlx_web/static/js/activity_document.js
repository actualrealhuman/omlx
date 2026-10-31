// SPDX-License-Identifier: Apache-2.0
// Shared by the inspector worker and local Node tests. No DOM or HTML parsing.
(function (root) {
    'use strict';
    const STRIDE = 128;
    function nextBreak(text, start, columns) {
        let end = start, width = 0;
        while (end < text.length && end - start < columns * 4) {
            const code = text.codePointAt(end), size = code > 0xFFFF ? 2 : 1;
            if (code === 10) return end + 1;
            const cells = code === 9 ? 4 - width % 4
                : /\p{Mark}/u.test(String.fromCodePoint(code)) ? 0
                : code >= 0x2E80 && !(code >= 0xA000 && code < 0xAC00) ? 2 : 1;
            if (width + cells > columns && end > start) break;
            width += cells; end += size;
        }
        return end;
    }
    function textValue(value, labels = {}) {
        if (typeof value === 'string') return value;
        if (Array.isArray(value)) return value.map(part => {
            if (typeof part === 'string') return part;
            if (part && typeof part.text === 'string') return part.text;
            // Media stays inert; never render URLs/base64 as HTML or refetch it.
            if (part && /image|audio|video|file|document/.test(part.type || '')) {
                if (part.type === 'document' && part.source?.type === 'text') return String(part.source.data || '');
                const candidates = [part.image_url?.url, part.image_url, part.input_image, part.url, part.source?.data,
                    part.source?.url, part.input_audio?.data, part.video_url?.url, part.video_url, part.input_video?.url,
                    part.input_video, part.file?.file_data, part.file?.data, part.file?.file_id, part.file_data, part.file_id];
                const reference = candidates.find(item => item?._omlx_activity_attachment)?._omlx_activity_attachment;
                return `[${part.type}${reference?.id ? ' · ' + reference.id : ''}: ${labels.attachment_not_retained || 'attachment preview unavailable'}]`;
            }
            return JSON.stringify(part, null, 2);
        }).join('\n');
        return value == null ? '' : JSON.stringify(value, null, 2);
    }
    function submittedSections(text, labels = {}) {
        try {
            const payload = JSON.parse(text), sections = [];
            const add = (key, role, value) => {
                const body = textValue(value, labels);
                if (body) sections.push({key, role, text: body});
            };
            add('instructions', 'system', payload.system ?? payload.instructions);
            const messages = payload.messages ?? payload.input;
            if (Array.isArray(messages)) messages.forEach((message, i) => {
                if (typeof message === 'string') add(`message-${i}`, 'user', message);
                else {
                    const role = String(message.role || message.type || 'message').slice(0, 64);
                    add(`message-${i}`, role, message.content ?? message);
                    if (message.tool_calls) add(`tools-${i}`, 'tool_calls', message.tool_calls);
                }
            });
            else add('input', 'user', messages ?? payload.prompt);
            add('tools', 'tools', payload.tools);
            const options = {...payload};
            for (const key of ['system', 'instructions', 'messages', 'input', 'prompt', 'tools']) delete options[key];
            if (Object.keys(options).length) add('options', 'options', options);
            return sections.length ? sections : [{key: 'submitted', role: 'submitted', text}];
        } catch (_) {
            // Partial capture is readable but never presented as parsed messages.
            return [{key: 'submitted', role: 'unparsed', text}];
        }
    }

    class ActivityDocument {
        constructor() {
            this.columns = 100;
            this.sections = [];
            this.rows = 0;
        }
        setSections(sections, columns = this.columns) {
            const old = new Map(this.sections.map(section => [section.key, section]));
            this.columns = Math.max(24, Math.min(240, columns));
            let row = 0;
            this.sections = sections.map(item => {
                const previous = old.get(item.key);
                const section = {...item, start: row, checkpoints: [0], count: 0};
                // Append indexing starts at the old final line. Metadata and text
                // remain in the worker; only a bounded viewport crosses to the UI.
                let offset = 0;
                if (previous && previous.columns === this.columns && item.text.startsWith(previous.text)) {
                    section.checkpoints = previous.checkpoints.slice();
                    section.count = Math.floor(Math.max(0, previous.count - 1) / STRIDE) * STRIDE;
                    offset = section.checkpoints[section.count / STRIDE];
                    section.checkpoints.length = section.count / STRIDE + 1;
                }
                while (offset < item.text.length) {
                    if (section.count % STRIDE === 0) section.checkpoints[section.count / STRIDE] = offset;
                    offset = nextBreak(item.text, offset, this.columns);
                    section.count++;
                }
                section.columns = this.columns;
                section.count = Math.max(1, section.count);
                row += section.count + 1; // A labeled header for every semantic section.
                return section;
            });
            this.rows = row;
        }
        offset(section, line) {
            const checkpoint = Math.floor(Math.max(0, line) / STRIDE);
            let offset = section.checkpoints[checkpoint] ?? section.text.length;
            for (let i = checkpoint * STRIDE; i < line; i++) offset = nextBreak(section.text, offset, this.columns);
            return offset;
        }
        anchor(row) {
            const section = this.sections.find(s => row >= s.start && row < s.start + s.count + 1);
            return section ? {key: section.key, offset: this.offset(section, Math.max(0, row - section.start - 1))} : null;
        }
        rowFor(key, offset = 0) {
            const s = this.sections.find(section => section.key === key);
            if (!s) return 0;
            let line = 0, cursor = 0;
            // Binary search sparse checkpoints, then scan at most STRIDE lines.
            let lo = 0, hi = s.checkpoints.length;
            while (lo + 1 < hi) {
                const mid = (lo + hi) >> 1;
                if (s.checkpoints[mid] <= offset) lo = mid; else hi = mid;
            }
            line = lo * STRIDE; cursor = s.checkpoints[lo];
            while (cursor < offset && line < s.count) {
                const next = nextBreak(s.text, cursor, this.columns);
                if (next > offset) break;
                cursor = next; line++;
            }
            return s.start + 1 + Math.min(line, s.count - 1);
        }
        window(start, count = 100) {
            const result = [], end = Math.min(this.rows, start + Math.min(200, count));
            for (const s of this.sections) {
                if (s.start >= end) break;
                if (s.start + s.count < start) continue;
                if (s.start >= start) result.push({row: s.start, key: `${s.key}:header`, role: s.role, header: true, text: s.label || s.role});
                const first = Math.max(0, start - s.start - 1);
                let offset = this.offset(s, first);
                for (let i = first; i < s.count && s.start + 1 + i < end; i++) {
                    const next = nextBreak(s.text, offset, this.columns);
                    result.push({row: s.start + 1 + i, key: `${s.key}:${i}`, role: s.role, text: s.text.slice(offset, next).replace(/\n$/, '')});
                    offset = next;
                }
            }
            return result;
        }
        search(query, selected = 0) {
            // Literal, case-sensitive, complete scan of retained text in this view.
            // Store one target and bounded rail marks, never an unbounded hit list.
            let count = 0, target = null;
            const marks = [];
            if (query) for (const section of this.sections) {
                let offset = section.text.indexOf(query);
                while (offset >= 0) {
                    if (count === selected) target = {key: section.key, offset};
                    if (marks.length < 500) marks.push(this.rowFor(section.key, offset));
                    count++;
                    offset = section.text.indexOf(query, offset + Math.max(1, query.length));
                }
            }
            return {count, target, marks, markersTruncated: count > marks.length};
        }
    }
    const api = {ActivityDocument, submittedSections, textValue};
    if (typeof module !== 'undefined') module.exports = api;
    else root.ActivityDocumentTools = api;
})(typeof self !== 'undefined' ? self : globalThis);
