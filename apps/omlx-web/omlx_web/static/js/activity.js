// SPDX-License-Identifier: Apache-2.0
(function (root) {
    'use strict';
    const ROW_HEIGHT = 22;
    const API = '/admin/api/activity';
    const pauseReasons = ['prefill_eviction', 'memory_requeue', 'generation_overflow', 'cache_recovery', 'batch_reset', 'oom_retry'];
    const invocationActiveStates = ['admitted', 'queued', 'prefilling', 'generating'];
    const roles = ['system', 'developer', 'user', 'assistant', 'reasoning', 'output_error', 'output_refusal', 'choice', 'block', 'item', 'tool', 'tool_calls', 'tools', 'options', 'submitted', 'unparsed', 'model_input', 'raw_output', 'api_response', 'model_tokens', 'events'];
    function parseActivityCursor(value) {
        if (typeof value !== 'string' || value.length > 128) return null;
        const match = /^([0-9a-f]{32}):([0-9a-f]{32}):(0|[1-9][0-9]{0,19})$/.exec(value);
        if (!match || match[0].length !== value.length) return null;
        const sequence = match[3];
        if (sequence.length > 20 || (sequence.length === 20 && sequence > '18446744073709551615')) return null;
        return {epoch: match[1], channel: match[2], sequence};
    }
    function compareActivitySequence(left, right) {
        return left.length === right.length ? (left === right ? 0 : left < right ? -1 : 1) : left.length < right.length ? -1 : 1;
    }
    function activityInspector() {
        let stream, worker, resize, controller, retry, searchTimer, frame, invocationController, pageController;
        let observationTimer = null, observationGeneration = 0;
        let invocationGeneration = 0;
        let generation = 0, activation = 0, pageGeneration = 0, loadedVersion = '', stopped = true, lastFacets = 0, exportSequence = 0, programmaticScrollRow = null;
        let resumeCursor = '', appliedCursor = null, streamChannel = '', freshHeadRetried = false;
        const positions = new Map();
        const label = key => root.t(`activity.${key}`);
        return {
            active: [], recent: [], older: [], detail: null, selected: '', health: {}, facets: {models: [], sources: []},
            modelFilter: '', sourceFilter: '', scopeFilter: 'all', stateFilter: '', receivedFrom: '', receivedUntil: '',
            selectedOutsideFilters: null, filtersLoading: false, filterError: '', connection: 'connecting', error: '', missing: false, authorizationLost: false,
            mode: 'submitted', follow: true, loading: false, limited: false, rows: [], outline: [], rail: [], total: 0,
            outlineOpen: false, outlineOffset: 0, outlineTotal: 0, outlinePageSize: 128, outlineHasPrevious: false, outlineHasNext: false,
            current: null, row: 0, visibleRows: 25, columns: 100, observationNow: null,
            searchQuery: '', searchResult: {count: 0, match: 0, marks: []}, activeMore: false, recentMore: false,
            pageCursor: '', pageScope: 'recent', paging: false, exporting: false, exportError: '', exportContext: null, newOutput: false, viewReady: false, outputBytes: 0,
            settingsOpen: false, settingsDraft: {enabled: false, retain_images: true, max_requests: 1000, max_age_days: 7, storageGiB: 5},
            attachmentsOpen: false, previewId: '', previewFailed: false,
            projectionState: 'waiting', projectionIncomplete: false,
            coverageOpen: false,
            invocationId: '', invocationOpen: false, invocationDetail: null, invocationBusy: false, invocationError: '', allPosition: null,
            configVersion: '', controlBusy: false, pendingAction: '', pendingDeleteId: '',
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
                    this.stopObservationClock();
                    this.pendingAction = '';
                    this.attachmentsOpen = false; this.previewId = '';
                    this.resetInvocationDetail();
                    this.exporting = false;
                    this.exportContext = null;
                    pageGeneration++; pageController?.abort(); pageController = null; this.paging = false;
                    stopped = true; generation++; activation++; stream?.close(); stream = null;
                    worker?.terminate(); worker = null; controller?.abort();
                    clearTimeout(retry); clearTimeout(searchTimer); cancelAnimationFrame(frame);
                    if (this.selected) positions.set(this.selected, {row: this.row, mode: this.mode, follow: this.follow, invocationId: this.invocationId, allPosition: this.allPosition});
                    return;
                }
                if (!stopped) return;
                const token = ++activation;
                stopped = false; controller = new AbortController();
                this.selectFromUrl();
                try {
                    const status = await this.read('/status');
                    if (stopped || token !== activation) return;
                    this.authorizationLost = false;
                    this.health = status.health;
                    this.configVersion = status.config_version || '';
                    await this.loadFacets();
                    if (!stopped && token === activation && !stream) this.connect();
                } catch (error) { if (!stopped && token === activation && error.name !== 'AbortError') this.error = error.message; }
            },
            async read(path, signal = controller?.signal) {
                const response = await fetch(API + path, {cache: 'no-store', credentials: 'same-origin', signal});
                if (!response.ok) throw new Error(response.status === 401 ? label('sign_in') : `${label('read_failed')} (${response.status})`);
                return response.json();
            },
            async loadFacets() {
                if (Date.now() - lastFacets < 15000) return;
                lastFacets = Date.now();
                this.facets = await this.read('/facets');
            },
            async openSettings() {
                try {
                    const status = await this.read('/status');
                    this.health = status.health; this.configVersion = status.config_version;
                    this.settingsDraft = {...status.settings, storageGiB: status.settings.max_storage_bytes / 1024 ** 3};
                    this.settingsOpen = !this.settingsOpen;
                } catch (error) { this.error = error.message; }
            },
            async write(path, method, action, body) {
                const response = await fetch(API + path, {method, credentials: 'same-origin', cache: 'no-store', signal: controller?.signal,
                    headers: {'Content-Type': 'application/json', 'X-Activity-Action': action,
                        ...(action === 'settings' && this.configVersion ? {'If-Match': this.configVersion} : {})},
                    body: JSON.stringify(body)});
                if (!response.ok) {
                    const data = await response.json().catch(() => ({}));
                    throw new Error(typeof data.detail === 'string' ? data.detail : `${label('control_failed')} (${response.status})`);
                }
                return response.json();
            },
            async saveSettings() {
                this.controlBusy = true; this.error = '';
                try {
                    const draft = this.settingsDraft;
                    const status = await this.write('/settings', 'PUT', 'settings', {enabled: draft.enabled, retain_images: draft.retain_images,
                        max_requests: Number(draft.max_requests), max_age_days: Number(draft.max_age_days),
                        max_storage_bytes: Math.round(Number(draft.storageGiB) * 1024 ** 3)});
                    this.health = status.health; this.configVersion = status.config_version;
                    this.settingsOpen = false; this.connect();
                } catch (error) { if (error.name !== 'AbortError') this.error = error.message; }
                finally { this.controlBusy = false; }
            },
            confirmRemoval(action) {
                this.pendingAction = action; this.pendingDeleteId = this.selected;
            },
            async removeConfirmed() {
                const action = this.pendingAction, id = this.pendingDeleteId;
                if (!['clear', 'delete'].includes(action) || (action === 'delete' && !id)) return;
                this.controlBusy = true; this.error = '';
                try {
                    await this.write(action === 'clear' ? '/clear' : `/requests/${encodeURIComponent(id)}`,
                        action === 'clear' ? 'POST' : 'DELETE', action, {confirm: action});
                    this.pendingAction = ''; this.older = []; this.pageCursor = ''; lastFacets = 0;
                    if (action === 'clear' || this.selected === id) this.select(''); else this.connect();
                    this.loadFacets().catch(() => {});
                } catch (error) { if (error.name !== 'AbortError') this.error = error.message; }
                finally { this.controlBusy = false; }
            },
            connect(preserveCursor = false, freshHead = false) {
                stream?.close(); clearTimeout(retry);
                if (stopped) return;
                this.filterError = this.rangeError();
                if (this.filterError) {
                    stream = null; this.filtersLoading = false; this.connection = 'connecting';
                    return;
                }
                this.filtersLoading = true;
                const params = new URLSearchParams();
                if (this.selected) params.set('request_id', this.selected);
                params.set('scope', this.scopeFilter);
                if (this.modelFilter) params.set('model_id', this.modelFilter);
                if (this.sourceFilter) params.set('source_id', this.sourceFilter);
                if (this.stateFilter) params.set('state', this.stateFilter);
                const since = this.localTimeSeconds(this.receivedFrom), until = this.localTimeSeconds(this.receivedUntil);
                if (since !== null) params.set('since', since);
                if (until !== null) params.set('until', until);
                const channel = params.toString();
                if (!preserveCursor || channel !== streamChannel) {
                    resumeCursor = ''; appliedCursor = null;
                    this.selectedOutsideFilters = null;
                }
                if (!preserveCursor && !freshHead) freshHeadRetried = false;
                streamChannel = channel;
                if (preserveCursor && resumeCursor) params.set('cursor', resumeCursor);
                this.connection = 'connecting';
                const current = new EventSource(`${API}/events?${params}`);
                stream = current;
                const rejectCursor = () => {
                    if (stream !== current || stopped || document.hidden || this.mainTab !== 'activity') return;
                    this.error = label('invalid_snapshot');
                    if (freshHeadRetried) {
                        current.close(); stream = null; clearTimeout(retry);
                        this.connection = 'disconnected'; this.filtersLoading = false;
                        return;
                    }
                    freshHeadRetried = true; resumeCursor = ''; appliedCursor = null;
                    current.close(); stream = null; this.filtersLoading = false;
                    retry = setTimeout(() => this.connect(false, true), 3000);
                };
                current.addEventListener('snapshot', event => {
                    if (stream !== current || stopped || document.hidden || this.mainTab !== 'activity') return;
                    try {
                        const snapshot = JSON.parse(event.data);
                        const cursor = parseActivityCursor(event.lastEventId);
                        if (!cursor) {
                            rejectCursor();
                            return;
                        }
                        if (appliedCursor && (cursor.epoch !== appliedCursor.epoch || cursor.channel !== appliedCursor.channel)) {
                            rejectCursor();
                            return;
                        }
                        if (appliedCursor && compareActivitySequence(cursor.sequence, appliedCursor.sequence) <= 0) {
                            this.authorizationLost = false;
                            this.connection = 'live'; this.error = ''; this.filtersLoading = false;
                            return;
                        }
                        this.authorizationLost = false;
                        this.connection = 'live'; this.error = '';
                        this.filtersLoading = false;
                        this.active = snapshot.active; this.recent = snapshot.recent;
                        this.activeMore = snapshot.active_more; this.recentMore = snapshot.recent_more;
                        this.health = snapshot.health;
                        this.activityActiveCount = this.hasPopulationFilters() ? null : snapshot.active_more ? `${snapshot.active.length}+` : snapshot.active.length;
                        this.selectedOutsideFilters = this.selected ? snapshot.selected_outside_filters : null;
                        if (this.selected) {
                            this.missing = !snapshot.detail;
                            this.detail = snapshot.detail;
                            if (snapshot.detail && snapshot.detail.id === this.selected) this.inspect(snapshot.detail);
                            else if (!snapshot.detail) { this.resetInvocationDetail(); this.exportContext = null; this.exporting = false; this.exportError = ''; this.attachmentsOpen = false; this.previewId = ''; generation++; worker?.terminate(); worker = null; loadedVersion = ''; this.rows = []; this.outline = []; this.rail = []; this.current = null; this.total = 0; this.outlineOffset = 0; this.outlineTotal = 0; }
                        }
                        this.syncObservationClock();
                        this.loadFacets().catch(() => {});
                        appliedCursor = cursor;
                        resumeCursor = event.lastEventId;
                        freshHeadRetried = false;
                    } catch (_) { this.error = label('invalid_snapshot'); }
                });
                current.addEventListener('resync_required', () => {
                    if (stream !== current || stopped || document.hidden || this.mainTab !== 'activity') return;
                    resumeCursor = ''; appliedCursor = null;
                    this.connection = 'connecting'; this.filtersLoading = true;
                });
                for (const type of ['reconnect', 'unavailable', 'authorization_lost']) current.addEventListener(type, () => {
                    if (stream !== current) return;
                    current.close(); stream = null; this.connection = 'disconnected';
                    this.filtersLoading = false;
                    if (type === 'authorization_lost') { resumeCursor = ''; appliedCursor = null; this.stopObservationClock(); pageGeneration++; pageController?.abort(); pageController = null; this.paging = false; this.authorizationLost = true; this.resetInvocationDetail(); this.exportContext = null; this.exporting = false; this.exportError = label('sign_in'); this.error = label('sign_in'); this.attachmentsOpen = false; this.previewId = ''; generation++; worker?.terminate(); worker = null; loadedVersion = ''; }
                    else { if (type === 'unavailable') this.error = label('unavailable'); retry = setTimeout(() => this.connect(true), 3000); }
                });
                current.onerror = () => { if (stream === current) { this.connection = 'reconnecting'; this.filtersLoading = false; } };
            },
            filtersChanged() {
                pageGeneration++; pageController?.abort(); pageController = null; this.paging = false;
                this.older = []; this.pageCursor = ''; this.pageScope = this.scopeFilter === 'active' ? 'active' : 'recent';
                this.active = []; this.recent = []; this.activeMore = false; this.recentMore = false;
                this.selectedOutsideFilters = null; this.activityActiveCount = null;
                this.filterError = this.rangeError();
                if (this.filterError) {
                    stream?.close(); stream = null; clearTimeout(retry);
                    this.filtersLoading = false; this.connection = 'connecting';
                    return;
                }
                this.connect();
            },
            localTimeSeconds(value) {
                if (!value) return null;
                const milliseconds = new Date(value).getTime();
                return Number.isFinite(milliseconds) ? milliseconds / 1000 : NaN;
            },
            modelTitle(record) {
                if (record?.model_id === '<default>' && record.model_resolution_observed !== true) return label('default_model');
                return record?.model_id || label('loading');
            },
            modelRequestTitle(record) {
                return record?.requested_model_id === '<default>' ? label('default_model') : record?.requested_model_id || '';
            },
            modelSummary(record) {
                const requested = this.modelRequestTitle(record);
                const serving = record?.model_resolution_observed === true ? (record.model_id || '') : label('resolution_unobserved');
                return requested && (record?.model_resolution_observed !== true || requested !== serving)
                    ? `${label('requested_model')}: ${requested} · ${label('serving_model')}: ${serving}`
                    : record?.model_resolution_observed === true ? '' : label('resolution_unobserved');
            },
            rangeError() {
                const since = this.localTimeSeconds(this.receivedFrom), until = this.localTimeSeconds(this.receivedUntil);
                if ((this.receivedFrom && !Number.isFinite(since)) || (this.receivedUntil && !Number.isFinite(until)) || (since !== null && until !== null && since > until)) return label('invalid_received_range');
                return '';
            },
            hasPopulationFilters() {
                return this.scopeFilter !== 'all' || !!this.stateFilter || !!this.receivedFrom || !!this.receivedUntil || !!this.modelFilter || !!this.sourceFilter;
            },
            visibleGroups() {
                const groups = [];
                if (this.scopeFilter !== 'recent') groups.push({title: label('active'), rows: this.active, more: this.activeMore, scope: 'active'});
                if (this.scopeFilter !== 'active') groups.push({title: label('recent'), rows: this.recent, more: this.recentMore, scope: 'recent'});
                return groups;
            },
            selectFromUrl() {
                const value = new URLSearchParams(window.location.search).get('request') || '';
                if (value !== this.selected || (!worker && value)) this.select(value, false);
            },
            select(id, push = true) {
                this.stopObservationClock();
                if (this.selected) {
                    positions.set(this.selected, {row: this.row, mode: this.mode, follow: this.follow, invocationId: this.invocationId, allPosition: this.allPosition});
                    if (positions.size > 20) positions.delete(positions.keys().next().value);
                }
                generation++; worker?.terminate(); worker = null; loadedVersion = '';
                this.selected = id; this.detail = null; this.missing = false; this.error = '';
                this.exportContext = null; this.exporting = false; this.exportError = '';
                this.attachmentsOpen = false; this.previewId = ''; this.previewFailed = false;
                this.rows = []; this.outline = []; this.rail = []; this.outlineOffset = 0; this.outlineTotal = 0; this.total = 0; this.loading = !!id;
                this.current = null; this.viewReady = false; this.outputBytes = 0; this.exporting = false;
                this.projectionState = 'waiting';
                this.projectionIncomplete = false;
                this.coverageOpen = false;
                this.resetInvocationDetail();
                this.searchQuery = ''; this.searchResult = {count: 0, match: 0, marks: []}; this.newOutput = false;
                const saved = positions.get(id);
                this.invocationId = saved?.invocationId || ''; this.allPosition = saved?.allPosition || null;
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
                    worker = new Worker(this.$root.dataset.workerUrl);
                    worker.onmessage = ({data}) => {
                        if (token !== generation || stopped) return;
                        if (data.type === 'error') { this.error = data.message; this.loading = false; return; }
                        if (data.type === 'export' || data.type === 'export_error') { this.saveExport(data); return; }
                        if (data.type !== 'render') return;
                        if (data.mode !== undefined && data.mode !== this.mode) return;
                        if (!this.follow && this.viewReady && data.outputBytes > this.outputBytes) this.newOutput = true;
                        const previous = new Map(this.rows.map(line => [line.key, line]));
                        const copyContext = {...this.exportIdentity()};
                        this.rows = data.rows.map(line => {
                            const existing = previous.get(line.key);
                            if (!existing) return {...line, copyContext};
                            // Preserve mounted line identity and avoid rewriting unchanged
                            // text nodes when output elsewhere in the document grows.
                            Object.assign(existing, line, {copyContext}); return existing;
                        });
                        this.outline = data.outline || []; this.rail = data.rail || []; this.total = data.total;
                        this.outlineOffset = data.outlineOffset || 0; this.outlineTotal = data.outlineTotal || 0;
                        this.outlinePageSize = data.outlinePageSize || 128;
                        this.outlineHasPrevious = !!data.outlineHasPrevious; this.outlineHasNext = !!data.outlineHasNext;
                        this.outputBytes = data.outputBytes || 0;
                        if (!data.loading) this.viewReady = true;
                        this.current = data.current;
                        this.loading = data.loading; this.limited = data.limited;
                        this.projectionState = data.projectionState || 'waiting';
                        this.projectionIncomplete = !!data.projectionIncomplete;
                        this.searchResult = data.search;
                        this.row = data.row;
                        this.$nextTick(() => {
                            if (token !== generation || stopped) return;
                            const el = this.$refs.contextViewport, top = this.scrollPixels(data.row);
                            if (Math.abs(el.scrollTop - top) > ROW_HEIGHT) { programmaticScrollRow = data.row; el.scrollTop = top; }
                        });
                    };
                    worker.onerror = () => { this.error = label('worker_failed'); this.loading = false; };
                    worker.postMessage({type: 'mode', mode: this.mode});
                    this.measure();
                }
                if (loadedVersion === detail.version) return;
                loadedVersion = detail.version;
                const view = this.invocationId ? {...detail, representations: detail.representations.filter(item => item.invocation_id === this.invocationId)} : detail;
                worker.postMessage({type: 'sync', detail: view, labels: {...Object.fromEntries(roles.map(role => [role, label(`role.${role}`)])), attachment_not_retained: label('attachment_not_retained'), attachment_reference: label('attachment_reference')}});
            },
            resetInvocationDetail() {
                invocationGeneration++; invocationController?.abort(); invocationController = null;
                this.invocationOpen = false; this.invocationDetail = null; this.invocationBusy = false; this.invocationError = '';
            },
            invocationChoices() {
                const ids = new Set([...(this.detail?.invocations || []).map(item => item.id),
                    ...(this.detail?.representations || []).map(item => item.invocation_id), this.invocationId]);
                return [...ids].filter(id => typeof id === 'string' && /^[1-9]\d{0,2}$/.test(id) && Number(id) <= 256).sort((a, b) => Number(a) - Number(b)).slice(0, 256);
            },
            invocationSummary() {
                return {...this.invocationDetail, ...(this.detail?.invocations || []).find(item => item.id === this.invocationId)};
            },
            invocationLabel(id) {
                const item = (this.detail?.invocations || []).find(value => value.id === id);
                const parts = [`#${id}`, this.invocationState(id)];
                if (Number.isInteger(item?.pause_count) && item.pause_count > 0) {
                    parts.push(label('invocation.pause_count').replace('{count}', item.pause_count));
                    const reason = item.last_pause_reason;
                    if (typeof reason === 'string' && pauseReasons.includes(reason)) {
                        parts.push(label(`invocation.pause_reason.${reason}`));
                    }
                    if (item.state === 'observation_ended' && item.last_observed_paused === true) {
                        parts.push(label('invocation.last_observed_paused'));
                    } else if (invocationActiveStates.includes(item.state) && item.currently_paused === true) {
                        parts.push(label('invocation.paused'));
                    }
                }
                return parts.join(' · ');
            },
            invocationPauseSummary() {
                const item = this.invocationSummary() || {};
                if (!Number.isInteger(item.pause_count) || item.pause_count <= 0) return label('invocation.no_pauses');
                const parts = [label('invocation.pause_count').replace('{count}', item.pause_count)];
                const reason = item.last_pause_reason;
                if (typeof reason === 'string' && pauseReasons.includes(reason)) {
                    parts.push(label(`invocation.pause_reason.${reason}`));
                }
                if (item.state === 'observation_ended') {
                    if (item.last_observed_paused === true) parts.push(label('invocation.last_observed_paused'));
                } else if (invocationActiveStates.includes(item.state) && item.currently_paused === true) {
                    parts.push(label('invocation.paused'));
                }
                return parts.join(' · ');
            },
            invocationState(id = this.invocationId) {
                const state = (this.detail?.invocations || []).find(item => item.id === id)?.state
                    || (this.invocationDetail?.id === id ? this.invocationDetail.state : null);
                return ['admitted', 'observation_ended'].includes(state) ? label(`invocation.${state}`)
                    : ['received', 'loading', 'preparing', 'queued', 'prefilling', 'generating', 'completed', 'rejected', 'cancelled', 'error', 'interrupted', 'capture_stopped'].includes(state) ? this.stateLabel(state) : label('invocation.unknown');
            },
            chooseInvocation(id) {
                if (typeof id !== 'string' || (id && (!/^[1-9]\d{0,2}$/.test(id) || Number(id) > 256))) return;
                if (id === this.invocationId) return;
                if (!this.invocationId) this.allPosition = {row: this.row, mode: this.mode, follow: this.follow};
                this.resetInvocationDetail(); this.invocationId = id;
                const position = !id && this.allPosition;
                this.row = position?.row || 0; this.mode = position?.mode || 'model'; this.follow = position?.follow ?? false;
                if (!id) this.allPosition = null;
                clearTimeout(searchTimer); this.searchQuery = ''; this.searchResult = {count: 0, match: 0, marks: []};
                generation++; worker?.terminate(); worker = null; loadedVersion = '';
                this.rows = []; this.outline = []; this.rail = []; this.outlineOffset = 0; this.outlineTotal = 0; this.total = 0; this.current = null; this.viewReady = false; this.outputBytes = 0;
                this.loading = !!this.detail; this.newOutput = false; this.exporting = false;
                this.exportContext = null; this.exportError = '';
                if (this.detail) this.inspect(this.detail);
            },
            async toggleInvocationOptions() {
                if (this.invocationOpen) { this.resetInvocationDetail(); return; }
                this.invocationOpen = true;
                await this.loadInvocation();
            },
            async loadInvocation() {
                if (!this.invocationId || !this.selected || stopped || !this.invocationOpen || this.invocationBusy) return;
                const token = ++invocationGeneration, request = this.selected, id = this.invocationId;
                invocationController?.abort(); invocationController = new AbortController();
                this.invocationBusy = true; this.invocationError = ''; this.invocationDetail = null;
                try {
                    const value = await this.read(`/requests/${encodeURIComponent(request)}/invocations/${encodeURIComponent(id)}`, invocationController.signal);
                    if (token !== invocationGeneration || stopped || request !== this.selected || id !== this.invocationId) return;
                    if (value?.id !== id) throw new Error(label('invocation.unavailable'));
                    this.invocationDetail = value;
                } catch (error) {
                    if (token === invocationGeneration && !stopped && error.name !== 'AbortError') this.invocationError = error.message;
                } finally { if (token === invocationGeneration) this.invocationBusy = false; }
            },
            invocationOptions() {
                return this.invocationDetail?.admission_options ? JSON.stringify(this.invocationDetail.admission_options, null, 2) : label('invocation.unavailable');
            },
            toggleAttachments() {
                if (this.authorizationLost) { this.attachmentsOpen = false; this.previewId = ''; return; }
                this.attachmentsOpen = !this.attachmentsOpen; this.previewFailed = false;
                this.previewId = this.attachmentsOpen ? (this.detail?.attachments?.find(item => item.status === 'retained') || this.detail?.attachments?.[0])?.id || '' : '';
            },
            backendLabel() {
                const known = ['local_text', 'local_vlm', 'local_diffusion', 'distributed_proxy', 'splash_native', 'unknown'];
                const kinds = this.detail?.coverage?.backend_kinds;
                return Array.isArray(kinds) && kinds.length ? kinds.slice(0, 6).map(kind => label(`backend.${known.includes(kind) ? kind : 'unknown'}`)).join(' + ') : label('backend.unresolved');
            },
            coverageRows() {
                return ['submitted', 'prepared_text', 'prepared_tokens', 'producer_output', 'api_wire', 'images'].map(key => {
                    const value = this.detail?.coverage?.evidence?.[key];
                    return {key, state: ['retained', 'observed_not_retained'].includes(value) ? value : 'not_observed'};
                });
            },
            previewInfo() {
                return this.detail?.attachments?.find(item => item.id === this.previewId);
            },
            imageUrl(id) {
                if (stopped || !this.attachmentsOpen || this.detail?.id !== this.selected || !/^attachment-[0-9]+$/.test(id)) return null;
                if (!this.detail.attachments?.some(item => item.id === id && item.status === 'retained')) return null;
                return `${API}/requests/${encodeURIComponent(this.selected)}/attachments/${encodeURIComponent(id)}`;
            },
            imageFailed(event) {
                const current = this.imageUrl(this.previewId);
                if (current && new URL(event.target.src, window.location.href).pathname === current) this.previewFailed = true;
            },
            imageReason(info) {
                const known = ['attachment_size_limit', 'attachment_request_limit', 'attachment_queue_limit', 'image_retention_disabled',
                    'reference_not_fetched', 'unsupported_image_type', 'unsupported_image_encoding', 'image_verification_failed'];
                return label(known.includes(info?.reason) ? `image_reason.${info.reason}` : 'image_unavailable');
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
            viewport(revealOutline = false) {
                worker?.postMessage({type: 'viewport', row: this.row, count: this.visibleRows + 20, columns: this.columns, follow: this.follow, revealOutline});
            },
            onScroll() {
                cancelAnimationFrame(frame);
                frame = requestAnimationFrame(() => {
                    const physical = this.$refs.contextViewport.scrollTop / ROW_HEIGHT;
                    const next = this.total <= 200000 ? Math.floor(physical) : Math.round(
                        physical / Math.max(1, 200000 - this.visibleRows) * Math.max(0, this.total - this.visibleRows));
                    if (programmaticScrollRow !== null && Math.abs(next - programmaticScrollRow) <= 1) { programmaticScrollRow = null; return; }
                    programmaticScrollRow = null;
                    if (Math.abs(next - this.row) > 1) { this.follow = false; this.row = next; this.viewport(); }
                });
            },
            scrollHeight() { return Math.min(200000, this.total) * ROW_HEIGHT; },
            scrollPixels(row) {
                return this.total <= 200000 ? row * ROW_HEIGHT : row / Math.max(1, this.total - this.visibleRows) * (200000 - this.visibleRows) * ROW_HEIGHT;
            },
            lineTop(line) { return this.scrollPixels(this.row) + (line.row - this.row) * ROW_HEIGHT; },
            changeMode() {
                if (this.invocationId && !['model', 'raw_output', 'model_tokens'].includes(this.mode)) {
                    const mode = this.mode; this.chooseInvocation(''); this.mode = mode;
                }
                if (this.exportContext) { this.exportContext = null; this.exporting = false; this.exportError = label('selection_changed'); }
                this.row = 0; this.follow = false; worker?.postMessage({type: 'mode', mode: this.mode});
            },
            jump(row) { this.follow = false; worker?.postMessage({type: 'jump', row}); },
            jumpSection(key) { this.follow = false; worker?.postMessage({type: 'jump-section', key}); },
            pageOutline(direction) {
                const offset = Math.max(0, Math.min(Math.floor(Math.max(0, this.outlineTotal - 1) / this.outlinePageSize) * this.outlinePageSize,
                    this.outlineOffset + direction * this.outlinePageSize));
                worker?.postMessage({type: 'outline-page', offset});
            },
            outlinePageLabel() {
                const start = this.outlineTotal ? this.outlineOffset + 1 : 0;
                const end = Math.min(this.outlineTotal, this.outlineOffset + this.outlinePageSize);
                return label('outline_page').replace('{start}', start).replace('{end}', end).replace('{total}', this.outlineTotal);
            },
            messageOrdinal() {
                return this.current?.ordinal ? label('message_ordinal').replace('{current}', this.current.ordinal).replace('{total}', this.outlineTotal) : '';
            },
            railTitle(bucket) {
                const range = bucket.from === bucket.to ? `${bucket.from}` : `${bucket.from}–${bucket.to}`;
                const group = label(bucket.role === 'mixed' ? 'role.mixed' : `role.${bucket.role}`);
                const preview = String(bucket.preview || '').slice(0, 128);
                return label('rail_bucket').replace('{start}', range).replace('{count}', bucket.count).replace('{role}', group).replace('{preview}', preview);
            },
            navigate(direction) {
                this.follow = false; worker?.postMessage({type: 'navigate', direction});
            },
            followOutput() { this.follow = true; this.newOutput = false; this.row = Math.max(0, this.total - this.visibleRows); this.viewport(true); },
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
                if (stopped || this.authorizationLost || this.paging || !['active', 'recent'].includes(scope)
                    || (this.scopeFilter !== 'all' && scope !== this.scopeFilter)) return;
                this.filterError = this.rangeError();
                if (this.filterError) { this.paging = false; return; }
                const token = ++pageGeneration;
                pageController?.abort(); pageController = new AbortController();
                this.paging = true; this.pageScope = scope;
                const snapshot = {scope: this.scopeFilter, state: this.stateFilter, model: this.modelFilter, source: this.sourceFilter,
                    from: this.receivedFrom, until: this.receivedUntil, cursor: this.pageCursor};
                try {
                    const params = new URLSearchParams({scope, limit: 100});
                    if (this.stateFilter) params.set('state', this.stateFilter);
                    if (this.modelFilter) params.set('model_id', this.modelFilter);
                    if (this.sourceFilter) params.set('source_id', this.sourceFilter);
                    const since = this.localTimeSeconds(this.receivedFrom), until = this.localTimeSeconds(this.receivedUntil);
                    if (since !== null) params.set('since', since);
                    if (until !== null) params.set('until', until);
                    if (this.pageCursor) params.set('cursor', this.pageCursor);
                    const data = await this.read(`/requests?${params}`, pageController.signal);
                    if (token !== pageGeneration || stopped || this.authorizationLost || this.filterError || Object.entries(snapshot).some(([key, value]) => ({scope: this.scopeFilter, state: this.stateFilter, model: this.modelFilter, source: this.sourceFilter, from: this.receivedFrom, until: this.receivedUntil, cursor: this.pageCursor}[key] !== value))) return;
                    const shownIds = new Set([...this.active, ...this.recent].map(record => record.id));
                    this.older = data.requests.filter(record => !shownIds.has(record.id));
                    this.pageCursor = data.next_cursor || '';
                    this.syncObservationClock();
                } catch (error) { if (token === pageGeneration && error.name !== 'AbortError') this.error = error.message; }
                finally { if (token === pageGeneration) this.paging = false; }
            },
            exportIdentity() {
                return {requestId: this.selected, view: this.mode, invocationId: this.invocationId,
                    epoch: generation};
            },
            exportSelectionIsCurrent(context) {
                return !!context && !this.authorizationLost && !stopped && context.epoch === generation
                    && context.requestId === this.selected && context.view === this.mode
                    && context.invocationId === this.invocationId;
            },
            exportIsCurrent(context) {
                return this.exportContext?.requestToken === context?.requestToken && this.exportSelectionIsCurrent(context);
            },
            beginExport(extra = {}) {
                const requestToken = ++exportSequence;
                const context = {...this.exportIdentity(), ...extra, requestToken};
                this.exportContext = context; this.exporting = true; this.exportError = '';
                return context;
            },
            exportView(action = 'download') {
                if (!worker || !this.selected || stopped || this.authorizationLost) return;
                const context = this.beginExport();
                worker.postMessage({type: 'export', action, requestToken: context.requestToken});
            },
            exportSection(sectionKey, origin) {
                if (!worker || !this.selected || stopped || this.authorizationLost || typeof sectionKey !== 'string' || !sectionKey) return;
                if (!this.exportSelectionIsCurrent(origin)) { this.exportError = label('selection_changed'); return; }
                const context = this.beginExport({sectionKey});
                worker.postMessage({type: 'export-section', sectionKey, requestToken: context.requestToken});
            },
            async copyRequestInfo(kind) {
                if (!this.selected || stopped || this.authorizationLost || !['id', 'link'].includes(kind)) return;
                const context = this.beginExport(), id = context.requestId;
                const text = kind === 'id' ? id : (() => {
                    const url = new URL('/admin/dashboard', window.location.origin);
                    url.searchParams.set('tab', 'activity'); url.searchParams.set('request', id);
                    return url.href;
                })();
                await this.writeClipboardOrDownload(text, `activity-request-${kind}-${id}.txt`, context);
            },
            async writeClipboardOrDownload(text, filename, context) {
                if (!this.exportIsCurrent(context)) return;
                try {
                    await navigator.clipboard.writeText(text);
                    if (this.exportIsCurrent(context)) { this.exporting = false; this.exportContext = null; }
                    return;
                } catch (_) {
                    if (!this.exportIsCurrent(context)) return;
                    try { this.downloadBlob(new Blob([text], {type: 'text/plain;charset=utf-8'}), filename); }
                    catch (error) { if (this.exportIsCurrent(context)) this.exportError = error.message || label('copy_failed'); }
                    if (this.exportIsCurrent(context)) { this.exporting = false; this.exportContext = null; }
                }
            },
            downloadBlob(blob, filename) {
                const url = URL.createObjectURL(blob), link = document.createElement('a');
                link.href = url; link.download = filename; link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            },
            async saveExport(data) {
                const context = this.exportContext;
                if (!this.exportIsCurrent(context) || data.requestToken !== context.requestToken
                    || (data.action === 'copy-section' && data.sectionKey !== context.sectionKey)) return;
                if (data.type === 'export_error') {
                    this.exportError = label(data.reason === 'section_unavailable' ? 'section_unavailable' : 'copy_failed');
                    this.exporting = false; this.exportContext = null;
                    return;
                }
                if (data.action === 'copy' || data.action === 'copy-section') {
                    try {
                        const text = await data.blob.text();
                        if (!this.exportIsCurrent(context)) return;
                        await navigator.clipboard.writeText(text);
                        if (this.exportIsCurrent(context)) { this.exporting = false; this.exportContext = null; }
                        return;
                    } catch (_) {
                        if (!this.exportIsCurrent(context)) return;
                        try {
                            this.downloadBlob(data.blob, `activity-${context.requestId}${context.invocationId ? '-generation-' + context.invocationId : ''}${data.action === 'copy-section' ? '-section' : ''}.txt`);
                        } catch (error) { if (this.exportIsCurrent(context)) this.exportError = error.message || label('copy_failed'); }
                        if (this.exportIsCurrent(context)) { this.exporting = false; this.exportContext = null; }
                        return;
                    }
                }
                if (!this.exportIsCurrent(context)) return;
                try { this.downloadBlob(data.blob, `activity-${context.requestId}${context.invocationId ? '-generation-' + context.invocationId : ''}.txt`); }
                catch (error) { if (this.exportIsCurrent(context)) this.exportError = error.message || label('copy_failed'); }
                if (this.exportIsCurrent(context)) { this.exporting = false; this.exportContext = null; }
            },
            label, stateLabel(value) { return label(`state.${value}`); },
            bytes(value) { if (!value) return '0 B'; const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1024))); return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${['B', 'KiB', 'MiB', 'GiB'][unit]}`; },
            count(value) { return Number.isFinite(value) ? value.toLocaleString() : '—'; },
            milliseconds(value) { return Number.isFinite(value) ? `${Math.round(value).toLocaleString()} ms` : '—'; },
            time(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? new Date(value * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '—'; },
            duration(record) {
                const received = record?.received_at;
                if (typeof received !== 'number' || !Number.isFinite(received) || received < 0) return '—';
                if (record.finished_at !== null) {
                    if (typeof record.finished_at !== 'number' || !Number.isFinite(record.finished_at) || record.finished_at < 0) return '—';
                    return `${Math.max(0, record.finished_at - received).toFixed(1)} s`;
                }
                if (!this.observationEligible(record) || typeof this.observationNow !== 'number' || !Number.isFinite(this.observationNow)) return '—';
                return `${Math.max(0, this.observationNow / 1000 - received).toFixed(1)} s`;
            },
            observationEligible(record) {
                const id = record?.id;
                const loaded = typeof id === 'string' && id.length > 0 && id.length <= 128
                    && [this.detail, ...this.active, ...this.recent, ...this.older].some(item => item?.id === id);
                return !stopped && this.mainTab === 'activity' && !document.hidden && !this.authorizationLost
                    && loaded && record?.finished_at === null
                    && typeof record.received_at === 'number' && Number.isFinite(record.received_at) && record.received_at >= 0;
            },
            needsObservationClock() {
                return [...this.active, ...this.recent, ...this.older, this.detail].some(record => this.observationEligible(record));
            },
            stopObservationClock() {
                observationGeneration++;
                if (observationTimer !== null) clearInterval(observationTimer);
                observationTimer = null;
                this.observationNow = null;
            },
            syncObservationClock() {
                if (!this.needsObservationClock()) {
                    this.stopObservationClock();
                    return;
                }
                if (observationTimer !== null) return;
                const wallAtStart = Date.now(), monotonicAtStart = performance.now();
                this.observationNow = wallAtStart;
                const token = ++observationGeneration;
                observationTimer = setInterval(() => {
                    if (token !== observationGeneration) return;
                    if (!this.needsObservationClock()) {
                        this.stopObservationClock();
                        return;
                    }
                    this.observationNow = wallAtStart + Math.max(0, performance.now() - monotonicAtStart);
                }, 1000);
            },
            currentSection() { return this.current || this.outline[0]; },
        };
    }
    root.activityInspector = activityInspector;
    if (typeof module !== 'undefined') module.exports = {activityInspector};
})(typeof window !== 'undefined' ? window : globalThis);
