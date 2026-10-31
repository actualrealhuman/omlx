# SPDX-License-Identifier: Apache-2.0
"""Endpoint lifecycle and attribution checks without a model or server process."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.identity import credential_source
from omlx.activity.runtime import (
    RequestCapture,
    configure_recorder,
    current_capture,
    inference_request_id,
    observe_request,
)
from omlx.settings import GlobalSettings, SubKeyEntry


class Payload(BaseModel):
    model: str
    prompt: str = "hello"
    stream: bool = False


class RuntimeTests(unittest.TestCase):
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

    def app(self):
        app = FastAPI()

        async def authenticate(http_request: Request):
            if (
                http_request.headers.get("authorization")
                != "Bearer synthetic-credential"
            ):
                raise HTTPException(401)
            http_request.state.activity_source = ("source-a", "Example client")

        @observe_request
        async def endpoint(request: Payload, http_request: Request):
            if request.prompt == "reject":
                raise HTTPException(400, detail="synthetic rejection")
            if request.stream:

                async def body():
                    yield 'data: {"delta":"hello"}\n\n'
                    yield "data: [DONE]\n\n"

                return StreamingResponse(body())
            return JSONResponse(
                {
                    "answer": request.prompt,
                    "request_id": inference_request_id(http_request),
                }
            )

        for path in (
            "/v1/completions",
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/responses",
        ):
            app.post(path, dependencies=[Depends(authenticate)])(endpoint)
        return app

    def test_all_api_families_preserve_identity_source_and_semantic_input(self):
        with TestClient(self.app()) as client:
            for path in (
                "/v1/completions",
                "/v1/chat/completions",
                "/v1/messages",
                "/v1/responses",
            ):
                result = client.post(
                    path,
                    headers={"authorization": "Bearer synthetic-credential"},
                    json={
                        "model": "model-a",
                        "prompt": "hello",
                        "ignored_field": "not accepted",
                    },
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(
                    result.json()["request_id"], result.headers["x-omlx-request-id"]
                )
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["state"], "completed")
            self.assertEqual(row["source_name"], "Example client")
            body = self.recorder.reader.content(row["id"], "submitted")["data"].decode()
            self.assertEqual(json.loads(body), {"model": "model-a", "prompt": "hello"})
            self.assertNotIn("synthetic-credential", body)
            self.assertNotIn("ignored_field", body)

    def test_auth_and_validation_failures_do_not_capture_bodies(self):
        with TestClient(self.app()) as client:
            self.assertEqual(
                client.post("/v1/messages", json={"model": "a"}).status_code, 401
            )
            self.assertEqual(
                client.post(
                    "/v1/messages",
                    headers={"authorization": "Bearer synthetic-credential"},
                    json={"prompt": "missing model"},
                ).status_code,
                422,
            )
        self.assertEqual(self.rows(), [])

    def test_preflight_rejection_is_retained(self):
        with TestClient(self.app()) as client:
            result = client.post(
                "/v1/messages",
                headers={"authorization": "Bearer synthetic-credential"},
                json={"model": "a", "prompt": "reject"},
            )
            self.assertEqual(result.status_code, 400)
        row = self.rows()[0]
        self.assertEqual(row["state"], "rejected")
        self.assertEqual(result.headers["x-omlx-request-id"], row["id"])

    def test_stream_is_not_completed_when_response_object_is_returned(self):
        async def check():
            state = SimpleNamespace(activity_source=("source-a", "Example"))
            http = SimpleNamespace(
                state=state, url=SimpleNamespace(path="/v1/responses")
            )
            closed = []

            @observe_request
            async def endpoint(request, http_request):
                async def body():
                    try:
                        self.assertIsNotNone(current_capture())
                        yield "first"
                        yield "second"
                    finally:
                        closed.append(True)

                return StreamingResponse(body())

            response = await endpoint(request=Payload(model="a"), http_request=http)
            self.assertIsNone(current_capture())
            self.assertIsNone(self.rows()[0]["finished_at"])
            chunks = [chunk async for chunk in response.body_iterator]
            self.assertEqual(chunks, ["first", "second"])
            self.assertIsNone(current_capture())
            self.assertEqual(closed, [True])
            row = self.rows()[0]
            self.assertEqual(row["state"], "completed")
            self.assertEqual(
                self.recorder.reader.content(row["id"], "api_response")["data"],
                b"firstsecond",
            )

        asyncio.run(check())

    def test_stream_close_retains_partial_and_reports_interruption(self):
        async def check():
            http = SimpleNamespace(
                state=SimpleNamespace(), url=SimpleNamespace(path="/v1/messages")
            )
            closed = []

            @observe_request
            async def endpoint(request, http_request):
                async def body():
                    try:
                        yield "partial"
                        yield "unconsumed"
                    finally:
                        closed.append(True)

                return StreamingResponse(body())

            response = await endpoint(request=Payload(model="a"), http_request=http)
            self.assertEqual(await anext(response.body_iterator), "partial")
            await response.body_iterator.aclose()
            self.assertEqual(closed, [True])
            self.assertIsNone(current_capture())

        asyncio.run(check())
        row = self.rows()[0]
        self.assertEqual(row["state"], "interrupted")
        self.assertEqual(
            self.recorder.reader.content(row["id"], "api_response")["data"], b"partial"
        )

    def test_transport_done_waits_for_engine_and_retains_raw_model_input(self):
        handle = self.recorder.begin(model_id="a", endpoint="chat")
        capture = RequestCapture(self.recorder, handle)
        invocation = capture.invocation(
            "engine-a", prepared_text="<user>actual<assistant>"
        )
        invocation.prepared([1, 2, 3], [1, 2, 3])
        capture.api_done("completed")
        self.assertIsNone(self.rows()[0]["finished_at"])
        invocation.output(
            SimpleNamespace(
                new_text="raw", finished=True, error=None, finish_reason="stop"
            )
        )
        invocation.end()  # idempotent cleanup cannot alter completed outcome.
        row = self.rows()[0]
        self.assertEqual(row["state"], "completed")
        self.assertEqual(
            self.recorder.reader.content(row["id"], "model_input", invocation_id="1")[
                "data"
            ],
            b"<user>actual<assistant>",
        )
        self.assertEqual(
            json.loads(
                self.recorder.reader.content(
                    row["id"], "model_tokens", invocation_id="1"
                )["data"]
            ),
            [1, 2, 3],
        )
        self.assertEqual(
            self.recorder.reader.content(row["id"], "raw_output", invocation_id="1")[
                "data"
            ],
            b"raw",
        )

    def test_disabled_fast_path_does_not_export_request_or_modify_response(self):
        configure_recorder(None)

        async def check():
            response = JSONResponse({"answer": "ok"})

            @observe_request
            async def endpoint(request, http_request):
                self.assertIsNone(current_capture())
                return response

            result = await endpoint(request=object(), http_request=object())
            self.assertIs(result, response)
            self.assertNotIn("x-omlx-request-id", result.headers)

        asyncio.run(check())
        self.assertEqual(self.rows(), [])

    def test_credential_ids_roundtrip_and_migration_preserves_saved_configuration(self):
        root = Path(self.temp.name)
        path = root / "settings.json"
        legacy = {
            "auth": {
                "api_key": "saved-main",
                "sub_keys": [{"key": "saved-sub", "name": "Example"}],
            },
            "server": {"port": 8123},
            "unrelated": {"keep": True},
        }
        path.write_text(json.dumps(legacy))
        settings = GlobalSettings(base_path=root)
        settings._load_from_file(path)
        identity = settings.auth.sub_keys[0].id
        settings.auth.api_key = "runtime-only-override"
        settings.server.port = 9000
        settings.ensure_activity_source_ids()
        saved = json.loads(path.read_text())
        self.assertEqual(saved["auth"]["sub_keys"][0]["id"], identity)
        self.assertEqual(saved["auth"]["api_key"], "saved-main")
        self.assertEqual(saved["server"]["port"], 8123)
        self.assertEqual(saved["unrelated"], {"keep": True})
        self.assertEqual(
            credential_source("saved-sub", "saved-main", settings.auth.sub_keys),
            (identity, "Example"),
        )
        self.assertEqual(
            credential_source("saved-main", "saved-main", settings.auth.sub_keys),
            ("main", "Main key"),
        )
        self.assertEqual(
            credential_source("unknown", "saved-main", settings.auth.sub_keys),
            ("unknown", "Unknown"),
        )
        restored = GlobalSettings(base_path=root)
        restored._load_from_file(path)
        self.assertEqual(restored.auth.sub_keys[0].id, identity)
        restored.auth.sub_keys[0].name = "Renamed"
        self.assertEqual(restored.auth.sub_keys[0].id, identity)
        self.assertNotEqual(SubKeyEntry(key="replacement").id, identity)


if __name__ == "__main__":
    unittest.main()
