# SPDX-License-Identifier: Apache-2.0
"""Immutable, budgeted submitted images; decoding is exclusive to the writer."""

import base64
import struct
import sys
import zlib
from dataclasses import dataclass, replace

IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
MAX_IMAGE_PIXELS = 16 * 1024**2
MAX_PREVIEW_BYTES = 16 * 1024**2


def _png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG signature")
    position, size, pixels = 8, None, False
    view = memoryview(data)
    for _ in range(4096):
        if position + 12 > len(data):
            break
        length = struct.unpack_from(">I", data, position)[0]
        end = position + 12 + length
        kind = data[position + 4 : position + 8]
        if end > len(data) or kind == b"acTL":
            break
        if (
            zlib.crc32(view[position + 4 : end - 4])
            != struct.unpack_from(">I", data, end - 4)[0]
        ):
            break
        if size is None:
            if kind != b"IHDR" or length != 13:
                break
            size = struct.unpack_from(">II", data, position + 8)
        elif kind == b"IHDR":
            break
        pixels |= kind == b"IDAT"
        if kind == b"IEND":
            if length == 0 and end == len(data) and pixels:
                return size
            break
        position = end
    raise ValueError("PNG container")


def _jpeg_size(data):
    if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise ValueError("JPEG signature")
    position, size = 2, None
    for _ in range(4096):
        if position + 4 > len(data) or data[position] != 255:
            break
        position += 1
        while position < len(data) and data[position] == 255:
            position += 1
        if position + 3 > len(data):
            break
        marker = data[position]
        position += 1
        length = struct.unpack_from(">H", data, position)[0]
        if length < 2 or position + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2}:
            if size is not None or length < 8:
                break
            height, width = struct.unpack_from(">HH", data, position + 3)
            size = (width, height)
        if marker == 0xDA:
            if size is not None:
                return size
            break
        position += length
    raise ValueError("JPEG container")


def _webp_size(data):
    if (
        data[:4] != b"RIFF"
        or data[8:12] != b"WEBP"
        or struct.unpack_from("<I", data, 4)[0] + 8 != len(data)
    ):
        raise ValueError("WebP signature")
    position, size, extended = 12, None, None
    for _ in range(4096):
        if (
            position == len(data)
            and size is not None
            and (extended is None or extended == size)
        ):
            return size
        if position + 8 > len(data):
            break
        kind = data[position : position + 4]
        length = struct.unpack_from("<I", data, position + 4)[0]
        start = position + 8
        end = start + length
        if end > len(data) or kind in {b"ANIM", b"ANMF"}:
            break
        if kind == b"VP8X":
            if extended is not None or length != 10 or data[start] & 2:
                break
            extended = (
                int.from_bytes(data[start + 4 : start + 7], "little") + 1,
                int.from_bytes(data[start + 7 : start + 10], "little") + 1,
            )
        elif kind in {b"VP8 ", b"VP8L"}:
            if size is not None:
                break
            if (
                kind == b"VP8 "
                and length >= 10
                and data[start + 3 : start + 6] == b"\x9d\x01\x2a"
            ):
                width, height = struct.unpack_from("<HH", data, start + 6)
                size = (width & 0x3FFF, height & 0x3FFF)
            elif kind == b"VP8L" and length >= 5 and data[start] == 0x2F:
                bits = int.from_bytes(data[start + 1 : start + 5], "little")
                size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            else:
                break
        position = end + (length & 1)
    raise ValueError("WebP container")


@dataclass(frozen=True, slots=True)
class SubmittedImage:
    id: str
    mime_type: str | None
    encoded: str | None = None
    offset: int = 0
    estimated_bytes: int = 0
    memory_bytes: int = 0
    reason: str | None = None

    def admit(self, *, memory, logical, attachment_limit):
        if self.encoded is None:
            return self
        # Account for retained text, ASCII/base64 temporaries, decoded bytes,
        # container validation and SQLite binding copies. No pixels
        # are decoded. Effective limits can be lower than the attachment cap.
        size = (len(self.encoded) - self.offset + 3) // 4 * 3
        cost = 2 * sys.getsizeof(self.encoded) + 6 * size + 65536
        reason = (
            "attachment_size_limit"
            if size > min(attachment_limit, MAX_PREVIEW_BYTES)
            else "attachment_request_limit"
            if size > logical
            else "attachment_queue_limit"
            if cost > memory
            else None
        )
        if reason:
            return replace(self, encoded=None, reason=reason)
        return replace(self, estimated_bytes=size, memory_bytes=cost)


def submitted_image(identifier, value, *, mime_type=None, enabled=False):
    """Inspect only a bounded header, without slicing/decoding the payload."""
    offset = 0
    if type(mime_type) is not str:
        mime_type = None
    if value.startswith("data:"):
        comma = value.find(",", 0, 256)
        header = value[5:comma].lower() if comma >= 0 else ""
        mime_type, _, encoding = header.partition(";")
        if encoding != "base64":
            return SubmittedImage(
                identifier, mime_type, reason="unsupported_image_encoding"
            )
        offset = comma + 1
    elif mime_type is None:
        return SubmittedImage(identifier, None, reason="reference_not_fetched")
    if mime_type not in IMAGE_FORMATS:
        return SubmittedImage(identifier, None, reason="unsupported_image_type")
    if not enabled:
        return SubmittedImage(identifier, mime_type, reason="image_retention_disabled")
    return SubmittedImage(identifier, mime_type, value, offset)


def prepare_image(image):
    """Verify original encoded file structure, never resize/evaluate a model.

    This is independent observer validation. It does not establish that the
    inference decoder accepted the image or used these bytes unchanged.
    """
    metadata = {
        "id": image.id,
        "status": "not_retained",
        "reason": image.reason,
        "mime_type": image.mime_type,
        "provenance": "submitted_original",
        "validation": "unavailable",
    }
    if image.encoded is None:
        return metadata, None
    try:
        data = base64.b64decode(image.encoded[image.offset :], validate=True)
        if not data or len(data) > image.estimated_bytes:
            raise ValueError("Invalid image size")
        # Bounded stdlib container/header checks, no metadata decompression or
        # native image decoder allocations. Pixels are decoded only by the
        # browser's inert image element. This is not full codec validation.
        width, height = {
            "image/png": _png_size,
            "image/jpeg": _jpeg_size,
            "image/webp": _webp_size,
        }[image.mime_type](data)
        if (
            width <= 0
            or height <= 0
            or max(width, height) > 16384
            or width * height > MAX_IMAGE_PIXELS
        ):
            raise ValueError("Image dimensions outside preview bounds")
        metadata.update(
            status="retained",
            reason=None,
            validation="observer_container_checked",
            bytes=len(data),
            width=width,
            height=height,
        )
        return metadata, data
    except Exception:
        metadata["reason"] = "image_verification_failed"
    return metadata, None
