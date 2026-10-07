# Inference throttle scope

The Inference Throttle setting controls the scheduler's target inference share
for supported local batched generation. At 100%, the engine uses its ordinary
full-speed path without pacing admission, waits, or interval accounting. Lower
values divide covered scheduler inference time from natural idle time and may
add a bounded wait before the next scheduler burst.

The target is best-effort. A scheduler call is allowed to finish before the
engine enters its rest phase, and continuous admission deferral is capped at
one second. The default nominal scheduling quantum is 400 ms; its work window
scales with the requested share. This gives schedulers room to process useful
chunks without imposing a hard cutoff. The setting is not a measurement or a
guarantee of electrical power use.

The shared process controller coordinates local batched-generation engines.
Model loading and preparation, standalone diffusion/audio paths, and remote or
distributed workers are outside this first control surface. Dashboard layouts
that users have already saved remain unchanged; the new widget is included in
the default layout and remains available in the layout tray.

The slider and exact numeric field share the same live setting in the dashboard
and basic server settings. Slider changes are coalesced; typed values commit on
Enter or blur. General settings Save and Reset Defaults manage their draft
fields independently and never submit or reset the live throttle value.
