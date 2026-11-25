from __future__ import annotations
import base64
import json
import os
import urllib.request
from typing import Any, Dict, Optional, Tuple
from tracecinder.budget import BudgetLedger
from tracecinder.cache import format_cache_friendly_prompt
from tracecinder.models import EscalationTier, IncidentSeverity, TriageStepResult

DEFAULT_MODEL = "meta-llama/muse-ember-1.0"


class MuseEmberProvider:
    """
    Adapter for Meta Model API (muse-ember-1.0) with native multimodal input,
    prefix caching support, and a high-fidelity local simulator for zero-credit testing.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        mock_mode: bool = False,
    ):
        self.api_key = api_key or os.environ.get("META_API_KEY") or os.environ.get("MUSE_API_KEY")
        self.base_url = base_url or os.environ.get("META_API_BASE", "https://api.meta.ai/v1")
        self.model = model
        self.mock_mode = mock_mode or (not self.api_key)

    def _encode_image_b64(self, image_path: str) -> Optional[str]:
        if not os.path.exists(image_path):
            return None
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def execute_tier(
        self,
        tier: EscalationTier,
        prompt_context: str,
        budget_ledger: BudgetLedger,
        image_path: Optional[str] = None,
    ) -> TriageStepResult:
        static_sys, dynamic_user = format_cache_friendly_prompt(prompt_context)

        # Estimate tokens roughly
        estimated_prompt_tokens = int((len(static_sys) + len(dynamic_user)) / 3.8)
        # Prefix caching assumes ~80% of system prompt is cached
        estimated_cached_tokens = int(len(static_sys) / 4.0)

        # Pre-flight budget headroom check
        budget_ledger.check_headroom(estimated_prompt_tokens)

        if self.mock_mode:
            return self._mock_tier_response(
                tier, dynamic_user, budget_ledger, estimated_prompt_tokens, estimated_cached_tokens
            )

        return self._call_meta_api(
            tier, static_sys, dynamic_user, image_path, budget_ledger
        )

    def _mock_tier_response(
        self,
        tier: EscalationTier,
        context: str,
        budget_ledger: BudgetLedger,
        prompt_tokens: int,
        cached_tokens: int,
    ) -> TriageStepResult:
        """Deterministic simulation for local offline preview and testing."""
        completion_tokens = 320

        if tier == EscalationTier.PULSE:
            usage = budget_ledger.record_usage(prompt_tokens, cached_tokens, completion_tokens=180)
            return TriageStepResult(
                tier=tier,
                fingerprint="ERR_AUTH_NULL_AVATAR: TypeError at user_service/serializer.py",
                severity=IncidentSeverity.HIGH,
                root_cause="Unhandled NoneType on user avatar_url property during payload serialization.",
                proposed_remediation="Add safe navigation or fallback default to user profile serializer.",
                confidence=0.88,
                usage=usage,
                escalate_to=EscalationTier.DIFF_CORRELATOR,
                raw_response="Identified recurring 500 burst on /v1/checkout/session endpoint.",
            )

        elif tier == EscalationTier.DIFF_CORRELATOR:
            usage = budget_ledger.record_usage(prompt_tokens, cached_tokens, completion_tokens=260)
            return TriageStepResult(
                tier=tier,
                fingerprint="COMMIT_CORRELATION: a8f41bc ('Refactor UserProfile to strict schemas')",
                severity=IncidentSeverity.CRITICAL,
                culprit_file="src/services/user_serializer.py",
                culprit_line_range="L42-L48",
                root_cause="Commit a8f41bc removed nullable check on 'avatar_url', causing crash when guest accounts initiate checkout.",
                proposed_remediation="Reinstate 'avatar_url: Optional[str] = None' and guard serializer dictionary access.",
                confidence=0.96,
                usage=usage,
                escalate_to=EscalationTier.MULTIMODAL_RCA,
                raw_response="Direct correlation found between commit a8f41bc and production crash stack trace.",
            )

        elif tier == EscalationTier.MULTIMODAL_RCA:
            usage = budget_ledger.record_usage(prompt_tokens, cached_tokens, completion_tokens=420)
            return TriageStepResult(
                tier=tier,
                fingerprint="MULTIMODAL_CROSS_VALIDATED: UI Checkout Modal Lockup",
                severity=IncidentSeverity.CRITICAL,
                culprit_file="src/services/user_serializer.py",
                culprit_line_range="L44-L46",
                visual_evidence="Screenshot shows persistent spinning modal on Checkout button with red toast: 'Payment Failed: Internal Server Error'.",
                root_cause="Frontend checkout action is blocked waiting on /v1/user/profile response, which crashes with 500 due to unhandled None avatar in commit a8f41bc.",
                proposed_remediation=(
                    "```python\n"
                    "- avatar_url: str = profile['avatar_url']\n"
                    "+ avatar_url: Optional[str] = profile.get('avatar_url') or DEFAULT_AVATAR_PLACEHOLDER\n"
                    "```"
                ),
                confidence=0.99,
                usage=usage,
                escalate_to=None,
                raw_response="Full multimodal correlation confirmed between UI failure screen, network telemetry, and git diff.",
            )

        return TriageStepResult(
            tier=tier,
            fingerprint="UNKNOWN",
            severity=IncidentSeverity.LOW,
            usage=budget_ledger.record_usage(100, 50, 50),
        )

    def _call_meta_api(
        self,
        tier: EscalationTier,
        system_prompt: str,
        user_prompt: str,
        image_path: Optional[str],
        budget_ledger: BudgetLedger,
    ) -> TriageStepResult:
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        user_content: Any = user_prompt
        if image_path and os.path.exists(image_path):
            b64_img = self._encode_image_b64(image_path)
            if b64_img:
                user_content = [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
                    },
                ]

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        usage_data = data.get("usage", {})
        prompt_tokens = usage_data.get("prompt_tokens", 500)
        cached_tokens = usage_data.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        completion_tokens = usage_data.get("completion_tokens", 250)

        usage = budget_ledger.record_usage(prompt_tokens, cached_tokens, completion_tokens)
        content_str = data["choices"][0]["message"]["content"]
        parsed = json.loads(content_str)

        return TriageStepResult(
            tier=tier,
            fingerprint=parsed.get("fingerprint", "GENERIC_EXCEPTION"),
            severity=IncidentSeverity(parsed.get("severity", "MEDIUM").upper()),
            culprit_file=parsed.get("culprit_file"),
            culprit_line_range=parsed.get("culprit_line_range"),
            visual_evidence=parsed.get("visual_evidence"),
            root_cause=parsed.get("root_cause", ""),
            proposed_remediation=parsed.get("proposed_remediation", ""),
            confidence=float(parsed.get("confidence", 0.85)),
            usage=usage,
            escalate_to=EscalationTier(parsed["escalate_to"]) if parsed.get("escalate_to") else None,
            raw_response=content_str,
        )
