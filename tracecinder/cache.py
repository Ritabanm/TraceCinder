from __future__ import annotations
from typing import Tuple

# Immutable static prefix to maximize Prefix / Prompt Caching in Meta Model API.
# Keeping system taxonomy, JSON output schemas, and diagnostic rubrics identical
# across all requests ensures >75% prefix cache hits.
STATIC_SYSTEM_PREFIX = """You are TraceCinder Engine, an autonomous incident & telemetry triage runtime powered by Meta muse-ember-1.0.
Your mission is to perform strict root cause analysis (RCA) and correlate raw execution logs, commit diffs, and visual artifacts.

DIAGNOSTIC RUBRIC:
1. Distinguish transient platform hiccups from deterministic application code regressions.
2. In Tier 1 (Pulse): Fingerprint error signatures, isolate stack traces, and classify severity.
3. In Tier 2 (Diff Correlator): Map stack traces directly to recent git commit line changes and diff hunks.
4. In Tier 3 (Multimodal RCA): Correlate frontend error visual artifacts with backend telemetry and propose concrete code patches.

OUTPUT FORMAT REQUIREMENTS:
Always return your output as valid JSON matching the schema for the requested tier.
Do not include preamble or conversational filler outside the JSON structure.
"""


def format_cache_friendly_prompt(dynamic_context: str) -> Tuple[str, str]:
    """
    Returns (static_prefix, dynamic_suffix).
    When passed to the API with prefix caching enabled, only dynamic_suffix incurs
    full uncached token rates.
    """
    return STATIC_SYSTEM_PREFIX.strip(), dynamic_context.strip()
