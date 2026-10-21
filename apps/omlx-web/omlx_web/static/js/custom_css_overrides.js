/* SPDX-License-Identifier: Apache-2.0 */
(function () {
    'use strict';

    var PRESET_KEY = 'omlx-style-preset';
    var CSS_KEY = 'omlx-custom-css';
    var ENABLED_KEY = 'omlx-custom-css-enabled';
    var BACKUP_KEY = 'omlx-custom-css-backup';
    var MAX_CSS_BYTES = 64 * 1024;
    var VALID_PRESETS = { 'default': true, 'true-black': true };
    var safeMode = false;

    try {
        safeMode = new URLSearchParams(window.location.search).get('custom_css') === 'off';
    } catch (_error) {
        safeMode = false;
    }

    function storageGet(key, fallback) {
        try {
            var value = window.localStorage.getItem(key);
            return value === null ? fallback : value;
        } catch (_error) {
            return fallback;
        }
    }

    function storageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
            return true;
        } catch (_error) {
            return false;
        }
    }

    function storageRemove(key) {
        try {
            window.localStorage.removeItem(key);
            return true;
        } catch (_error) {
            return false;
        }
    }

    function normalizePreset(value) {
        return VALID_PRESETS[value] ? value : 'default';
    }

    function readState() {
        var css = storageGet(CSS_KEY, '');
        return {
            preset: normalizePreset(storageGet(PRESET_KEY, 'default')),
            enabled: storageGet(ENABLED_KEY, 'off') === 'on' && byteLength(css) <= MAX_CSS_BYTES,
            css: css,
        };
    }

    function byteLength(value) {
        if (typeof TextEncoder !== 'undefined') {
            return new TextEncoder().encode(value).length;
        }
        return unescape(encodeURIComponent(value)).length;
    }

    function applyState(state) {
        var root = document.documentElement;
        var style = document.getElementById('omlx-custom-css');

        if (safeMode) {
            root.removeAttribute('data-style-preset');
            root.setAttribute('data-custom-css-safe-mode', '');
            if (style) style.textContent = '';
            return;
        }

        root.removeAttribute('data-custom-css-safe-mode');
        if (state.preset === 'true-black') {
            root.setAttribute('data-style-preset', 'true-black');
        } else {
            root.removeAttribute('data-style-preset');
        }
        if (style) style.textContent = state.enabled ? state.css : '';

        try {
            window.dispatchEvent(new CustomEvent('omlx-style-overrides-changed', {
                detail: {
                    preset: state.preset,
                    customCssEnabled: state.enabled,
                },
            }));
        } catch (_error) {
            // Styling still applies when CustomEvent is unavailable.
        }
    }

    function boot() {
        applyState(readState());
    }

    function dialog() {
        return document.getElementById('omlx-custom-css-dialog');
    }

    function textarea() {
        return document.getElementById('omlx-custom-css-input');
    }

    function enabledInput() {
        return document.getElementById('omlx-custom-css-enabled');
    }

    function selectedPreset() {
        var selected = document.querySelector('input[name="omlx-style-preset"]:checked');
        return normalizePreset(selected ? selected.value : 'default');
    }

    function selectPreset(value) {
        var input = document.querySelector(
            'input[name="omlx-style-preset"][value="' + normalizePreset(value) + '"]'
        );
        if (input) input.checked = true;
    }

    function setStatus(message, isError) {
        var status = document.getElementById('omlx-custom-css-status');
        if (!status) return;
        status.textContent = message || '';
        status.classList.toggle('is-error', !!isError);
    }

    function updateCount() {
        var input = textarea();
        var count = document.getElementById('omlx-custom-css-count');
        if (!input || !count) return;
        count.textContent = byteLength(input.value) + ' / ' + MAX_CSS_BYTES + ' bytes';
    }

    function open() {
        var modal = dialog();
        var input = textarea();
        var toggle = enabledInput();
        var state = readState();
        if (!modal || !input || !toggle) return;

        selectPreset(state.preset);
        toggle.checked = state.enabled;
        input.value = state.css;
        setStatus('', false);
        updateCount();

        var safeNotice = document.getElementById('omlx-custom-css-safe-notice');
        if (safeNotice) safeNotice.hidden = !safeMode;

        if (typeof modal.showModal === 'function') {
            if (!modal.open) modal.showModal();
        } else {
            modal.setAttribute('open', '');
        }
        window.setTimeout(function () { input.focus(); }, 0);
    }

    function close() {
        var modal = dialog();
        if (!modal) return;
        if (typeof modal.close === 'function') modal.close();
        else modal.removeAttribute('open');
    }

    function save() {
        var input = textarea();
        var toggle = enabledInput();
        if (!input || !toggle) return false;

        var css = input.value;
        if (byteLength(css) > MAX_CSS_BYTES) {
            setStatus(window.t('style_overrides.too_large'), true);
            return false;
        }

        storageSet(BACKUP_KEY, JSON.stringify(readState()));
        var ok = storageSet(PRESET_KEY, selectedPreset())
            && storageSet(CSS_KEY, css)
            && storageSet(ENABLED_KEY, toggle.checked ? 'on' : 'off');
        if (!ok) {
            setStatus(window.t('style_overrides.storage_error'), true);
            return false;
        }

        applyState(readState());
        setStatus(
            safeMode ? window.t('style_overrides.saved_safe_mode') : window.t('style_overrides.saved'),
            false
        );
        return true;
    }

    function saveSelectedPreset() {
        storageSet(BACKUP_KEY, JSON.stringify(readState()));
        if (!storageSet(PRESET_KEY, selectedPreset())) {
            setStatus(window.t('style_overrides.storage_error'), true);
            return;
        }
        applyState(readState());
        setStatus(
            safeMode ? window.t('style_overrides.saved_safe_mode') : window.t('style_overrides.saved'),
            false
        );
    }

    function reset() {
        if (!window.confirm(window.t('style_overrides.reset_confirm'))) return;
        storageSet(BACKUP_KEY, JSON.stringify(readState()));
        storageRemove(PRESET_KEY);
        storageRemove(CSS_KEY);
        storageRemove(ENABLED_KEY);
        selectPreset('default');
        if (enabledInput()) enabledInput().checked = false;
        if (textarea()) textarea().value = '';
        applyState(readState());
        setStatus(window.t('style_overrides.reset_done'), false);
        updateCount();
    }

    function restoreBackup() {
        var raw = storageGet(BACKUP_KEY, '');
        var backup;
        try {
            backup = JSON.parse(raw);
        } catch (_error) {
            backup = null;
        }
        if (!backup || typeof backup.css !== 'string' || byteLength(backup.css) > MAX_CSS_BYTES) {
            setStatus(window.t('style_overrides.no_backup'), true);
            return;
        }

        var current = readState();
        var restored = {
            preset: normalizePreset(backup.preset),
            enabled: backup.enabled === true,
            css: backup.css,
        };
        var ok = storageSet(BACKUP_KEY, JSON.stringify(current))
            && storageSet(PRESET_KEY, restored.preset)
            && storageSet(CSS_KEY, restored.css)
            && storageSet(ENABLED_KEY, restored.enabled ? 'on' : 'off');
        if (!ok) {
            setStatus(window.t('style_overrides.storage_error'), true);
            return;
        }

        selectPreset(restored.preset);
        enabledInput().checked = restored.enabled;
        textarea().value = restored.css;
        updateCount();
        applyState(restored);
        setStatus(window.t('style_overrides.restored'), false);
    }

    function loadTrueBlackSource() {
        var modal = dialog();
        var sourceUrl = modal ? modal.getAttribute('data-true-black-url') : '';
        if (!sourceUrl) return;

        window.fetch(sourceUrl, { credentials: 'same-origin' })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.text();
            })
            .then(function (css) {
                if (byteLength(css) > MAX_CSS_BYTES) throw new Error('too large');
                selectPreset('true-black');
                enabledInput().checked = true;
                textarea().value = css;
                updateCount();
                setStatus(window.t('style_overrides.preset_loaded'), false);
            })
            .catch(function () {
                setStatus(window.t('style_overrides.preset_load_error'), true);
            });
    }

    function exportCss() {
        var input = textarea();
        if (!input) return;
        var blob = new Blob([input.value], { type: 'text/css;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = 'omlx-custom.css';
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    }

    function importCss(event) {
        var file = event.target.files && event.target.files[0];
        event.target.value = '';
        if (!file) return;
        if (file.size > MAX_CSS_BYTES) {
            setStatus(window.t('style_overrides.too_large'), true);
            return;
        }
        file.text().then(function (css) {
            textarea().value = css;
            enabledInput().checked = true;
            updateCount();
            setStatus(window.t('style_overrides.imported'), false);
        }).catch(function () {
            setStatus(window.t('style_overrides.import_error'), true);
        });
    }

    function wireDialog() {
        var modal = dialog();
        if (!modal) return;
        var input = textarea();
        var importInput = document.getElementById('omlx-custom-css-file');

        document.querySelectorAll('[data-open-style-overrides]').forEach(function (button) {
            button.addEventListener('click', open);
        });
        document.getElementById('omlx-custom-css-close').addEventListener('click', close);
        document.getElementById('omlx-custom-css-cancel').addEventListener('click', close);
        document.getElementById('omlx-custom-css-save').addEventListener('click', save);
        document.getElementById('omlx-custom-css-reset').addEventListener('click', reset);
        document.getElementById('omlx-custom-css-restore').addEventListener('click', restoreBackup);
        document.getElementById('omlx-custom-css-load-preset').addEventListener('click', loadTrueBlackSource);
        document.getElementById('omlx-custom-css-export').addEventListener('click', exportCss);
        document.getElementById('omlx-custom-css-import').addEventListener('click', function () {
            importInput.click();
        });
        document.querySelectorAll('input[name="omlx-style-preset"]').forEach(function (input) {
            input.addEventListener('change', saveSelectedPreset);
        });
        input.addEventListener('input', updateCount);
        importInput.addEventListener('change', importCss);
        modal.addEventListener('click', function (event) {
            if (event.target === modal) close();
        });
    }

    window.omlxStyleOverrides = {
        apply: applyState,
        boot: boot,
        close: close,
        isSafeMode: function () { return safeMode; },
        open: open,
        read: readState,
        save: save,
    };

    boot();
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wireDialog);
    } else {
        wireDialog();
    }
})();
