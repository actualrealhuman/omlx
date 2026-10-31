// SPDX-License-Identifier: Apache-2.0
/* global ActivityDocumentTools, ActivityOutputTools */
importScripts('activity_document.js', 'activity_output.js');
const {ActivityDocument, submittedSections} = ActivityDocumentTools;
const {ApiOutput} = ActivityOutputTools;
const documentView = new ActivityDocument();
const sources = new Map();
const MAX_BYTES = 64 * 1024 * 1024;
let detail, mode = 'submitted', labels = {}, viewport = {row: 0, count: 100, columns: 100};
let bytes = 0, busy = false, pending = false, loading = false, limited = false;
let query = '', match = 0, follow = true;
let searchCache = null;

function sections() {
    const result = [];
    const hasWire = [...sources.values()].some(source => source.item.representation === 'api_response' && source.projection?.family);
    for (const source of sources.values()) {
        const rep = source.item.representation;
        const projected = rep === 'api_response' && ['submitted', 'parsed_output'].includes(mode) && source.projection?.family;
        const include = projected || (mode === 'submitted_json' ? rep === 'submitted' : mode === 'submitted' ? rep === 'submitted' || (!hasWire && rep === 'raw_output')
            : mode === 'model' ? ['model_input', 'raw_output'].includes(rep)
            : mode === rep);
        if (!include || !source.text) continue;
        if (rep === 'submitted' && source.parsedAt !== source.offset) {
            source.parts = submittedSections(source.text, labels); source.parsedAt = source.offset;
        }
        const parts = projected ? source.projection.sections() : rep === 'submitted' && mode !== 'submitted_json' ? source.parts
            : [{key: rep, role: rep, text: source.text}];
        parts.forEach((part, i) => result.push({
            ...part, key: `${source.key}:${part.key}`, isOutput: projected || rep === 'raw_output',
            label: `${labels[part.role] || part.role}${part.group ? ` · ${labels[part.group[0]] || part.group[0]} ${Number(part.group[1]) + 1}` : parts.length > 1 ? ` · ${i + 1}` : ''}${part.name ? ` · ${part.name}` : ''}${source.item.invocation_id !== '0' ? ` · #${source.item.invocation_id}` : ''}`,
        }));
    }
    // Submitted/prepared input precedes output regardless of insertion order.
    return result.sort((a, b) => !!a.isOutput - !!b.isOutput);
}
function projectionState() {
    const projection = [...sources.values()].find(source => source.projection)?.projection;
    return projection ? projection.status() : detail?.finished_at ? 'unavailable' : 'waiting';
}
function projectionIncomplete() {
    return ['submitted', 'parsed_output'].includes(mode) && ['partial', 'limited', 'streaming', 'waiting'].includes(projectionState());
}
function render(reindex = false) {
    if (reindex) {
        const anchor = documentView.anchor(viewport.row);
        documentView.setSections(sections(), viewport.columns);
        if (follow) viewport.row = Math.max(0, documentView.rows - Math.max(1, viewport.count - 20));
        else if (anchor) viewport.row = documentView.rowFor(anchor.key, anchor.offset);
        searchCache = null;
    }
    const search = searchCache || (searchCache = documentView.search(query, match));
    const start = Math.max(0, viewport.row - 10);
    const targetRow = search.target ? documentView.rowFor(search.target.key, search.target.offset) : -1;
    const current = documentView.sections.find(s => viewport.row >= s.start && viewport.row < s.start + s.count + 1);
    postMessage({type: 'render', rows: documentView.window(start, viewport.count).map(line => ({...line, match: line.row === targetRow})), total: documentView.rows,
        row: viewport.row, loading, limited, bytes,
        projectionState: projectionState(), projectionIncomplete: projectionIncomplete(),
        outputBytes: [...sources.values()].filter(source => source.item.representation === 'raw_output' || (['submitted', 'parsed_output', 'api_response'].includes(mode) && source.item.representation === 'api_response')).reduce((sum, source) => sum + source.offset, 0),
        current: current ? {key: current.key, label: current.label, row: current.start} : null,
        outline: documentView.sections.slice(0, 2000).map(s => ({key: s.key, label: s.label, role: s.role, row: s.start, rows: s.count + 1})),
        outlineTruncated: documentView.sections.length > 2000,
        search: {count: search.count, match, marks: search.marks, markersTruncated: search.markersTruncated},
    });
}
async function sync() {
    if (busy) { pending = true; return; }
    busy = true;
    try {
        do {
            pending = false; loading = true; render();
            for (const item of detail.representations) {
                const key = `${item.representation}:${item.invocation_id}`;
                let source = sources.get(key);
                if (!source) {
                    source = {key, item, text: '', offset: 0, decoder: new TextDecoder(),
                        projection: item.representation === 'api_response' ? new ApiOutput(detail.endpoint) : null};
                    sources.set(key, source);
                }
                if (item.bytes < source.offset) throw new Error('History changed; reopen this request.');
                source.item = item;
                while (source.offset < item.bytes && bytes < MAX_BYTES) {
                    const params = new URLSearchParams({representation: item.representation, invocation_id: item.invocation_id,
                        offset: source.offset, limit: Math.min(1024 * 1024, item.bytes - source.offset, MAX_BYTES - bytes)});
                    const response = await fetch(`/admin/api/activity/requests/${encodeURIComponent(detail.id)}/content?${params}`, {cache: 'no-store', credentials: 'same-origin'});
                    if (!response.ok) throw new Error(response.status === 404 ? 'This request is no longer retained.' : `Context read failed (${response.status}).`);
                    const data = new Uint8Array(await response.arrayBuffer());
                    if (!data.length) break;
                    const fragment = source.decoder.decode(data, {stream: true});
                    source.text += fragment;
                    source.projection?.feed(fragment);
                    source.offset += data.length; bytes += data.length;
                    source.projection?.finish(!!detail.finished_at && source.offset >= item.bytes);
                    // Semantic JSON is parsed once its current retained range is complete.
                    if (item.representation !== 'submitted' || source.offset >= item.bytes) render(true);
                }
                if (source.offset < item.bytes) limited = true;
                source.projection?.finish(!!detail.finished_at && source.offset >= item.bytes);
            }
            loading = false; render(true);
        } while (pending);
    } catch (error) {
        loading = false;
        postMessage({type: 'error', message: error.message});
    } finally { busy = false; }
}
self.onmessage = ({data}) => {
    if (data.type === 'sync') {
        detail = data.detail; labels = data.labels || labels;
        sync();
    } else if (data.type === 'viewport') {
        const changed = data.columns !== viewport.columns;
        viewport = {...viewport, ...data}; follow = data.follow;
        render(changed);
    } else if (data.type === 'mode') {
        mode = data.mode; viewport.row = 0; follow = false; render(true);
    } else if (data.type === 'jump') {
        follow = false; viewport.row = Math.max(0, data.row); render();
    } else if (data.type === 'navigate') {
        const starts = documentView.sections.map(s => s.start);
        viewport.row = data.direction > 0 ? starts.find(row => row > viewport.row + 1) ?? Math.max(0, documentView.rows - 1)
            : starts.reverse().find(row => row < viewport.row - 1) ?? 0;
        follow = false; render();
    } else if (data.type === 'search') {
        query = data.query; match = data.match;
        let found = documentView.search(query, match);
        if (found.count && !found.target) {
            match = ((match % found.count) + found.count) % found.count;
            found = documentView.search(query, match);
        }
        if (found.target) { follow = false; viewport.row = documentView.rowFor(found.target.key, found.target.offset); }
        searchCache = found;
        render();
    } else if (data.type === 'export') {
        const text = documentView.sections.map(s => `${s.label}\n${s.text}`).join('\n\n');
        const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
        postMessage({type: 'export', blob, action: data.action, incomplete: loading || limited || !!detail?.capture_gaps || projectionIncomplete()});
    }
};
