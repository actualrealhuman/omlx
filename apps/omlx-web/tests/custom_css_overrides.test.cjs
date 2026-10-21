// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(
    path.join(__dirname, '../omlx_web/static/js/custom_css_overrides.js'),
    'utf8',
);

function createHarness({ search = '', values = {}, rootAttributes = {} } = {}) {
    const attrs = new Map(Object.entries(rootAttributes));
    const style = { textContent: '' };
    const events = [];
    const saved = new Map(Object.entries(values));

    const localStorage = {
        getItem(key) { return saved.has(key) ? saved.get(key) : null; },
        setItem(key, value) { saved.set(key, String(value)); },
        removeItem(key) { saved.delete(key); },
    };
    const documentElement = {
        getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
        setAttribute(name, value) { attrs.set(name, String(value)); },
        removeAttribute(name) { attrs.delete(name); },
    };
    const document = {
        documentElement,
        readyState: 'loading',
        addEventListener() {},
        getElementById(id) { return id === 'omlx-custom-css' ? style : null; },
    };
    const window = {
        document,
        localStorage,
        location: { search },
        dispatchEvent(event) { events.push(event); },
    };
    const context = {
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init.detail; }
        },
        TextEncoder,
        URLSearchParams,
        document,
        window,
    };
    vm.runInNewContext(SOURCE, context, { filename: 'custom_css_overrides.js' });
    return { api: window.omlxStyleOverrides, attrs, events, saved, style };
}

test('default state leaves the upstream appearance unchanged', () => {
    const harness = createHarness();
    assert.equal(harness.attrs.has('data-style-preset'), false);
    assert.equal(harness.attrs.has('data-custom-css-safe-mode'), false);
    assert.equal(harness.style.textContent, '');
    assert.equal(
        JSON.stringify(harness.api.read()),
        JSON.stringify({ preset: 'default', enabled: false, css: '' }),
    );
});

test('an adopter can configure the initial preset without changing saved choices', () => {
    const configured = createHarness({
        rootAttributes: { 'data-default-style-preset': 'true-black' },
    });
    assert.equal(configured.api.read().preset, 'true-black');
    assert.equal(configured.attrs.get('data-style-preset'), 'true-black');

    const savedChoice = createHarness({
        rootAttributes: { 'data-default-style-preset': 'true-black' },
        values: { 'omlx-style-preset': 'default' },
    });
    assert.equal(savedChoice.api.read().preset, 'default');
    assert.equal(savedChoice.attrs.has('data-style-preset'), false);
});

test('restores the True Black preset and custom CSS before body rendering', () => {
    const css = 'body { letter-spacing: 0.01em; }';
    const harness = createHarness({
        values: {
            'omlx-style-preset': 'true-black',
            'omlx-custom-css-enabled': 'on',
            'omlx-custom-css': css,
        },
    });
    assert.equal(harness.attrs.get('data-style-preset'), 'true-black');
    assert.equal(harness.style.textContent, css);
    assert.equal(harness.events.at(-1).type, 'omlx-style-overrides-changed');
    assert.equal(harness.events.at(-1).detail.customCssEnabled, true);
});

test('safe-mode query suppresses saved preset and CSS without deleting them', () => {
    const values = {
        'omlx-style-preset': 'true-black',
        'omlx-custom-css-enabled': 'on',
        'omlx-custom-css': 'body { display: none; }',
    };
    const harness = createHarness({ search: '?custom_css=off', values });
    assert.equal(harness.api.isSafeMode(), true);
    assert.equal(harness.attrs.has('data-style-preset'), false);
    assert.equal(harness.attrs.has('data-custom-css-safe-mode'), true);
    assert.equal(harness.style.textContent, '');
    assert.equal(harness.saved.get('omlx-custom-css'), values['omlx-custom-css']);
});

test('unknown preset values fail closed to the app default', () => {
    const harness = createHarness({ values: { 'omlx-style-preset': 'remote-theme' } });
    assert.equal(harness.api.read().preset, 'default');
    assert.equal(harness.attrs.has('data-style-preset'), false);
});

test('oversized stored CSS remains saved but is not applied', () => {
    const css = 'x'.repeat((64 * 1024) + 1);
    const harness = createHarness({
        values: {
            'omlx-custom-css-enabled': 'on',
            'omlx-custom-css': css,
        },
    });
    assert.equal(harness.api.read().enabled, false);
    assert.equal(harness.style.textContent, '');
    assert.equal(harness.saved.get('omlx-custom-css'), css);
});
