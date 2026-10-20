# Battery-aware batched generation

Battery protection is opt-in and covers local batched text and vision-language
generation, including vision preprocessing. Audio, embeddings, reranking,
decision models, diffusion, Splash, and distributed inference remain usable and
outside this policy. This does not claim to protect every server workload.

The saved inference setting is the user's maximum. Runtime limits never replace
that preference. The default policy permits the maximum at battery levels of
75% or higher. Between 50% and 75%, it interpolates from an absolute 10% share.
All policy values are configurable in Global Settings → Battery Protection.
For configured thresholds and minimum share, the curve is:

```
floor = min(minimum_share, requested_share)
target_share = floor + (requested_share - floor) * (battery_percent - suspend_percent) / (throttle_start_percent - suspend_percent)
```

By default, at 50% or below, participating generation suspends at a safe boundary. It remains
suspended until charge reaches 55%. Restoration is limited to one inference
percentage point per second; reductions take effect promptly after observation.
Already-submitted work may finish. This is cooperative submission control, not
GPU preemption or a measured electrical power limit.

The default external-power policy caps generation at 25%, then 10% after 60 seconds. Generation
continues until the reserve cutoff. Restoration requires ten seconds of stable
external power. Brief power flickers do not reset outage age. The lowest limit
wins; restrictions are not multiplied. Connected power and net battery charging
are separate: plugged-in discharge still follows the reserve curve.

The server evaluates policy before pinned preload and admission, independently
of the dashboard. Missing or stale required readings on a battery host suspend
participating generation. Battery-less and unsupported hosts are reported
explicitly. A small atomic runtime record preserves the reserve latch and outage
history across restarts. Startup on battery with unknown outage age uses the configured emergency cap
rather than granting another grace period.

Suspension retains generation state. Cancellation, cleanup and shutdown remain
available. New participating inference receives HTTP 503 with
`inference_suspended`. No precise recovery time or client timeout guarantee is
promised. The dashboard explains the server battery, configured maximum and
effective limit even if the throttle widget is removed.

The existing shared pacer coordinates participating scheduler calls and vision
preparation. Internal multimodal prefill chunks also rest while a battery limit
is active. Large indivisible operations can exceed a pacing window; all other
workloads may continue consuming battery during suspension. No system power-mode
switching, watt-feedback governor or battery-history database is included.

The opt-in remains `server.battery_protection_enabled` and defaults to false.
The numeric policy is stored in `server.battery_protection`; existing settings
without that object receive the original defaults. The same object is accepted
by the global-settings API. Partial option updates merge with saved values, and
the complete candidate is validated before any setting is changed. Saved policy
changes take effect without a restart and retain reserve/outage and restoration
history. Updating the user's inference maximum remains a separate live control.

The panel exposes throttle-start, suspend and resume charge levels, the curve's
minimum inference share, initial and emergency outage caps, interruption grace,
AC stabilization, restoration speed and telemetry timeout. The UI uses percentages
and seconds; the API and saved configuration use fractional shares and fractional
share per second for restoration. Advanced timing controls are collapsed initially.

Charge levels must satisfy `0 <= suspend < resume < throttle_start <= 100`.
Inference shares are between 0.1 and 1, with the emergency cap no greater than
the initial outage cap. The curve floor is independent of outage caps, and all
limits remain bounded by the user's maximum. Grace and stabilization may be
zero for immediate transitions. Restoration must be greater than zero and no
more than one full share per second; the telemetry timeout is at least one
second to match sensor sampling. Unknown options, nonnumeric values, booleans,
null numeric fields and nonfinite numbers are rejected even when protection is
disabled. Dashboard explanations use the running policy's configured values.
