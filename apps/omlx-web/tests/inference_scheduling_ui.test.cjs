// SPDX-License-Identifier: Apache-2.0
// Run with: node apps/omlx-web/tests/inference_scheduling_ui.test.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function makeApp() {
    let timerId = 0;
    const timers = new Map();
    const context = {
        localStorage: { getItem: () => null },
        window: { t: key => key },
        Event: class { constructor(type, options = {}) { this.type = type; this.bubbles = options.bubbles; } },
        console,
        setTimeout: (callback) => {
            const id = ++timerId;
            timers.set(id, callback);
            return id;
        },
        clearTimeout: id => timers.delete(id),
    };
    vm.createContext(context);
    const dashboardPath = path.resolve(__dirname, '../omlx_web/static/js/dashboard.js');
    vm.runInContext(fs.readFileSync(dashboardPath, 'utf8'), context);
    return { app: context.dashboard(), context, timers };
}

const response = data => ({ok: true, json: async () => data});
const policy = (share, priority = 0, exclusive = false) => ({
    inference_allowance: share,
    inference_priority: priority,
    inference_exclusive: exclusive,
});

(async () => {
    // The debounce keeps only the most recent slider value.
    {
        const {app, context, timers} = makeApp();
        let patchBodies = [];
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: true, models: [{
                model_id: 'ling', supported: true, allowance: 0.4,
                effective_share: 0.4, priority: 0, exclusive: false, state: 'idle',
            }]});
            patchBodies.push(JSON.parse(init.body));
            return response({settings: policy(0.5)});
        };
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        app.queueModelShareUpdate(row, 44);
        app.queueModelShareUpdate(row, 51);
        assert.equal(timers.size, 1);
        const [callback] = timers.values();
        await callback();
        await app.flushModelSchedulingUpdate(row);
        assert.deepEqual(patchBodies, [{inference_allowance: 0.51}]);
    }

    // New edits arriving during an in-flight save are serialized and merged
    // into the later write, so the last value for each field wins.
    {
        const {app, context} = makeApp();
        let holdWrites = true;
        const patchBodies = [];
        const pending = [];
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: true, models: [{
                model_id: 'ling', supported: true, allowance: 0.4,
                effective_share: 0.4, priority: 0, exclusive: false, state: 'idle',
            }]});
            patchBodies.push(JSON.parse(init.body));
            if (holdWrites) {
                return new Promise(resolve => pending.push(() => resolve(response({
                    settings: policy(0.55, 7),
                }))));
            }
            return response({settings: policy(0.55, 7)});
        };
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        app.queueModelShareUpdate(row, 55);
        const shareFlush = app.flushModelSchedulingUpdate(row);
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(patchBodies, [{inference_allowance: 0.55}]);

        const priorityFlush = app.saveModelSchedulingField(row, 'inference_priority', 7);
        pending.shift()();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(patchBodies, [
            {inference_allowance: 0.55},
            {inference_allowance: 0.55, inference_priority: 7},
        ]);
        holdWrites = false;
        pending.shift()();
        await Promise.all([shareFlush, priorityFlush]);
    }

    // Each loaded model has an independent write queue and policy payload.
    {
        const {app, context} = makeApp();
        const patches = [];
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: true, models: ['ling', 'other'].map(model_id => ({
                model_id, supported: true, allowance: 0.4,
                effective_share: 0.4, priority: 0, exclusive: false, state: 'idle',
            }))});
            patches.push({url, body: JSON.parse(init.body)});
            return response({settings: policy(JSON.parse(init.body).inference_allowance)});
        };
        await app.loadInferenceScheduling();
        const [ling, other] = app.inferenceScheduling.models;
        app.queueModelShareUpdate(ling, 35);
        app.queueModelShareUpdate(other, 85);
        await Promise.all([
            app.flushModelSchedulingUpdate(ling),
            app.flushModelSchedulingUpdate(other),
        ]);
        assert.deepEqual(patches.map(item => [item.url, item.body]), [
            ['/admin/api/models/ling/scheduling', {inference_allowance: 0.35}],
            ['/admin/api/models/other/scheduling', {inference_allowance: 0.85}],
        ]);
    }

    // A periodic status refresh preserves the in-progress slider draft.
    {
        const {app, context} = makeApp();
        context.fetch = async () => response({enabled: true, models: [{
            model_id: 'ling', supported: true, allowance: 0.4,
            effective_share: 0.4, priority: 0, exclusive: false, state: 'idle',
        }]});
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        app.queueModelShareUpdate(row, 73);
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceScheduling.models[0].draftPercent, 73);
    }

    // Polling mutates keyed row objects in place, retaining numeric input
    // text and focus-associated drafts rather than replacing the row.
    {
        const {app, context} = makeApp();
        context.fetch = async () => response({enabled: true, models: [{
            model_id: 'ling', supported: true, allowance: 0.4,
            effective_share: 0.4, priority: 0, exclusive: false, state: 'idle',
        }]});
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        row.numericDraft = '61.5';
        await app.loadInferenceScheduling();
        assert.strictEqual(app.inferenceScheduling.models[0], row);
        assert.equal(row.numericDraft, '61.5');
    }

    // Fractional and very small server allowances survive load, save, reload,
    // and ceiling composition without rounding to an integer or zero.
    {
        const {app, context} = makeApp();
        app.globalSettings.server.inference_share = 0.8;
        app.globalSettings.server.inference_interactive_allowance = 0.75;
        app.globalSettings.server.inference_background_allowance = 0.5;
        const rows = [
            {model_id: 'fractional', allowance: 0.755, exclusive: true},
            {model_id: 'tiny', allowance: 0.0005, exclusive: false},
        ];
        const patches = [];
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: true, models: rows.map(row => ({
                ...row, supported: true, priority: 0, state: 'idle',
            }))});
            const id = decodeURIComponent(url.split('/').at(-2));
            const patch = JSON.parse(init.body);
            patches.push({id, ...patch});
            rows.find(row => row.model_id === id).allowance = patch.inference_allowance;
            return response({settings: policy(patch.inference_allowance)});
        };
        await app.loadInferenceScheduling();
        const [fractional, tiny] = app.inferenceScheduling.models;
        assert.equal(fractional.draftPercent, 75.5);
        assert.equal(tiny.draftPercent, 0.05);
        assert.equal(app.modelSchedulingConfiguredShare(fractional), 0.453);
        assert.equal(app.modelSchedulingConfiguredShare(tiny), 0.0002);
        assert.equal(app.formatSchedulingPercent(fractional.draftPercent), '75.5');
        assert.equal(app.formatSchedulingPercent(app.modelSchedulingConfiguredShare(tiny) * 100), '0.02');
        assert.equal(app.formatSchedulingPercent(1e-10), '1.00e-10');
        assert.equal(app.formatSchedulingPercent(0), '0');

        await app.commitModelShareNumeric(fractional, '75.5');
        await app.commitModelShareNumeric(tiny, '0.05');
        assert.deepEqual(patches, [
            {id: 'fractional', inference_allowance: 0.755},
            {id: 'tiny', inference_allowance: 0.0005},
        ]);
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceScheduling.models[0].draftPercent, 75.5);
        assert.equal(app.inferenceScheduling.models[1].draftPercent, 0.05);

        const template = fs.readFileSync(
            path.resolve(__dirname, '../omlx_web/templates/dashboard/blocks/_inference_throttle.html'),
            'utf8',
        );
        assert.match(template, /:value="row\.numericDraft \?\? row\.draftPercent"/);
        assert.match(template, /@keydown\.enter\.prevent="commitModelShareNumeric\(row, \$event\.target\.value\); \$el\.blur\(\)"/);
        assert.match(template, /@blur="if \(row\.numericDraft !== undefined\) commitModelShareNumeric\(row, row\.numericDraft\)"/);
        assert.match(template, /min="0" max="100" step="any"/);
    }

    // Global changes coalesce while one POST is in flight, and a failed save
    // remains visible and can be retried through the same save action.
    {
        const {app, context} = makeApp();
        const bodies = [];
        const pending = [];
        let failNext = false;
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: false, models: []});
            bodies.push(JSON.parse(init.body));
            if (failNext) {
                failNext = false;
                return {ok: false};
            }
            if (pending.length < 2 && bodies.length <= 2) {
                return new Promise(resolve => pending.push(() => resolve(response({}))));
            }
            return response({});
        };
        app.globalSettings.server.inference_scheduling_enabled = true;
        const first = app.saveInferenceSchedulingConfig();
        await new Promise(resolve => setImmediate(resolve));
        app.globalSettings.server.inference_scheduling_quantum_seconds = 7;
        const second = app.saveInferenceSchedulingConfig();
        pending.shift()();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(bodies, [
            {inference_scheduling_enabled: true, inference_scheduling_quantum_seconds: 5, inference_interactive_allowance: 1, inference_background_allowance: 1},
            {inference_scheduling_enabled: true, inference_scheduling_quantum_seconds: 7, inference_interactive_allowance: 1, inference_background_allowance: 1},
        ]);
        pending.shift()();
        await Promise.all([first, second]);

        failNext = true;
        await app.saveInferenceSchedulingConfig();
        assert.equal(app.inferenceSchedulingError, 'settings.inference_scheduling.save_error');
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceSchedulingError, 'settings.inference_scheduling.save_error');
        await app.saveInferenceSchedulingConfig();
        assert.equal(app.inferenceSchedulingError, '');
    }

    // A recovered status poll clears only its own load error, not a failed
    // global-settings save or the retry action associated with that save.
    {
        const {app, context} = makeApp();
        let failGet = true;
        let failSave = true;
        context.fetch = async (url, init) => {
            if (init) return failSave ? {ok: false} : response({});
            if (failGet) throw Error('offline');
            return response({enabled: false, models: []});
        };
        await app.saveInferenceSchedulingConfig();
        assert.equal(app.inferenceSchedulingError, 'settings.inference_scheduling.save_error');
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceSchedulingLoadError, 'settings.inference_scheduling.load_error');
        failGet = false;
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceSchedulingLoadError, '');
        assert.equal(app.inferenceSchedulingError, 'settings.inference_scheduling.save_error');
        failSave = false;
        await app.saveInferenceSchedulingConfig();
        assert.equal(app.inferenceSchedulingError, '');
    }

    // Failed model writes remain visibly failed (not saving) through polls,
    // retain the draft, and can be retried from the row's accessible action.
    {
        const {app, context} = makeApp();
        let failSave = true;
        const status = {enabled: true, models: [{
            model_id: 'ling', display_name: 'Ling', supported: true,
            allowance: 0.4, configured_share: 0.4, effective_share: 0.4, priority: 0,
            exclusive: false, state: 'idle',
        }]};
        context.fetch = async (url, init) => {
            if (!init) return response(status);
            if (failSave) return {ok: false, json: async () => ({detail: 'write failed'})};
            status.models[0].allowance = JSON.parse(init.body).inference_allowance;
            return response({settings: policy(status.models[0].allowance)});
        };
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        app.queueModelShareUpdate(row, 63);
        await app.flushModelSchedulingUpdate(row);
        assert.equal(row.error, 'settings.inference_scheduling.save_error');
        assert.equal(row.saving, false);
        await app.loadInferenceScheduling();
        assert.strictEqual(app.inferenceScheduling.models[0], row);
        assert.equal(row.draftPercent, 63);
        assert.equal(row.error, 'settings.inference_scheduling.save_error');
        assert.equal(row.saving, false);

        const rowsTemplate = fs.readFileSync(
            path.resolve(__dirname, '../omlx_web/templates/dashboard/blocks/_inference_throttle.html'),
            'utf8',
        );
        assert.match(rowsTemplate, /@click="retryModelSchedulingUpdate\(row\)"/);
        assert.match(rowsTemplate, /x-show="row\.error && modelSchedulingCanRetry\(row\)"/);
        assert.match(rowsTemplate, /:aria-label="window\.t\('settings\.inference_scheduling\.retry'\)/);
        failSave = false;
        await app.retryModelSchedulingUpdate(row);
        assert.equal(row.error, '');
        assert.equal(row.saving, false);
        assert.equal(row.allowance, 0.63);
    }

    // Validation errors without an unsaved PATCH never expose Retry or leave
    // the row stuck in a saving state.
    {
        const {app, context} = makeApp();
        let fetchCalls = 0;
        context.fetch = async () => { fetchCalls += 1; return response({}); };
        const row = {model_id: 'ling', supported: true, saving: false};
        app.queueModelShareUpdate(row, 101);
        assert.equal(row.error, 'settings.inference_throttle.validation');
        assert.equal(app.modelSchedulingCanRetry(row), false);
        await app.retryModelSchedulingUpdate(row);
        assert.equal(row.saving, false);
        assert.equal(fetchCalls, 0);

        app._modelSchedulingWrites.ling = {
            sequence: 1, saved: 1, timer: null, writePromise: null, pending: {},
        };
        await app.retryModelSchedulingUpdate(row);
        assert.equal(row.saving, false);
        assert.equal(fetchCalls, 0);
    }

    // Resetting a custom allowance writes null and returns the slider to 100%
    // of the selected class.
    {
        const {app, context} = makeApp();
        app.globalSettings.server.inference_share = 0.8;
        let savedShare = 0.4;
        let patchBody;
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: true, models: [{
                model_id: 'ling', supported: true, allowance: savedShare ?? 1,
                effective_share: 0.4, priority: 0, exclusive: false, state: 'idle',
            }]});
            patchBody = JSON.parse(init.body);
            savedShare = patchBody.inference_allowance ?? 1;
            return response({settings: policy(savedShare)});
        };
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        await app.saveModelSchedulingField(row, 'inference_allowance', null);
        assert.deepEqual(patchBody, {inference_allowance: null});
        assert.equal(row.allowance, 1);
        assert.equal(row.draftPercent, 100);
    }

    // Class allowances retain the latest values through serialized writes and
    // report the composed configured ceiling from the global limit.
    {
        const {app, context} = makeApp();
        app.globalSettings.server.inference_share = 0.8;
        app.globalSettings.server.inference_interactive_allowance = 0.75;
        app.globalSettings.server.inference_background_allowance = 0.5;
        assert.equal(app.classAllowanceValueText('interactive'), '75% of global; 60% configured ceiling');
        const bodies = [];
        const pending = [];
        context.fetch = async (_url, init) => {
            bodies.push(JSON.parse(init.body));
            return new Promise(resolve => pending.push(() => resolve(response({}))));
        };
        app.queueClassAllowanceUpdate('inference_interactive_allowance', 60);
        const first = app.flushClassAllowanceUpdate();
        await new Promise(resolve => setImmediate(resolve));
        app.queueClassAllowanceUpdate('inference_background_allowance', 25);
        const second = app.flushClassAllowanceUpdate();
        pending.shift()();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(bodies, [
            {inference_interactive_allowance: 0.6, inference_background_allowance: 0.5},
            {inference_interactive_allowance: 0.6, inference_background_allowance: 0.25},
        ]);
        pending.shift()();
        await Promise.all([first, second]);
    }

    // Class and global-share writes share one serialized POST queue so that
    // separate controls cannot race one another at the settings endpoint.
    {
        const {app, context} = makeApp();
        const bodies = [];
        const pending = [];
        context.fetch = async (_url, init) => {
            bodies.push(JSON.parse(init.body));
            return new Promise(resolve => pending.push(() => resolve(response({}))));
        };
        app.queueClassAllowanceUpdate('inference_interactive_allowance', 70);
        const classFlush = app.flushClassAllowanceUpdate();
        await new Promise(resolve => setImmediate(resolve));
        app.queueInferenceThrottleUpdate(60);
        const globalFlush = app.flushInferenceThrottleUpdate();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(bodies, [{inference_interactive_allowance: 0.7, inference_background_allowance: 1}]);
        pending.shift()();
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(bodies, [
            {inference_interactive_allowance: 0.7, inference_background_allowance: 1},
            {inference_share: 0.6},
        ]);
        pending.shift()();
        await Promise.all([classFlush, globalFlush]);
    }

    // The full 52px tray maps track taps and horizontal drags into the native
    // range, emits only local previews during movement, and flushes on release.
    {
        const {app, context} = makeApp();
        const sent = [];
        const status = {models: [{model_id: 'ling', supported: true, loaded: true, allowance: 0.5, state: 'idle'}]};
        context.fetch = async (_url, init) => {
            if (!init) return response(status);
            const patch = JSON.parse(init.body);
            sent.push(patch);
            status.models[0].allowance = patch.inference_allowance;
            return response({settings: policy(patch.inference_allowance)});
        };
        await app.loadInferenceScheduling();
        const row = app.inferenceScheduling.models[0];
        const inputEvents = new Map();
        context.document = {addEventListener: (name, handler) => inputEvents.set(name, handler)};
        app.bindInferenceAllowancePointerControls();
        const classes = new Set();
        const track = {
            classList: {add: value => classes.add(value), remove: value => classes.delete(value)},
            querySelector: () => input,
            setPointerCapture() {}, hasPointerCapture: () => false,
        };
        const input = {
            min: '0', max: '100', value: '50', disabled: false,
            getBoundingClientRect: () => ({left: 6, width: 288}),
            focus() { row.sliderEditing = true; },
            blur() { row.sliderEditing = false; },
            dispatchEvent(event) {
                if (event.type === 'input') app.queueModelShareUpdate(row, this.value);
                if (event.type === 'change') app.flushModelSchedulingUpdate(row);
            },
        };
        const pointer = (name, x, y = 10, pointerType = 'mouse') => inputEvents.get(name)({
            target: {closest: () => track}, isPrimary: true, button: 0, pointerId: 4,
            pointerType, clientX: x, clientY: y, cancelable: true,
            preventDefault() {},
        });
        pointer('pointerdown', 204);
        pointer('pointermove', 220);
        assert.equal(row.draftPercent, 77);
        assert.deepEqual(sent, []);
        pointer('pointerup', 220);
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(sent, [{inference_allowance: 0.77}]);
        assert.equal(row.saving, false);

        // A cancel event with bogus coordinates keeps the last horizontal
        // preview and sends that value exactly once; it never maps (0, 0).
        pointer('pointerdown', 221);
        pointer('pointermove', 240);
        const cancelledValue = Number(input.value) / 100;
        pointer('pointercancel', 0, 0);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(Number(row.draftPercent) / 100, cancelledValue);
        assert.deepEqual(sent, [{inference_allowance: 0.77}, {inference_allowance: cancelledValue}]);

        pointer('pointerdown', 240);
        pointer('pointermove', 250);
        const lostCaptureValue = Number(input.value) / 100;
        inputEvents.get('lostpointercapture')({pointerId: 4, clientX: 0, clientY: 0});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(Number(row.draftPercent) / 100, lostCaptureValue);
        assert.deepEqual(sent, [
            {inference_allowance: 0.77},
            {inference_allowance: cancelledValue},
            {inference_allowance: lostCaptureValue},
        ]);
    }

    // Unloaded policies remain editable; only explicit unsupported engine
    // capability should disable scheduling controls.
    {
        const {app} = makeApp();
        assert.equal(app.modelSchedulingEditable({supported: false, loaded: false, state: 'idle'}), true);
        assert.equal(app.modelSchedulingEditable({supported: false, loaded: true, state: 'unsupported'}), false);
    }

    // The global 1..100 range maps its inset thumb centers to the visual 18px
    // endpoint marks without treating 1% as zero.
    {
        const {app, context} = makeApp();
        const listeners = new Map();
        context.document = {addEventListener: (name, handler) => listeners.set(name, handler)};
        context.fetch = async () => response({});
        app.bindInferenceAllowancePointerControls();
        const track = {
            classList: {add() {}, remove() {}}, querySelector: () => input,
            setPointerCapture() {}, hasPointerCapture: () => false,
        };
        const input = {
            min: '1', max: '100', value: '1', disabled: false,
            getBoundingClientRect: () => ({left: 6, width: 288}),
            focus() {}, blur() {},
            dispatchEvent(event) {
                if (event.type === 'input') app.queueInferenceThrottleUpdate(this.value);
                if (event.type === 'change') app.flushInferenceThrottleUpdate();
            },
        };
        const event = x => ({target: {closest: () => track}, isPrimary: true, button: 0,
            pointerId: 8, pointerType: 'mouse', clientX: x, clientY: 0, cancelable: true, preventDefault() {}});
        listeners.get('pointerdown')(event(18));
        assert.equal(input.value, '1');
        listeners.get('pointermove')(event(282));
        assert.equal(input.value, '100');
        listeners.get('pointerup')(event(282));
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(app.globalSettings.server.inference_share, 1);
    }

    // The production control exposes a global ceiling, both relative class
    // controls, and a searchable catalog-backed model list.
    {
        const controls = fs.readFileSync(
            path.resolve(__dirname, '../omlx_web/templates/dashboard/_inference_throttle_control.html'),
            'utf8',
        );
        const rows = fs.readFileSync(
            path.resolve(__dirname, '../omlx_web/templates/dashboard/blocks/_inference_throttle.html'),
            'utf8',
        );
        const styles = fs.readFileSync(path.resolve(__dirname, '../omlx_web/static/css/dashboard.css'), 'utf8');
        assert.match(controls, /inference-allowance-tray/);
        assert.match(styles, /\.inference-allowance-tray\s*\{[^}]*height:52px/);
        assert.match(styles, /\.allowance-plot\s*\{[^}]*background:var\(--bg-secondary\)/);
        assert.match(styles, /\.inference-allowance-range\s*\{[^}]*touch-action:pan-y/);
        assert.match(styles, /\[data-theme="dark"\] \.inference-allowance-tray input\.inference-allowance-range\s*\{[^}]*background:transparent!important/);
        assert.match(rows, /inference_interactive_allowance/);
        assert.match(rows, /inference_background_allowance/);
        assert.match(rows, /classAbsoluteCeiling/);
        assert.match(rows, /allowance-shell/);
        assert.match(rows, /x-for="kind in \['interactive', 'background'\]"/);
        assert.match(rows, /schedulingClassRows\(kind\)\.filter/);
        assert.match(rows, /row\.exclusive \? '🧑' : '🤖'/);
        assert.match(rows, /inference-icon-button/);
        assert.match(styles, /\.inference-icon-button\s*\{[^}]*width:44px;[^}]*min-height:44px/);
        assert.match(rows, /🤖/);
        assert.match(rows, /🧑/);
        assert.match(rows, /inference_allowance/);
        assert.match(rows, /schedulingClassRows\(kind\)\.filter\(candidate => schedulingModelVisible\(candidate\)\)/);
        assert.match(rows, /moveModelSchedulingPriority\(row, -1\)/);
        assert.match(rows, /moveModelSchedulingPriority\(row, 1\)/);
    }

    // Equal-priority neighbors receive bounded rank values that swap their
    // order without producing values above the supported 0..100 range.
    {
        const {app, context} = makeApp();
        const patches = [];
        context.fetch = async (url, init) => {
            if (!init) return response({enabled: true, models: []});
            const body = JSON.parse(init.body);
            patches.push([url, body]);
            return response({settings: policy(1, body.inference_priority ?? 0, body.inference_exclusive ?? false)});
        };
        const first = {model_id: 'first', priority: 50, exclusive: true, allowance: 1, loaded: true};
        const second = {model_id: 'second', priority: 50, exclusive: true, allowance: 1, loaded: true};
        app.inferenceScheduling.models = [first, second];
        await app.moveModelSchedulingPriority(first, 1);
        assert.deepEqual(patches.map(([, body]) => body.inference_priority), [99, 100]);
        assert.ok(patches.every(([, body]) => body.inference_priority >= 0 && body.inference_priority <= 100));
    }

    // The nested plot keeps class and model ceilings on one absolute scale.
    {
        const {app} = makeApp();
        app.globalSettings.server.inference_share = 0.8;
        app.globalSettings.server.inference_interactive_allowance = 0.5;
        app.globalSettings.server.inference_background_allowance = 0.25;
        assert.equal(app.inferenceGlobalCeiling(), 80);
        assert.equal(app.classAbsoluteCeiling('interactive'), 40);
        assert.equal(app.classAbsoluteCeiling('background'), 20);
    }

    // A deliberate hide survives refresh and a fresh browser app instance,
    // including while the model remains loaded.
    {
        const values = new Map();
        const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
        const {app, context} = makeApp();
        context.localStorage = storage;
        context.fetch = async () => response({enabled: true, models: [
            {model_id: 'loaded', display_name: 'Loaded', loaded: true, state: 'active'},
            {model_id: 'other', display_name: 'Other', loaded: false, is_favorite: true, customized: true},
        ]});
        await app.loadInferenceScheduling();
        const loaded = app.inferenceScheduling.models[0];
        app.hideSchedulingModel(loaded);
        assert.equal(app.schedulingPanelRows().length, 0);
        const reloaded = makeApp();
        reloaded.context.localStorage = storage;
        reloaded.context.fetch = context.fetch;
        await reloaded.app.loadInferenceScheduling();
        assert.equal(reloaded.app.schedulingPanelRows().length, 0);
        assert.deepEqual(Array.from(reloaded.app.schedulingCatalogRows(), row => row.model_id), ['loaded', 'other']);
    }

    // Previously loaded models stay visible after unload; favorites and
    // customized policies do not opt themselves into the panel.
    {
        const values = new Map();
        const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
        const {app, context} = makeApp();
        context.localStorage = storage;
        let loaded = true;
        context.fetch = async () => response({enabled: true, models: [
            {model_id: 'seen', loaded, state: loaded ? 'active' : 'idle'},
            {model_id: 'favorite-custom', loaded: false, is_favorite: true, customized: true, allowance: 0.2},
        ]});
        await app.loadInferenceScheduling();
        loaded = false;
        await app.loadInferenceScheduling();
        assert.deepEqual(Array.from(app.schedulingPanelRows(), row => row.model_id), ['seen']);
        app.inferenceSchedulingSearch = 'favorite';
        assert.deepEqual(Array.from(app.schedulingPanelRows(), row => row.model_id), ['seen']);
        assert.deepEqual(Array.from(app.schedulingCatalogRows(), row => row.model_id), ['favorite-custom']);
        app.setSchedulingModelSelected(app.inferenceScheduling.models[1], true);
        assert.deepEqual(Array.from(app.schedulingPanelRows(), row => row.model_id), ['favorite-custom', 'seen']);
    }

    // Old deliberate pins migrate once as explicit includes, and ceiling
    // labels round for display while numeric class drafts retain precision.
    {
        const values = new Map([['omlx_inference_scheduling_pins', JSON.stringify(['pinned'])]]);
        const storage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
        const {app, context} = makeApp();
        context.localStorage = storage;
        app.loadSchedulingSelection();
        app.inferenceScheduling.models = [
            {model_id: 'pinned', loaded: false},
            {model_id: 'custom', loaded: false, customized: true, is_favorite: true},
        ];
        assert.deepEqual(Array.from(app.schedulingPanelRows(), row => row.model_id), ['pinned']);
        assert.equal(app.formatSchedulingCeiling(75.555), '75.6');
        assert.equal(app.formatSchedulingCeiling(0.05), '<0.1');
        assert.equal(app.formatSchedulingCeiling(0), '0');
        let classFlushes = 0;
        app.flushClassAllowanceUpdate = async () => { classFlushes += 1; };
        app.stageClassAllowanceNumeric('background', '75.55');
        assert.equal(app.classAllowanceNumericDraft.background, '75.55');
        assert.equal(app.commitClassAllowanceNumeric('background', '75.55'), true);
        assert.equal(app.globalSettings.server.inference_background_allowance, 0.7555);
        assert.equal(classFlushes, 1);
        app.stageClassAllowanceNumeric('background', 'invalid');
        app.queueClassAllowanceUpdate('inference_background_allowance', 50);
        assert.equal(app.classAllowanceNumericDraft.background, undefined);
    }

    // Clearing a numeric input must not silently pause a running model.
    {
        const {app, context} = makeApp();
        let writes = 0;
        context.fetch = async () => { writes += 1; return response({}); };
        const row = {model_id: 'prose', draftPercent: 75, numericDraft: '', error: ''};
        await app.commitModelShareNumeric(row, '');
        assert.equal(row.draftPercent, 75);
        assert.equal(writes, 0);
        assert.ok(row.error);
        app.queueModelShareUpdate(row, 50);
        assert.equal(row.numericDraft, undefined);
        assert.equal(row.draftPercent, 50);
        clearTimeout(app._modelSchedulingWrites.prose.timer);
    }

    // Global cap markers follow the native range's min..max thumb geometry,
    // and model configured values track parent and model drafts immediately.
    {
        const {app} = makeApp();
        app.globalSettings.server.inference_share = 0.8;
        app.globalSettings.server.inference_background_allowance = 0.5;
        const row = {exclusive: false, allowance: 0.6, draftPercent: 60,
            configured_share: 0.24, effective_share: 0.12, state: 'active'};
        assert.ok(Math.abs(app.inferenceGlobalThumbPercent() - (79 / 99 * 100)) < 1e-9);
        assert.equal(app.modelSchedulingConfiguredShare(row), 0.24);
        assert.equal(app.modelSchedulingShowsEffectiveShare(row), true);
        app.globalSettings.server.inference_share = 0.5;
        assert.equal(app.modelSchedulingConfiguredShare(row), 0.15);
        assert.equal(app.modelSchedulingShowsEffectiveShare(row), false);
        row.state = 'battery_suspended';
        assert.equal(app.modelSchedulingShowsEffectiveShare(row), true);
        row.state = 'active';
        row.draftPercent = 0;
        assert.equal(app.modelSchedulingConfiguredShare(row), 0);
    }

    // Bypass delegates to an available host and preserves saved policy values.
    {
        const {app} = makeApp();
        app.globalSettings.server.inference_share = 0.8;
        app.globalSettings.server.inference_interactive_allowance = 0.75;
        app.globalSettings.server.inference_scheduling_enabled = true;
        app.inferenceScheduling.models = [{model_id: 'prose', allowance: 0, priority: 80, exclusive: true}];
        const serverBefore = JSON.stringify(app.globalSettings.server);
        const modelsBefore = JSON.stringify(app.inferenceScheduling.models);
        const calls = [];
        app.inferenceThrottleControl = {enabled: true, async setEnabled(value) { calls.push(value); this.enabled = value; }};
        await app.toggleInferenceControls();
        assert.equal(app.inferenceControlsAreEnabled(), false);
        await app.toggleInferenceControls();
        assert.equal(app.inferenceControlsAreEnabled(), true);
        assert.deepEqual(calls, [false, true]);
        assert.equal(JSON.stringify(app.globalSettings.server), serverBefore);
        assert.equal(JSON.stringify(app.inferenceScheduling.models), modelsBefore);
    }

    // The real adapter waits for persistence, changes only the master flag,
    // and ignores an older settings read that finishes after the toggle.
    {
        const {app, context} = makeApp();
        let saved = true;
        let acknowledge;
        const writes = [];
        context.fetch = async (url, init) => {
            if (init) {
                writes.push(JSON.parse(init.body));
                await new Promise(resolve => { acknowledge = resolve; });
                saved = writes.at(-1).inference_controls_enabled;
                return response({status: 'success'});
            }
            return response({controls_enabled: saved, enabled: false, models: []});
        };
        app.globalSettings.server.inference_share = 0.8;
        app.globalSettings.server.inference_scheduling_enabled = true;
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceThrottleControl.source, 'server');
        const toggle = app.toggleInferenceControls();
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(app.inferenceControlsAreEnabled(), true);
        assert.equal(app.inferenceControlsSaving, true);
        assert.deepEqual(writes, [{inference_controls_enabled: false}]);
        app.syncInferenceControls(true, 0);
        acknowledge();
        await toggle;
        assert.equal(app.inferenceControlsAreEnabled(), false);
        assert.equal(app.globalSettings.server.inference_share, 0.8);
        assert.equal(app.globalSettings.server.inference_scheduling_enabled, true);
        app.syncInferenceControls(true, 0);
        assert.equal(app.inferenceControlsAreEnabled(), false);
        const reload = makeApp();
        reload.context.fetch = context.fetch;
        await reload.app.loadInferenceScheduling();
        assert.equal(reload.app.inferenceControlsAreEnabled(), false);
        assert.equal(app.modelSchedulingShowsEffectiveShare({configured_share: 0, effective_share: 0.3}), true);

        context.fetch = async () => ({ok: false, status: 503});
        await app.toggleInferenceControls();
        assert.equal(app.inferenceControlsAreEnabled(), false);
        assert.equal(app.inferenceControlsError, 'settings.inference_scheduling.master_error');
        assert.equal(app.inferenceControlsSaving, false);
    }

    // A host without the persisted capability cannot expose a visual-only switch.
    {
        const {app, context} = makeApp();
        context.fetch = async () => response({enabled: false, models: []});
        await app.loadInferenceScheduling();
        assert.equal(app.inferenceThrottleControl, null);
    }

    console.log('PASS: allowance debounce, save/load errors, retries, draft preservation, inheritance, class composition, chart controls, and persisted master bypass');
})().catch(error => { console.error(error); process.exitCode = 1; });
