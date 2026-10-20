# SPDX-License-Identifier: Apache-2.0
"""Browserless regression coverage for configurable battery protection UI."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_battery_protection_settings_section_and_status_contract():
    settings = (
        ROOT / "apps/omlx-web/omlx_web/templates/dashboard/_settings.html"
    ).read_text()
    status = (
        ROOT / "apps/omlx-web/omlx_web/templates/dashboard/_status.html"
    ).read_text()
    dashboard = (ROOT / "apps/omlx-web/omlx_web/static/js/dashboard.js").read_text()
    english = (ROOT / "apps/omlx-web/omlx_web/i18n/en.json").read_text()

    assert (
        "('settings-battery-protection', t('battery_protection.section_label'))"
        in settings
    )
    assert 'id="settings-battery-protection"' in settings
    assert 'x-model="globalSettings.server.battery_protection_enabled"' in settings
    assert "batteryProtectionMainFields" in settings
    assert "batteryProtectionAdvancedFields" in settings
    battery_section = settings[
        settings.index("<!-- Battery Protection Section -->") : settings.index(
            "<!-- Model Section -->"
        )
    ]
    assert "x-model.number" not in battery_section
    assert "battery_protection: batteryProtectionOptions" in dashboard
    assert "state.policy || this.globalSettings.server.battery_protection" in dashboard
    assert "Resumes when battery reaches {resume}%" in english
    assert "batteryProtection" in status
    catalog_dir = ROOT / "apps/omlx-web/omlx_web/i18n"
    for catalog_path in catalog_dir.glob("*.json"):
        catalog = json.loads(catalog_path.read_text())
        assert catalog["battery_protection.section_label"]
        assert catalog["battery_protection.minimum_share_hint"]
        assert "{resume}" in catalog["battery_protection.reserve_suspended"]
        assert "75%" not in catalog["battery_protection.description"]


def test_battery_protection_load_validation_save_and_reset_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard behavior tests")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('apps/omlx-web/omlx_web/static/js/dashboard.js', 'utf8');
const defaults = {
    throttle_start_percent: 75, suspend_percent: 50, resume_percent: 55,
    minimum_share: 0.10, outage_share: 0.25, emergency_share: 0.10,
    outage_grace_seconds: 60, ac_stabilization_seconds: 10,
    restoration_share_per_second: 0.01, telemetry_stale_seconds: 5,
};
let postBody = null;
let getSettings = {
    server: { inference_share: 0.62, battery_protection_enabled: true,
        battery_protection: {resume_percent: 57, minimum_share: 0.2} },
};
let getDefaults = null;
const context = {
    localStorage: {getItem: () => null},
    THEME_STORAGE_KEY: 'theme', ENHANCED_READABILITY_KEY: 'readability',
    window: {t: key => key}, navigator: {language: 'en'}, document: {}, console,
    setTimeout: () => ({}), clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
    fetch: async (url, options = {}) => {
        if (options.method === 'POST') {
            postBody = JSON.parse(options.body);
            return {ok: true, json: async () => ({message: 'saved'})};
        }
        return {ok: true, json: async () => url.endsWith('/defaults') ? getDefaults : getSettings};
    },
};
(async () => {
    const state = vm.runInNewContext(source + '\n dashboard;', context)();
    await state.loadGlobalSettings();
    assert.equal(state.globalSettings.server.battery_protection.resume_percent, 57);
    assert.equal(state.batteryProtectionDraft.resume_percent, '57');
    assert.equal(state.batteryProtectionDraft.minimum_share, '20');
    assert.equal(state.globalSettings.server.battery_protection.outage_share, 0.25);
    assert.equal(state.globalSettings.server.inference_share, 0.62);
    assert.equal(state.batteryProtectionDraftFromOptions({minimum_share: 0.29}).minimum_share, '29');

    assert.equal(state.batteryProtectionOptionsForSave().minimum_share, 0.2);
    assert.equal(state.batteryProtectionOptionsForSave().restoration_share_per_second, 0.01);
    state.batteryProtectionDraft.resume_percent = '';
    assert.equal(state.batteryProtectionOptionsForSave(), null);
    assert.equal(state.batteryProtectionErrors.resume_percent, 'number');
    state.batteryProtectionDraft = {...state.batteryProtectionDraft, ...{
        throttle_start_percent: '75', suspend_percent: '60', resume_percent: '55',
        minimum_share: '20', outage_share: '25', emergency_share: '10',
        outage_grace_seconds: '0', ac_stabilization_seconds: '0',
        restoration_share_per_second: '1', telemetry_stale_seconds: '1',
    }};
    assert.equal(state.batteryProtectionOptionsForSave(), null);
    assert.equal(state.batteryProtectionErrors.resume_percent, 'thresholds');
    state.batteryProtectionDraft.suspend_percent = '50';
    state.batteryProtectionDraft.outage_share = '20';
    state.batteryProtectionDraft.emergency_share = '25';
    assert.equal(state.batteryProtectionOptionsForSave(), null);
    assert.equal(state.batteryProtectionErrors.outage_share, 'outage_caps');

    state.globalSettings.model.model_dirs = ['/models'];
    state.globalSettings.server.host = '127.0.0.1';
    state.globalSettings.server.port = 8000;
    state.globalSettings.server.max_audio_upload_size = '100MB';
    state.loadStats = async () => {};
    state.loadModels = async () => {};
    await state.saveGlobalSettings();
    assert.equal(postBody, null);
    assert.equal(state.saveError, 'battery_protection.validation_summary');

    state.batteryProtectionDraft = {
        throttle_start_percent: '75', suspend_percent: '50', resume_percent: '57',
        minimum_share: '20', outage_share: '25', emergency_share: '10',
        outage_grace_seconds: '0', ac_stabilization_seconds: '0',
        restoration_share_per_second: '1', telemetry_stale_seconds: '1',
    };
    await state.saveGlobalSettings();
    assert.equal(postBody.battery_protection_enabled, true);
    assert.equal(postBody.battery_protection.minimum_share, 0.2);
    assert.equal(postBody.battery_protection.outage_grace_seconds, 0);
    assert.equal(postBody.battery_protection.restoration_share_per_second, 0.01);
    assert.equal(postBody.inference_share, undefined);
    assert.equal(state.globalSettings.server.inference_share, 0.62);

    const previous = JSON.parse(JSON.stringify(state.globalSettings));
    state.globalSettings.server.battery_protection.resume_percent = 60;
    state.batteryProtectionDraft.resume_percent = '60';
    getDefaults = JSON.parse(JSON.stringify(state.globalSettings));
    getDefaults.server.battery_protection = {...defaults};
    state.inferenceThrottlePercent = 62;
    await state.resetGlobalSettingsDefaults();
    assert.equal(state.batteryProtectionDraft.resume_percent, '55');
    assert.equal(state.globalSettings.server.inference_share, 0.62);
    state.cancelGlobalSettingsReset();
    assert.equal(state.globalSettings.server.battery_protection.resume_percent, 60);
    assert.equal(state.batteryProtectionDraft.resume_percent, '60');
    assert.equal(state.globalSettings.server.inference_share, 0.62);

    state.batteryProtection = {state: 'reserve_suspended', suspended: true,
        configured_share: 0.875, effective_share: 0.725,
        policy: {...defaults, resume_percent: 58.5}};
    assert.equal(state.batteryProtectionMessage(), 'battery_protection.reserve_suspended');
    context.window.t = key => key === 'battery_protection.reserve_suspended'
        ? 'resume at {resume}; max {maximum}; effective {percent}' : key;
    assert.equal(state.batteryProtectionMessage(), 'resume at 58.5; max 87.5; effective 72.5');

    // Both server rejection and transport failure retain the staged values,
    // including the opt-in, while reloading the server's other settings.
    for (const networkFailure of [false, true]) {
        state.globalSettings.model.model_dirs = ['/models'];
        state.globalSettings.server.battery_protection_enabled = false;
        state.batteryProtectionDraft.resume_percent = '61';
        context.fetch = async (url, options = {}) => {
            if (options.method === 'POST') {
                if (networkFailure) throw new Error('offline');
                return {ok: false, status: 400, json: async () => ({detail: 'rejected'})};
            }
            return {ok: true, json: async () => getSettings};
        };
        await state.saveGlobalSettings();
        assert.equal(state.batteryProtectionDraft.resume_percent, '61');
        assert.equal(state.globalSettings.server.battery_protection_enabled, false);
        assert.ok(state.saveError);
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, timeout=30)
