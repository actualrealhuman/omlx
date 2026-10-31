// SPDX-License-Identifier: Apache-2.0
(function (root) {
    'use strict';
    const ROW_HEIGHT = 22;
    const API = '/admin/api/activity';
    const roles = ['system', 'developer', 'user', 'assistant', 'tool', 'tool_calls', 'tools', 'options', 'submitted', 'unparsed', 'model_input', 'raw_output', 'api_response', 'model_tokens', 'events'];
    function activityInspector() {
        let stream, worker, resize, controller, retry, searchTimer, frame;
        let generation = 0, activation = 0, loadedVersion = '', stopped = true, lastFacets = 0;
        const positions = new Map();
        const label = key => root.t(`activity.${key}`);
        return {
            active: [], recent: [], older: [], detail: null, selected: '', health: {}, facets: {models: [], sources: []},
            modelFilter: '', sourceFilter: '', connection: 'connecting', error: '', missing: false,
            mode: 'submitted', follow: true, loading: false, limited: false, rows: [], outline: [], total: 0,
            outlineOpen: false, outlineTruncated: false, current: null, row: 0, visibleRows: 25, columns: 100,
            searchQuery: '', searchResult: {count: 0, match: 0, marks: []}, activeMore: false, recentMore: false,
            pageCursor: '', pageScope: 'recent', paging: false, exporting: false, newOutput: false, viewReady: false, outputBytes: 0,
            boot() {
                this._visibility = () => this.activate(this.mainTab === 'activity' && !document.hidden);
                this._navigation = () => { if (this.mainTab === 'activity') this.selectFromUrl(); };
                this.$watch('mainTab', () => this._visibility());
                document.addEventListener('visibilitychange', this._visibility);
                window.addEventListener('popstate', this._navigation);
                window.addEventListener('activity:open', this._navigation);
                resize = new ResizeObserver(() => this.measure());
                resize.observe(this.$refs.contextViewport);
                this._visibility();
            },
            destroy() {
                this.activate(false); resize?.disconnect();
                document.removeEventListener('visibilitychange', this._visibility);
                window.removeEventListener('popstate', this._navigation);
                window.removeEventListener('activity:open', this._navigation);
            },
            async activate(value) {
                if (!value) {
                    stopped = true; generation++; activation++; stream?.close(); stream = null;
                    worker?.terminate(); worker = null; controller?.abort();
                    clearTimeout(retry); clearTimeout(searchTimer); cancelAnimationFrame(frame);
                    if (this.selected) positions.set(this.selected, {row: this.row, mode: this.mode, follow: this.follow});
                    return;
                }
                if (!stopped) return;
                const token = ++activation;
                stopped = false; controller = new AbortController();
                this.selectFromUrl();
                try {
                    const status = await this.read('/status');
                    if (stopped || token !== activation) return;
                    this.health = status.health;
                    await this.loadFacets();
                    if (!stopped && token === activation && !stream) this.connect();
                } catch (error) { if (!stopped && token === activation && error.name !== 'AbortError') this.error = error.message; }
            },
            async read(path) {
                const response = await fetch(API + path, {cache: 'no-store', credentials: 'same-origin', signal: controller?.signal});
                if (!response.ok) throw new Error(response.status === 401 ? label('sign_in') : `${label('read_failed')} (${response.status})`);
                return response.json();
            },
            async loadFacets() {
                if (Date.now() - lastFacets < 15000) return;
                lastFacets = Date.now();
                this.facets = await this.read('/facets');
            },
            connect() {
                stream?.close(); clearTimeout(retry);
                if (stopped) return;
                const params = new URLSearchParams();
                if (this.selected) params.set('request_id', this.selected);
                if (this.modelFilter) params.set('model_id', this.modelFilter);
                if (this.sourceFilter) params.set('source_id', this.sourceFilter);
                this.connection = 'connecting';
                const current = new EventSource(`${API}/events?${params}`);
                stream = current;
                current.addEventListener('snapshot', event => {
                    if (stream !== current || stopped) return;
                    try {
                        const snapshot = JSON.parse(event.data);
                        this.connection = 'live'; this.error = '';
                        this.active = snapshot.active; this.recent = snapshot.recent;
                        this.activeMore = snapshot.active_more; this.recentMore = snapshot.recent_more;
                        this.health = snapshot.health;
                        this.activityActiveCount = this.modelFilter || this.sourceFilter ? null : snapshot.active_more ? `${snapshot.active.length}+` : snapshot.active.length;
                        if (this.selected) {
                            this.missing = !snapshot.detail;
                            this.detail = snapshot.detail;
                            if (snapshot.detail && snapshot.detail.id === this.selected) this.inspect(snapshot.detail);
                            else if (!snapshot.detail) { worker?.terminate(); worker = null; this.rows = []; this.outline = []; this.total = 0; }
                        }
                        this.loadFacets().catch(() => {});
                    } catch (_) { this.error = label('invalid_snapshot'); }
                });
                current.addEventListener('resync_required', () => { this.connection = 'connecting'; });
                for (const type of ['reconnect', 'unavailable', 'authorization_lost']) current.addEventListener(type, () => {
                    if (stream !== current) return;
                    current.close(); this.connection = 'disconnected';
                    if (type === 'authorization_lost') this.error = label('sign_in');
                    else { if (type === 'unavailable') this.error = label('unavailable'); retry = setTimeout(() => this.connect(), 3000); }
                });
                current.onerror = () => { if (stream === current) this.connection = 'reconnecting'; };
            },
            filtersChanged() {
                this.older = []; this.pageCursor = ''; this.connect();
            },
            selectFromUrl() {
                const value = new URLSearchParams(window.location.search).get('request') || '';
                if (value !== this.selected || (!worker && value)) this.select(value, false);
            },
            select(id, push = true) {
                if (this.selected) {
                    positions.set(this.selected, {row: this.row, mode: this.mode, follow: this.follow});
                    if (positions.size > 20) positions.delete(positions.keys().next().value);
                }
                generation++; worker?.terminate(); worker = null; loadedVersion = '';
                this.selected = id; this.detail = null; this.missing = false; this.error = '';
                this.rows = []; this.outline = []; this.total = 0; this.loading = !!id;
                this.current = null; this.viewReady = false; this.outputBytes = 0;
                this.searchQuery = ''; this.searchResult = {count: 0, match: 0, marks: []}; this.newOutput = false;
                const saved = positions.get(id);
                this.row = saved?.row || 0; this.mode = saved?.mode || 'submitted'; this.follow = saved?.follow ?? false;
                if (push) {
                    const url = new URL(window.location.href);
                    url.searchParams.set('tab', 'activity');
                    if (id) url.searchParams.set('request', id); else url.searchParams.delete('request');
                    window.history.pushState({}, '', url);
                }
                if (!stopped) this.connect();
            },
            inspect(detail) {
                if (!worker) {
                    const token = generation;
                    loadedVersion = '';
                    worker = new Worker(this.$el.dataset.workerUrl);
                    worker.onmessage = ({data}) => {
                        if (token !== generation || stopped) return;
                        if (data.type === 'error') { this.error = data.message; this.loading = false; return; }
                        if (data.type === 'export') { this.saveExport(data); return; }
                        if (data.type !== 'render') return;
                        if (!this.follow && this.viewReady && data.outputBytes > this.outputBytes) this.newOutput = true;
                        const previous = new Map(this.rows.map(line => [line.key, line]));
                        this.rows = data.rows.map(line => {
                            const existing = previous.get(line.key);
                            if (!existing) return line;
                            // Preserve mounted line identity and avoid rewriting unchanged
                            // text nodes when output elsewhere in the document grows.
                            Object.assign(existing, line); return existing;
                        });
                        this.outline = data.outline; this.total = data.total;
                        this.outputBytes = data.outputBytes || 0;
                        if (!data.loading) this.viewReady = true;
                        this.current = data.current;
                        this.loading = data.loading; this.limited = data.limited;
                        this.outlineTruncated = data.outlineTruncated; this.searchResult = data.search;
                        this.row = data.row;
                        this.$nextTick(() => {
                            const el = this.$refs.contextViewport, top = this.scrollPixels(data.row);
                            if (Math.abs(el.scrollTop - top) > ROW_HEIGHT) el.scrollTop = top;
                        });
                    };
                    worker.onerror = () => { this.error = label('worker_failed'); this.loading = false; };
                    worker.postMessage({type: 'mode', mode: this.mode});
                    this.measure();
                }
                if (loadedVersion === detail.version) return;
                loadedVersion = detail.version;
                worker.postMessage({type: 'sync', detail, labels: Object.fromEntries(roles.map(role => [role, label(`role.${role}`)]))});
            },
            measure() {
                const el = this.$refs.contextViewport;
                if (!el?.clientWidth) return;
                const canvas = document.createElement('canvas'), context = canvas.getContext('2d');
                context.font = getComputedStyle(el).font;
                this.columns = Math.max(24, Math.min(240, Math.floor((el.clientWidth - 40) / context.measureText('M').width)));
                this.visibleRows = Math.ceil(el.clientHeight / ROW_HEIGHT);
                this.viewport();
            },
            viewport() {
                worker?.postMessage({type: 'viewport', row: this.row, count: this.visibleRows + 20, columns: this.columns, follow: this.follow});
            },
            onScroll() {
                cancelAnimationFrame(frame);
                frame = requestAnimationFrame(() => {
                    const physical = this.$refs.contextViewport.scrollTop / ROW_HEIGHT;
                    const next = this.total <= 200000 ? Math.floor(physical) : Math.round(
                        physical / Math.max(1, 200000 - this.visibleRows) * Math.max(0, this.total - this.visibleRows));
                    if (Math.abs(next - this.row) > 1) { this.follow = false; this.row = next; this.viewport(); }
                });
            },
            scrollHeight() { return Math.min(200000, this.total) * ROW_HEIGHT; },
            scrollPixels(row) {
                return this.total <= 200000 ? row * ROW_HEIGHT : row / Math.max(1, this.total - this.visibleRows) * (200000 - this.visibleRows) * ROW_HEIGHT;
            },
            lineTop(line) { return this.scrollPixels(this.row) + (line.row - this.row) * ROW_HEIGHT; },
            changeMode() { this.row = 0; this.follow = false; worker?.postMessage({type: 'mode', mode: this.mode}); },
            jump(row) { this.follow = false; worker?.postMessage({type: 'jump', row}); },
            navigate(direction) {
                this.follow = false; worker?.postMessage({type: 'navigate', direction});
            },
            followOutput() { this.follow = true; this.newOutput = false; this.row = Math.max(0, this.total - this.visibleRows); this.viewport(); },
            search(direction = 0) {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(() => {
                    this.follow = false;
                    worker?.postMessage({type: 'search', query: this.searchQuery, match: direction ? this.searchResult.match + direction : 0});
                }, direction ? 0 : 200);
            },
            railJump(event) {
                const rect = event.currentTarget.getBoundingClientRect();
                this.jump(Math.floor(Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) * Math.max(0, this.total - this.visibleRows)));
            },
            async more(scope) {
                this.paging = true; this.pageScope = scope;
                const filters = [this.modelFilter, this.sourceFilter].join('\n');
                try {
                    const params = new URLSearchParams({scope, limit: 100});
                    if (this.modelFilter) params.set('model_id', this.modelFilter);
                    if (this.sourceFilter) params.set('source_id', this.sourceFilter);
                    if (this.pageCursor) params.set('cursor', this.pageCursor);
                    const data = await this.read(`/requests?${params}`);
                    if (filters !== [this.modelFilter, this.sourceFilter].join('\n')) return;
                    const visible = new Set([...this.active, ...this.recent].map(record => record.id));
                    this.older = data.requests.filter(record => !visible.has(record.id));
                    this.pageCursor = data.next_cursor || '';
                } catch (error) { this.error = error.message; } finally { this.paging = false; }
            },
            exportView(action = 'download') { this.exporting = true; worker?.postMessage({type: 'export', action}); },
            async saveExport(data) {
                if (data.action === 'copy') {
                    try {
                        await navigator.clipboard.writeText(await data.blob.text());
                        this.exporting = false; return;
                    } catch (_) { /* A download is the fallback for denied clipboard access. */ }
                }
                const url = URL.createObjectURL(data.blob), link = document.createElement('a');
                link.href = url; link.download = `activity-${this.selected}.txt`; link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000); this.exporting = false;
            },
            label, stateLabel(value) { return label(`state.${value}`); },
            bytes(value) { if (!value) return '0 B'; const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1024))); return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${['B', 'KiB', 'MiB', 'GiB'][unit]}`; },
            time(value) { return value ? new Date(value * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '—'; },
            duration(record) { return record?.finished_at ? `${Math.max(0, record.finished_at - record.received_at).toFixed(1)} s` : '—'; },
            currentSection() { return this.current || this.outline[0]; },
        };
    }
    root.activityInspector = activityInspector;
    if (typeof module !== 'undefined') module.exports = {activityInspector};
})(typeof window !== 'undefined' ? window : globalThis);
