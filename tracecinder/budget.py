from __future__ import annotations
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional
from tracecinder.models import TokenUsage

# Pricing based on Meta Muse Ember rates ($ / Million tokens)
# Input: $0.10 / 1M tokens ($0.00000010 / token)
# Cached Prompt (Prefix Hit): $0.025 / 1M tokens ($0.000000025 / token)
# Completion (Output): $0.40 / 1M tokens ($0.00000040 / token)
PRICE_PER_PROMPT_TOKEN = 0.10 / 1_000_000
PRICE_PER_CACHED_PROMPT_TOKEN = 0.025 / 1_000_000
PRICE_PER_COMPLETION_TOKEN = 0.40 / 1_000_000


class BudgetExceededError(Exception):
    """Raised when projected or actual token spend exceeds the strict cap."""
    pass


@dataclass
class BudgetLedger:
    cap_usd: float = 10.00
    spent_usd: float = 0.0
    total_prompt_tokens: int = 0
    total_cached_tokens: int = 0
    total_completion_tokens: int = 0
    incidents_triaged: int = 0
    ledger_path: Optional[Path] = None

    @property
    def audit_log_path(self) -> Optional[Path]:
        if self.ledger_path:
            return self.ledger_path.parent / "usage_audit.jsonl"
        return None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> BudgetLedger:
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                return cls(
                    cap_usd=data.get("cap_usd", 10.00),
                    spent_usd=data.get("spent_usd", 0.0),
                    total_prompt_tokens=data.get("total_prompt_tokens", 0),
                    total_cached_tokens=data.get("total_cached_tokens", 0),
                    total_completion_tokens=data.get("total_completion_tokens", 0),
                    incidents_triaged=data.get("incidents_triaged", 0),
                    ledger_path=path,
                )
            except Exception:
                pass
        return cls(ledger_path=path)

    def save(self) -> None:
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "cap_usd": self.cap_usd,
                "spent_usd": round(self.spent_usd, 8),
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_cached_tokens": self.total_cached_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "incidents_triaged": self.incidents_triaged,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            self.ledger_path.write_text(json.dumps(data, indent=2))

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def percent_used(self) -> float:
        if self.cap_usd <= 0:
            return 100.0
        return min(100.0, (self.spent_usd / self.cap_usd) * 100.0)

    def calculate_cost(
        self, prompt_tokens: int, cached_tokens: int, completion_tokens: int
    ) -> float:
        uncached = max(0, prompt_tokens - cached_tokens)
        cost = (
            (uncached * PRICE_PER_PROMPT_TOKEN)
            + (cached_tokens * PRICE_PER_CACHED_PROMPT_TOKEN)
            + (completion_tokens * PRICE_PER_COMPLETION_TOKEN)
        )
        return round(cost, 8)

    def check_headroom(self, projected_tokens: int) -> bool:
        """Verify if we have enough budget remaining before triggering an escalation tier."""
        projected_cost = (projected_tokens * PRICE_PER_PROMPT_TOKEN) + (500 * PRICE_PER_COMPLETION_TOKEN)
        if self.spent_usd + projected_cost > self.cap_usd:
            raise BudgetExceededError(
                f"Strict budget cap breached! Projected cost ${projected_cost:.5f} exceeds remaining budget ${self.remaining_usd:.5f}."
            )
        return True

    def record_usage(
        self,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        tier_name: str = "unknown",
        incident_id: str = "unknown",
    ) -> TokenUsage:
        cost = self.calculate_cost(prompt_tokens, cached_tokens, completion_tokens)
        if self.spent_usd + cost > self.cap_usd:
            raise BudgetExceededError(
                f"Cost ${cost:.5f} exceeds remaining budget ${self.remaining_usd:.5f} (Cap: ${self.cap_usd:.2f})"
            )
        self.spent_usd += cost
        self.total_prompt_tokens += prompt_tokens
        self.total_cached_tokens += cached_tokens
        self.total_completion_tokens += completion_tokens
        self.save()

        # Append to granular audit log
        if self.audit_log_path:
            audit_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "incident_id": incident_id,
                "tier": tier_name,
                "prompt_tokens": prompt_tokens,
                "cached_tokens": cached_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "cumulative_spent_usd": round(self.spent_usd, 6),
                "remaining_usd": round(self.remaining_usd, 6),
            }
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit_entry) + "\n")

        return TokenUsage(
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
