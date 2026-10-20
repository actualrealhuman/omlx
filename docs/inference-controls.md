# Inference controls

Inference controls let you reduce sustained local generation activity, select
which models run first, and protect a laptop's battery reserve. The dashboard
shows how the settings compose and changes them live without a server restart.

These are cooperative submission controls for supported engines. A percentage
is a best-effort activity ceiling, not measured watts, GPU utilization, token
throughput, or a reservation of RAM or processing capacity. An operation already
submitted to the device is allowed to finish.

## Start with the defaults

Run this branch using the normal [source setup](../README.md#from-source).
The web dashboard is available at `/admin`. Add the **Inference Throttle**
widget from the dashboard layout tray if your saved layout does not include it.

Fresh settings retain normal full-speed generation: the global limit and both
class allowances are 100%, unconfigured models inherit their class allowance,
serial model inference is off, and battery protection is off. The master switch
is on, so any limits you choose are enforced for participating work.

Start by reducing the global limit while generating a bounded response. Then
choose the models you want on the panel and adjust individual allowances.
Enable serial model inference when you want supported models to take turns.
There is no need to enable battery protection to use the other controls.

## Read the percentage equations

The configured model ceiling is:

```text
Global inference limit × Class allowance × Model allowance
80%                    × 50%             × 75% = 30%
```

Class and model allowances are fractions of their parent's ceiling. A 100%
model allowance uses its entire class allowance. It never overrides the global
limit while the master controls are enabled. Changing a parent scales its
children without changing their saved relative percentages.

The dashboard shows these equations beside the sliders. Only the allowance
input is editable; the parent and resulting ceiling are derived values. Drag
or tap the slider tray, use the keyboard, or enter an exact number. The global
limit accepts 1–100%; class and model allowances accept 0–100%. A zero class or
model allowance pauses its covered generation until that limit changes or the
master controls are bypassed. Cancellation and cleanup remain available.

The model ceilings are not summed reservations. With serial inference enabled,
the selected model runs within its own ceiling during its turn. Without serial
inference, supported engines can run concurrently while sharing the process
pacer. Configured ceilings, battery-restricted effective ceilings and actual
queued/active state are different information.

## Choose which models appear

A model enters the panel when that browser observes it loaded, or when you
select it in **Choose models**. Observed models remain listed after unloading.
The searchable checklist includes installed models so you can configure one
before loading it. Favorites and custom scheduling settings do not add a model
to the panel by themselves.

Use **Hide from panel** to remove an entry. Hiding wins over automatic inclusion,
even if the model remains loaded. Panel membership is saved per browser; it
neither loads a model nor changes its scheduling settings.

## Classes, priority and turns

Models are **Background** by default. The human/robot control changes between
**Interactive** and **Background**. Interactive models take priority over
Background models when serial inference is enabled. Higher numeric priorities
run first within a class; equal-priority models rotate when they compete.
Priority belongs to runnable work, not simply loaded weights.

Serial inference keeps several models resident while allowing one participating
model session to perform inference at a time. Requests for the selected model
can still batch together. Requests and their generation state remain resident
between turns; scheduling does not unload and later reconstruct them.

The default contested turn is five seconds. With no competing model waiting,
useful work continues. A completed short burst can stream output immediately;
the turn length is not an output buffer. Safe indivisible operations and long
chunks may overrun a turn. This is cooperative model scheduling, not kernel
preemption or a hard response-time guarantee.

Strict priority is intentional: continuous higher-priority or Interactive work
can starve lower-priority Background work. Use equal priorities and the same
class for workloads that should share turns. Interactive classification is a
per-model preference; this branch adds no public request argument that detects
whether an individual caller is a person or an agent.

## Master switch

The top-level master switch bypasses user throttle limits and serial scheduling
while retaining global, class, model, priority and turn preferences. The panel
labels the retained limits as saved while controls are off. Re-enabling restores
their enforcement, including preferences changed while bypassed.

The switch changes only after the server acknowledges saving the setting.
A failed save displays an error and leaves the previous confirmed state intact.
Battery protection remains a separately configured safeguard in both modes.

## Battery protection

Enable and configure battery protection in **Global Settings → Battery
Protection**. The dashboard reports its running restrictions and links to those
settings. It uses server-side battery observations, not the browser device's
battery, and works without keeping the dashboard open.

Battery policy may reduce a configured ceiling, suspend covered generation at
a reserve threshold, and restore activity gradually. It does not raise a
smaller user ceiling. The master bypass does not bypass battery suspension.
Cancellation, shutdown and cleanup remain available during suspension.

By default, protection starts reducing activity below 75% charge, suspends at
50% and resumes at 55%. External-power interruption caps, stabilization and
restoration rates are configurable. Missing or stale required telemetry can
suspend participating work on a battery host. Battery-less and unsupported
hosts are reported explicitly. See the [battery policy](decisions/battery-protection.md)
for the curve, outage behavior and recovery rules.

## Coverage and limitations

Model turns cover registered local batched text and vision-language model
sessions, including their supported synchronous and preparation paths.
Battery protection also gates covered vision preparation. Standalone pacing
without those safeguards has a narrower scheduler/decode scope; see the
[throttle contract](decisions/inference-throttle.md).

Audio, embeddings, reranking, decision, diffusion, distributed/remote engines,
model loading, separate processes and independent device work are outside the
model-turn contract. This is not server-wide GPU exclusivity. Other work can
continue using memory, power and the device. Memory pressure can prevent several
models and their request state from remaining resident.

Lower settings may increase first-token latency and streaming gaps. Serial
execution can help some workloads and hurt others. No universal speedup,
proportional token rate or electrical-energy saving is promised.

## Settings, compatibility and validation

The global settings API persists `inference_share`, the two class allowances,
`inference_scheduling_enabled`, `inference_scheduling_quantum_seconds`,
`inference_controls_enabled`, and the opt-in battery policy. Model scheduling
persists separately from generation profiles and templates. Scheduling status
and partial model scheduling updates use authenticated admin endpoints; see
[the scheduling contract](decisions/inference-scheduling.md) for field names.

For users of earlier feature revisions, legacy absolute model limits are
converted once to relative allowances with verified backups. The migration
preserves the configured ceiling at the original global/class values; later
parent changes intentionally scale it. Generation profiles and templates are
not rewritten to change model scheduling. A migration failure stops startup
rather than silently relaxing saved limits.

Use temporary settings and synthetic battery readings when running the
[relevant tests](TESTING.md#inference-controls). Do not deliberately drain a
battery or change an installed server's policy for a test. Report the exact
source revision, dependencies and workload when sharing validation or performance
results. A test of queue ownership does not establish throughput or energy gains.
