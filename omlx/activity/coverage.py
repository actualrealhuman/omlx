# SPDX-License-Identifier: Apache-2.0
"""Recorded evidence coverage, separate from unqualified backend promises."""

BACKENDS = frozenset(
    {
        "local_text",
        "local_vlm",
        "local_diffusion",
        "distributed_proxy",
        "splash_native",
        "unknown",
    }
)


def evidence_coverage(record, representations, attachments):
    retained = {item["representation"] for item in representations}

    def state(representation, observed=False):
        if representation in retained:
            return "retained"
        return "observed_not_retained" if observed else "not_observed"

    kinds = record.get("resolved_backends", ())
    kinds = kinds if isinstance(kinds, (list, tuple)) else ()
    kinds = [
        kind
        for kind in kinds[: len(BACKENDS)]
        if type(kind) is str and kind in BACKENDS
    ]
    return {
        "backend_kinds": kinds,
        "backend_source": "resolved_engine_dispatch" if kinds else "unavailable",
        "scheduler_path": "observed"
        if record.get("observed_invocations", 0)
        else "not_observed",
        "scope": "recorded_evidence_only",
        "preparser_output_guaranteed": False,
        "evidence": {
            "submitted": state("submitted", "submitted_normalized" in record),
            "prepared_text": state(
                "model_input", record.get("prepared_text_invocations", 0) > 0
            ),
            "prepared_tokens": state(
                "model_tokens", record.get("prepared_invocations", 0) > 0
            ),
            "producer_output": state("raw_output", "first_observed_text_ms" in record),
            "api_wire": state("api_response"),
            "images": "retained"
            if any(item.get("status") == "retained" for item in attachments)
            else "observed_not_retained"
            if attachments
            else "not_observed",
        },
    }
