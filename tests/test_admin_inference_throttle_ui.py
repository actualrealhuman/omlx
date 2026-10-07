# SPDX-License-Identifier: Apache-2.0
"""Browserless coverage for the live inference-throttle controls."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_inference_throttle_controls_and_live_write_contract():
    settings = (ROOT / "omlx/admin/templates/dashboard/_settings.html").read_text()
    status = (ROOT / "omlx/admin/templates/dashboard/_status.html").read_text()
    control = (
        ROOT / "omlx/admin/templates/dashboard/_inference_throttle_control.html"
    ).read_text()
    dashboard = (ROOT / "omlx/admin/static/js/dashboard.js").read_text()

    assert settings.count("dashboard/_inference_throttle_control.html") == 1
    assert "('inference_throttle', '_inference_throttle.html')" in status
    assert 'min="10" max="100" step="5"' in control
    assert 'min="10" max="100" step="any"' in control
    assert "settings.inference_throttle.immediate" in settings
    assert (
        "inference_share: this.globalSettings.server.inference_share" not in dashboard
    )

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard behavior tests")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('omlx/admin/static/js/dashboard.js', 'utf8');
let firstResolve;
const requests = [];
const context = {
    localStorage: {getItem: () => null},
    THEME_STORAGE_KEY: 'theme', ENHANCED_READABILITY_KEY: 'readability',
    window: {t: key => ({
        'settings.inference_throttle.status_full': 'FULL THROTTLE • MAXIMUM POWER',
        'settings.inference_throttle.status_power': '{percent}% POWER',
        'settings.inference_throttle.saving': 'Applying…',
        'settings.inference_throttle.saved': 'Saved',
        'settings.inference_throttle.save_error': 'Could not save',
    }[key] || key)}, navigator: {language: 'en'}, document: {}, console,
    setTimeout: (callback) => ({callback}), clearTimeout: () => {},
    fetch: (url, options = {}) => {
        if (options.method !== 'POST') {
            return Promise.resolve({ok: true, json: async () => ({server: {inference_share: 0.6}})});
        }
        requests.push(JSON.parse(options.body));
        if (requests.length === 1) {
            return new Promise(resolve => { firstResolve = resolve; });
        }
        return Promise.resolve({ok: true});
    },
};
(async () => {
    const state = vm.runInNewContext(source + '\n dashboard;', context)();
    await state.loadGlobalSettings();
    assert.equal(state.inferenceThrottlePercent, 60);
    assert.equal(state.inferenceThrottleStatus(), '60% POWER');

    state.queueInferenceThrottleUpdate('72');
    assert.equal(state.inferenceThrottleSaveStatus(), 'Applying…');
    const firstWrite = state.flushInferenceThrottleUpdate();
    await Promise.resolve();
    const sequenceBeforeTyping = state._inferenceThrottleSequence;
    state.stageInferenceThrottleNumeric('87.5');
    assert.equal(state._inferenceThrottleSequence, sequenceBeforeTyping);
    const latestCommit = state.commitInferenceThrottleNumeric('87.5');
    firstResolve({ok: true});
    await firstWrite;
    await latestCommit;
    assert.deepEqual(requests, [
        {inference_share: 0.72},
        {inference_share: 0.875},
    ]);
    assert.equal(state.inferenceThrottlePercent, 87.5);
    assert.equal(state.globalSettings.server.inference_share, 0.875);
    assert.equal(state._inferenceThrottleSavedSequence, state._inferenceThrottleSequence);
    assert.equal(state.inferenceThrottleSaveStatus(), 'Saved');

    state.queueInferenceThrottleUpdate('100');
    assert.equal(state.inferenceThrottleStatus(), 'FULL THROTTLE • MAXIMUM POWER');
    await state.flushInferenceThrottleUpdate();
    assert.deepEqual(requests.at(-1), {inference_share: 1});

    state.commitInferenceThrottleNumeric('9');
    assert.equal(state.inferenceThrottleError, 'settings.inference_throttle.validation');
    assert.equal(state.inferenceThrottlePercent, 100);

    // Failed writes remain dirty, survive a stale GET, and retry on a new
    // committed numeric value instead of appearing saved.
    context.fetch = (url, options = {}) => options.method === 'POST'
        ? Promise.resolve({ok: false, status: 503})
        : Promise.resolve({ok: true, json: async () => ({server: {inference_share: 0.6}})});
    state.queueInferenceThrottleUpdate('85');
    await state.flushInferenceThrottleUpdate();
    const failedSequence = state._inferenceThrottleSequence;
    assert.ok(state._inferenceThrottleSavedSequence < failedSequence);
    assert.equal(state.inferenceThrottleSaveStatus(), '');
    assert.equal(state.inferenceThrottleError, 'Could not save');
    state.stageInferenceThrottleNumeric('85.5');
    await state.loadGlobalSettings();
    assert.equal(state.inferenceThrottlePercent, 85);
    assert.equal(state.inferenceThrottleError, 'Could not save');
    context.fetch = async () => ({ok: true});
    await state.commitInferenceThrottleNumeric('85.5');
    assert.equal(state.inferenceThrottlePercent, 85.5);
    assert.equal(state.inferenceThrottleError, '');
    assert.equal(state._inferenceThrottleSavedSequence, state._inferenceThrottleSequence);

    // Resetting the general settings draft must preserve the live throttle and
    // must not issue another throttle request.
    state.inferenceThrottlePercent = 87.5;
    state.globalSettings.server.inference_share = 0.4;
    const resetCalls = [];
    context.fetch = async (url, options = {}) => {
        resetCalls.push({url, method: options.method || 'GET'});
        return {ok: true, json: async () => ({
            server: {inference_share: 1}, model: {}, memory: {}, scheduler: {},
            cache: {}, sampling: {}, mcp: {}, usage: {}, huggingface: {},
            network: {}, auth: {}, idle_timeout: {}, ui: {language: 'en'},
        })};
    };
    await state.resetGlobalSettingsDefaults();
    assert.equal(state.globalSettings.server.inference_share, 0.875);
    state.cancelGlobalSettingsReset();
    assert.equal(state.globalSettings.server.inference_share, 0.875);
    assert.deepEqual(resetCalls, [{url: '/admin/api/global-settings/defaults', method: 'GET'}]);

    // A settings GET that began before a live edit must not replace its value.
    let resolveGet;
    context.fetch = (url, options = {}) => options.method === 'POST'
        ? Promise.resolve({ok: true})
        : new Promise(resolve => { resolveGet = resolve; });
    const loading = state.loadGlobalSettings();
    state.queueInferenceThrottleUpdate('83.25');
    resolveGet({ok: true, json: async () => ({server: {inference_share: 0.4}})});
    await loading;
    assert.equal(state.inferenceThrottlePercent, 83.25);
    assert.equal(state.globalSettings.server.inference_share, 0.8325);
    await state.flushInferenceThrottleUpdate();

    // An uncommitted numeric draft also survives a pending settings GET.
    let resolveDraftGet;
    let lastPost;
    context.fetch = (url, options = {}) => options.method === 'POST'
        ? (lastPost = JSON.parse(options.body), Promise.resolve({ok: true}))
        : new Promise(resolve => { resolveDraftGet = resolve; });
    const beforeDraftLoad = state.inferenceThrottlePercent;
    const draftLoad = state.loadGlobalSettings();
    state.stageInferenceThrottleNumeric('82.25');
    resolveDraftGet({ok: true, json: async () => ({server: {inference_share: 0.4}})});
    await draftLoad;
    assert.equal(state.inferenceThrottlePercent, beforeDraftLoad);
    assert.equal(state.inferenceThrottleNumericDraft, '82.25');
    await state.commitInferenceThrottleNumeric('82.25');
    assert.equal(state.inferenceThrottlePercent, 82.25);
    assert.deepEqual(lastPost, {inference_share: 0.8225});
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_general_global_save_does_not_overwrite_live_throttle():
    dashboard = (ROOT / "omlx/admin/static/js/dashboard.js").read_text()
    start = dashboard.index("async saveGlobalSettings()")
    end = dashboard.index("async ", start + len("async saveGlobalSettings()"))
    payload = dashboard[start:end]
    assert "inference_share:" not in payload
