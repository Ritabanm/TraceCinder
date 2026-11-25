from __future__ import annotations
import re
from typing import List, Tuple

# Approximating 4 characters per token
MAX_CHUNK_TOKENS = 180_000
MAX_CHUNK_CHARS = MAX_CHUNK_TOKENS * 4

ANOMALY_PATTERNS = [
    re.compile(r"(?i)(error|exception|fatal|panic|traceback|undefined|unhandled|critical|segfault)"),
    re.compile(r"HTTP/\d\.\d\s+5\d\d"),
    re.compile(r"status[:=]\s*5\d\d"),
    re.compile(r"status_code[:=]\s*5\d\d"),
]


class TelemetryChunker:
    """
    Parses arbitrary-size execution logs and bounds token consumption under 200K tokens.
    Extracts high-density anomaly windows with configurable preceding/following context.
    """

    def __init__(self, max_tokens: int = MAX_CHUNK_TOKENS):
        self.max_tokens = max_tokens
        self.max_chars = max_tokens * 4

    def find_anomalies(self, lines: List[str]) -> List[int]:
        anomaly_indices = []
        for idx, line in enumerate(lines):
            if any(pattern.search(line) for pattern in ANOMALY_PATTERNS):
                anomaly_indices.append(idx)
        return anomaly_indices

    def extract_anomaly_window(
        self, log_content: str, context_before: int = 50, context_after: int = 70
    ) -> str:
        lines = log_content.splitlines()
        if not lines:
            return ""

        anomaly_indices = self.find_anomalies(lines)
        if not anomaly_indices:
            # No obvious keyword match, return tail window
            tail_lines = lines[-min(len(lines), 300):]
            return "\n".join(tail_lines)

        # Merge overlapping context windows
        ranges: List[Tuple[int, int]] = []
        for idx in anomaly_indices:
            start = max(0, idx - context_before)
            end = min(len(lines), idx + context_after + 1)
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))

        extracted_lines: List[str] = []
        for start, end in ranges:
            extracted_lines.append(f"\n--- [LOG STREAM TELEMETRY WINDOW L{start+1}-L{end}] ---")
            extracted_lines.extend(lines[start:end])

        joined = "\n".join(extracted_lines)

        # Enforce hard ceiling under <200K tokens
        if len(joined) > self.max_chars:
            joined = joined[: self.max_chars] + "\n... [TRUNCATED TO ENFORCE <200K TOKEN BOUNDARY] ..."

        return joined
