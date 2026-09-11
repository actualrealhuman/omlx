from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_TEMPLATE = ROOT / "omlx/admin/templates/dashboard/_settings.html"
THROUGHPUT_TEMPLATE = ROOT / "omlx/admin/templates/dashboard/_bench.html"
ACCURACY_TEMPLATE = ROOT / "omlx/admin/templates/dashboard/_bench_accuracy.html"
DASHBOARD_JS = ROOT / "omlx/admin/static/js/dashboard.js"


def test_dashboard_exposes_master_and_per_kind_upload_controls():
    template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

    assert "globalSettings.benchmark_uploads.enabled =" in template
    assert "globalSettings.benchmark_uploads.throughput_enabled =" in template
    assert "globalSettings.benchmark_uploads.accuracy_enabled =" in template
    assert ':disabled="!globalSettings.benchmark_uploads.enabled"' in template


def test_dashboard_round_trips_upload_policy_with_safe_defaults():
    javascript = DASHBOARD_JS.read_text(encoding="utf-8")

    assert (
        "benchmark_uploads: { enabled: false, throughput_enabled: true, "
        "accuracy_enabled: true }"
    ) in javascript
    assert "...this.globalSettings.benchmark_uploads" in javascript
    assert "...data.benchmark_uploads" in javascript
    assert "benchmark_uploads_enabled:" in javascript
    assert "benchmark_uploads_throughput_enabled:" in javascript
    assert "benchmark_uploads_accuracy_enabled:" in javascript


def test_dashboard_explains_policy_skips_for_both_benchmark_kinds():
    throughput = THROUGHPUT_TEMPLATE.read_text(encoding="utf-8")
    accuracy = ACCURACY_TEMPLATE.read_text(encoding="utf-8")

    assert "Automatic throughput uploads are disabled in Settings" in throughput
    assert "Not uploaded (disabled in Settings)" in accuracy
