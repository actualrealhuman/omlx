# Observe scheduler pauses without changing execution

Activity keeps scheduler pause/retry observations on the existing invocation.
A retry is not a new user request or generation. Its known phase remains
separate from a bounded pause annotation; unsupported phases and causes are
not inferred from elapsed time, queue membership or aggregate model activity.

Authoritative queued and in-flight prefill eviction paths record one pause.
The inner external-prefill exception only preserves progress and re-raises, so
it is not counted again. Actual memory-pressure, overflow, cache-recovery,
batch-reset and bounded OOM requeues have fixed reason labels. Repeated pause
calls within one outstanding cycle are idempotent. Counts saturate at a fixed
integer ceiling; timestamps use receipt-relative monotonic elapsed time.

Resume is observed at successful admission/insertion or successful prefill
advancement. Packed companions use their existing completion loop; an unobserved
head does not hide progress in an observed companion. Eviction callback completion,
preparation, repeated old progress and error-only output do not prove resume.
Terminal completion clears the current pause without inventing a resume event.
Stopped observation retains last-observed evidence and leaves current state unknown.

Hooks are best-effort CPU observations. They do not alter queue/cache decisions,
and observer failures are ignored by scheduling. Unobserved paths do not allocate
progress maps or perform added packing scans. The existing fixed observer,
snapshot and request metadata reservations still govern admission. Structural
fixtures cover expanded fixed storage and encoded snapshot size; these do not
measure total process RSS or serving overhead.

Compact live summaries and full details share a whitelist. The browser scopes
pause labels to individual generations, including an observation-ended distinction;
it never derives a request-wide paused state from one paused sibling. Repeated
preparation retains the existing explicit capture-gap behavior rather than
fabricating a newly effective context. Actual backend/endpoint fidelity and
performance require their own qualification.
