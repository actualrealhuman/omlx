# SPDX-License-Identifier: Apache-2.0
"""Render the real dashboard and validate its Activity integration."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1] / "omlx_web"


class ActivityMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.expressions = []
        self.errors = []
        self.stack = []

    def handle_starttag(self, tag, attributes):
        if tag not in {"input", "link", "img", "meta", "br", "hr"}:
            self.stack.append(tag)
        for name, value in attributes:
            if not value or not name.startswith(("x-", "@", ":")):
                continue
            if name.startswith(("x-transition", "x-ref")) or name == "x-model":
                continue
            if name == "x-for":
                value = value.split(" in ", 1)[1]
            statement = name.startswith("@") or name in {"x-init", "x-effect"}
            self.expressions.append(
                {"name": name, "value": value, "statement": statement}
            )

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
        else:
            self.stack.pop()


def test_activity_template_balances_and_all_alpine_expressions_compile():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Alpine expression validation")
    parser = ActivityMarkup()
    parser.feed((ROOT / "templates/dashboard/_activity.html").read_text())
    assert parser.errors == []
    assert parser.stack == []
    script = """
const checks = JSON.parse(require('fs').readFileSync(0, 'utf8'));
for (const c of checks) {
  try { new Function('$event', c.statement ? c.value : `return (${c.value})`); }
  catch (error) { throw new Error(c.name + ': ' + c.value + ': ' + error.message); }
}
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps(parser.expressions),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_full_dashboard_renders_activity_with_local_assets_and_translatable_text():
    locale = json.loads((ROOT / "i18n/en.json").read_text())
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    env.globals.update(
        t=lambda key: locale[key],
        static=lambda path: "/admin/static/" + path,
        current_lang="en",
        locale_json=json.dumps(locale),
        version="test",
    )
    rendered = env.get_template("dashboard.html").render()
    assert 'id="panel-activity"' in rendered
    assert 'id="tab-activity"' in rendered
    assert "/admin/static/js/activity.js" in rendered
    assert "/admin/static/css/activity.css" in rendered
    assert "activityRequestHref(pp.activity_request_id)" in rendered
    assert "activityRequestHref(gen.activity_request_id)" in rendered
    assert "activityRequestHref(wait.activity_request_id)" in rendered
    activity = (ROOT / "templates/dashboard/_activity.html").read_text()
    assert "x-html=" not in activity
    assert "detail?.requested_model_id !== detail?.model_id" in activity
    assert "detail?.model_resolution_observed === true" in activity
    assert "activity.duration_hint" in activity
    assert activity.count("record.id") >= 4
    assert activity.count("record.endpoint") >= 4
    assert activity.count("observed_input_tokens") >= 2
    assert activity.count("observed_generated_tokens") >= 2
    assert activity.count("modelSummary(record)") >= 4
    assert activity.count("duration(record)") >= 2
    assert "activity.counts_scope" in activity
    coverage_detail = activity.split('id="activity-coverage-detail"', 1)[1].split('<div x-show="detail?.capture_gaps"', 1)[0]
    assert "activity.duration_hint" in coverage_detail
    assert "activity.counts_scope" in coverage_detail
    assert "submitted_fidelity" in coverage_detail
    assert "projection_fidelity" in coverage_detail
    assert "media_normalized" in coverage_detail
    assert "label('projection.' + projectionState)" in activity
    expected = {key for key in locale if key.startswith("activity.")}
    for file in (ROOT / "i18n").glob("*.json"):
        translated = json.loads(file.read_text())
        assert expected <= translated.keys()
        assert translated["navbar.tab.activity"]
