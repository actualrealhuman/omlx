# Activity API body observation

Record body messages at the existing Activity transport middleware's ASGI send
boundary when it is present. Endpoint-returned response wrappers cannot observe
handled error responses constructed after an endpoint raises; recording in both
places would duplicate successful output. Keep wrapper capture as the fallback
for direct use without transport observation.

Treat retained body bytes as outgoing attempts, independently from transport
completion and engine outcomes. Sending may fail after bytes were observed.
Preserve the original request after authenticated/schema-valid admission;
unauthenticated and malformed requests still do not create content history.

Do not reopen terminal, disabled or deleted captures to append a body generated
outside the observer. Starlette's outer unhandled-error response is such a case:
retain its known error outcome and request-ID correlation while reporting body
evidence unavailable. Keep generic 500 response content unchanged. Full middleware
composition, outer compression and real network delivery are separate evidence
boundaries, not inferred from a retained API body.
