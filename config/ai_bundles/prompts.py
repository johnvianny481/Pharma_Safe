"""Source of the prompts in the committed AI bundles.

Run ``python config/ai_bundles/prompts.py`` to rewrite ``bundle.json`` in each bundle
directory from this file. Then re-sign each bundle with the release key (see the
runbook): an unsigned or stale signature makes the bundle unusable, by design.

Templates use ``{variable}`` placeholders, which are replaced with delimited data blocks;
literal braces must be doubled.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent

SYSTEM = (
    "You are a pharmacovigilance data extraction assistant. You read adverse event"
    " reports and propose structured values for a human safety reviewer, who checks and"
    " signs every value. Be literal and conservative: propose a value only when the report"
    " states it, and quote the exact words of the report that support it. Never guess."
    " Personal names and contact details have been replaced by tokens such as [NAME_1];"
    " keep tokens exactly as they appear."
)

PROMPTS = {
    "extraction": {
        "version": "extraction-v1",
        "system": SYSTEM,
        "user": (
            "Extract the patient, reporters, drugs and reactions from the report.\n"
            "For every value give `quote`: a short verbatim excerpt of the report (copied"
            " character for character) that contains the value, and `confidence` between 0"
            " and 1. Use null when the report does not state a value.\n"
            "Codes: sex male|female|unknown; age unit a (years), mo, wk, d or h; reporter"
            " qualification physician|pharmacist|other_health_professional|lawyer|consumer;"
            " drug role suspect|concomitant|interacting|not_administered; outcome recovered|"
            "recovering|not_recovered|recovered_with_sequelae|fatal|unknown; dates YYYY,"
            " YYYY-MM or YYYY-MM-DD; countries as ISO 3166 alpha-2.\n\n{report}"
        ),
    },
    "seriousness": {
        "version": "seriousness-v1",
        "system": SYSTEM,
        "user": (
            "For each listed reaction, assess the ICH E2A seriousness criteria: death,"
            " life_threatening, hospitalisation (initial or prolonged), disability"
            " (persistent or significant), congenital_anomaly and other_medically_important."
            " Set `met` true only when the report says so, with the verbatim `quote`."
            ' Negated mentions ("no hospitalisation") are not met.\n\n{reactions}\n\n{report}'
        ),
    },
    "coding": {
        "version": "coding-v1",
        "system": SYSTEM,
        "user": (
            "For each item, choose the candidate that best matches the reported term, by its"
            " `id`. Choose only from that item's candidates; if none fits, answer null."
            " Never invent a code.\n\n{items}"
        ),
    },
    "follow_up": {
        "version": "follow-up-v1",
        "system": SYSTEM,
        "user": (
            "Write one short, polite question to the reporter for each listed gap, in plain"
            " language. Use the gap's `path` unchanged. Do not ask about anything else and"
            " do not include personal data.\n\n{gaps}"
        ),
    },
    "narrative": {
        "version": "narrative-v1",
        "system": SYSTEM,
        "user": (
            "Write a factual case narrative (E2B H.1) of at most 200 words from these"
            " confirmed facts only. Do not add, infer or embellish anything. Keep tokens"
            " such as [NAME_1] unchanged.\n\n{facts}"
        ),
    },
}

THRESHOLDS = {"low_confidence": 0.7, "straight_through_confidence": 0.9, "duplicate_min_score": 0.5}
RETRIEVAL = {
    "coding_candidates": 8,
    "duplicate_block_limit": 50,
    "duplicate_top": 3,
    "ngram": 3,
    "dimensions": 256,
}


def _route(
    primary: dict[str, object], fallback: dict[str, object] | None, max_tokens: int
) -> dict[str, object]:
    return {
        "primary": primary,
        "fallback": fallback,
        "max_tokens": max_tokens,
        "max_input_chars": 60000,
        "timeout_seconds": 60,
        "max_attempts": 3,
        "temperature": 0.0,
    }


MAX_TOKENS = {
    "extraction": 3000,
    "seriousness": 1500,
    "coding": 1000,
    "follow_up": 800,
    "narrative": 600,
}

AZURE = {
    "provider": "azure_openai",
    "model": "gpt-4o-2024-08-06",
    "deployment": "pharmasafe-gpt-4o",
    "api_version": "2024-10-21",
    "usd_per_1k_input": 0.0025,
    "usd_per_1k_output": 0.01,
}
OPENAI = {
    "provider": "openai",
    "model": "gpt-4o-2024-08-06",
    "usd_per_1k_input": 0.0025,
    "usd_per_1k_output": 0.01,
}
MOCK = {
    "provider": "mock",
    "model": "mock-pv-v1",
    "usd_per_1k_input": 0.0001,
    "usd_per_1k_output": 0.0002,
}

BUNDLES = {
    "2026.10.1": {
        "description": "Azure OpenAI gpt-4o (pinned), OpenAI gpt-4o fallback. Not validated yet.",
        "routes": {step: _route(AZURE, OPENAI, MAX_TOKENS[step]) for step in PROMPTS},
    },
    "mock-2026.10.1": {
        "description": "MockLLM for development and CI. Never served outside dev and test.",
        "routes": {step: _route(MOCK, MOCK, MAX_TOKENS[step]) for step in PROMPTS},
    },
}


def bundle(version: str) -> dict[str, object]:
    spec = BUNDLES[version]
    return {
        "version": version,
        "description": spec["description"],
        "prompts": PROMPTS,
        "routes": spec["routes"],
        "thresholds": THRESHOLDS,
        "retrieval": RETRIEVAL,
    }


if __name__ == "__main__":
    for version in BUNDLES:
        directory = HERE / version
        directory.mkdir(exist_ok=True)
        (directory / "bundle.json").write_text(
            json.dumps(bundle(version), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
