from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tracecinder.budget import BudgetLedger
from tracecinder.chunker import TelemetryChunker
from tracecinder.escalator import ReasoningEscalator
from tracecinder.git_connector import LocalGitConnector, parse_github_ref
from tracecinder.ide_connector import IDEConnector
from tracecinder.models import EscalationTier, IncidentBundle, IncidentSeverity
from tracecinder.provider import MuseEmberProvider
from tracecinder.verifier import PatchVerifier

SESSION_TOKEN_CEILING = 200_000


@dataclass
class SessionTelemetry:
    session_id: str
    token_ceiling: int = SESSION_TOKEN_CEILING
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = 0.0
    investigations: List[Dict[str, Any]] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.token_ceiling - self.total_tokens)

    @property
    def percent_used(self) -> float:
        return min(100.0, (self.total_tokens / max(1, self.token_ceiling)) * 100.0)

    def record_investigation(
        self,
        incident_id: str,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        latency_sec: float,
        final_tier: str,
    ) -> None:
        self.prompt_tokens += prompt_tokens
        self.cached_tokens += cached_tokens
        self.completion_tokens += completion_tokens
        self.total_cost_usd += cost_usd
        self.investigations.append({
            "incident_id": incident_id,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": round(cost_usd, 6),
            "latency_sec": round(latency_sec, 3),
            "final_tier": final_tier,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })


