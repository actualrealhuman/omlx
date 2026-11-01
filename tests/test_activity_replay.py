# SPDX-License-Identifier: Apache-2.0
"""Focused standard-library tests for the bounded Activity replay buffer."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace

from omlx.activity.replay import (
    ActivityReplayBuffer,
    ReplayBufferFullError,
    StaleLeaseError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"


def digest(value: int) -> bytes:
    return value.to_bytes(32, "big")


class ReplayBufferTests(unittest.TestCase):
    def make_buffer(self, **kwargs) -> tuple[ActivityReplayBuffer, FakeClock]:
        clock = FakeClock()
        options = {"clock": clock, "id_factory": Ids(), **kwargs}
        return ActivityReplayBuffer(**options), clock

    def test_exact_frame_bytes_and_dedupe(self) -> None:
        buffer, _ = self.make_buffer()
        lease = buffer.acquire(digest(1))
        first = buffer.observe(lease, b'{"v":1}')
        expected = (f'id: {first.cursor}\nevent: snapshot\ndata: {{"v":1}}\n\n').encode(
            "ascii"
        )
        self.assertEqual(first.frame, expected)
        self.assertTrue(first.changed)
        self.assertEqual(buffer.stats().encoded_bytes, len(expected))
        same = buffer.observe(lease, b'{"v":1}')
        self.assertFalse(same.changed)
        self.assertTrue(same.retained)
        self.assertEqual(same.frame, expected)
        self.assertEqual(same.cursor, first.cursor)
        self.assertEqual(buffer.stats().events, 1)

    def test_payload_validation_is_atomic(self) -> None:
        buffer, _ = self.make_buffer(
            max_payload_bytes=16,
            max_frame_bytes=272,
            max_channel_bytes=272,
            max_global_bytes=272,
        )
        lease = buffer.acquire(digest(1))
        good = buffer.observe(lease, b'{"ok":1}')
        before = buffer.stats()
        for bad in (bytearray(b"{}"), b"\xff", b"{\n}", b"{" + b"x" * 16):
            with self.assertRaises(ValueError):
                buffer.observe(lease, bad)
            self.assertEqual(buffer.stats(), before)
        self.assertEqual(buffer.observe(lease, b'{"ok":1}').cursor, good.cursor)

    def test_payload_and_frame_caps_reject_without_mutation(self) -> None:
        buffer, _ = self.make_buffer(
            max_payload_bytes=8,
            max_frame_bytes=32,
            max_channel_bytes=32,
            max_global_bytes=32,
        )
        lease = buffer.acquire(digest(1))
        before = buffer.stats()
        with self.assertRaises(ValueError):
            buffer.observe(lease, b"12345678")
        self.assertEqual(buffer.stats(), before)

    def test_count_caps_and_channel_byte_eviction_keep_gap_visible(self) -> None:
        buffer, _ = self.make_buffer(
            max_payload_bytes=64,
            max_frame_bytes=320,
            max_global_bytes=640,
            max_channel_bytes=320,
            max_global_events=2,
            max_channel_events=2,
        )
        lease = buffer.acquire(digest(1))
        first = buffer.observe(lease, b'{"n":1}')
        second = buffer.observe(lease, b'{"n":2}')
        third = buffer.observe(lease, b'{"n":3}')
        stats = buffer.stats()
        self.assertLessEqual(stats.events, 2)
        self.assertLessEqual(stats.encoded_bytes, 640)
        self.assertEqual(buffer.next_after(lease, first.cursor).status, "event")
        self.assertEqual(buffer.next_after(lease, second.cursor).status, "event")
        # The byte cap of one frame evicts older frames; a missing next event resyncs.
        tiny, _ = self.make_buffer(
            max_payload_bytes=64,
            max_frame_bytes=104,
            max_global_bytes=104,
            max_channel_bytes=104,
            max_global_events=2,
            max_channel_events=2,
        )
        tiny_lease = tiny.acquire(digest(2))
        old = tiny.observe(tiny_lease, b'{"n":1}')
        tiny.observe(tiny_lease, b'{"n":2}')
        current = tiny.observe(tiny_lease, b'{"n":3}')
        self.assertEqual(tiny.stats().events, 1)
        self.assertEqual(tiny.next_after(tiny_lease, old.cursor).status, "resync")
        self.assertEqual(
            tiny.next_after(tiny_lease, current.cursor).status, "caught_up"
        )
        self.assertEqual(third.cursor.split(":")[-1], "3")

    def test_global_byte_eviction_can_remove_other_channel_history(self) -> None:
        buffer, _ = self.make_buffer(
            max_payload_bytes=64,
            max_frame_bytes=320,
            max_global_bytes=640,
            max_channel_bytes=320,
            max_global_events=2,
            max_channel_events=2,
        )
        a = buffer.acquire(digest(1))
        b = buffer.acquire(digest(2))
        buffer.observe(a, b'{"n":1}')
        old_a = buffer.observe(a, b'{"n":2}')
        buffer.observe(b, b'{"n":1}')
        self.assertLessEqual(buffer.stats().encoded_bytes, 640)
        self.assertEqual(
            buffer.next_after(a, f"{a.epoch}:{a.channel_id}:0").status, "resync"
        )
        self.assertEqual(buffer.next_after(a, old_a.cursor).status, "caught_up")

    def test_event_ttl_and_head_reframing_do_not_restore_history(self) -> None:
        buffer, clock = self.make_buffer(event_ttl=5, inactive_ttl=20)
        lease = buffer.acquire(digest(1))
        first = buffer.observe(lease, b'{"n":1}')
        second = buffer.observe(lease, b'{"n":2}')
        clock.advance(5)
        self.assertEqual(buffer.next_after(lease, first.cursor).status, "resync")
        reframed = buffer.observe(lease, b'{"n":2}')
        self.assertFalse(reframed.changed)
        self.assertFalse(reframed.retained)
        self.assertEqual(reframed.frame, second.frame)
        self.assertEqual(buffer.stats().events, 0)
        self.assertEqual(buffer.next_after(lease, first.cursor).status, "resync")

    def test_inactive_ttl_and_lru_channel_eviction(self) -> None:
        buffer, clock = self.make_buffer(max_channels=2, inactive_ttl=10)
        one = buffer.acquire(digest(1))
        buffer.release(one)
        clock.advance(1)
        two = buffer.acquire(digest(2))
        buffer.release(two)
        three = buffer.acquire(digest(3))
        self.assertEqual(buffer.stats().channels, 2)
        self.assertEqual(
            buffer.next_after(three, f"{three.epoch}:{one.channel_id}:0").status,
            "resync",
        )
        buffer.release(three)
        clock.advance(10)
        buffer.prune()
        self.assertEqual(buffer.stats().channels, 0)

    def test_active_channels_are_pinned_and_lease_limit_is_global(self) -> None:
        buffer, _ = self.make_buffer(max_channels=2, max_leases=2)
        first = buffer.acquire(digest(1))
        same = buffer.acquire(digest(1))
        with self.assertRaises(ReplayBufferFullError):
            buffer.acquire(digest(2))
        self.assertEqual(buffer.stats().channels, 1)
        buffer.release(first)
        buffer.release(first)
        third = buffer.acquire(digest(2))
        self.assertEqual(buffer.stats().active_leases, 2)
        with self.assertRaises(ReplayBufferFullError):
            buffer.acquire(digest(3))
        buffer.release(same)
        buffer.release(third)

    def test_mismatched_lease_release_preserves_valid_registration(self) -> None:
        buffer, _ = self.make_buffer()
        lease = buffer.acquire(digest(1))
        forged = replace(lease, channel_id="f" * 32)
        malformed = replace(lease, token=[])
        buffer.release(forged)
        buffer.release(malformed)
        self.assertEqual(buffer.stats().active_leases, 1)
        observed = buffer.observe(lease, b'{"still":"active"}')
        self.assertTrue(observed.changed)
        buffer.release(lease)
        self.assertEqual(buffer.stats().active_leases, 0)

    def test_invalid_ttls_and_clock_values_are_rejected_atomically(self) -> None:
        for field, invalid in (
            ("event_ttl", math.nan),
            ("event_ttl", math.inf),
            ("event_ttl", True),
            ("event_ttl", 31),
            ("inactive_ttl", math.nan),
            ("inactive_ttl", math.inf),
            ("inactive_ttl", True),
            ("inactive_ttl", 61),
        ):
            with (
                self.subTest(field=field, invalid=invalid),
                self.assertRaises(ValueError),
            ):
                ActivityReplayBuffer(**{field: invalid})

        buffer, clock = self.make_buffer()
        lease = buffer.acquire(digest(1))
        buffer.observe(lease, b'{"n":1}')
        before = buffer.stats()
        for invalid in (math.nan, math.inf, -math.inf, True):
            clock.value = invalid
            with self.assertRaises(ValueError):
                buffer.prune()
            with self.assertRaises(ValueError):
                buffer.release(lease)
        clock.value = 100.0
        self.assertEqual(buffer.stats(), before)
        buffer.release(lease)
        self.assertEqual(buffer.stats().active_leases, 0)

    def test_failed_channel_id_admission_does_not_evict_inactive_cache(self) -> None:
        ids = Ids()
        buffer, _ = self.make_buffer(id_factory=ids, max_channels=1)
        first = buffer.acquire(digest(1))
        original = buffer.observe(first, b'{"keep":true}')
        buffer.release(first)
        before = buffer.stats()

        collision = first.channel_id
        generated = iter([f"{100:032x}", *([collision] * 8)])
        buffer._id_factory = lambda: next(generated)
        with self.assertRaises(ValueError):
            buffer.acquire(digest(2))
        self.assertEqual(buffer.stats(), before)

        buffer._id_factory = ids
        again = buffer.acquire(digest(1))
        replayed = buffer.observe(again, b'{"keep":true}')
        self.assertEqual(again.channel_id, first.channel_id)
        self.assertEqual(replayed.frame, original.frame)
        self.assertFalse(replayed.changed)

    def test_reset_invalidates_leases_and_clears_history_without_tombstones(
        self,
    ) -> None:
        buffer, _ = self.make_buffer()
        lease = buffer.acquire(digest(1))
        buffer.observe(lease, b'{"n":1}')
        old_epoch = lease.epoch
        buffer.reset()
        self.assertNotEqual(buffer.stats().epoch, old_epoch)
        self.assertEqual(buffer.stats().channels, 0)
        self.assertEqual(buffer.stats().events, 0)
        self.assertEqual(buffer.stats().active_leases, 0)
        buffer.release(lease)
        with self.assertRaises(StaleLeaseError):
            buffer.observe(lease, b'{"n":2}')
        fresh = buffer.acquire(digest(1))
        self.assertEqual(fresh.epoch, buffer.stats().epoch)

    def test_sequence_ceiling_rotates_epoch_without_wrap(self) -> None:
        buffer, _ = self.make_buffer(sequence_ceiling=2)
        lease = buffer.acquire(digest(1))
        buffer.observe(lease, b"1")
        buffer.observe(lease, b"2")
        old_epoch = lease.epoch
        with self.assertRaises(StaleLeaseError):
            buffer.observe(lease, b"3")
        self.assertNotEqual(buffer.stats().epoch, old_epoch)
        self.assertEqual(buffer.stats().events, 0)

    def test_cursor_replay_is_one_contiguous_event_or_resync(self) -> None:
        buffer, _ = self.make_buffer()
        lease = buffer.acquire(digest(1))
        first = buffer.observe(lease, b"1")
        second = buffer.observe(lease, b"2")
        third = buffer.observe(lease, b"3")
        one = buffer.next_after(lease, first.cursor)
        self.assertEqual(one.status, "event")
        self.assertEqual(one.frame, second.frame)
        self.assertEqual(buffer.next_after(lease, one.cursor).frame, third.frame)
        self.assertEqual(buffer.next_after(lease, third.cursor).status, "caught_up")
        for cursor in (
            None,
            "bad",
            first.cursor + ":extra",
            f"{lease.epoch}:{lease.channel_id}:04",
            f"{lease.epoch}:{lease.channel_id}:4",
            f"{'f' * 32}:{lease.channel_id}:1",
            f"{lease.epoch}:{'f' * 32}:1",
            "x" * 129,
        ):
            self.assertEqual(buffer.next_after(lease, cursor).status, "resync", cursor)

    def test_invalid_digest_and_id_collisions_are_bounded(self) -> None:
        buffer, _ = self.make_buffer(id_factory=lambda: "0" * 32)
        with self.assertRaises(ValueError):
            buffer.acquire(b"short")
        with self.assertRaises(ValueError):
            buffer.acquire(digest(1))
        self.assertEqual(buffer.stats().channels, 0)

    def test_stress_counters_never_exceed_hard_caps(self) -> None:
        buffer, clock = self.make_buffer(
            max_payload_bytes=64,
            max_frame_bytes=320,
            max_global_bytes=640,
            max_channel_bytes=320,
            max_global_events=4,
            max_channel_events=2,
            max_channels=4,
            max_leases=4,
            event_ttl=3,
            inactive_ttl=5,
        )
        leases = [buffer.acquire(digest(i)) for i in range(4)]
        for index in range(100):
            lease = leases[index % len(leases)]
            buffer.observe(lease, f'{{"n":{index}}}'.encode("ascii"))
            if index % 7 == 0:
                clock.advance(1)
            stats = buffer.stats()
            self.assertLessEqual(stats.channels, 4)
            self.assertLessEqual(stats.active_leases, 4)
            self.assertLessEqual(stats.events, 4)
            self.assertLessEqual(stats.encoded_bytes, 640)
            self.assertLessEqual(
                buffer._by_id[next(iter(buffer._by_id))].encoded_bytes, 320
            )
        buffer.reset()
        self.assertEqual(buffer.stats().encoded_bytes, 0)


if __name__ == "__main__":
    unittest.main()
