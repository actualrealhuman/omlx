# SPDX-License-Identifier: Apache-2.0
"""Request-context hooks without imports of engines, FastAPI or device libraries."""

import asyncio
import json
import threading
import uuid
from contextvars import ContextVar
from functools import wraps

from .recorder import ActivityRecorder
from .store import MAX_CHUNK_BYTES

_recorder: ActivityRecorder | None = None
_current: ContextVar["RequestCapture | None"] = ContextVar(
    "omlx_activity", default=None
)


def get_recorder():
    return _recorder


def configure_recorder(recorder):
    """Startup/shutdown only; callers close the replaced writer explicitly."""
    global _recorder
    previous, _recorder = _recorder, recorder
    return previous


def current_capture():
    return _current.get()


def inference_request_id(http_request):
    capture = getattr(http_request.state, "activity_capture", None)
    return capture.handle.request_id if capture is not None else str(uuid.uuid4())


def phase(state, *, model_id=None):
    capture = _current.get()
    if capture is not None:
        try:
            capture.recorder.state(capture.handle, state, model_id=model_id)
        except ValueError:
            capture.recorder.gap(capture.handle, "model_metadata_unavailable")


class RequestCapture:
    """Small request/invocation state; owns no engine, lease or device arrays."""

    def __init__(self, recorder, handle):
        self.recorder, self.handle = recorder, handle
        self._next_invocation = 0
        self._active = 0
        self._api_outcome = None
        self._engine_outcomes = set()
        self._lock = threading.Lock()
        self._finalized = False

    def text(self, representation, value, *, invocation_id="0"):
        if value:
            self.recorder.append_text(
                self.handle, representation, value, invocation_id=invocation_id
            )

    def wire(self, value):
        if isinstance(value, str):
            self.text("api_response", value)
        elif isinstance(value, bytes):
            for start in range(0, len(value), MAX_CHUNK_BYTES):
                if not self.recorder.append(
                    self.handle, "api_response", value[start : start + MAX_CHUNK_BYTES]
                ):
                    break

    def invocation(self, engine_request_id, *, prepared_text=None):
        with self._lock:
            if self._next_invocation >= 256 or self._api_outcome is not None:
                self.recorder.gap(self.handle, "invocation_limit_or_closed_request")
                return None
            self._next_invocation += 1
            self._active += 1
            result = InvocationCapture(self, str(self._next_invocation), prepared_text)
        self.text(
            "events",
            json.dumps(
                {
                    "type": "invocation",
                    "invocation_id": result.id,
                    "engine_request_id": engine_request_id,
                }
            )
            + "\n",
        )
        return result

    def api_done(self, outcome):
        with self._lock:
            self._api_outcome = outcome
            final = self._final_outcome()
        if final is not None:
            self.recorder.finish(self.handle, final)

    def _final_outcome(self):
        if self._api_outcome is None or self._active or self._finalized:
            return
        self._finalized = True
        outcomes = self._engine_outcomes | {self._api_outcome}
        outcome = next(
            (
                value
                for value in ("rejected", "error", "cancelled", "interrupted")
                if value in outcomes
            ),
            "completed",
        )
        return outcome


class InvocationCapture:
    def __init__(self, request, invocation_id, prepared_text):
        self.request, self.id = request, invocation_id
        self.prepared_text = prepared_text
        self.done = False

    def prepared(self, prompt, token_ids):
        text = self.prepared_text if self.prepared_text is not None else prompt
        if isinstance(text, str):
            self.request.text("model_input", text, invocation_id=self.id)
        self.request.recorder.append_tokens(
            self.request.handle, token_ids, invocation_id=self.id
        )
        self.prepared_text = None
        self.request.recorder.state(self.request.handle, "queued")

    def output(self, output):
        # Observe every producer output, before stream-interval aggregation and
        # API parser cleanup. No cumulative-output comparison or token decoding.
        if output.new_text and not output.error:
            self.request.text("raw_output", output.new_text, invocation_id=self.id)
            self.request.recorder.state(self.request.handle, "generating")
        if output.finished:
            outcome = (
                "cancelled"
                if output.finish_reason == "abort"
                else "error"
                if output.error
                else "completed"
            )
            self.end(outcome)

    def end(self, outcome="interrupted"):
        with self.request._lock:
            if self.done:
                return
            self.done = True
            self.prepared_text = None
            self.request._active -= 1
            self.request._engine_outcomes.add(outcome)
            final = self.request._final_outcome()
        if final is not None:
            self.request.recorder.finish(self.request.handle, final)


def observe_request(function):
    """Wrap an already authenticated, validated endpoint; consume no extra stream."""

    @wraps(function)
    async def observed(*args, **kwargs):
        recorder = get_recorder()
        if recorder is None or not recorder.settings.enabled:
            return await function(*args, **kwargs)
        http_request, request = kwargs.get("http_request"), kwargs.get("request")
        if http_request is None or request is None:
            return await function(*args, **kwargs)
        source_id, source_name = getattr(
            http_request.state, "activity_source", ("unknown", "Unknown")
        )
        handle = recorder.begin(
            model_id=request.model or "<default>",
            endpoint=http_request.url.path,
            source_id=source_id,
            source_name=source_name,
        )
        if handle is None:
            return await function(*args, **kwargs)
        capture = RequestCapture(recorder, handle)
        http_request.state.activity_capture = capture
        token = _current.set(capture)
        try:
            raw_body = getattr(http_request, "_body", b"")
            if len(raw_body) > recorder.settings.max_request_bytes:
                recorder.gap(handle, "submitted_input_size_limit")
            else:
                try:
                    capture.text(
                        "submitted", request.model_dump_json(exclude_unset=True)
                    )
                except Exception:
                    recorder.gap(handle, "submitted_input_unavailable")
            recorder.state(handle, "preparing")
            try:
                response = await function(*args, **kwargs)
            except BaseException as exc:
                status = getattr(exc, "status_code", 500)
                if hasattr(exc, "status_code") and hasattr(exc, "headers"):
                    exc.headers = {
                        **(exc.headers or {}),
                        "X-oMLX-Request-ID": handle.request_id,
                    }
                capture.api_done(
                    "interrupted"
                    if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                    else "rejected"
                    if 400 <= status < 500
                    else "error"
                )
                raise
            outcome = (
                "rejected"
                if 400 <= response.status_code < 500
                else "error"
                if response.status_code >= 500
                else "completed"
            )
            response.headers["X-oMLX-Request-ID"] = handle.request_id
            if hasattr(response, "body_iterator"):
                iterator = response.body_iterator

                async def observed_body():
                    body_token = _current.set(capture)
                    body_outcome = outcome
                    try:
                        async for chunk in iterator:
                            capture.wire(chunk)
                            yield chunk
                    except BaseException as exc:
                        body_outcome = (
                            "interrupted"
                            if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                            else "error"
                        )
                        raise
                    finally:
                        try:
                            closer = getattr(iterator, "aclose", None)
                            if closer is not None:
                                try:
                                    await closer()
                                except BaseException:
                                    body_outcome = "error"
                                    raise
                        finally:
                            capture.api_done(body_outcome)
                            _current.reset(body_token)

                response.body_iterator = observed_body()
            else:
                capture.wire(response.body)
                capture.api_done(outcome)
            return response
        finally:
            _current.reset(token)

    return observed
