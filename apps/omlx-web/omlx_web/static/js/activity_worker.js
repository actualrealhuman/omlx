// SPDX-License-Identifier: Apache-2.0
/* global ActivityDocumentTools, ActivityOutputTools */
importScripts('activity_document.js', 'activity_output.js');
const {ActivityDocument, submittedSections} = ActivityDocumentTools;
const {ApiOutput} = ActivityOutputTools;
const documentView = new ActivityDocument();
const sources = new Map();
const MAX_BYTES = 64 * 1024 * 1024;
const OUTLINE_PAGE_SIZE = 128, MAX_RAIL_BUCKETS = 256;
let detail, mode = 'submitted', labels = {}, viewport = {row: 0, count: 100, columns: 100};
let bytes = 0, busy = false, pending = false, loading = false, limited = false;
let query = '', match = 0, follow = true;
let searchCache = null, pendingSearch = null;
let outlineOffset = 0, outlineManual = false, rail = [];

function boundedText(value, limit) {
    return String(value ?? '').slice(0, limit).replace(/[\uD800-\uDBFF]$/, '');
}
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
function buildRail() {
    const all = documentView.sections, bucketSize = Math.max(1, Math.ceil(all.length / MAX_RAIL_BUCKETS));
    rail = [];
    for (let start = 0; start < all.length; start += bucketSize) {
        const group = all.slice(start, start + bucketSize), first = group[0], last = group[group.length - 1];
        const roles = new Set(group.map(section => section.role));
        rail.push({role: roles.size === 1 ? first.role : 'mixed', row: first.start,
            rows: last.start + last.count + 1 - first.start, from: start + 1, to: start + group.length, count: group.length,
            preview: boundedText(first.text, 128).replace(/\s+/g, ' ')});
    }
}
function render(reindex = false) {
    if (reindex) {
        const anchor = documentView.anchor(viewport.row);
        documentView.setSections(sections(), viewport.columns);
        buildRail();
        outlineOffset = Math.min(Math.floor(outlineOffset / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE,
            Math.max(0, Math.floor((documentView.sections.length - 1) / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE));
        if (follow) viewport.row = Math.max(0, documentView.rows - Math.max(1, viewport.count - 20));
        else if (anchor) viewport.row = documentView.rowFor(anchor.key, anchor.offset);
        searchCache = null;
    }
    if (pendingSearch && !loading && !pending) {
        const intent = pendingSearch;
        pendingSearch = null;
        if (intent.requestId === detail?.id && intent.query === query) {
            match = intent.match;
            let settled = documentView.search(query, match);
            if (settled.count) {
                const normalized = ((match % settled.count) + settled.count) % settled.count;
                if (normalized !== match) {
                    match = normalized;
                    settled = documentView.search(query, match);
                }
                if (settled.target) {
                    follow = false;
                    outlineManual = false;
                    viewport.row = documentView.rowFor(settled.target.key, settled.target.offset);
                    const index = documentView.sections.findIndex(section => section.key === settled.target.key);
                    if (index >= 0) outlineOffset = Math.floor(index / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE;
                }
            }
            searchCache = settled;
        }
    }
    const search = searchCache || (searchCache = documentView.search(query, match));
    const start = Math.max(0, viewport.row - 10);
    const targetRow = search.target ? documentView.rowFor(search.target.key, search.target.offset) : -1;
    const currentIndex = documentView.sections.findIndex(s => viewport.row >= s.start && viewport.row < s.start + s.count + 1);
    const current = currentIndex >= 0 ? documentView.sections[currentIndex] : null;
    if (!outlineManual && currentIndex >= 0) outlineOffset = Math.floor(currentIndex / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE;
    const outline = documentView.sections.slice(outlineOffset, outlineOffset + OUTLINE_PAGE_SIZE).map((section, index) => ({
        key: section.key, label: boundedText(section.label || section.role, 256), role: section.role,
        row: section.start, rows: section.count + 1, ordinal: outlineOffset + index + 1,
    }));
    postMessage({type: 'render', mode, layoutRevision: viewport.layoutRevision || 0, rows: documentView.window(start, viewport.count).map(line => ({...line, match: line.row === targetRow})), total: documentView.rows,
        row: viewport.row, loading, limited, bytes,
        projectionState: projectionState(), projectionIncomplete: projectionIncomplete(),
        outputBytes: [...sources.values()].filter(source => source.item.representation === 'raw_output' || (['submitted', 'parsed_output', 'api_response'].includes(mode) && source.item.representation === 'api_response')).reduce((sum, source) => sum + source.offset, 0),
        current: current ? {key: current.key, label: boundedText(current.label || current.role, 256), row: current.start, ordinal: currentIndex + 1} : null,
        outline, outlineOffset, outlineTotal: documentView.sections.length, outlinePageSize: OUTLINE_PAGE_SIZE,
        outlineHasPrevious: outlineOffset > 0, outlineHasNext: outlineOffset + OUTLINE_PAGE_SIZE < documentView.sections.length,
        rail,
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
        pendingSearch = null;
        loading = false;
        postMessage({type: 'error', message: error.message});
    } finally { busy = false; }
}
self.onmessage = ({data}) => {
    if (data.type === 'sync') {
        if (detail && detail.id !== data.detail.id) pendingSearch = null;
        detail = data.detail; labels = data.labels || labels;
        sync();
    } else if (data.type === 'viewport') {
        const changed = data.columns !== viewport.columns;
        const countChanged = data.count !== viewport.count;
        const moved = data.row !== undefined && data.row !== viewport.row;
        viewport = {...viewport, ...data}; follow = data.follow;
        if (follow && countChanged && !changed) viewport.row = Math.max(0, documentView.rows - Math.max(1, viewport.count - 20));
        if (moved || data.follow === true || data.revealOutline) pendingSearch = null;
        if (moved || data.revealOutline) outlineManual = false;
        render(changed);
    } else if (data.type === 'mode') {
        pendingSearch = null;
        mode = data.mode; viewport.row = 0; follow = false; outlineOffset = 0; outlineManual = false; render(true);
    } else if (data.type === 'outline-page') {
        outlineOffset = Math.max(0, Math.min(Math.floor(Math.max(0, data.offset || 0) / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE,
            Math.floor(Math.max(0, documentView.sections.length - 1) / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE));
        outlineManual = true; render();
    } else if (data.type === 'jump-section') {
        pendingSearch = null;
        const section = documentView.sections.find(item => item.key === data.key);
        if (section) {
            follow = false; outlineManual = false;
            outlineOffset = Math.floor(documentView.sections.indexOf(section) / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE;
            viewport.row = section.start; render();
        }
    } else if (data.type === 'jump') {
        pendingSearch = null;
        follow = false; outlineManual = false; viewport.row = Math.max(0, data.row); render();
    } else if (data.type === 'navigate') {
        pendingSearch = null;
        const starts = documentView.sections.map(s => s.start);
        viewport.row = data.direction > 0 ? starts.find(row => row > viewport.row + 1) ?? Math.max(0, documentView.rows - 1)
            : starts.reverse().find(row => row < viewport.row - 1) ?? 0;
        follow = false; outlineManual = false;
        const index = documentView.sections.findIndex(section => viewport.row >= section.start && viewport.row < section.start + section.count + 1);
        if (index >= 0) outlineOffset = Math.floor(index / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE;
        render();
    } else if (data.type === 'search') {
        query = data.query; match = data.match;
        pendingSearch = null;
        let found = documentView.search(query, match);
        if (query && loading) pendingSearch = {query, match, requestId: detail?.id};
        if (found.count && !found.target) {
            match = ((match % found.count) + found.count) % found.count;
            found = documentView.search(query, match);
        }
        if (found.target && !loading) {
            follow = false; outlineManual = false; viewport.row = documentView.rowFor(found.target.key, found.target.offset);
            const index = documentView.sections.findIndex(section => section.key === found.target.key);
            if (index >= 0) outlineOffset = Math.floor(index / OUTLINE_PAGE_SIZE) * OUTLINE_PAGE_SIZE;
        }
        searchCache = found;
        render();
    } else if (data.type === 'export') {
        const text = documentView.sections.map(s => `${s.label}\n${s.text}`).join('\n\n');
        const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
        postMessage({type: 'export', blob, action: data.action, requestToken: data.requestToken,
            incomplete: loading || limited || !!detail?.capture_gaps || projectionIncomplete()});
    } else if (data.type === 'export-section') {
        const section = documentView.sections.find(item => item.key === data.sectionKey);
        if (!section) {
            postMessage({type: 'export_error', requestToken: data.requestToken, reason: 'section_unavailable'});
            return;
        }
        const blob = new Blob([`${section.label}\n${section.text}`], {type: 'text/plain;charset=utf-8'});
        postMessage({type: 'export', blob, action: 'copy-section', sectionKey: section.key,
            requestToken: data.requestToken, incomplete: loading || limited || !!detail?.capture_gaps || projectionIncomplete()});
    }
};
