# SPDX-License-Identifier: Apache-2.0
"""Per-model curves share system suspension without replacing preferences."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omlx.battery_service import BatteryProtectionService
from omlx.engine_pool import EnginePool
from omlx.generation_gate import GenerationGate


def service(tmp_path, monkeypatch, charge=65):
    monkeypatch.setattr("omlx.battery_service.sys.platform", "darwin")
    raw = SimpleNamespace(
        source="ac",
        adapter_present=True,
        battery_present=True,
        charge_percent=charge,
        charging=True,
    )
    applied = {}
    suspended = []
    instance = BatteryProtectionService(
        enabled=True,
        requested_share=1.0,
        apply_share=lambda share: None,
        state_path=tmp_path / "recovery.json",
        reader=SimpleNamespace(sample=lambda: raw),
        notifier=SimpleNamespace(),
        gate=GenerationGate(),
        clock=lambda: 0.0,
        apply_model_share=lambda model, share: applied.update({model: share}),
        apply_suspension=suspended.append,
    )
    instance.sample_now()
    return instance, applied, suspended


def test_each_model_uses_its_own_requested_maximum_on_reserve_curve(
    tmp_path, monkeypatch
):
    policy, applied, _ = service(tmp_path, monkeypatch)
    policy.configure_model_share("prose", 1.0)
    policy.configure_model_share("workhorse", 0.75)
    assert applied["prose"] == pytest.approx(0.64)
    assert applied["workhorse"] == pytest.approx(0.49)


def test_global_ceiling_caps_models_without_overwriting_saved_preference(
    tmp_path, monkeypatch
):
    policy, applied, _ = service(tmp_path, monkeypatch, charge=80)
    policy.configure_model_share("prose", 1.0)
    policy.set_requested_share(0.8)
    assert applied["prose"] == 0.8
    assert policy._model_shares["prose"] == 1.0
    policy.set_requested_share(1.0)
    # Restoration ramps; raising the global ceiling cannot bypass that ramp.
    assert applied["prose"] == 0.8


def test_reserve_suspension_reports_zero_without_sending_zero_to_pacers(
    tmp_path, monkeypatch
):
    policy, applied, suspended = service(tmp_path, monkeypatch, charge=49)
    policy.configure_model_share("prose", 1.0)
    assert policy.model_effective_share("prose") == 0
    assert suspended[-1] is True
    assert applied["prose"] == 0


def test_zero_model_ceiling_survives_battery_curve_and_reports_pause(
    tmp_path, monkeypatch
):
    policy, applied, _ = service(tmp_path, monkeypatch)
    policy.configure_model_share("paused", 0.0)
    assert policy.model_effective_share("paused") == 0.0
    assert applied["paused"] == 0.0


def test_battery_never_rounds_a_small_positive_model_ceiling_to_zero(
    tmp_path, monkeypatch
):
    policy, applied, _ = service(tmp_path, monkeypatch, charge=65)
    policy.configure_model_share("tiny", 0.004)
    assert policy.model_effective_share("tiny") == pytest.approx(0.004)
    assert 0.0 < applied["tiny"] <= 0.004


def test_hierarchical_model_ceiling_reaches_battery_and_model_pacer(
    tmp_path, monkeypatch
):
    policy, _, _ = service(tmp_path, monkeypatch, charge=80)
    policy.set_requested_share(0.8)

    pacer = SimpleNamespace(share=1.0)
    pacer.set_share = lambda share: setattr(pacer, "share", share)
    applied = []
    coordinator = SimpleNamespace(
        status_snapshot=lambda: {"models": {}}, set_model_admission=Mock()
    )
    pool = EnginePool.__new__(EnginePool)
    pool._inference_controls_enabled = True
    pool._inference_requested_share = 0.8
    pool._inference_class_allowances = {"interactive": 1.0, "background": 0.5}
    pool._inference_coordinator = coordinator
    pool._model_pacers = {"ling": pacer}
    pool._model_scheduling = {}
    pool._battery_protection = policy

    def apply_model_share(model_id, share):
        applied.append((model_id, share))
        pool._apply_model_inference_share(model_id, share)

    policy._apply_model_share = apply_model_share

    pool.configure_model_scheduling("ling", allowance=0.25)

    assert policy._model_shares["ling"] == pytest.approx(0.1)
    assert policy.model_effective_share("ling") == pytest.approx(0.1)
    assert applied == [("ling", pytest.approx(0.1))]
    assert pacer.share == pytest.approx(0.1)
