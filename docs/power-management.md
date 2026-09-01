# Battery-aware inference power management

## Goals

oMLX must not continue inference while macOS is drawing from a MacBook's
internal battery. Active requests pause without cancellation, unloading, or
loss of model/KV state and resume when the required power state returns.

When AC power is available and charge is below a configurable floor, oMLX may
pace inference to preserve a configurable minimum net battery charging rate.
Every numeric policy value is persisted and hot-applicable. Values that can be
derived at runtime support both automatic selection and a fixed user override.

## Version-one policy

The process-wide governor has these states, in priority order:

1. **Unsupported** — no internal battery is present or power telemetry is
   unavailable.
2. **Paused on battery** — macOS reports the battery as the effective power
   source. No new MLX work is submitted.
3. **AC stabilization** — AC has returned, but inference remains paused while
   the battery signal settles and an idle charging baseline is measured.
4. **Charge recovery** — AC is effective and charge is at or below the
   configured floor. Inference duty is limited so the filtered net battery
   charge rate meets the configured target.
5. **Normal** — AC is effective and charge is above the recovery threshold.

Loss of AC closes the work gate immediately. Restoration is deliberately
debounced. Recovery ends only after charge exceeds the floor plus the
configured percentage hysteresis.

If the idle charging baseline cannot meet the recovery target with inference
fully paused, oMLX remains paused and reports that no inference power budget is
available.

## Pause semantics

- A Metal operation already submitted to the GPU is allowed to complete; oMLX
  submits no following decode step or prefill chunk.
- Long prefills use bounded chunks while power management is enabled so AC loss
  always reaches a cooperative pause boundary.
- Active requests, models, prompt/KV caches, sampler state, and output
  collectors remain resident.
- Requests are not completed, failed, cancelled, or unloaded because of a
  power pause.
- Requests arriving during a pause use the existing bounded waiting queue and
  its existing overflow response.
- SSE keepalives and server/admin work continue. Power-paused time does not
  count toward inference-stall or idle-unload decisions.
- System sleep/wake and client disconnect cleanup retain their existing
  semantics.

The governor never suspends the server process and does not change macOS Low
Power Mode.

## Telemetry contract

Version one uses supported, unprivileged IOKit power-source interfaces:

- `IOPSGetProvidingPowerSourceType` and `kIOPSPowerSourceStateKey` for the
  effective system power source;
- `IOPSNotificationCreateRunLoopSource` for prompt change notification, with
  periodic polling as reconciliation;
- `kIOPSCurrentCapacityKey` and `kIOPSMaxCapacityKey` for charge percentage;
- `kIOPSCurrentKey`/`kIOPMPSAmperageKey` and `kIOPSVoltageKey` for signed net
  battery watts;
- `kIOPSIsChargingKey` for charging state; and
- `IOPSCopyExternalPowerAdapterDetails` for adapter capability metadata.

Internally, positive battery watts always means charging and negative watts
means discharging, regardless of the sign convention published by a particular
source. Charging state and capacity trend are used to validate normalization.

Physical adapter presence, effective power source, and net battery flow are
separate signals. The hard gate follows the effective source. A connected
adapter therefore does not permit inference when macOS says the system is
currently battery-powered.

Detailed component power from direct SMC access or manually declared
`IOReport` symbols is not required by the version-one control loop.

## Configured and effective settings

The settings API exposes both the persisted configured value and the live
effective value for automatically derived settings. Changing a setting does
not restart the server or unload a model.

Behavioral settings include:

- enable/disable;
- battery behavior (`pause` in version one);
- charge floor and recovery percentage hysteresis;
- target net charging watts;
- AC restoration stabilization duration;
- telemetry sampling and stale-data limits;
- charge-rate filtering, deadband, and confirmation windows;
- duty reduction and restoration limits;
- paused probe size and interval; and
- maximum cooperative pause latency / prefill chunk-duration target.

Automatic values are bounded by documented minimum and maximum settings. User
overrides replace one derived value without disabling unrelated auto-tuning.

## Control behavior

The charge controller learns the idle charging baseline and the approximate
change in battery power caused by inference duty. This feed-forward estimate
selects an initial sustainable duty; conservative feedback corrects it for
changing system load.

Throttling reacts faster than restoration. The power deadband derives from
observed signal variation when automatic, state transitions have minimum dwell
times, and a fully paused controller waits for stable headroom before issuing a
small probe. These rules avoid repeated pause/resume chatter without assuming
that power-source telemetry is intrinsically slow.

## Measurement and acceptance

Before enabling control, a read-only probe records telemetry cadence and the
latency from a power-source event to completion of the current work quantum.
Validation covers:

- starting on battery;
- AC loss during decode and during prefill;
- requests arriving while paused;
- AC restoration above and below the charge floor;
- insufficient idle charging headroom;
- stale or unavailable telemetry;
- system sleep/wake; and
- macOS managed battery discharge while an adapter remains connected.

Version one is accepted only if requests resume with identical logical state,
the server remains responsive throughout a pause, and no post-notification MLX
work begins after the current bounded work quantum.

## Deferred behavior

A later version may offer a deliberately low battery-only inference budget and
may regulate sustained battery discharge while macOS continues to report AC as
the effective source. Version one always pauses on effective battery power.
