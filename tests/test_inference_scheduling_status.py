# SPDX-License-Identifier: Apache-2.0
"""Scheduling status does not load models or misclassify editable policies."""

from types import SimpleNamespace

from omlx.engine_pool import EnginePool
from omlx.inference_pacing import InferencePacer


class SettingsReader:
    def __init__(self, settings):
        self.settings = settings

    def get_settings(self, model_id):
        return self.settings[model_id]


def pool_for(entry, settings):
    pool = EnginePool.__new__(EnginePool)
    pool._inference_controls_enabled = True
    pool._entries = {entry.model_id: entry}
    pool._settings_manager = SettingsReader({entry.model_id: settings})
    pool._inference_coordinator = SimpleNamespace(
        status_snapshot=lambda: {
            "enabled": False,
            "quantum_seconds": 5.0,
            "owner": None,
            "models": {},
        }
    )
    pool._inference_requested_share = 0.8
    pool._inference_class_allowances = {"interactive": 0.5, "background": 0.25}
    pool._model_pacers = {}
    pool._model_scheduling = {}
    pool._inference_pacer = InferencePacer(0.8)
    pool._battery_protection = None
    return pool


def test_unloaded_text_model_keeps_editable_policy_and_uses_relative_ceiling():
    entry = SimpleNamespace(
        model_id="text-model",
        model_type="llm",
        engine_type="batched",
        config_model_type="llama",
        engine=None,
    )
    settings = SimpleNamespace(
        inference_share=None,
        inference_allowance=0.25,
        inference_priority=7,
        inference_exclusive=False,
        display_name="Text model",
        is_favorite=True,
    )
    row = EnginePool.get_inference_scheduling_status(pool_for(entry, settings))[
        "models"
    ][0]

    assert row["loaded"] is False
    assert row["supported"] is False
    assert row["capable"] is True
    assert row["controllable"] is True
    assert row["unsupported"] is False
    assert row["configured_share"] == 0.8 * 0.25 * 0.25
    assert row["allowance"] == 0.25
    assert row["display_name"] == "Text model"
    assert row["is_favorite"] is True
    assert row["state"] == "unloaded"

    # Saved policy remains visible while bypassed, but must not masquerade as
    # an active battery restriction for an unloaded model.
    pool = pool_for(entry, settings)
    pool._inference_controls_enabled = False
    bypassed = pool.get_inference_scheduling_status()
    assert bypassed["controls_enabled"] is False
    assert bypassed["global_maximum_share"] == 0.8
    assert bypassed["effective_global_maximum_share"] == 1.0
    assert bypassed["models"][0]["configured_share"] == row["configured_share"]
    assert bypassed["models"][0]["effective_share"] == 1.0


def test_loaded_engine_without_enforcement_hooks_is_marked_unsupported():
    entry = SimpleNamespace(
        model_id="unpaced-model",
        model_type="llm",
        engine_type="batched",
        config_model_type="llama",
        engine=object(),
    )
    settings = SimpleNamespace(
        inference_share=None,
        inference_allowance=1.0,
        inference_priority=0,
        inference_exclusive=False,
        display_name=None,
        is_favorite=False,
    )
    row = EnginePool.get_inference_scheduling_status(pool_for(entry, settings))[
        "models"
    ][0]

    assert row["loaded"] is True
    assert row["supported"] is False
    assert row["capable"] is True
    assert row["controllable"] is False
    assert row["unsupported"] is True
    assert row["state"] == "unsupported"
