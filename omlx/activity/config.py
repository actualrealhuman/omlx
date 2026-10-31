# SPDX-License-Identifier: Apache-2.0
"""Validated resource bounds for local request observation."""

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class ActivitySettings:
    enabled: bool = False
    max_requests: int = 1000
    max_age_days: int = 7
    max_storage_bytes: int = 5 * 1024**3
    max_request_bytes: int = 64 * 1024**2
    max_attachment_bytes: int = 16 * 1024**2
    max_memory_bytes: int = 128 * 1024**2
    max_pending_bytes: int = 16 * 1024**2
    max_active_requests: int = 1024
    retain_images: bool = True

    def __post_init__(self):
        for name in ("enabled", "retain_images"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"activity.{name} must be a boolean")
        for item in fields(self):
            if item.name in ("enabled", "retain_images"):
                continue
            value = getattr(self, item.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"activity.{item.name} must be a positive integer")
        if self.max_storage_bytes < 1024**2:
            raise ValueError("activity.max_storage_bytes must be at least 1 MiB")
        if self.max_request_bytes > self.max_storage_bytes:
            raise ValueError("Activity request cap exceeds the store budget")
        if self.max_attachment_bytes > self.max_request_bytes:
            raise ValueError("Activity attachment cap exceeds the request cap")
        # Reserve bounded lifecycle metadata independently of the content queue.
        if (
            self.max_pending_bytes + self.max_active_requests * 8192
            > self.max_memory_bytes
        ):
            raise ValueError("Activity content and metadata exceed the memory budget")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict):
            raise ValueError("activity settings must be an object")
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})
