# SPDX-License-Identifier: Apache-2.0
"""Static integration coverage for built-in and user-authored style overrides."""

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "omlx_web/templates"
STATIC = ROOT / "omlx_web/static"
I18N = ROOT / "omlx_web/i18n"

BASE = (TEMPLATES / "base.html").read_text(encoding="utf-8")
DIALOG = (TEMPLATES / "_style_overrides_dialog.html").read_text(encoding="utf-8")
CHAT = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
DASHBOARD_SETTINGS = (TEMPLATES / "dashboard/_settings.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "js/custom_css_overrides.js").read_text(encoding="utf-8")
PRESET = (STATIC / "css/true-black.css").read_text(encoding="utf-8")


def test_templates_compile():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.get_template("base.html")
    env.get_template("chat.html")
    env.get_template("dashboard.html")


def test_stylesheet_cascade_order():
    head = BASE.index("{% block head %}")
    preset = BASE.index("css/true-black.css")
    readability = BASE.index("Enhanced Readability: global overrides")
    custom = BASE.index('id="omlx-custom-css"')
    script = BASE.index("js/custom_css_overrides.js")
    assert head < preset < readability < custom < script


def test_app_default_is_inert_and_custom_css_uses_text_content():
    assert "data-style-preset" not in BASE.split("<body", 1)[0].split("true-black.css", 1)[0]
    assert "style.textContent = state.enabled ? state.css : ''" in SCRIPT
    assert "innerHTML" not in SCRIPT
    assert "MAX_CSS_BYTES = 64 * 1024" in SCRIPT


def test_recovery_mode_suppresses_without_deleting_saved_css():
    assert "new URLSearchParams(window.location.search)" in SCRIPT
    assert "get('custom_css') === 'off'" in SCRIPT
    safe_branch = SCRIPT.split("if (safeMode)", 1)[1].split("return;", 1)[0]
    assert "data-style-preset" in safe_branch
    assert "style.textContent = ''" in safe_branch
    assert "storageRemove(CSS_KEY)" not in safe_branch
    assert "?custom_css=off" in DIALOG


def test_true_black_preset_is_scoped_and_high_contrast():
    assert PRESET.count(':root[data-style-preset="true-black"][data-theme="dark"]') >= 10
    assert "--bg-primary: #000000" in PRESET
    assert "--text-primary: #ffffff" in PRESET
    assert "--btn-primary: #ffffff" in PRESET
    assert "--btn-primary-text: #000000" in PRESET
    selectors = [
        selector.strip()
        for selector in re.findall(r"([^{}]+)\{[^{}]*\}", PRESET)
        if not selector.strip().startswith("/*")
    ]
    assert selectors
    assert all("data-style-preset" in selector for selector in selectors)


def test_shared_editor_is_reachable_from_dashboard_and_chat():
    assert BASE.count('{% include "_style_overrides_dialog.html" %}') == 1
    assert "data-open-style-overrides" in DASHBOARD_SETTINGS
    assert "data-open-style-overrides" in CHAT
    assert 'id="omlx-custom-css-input"' in DIALOG
    assert 'value="default"' in DIALOG
    assert 'value="true-black"' in DIALOG


def test_all_locales_have_complete_style_override_strings():
    required = {
        "style_overrides.title",
        "style_overrides.description",
        "style_overrides.open",
        "style_overrides.preset.default",
        "style_overrides.preset.true_black",
        "style_overrides.custom",
        "style_overrides.enable",
        "style_overrides.safe_mode",
        "style_overrides.save",
        "style_overrides.reset",
        "style_overrides.restore",
        "style_overrides.too_large",
    }
    for path in I18N.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = required - data.keys()
        assert not missing, f"{path.name} missing {sorted(missing)}"
        assert all(data[key].strip() for key in required)
