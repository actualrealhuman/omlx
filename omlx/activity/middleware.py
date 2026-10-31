# SPDX-License-Identifier: Apache-2.0
"""Outer transport observation; never buffer or consume an inference body."""

import asyncio

from .runtime import capture_enabled


class ActivityTransportMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not capture_enabled():
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["activity_transport_observed"] = True
        status, sent_end, interrupted, failed = 500, False, False, False

        async def observed_send(message):
            nonlocal status, sent_end
            capture = state.get("activity_capture")
            if message["type"] == "http.response.start":
                status = message["status"]
                if capture is not None:
                    headers = [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"x-omlx-request-id"
                    ]
                    headers.append(
                        (
                            b"x-omlx-request-id",
                            capture.handle.request_id.encode("ascii"),
                        )
                    )
                    message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and capture is not None:
                # Capture the actual ASGI bytes, including handled exceptions
                # produced after an observed endpoint raises. Delivery to a
                # client is still a separate transport outcome.
                capture.wire(message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                sent_end = True

        try:
            await self.app(scope, receive, observed_send)
        except (asyncio.CancelledError, GeneratorExit, OSError):
            interrupted = True
            raise
        except BaseException:
            failed = True
            raise
        finally:
            capture = state.get("activity_capture")
            if capture is not None:
                outcome = (
                    "error"
                    if failed
                    else "interrupted"
                    if interrupted or not sent_end
                    else "rejected"
                    if 400 <= status < 500
                    else "error"
                    if status >= 500
                    else "completed"
                )
                capture.transport_done(outcome)
