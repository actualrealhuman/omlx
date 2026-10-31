# SPDX-License-Identifier: Apache-2.0
"""Non-secret credential attribution, used only after ordinary authentication."""

import secrets


def credential_source(provided, main_key, sub_keys):
    def matches(expected):
        return bool(expected) and secrets.compare_digest(
            provided.encode("utf-8", "surrogatepass"),
            expected.encode("utf-8", "surrogatepass"),
        )

    if provided and matches(main_key):
        return "main", "Main key"
    if provided:
        for entry in sub_keys:
            if matches(entry.key):
                return entry.id, entry.name or "Unnamed key"
    return "unknown", "Unknown"
