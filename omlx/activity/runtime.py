# SPDX-License-Identifier: Apache-2.0
"""Request-context hooks without imports of engines, FastAPI or device libraries."""

import asyncio
import json
import math
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .recorder import ActivityRecorder
from .store import MAX_CHUNK_BYTES

_recorder: ActivityRecorder | None = None
_current: ContextVar["RequestCapture | None"] = ContextVar(
    "omlx_activity", default=None
)
_SCHEDULER_PAUSE_REASONS = frozenset(
    {
        "prefill_eviction",
        "memory_requeue",
        "generation_overflow",
        "cache_recovery",
        "batch_reset",
        "oom_retry",
    }
)


def get_recorder():
    return _recorder


def capture_enabled():
    recorder = _recorder
    return recorder is not None and recorder.settings.enabled


def configure_recorder(recorder):
    """Startup/shutdown or serialized admin control; close replaced writers explicitly."""
    global _recorder
    previous, _recorder = _recorder, recorder
    return previous


def current_capture():
    return _current.get()


@contextmanager
def without_capture():
    """Detach internal work from the caller's observation; preserve other context."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def api_failure(outcome="error"):
    """Signal an error the API actually emits; do not inspect response bytes."""
    capture = _current.get()
    return capture.api_failure(outcome) if capture is not None else False


def inference_request_id(http_request):
    state = getattr(http_request, "state", None)
    capture = getattr(state, "activity_capture", None)
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

    def __init__(self, recorder, handle, *, require_transport=False):
        self.recorder, self.handle = recorder, handle
        self._next_invocation = 0
        self._active = 0
        self._api_outcome = None
        self._engine_outcomes = set()
        self._api_failures = set()
        self._lock = threading.Lock()
        self._finalized = False
        self._require_transport = require_transport
        self._transport_done = False
        self._started = time.monotonic()

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

    def invocation(self, engine_request_id, *, prepared_text=None, options=None):
        with self._lock:
            if self._next_invocation >= 256 or self._api_outcome is not None:
                self.recorder.gap(self.handle, "invocation_limit_or_closed_request")
                return None
            if not self.recorder.reserve_invocation(self.handle):
                return None
            self._next_invocation += 1
            self._active += 1
            result = InvocationCapture(self, str(self._next_invocation), prepared_text)
            self.recorder.metrics(self.handle, counts={"observed_invocations": 1})
            admitted_ms = (time.monotonic() - self._started) * 1000
            self.recorder.invocation_snapshot(
                self.handle,
                result.id,
                state="admitted",
                since_receipt_ms=admitted_ms,
                admitted_at_ms=admitted_ms,
                engine_request_id=engine_request_id,
                options=options or (),
                currently_paused=False,
                last_observed_paused=None,
                pause_count=0,
                resume_count=0,
            )
        self.text(
            "events",
            json.dumps(
                {
                    "type": "invocation",
                    "invocation_id": result.id,
                    "engine_request_id": engine_request_id[:128]
                    if isinstance(engine_request_id, str)
                    else None,
                    "backend": "local_scheduler_core",
                    "sampling_subset": {
                        key: value
                        for key, value in (options or ())
                        if (
                            value is None
                            or type(value) is bool
                            or (type(value) is int and -(2**63) < value < 2**63)
                            or (type(value) is float and math.isfinite(value))
                        )
                    },
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

    def api_failure(self, outcome="error"):
        if outcome not in {"error", "rejected", "cancelled"}:
            return False
        with self._lock:
            if self._finalized:
                return False
            self._api_failures.add(outcome)
            strongest = next(
                value
                for value in ("rejected", "error", "cancelled")
                if value in self._api_failures
            )
            return self.recorder.api_failure(self.handle, strongest)

    def transport_done(self, outcome):
        with self._lock:
            self._transport_done = True
            self._api_outcome = outcome
            final = self._final_outcome()
        if final is not None:
            self.recorder.finish(self.handle, final)

    def _final_outcome(self):
        if (
            self._api_outcome is None
            or self._active
            or self._finalized
            or (self._require_transport and not self._transport_done)
        ):
            return
        self._finalized = True
        outcomes = self._engine_outcomes | self._api_failures | {self._api_outcome}
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
        self._prepared = False
        self._output_tokens = 0
        self._counter_seen = False
        self._counter_regressed = False
        self._prefill_seen = False
        self._phase = "admitted"
        self._currently_paused = False
        self._pause_count = 0
        self._resume_count = 0
        self._last_pause_reason = None
        self._last_pause_ms = None
        self._last_resume_ms = None
        self._last_snapshot = float("-inf")
        self._first_token_ms = None
        self._first_text_ms = None
        weakref.finalize(self, request.recorder.release_invocation)

    def _event(self, kind, **values):
        elapsed = round((time.monotonic() - self.request._started) * 1000, 3)
        phase = {"prepared": "queued", "prefilling": "prefilling"}.get(kind)
        if phase is not None:
            self._phase = phase
        elif kind == "invocation_end":
            phase = values.get("outcome", self._phase)
        else:
            phase = self._phase
        timing = {
            "prepared": "prepared_at_ms",
            "prefilling": "prefill_at_ms",
            "invocation_end": "finished_at_ms",
        }.get(kind)
        self.request.recorder.invocation_snapshot(
            self.request.handle,
            self.id,
            state=phase,
            since_receipt_ms=elapsed,
            **({timing: elapsed} if timing else {}),
            **values,
        )
        self.request.text(
            "events",
            json.dumps(
                {
                    "type": kind,
                    "invocation_id": self.id,
                    "state": phase,
                    "since_receipt_ms": elapsed,
                    **values,
                }
            )
            + "\n",
        )

    def pause(self, reason):
        """Record one bounded scheduler pause without changing invocation phase."""
        if type(reason) is not str or reason not in _SCHEDULER_PAUSE_REASONS:
            return False
        with self.request._lock:
            if self.done or self._currently_paused:
                return False
            self._currently_paused = True
            self._pause_count = min(self._pause_count + 1, 2**31 - 1)
            self._last_pause_reason = reason
            self._last_pause_ms = round(
                (time.monotonic() - self.request._started) * 1000, 3
            )
            self._event(
                "scheduler_paused",
                currently_paused=True,
                pause_count=self._pause_count,
                resume_count=self._resume_count,
                last_pause_reason=self._last_pause_reason,
                last_pause_ms=self._last_pause_ms,
                last_resume_ms=self._last_resume_ms,
            )
            return True

    def resume(self):
        """Record resumed work only when a scheduler hook observes real progress."""
        with self.request._lock:
            if self.done or not self._currently_paused:
                return False
            self._currently_paused = False
            self._resume_count = min(self._resume_count + 1, 2**31 - 1)
            self._last_resume_ms = round(
                (time.monotonic() - self.request._started) * 1000, 3
            )
            self._event(
                "scheduler_resumed",
                currently_paused=False,
                pause_count=self._pause_count,
                resume_count=self._resume_count,
                last_pause_reason=self._last_pause_reason,
                last_pause_ms=self._last_pause_ms,
                last_resume_ms=self._last_resume_ms,
            )
            return True

    def prepared(self, prompt, token_ids):
        with self.request._lock:
            if self.done:
                return
            if self._prepared:
                self.request.recorder.gap(
                    self.request.handle, "prepared_input_repeated"
                )
                return
            self._prepared = True
            text = self.prepared_text if self.prepared_text is not None else prompt
            if isinstance(text, str):
                self.request.text("model_input", text, invocation_id=self.id)
            self.request.recorder.append_tokens(
                self.request.handle, token_ids, invocation_id=self.id
            )
            self.request.recorder.metrics(
                self.request.handle,
                counts={
                    "prepared_invocations": 1,
                    "prepared_text_invocations": int(isinstance(text, str)),
                    "observed_input_tokens": len(token_ids),
                },
            )
            self.prepared_text = None
            self.request.recorder.state(self.request.handle, "queued")
            self._event(
                "prepared",
                input_tokens=len(token_ids),
                prepared_text=isinstance(text, str),
            )

    def prefill(self):
        with self.request._lock:
            if not self.done:
                if not self._prefill_seen:
                    self._prefill_seen = True
                    self.request.recorder.metrics(
                        self.request.handle, counts={"prefilling_invocations": 1}
                    )
                    self._event("prefilling")
                self.request.recorder.state(self.request.handle, "prefilling")

    def output(self, output):
        # Observe every producer output, before stream-interval aggregation and
        # API parser cleanup. No cumulative-output comparison or token decoding.
        with self.request._lock:
            if self.done:
                return
            tokens = getattr(output, "completion_tokens", None)
            counts, times = {}, {}
            elapsed = (time.monotonic() - self.request._started) * 1000
            known_counter = type(tokens) is int and 0 <= tokens < 2**63
            if known_counter:
                self._counter_seen = True
                if tokens < self._output_tokens and not self._counter_regressed:
                    self._counter_regressed = True
                    self.request.recorder.gap(
                        self.request.handle, "output_counter_regressed"
                    )
                counts["observed_generated_tokens"] = max(
                    0, tokens - self._output_tokens
                )
                self._output_tokens = max(tokens, self._output_tokens)
                if tokens:
                    times["first_observed_token_ms"] = elapsed
                    if self._first_token_ms is None:
                        self._first_token_ms = elapsed
            if output.new_text and not output.error:
                self.request.text("raw_output", output.new_text, invocation_id=self.id)
                times["first_observed_text_ms"] = elapsed
                if self._first_text_ms is None:
                    self._first_text_ms = elapsed
            if (known_counter and tokens) or (output.new_text and not output.error):
                self._phase = "generating"
                self.request.recorder.state(self.request.handle, "generating")
            self.request.recorder.metrics(
                self.request.handle, counts=counts, times=times
            )
            if (
                elapsed - self._last_snapshot >= 500
                and not output.finished
                and (
                    (known_counter and tokens) or (output.new_text and not output.error)
                )
            ):
                self._snapshot_output(elapsed)
        if output.finished:
            outcome = (
                "cancelled"
                if output.finish_reason == "abort"
                else "error"
                if output.error
                else "completed"
            )
            self.end(outcome, finish_reason=output.finish_reason)

    def _snapshot_output(self, elapsed):
        self._last_snapshot = elapsed
        self.request.recorder.invocation_snapshot(
            self.request.handle,
            self.id,
            state="generating",
            since_receipt_ms=elapsed,
            generated_tokens=self._output_tokens if self._counter_seen else None,
            first_token_ms=self._first_token_ms,
            first_text_ms=self._first_text_ms,
            counter_regressed=self._counter_regressed,
        )

    def end(self, outcome="interrupted", *, finish_reason=None):
        with self.request._lock:
            if self.done:
                return
            self.done = True
            self._currently_paused = False
            self.prepared_text = None
            self.request._active -= 1
            self.request._engine_outcomes.add(outcome)
            self._event(
                "invocation_end",
                outcome=outcome,
                generated_tokens=self._output_tokens if self._counter_seen else None,
                finish_reason=finish_reason[:128]
                if isinstance(finish_reason, str)
                else None,
                first_token_ms=self._first_token_ms,
                first_text_ms=self._first_text_ms,
                counter_regressed=self._counter_regressed,
                currently_paused=False,
                pause_count=self._pause_count,
                resume_count=self._resume_count,
                last_pause_reason=self._last_pause_reason,
                last_pause_ms=self._last_pause_ms,
                last_resume_ms=self._last_resume_ms,
            )
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
        capture = RequestCapture(
            recorder,
            handle,
            require_transport=bool(
                getattr(http_request.state, "activity_transport_observed", False)
            ),
        )
        http_request.state.activity_capture = capture
        token = _current.set(capture)
        try:
            recorder.capture_submitted(handle, request)
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
                            if not capture._require_transport:
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
                if not capture._require_transport:
                    capture.wire(response.body)
                capture.api_done(outcome)
            return response
        finally:
            _current.reset(token)

    return observed
