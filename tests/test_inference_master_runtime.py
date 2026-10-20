# SPDX-License-Identifier: Apache-2.0
"""Master inference-control bypass preserves preferences and battery authority."""

import threading
import time
from types import SimpleNamespace

import pytest

from omlx.battery_service import BatteryProtectionService
from omlx.engine_pool import EnginePool
from omlx.generation_gate import GenerationGate
from omlx.inference_pacing import InferencePacer
from omlx.inference_scheduling import ModelExecutionCoordinator
from omlx.model_pacing import ModelPacer


def wait_for_event(event):
    assert event.wait(2), "worker did not reach the expected point"


def wait_for_model_waiting(coordinator, model_id):
    deadline = time.monotonic() + 2
    with coordinator._condition:
        while coordinator.status_snapshot()["models"][model_id]["waiting"] == 0:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{model_id} did not wait for its execution turn"
            # Entering the waiting set need not notify observers; recheck it
            # without waiting for the held worker's own timeout to expire.
            coordinator._condition.wait(timeout=min(remaining, 0.01))


class Coordinator:
    def __init__(self):
        self.enabled = False
        self.quantum = 5.0
        self.admission = {}
        self.model_policies = {}

    def status_snapshot(self):
        return {
            "enabled": self.enabled,
            "quantum_seconds": self.quantum,
            "models": {},
        }

    def configure(self, *, enabled, quantum_seconds):
        self.enabled = enabled
        self.quantum = quantum_seconds

    def configure_model(self, model_id, **policy):
        self.model_policies[model_id] = policy

    def set_model_admission(self, model_id, admitted):
        self.admission[model_id] = admitted


class Battery:
    def __init__(self):
        self.requested_share = 1.0
        self.model_shares = {}

    def set_requested_share(self, share):
        self.requested_share = share

    def configure_model_share(self, model_id, share):
        self.model_shares[model_id] = share

    def status(self):
        return {"configured_share": self.requested_share, "effective_share": 0.4}


def pool_for_controls():
    pool = EnginePool.__new__(EnginePool)
    pool._inference_pacer = InferencePacer()
    pool._inference_requested_share = 0.6
    pool._inference_class_allowances = {"interactive": 0.5, "background": 0.25}
    pool._inference_controls_enabled = True
    pool._inference_scheduling_requested_enabled = True
    pool._inference_scheduling_quantum_seconds = 7.0
    pool._inference_coordinator = Coordinator()
    pool._model_pacers = {"model-a": SimpleNamespace(set_share=lambda share: None)}
    pool._model_scheduling = {}
    pool._battery_protection = Battery()
    return pool


def test_master_bypass_preserves_user_preferences_and_disables_their_enforcement():
    pool = pool_for_controls()
    pool.configure_model_scheduling(
        "model-a", priority=9, exclusive=True, allowance=0.2
    )
    pool.configure_inference_controls(False)

    assert pool._inference_requested_share == 0.6
    assert pool._inference_class_allowances == {"interactive": 0.5, "background": 0.25}
    assert pool._model_scheduling["model-a"] == {
        "share": None,
        "priority": 9,
        "exclusive": True,
        "allowance": 0.2,
    }
    assert pool.get_requested_inference_share() == 1.0
    assert pool._battery_protection.requested_share == 1.0
    assert pool._battery_protection.model_shares["model-a"] == 1.0
    assert pool._inference_coordinator.enabled is False
    assert pool._inference_coordinator.admission["model-a"] is True

    status = pool.get_inference_controls_status()
    assert status["enabled"] is False
    assert status["configured_global_share"] == 0.6
    assert status["effective_global_share"] == 1.0
    assert status["effective_global_maximum_share"] == 0.4
    assert status["effective_interactive_allowance"] == 1.0
    assert status["effective_scheduling_enabled"] is False


def test_preferences_changed_while_bypassed_apply_after_reenable():
    pool = pool_for_controls()
    pool.configure_inference_controls(False)
    pool.configure_inference_share(0.4)
    pool.configure_inference_allowances(interactive=0.8, background=0.3)
    pool.configure_model_scheduling("model-a", priority=4, allowance=0.5)
    pool.configure_inference_scheduling(False, quantum_seconds=11.0)

    assert pool.get_requested_inference_share() == 1.0
    assert pool._battery_protection.model_shares["model-a"] == 1.0
    assert pool._inference_coordinator.enabled is False

    pool.configure_inference_controls(True)

    assert pool.get_requested_inference_share() == 0.4
    assert pool._battery_protection.requested_share == 0.4
    assert pool._battery_protection.model_shares["model-a"] == pytest.approx(
        0.4 * 0.3 * 0.5
    )
    assert pool._inference_coordinator.enabled is False
    assert pool._inference_coordinator.quantum == 11.0
    assert pool._inference_coordinator.admission["model-a"] is True