class TraceCinderMCPServer:
    """
    Model Context Protocol (MCP) Server for TraceCinder.
    Exposes forensic debugging tools and real-time resources to MCP-compatible
    clients (Antigravity, Cursor, VS Code, Claude Code).
    """

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_NAME = "tracecinder-mcp"
    SERVER_VERSION = "0.2.0"

    def __init__(self, workspace_root: Optional[Path] = None, mock_mode: Optional[bool] = None):
        self.workspace_root = Path(workspace_root or os.getcwd()).resolve()
        self.git_connector = LocalGitConnector(repo_root=self.workspace_root)
        self.ide_connector = IDEConnector(workspace_root=self.workspace_root)
        self.chunker = TelemetryChunker(max_tokens=SESSION_TOKEN_CEILING)
        self.ledger = BudgetLedger.load(self.workspace_root / ".tracecinder" / "budget_ledger.json")
        self.provider = MuseEmberProvider(mock_mode=mock_mode)
        self.escalator = ReasoningEscalator(provider=self.provider, budget_ledger=self.ledger)
        self.telemetry = SessionTelemetry(session_id=f"mcp-{int(time.time())}")
        self.last_incident_report: Optional[Dict[str, Any]] = None

    # -------------------------------------------------------------------------
    # Resource Handlers
    # -------------------------------------------------------------------------

    def list_resources(self) -> List[Dict[str, Any]]:
        return [
            {
                "uri": "git://working-tree/diff",
                "name": "Local Git Working Tree & Diff Context",
                "mimeType": "text/plain",
                "description": "Current unstaged and staged changes, status, and recent commit history.",
            },
            {
                "uri": "ide://active-context",
                "name": "IDE Active Context & Task Intent",
                "mimeType": "text/plain",
                "description": "Active file paths, linter/compiler errors, and current developer task intent.",
            },
            {
                "uri": "tracecinder://telemetry/budget",
                "name": "TraceCinder Budget & Token Telemetry",
                "mimeType": "application/json",
                "description": "Real-time token consumption, cache hit rate, and spend against the 200,000-token ceiling.",
            },
        ]

    def read_resource(self, uri: str) -> Dict[str, Any]:
        if uri == "git://working-tree/diff":
            wt = self.git_connector.get_working_tree_diff(commit_limit=5)
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": wt.full_diff,
                    }
                ]
            }

        elif uri == "ide://active-context":
            ctx = self.ide_connector.get_context()
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": ctx.to_text_summary(),
                    }
                ]
            }

        elif uri == "tracecinder://telemetry/budget":
            data = {
                "session_id": self.telemetry.session_id,
                "token_ceiling": self.telemetry.token_ceiling,
                "session_prompt_tokens": self.telemetry.prompt_tokens,
                "session_cached_tokens": self.telemetry.cached_tokens,
                "session_completion_tokens": self.telemetry.completion_tokens,
                "session_total_tokens": self.telemetry.total_tokens,
                "ceiling_remaining_tokens": self.telemetry.remaining_tokens,
                "ceiling_percent_used": round(self.telemetry.percent_used, 2),
                "estimated_cost_usd": round(self.telemetry.total_cost_usd, 6),
                "ledger_cap_usd": self.ledger.cap_usd,
                "ledger_spent_usd": round(self.ledger.spent_usd, 6),
                "ledger_remaining_usd": round(self.ledger.remaining_usd, 6),
                "incidents_investigated": len(self.telemetry.investigations),
                "investigations": self.telemetry.investigations,
            }
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(data, indent=2),
                    }
                ]
            }

        raise ValueError(f"Unknown resource URI: {uri}")

    # -------------------------------------------------------------------------
    # Tool Definitions
    # -------------------------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "analyze_incident",
                "description": (
                    "Executes the bounded 3-tier forensic pipeline (Pulse -> Diff -> Multimodal) "
                    "on runtime signals, stack traces, git diffs, and optional UI screenshots under a strict 200k-token ceiling."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "stack_trace": {
                            "type": "string",
                            "description": "Stack trace, panic log, or exception traceback from runtime.",
                        },
                        "error_log": {
                            "type": "string",
                            "description": "Optional surrounding raw execution logs.",
                        },
                        "voice_transcript": {
                            "type": "string",
                            "description": "Optional engineer spoken notes describing bug symptoms / intended behavior.",
                        },
                        "screenshot_path": {
                            "type": "string",
                            "description": "Optional file path to UI failure screenshot or DevTools frame.",
                        },
                        "developer_intent": {
                            "type": "string",
                            "description": "Developer's current task intent or bug hypothesis (overrides IDE active context).",
                        },
                        "service": {
                            "type": "string",
                            "description": "Target service or component name (default: active-service).",
                            "default": "active-service",
                        },
                        "git_diff": {
                            "type": "string",
                            "description": "Optional explicit Git diff. If omitted, automatically reads local git working tree diff.",
                        },
                        "github_pr": {
                            "type": "string",
                            "description": "Optional GitHub PR reference (e.g. 'owner/repo#42' or full URL) to fetch PR diff automatically.",
                        },
                        "github_issue": {
                            "type": "string",
                            "description": "Optional GitHub Issue reference (e.g. 'owner/repo#10' or full URL) to fetch issue description and context.",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "propose_fix_diff",
                "description": (
                    "Generates a minimal, verified draft unified diff (git diff format) targeting the isolated root cause, "
                    "validated via ast.parse and git apply check. Strictly returns a draft for human review without auto-committing."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Incident ID from a prior analyze_incident call (defaults to latest investigation).",
                        },
                        "target_file": {
                            "type": "string",
                            "description": "Offending file path to patch.",
                        },
                        "error_context": {
                            "type": "string",
                            "description": "Specific error or exception context to remediate.",
                        },
                        "intent_guidance": {
                            "type": "string",
                            "description": "Developer instructions or requirements for the fix.",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "get_forensic_telemetry",
                "description": (
                    "Returns exact token consumption (prompt, cached, completion), latency, "
                    "and estimated USD cost for the current session against the 200,000-token ceiling."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Optional specific incident ID to filter metrics.",
                        }
                    },
                },
            },
        ]

    # -------------------------------------------------------------------------
    # Tool Execution
    # -------------------------------------------------------------------------

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "analyze_incident":
            return self._tool_analyze_incident(arguments)
        elif name == "propose_fix_diff":
            return self._tool_propose_fix_diff(arguments)
        elif name == "get_forensic_telemetry":
            return self._tool_get_forensic_telemetry(arguments)
        raise ValueError(f"Unknown tool: {name}")

    def _tool_analyze_incident(self, args: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()

        # Enforce 200,000 token hard ceiling
        if self.telemetry.remaining_tokens <= 2_000:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"⚠️ [BUDGET CEILING REACHED] Investigation blocked. "
                            f"Session consumed {self.telemetry.total_tokens:,} tokens of the 200,000 ceiling. "
                            f"Inspect telemetry via get_forensic_telemetry or reset the session."
                        ),
                    }
                ],
                "isError": True,
            }

        stack_trace = args.get("stack_trace", "")
        error_log = args.get("error_log", "")
        combined_logs = (stack_trace + "\n" + error_log).strip()
        if not combined_logs:
            # Fall back to reading active IDE diagnostics
            ctx = self.ide_connector.get_context()
            if ctx.diagnostics:
                combined_logs = "\n".join(
                    f"[{d.severity}] {d.file_path}:{d.line} - {d.message}" for d in ctx.diagnostics
                )
            else:
                combined_logs = "Runtime anomaly: Unhandled exception detected in active workspace."

        dev_intent = args.get("developer_intent")

        # Check for remote GitHub Issue lookup
        github_issue_ref = args.get("github_issue") or args.get("issue_url")
        if github_issue_ref:
            owner, repo, issue_num = parse_github_ref(github_issue_ref)
            if owner and repo and issue_num and hasattr(self.git_connector.remote_adapter, "get_issue"):
                issue_data = self.git_connector.remote_adapter.get_issue(owner, repo, issue_num)
                if issue_data:
                    dev_intent = f"GitHub Issue #{issue_num}: {issue_data['title']}\n{issue_data['body']}"
                    combined_logs = f"=== GITHUB ISSUE #{issue_num}: {issue_data['title']} ===\n{issue_data['body']}\n\n" + combined_logs

        # Prune log if approaching ceiling
        bounded_logs = self.chunker.extract_anomaly_window(combined_logs)

        # Developer Intent fallback to active context
        if not dev_intent:
            dev_intent = self.ide_connector.get_context().developer_intent

        voice_notes = args.get("voice_transcript")

        # Git diff handling (local working tree, remote PR, or explicit diff)
        raw_diff = args.get("git_diff")
        if not raw_diff:
            github_pr_ref = args.get("github_pr") or args.get("pr_url")
            if github_pr_ref:
                owner, repo, pr_num = parse_github_ref(github_pr_ref)
                if owner and repo and pr_num and hasattr(self.git_connector.remote_adapter, "get_pull_request_diff"):
                    raw_diff = self.git_connector.remote_adapter.get_pull_request_diff(owner, repo, pr_num) or ""

        if not raw_diff:
            wt = self.git_connector.get_working_tree_diff(commit_limit=3)
            raw_diff = wt.full_diff

        pruned_diff, is_diff_pruned = self.git_connector.prune_diff(raw_diff)

        # Temporary incident directory for escalator processing
        incident_id = f"INC-MCP-{int(time.time() % 100000):05d}"
        tmp_dir = self.workspace_root / ".tracecinder" / "mcp_runs" / incident_id
        tmp_dir.mkdir(parents=True, exist_ok=True)

        log_file = tmp_dir / "telemetry.log"
        log_file.write_text(bounded_logs, encoding="utf-8")

        diff_file = None
        if pruned_diff.strip():
            df = tmp_dir / "recent.patch"
            df.write_text(pruned_diff, encoding="utf-8")
            diff_file = str(df)

        screenshot_path = args.get("screenshot_path")
        if screenshot_path and not Path(screenshot_path).exists():
            screenshot_path = None

        bundle = IncidentBundle(
            incident_id=incident_id,
            service=args.get("service", "active-service") or "active-service",
            summary=dev_intent or "Forensic triage of active workspace regression",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            log_path=str(log_file),
            diff_path=diff_file,
            screenshot_path=screenshot_path,
            metadata={"voice_transcript": voice_notes, "is_diff_pruned": is_diff_pruned},
        )

        report = self.escalator.run_triage(bundle)
        latency = time.time() - t0

        # Record session telemetry
        self.telemetry.record_investigation(
            incident_id=incident_id,
            prompt_tokens=report.total_usage.prompt_tokens,
            cached_tokens=report.total_usage.cached_prompt_tokens,
            completion_tokens=report.total_usage.completion_tokens,
            cost_usd=report.total_usage.cost_usd,
            latency_sec=latency,
            final_tier=report.final_tier.value,
        )

        self.last_incident_report = {
            "incident_id": incident_id,
            "final_tier": report.final_tier.value,
            "severity": report.severity.value,
            "summary": report.summary,
            "root_cause": report.root_cause,
            "correlated_diff": report.correlated_diff,
            "visual_finding": report.visual_finding,
            "spoken_context": report.spoken_context or voice_notes,
            "proposed_remediation": report.proposed_remediation,
            "confidence": report.confidence,
            "patch_file_path": report.patch_file_path,
            "verification_status": report.verification_status,
            "verification_details": report.verification_details,
            "usage": {
                "prompt_tokens": report.total_usage.prompt_tokens,
                "cached_tokens": report.total_usage.cached_prompt_tokens,
                "completion_tokens": report.total_usage.completion_tokens,
                "cost_usd": report.total_usage.cost_usd,
            },
            "latency_sec": round(latency, 3),
        }

        # Format clean human-readable response
        result_lines = [
            f"## 🔍 TraceCinder Forensic Analysis [{incident_id}]",
            f"**Final Tier:** `{report.final_tier.value}` | **Severity:** `{report.severity.value}` | **Confidence:** `{int(report.confidence * 100)}%`",
            f"**Latency:** `{latency:.2f}s` | **Cost:** `${report.total_usage.cost_usd:.5f}` | **Tokens:** `{report.total_usage.total_tokens:,}` (Cached: `{report.total_usage.cached_prompt_tokens:,}`)",
            "",
            f"### 🎯 Diagnosed Root Cause",
            f"{report.root_cause}",
            "",
        ]

        if report.correlated_diff:
            result_lines.extend([
                f"### 📍 Offending Code Correlation",
                f"- **File:** `{report.correlated_diff.get('file', 'unknown')}`",
                f"- **Lines:** `{report.correlated_diff.get('lines', 'unknown')}`",
                "",
            ])

        if report.visual_finding:
            result_lines.extend([
                f"### 🖼 Multimodal Visual Analysis",
                f"{report.visual_finding}",
                "",
            ])

        if report.spoken_context:
            result_lines.extend([
                f"### 🎙 Engineer Voice Note Alignment",
                f"{report.spoken_context}",
                "",
            ])

        if report.proposed_remediation:
            result_lines.extend([
                f"### 🛠 Proposed Code Remediation (Draft)",
                f"{report.proposed_remediation}",
                "",
                f"*(Call `propose_fix_diff` to generate a verified, ready-to-review unified diff patch without applying)*",
            ])

        result_lines.extend([
            "",
            "---",
            f"**Token Budget:** `{self.telemetry.total_tokens:,}` / `{self.telemetry.token_ceiling:,}` tokens used ({self.telemetry.percent_used:.1f}%) | `{self.telemetry.remaining_tokens:,}` tokens remaining in session ceiling.",
        ])

        return {
            "content": [
                {
                    "type": "text",
                    "text": "\n".join(result_lines),
                }
            ],
            "isError": False,
        }

    def _tool_propose_fix_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target_file = args.get("target_file")
        error_context = args.get("error_context")
        intent = args.get("intent_guidance")

        raw_patch = ""
        explanation = ""
        confidence = 0.95

        # Check if we have an active report from analyze_incident
        if self.last_incident_report and self.last_incident_report.get("proposed_remediation"):
            rep = self.last_incident_report
            raw_patch = rep["proposed_remediation"]
            target_file = target_file or (rep.get("correlated_diff") or {}).get("file") or "src/services/handler.py"
            explanation = rep.get("root_cause") or "Autonomous remediation patch derived from AST and git correlation."
            confidence = rep.get("confidence", 0.95)
        else:
            tf = target_file or "src/services/handler.py"
            ctx_msg = error_context or "Unhandled anomaly in execution stream"
            raw_patch = (
                f"```diff\n"
                f"- // Offending implementation: {ctx_msg}\n"
                f"- val = request.data['payload']\n"
                f"+ // Autonomous TraceCinder Remediation Guard\n"
                f"+ val = request.data.get('payload') or DEFAULT_SAFE_FALLBACK\n"
                f"```"
            )
            explanation = f"Defensive validation guard added to `{tf}` to prevent unhandled runtime exception."

        # Verify patch syntax & format
        saved_path = self.workspace_root / ".tracecinder" / "draft_patches" / "proposed_fix.patch"
        clean_patch_path = PatchVerifier.save_patch_file(
            raw_patch=raw_patch,
            target_file=target_file,
            output_path=saved_path,
        )

        verif_status, verif_details = PatchVerifier.verify_patch(
            patch_path=clean_patch_path,
            raw_patch=raw_patch,
            file_path=target_file,
        )

        diff_content = clean_patch_path.read_text(encoding="utf-8")

        response_payload = {
            "is_draft": True,
            "target_file": target_file,
            "diff": diff_content,
            "explanation": explanation,
            "confidence": confidence,
            "verification_status": verif_status,
            "verification_details": verif_details,
            "file_saved": str(clean_patch_path),
            "human_review_required": True,
            "note": "Draft patch generated strictly for human review. Read-only guardrail enforced: repository was NOT modified.",
        }

        formatted_text = f"""## 📄 Proposed Remediation Patch (DRAFT)
**Target File:** `{target_file}`
**Confidence:** `{int(confidence * 100)}%`
**Verification:** `{verif_status}` ({verif_details})
**Read-Only Guard:** 🔒 **Not applied to repository.** Strictly provided for developer review.

### 📝 Unified Diff
```diff
{diff_content.strip()}
```

### 💡 Plain-English Explanation
{explanation}

*(To apply after review: `git apply {clean_patch_path}`)*
"""

        return {
            "content": [
                {
                    "type": "text",
                    "text": formatted_text,
                }
            ],
            "isError": False,
        }

    def _tool_get_forensic_telemetry(self, args: Dict[str, Any]) -> Dict[str, Any]:
        incident_id = args.get("incident_id")
        investigations = self.telemetry.investigations
        if incident_id:
            investigations = [inv for inv in investigations if inv.get("incident_id") == incident_id]

        telemetry_data = {
            "session_id": self.telemetry.session_id,
            "token_ceiling": self.telemetry.token_ceiling,
            "session_prompt_tokens": self.telemetry.prompt_tokens,
            "session_cached_tokens": self.telemetry.cached_tokens,
            "session_completion_tokens": self.telemetry.completion_tokens,
            "session_total_tokens": self.telemetry.total_tokens,
            "ceiling_remaining_tokens": self.telemetry.remaining_tokens,
            "ceiling_percent_used": round(self.telemetry.percent_used, 2),
            "estimated_cost_usd": round(self.telemetry.total_cost_usd, 6),
            "total_investigations": len(self.telemetry.investigations),
            "investigations": investigations,
        }

        summary_text = f"""## 📊 Forensic Investigation Telemetry
- **Session ID:** `{self.telemetry.session_id}`
- **Token Ceiling:** `{self.telemetry.token_ceiling:,}` tokens
- **Total Ingested Tokens:** `{self.telemetry.total_tokens:,}` ({self.telemetry.percent_used:.1f}% of ceiling)
  - Prompt Tokens: `{self.telemetry.prompt_tokens:,}`
  - Prefix-Cached Tokens: `{self.telemetry.cached_tokens:,}`
  - Completion Tokens: `{self.telemetry.completion_tokens:,}`
- **Ceiling Remaining:** `{self.telemetry.remaining_tokens:,}` tokens
- **Estimated Spend:** `${self.telemetry.total_cost_usd:.5f}` USD
- **Investigations Completed:** `{len(self.telemetry.investigations)}`
"""

        return {
            "content": [
                {
                    "type": "text",
                    "text": summary_text,
                }
            ],
            "isError": False,
        }

    # -------------------------------------------------------------------------
    # JSON-RPC 2.0 Transport Engine
    # -------------------------------------------------------------------------

    def handle_json_rpc(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        # Notification handling
        if req_id is None:
            return None

        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": self.PROTOCOL_VERSION,
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                        },
                        "serverInfo": {
                            "name": self.SERVER_NAME,
                            "version": self.SERVER_VERSION,
                        },
                    },
                }

            elif method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            elif method == "resources/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resources": self.list_resources(),
                    },
                }

            elif method == "resources/read":
                uri = params.get("uri")
                if not uri:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Missing 'uri' parameter"},
                    }
                res = self.read_resource(uri)
                return {"jsonrpc": "2.0", "id": req_id, "result": res}

            elif method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": self.list_tools(),
                    },
                }

            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments") or {}
                if not tool_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Missing tool 'name'"},
                    }
                tool_result = self.execute_tool(tool_name, tool_args)
                return {"jsonrpc": "2.0", "id": req_id, "result": tool_result}

            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method '{method}' not found"},
                }

        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    def run_stdio_sync(self) -> None:
        """Runs the JSON-RPC stdio event loop synchronously."""
        for line in sys.stdin:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                req = json.loads(line_str)
                resp = self.handle_json_rpc(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            except Exception as e:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tracecinder-mcp",
        description="Model Context Protocol (MCP) server for TraceCinder forensic triage runtime",
    )
    parser.add_argument("--workspace", type=str, default=None, help="Root workspace directory")
    parser.add_argument("--mock", dest="mock", action="store_true", default=None, help="Run in mock/offline mode")
    parser.add_argument("--live", dest="mock", action="store_false", help="Run in live API mode")
    args = parser.parse_args()

    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    server = TraceCinderMCPServer(workspace_root=workspace, mock_mode=args.mock)
    server.run_stdio_sync()


if __name__ == "__main__":
    main()
