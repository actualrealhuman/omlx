# SPDX-License-Identifier: Apache-2.0
"""Bounded immutable semantic snapshots, with media normalized before admission.

No model/device imports, media decoding, callbacks or JSON serialization occur
while walking a request. Encoding happens later on the recorder's writer.
"""

import json
import math
import sys
from dataclasses import dataclass
from enum import Enum
from itertools import chain

from .images import submitted_image

MAX_NODES = 8192
MAX_DEPTH = 32
MAX_MEDIA = 128
_MARGIN = 4096


@dataclass(frozen=True, slots=True)
class Object:
    entries: tuple


@dataclass(frozen=True, slots=True)
class Array:
    items: tuple


@dataclass(frozen=True, slots=True)
class Omitted:
    reason: str


def _pieces(value):
    if isinstance(value, Object):
        yield "{"
        for index, (key, child) in enumerate(value.entries):
            if index:
                yield ","
            yield from _pieces(key)
            yield ":"
            yield from _pieces(child)
        yield "}"
    elif isinstance(value, Array):
        yield "["
        for index, child in enumerate(value.items):
            if index:
                yield ","
            yield from _pieces(child)
        yield "]"
    elif isinstance(value, Omitted):
        yield '{"_omlx_activity_omitted":'
        yield from _pieces(value.reason)
        yield "}"
    elif type(value) is str:
        yield '"'
        # JSONEncoder on a whole long string would allocate its escaped copy.
        for start in range(0, len(value), 8192):
            yield json.dumps(value[start : start + 8192], ensure_ascii=True)[1:-1]
        yield '"'
    else:
        yield json.dumps(value, allow_nan=False)


@dataclass(frozen=True, slots=True)
class SemanticSnapshot:
    root: object
    memory_bytes: int
    estimated_bytes: int
    media_count: int
    reasons: tuple[str, ...]
    images: tuple = ()

    def encoded_chunks(self, limit):
        parts, size = [], 0
        for piece in _pieces(self.root):
            if size + len(piece) > limit and parts:
                yield "".join(parts).encode("ascii")
                parts, size = [], 0
            parts.append(piece)
            size += len(piece)
        if parts:
            yield "".join(parts).encode("ascii")


def _mapping(value):
    if type(value) is dict:
        return value, None, {}
    # Public Pydantic v2 field/extra metadata plus field storage. Do not invoke
    # recursive serializers or copy the full graph; no web dependency here.
    if isinstance(getattr(type(value), "model_fields", None), dict):
        return (
            value.__dict__,
            value.model_fields_set,
            value.model_extra or {},
        )
    return None, None, {}