def test_reenabling_restores_saved_turn_policy():
    pool = pool_for_controls()
    pool.configure_inference_scheduling(True, quantum_seconds=13.0)
    pool.configure_inference_controls(False)

    assert pool._inference_coordinator.enabled is False
    assert pool._inference_scheduling_requested_enabled is True

    pool.configure_inference_controls(True)

    assert pool._inference_coordinator.enabled is True
    assert pool._inference_coordinator.quantum == 13.0


class PowerReader:
    def __init__(self, charge_percent):
        self.charge_percent = charge_percent

    def sample(self):
        return SimpleNamespace(
            source="ac",
            adapter_present=True,
            battery_present=True,
            charge_percent=self.charge_percent,
            charging=True,
        )

    def close(self):
        pass


class PowerNotifier:
    def changed(self):
        return False

    def close(self):
        pass


def test_bypass_does_not_clear_battery_suspension_and_restores_zero_allowance(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("omlx.battery_service.sys.platform", "darwin")
    gate = GenerationGate()
    coordinator = ModelExecutionCoordinator(enabled=True)
    global_pacer = InferencePacer(0.8)
    model_pacer = ModelPacer(global_pacer)
    pool = EnginePool.__new__(EnginePool)
    pool._inference_pacer = global_pacer
    pool._inference_requested_share = 0.8
    pool._inference_class_allowances = {"interactive": 0.5, "background": 0.25}
    pool._inference_controls_enabled = True
    pool._inference_scheduling_requested_enabled = True
    pool._inference_scheduling_quantum_seconds = 5.0
    pool._inference_coordinator = coordinator
    pool._model_pacers = {"model-a": model_pacer}
    pool._model_scheduling = {}
    pool._battery_protection = None
    coordinator.register_model("model-a")

    reader = PowerReader(charge_percent=65)
    now = [0.0]
    battery = BatteryProtectionService(
        enabled=True,
        requested_share=pool.get_requested_inference_share(),
        apply_share=global_pacer.set_share,
        apply_model_share=pool._apply_model_inference_share,
        apply_suspension=coordinator.set_suspended,
        state_path=tmp_path / "battery-state.json",
        reader=reader,
        notifier=PowerNotifier(),
        gate=gate,
        clock=lambda: now[0],
    )
    pool._battery_protection = battery
    battery.sample_now()
    pool.configure_model_scheduling("model-a", allowance=0.0)

    assert model_pacer.share == 0.0
    assert (
        coordinator.status_snapshot()["models"]["model-a"]["admission_enabled"] is False
    )

    reader.charge_percent = 49
    now[0] = 0.5
    battery.sample_now()
    assert battery.status()["suspended"] is True
    assert gate.paused_snapshot is True
    assert coordinator.status_snapshot()["suspended"] is True

    # Bypass user zero limits while the independent battery hard pause is active.
    pool.configure_inference_controls(False)
    assert battery.status()["configured_share"] == 1.0
    assert battery.status()["suspended"] is True
    assert gate.paused_snapshot is True
    assert coordinator.status_snapshot()["suspended"] is True
    assert model_pacer.share == 0.0
    assert (
        coordinator.status_snapshot()["models"]["model-a"]["admission_enabled"] is False
    )

    # Battery recovery works while bypassed, and the battery cap still limits pacing.
    reader.charge_percent = 56
    now[0] = 1.0
    battery.sample_now()
    assert battery.status()["suspended"] is False
    assert gate.paused_snapshot is False
    assert coordinator.status_snapshot()["suspended"] is False
    assert 0.0 < model_pacer.share <= 0.1

    # Re-enabling user controls restores the saved zero model allowance.
    pool.configure_inference_controls(True)
    assert battery.status()["configured_share"] == 0.8
    assert battery.status()["suspended"] is False
    assert gate.paused_snapshot is False
    assert model_pacer.share == 0.0
    assert (
        coordinator.status_snapshot()["models"]["model-a"]["admission_enabled"] is False
    )


def test_invalid_turn_configuration_does_not_replace_saved_preferences():
    pool = pool_for_controls()
    pool._inference_coordinator = ModelExecutionCoordinator(
        enabled=True, quantum_seconds=7
    )
    pool._inference_scheduling_requested_enabled = True
    pool._inference_scheduling_quantum_seconds = 7.0

    with pytest.raises(ValueError):
        pool.configure_inference_scheduling(False, quantum_seconds=-1)

    assert pool._inference_scheduling_requested_enabled is True
    assert pool._inference_scheduling_quantum_seconds == 7.0
    assert pool._inference_coordinator.status_snapshot()["enabled"] is True
    assert pool._inference_coordinator.status_snapshot()["quantum_seconds"] == 7.0


def real_coordinator_pool(*, scheduling_enabled):
    coordinator = ModelExecutionCoordinator(enabled=scheduling_enabled)
    global_pacer = InferencePacer()
    pool = EnginePool.__new__(EnginePool)
    pool._inference_pacer = global_pacer
    pool._inference_requested_share = 0.8
    pool._inference_class_allowances = {"interactive": 1.0, "background": 1.0}
    pool._inference_controls_enabled = True
    pool._inference_scheduling_requested_enabled = scheduling_enabled
    pool._inference_scheduling_quantum_seconds = 5.0
    pool._inference_coordinator = coordinator
    pool._model_pacers = {}
    pool._model_scheduling = {}
    pool._battery_protection = None
    return pool


def test_master_bypass_releases_worker_blocked_by_zero_model_allowance():
    pool = real_coordinator_pool(scheduling_enabled=False)
    coordinator = pool._inference_coordinator
    registration = coordinator.register_model("paused")
    pool._model_pacers["paused"] = ModelPacer(pool._inference_pacer)
    pool.configure_model_scheduling("paused", allowance=0.0)
    attempting = threading.Event()
    entered = threading.Event()

    def run_generation():
        attempting.set()
        with registration.work():
            entered.set()

    worker = threading.Thread(target=run_generation)
    worker.start()
    try:
        wait_for_event(attempting)
        assert not entered.is_set()
        assert pool._model_pacers["paused"].share == 0.0
        pool.configure_inference_controls(False)
        wait_for_event(entered)
        assert pool._model_pacers["paused"].share == 1.0
    finally:
        # If an assertion fails, opening admission ensures the worker can exit.
        if worker.is_alive():
            pool.configure_inference_controls(False)
            wait_for_event(entered)
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_reenabled_serial_turn_waits_for_bypass_in_flight_work_to_exit():
    pool = real_coordinator_pool(scheduling_enabled=True)
    coordinator = pool._inference_coordinator
    a = coordinator.register_model("a")
    b = coordinator.register_model("b")
    pool._model_pacers["a"] = ModelPacer(pool._inference_pacer)
    pool._model_pacers["b"] = ModelPacer(pool._inference_pacer)
    pool.configure_model_scheduling("a", priority=0)
    pool.configure_model_scheduling("b", priority=0, exclusive=True)
    a_entered = threading.Event()
    let_a_exit = threading.Event()
    b_attempting = threading.Event()
    b_entered = threading.Event()

    def run_a():
        with a.work():
            a_entered.set()
            wait_for_event(let_a_exit)

    def run_b():
        b_attempting.set()
        with b.work():
            b_entered.set()

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    thread_b = None
    try:
        wait_for_event(a_entered)
        pool.configure_inference_controls(False)
        assert coordinator.status_snapshot()["enabled"] is False
        pool.configure_inference_controls(True)
        assert coordinator.status_snapshot()["enabled"] is True

        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        wait_for_event(b_attempting)
        wait_for_model_waiting(coordinator, "b")
        assert not b_entered.is_set()
        let_a_exit.set()
        wait_for_event(b_entered)
    finally:
        let_a_exit.set()
        if thread_b is not None and thread_b.is_alive():
            wait_for_event(b_entered)
        thread_a.join(timeout=2)
        if thread_b is not None:
            thread_b.join(timeout=2)
    assert not thread_a.is_alive()
    assert thread_b is not None and not thread_b.is_alive()
