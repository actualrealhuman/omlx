# Model execution turns and activity limits

Model scheduling is opt-in. With `server.inference_scheduling_enabled` enabled,
participating local batched text and vision-language engines execute one model
session at a time. Several models may remain resident, and the selected model
retains continuous batching. Unfinished requests and their KV/generator state
remain resident between turns; turning a model's execution off does not release
its request lifetime or eviction protection.

The initial contested turn is five seconds of monotonic wall time, configured
by `server.inference_scheduling_quantum_seconds` (0.1–60). Short engine bursts
remain short so a turn does not buffer five seconds of output. With no peer
waiting, useful work continues. Equal-rank models rotate under contention;
arrivals do not reset the owner's clock. A safe indivisible operation can
overrun a turn: this is cooperative scheduling, not kernel preemption or a
hard response-time guarantee.

Models have these persisted settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `inference_allowance` | `null` | Full class allowance, or a relative fraction from 0 to 1 |
| `inference_share` | `null` | Legacy absolute activity ceiling, retained for explicit compatibility and rollback |
| `inference_priority` | `0` | Higher integer priorities (0–100) run before lower priorities |
| `inference_exclusive` | `false` | Interactive class, selected before Background models regardless of their numeric priority |

The global ceiling is `server.inference_share` (0.01–1). The two relative class
allowances are `server.inference_interactive_allowance` and
`server.inference_background_allowance` (0–1, both defaulting to 1). A configured
model maximum is the global ceiling multiplied by its class allowance and model
allowance. For example, a global limit of 80%, class allowance of 50% and model
allowance of 75% yields a configured ceiling of 30%. The class and model numbers
retain their denominators when a parent changes. These ceilings describe each
model's covered activity when it runs; they are not summed reservations for
simultaneously running models.

Within the interactive class, numeric priority and equal-priority turns still
apply. Priority attaches to actual runnable work, not merely loaded weights.
Continuous higher-rank work can starve lower ranks; there is no hidden minimum
background service that weakens interactive exclusivity. The execution policy
does not classify requests as human or agent work, and adds no public inference
request argument. Those can be considered independently later.

While the master controls are enabled, the global maximum constrains every
model. Pacing retains one persistent
global controller plus model-local controllers, accounting the same actual
submission intervals. Model switches and preference changes do not reset the
global controller. Admission waits, model-turn waits and battery suspension are
excluded from model-cost learning; they remain part of end-to-end latency.
These settings regulate best-effort submission activity, not electrical watts,
a fixed fraction of token throughput, or reserved hardware capacity.

Battery policy remains authoritative. The relative configured maximum is
composed once before evaluating the reserve curve. Battery restrictions,
outage caps, suspension, hysteresis and controlled restoration may reduce it;
none may increase a smaller configured maximum. Saved relative allowances
survive temporary battery restrictions. A zero class or model allowance pauses
covered generation while retaining cancellation and maintenance. It cannot
hold a model turn and block positive-allowance peers. Positive composed maxima
below 10% retain their value instead of being rounded up to the former pacing
minimum. Safe indivisible work can still overrun cooperative timing windows.

Legacy absolute settings are migrated before configuring engines. A verified
backup precedes changes, the migration is versioned and repeatable, and a failed
migration stops startup with the originals retained. At the migration's global
and class maximum, a legacy absolute preference is converted to a relative
allowance that preserves its current configured ceiling. Subsequent parent
changes intentionally scale it. Generation profiles and templates remain independent of model scheduling;
changing a generation preset cannot reset a model allowance.
The legacy API field retains its absolute meaning and must be explicitly
converted; a request cannot ambiguously supply both absolute and relative
preferences. A relative reset restores full class allowance rather than
reviving an archived absolute preference.

Ownership is independent of the full-speed pacing bypass. A transfer stops new
owner submissions, completes the owner's stream drain, then grants the next
model. A failed drain prevents further exclusive grants. Cancelling an async
waiter does not release a still-running worker's ownership. Long multimodal
prefills checkpoint after completed chunks outside transient memory guards;
request abort cleanup reacquires a maintenance slot, which remains eligible
during battery suspension. Vision preparation and synchronous generation use
the same registered model session. Auxiliary drafting belongs to its target
session. Paused model activity must not be reported as active device competition.

This scope does not establish server-wide GPU exclusivity. Audio, embeddings,
reranking, decision, diffusion, distributed and separate-process inference,
model loading and independent background work are outside the model-turn
contract. Memory pressure can still prevent keeping all weights and KV states
resident; scheduling does not implement eviction and later reconstruction of
an interrupted model's state.

The dashboard uses one chart as its control surface. The global limit is
violet, Interactive green and Background teal; labels and distinct limit marks
carry the meaning without relying on color alone. Whole slider trays support
taps and horizontal drags while preserving vertical touch scrolling, native
keyboard controls and accessible value descriptions. Pointer-only endpoint
snapping does not change exact keyboard values. The chart groups models by
class and orders them by descending priority, with manual equal-priority
settings available separately. Models enter the panel when explicitly selected
or observed loaded by that browser. Observed models remain after unloading;
explicit hiding overrides automatic inclusion, even while loaded. Favorites
and custom scheduling policies do not imply panel membership. A searchable
installed-model checklist permits inclusion before loading. Selection state
is versioned in browser storage, migrating only deliberate legacy panel pins.
This presentation choice never loads a model or changes its scheduling policy.

Editable percentages sit beside their control headings. Derived ceilings use
at most one decimal place, with positive values below 0.1% shown as <0.1%;
editable values retain their precision. Slider surfaces use semantic theme
tokens. Interface previews render the full dashboard base and production
stylesheets in their original cascade order, including readability overrides,
so isolated component wrappers cannot hide layout or theme conflicts.

Each relative allowance is shown as an equation: editable percentage of its
parent's limit equals the derived limit. A single expandable guide explains
composition, serial turns, priority, battery restrictions and panel selection.
The persisted `server.inference_controls_enabled` master control defaults to
true for existing installations. Disabling it preserves global, class, model
and serial preferences while bypassing their enforcement. Battery protection
remains independent, including hard suspension and controlled recovery. Saved
limits are labelled as saved while bypassed; re-enabling restores their
enforcement. Changes made through other settings interfaces while bypassed
update the saved preferences for later use. Disabling serial turns remains
cooperative: already-submitted work and maintenance retain their lifetime and
cancellation guarantees.

The dashboard waits for the server's persistence acknowledgement before
changing the master state. Its write shares the existing serialized global
settings queue, and reads begun before that write cannot replace its result.
A client-only visual toggle must not claim to change server enforcement; hosts
without the persisted capability expose no master switch. Preview hosts can
supply an isolated adapter for interface review without contacting the
inference server.

Configured maximum, battery-restricted effective maximum and active/queued/
idle/suspended state are separate information. Unsupported inference paths
must not look controlled by a setting that does not cover them. Admin-only
`GET /admin/api/inference-scheduling` returns policy/runtime status;
`PATCH /admin/api/models/{model_id}/scheduling` saves a partial policy. The
ordinary global and model settings APIs carry the allowance fields. Writes
are serialized and coalesced; failed writes retain drafts and offer retry.
Polling cannot overwrite an actively edited allowance or a newer saved draft.

Tests of ownership and queue policy do not establish throughput gains. Hardware
comparison must keep both models resident and warmed, compare sequential AB/BA,
current concurrency and exclusive turns, retain same-model batching controls,
and include queued time, stream gaps, actual output tokens and complete-workload
makespan. No energy-saving or universal performance improvement is promised.
