# SPDX-License-Identifier: Apache-2.0
"""Resolved dispatch is distinct from actual observed and retained evidence."""

import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.runtime import RequestCapture, _current, configure_recorder


@pytest.fixture
def capture(tmp_path):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    assert recorder.wait_ready()
    previous = configure_recorder(recorder)
    handle = recorder.begin(model_id="example", endpoint="/v1/chat/completions")
    result = RequestCapture(recorder, handle)
    token = _current.set(result)
    yield result
    _current.reset(token)
    configure_recorder(previous)
    assert recorder.close()


def detail(capture):
    assert capture.recorder.flush()
    return capture.recorder.reader.detail(capture.handle.request_id)


@pytest.mark.parametrize(
    "kind",
    [
        "local_text",
        "local_vlm",
        "local_diffusion",
        "distributed_proxy",
        "splash_native",
        "unknown",
    ],
)
@pytest.mark.parametrize("leased", [False, True])
async def test_actual_resolution_dispatch_records_only_known_backend_identity(
    capture, monkeypatch, kind, leased
):
    from omlx import server
    from omlx.engine import BatchedEngine, VLMBatchedEngine
    from omlx.engine.distributed import DistributedBatchedEngine
    from omlx.engine.splash import SplashEngine

    cls = {
        "local_text": BatchedEngine,
        "local_vlm": VLMBatchedEngine,
        "local_diffusion": VLMBatchedEngine,
        "distributed_proxy": DistributedBatchedEngine,
        "splash_native": SplashEngine,
    }.get(kind)
    engine = cls.__new__(cls) if cls else SimpleNamespace()
    if kind == "local_diffusion":
        engine._diffusion_family = "block"

    async def resolve(model, engine_type, **kwargs):
        if leased:
            assert kwargs["_lease"] is True
            kwargs["_leased_out"].append("resolved-example")
        return engine

    get_engine = AsyncMock(side_effect=resolve)
    monkeypatch.setattr(server, "get_engine", get_engine)
    lease = SimpleNamespace(model_id=None) if leased else None
    assert await server.get_engine_for_model("alias", lease=lease) is engine
    if leased:
        assert lease.model_id == "resolved-example"
    result = detail(capture)
    assert result["coverage"]["backend_kinds"] == [kind]
    assert result["coverage"]["backend_source"] == "resolved_engine_dispatch"
    assert result["coverage"]["scheduler_path"] == "not_observed"
    assert set(result["coverage"]["evidence"].values()) == {"not_observed"}
    assert result["coverage"]["preparser_output_guaranteed"] is False


def test_proxy_resolution_can_coexist_with_actual_local_core_evidence(capture):
    recorder = capture.recorder
    assert recorder.backend(capture.handle, "distributed_proxy")
    invocation = capture.invocation("local-fallback")
    invocation.prepared("actual formatted prompt", [10, 11])
    invocation.output(
        SimpleNamespace(
            new_text="answer",
            completion_tokens=1,
            finished=True,
            error=None,
            finish_reason="stop",
        )
    )
    capture.wire('data: {"choices":[]}\n\n')
    result = detail(capture)
    assert result["coverage"]["backend_kinds"] == ["distributed_proxy"]
    assert result["coverage"]["scheduler_path"] == "observed"
    assert result["coverage"]["evidence"]["prepared_text"] == "retained"
    assert result["coverage"]["evidence"]["prepared_tokens"] == "retained"
    assert result["coverage"]["evidence"]["producer_output"] == "retained"
    assert result["coverage"]["evidence"]["api_wire"] == "retained"


def test_vlm_ids_only_do_not_invent_prepared_text(capture):
    assert capture.recorder.backend(capture.handle, "local_vlm")
    capture.invocation("vlm").prepared([10, 11], [10, 11])
    result = detail(capture)
    assert result["coverage"]["evidence"]["prepared_tokens"] == "retained"
    assert result["coverage"]["evidence"]["prepared_text"] == "not_observed"
    assert not any(
        item["representation"] == "model_input" for item in result["representations"]
    )


def test_observed_content_loss_is_distinct_from_no_observation(capture, monkeypatch):
    monkeypatch.setattr(capture.recorder, "append_text", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        capture.recorder, "append_tokens", lambda *args, **kwargs: False
    )
    invocation = capture.invocation("lost")
    invocation.prepared("observed but not retained", [10, 11])
    invocation.output(
        SimpleNamespace(
            new_text="observed",
            completion_tokens=1,
            finished=True,
            error=None,
            finish_reason="stop",
        )
    )
    coverage = detail(capture)["coverage"]
    assert coverage["scheduler_path"] == "observed"
    assert all(
        coverage["evidence"][key] == "observed_not_retained"
        for key in ["prepared_text", "prepared_tokens", "producer_output"]
    )
    assert coverage["evidence"]["api_wire"] == "not_observed"


def test_backend_observations_are_bounded_immutable_scoped_and_fenced(capture):
    recorder = capture.recorder
    before = detail(capture)["version"]
    for _ in range(20):
        for kind in ["local_text", "local_vlm", "distributed_proxy", "unknown"]:
            assert recorder.backend(capture.handle, kind)
    assert not recorder.backend(capture.handle, "path-or-unrecognized-backend")
    assert not recorder.backend(capture.handle, {})
    after = detail(capture)
    assert after["version"] != before
    assert after["coverage"]["backend_kinds"] == [
        "local_text",
        "local_vlm",
        "distributed_proxy",
        "unknown",
    ]
    assert isinstance(
        recorder._pending[capture.handle.request_id].metadata["resolved_backends"],
        tuple,
    )
    assert recorder.backend(capture.handle, "local_text")
    assert detail(capture)["version"] == after["version"]
    capture.api_done("completed")
    assert not recorder.backend(capture.handle, "splash_native")
    assert (
        detail(capture)["coverage"]["backend_kinds"]
        == after["coverage"]["backend_kinds"]
    )


def test_old_or_early_records_do_not_assume_a_backend(capture):
    coverage = detail(capture)["coverage"]
    assert coverage["backend_kinds"] == []
    assert coverage["backend_source"] == "unavailable"
    assert coverage["scope"] == "recorded_evidence_only"


def test_backend_classification_keeps_no_engine_reference_and_does_not_guess_subclasses(
    capture,
):
    from omlx import server
    from omlx.engine import BatchedEngine

    class CustomEngine(BatchedEngine):
        pass

    engine = CustomEngine.__new__(CustomEngine)
    reference = weakref.ref(engine)
    server._observe_engine_backend(engine)
    del engine
    assert reference() is None
    assert detail(capture)["coverage"]["backend_kinds"] == ["unknown"]
