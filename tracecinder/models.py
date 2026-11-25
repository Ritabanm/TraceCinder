from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EscalationTier(str, Enum):
    PULSE = "tier_1_pulse"              # Fast log fingerprinting & known-issue triage
    DIFF_CORRELATOR = "tier_2_diff"     # Pinpoint commit diff correlation
    MULTIMODAL_RCA = "tier_3_multimodal"# Full cross-modal RCA with UI visual + patch generation


class IncidentSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class IncidentBundle:
    incident_id: str
    service: str
    summary: str
    timestamp: str
    log_path: str
    diff_path: Optional[str] = None
    screenshot_path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TriageStepResult:
    tier: EscalationTier
    fingerprint: str
    severity: IncidentSeverity
    culprit_file: Optional[str] = None
    culprit_line_range: Optional[str] = None
    visual_evidence: Optional[str] = None
    root_cause: str = ""
    proposed_remediation: str = ""
    confidence: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    escalate_to: Optional[EscalationTier] = None
    raw_response: str = ""


@dataclass
class IncidentReport:
    incident_id: str
    final_tier: EscalationTier
    severity: IncidentSeverity
    summary: str
    root_cause: str
    correlated_diff: Optional[Dict[str, str]] = None
    visual_finding: Optional[str] = None
    proposed_remediation: str = ""
    confidence: float = 0.0
    steps: List[TriageStepResult] = field(default_factory=list)
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    budget_remaining_usd: float = 10.00
