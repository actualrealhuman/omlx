# SPDX-License-Identifier: Apache-2.0
"""Bounded immutable invocation updates; JSON encoding belongs to the writer."""

import json
import math
from dataclasses import dataclass

MAX_INVOCATION_BYTES = 16384
INVOCATION_MEMORY_BYTES = 131072
MAX_INVOCATIONS = 256
INVOCATION_SUMMARY_FIELDS = (
    "state",
    "last_observed_state",
    "input_tokens",
    "generated_tokens",
    "first_token_ms",
    "first_text_ms",
    "counter_regressed",
    "currently_paused",
    "last_observed_paused",
    "pause_count",
    "resume_count",
    "last_pause_reason",
    "last_pause_ms",
    "last_resume_ms",
)
SAMPLING_FIELDS = (
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "xtc_probability",
    "xtc_threshold",
    "repetition_penalty",
    "repetition_context_size",
    "no_repeat_ngram_size",
    "ngram_window",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "thinking_budget",
    "seed",
    "stop_skips_reasoning",
)
SCALAR_FIELDS = frozenset(
    {
        "since_receipt_ms",
        "input_tokens",
        "prepared_text",
        "generated_tokens",
        "first_token_ms",
        "first_text_ms",
        "counter_regressed",
        "admitted_at_ms",
        "prepared_at_ms",
        "prefill_at_ms",
        "finished_at_ms",
        "currently_paused",
        "last_observed_paused",
        "pause_count",
        "resume_count",
        "last_pause_ms",
        "last_resume_ms",
    }
)
STRING_FIELDS = frozenset(
    {
        "engine_request_id",
        "state",
        "outcome",
        "finish_reason",
        "model_id",
        "last_pause_reason",
    }
)


def scalar(value):
    return (
        value is None
        or type(value) is bool
        or (type(value) is int and -(2**63) < value < 2**63)
        or (type(value) is float and math.isfinite(value))
    )


@dataclass(frozen=True)
class InvocationSnapshot:
    fields: tuple

    @classmethod
    def freeze(cls, values, model_id):
        fields = []
        for name in STRING_FIELDS | SCALAR_FIELDS:
            if name not in values:
                continue
            value = values[name]
            if name in STRING_FIELDS:
                if value is None or type(value) is str:
                    fields.append((name, value[:128] if value is not None else None))
            elif scalar(value):
                fields.append((name, value))
        if values.get("state") == "admitted":
            fields.append(("model_id", model_id[:256]))
            options, omitted = [], []
            # Only known names are inspected; no repr/serialization of objects.
            source = dict(values.get("options", ())[:32])
            for name in SAMPLING_FIELDS + (
                "grammar_constrained",
                "logits_observer_present",
            ):
                if name in source:
                    if scalar(source[name]):
                        options.append((name, source[name]))
                    else:
                        omitted.append(name)
            for name in ("stop", "stop_token_ids"):
                if name not in source:
                    continue
                value = source[name]
                if type(value) not in (list, tuple):
                    omitted.append(name)
                    continue
                kept, chars = [], 0
                for item in value[:32]:
                    if (
                        name == "stop"
                        and type(item) is str
                        and chars + len(item) <= 1024
                    ):
                        kept.append(item)
                        chars += len(item)
                    elif (
                        name == "stop_token_ids"
                        and type(item) is int
                        and 0 <= item <= 0xFFFFFFFF
                    ):
                        kept.append(item)
                    else:
                        omitted.append(name)
                        break
                if len(kept) != len(value):
                    omitted.append(name)
                options.append((name, tuple(kept)))
            fields.extend(
                (
                    ("admission_options", tuple(options)),
                    ("omitted_option_fields", tuple(sorted(set(omitted)))),
                    ("options_scope", "core_admission_not_final_backend_configuration"),
                    ("backend", "local_scheduler_core"),
                )
            )
        return cls(tuple(fields))

    def encode(self):
        result = dict(self.fields)
        if "admission_options" in result:
            result["admission_options"] = dict(result["admission_options"])
        data = json.dumps(result, ensure_ascii=True).encode("ascii")
        if len(data) > MAX_INVOCATION_BYTES:
            raise ValueError("Activity invocation snapshot outside bounds")
        return data