class _Builder:
    def __init__(self, memory, logical, retain_images):
        self.memory_limit, self.logical_limit = memory - _MARGIN, logical - _MARGIN
        self.memory = self.logical = self.nodes = self.media = 0
        self.reasons = set()
        self.exhausted = False
        self.retain_images = retain_images
        self.images = []

    def omit(self, reason, *, exhausted=False):
        self.reasons.add(reason)
        self.exhausted |= exhausted
        return Omitted(reason)

    def charge(self, memory, logical):
        if (
            self.memory + memory > self.memory_limit
            or self.logical + logical > self.logical_limit
        ):
            return False
        self.memory += memory
        self.logical += logical
        return True

    def attachment(self, value, kind, path, mime_type=None):
        self.media += 1
        if self.media > MAX_MEDIA:
            return self.omit("attachment_reference_limit")
        if kind not in {"image", "image_reference"} or self.retain_images is None:
            self.reasons.add("attachment_payload_not_retained")
        inline = value.startswith("data:")
        comma = value.find(",", 0, 256) if inline else -1
        mime = value[5:comma].split(";", 1)[0] if comma >= 0 else None
        reference = {
            "id": f"attachment-{self.media}",
            "kind": kind,
            "path": list(path),
            "status": "not_retained",
            "validation": "submitted_unvalidated",
            "reason": "preview_not_implemented",
            "encoded_chars": len(value),
        }
        if mime:
            reference["mime_type"] = mime
        if kind in {"image", "image_reference"} and self.retain_images is not None:
            self.images.append(
                submitted_image(
                    reference["id"],
                    value,
                    mime_type=mime_type,
                    enabled=self.retain_images,
                )
            )
            reference.update(status="reference", reason="see_attachment_index")
        if not inline and kind in {
            "image_reference",
            "video_reference",
            "file_reference",
        }:
            reference["reason"] = "reference_not_fetched"
            reference["reference"] = value[:1024]
            reference["reference_truncated"] = len(value) > 1024
        return self.freeze({"_omlx_activity_attachment": reference}, (), "plain", 0)

    def child(self, context, kind, key):
        if context == "root" and key in {"messages", "input"}:
            return "messages"
        if (context == "root" and key == "system") or (
            context == "message"
            and (
                key == "content" or (kind == "function_call_output" and key == "output")
            )
        ):
            return "blocks"
        if context == "block":
            if kind == "tool_result" and key == "content":
                return "blocks"
            if kind in {"image", "document"} and key == "source":
                return kind + "_source"
            if kind in {"image_url", "input_image"} and key in {
                "image_url",
                "input_image",
            }:
                return "image_container"
            if kind in {"video_url", "input_video"} and key in {
                "video_url",
                "input_video",
            }:
                return "video_container"
            if kind == "input_audio" and key == "input_audio":
                return "audio_container"
            if kind == "file" and key == "file":
                return "file_container"
        return "plain"

    def media_kind(self, context, kind, key):
        if context == "block":
            if kind in {"image_url", "input_image"} and key in {
                "image_url",
                "input_image",
                "url",
            }:
                return "image_reference"
            if kind in {"video_url", "input_video"} and key in {
                "video_url",
                "input_video",
                "url",
            }:
                return "video_reference"
            if kind == "input_file" and key in {"file_data", "file_id"}:
                return "file_reference" if key == "file_id" else "document"
        if context in {"image_container", "video_container"} and key == "url":
            return context.split("_", 1)[0] + "_reference"
        if context in {"image_source", "document_source"}:
            if key == "data" and kind == "base64":
                return context.split("_", 1)[0]
            if key == "url" and kind == "url":
                return context.split("_", 1)[0] + "_reference"
        if context == "audio_container" and key == "data":
            return "audio"
        if context == "file_container" and key in {"file_data", "data", "file_id"}:
            return "file_reference" if key == "file_id" else "document"
        return None

    def freeze(self, value, path=(), context="root", depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            return self.omit("snapshot_structure_limit", exhausted=True)
        # Covers owned nodes/containers, keys, references and bounded traversal
        # temporaries conservatively; immutable string storage is charged too.
        if not self.charge(1024, 4):
            return self.omit("snapshot_size_limit", exhausted=True)
        if isinstance(value, Enum):
            value = value.value
        if value is None or type(value) is bool:
            if not self.charge(0, 5):
                return self.omit("snapshot_size_limit", exhausted=True)
            return value
        if type(value) is int and -(2**63) <= value < 2**63:
            if not self.charge(0, 20):
                return self.omit("snapshot_size_limit", exhausted=True)
            return value
        if type(value) is float and math.isfinite(value):
            if not self.charge(0, 32):
                return self.omit("snapshot_size_limit", exhausted=True)
            return value
        if type(value) is str:
            if not self.charge(sys.getsizeof(value), len(value) * 6 + 2):
                return self.omit("snapshot_size_limit", exhausted=True)
            return value
        data, fields_set, extra = _mapping(value)
        if data is not None:
            entries = []
            kind = data.get("type")
            if type(kind) is not str:
                kind = None
            for key, child in chain(data.items(), extra.items()):
                if (
                    fields_set is not None
                    and key not in fields_set
                    and key not in extra
                ):
                    continue
                if type(key) is int and -(2**63) <= key < 2**63:
                    key = str(key)
                if type(key) is not str or len(key) > 256:
                    entries.append(
                        (
                            "_omlx_activity_omitted",
                            self.omit("snapshot_key_unavailable"),
                        )
                    )
                    break
                if not self.charge(128 + sys.getsizeof(key), len(key) * 6 + 4):
                    entries.append(
                        (
                            "_omlx_activity_omitted",
                            self.omit("snapshot_size_limit", exhausted=True),
                        )
                    )
                    break
                media_kind = self.media_kind(context, kind, key)
                frozen = (
                    self.attachment(
                        child,
                        media_kind,
                        path + (key,),
                        mime_type=data.get("media_type")
                        if context == "image_source"
                        else None,
                    )
                    if media_kind and type(child) is str
                    else self.freeze(
                        child, path + (key,), self.child(context, kind, key), depth + 1
                    )
                )
                entries.append((key, frozen))
                if self.exhausted:
                    entries.append(
                        (
                            "_omlx_activity_omitted",
                            Omitted("remaining_fields_not_captured"),
                        )
                    )
                    break
            return Object(tuple(entries))
        if type(value) in (list, tuple):
            items = []
            child_context = (
                "message"
                if context == "messages"
                else "block"
                if context == "blocks"
                else "plain"
            )
            for index, child in enumerate(value):
                items.append(
                    self.freeze(child, path + (index,), child_context, depth + 1)
                )
                if self.exhausted:
                    items.append(Omitted("remaining_items_not_captured"))
                    break
            return Array(tuple(items))
        return self.omit("snapshot_value_unavailable")


def snapshot_request(
    value, *, memory_bytes, max_bytes, retain_images=None, max_attachment_bytes=0
):
    if memory_bytes < 8192 or max_bytes < 8192:
        return None
    builder = _Builder(memory_bytes, max_bytes, retain_images)
    root = builder.freeze(value)
    # Text wins admission before any original media bytes. The API still owns
    # the source request during the walk; only charged immutable strings leave.
    images = []
    for image in builder.images:
        admitted = image.admit(
            memory=memory_bytes - builder.memory - _MARGIN,
            logical=max_bytes - builder.logical - _MARGIN,
            attachment_limit=max_attachment_bytes,
        )
        images.append(admitted)
        builder.memory += admitted.memory_bytes
        builder.logical += admitted.estimated_bytes
    return SemanticSnapshot(
        root,
        builder.memory + _MARGIN,
        builder.logical + _MARGIN,
        builder.media,
        tuple(sorted(builder.reasons)),
        tuple(images),
    )
