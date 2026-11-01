# SPDX-License-Identifier: Apache-2.0
"""Bounded store for immutable Activity SSE replay frames.

The admin route owns authentication, committed reads and stream admission.
This component only retains immutable encoded frames within fixed resource caps.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

_MAX_PAYLOAD = 1024 * 1024
_MAX_FRAME = _MAX_PAYLOAD + 256
_MAX_GLOBAL_BYTES = 2 * 1024 * 1024
_MAX_GLOBAL_EVENTS = 128
_MAX_CHANNEL_BYTES = 1024 * 1024 + 256
_MAX_CHANNEL_EVENTS = 64
_MAX_CHANNELS = 16
_MAX_LEASES = 8
_MAX_CURSOR = 128
_UINT64_MAX = (1 << 64) - 1
_CURSOR_RE = re.compile(r"([0-9a-f]{32}):([0-9a-f]{32}):(0|[1-9][0-9]*)\Z")
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


class ReplayBufferFullError(RuntimeError):
    """The bounded buffer cannot admit a channel or active lease."""


class StaleLeaseError(RuntimeError):
    """The lease was released or invalidated by a reset/epoch rotation."""


@dataclass(frozen=True, slots=True)
class Lease:
    """Opaque caller-held token; it contains no channel history or request data."""

    epoch: str
    channel_id: str
    token: str


@dataclass(frozen=True, slots=True)
class ReplayResult:
    status: str  # "event", "caught_up", or "resync"
    cursor: str | None
    frame: bytes | None = None


@dataclass(frozen=True, slots=True)
class ObserveResult:
    changed: bool
    cursor: str
    frame: bytes
    retained: bool


@dataclass(frozen=True, slots=True)
class ReplayStats:
    epoch: str
    channels: int
    active_leases: int
    events: int
    encoded_bytes: int


@dataclass(frozen=True, slots=True)
class _Event:
    seq: int
    digest: bytes
    frame: bytes
    created_at: float


@dataclass(slots=True)
class _Channel:
    digest: bytes
    channel_id: str
    last_used: float
    head_seq: int = 0
    head_digest: bytes | None = None
    active: int = 0
    events: deque[_Event] = field(default_factory=deque)
    encoded_bytes: int = 0


class ActivityReplayBuffer:
    """Thread-safe, bounded cache of fully framed immutable SSE events."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
        max_payload_bytes: int = _MAX_PAYLOAD,
        max_frame_bytes: int = _MAX_FRAME,
        max_global_bytes: int = _MAX_GLOBAL_BYTES,
        max_global_events: int = _MAX_GLOBAL_EVENTS,
        max_channel_bytes: int = _MAX_CHANNEL_BYTES,
        max_channel_events: int = _MAX_CHANNEL_EVENTS,
        max_channels: int = _MAX_CHANNELS,
        max_leases: int = _MAX_LEASES,
        event_ttl: float = 30.0,
        inactive_ttl: float = 60.0,
        sequence_ceiling: int = _UINT64_MAX,
    ) -> None:
        self._validate_bounds(
            max_payload_bytes,
            max_frame_bytes,
            max_global_bytes,
            max_global_events,
            max_channel_bytes,
            max_channel_events,
            max_channels,
            max_leases,
            event_ttl,
            inactive_ttl,
            sequence_ceiling,
        )
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._max_payload = max_payload_bytes
        self._max_frame = max_frame_bytes
        self._max_global_bytes = max_global_bytes
        self._max_global_events = max_global_events
        self._max_channel_bytes = max_channel_bytes
        self._max_channel_events = max_channel_events
        self._max_channels = max_channels
        self._max_leases = max_leases
        self._event_ttl = event_ttl
        self._inactive_ttl = inactive_ttl
        self._sequence_ceiling = sequence_ceiling
        self._lock = threading.RLock()
        self._epoch = self._new_id()
        self._channels: dict[bytes, _Channel] = {}
        self._by_id: dict[str, _Channel] = {}
        self._leases: dict[str, str] = {}
        self._event_count = 0
        self._encoded_bytes = 0

    @staticmethod
    def _validate_bounds(
        max_payload: int,
        max_frame: int,
        global_bytes: int,
        global_events: int,
        channel_bytes: int,
        channel_events: int,
        channels: int,
        leases: int,
        event_ttl: float,
        inactive_ttl: float,
        sequence_ceiling: int,
    ) -> None:
        ints = (
            max_payload,
            max_frame,
            global_bytes,
            global_events,
            channel_bytes,
            channel_events,
            channels,
            leases,
            sequence_ceiling,
        )
        if any(type(value) is not int for value in ints):
            raise ValueError("bounds must be integers")
        if not (1 <= max_payload <= _MAX_PAYLOAD):
            raise ValueError("max_payload_bytes must be in 1..1MiB")
        if not (1 <= max_frame <= min(_MAX_FRAME, max_payload + 256)):
            raise ValueError("max_frame_bytes is inconsistent with payload cap")
        if not (max_frame <= channel_bytes <= _MAX_CHANNEL_BYTES):
            raise ValueError("max_channel_bytes must fit a frame and stay bounded")
        if not (max_frame <= global_bytes <= _MAX_GLOBAL_BYTES):
            raise ValueError("max_global_bytes must fit a frame and stay bounded")
        if not (1 <= channel_events <= _MAX_CHANNEL_EVENTS):
            raise ValueError("max_channel_events must be in 1..64")
        if not (channel_events <= global_events <= _MAX_GLOBAL_EVENTS):
            raise ValueError(
                "max_global_events must cover channel cap and stay bounded"
            )
        if not (1 <= channels <= _MAX_CHANNELS):
            raise ValueError("max_channels must be in 1..16")
        if not (1 <= leases <= _MAX_LEASES):
            raise ValueError("max_leases must be in 1..8")
        for name, value, limit in (
            ("event_ttl", event_ttl, 30.0),
            ("inactive_ttl", inactive_ttl, 60.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            try:
                valid = math.isfinite(value) and 0 < value <= limit
            except (OverflowError, TypeError):
                valid = False
            if not valid:
                raise ValueError(f"{name} must be finite and in 0..{limit}")
        if not (1 <= sequence_ceiling <= _UINT64_MAX):
            raise ValueError("sequence_ceiling must be in 1..uint64 max")

    def _new_id(self, *, forbidden: set[str] | None = None) -> str:
        forbidden = forbidden or set()
        for _ in range(8):
            value = self._id_factory()
            if (
                isinstance(value, str)
                and _ID_RE.fullmatch(value)
                and value not in forbidden
            ):
                return value
        raise ValueError("ID factory returned invalid or colliding IDs")

    @staticmethod
    def _check_digest(digest: bytes) -> None:
        if type(digest) is not bytes or len(digest) != 32:
            raise ValueError("channel key must be an immutable 32-byte digest")

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock must return a finite number")
        try:
            valid = math.isfinite(value)
        except (OverflowError, TypeError):
            valid = False
        if not valid:
            raise ValueError("clock must return a finite number")
        return float(value)

    def _prune(self, now: float) -> None:
        for channel in tuple(self._channels.values()):
            while (
                channel.events and now - channel.events[0].created_at >= self._event_ttl
            ):
                self._drop_event(channel, channel.events[0])
            if channel.active == 0 and now - channel.last_used >= self._inactive_ttl:
                self._drop_channel(channel)

    def _drop_event(self, channel: _Channel, event: _Event) -> None:
        channel.events.popleft()
        channel.encoded_bytes -= len(event.frame)
        self._encoded_bytes -= len(event.frame)
        self._event_count -= 1

    def _drop_channel(self, channel: _Channel) -> None:
        if channel.active:
            raise AssertionError("attempted to evict a pinned channel")
        while channel.events:
            self._drop_event(channel, channel.events[0])
        self._channels.pop(channel.digest, None)
        self._by_id.pop(channel.channel_id, None)

    def _validated_channel(self, lease: Lease) -> _Channel:
        if (
            not isinstance(lease, Lease)
            or not all(
                isinstance(value, str) and _ID_RE.fullmatch(value)
                for value in (lease.epoch, lease.channel_id, lease.token)
            )
            or lease.epoch != self._epoch
        ):
            raise StaleLeaseError("lease belongs to another epoch")
        channel_id = self._leases.get(lease.token)
        channel = self._by_id.get(lease.channel_id)
        if channel_id != lease.channel_id or channel is None:
            raise StaleLeaseError("lease is no longer active")
        return channel

    def acquire(self, key_digest: bytes) -> Lease:
        self._check_digest(key_digest)
        with self._lock:
            now = self._now()
            self._prune(now)
            if len(self._leases) >= self._max_leases:
                raise ReplayBufferFullError("active lease cap reached")
            channel = self._channels.get(key_digest)
            if channel is None:
                victim = None
                if len(self._channels) >= self._max_channels:
                    idle = [
                        item for item in self._channels.values() if item.active == 0
                    ]
                    if not idle:
                        raise ReplayBufferFullError("all channel slots are pinned")
                    victim = min(idle, key=lambda item: item.last_used)
                forbidden = {self._epoch, *self._by_id, *self._leases}
                token = self._new_id(forbidden=forbidden)
                channel_id = self._new_id(forbidden=forbidden | {token})
                channel = _Channel(key_digest, channel_id, now)
                if victim is not None:
                    self._drop_channel(victim)
                self._channels[key_digest] = channel
                self._by_id[channel_id] = channel
            else:
                token = self._new_id(
                    forbidden={self._epoch, *self._by_id, *self._leases}
                )
            lease = Lease(self._epoch, channel.channel_id, token)
            self._leases[token] = channel.channel_id
            channel.active += 1
            channel.last_used = now
            return lease

    def release(self, lease: Lease) -> None:
        """Release a lease once; stale, reset, or already released leases are harmless."""
        if not isinstance(lease, Lease):
            return
        with self._lock:
            if (
                not all(
                    isinstance(value, str) and _ID_RE.fullmatch(value)
                    for value in (lease.epoch, lease.channel_id, lease.token)
                )
                or lease.epoch != self._epoch
            ):
                return
            channel_id = self._leases.get(lease.token)
            if channel_id != lease.channel_id:
                return
            channel = self._by_id.get(channel_id)
            if channel is not None:
                now = self._now()
                self._leases.pop(lease.token)
                channel.active -= 1
                channel.last_used = now

    @staticmethod
    def _validate_payload(payload: bytes, max_payload: int) -> bytes:
        if type(payload) is not bytes:
            raise ValueError("payload must be immutable bytes")
        if len(payload) > max_payload:
            raise ValueError("payload exceeds configured cap")
        if not payload.isascii():
            raise ValueError("payload must be ASCII")
        if b"\r" in payload or b"\n" in payload:
            raise ValueError("payload cannot contain raw CR or LF")
        return payload

    def _frame(self, channel: _Channel, seq: int, payload: bytes) -> bytes:
        cursor = f"{self._epoch}:{channel.channel_id}:{seq}"
        prefix = b"id: "
        cursor_bytes = cursor.encode("ascii")
        event_line = b"\nevent: snapshot\ndata: "
        suffix = b"\n\n"
        frame_size = (
            len(prefix)
            + len(cursor_bytes)
            + len(event_line)
            + len(payload)
            + len(suffix)
        )
        if frame_size > self._max_frame:
            raise ValueError("encoded SSE frame exceeds configured cap")
        return b"".join((prefix, cursor_bytes, event_line, payload, suffix))

    def _fit_and_append(self, channel: _Channel, event: _Event) -> bool:
        size = len(event.frame)
        if size > self._max_channel_bytes or size > self._max_global_bytes:
            raise ValueError("frame cannot fit configured cache byte caps")
        while channel.events and (
            len(channel.events) >= self._max_channel_events
            or channel.encoded_bytes + size > self._max_channel_bytes
        ):
            self._drop_event(channel, channel.events[0])
        while (
            self._event_count >= self._max_global_events
            or self._encoded_bytes + size > self._max_global_bytes
        ):
            oldest: tuple[_Channel, _Event] | None = None
            for candidate in self._channels.values():
                if candidate.events and (
                    oldest is None
                    or candidate.events[0].created_at < oldest[1].created_at
                ):
                    oldest = (candidate, candidate.events[0])
            if oldest is None:
                raise AssertionError("global counters disagree with retained events")
            self._drop_event(*oldest)
        channel.events.append(event)
        channel.encoded_bytes += size
        self._encoded_bytes += size
        self._event_count += 1
        return True

    def observe(self, lease: Lease, payload: bytes) -> ObserveResult:
        payload = self._validate_payload(payload, self._max_payload)
        digest = hashlib.sha256(payload).digest()
        with self._lock:
            channel = self._validated_channel(lease)
            now = self._now()
            # Compute/validate any possible frame before pruning or changing state.
            changed = digest != channel.head_digest
            seq = channel.head_seq + 1 if changed else channel.head_seq
            if changed and seq > self._sequence_ceiling:
                self._reset_locked()
                raise StaleLeaseError("sequence ceiling rotated the epoch; reacquire")
            frame = None
            if changed:
                frame = self._frame(channel, seq, payload)
                if (
                    len(frame) > self._max_channel_bytes
                    or len(frame) > self._max_global_bytes
                ):
                    raise ValueError("frame cannot fit configured cache byte caps")
            self._prune(now)
            channel = self._validated_channel(lease)
            channel.last_used = now
            if changed:
                assert frame is not None
                event = _Event(seq, digest, frame, now)
                self._fit_and_append(channel, event)
                channel.head_seq = seq
                channel.head_digest = digest
                return ObserveResult(
                    True, f"{self._epoch}:{channel.channel_id}:{seq}", frame, True
                )
            retained = next(
                (event for event in channel.events if event.seq == seq), None
            )
            if retained is not None:
                frame = retained.frame
                was_retained = True
            else:
                frame = self._frame(channel, seq, payload)
                was_retained = False
            assert frame is not None
            return ObserveResult(
                False, f"{self._epoch}:{channel.channel_id}:{seq}", frame, was_retained
            )

    def next_after(self, lease: Lease, cursor: str | None) -> ReplayResult:
        with self._lock:
            channel = self._validated_channel(lease)
            now = self._now()
            self._prune(now)
            channel = self._validated_channel(lease)
            channel.last_used = now
            head_cursor = (
                f"{self._epoch}:{channel.channel_id}:{channel.head_seq}"
                if channel.head_seq
                else None
            )
            if (
                not isinstance(cursor, str)
                or len(cursor) > _MAX_CURSOR
                or not cursor.isascii()
            ):
                return ReplayResult("resync", head_cursor)
            match = _CURSOR_RE.fullmatch(cursor)
            if match is None:
                return ReplayResult("resync", head_cursor)
            epoch, channel_id, raw_seq = match.groups()
            seq = int(raw_seq)
            if (
                seq > _UINT64_MAX
                or epoch != self._epoch
                or channel_id != channel.channel_id
            ):
                return ReplayResult("resync", head_cursor)
            if seq > channel.head_seq:
                return ReplayResult("resync", head_cursor)
            if seq == channel.head_seq:
                return ReplayResult("caught_up", head_cursor)
            wanted = seq + 1
            event = next((item for item in channel.events if item.seq == wanted), None)
            if event is None:
                return ReplayResult("resync", head_cursor)
            return ReplayResult(
                "event", f"{self._epoch}:{channel.channel_id}:{wanted}", event.frame
            )

    def prune(self) -> None:
        with self._lock:
            self._prune(self._now())

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        epoch = self._new_id(forbidden={self._epoch})
        self._epoch = epoch
        self._channels.clear()
        self._by_id.clear()
        self._leases.clear()
        self._event_count = 0
        self._encoded_bytes = 0

    def stats(self) -> ReplayStats:
        with self._lock:
            self._prune(self._now())
            return ReplayStats(
                self._epoch,
                len(self._channels),
                len(self._leases),
                self._event_count,
                self._encoded_bytes,
            )
