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
        has_diff = bool(bundle.diff_path and Path(bundle.diff_path).exists())
        has_screen = bool(bundle.screenshot_path and Path(bundle.screenshot_path).exists())
        has_audio = bool(bundle.audio_path and (Path(bundle.audio_path).exists() or str(bundle.audio_path).startswith("data:")))

        tier1_prompt = f"""[INCIDENT TELEMETRY - TIER 1 PULSE]
Service: {bundle.service}
Timestamp: {bundle.timestamp}
Summary: {bundle.summary}
Has Commit Diff: {has_diff}
Has UI Screenshot: {has_screen}
Has Engineer Voice Note: {has_audio}

RAW TELEMETRY LOG WINDOW (<200K Chunks):
{anomaly_window}

TASK:
1. Extract the canonical error fingerprint and exception signature.
2. Determine severity (LOW, MEDIUM, HIGH, CRITICAL).
3. Identify culprit file and line numbers from stack trace if visible.
4. If a commit diff is available to correlate, set "escalate_to": "tier_2_diff".
5. If no diff is available but a UI screenshot or voice note is present, set "escalate_to": "tier_3_multimodal".
6. If neither diff nor screenshot nor voice note is present, formulate root_cause and propose an atomic code remediation patch in "proposed_remediation" directly from the stack trace and set "escalate_to": null.
"""
        step1 = self.provider.execute_tier(
            tier=EscalationTier.PULSE,
            prompt_context=tier1_prompt,
            budget_ledger=self.budget_ledger,
            incident_id=bundle.incident_id,
        )
        steps.append(step1)

        # Handle direct escalation to Tier 3 if no diff exists but screenshot/audio is present
        if not has_diff and (has_screen or has_audio) and max_tier != EscalationTier.PULSE:
            voice_prompt_item = (
                "4. Listen to the attached native voice note describing the bug symptoms/expected behavior. In 'spoken_context', state the engineer's spoken symptom and align it with the failure analysis.\n"
                if has_audio else ""
            )
            tier3_direct_prompt = f"""[INCIDENT TELEMETRY - TIER 3 MULTIMODAL RCA]
Service: {bundle.service}
Culprit Hint: {step1.culprit_file or 'Identified in stack trace'} ({step1.culprit_line_range or ''})
Stack Trace Findings: {step1.root_cause}

TASK:
1. Analyze the attached UI failure screenshot / screen recording frame (if present).
2. Correlate visual error states (modal locks, HTTP error toast, blank canvas, console error) with backend logs and stack trace.
3. Formulate the definitive Root Cause Analysis (RCA) and generate a verified unified git diff or code fix in "proposed_remediation".
{voice_prompt_item}Set "escalate_to": null.
"""
            step3 = self.provider.execute_tier(
                tier=EscalationTier.MULTIMODAL_RCA,
                prompt_context=tier3_direct_prompt,
                budget_ledger=self.budget_ledger,
                image_path=bundle.screenshot_path,
                audio_path=bundle.audio_path,
                incident_id=bundle.incident_id,
            )
            steps.append(step3)
            return self._build_report(bundle, steps)

        # Check if escalation to Tier 2 required (either model requested it, or severe incident with git diff present)
        should_escalate_t2 = (
            step1.escalate_to == EscalationTier.DIFF_CORRELATOR
            or (has_diff and step1.severity in (IncidentSeverity.HIGH, IncidentSeverity.CRITICAL))
        )
        if not should_escalate_t2 or not has_diff or max_tier == EscalationTier.PULSE:
            return self._build_report(bundle, steps)

        # -------------------------------------------------------------------------
        # TIER 2: Diff Correlator (Correlate logs with git commit diffs)
        # -------------------------------------------------------------------------
        diff_text = Path(bundle.diff_path).read_text() if bundle.diff_path and Path(bundle.diff_path).exists() else ""

        tier2_prompt = f"""[INCIDENT TELEMETRY - TIER 2 DIFF CORRELATION]
Service: {bundle.service}
Previous Fingerprint: {step1.fingerprint}
Identified Root Cause Hint: {step1.root_cause}

GIT COMMIT DIFF:
{diff_text}

TASK:
1. Cross-reference the stack trace / error with the git diff lines.
2. Identify the culprit file and line numbers.
3. Formulate the root cause and generate a concrete unified git diff or code fix in "proposed_remediation".
4. If a UI failure screenshot is present, set "escalate_to": "tier_3_multimodal", otherwise set "escalate_to": null.
"""
        step2 = self.provider.execute_tier(
            tier=EscalationTier.DIFF_CORRELATOR,
            prompt_context=tier2_prompt,
            budget_ledger=self.budget_ledger,
            incident_id=bundle.incident_id,
        )
        steps.append(step2)

        # Check if escalation to Tier 3 required (either model requested it, or screenshot/audio artifact is present)
        should_escalate_t3 = (
            step2.escalate_to == EscalationTier.MULTIMODAL_RCA
            or has_screen
            or has_audio
        )
        if not should_escalate_t3 or (not has_screen and not has_audio) or max_tier == EscalationTier.DIFF_CORRELATOR:
            return self._build_report(bundle, steps)

        # -------------------------------------------------------------------------
        # TIER 3: Multimodal RCA (Correlate UI Screenshot/Clip + Audio + Diff + Stack Trace)
        # -------------------------------------------------------------------------
        voice_task_item = (
            "4. Cross-reference the attached engineer voice note description (symptom / intended behavior) with the visual capture and diff. State this alignment in 'spoken_context' and cite it in 'root_cause'.\n"
            if has_audio else ""
        )
        tier3_prompt = f"""[INCIDENT TELEMETRY - TIER 3 MULTIMODAL RCA]
Service: {bundle.service}
Culprit Code: {step2.culprit_file or step1.culprit_file or 'Identified in recent diff'} ({step2.culprit_line_range or step1.culprit_line_range or ''})
Diff Analysis: {step2.root_cause or step1.root_cause}

TASK:
1. Analyze the attached UI failure screenshot / screen recording frame (if present).
2. Correlate visual error states (modal locks, HTTP error toast, blank canvas) with backend logs and commit diff.
3. Formulate the definitive Root Cause Analysis (RCA) and generate a verified unified git diff or code fix in "proposed_remediation".
{voice_task_item}Set "escalate_to": null.
"""
        step3 = self.provider.execute_tier(
            tier=EscalationTier.MULTIMODAL_RCA,
            prompt_context=tier3_prompt,
            budget_ledger=self.budget_ledger,
            image_path=bundle.screenshot_path,
            audio_path=bundle.audio_path,
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

        culprit_file = None
        culprit_lines = None
        proposed_rem = None
        root_cause = last_step.root_cause
        visual_finding = last_step.visual_evidence
        spoken_context = None

        for s in reversed(steps):
            if s.culprit_file and not culprit_file:
                culprit_file = s.culprit_file
                culprit_lines = s.culprit_line_range
            if s.proposed_remediation and not proposed_rem:
                proposed_rem = s.proposed_remediation
            if s.root_cause and not root_cause:
                root_cause = s.root_cause
            if s.visual_evidence and not visual_finding:
                visual_finding = s.visual_evidence
            if s.spoken_context and not spoken_context:
                spoken_context = s.spoken_context

        correlated_diff = None
        if culprit_file:
            correlated_diff = {
                "file": culprit_file,
                "lines": culprit_lines or "unknown",
            }

        patch_file = None
        verif_status = None
        verif_details = None

        if proposed_rem:
            saved_patch = PatchVerifier.save_patch_file(
                raw_patch=proposed_rem,
                target_file=culprit_file,
            )
            patch_file = str(saved_patch)
            verif_status, verif_details = PatchVerifier.verify_patch(
                patch_path=saved_patch,
                raw_patch=proposed_rem,
                file_path=culprit_file,
            )

        return IncidentReport(
            incident_id=bundle.incident_id,
            final_tier=last_step.tier,
            severity=last_step.severity,
            summary=bundle.summary,
            root_cause=root_cause or "Automated telemetry diagnosis completed.",
            correlated_diff=correlated_diff,
            visual_finding=visual_finding,
            spoken_context=spoken_context,
            proposed_remediation=proposed_rem or "",
            confidence=max(s.confidence for s in steps) if steps else 0.90,
            patch_file_path=patch_file,
            verification_status=verif_status,
            verification_details=verif_details,
            steps=steps,
            total_usage=total_usage,
            budget_remaining_usd=self.budget_ledger.remaining_usd,
        )
