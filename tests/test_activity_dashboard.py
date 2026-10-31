# SPDX-License-Identifier: Apache-2.0
"""Dashboard correlation uses observed parent identity rather than engine IDs."""

from types import SimpleNamespace
from unittest.mock import patch

from omlx.admin import routes


def test_activity_dashboard_identity_does_not_guess_for_aggregate_rows():
    capture = SimpleNamespace(handle=SimpleNamespace(request_id="parent-request"))
    observation = SimpleNamespace(request=capture)
    assert (
        routes._activity_request_id(SimpleNamespace(activity=observation))
        == "parent-request"
    )
    core = SimpleNamespace(_activity_invocations={"engine-id": observation})
    assert (
        routes._activity_request_id(core=core, request_id="engine-id")
        == "parent-request"
    )
    assert routes._activity_request_id(core=core, request_id="rank0") is None
    assert routes._activity_request_id(SimpleNamespace(request_id="engine-id")) is None


def test_dashboard_wait_prefill_and_generate_rows_resolve_the_same_parent():
    def request(engine_id):
        observation = SimpleNamespace(
            request=SimpleNamespace(
                handle=SimpleNamespace(request_id="parent-" + engine_id)
            )
        )
        return SimpleNamespace(
            request_id=engine_id,
            activity=observation,
            arrival_time=0,
            num_prompt_tokens=12,
            num_output_tokens=2,
        )

    waiting, prefill, generate = [
        request(name) for name in ("waiting", "prefill", "generate")
    ]
    scheduler = SimpleNamespace(
        snapshot_for_admin=lambda: {
            "running_by_id": {"prefill": prefill, "generate": generate},
            "waiting": [waiting],
        }
    )
    core = SimpleNamespace(scheduler=scheduler, _output_collectors={})
    engine = SimpleNamespace(_engine=SimpleNamespace(engine=core))
    pool = SimpleNamespace(
        get_status=lambda: {"models": [{"id": "example", "loaded": True}]},
        _entries={"example": SimpleNamespace(engine=engine, in_use=0)},
    )
    tracker = SimpleNamespace(get_model_progress=lambda _: [{"request_id": "prefill"}])
    with (
        patch.object(routes, "_get_engine_pool", return_value=pool),
        patch.object(routes, "_get_server_state", return_value=None),
        patch.object(routes, "_get_settings_manager", return_value=None),
        patch.object(routes, "_get_global_settings", return_value=None),
        patch("omlx.prefill_progress.get_prefill_tracker", return_value=tracker),
    ):
        model = routes._build_active_models_data()["models"][0]
    for field, expected in (
        ("waiting", "waiting"),
        ("prefilling", "prefill"),
        ("generating", "generate"),
    ):
        assert model[field][0]["activity_request_id"] == "parent-" + expected
