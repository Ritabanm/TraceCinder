from __future__ import annotations
from pathlib import Path
from typing import List, Optional
from tracecinder.budget import BudgetLedger
from tracecinder.chunker import TelemetryChunker
from tracecinder.models import (
    EscalationTier,
    IncidentBundle,
    IncidentReport,
    IncidentSeverity,
    TokenUsage,
    TriageStepResult,
)
from tracecinder.provider import MuseEmberProvider
from tracecinder.verifier import PatchVerifier


class ReasoningEscalator:
    """
    Executes dynamic minimal-to-max reasoning escalation across tiers:
    Tier 1 (Pulse) -> Tier 2 (Diff Correlator) -> Tier 3 (Multimodal RCA).
    Monitors budget consumption at each threshold.
    """

    def __init__(self, provider: MuseEmberProvider, budget_ledger: BudgetLedger):
        self.provider = provider
        self.budget_ledger = budget_ledger
        self.chunker = TelemetryChunker()

    def run_triage(
        self, bundle: IncidentBundle, max_tier: EscalationTier = EscalationTier.MULTIMODAL_RCA
    ) -> IncidentReport:
        steps: List[TriageStepResult] = []

        # Read & chunk telemetry logs under <200K tokens
        log_text = Path(bundle.log_path).read_text() if Path(bundle.log_path).exists() else ""
        anomaly_window = self.chunker.extract_anomaly_window(log_text)

        # -------------------------------------------------------------------------
        # TIER 1: Pulse Triage (Minimal reasoning, fast fingerprinting)
        # -------------------------------------------------------------------------
        tier1_prompt = f"""[INCIDENT TELEMETRY - TIER 1 PULSE]
Service: {bundle.service}
Timestamp: {bundle.timestamp}
Summary: {bundle.summary}

RAW TELEMETRY LOG WINDOW (<200K Chunks):
{anomaly_window}

TASK:
1. Extract the canonical error fingerprint and exception signature.
2. Determine severity (LOW, MEDIUM, HIGH, CRITICAL).
3. If this is a complex or breaking incident with code changes, escalate to "tier_2_diff".
"""
        step1 = self.provider.execute_tier(
            tier=EscalationTier.PULSE,
            prompt_context=tier1_prompt,
            budget_ledger=self.budget_ledger,
            incident_id=bundle.incident_id,
        )
        steps.append(step1)

        # Check if escalation to Tier 2 required (either model requested it, or severe incident with git diff present)
        should_escalate_t2 = (
            step1.escalate_to == EscalationTier.DIFF_CORRELATOR
            or (bundle.diff_path and step1.severity in (IncidentSeverity.HIGH, IncidentSeverity.CRITICAL))
        )
        if not should_escalate_t2 or max_tier == EscalationTier.PULSE:
            return self._build_report(bundle, steps)

        # -------------------------------------------------------------------------
        # TIER 2: Diff Correlator (Correlate logs with git commit diffs)
        # -------------------------------------------------------------------------
        diff_text = ""
        if bundle.diff_path and Path(bundle.diff_path).exists():
            diff_text = Path(bundle.diff_path).read_text()

        tier2_prompt = f"""[INCIDENT TELEMETRY - TIER 2 DIFF CORRELATION]
Service: {bundle.service}
Previous Fingerprint: {step1.fingerprint}
Identified Root Cause Hint: {step1.root_cause}

GIT COMMIT DIFF:
{diff_text}

TASK:
1. Cross-reference the stack trace / error with the git diff lines.
2. Identify the culprit file and line numbers.
3. If UI anomalies or screenshots are present, set "escalate_to": "tier_3_multimodal".
"""
        step2 = self.provider.execute_tier(
            tier=EscalationTier.DIFF_CORRELATOR,
            prompt_context=tier2_prompt,
            budget_ledger=self.budget_ledger,
            incident_id=bundle.incident_id,
        )
        steps.append(step2)

        # Check if escalation to Tier 3 required (either model requested it, or screenshot artifact is present)
        should_escalate_t3 = (
            step2.escalate_to == EscalationTier.MULTIMODAL_RCA
            or bool(bundle.screenshot_path)
        )
        if not should_escalate_t3 or max_tier == EscalationTier.DIFF_CORRELATOR:
            return self._build_report(bundle, steps)

        # -------------------------------------------------------------------------
        # TIER 3: Multimodal RCA (Correlate UI Screenshot/Clip + Diff + Stack Trace)
        # -------------------------------------------------------------------------
        tier3_prompt = f"""[INCIDENT TELEMETRY - TIER 3 MULTIMODAL RCA]
Service: {bundle.service}
Culprit Code: {step2.culprit_file or 'Identified in recent diff'} ({step2.culprit_line_range or ''})
Diff Analysis: {step2.root_cause or step1.root_cause}

TASK:
1. Analyze the attached UI failure screenshot / screen recording frame.
2. Correlate visual error states (modal locks, HTTP error toast, blank canvas) with backend logs and commit diff.
3. Formulate the definitive Root Cause Analysis (RCA) and generate a verified unified git diff or code fix in "proposed_remediation".
Set "escalate_to": null.
"""
        step3 = self.provider.execute_tier(
            tier=EscalationTier.MULTIMODAL_RCA,
            prompt_context=tier3_prompt,
            budget_ledger=self.budget_ledger,
            image_path=bundle.screenshot_path,
            incident_id=bundle.incident_id,
        )
        steps.append(step3)

        return self._build_report(bundle, steps)

    def _build_report(
        self, bundle: IncidentBundle, steps: List[TriageStepResult]
    ) -> IncidentReport:
        last_step = steps[-1]
        total_prompt = sum(s.usage.prompt_tokens for s in steps)
        total_cached = sum(s.usage.cached_prompt_tokens for s in steps)
        total_completion = sum(s.usage.completion_tokens for s in steps)
        total_cost = sum(s.usage.cost_usd for s in steps)

        total_usage = TokenUsage(
            prompt_tokens=total_prompt,
            cached_prompt_tokens=total_cached,
            completion_tokens=total_completion,
            cost_usd=round(total_cost, 6),
        )

        correlated_diff = None
        if last_step.culprit_file:
            correlated_diff = {
                "file": last_step.culprit_file,
                "lines": last_step.culprit_line_range or "unknown",
            }

        patch_file = None
        verif_status = None
        verif_details = None

        if last_step.proposed_remediation:
            saved_patch = PatchVerifier.save_patch_file(
                raw_patch=last_step.proposed_remediation,
                target_file=last_step.culprit_file,
            )
            patch_file = str(saved_patch)
            verif_status, verif_details = PatchVerifier.verify_patch(
                patch_path=saved_patch,
                raw_patch=last_step.proposed_remediation,
                file_path=last_step.culprit_file,
            )

        return IncidentReport(
            incident_id=bundle.incident_id,
            final_tier=last_step.tier,
            severity=last_step.severity,
            summary=bundle.summary,
            root_cause=last_step.root_cause,
            correlated_diff=correlated_diff,
            visual_finding=last_step.visual_evidence,
            proposed_remediation=last_step.proposed_remediation,
            confidence=last_step.confidence,
            patch_file_path=patch_file,
            verification_status=verif_status,
            verification_details=verif_details,
            steps=steps,
            total_usage=total_usage,
            budget_remaining_usd=self.budget_ledger.remaining_usd,
        )
