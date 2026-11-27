from __future__ import annotations
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tracecinder.git_connector import LocalGitConnector, ReadOnlyViolationError
from tracecinder.ide_connector import DiagnosticItem, IDEConnector
from tracecinder.mcp_server import SESSION_TOKEN_CEILING, TraceCinderMCPServer


class TestMCPIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tracecinder_mcp_test_")
        self.workspace = Path(self.test_dir)

        # Initialize mock git repository
        os.system(f"git -C '{self.test_dir}' init -q")
        os.system(f"git -C '{self.test_dir}' config user.name 'TraceCinder Test'")
        os.system(f"git -C '{self.test_dir}' config user.email 'test@tracecinder.ai'")

        # Create sample files and commit
        sample_file = self.workspace / "service.py"
        sample_file.write_text("def handle_request(payload):\n    return payload['data'].strip()\n")
        os.system(f"git -C '{self.test_dir}' add service.py")
        os.system(f"git -C '{self.test_dir}' commit -q -m 'feat: initial request handler'")

        # Make uncommitted modification for diff testing
        sample_file.write_text("def handle_request(payload):\n    # Regression bug\n    return payload['missing'].strip()\n")

        self.server = TraceCinderMCPServer(workspace_root=self.workspace, mock_mode=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. MCP Protocol Handshake & Server Info
    # -------------------------------------------------------------------------

    def test_mcp_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cursor", "version": "1.0"},
            },
        }
        resp = self.server.handle_json_rpc(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        res = resp["result"]
        self.assertEqual(res["protocolVersion"], "2024-11-05")
        self.assertEqual(res["serverInfo"]["name"], "tracecinder-mcp")
        self.assertIn("tools", res["capabilities"])
        self.assertIn("resources", res["capabilities"])

    def test_mcp_ping(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
        resp = self.server.handle_json_rpc(req)
        self.assertEqual(resp["result"], {})

    # -------------------------------------------------------------------------
    # 2. Resource Handlers
    # -------------------------------------------------------------------------

    def test_resources_list(self):
        req = {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
        resp = self.server.handle_json_rpc(req)
        uris = [r["uri"] for r in resp["result"]["resources"]]
        self.assertIn("git://working-tree/diff", uris)
        self.assertIn("ide://active-context", uris)
        self.assertIn("tracecinder://telemetry/budget", uris)

    def test_read_git_working_tree_diff_resource(self):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "git://working-tree/diff"},
        }
        resp = self.server.handle_json_rpc(req)
        contents = resp["result"]["contents"]
        self.assertEqual(len(contents), 1)
        diff_text = contents[0]["text"]
        self.assertIn("payload['missing']", diff_text)
        self.assertIn("RECENT COMMIT LOG", diff_text)

    def test_read_ide_active_context_resource(self):
        # Update active context
        self.server.ide_connector.update_context(
            active_files=["service.py"],
            diagnostics=[
                {"file_path": "service.py", "line": 3, "message": "KeyError: 'missing'", "severity": "ERROR"}
            ],
            developer_intent="Fixing KeyError on missing payload attribute in checkout flow",
        )

        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": "ide://active-context"},
        }
        resp = self.server.handle_json_rpc(req)
        ctx_text = resp["result"]["contents"][0]["text"]
        self.assertIn("Developer Intent", ctx_text)
        self.assertIn("checkout flow", ctx_text)
        self.assertIn("KeyError", ctx_text)
        self.assertIn("service.py", ctx_text)

    def test_read_telemetry_budget_resource(self):
        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": "tracecinder://telemetry/budget"},
        }
        resp = self.server.handle_json_rpc(req)
        raw_json = resp["result"]["contents"][0]["text"]
        data = json.loads(raw_json)
        self.assertEqual(data["token_ceiling"], 200_000)
        self.assertIn("ceiling_remaining_tokens", data)
        self.assertIn("estimated_cost_usd", data)

    # -------------------------------------------------------------------------
    # 3. Tool Execution - analyze_incident
    # -------------------------------------------------------------------------

    def test_tool_analyze_incident_pipeline(self):
        stack_trace = """Traceback (most recent call last):
  File "/app/service.py", line 3, in handle_request
    return payload['missing'].strip()
KeyError: 'missing'"""

        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "analyze_incident",
                "arguments": {
                    "stack_trace": stack_trace,
                    "service": "checkout-service",
                    "developer_intent": "Checkout crashing when payload attribute is missing",
                    "voice_transcript": "User checkout triggered 500 when missing payload was submitted.",
                },
            },
        }
        resp = self.server.handle_json_rpc(req)
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("TraceCinder Forensic Analysis", text)
        self.assertIn("Diagnosed Root Cause", text)
        self.assertIn("Token Budget", text)

        # Check telemetry updated
        self.assertGreater(self.server.telemetry.total_tokens, 0)
        self.assertGreater(len(self.server.telemetry.investigations), 0)

    # -------------------------------------------------------------------------
    # 4. Tool Execution - propose_fix_diff (Human Review Draft)
    # -------------------------------------------------------------------------

    def test_tool_propose_fix_diff_verified_draft(self):
        # Run analysis first
        self.test_tool_analyze_incident_pipeline()

        req = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "propose_fix_diff",
                "arguments": {
                    "target_file": "service.py",
                    "error_context": "KeyError: 'missing'",
                },
            },
        }
        resp = self.server.handle_json_rpc(req)
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]

        self.assertIn("Proposed Remediation Patch (DRAFT)", text)
        self.assertIn("Unified Diff", text)
        self.assertIn("Not applied to repository", text)
        self.assertIn("Verification", text)

        # Confirm working tree was NOT modified by propose_fix_diff
        status = self.server.git_connector.get_status_summary()
        self.assertNotIn("committed", status)

    # -------------------------------------------------------------------------
    # 5. Tool Execution - get_forensic_telemetry
    # -------------------------------------------------------------------------

    def test_tool_get_forensic_telemetry(self):
        self.test_tool_analyze_incident_pipeline()

        req = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "get_forensic_telemetry",
                "arguments": {},
            },
        }
        resp = self.server.handle_json_rpc(req)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("Forensic Investigation Telemetry", text)
        self.assertIn("**Token Ceiling:** `200,000`", text)
        self.assertIn("Estimated Spend", text)

    # -------------------------------------------------------------------------
    # 6. Guardrails & Boundaries
    # -------------------------------------------------------------------------

    def test_read_only_git_guardrail(self):
        connector = LocalGitConnector(repo_root=self.workspace)
        with self.assertRaises(ReadOnlyViolationError):
            connector._run_git_cmd(["commit", "-m", "illegal mutation"])
        with self.assertRaises(ReadOnlyViolationError):
            connector._run_git_cmd(["push", "origin", "main"])
        with self.assertRaises(ReadOnlyViolationError):
            connector._run_git_cmd(["checkout", "-f", "main"])

    def test_context_pruning_on_large_diff(self):
        connector = LocalGitConnector(repo_root=self.workspace, max_diff_tokens=100)
        huge_diff = "diff --git a/big.py b/big.py\n" + ("+ line of data\n" * 500)
        pruned, is_pruned = connector.prune_diff(huge_diff)
        self.assertTrue(is_pruned)
        self.assertIn("DIFF PRUNED TO RESPECT 100 TOKEN CEILING", pruned)
        self.assertLessEqual(len(pruned), 400 + 100)

    def test_token_ceiling_budget_exhaustion(self):
        # Artificially consume the ceiling
        self.server.telemetry.prompt_tokens = 199_500
        req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "analyze_incident",
                "arguments": {"stack_trace": "Simulated error log"},
            },
        }
        resp = self.server.handle_json_rpc(req)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("BUDGET CEILING REACHED", resp["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
