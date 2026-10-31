# SPDX-License-Identifier: Apache-2.0
"""Transport completion is independent of response iterator consumption."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.middleware import ActivityTransportMiddleware
from omlx.activity.runtime import RequestCapture, configure_recorder, observe_request


class Payload(BaseModel):
    model: str


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recorder = ActivityRecorder(
            Path(self.temp.name) / "activity.sqlite3", ActivitySettings(enabled=True)
        )
        self.assertTrue(self.recorder.wait_ready())
        previous = configure_recorder(self.recorder)
        self.addCleanup(lambda: configure_recorder(previous))
        self.addCleanup(self.recorder.close)

    def rows(self):
        self.assertTrue(self.recorder.flush())
        return self.recorder.reader.list_requests()

    def test_handled_custom_exception_uses_actual_http_outcome_and_activity_header(
        self,
    ):
        app = FastAPI()
        app.add_middleware(ActivityTransportMiddleware)

        class RejectedError(Exception):
            pass

        @app.exception_handler(RejectedError)
        async def reject_handler(request, exc):
            return JSONResponse(
                {"detail": "synthetic preflight rejection"}, status_code=400
            )

        @app.post("/v1/messages")
        @observe_request
        async def endpoint(request: Payload, http_request: Request):
            raise RejectedError()

        with TestClient(app) as client:
            response = client.post("/v1/messages", json={"model": "a"})
        self.assertEqual(response.status_code, 400)
        row = self.rows()[0]
        self.assertEqual(row["state"], "rejected")
        self.assertEqual(response.headers["x-omlx-request-id"], row["id"])

    def test_body_never_started_does_not_leak_an_active_record(self):
        @observe_request
        async def endpoint(request, http_request):
            async def body():
                raise AssertionError("body must not be consumed")
                yield "never"

            return StreamingResponse(body())

        async def app(scope, receive, send):
            http_request = Request(scope)
            payload = Payload(model="a")
            response = await endpoint(request=payload, http_request=http_request)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": response.raw_headers,
                }
            )

        async def nothing(*args):
            return None

        asyncio.run(
            ActivityTransportMiddleware(app)(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/messages",
                    "headers": [],
                },
                nothing,
                nothing,
            )
        )
        self.assertEqual(self.rows()[0]["state"], "interrupted")

    def test_http_transport_does_not_claim_engine_stopped_before_abort_applies(self):
        held = []

        async def app(scope, receive, send):
            handle = self.recorder.begin(model_id="a", endpoint="chat")
            capture = RequestCapture(self.recorder, handle, require_transport=True)
            scope["state"]["activity_capture"] = capture
            held.append(capture.invocation("engine-a"))
            capture.api_done("completed")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise OSError("synthetic disconnect")

        async def nothing(*args):
            return None

        async def check():
            with self.assertRaises(OSError):
                await ActivityTransportMiddleware(app)(
                    {"type": "http", "state": {}}, nothing, nothing
                )

        asyncio.run(check())
        self.assertIsNone(self.rows()[0]["finished_at"])
        held[0].end("cancelled")
        self.assertEqual(self.rows()[0]["state"], "cancelled")

    def test_disabled_transport_is_a_direct_passthrough(self):
        configure_recorder(None)
        scope = {"type": "http"}
        calls = []

        async def nothing(*args):
            return None

        async def app(inner, receive, send):
            self.assertIs(inner, scope)
            self.assertIs(receive, nothing)
            self.assertIs(send, nothing)
            calls.append(True)

        asyncio.run(ActivityTransportMiddleware(app)(scope, nothing, nothing))
        self.assertNotIn("state", scope)
        self.assertEqual(calls, [True])

    def test_actual_asgi_body_fragments_are_retained_once_even_when_utf8_is_split(self):
        async def check():
            handle = self.recorder.begin(model_id="a", endpoint="chat")
            capture = RequestCapture(self.recorder, handle, require_transport=True)
            content = "wire 世界".encode()
            fragments = [content[:6], content[6:8], content[8:]]
            sent = []

            async def app(scope, receive, send):
                scope["state"]["activity_capture"] = capture
                capture.api_done("completed")
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                for index, fragment in enumerate(fragments):
                    await send(
                        {
                            "type": "http.response.body",
                            "body": fragment,
                            "more_body": index < 2,
                        }
                    )

            async def send(message):
                if message["type"] == "http.response.body":
                    sent.append(message["body"])

            await ActivityTransportMiddleware(app)({"type": "http"}, None, send)
            self.assertEqual(self.rows()[0]["state"], "completed")
            self.assertEqual(
                self.recorder.reader.content(handle.request_id, "api_response")["data"],
                b"".join(sent),
            )

        asyncio.run(check())

    def test_retained_wire_attempt_is_not_claimed_as_successful_client_delivery(self):
        async def check():
            handle = self.recorder.begin(model_id="a", endpoint="chat")
            capture = RequestCapture(self.recorder, handle, require_transport=True)

            async def app(scope, receive, send):
                scope["state"]["activity_capture"] = capture
                capture.api_done("completed")
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"attempted",
                        "more_body": False,
                    }
                )

            async def send(message):
                if message["type"] == "http.response.body":
                    raise OSError("synthetic send failure")

            with self.assertRaises(OSError):
                await ActivityTransportMiddleware(app)({"type": "http"}, None, send)
            self.assertEqual(self.rows()[0]["state"], "interrupted")
            self.assertEqual(
                self.recorder.reader.content(handle.request_id, "api_response")["data"],
                b"attempted",
            )

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
