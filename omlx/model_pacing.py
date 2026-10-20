# SPDX-License-Identifier: Apache-2.0
"""Compose a model's activity ceiling with the persistent global pacer.

Both controllers account the same actual work intervals. Selecting a model
never resets the global controller or buys another global work window.
"""

from dataclasses import dataclass

from .inference_pacing import PACING_DISABLED, InferencePacer


@dataclass(frozen=True)
class ModelPacingPermit:
    global_permit: object | None
    model_permit: object | None


class ModelPacer:
    def __init__(self, global_pacer: InferencePacer, share: float = 1.0):
        self.global_pacer = global_pacer
        self.model_pacer = InferencePacer(share)

    @property
    def full_speed_snapshot(self):
        return (
            self.global_pacer.full_speed_snapshot
            and self.model_pacer.full_speed_snapshot
        )

    @property
    def share(self):
        return min(self.global_pacer.share, self.model_pacer.share)

    def set_share(self, share):
        if share != self.model_pacer.share:
            self.model_pacer.set_share(share)

    def start_engine(self, engine_id):
        self.global_pacer.start_engine(engine_id)
        self.model_pacer.start_engine(engine_id)

    def cancel_engine(self, engine_id):
        self.global_pacer.cancel_engine(engine_id)
        self.model_pacer.cancel_engine(engine_id)

    def retire_engine(self, engine_id):
        self.global_pacer.retire_engine(engine_id)
        self.model_pacer.retire_engine(engine_id)

    def set_engine_busy(self, engine_id, busy):
        self.global_pacer.set_engine_busy(engine_id, busy)
        self.model_pacer.set_engine_busy(engine_id, busy)

    async def acquire(
        self, engine_id, wake_event=None, on_admission_wait=None, cancelled=None
    ):
        acquired = []
        try:
            for pacer in (self.model_pacer, self.global_pacer):
                permit = await pacer.acquire(
                    engine_id,
                    wake_event=wake_event,
                    on_admission_wait=on_admission_wait,
                    cancelled=cancelled,
                )
                if permit is None:
                    return None
                acquired.append(None if permit is PACING_DISABLED else permit)
            if all(permit is None for permit in acquired):
                return PACING_DISABLED
            result = ModelPacingPermit(acquired[1], acquired[0])
            acquired = []  # completion now belongs to the submitted worker
            return result
        finally:
            for pacer, permit in zip((self.model_pacer, self.global_pacer), acquired):
                if permit is not None:
                    pacer.complete(permit, [])

    def begin_step(self, permit):
        # Check the model policy even when a global full-speed snapshot would
        # otherwise bypass pacer admission. This fences already-issued permits
        # immediately after a user pauses this model.
        if self.model_pacer.share <= 0.0:
            return False
        pairs = (
            (self.global_pacer, permit.global_permit),
            (self.model_pacer, permit.model_permit),
        )
        if not all(
            token is None or pacer.step_allowed(token) for pacer, token in pairs
        ):
            return False
        return all(token is None or pacer.begin_step(token) for pacer, token in pairs)

    def complete(self, permit, activity_intervals):
        for pacer, token in (
            (self.global_pacer, permit.global_permit),
            (self.model_pacer, permit.model_permit),
        ):
            if token is not None:
                pacer.complete(token, activity_intervals)
