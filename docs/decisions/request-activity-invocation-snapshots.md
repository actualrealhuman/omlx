# Budgeted Activity invocation snapshots

One API request may own multiple scheduler generations. Request-wide token and
lifecycle metadata cannot describe each generation, while reconstructing that
state from a possibly truncated content/event stream would invent certainty.

Persist bounded immutable invocation updates separately from content chunks,
through the existing single worker. Keep the latest request-owned row per
invocation with transactional migration and cascading retention. Rich details
share bounded capture admission and explicitly allow gaps. Protected aggregate
request counters remain independent of detail retention.

Reserve temporary queue/writer buffers and live observer state before admission;
do not place hundreds of nested mutable records inside the fixed request metadata
slot. Keep live state reservations until the object is released, even when the
capture is deleted, stopped or its writer fails. Coalesce producer progress and
attempt immediate lifecycle snapshots instead of writing a row per token.

Capture named CPU admission parameters and bounded stop values, with omission
markers. Never retain an engine, compiled grammar, device callback or arbitrary
object representation. Core admission is not proof of final backend settings,
pre-parser text or a reconstructed attended context.

Use full detail reads for options and engine mappings. Live delivery carries
compact summaries. When the observation ends without a retained engine terminal
update, report observation-ended and the last observed phase; do not fabricate
completion, abort or failure. Real-backend overhead and browser interaction still
require explicit qualification.
