from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from tracecinder.budget import BudgetLedger
from tracecinder.escalator import ReasoningEscalator
from tracecinder.models import EscalationTier, IncidentBundle
from tracecinder.provider import MuseEmberProvider
from tracecinder.ui import render_banner, render_budget_meter, render_final_report, render_step_progress

DEFAULT_LEDGER_PATH = Path(".tracecinder/budget_ledger.json")


def load_incident_from_dir(incident_dir: Path) -> IncidentBundle:
    meta_path = incident_dir / "incident.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Missing incident.json in {incident_dir}")

    meta = json.loads(meta_path.read_text())
    log_path = str(incident_dir / meta.get("log_file", "telemetry.log"))
    diff_path = str(incident_dir / meta.get("diff_file", "recent.patch")) if meta.get("diff_file") else None
    screenshot_path = str(incident_dir / meta.get("screenshot_file", "failure_screen.png")) if meta.get("screenshot_file") else None
    audio_path = str(incident_dir / meta.get("audio_file", "incident_note.wav")) if meta.get("audio_file") else None
    if audio_path and not Path(audio_path).exists():
        audio_path = None

    return IncidentBundle(
        incident_id=meta.get("incident_id", incident_dir.name),
        service=meta.get("service", "unknown-service"),
        summary=meta.get("summary", "Unclassified production incident"),
        timestamp=meta.get("timestamp", "2026-11-24T20:00:00Z"),
        log_path=log_path,
        diff_path=diff_path,
        screenshot_path=screenshot_path,
        audio_path=audio_path,
        metadata=meta,
    )


def cmd_triage(args: argparse.Namespace) -> None:
    render_banner()

    ledger_path = Path(args.ledger) if args.ledger else DEFAULT_LEDGER_PATH
    ledger = BudgetLedger.load(ledger_path)
    if args.cap:
        ledger.cap_usd = args.cap

    render_budget_meter(ledger)

    # Check if direct flags --logs / --diff / --screen were used
    if getattr(args, "logs", None):
        log_path = Path(args.logs)
        if not log_path.exists():
            print(f"Error: Log file {log_path} not found.")
            sys.exit(1)
        diff_path = str(Path(args.diff)) if getattr(args, "diff", None) and Path(args.diff).exists() else None
        screen_path = str(Path(args.screen)) if getattr(args, "screen", None) and Path(args.screen).exists() else None
        audio_path = str(Path(args.audio)) if getattr(args, "audio", None) and Path(args.audio).exists() else None
        
        bundle = IncidentBundle(
            incident_id=f"CLI-{log_path.stem}",
            service=getattr(args, "service", "core-service") or "core-service",
            summary=f"Automated Triage of {log_path.name}",
            timestamp="2026-11-24T22:00:00Z",
            log_path=str(log_path),
            diff_path=diff_path,
            screenshot_path=screen_path,
            audio_path=audio_path,
        )
    else:
        incident_path = Path(args.incident_path) if getattr(args, "incident_path", None) else Path("incidents/sample_checkout_break")
        if not incident_path.exists():
            print(f"Error: Incident path {incident_path} not found.")
            sys.exit(1)
        bundle = load_incident_from_dir(incident_path)
        if getattr(args, "audio", None) and Path(args.audio).exists():
            bundle.audio_path = str(Path(args.audio))

    print(f"\n⚡ Ingesting Incident Bundle: {bundle.incident_id} [{bundle.service}]")
    print(f"   Log stream: {bundle.log_path}")
    if bundle.diff_path:
        print(f"   Git diff:   {bundle.diff_path}")
    if bundle.screenshot_path:
        print(f"   Visual UI:  {bundle.screenshot_path}")
    if bundle.audio_path:
        print(f"   Voice Note: {bundle.audio_path} (audio/ember-native)")

    provider = MuseEmberProvider(mock_mode=args.mock)
    mode_label = "Local Offline Simulator (--mock)" if provider.mock_mode else "Live Meta Model API (muse-ember-1.0)"
    print(f"   Execution Mode: {mode_label}")

    escalator = ReasoningEscalator(provider=provider, budget_ledger=ledger)

    report = escalator.run_triage(bundle)

    for idx, step in enumerate(report.steps):
        render_step_progress(step, idx)

    # Save full report JSON
    reports_dir = (ledger_path.parent / "reports") if ledger_path else Path(".tracecinder/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"{report.incident_id}_rca.json"
    report_data = {
        "incident_id": report.incident_id,
        "final_tier": report.final_tier.value,
        "severity": report.severity.value,
        "summary": report.summary,
        "root_cause": report.root_cause,
        "correlated_diff": report.correlated_diff,
        "visual_finding": report.visual_finding,
        "spoken_context": report.spoken_context,
        "proposed_remediation": report.proposed_remediation,
        "confidence": report.confidence,
        "patch_file_path": report.patch_file_path,
        "verification_status": report.verification_status,
        "verification_details": report.verification_details,
        "total_usage": {
            "prompt_tokens": report.total_usage.prompt_tokens,
            "cached_prompt_tokens": report.total_usage.cached_prompt_tokens,
            "completion_tokens": report.total_usage.completion_tokens,
            "cost_usd": report.total_usage.cost_usd,
        },
        "budget_remaining_usd": report.budget_remaining_usd,
    }
    report_file.write_text(json.dumps(report_data, indent=2))
    ledger.incidents_triaged += 1
    ledger.save()

    render_final_report(report)
    print(f"📁 Detailed RCA Report saved to: {report_file}")
    if report.patch_file_path:
        print(f"💾 Verified Git Patch saved to:  {report.patch_file_path} & artifacts/fix.patch")
    if ledger.audit_log_path:
        print(f"🧾 Usage Audit Log appended to:  {ledger.audit_log_path}\n")

    render_budget_meter(ledger)


def cmd_budget(args: argparse.Namespace) -> None:
    render_banner()
    ledger_path = Path(args.ledger) if args.ledger else DEFAULT_LEDGER_PATH
    ledger = BudgetLedger.load(ledger_path)
    render_budget_meter(ledger)
    print(f"Total Prompt Tokens Ingested:    {ledger.total_prompt_tokens:,}")
    print(f"Prefix-Cached Tokens Saved:      {ledger.total_cached_tokens:,}")
    print(f"Completion Tokens Emitted:       {ledger.total_completion_tokens:,}")
    print(f"Net Incident Spend (USD):        ${ledger.spent_usd:.5f}")
    print(f"Remaining Credit Balance (USD):  ${ledger.remaining_usd:.5f}\n")


def cmd_replay(args: argparse.Namespace) -> None:
    # Runs the pre-canned checkout incident
    sample_dir = Path(__file__).parent.parent / "incidents" / "sample_checkout_break"
    args.incident_path = str(sample_dir)
    args.mock = True
    cmd_triage(args)


def cmd_ui(args: argparse.Namespace) -> None:
    from tracecinder.web import start_server

    render_banner()
    port = getattr(args, "port", 8080) or 8080
    ledger_path = Path(args.ledger) if args.ledger else DEFAULT_LEDGER_PATH
    start_server(port=port, ledger_path=ledger_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cinder",
        description="TraceCinder: Budget-aware multimodal telemetry triage runtime powered by muse-ember-1.0",
    )
    parser.add_argument("--ledger", type=str, help="Path to custom budget ledger JSON")
    parser.add_argument("--cap", type=float, help="Override budget cap in USD (default: $10.00)")
    parser.add_argument("--mock", dest="mock", action="store_true", default=None, help="Run in local offline mock mode")
    parser.add_argument("--live", dest="mock", action="store_false", help="Run against live Meta Model API (muse-ember-1.0)")
    parser.add_argument("--ui", action="store_true", help="Launch interactive web trace viewer")

    subparsers = parser.add_subparsers(dest="command")

    triage_parser = subparsers.add_parser("triage", help="Triage an incident bundle or raw logs/diffs")
    triage_parser.add_argument("incident_path", nargs="?", default=None, help="Path to incident directory")
    triage_parser.add_argument("--logs", type=str, help="Path to raw telemetry log file")
    triage_parser.add_argument("--diff", type=str, help="Path to recent git commit diff / patch")
    triage_parser.add_argument("--screen", type=str, help="Path to UI failure screenshot / screen recording frame")
    triage_parser.add_argument("--audio", type=str, help="Path to engineer voice note recording (.wav / .mp3 / .webm)")
    triage_parser.add_argument("--service", type=str, default="core-service", help="Target service name")
    triage_parser.add_argument("--verify", action="store_true", default=True, help="Run ast.parse and git apply check")
    triage_parser.add_argument("--mock", dest="mock", action="store_true", default=None, help="Run in local offline mock mode")
    triage_parser.add_argument("--live", dest="mock", action="store_false", help="Run against live Meta Model API (muse-ember-1.0)")

    budget_parser = subparsers.add_parser("budget", help="Inspect current budget status and token ledger")

    replay_parser = subparsers.add_parser("replay", help="Replay a pre-canned multimodal incident showcase")

    ui_parser = subparsers.add_parser("ui", help="Launch interactive web dashboard")
    ui_parser.add_argument("--port", type=int, default=8080, help="Port to bind dashboard (default: 8080)")

    mcp_parser = subparsers.add_parser("mcp", help="Start TraceCinder MCP (Model Context Protocol) server over stdio")
    mcp_parser.add_argument("--workspace", type=str, default=None, help="Root workspace directory")
    mcp_parser.add_argument("--mock", dest="mock", action="store_true", default=None, help="Run in mock/offline mode")
    mcp_parser.add_argument("--live", dest="mock", action="store_false", help="Run in live API mode")

    args = parser.parse_args()
    if getattr(args, "ui", False) or args.command == "ui":
        cmd_ui(args)
    elif args.command == "triage":
        cmd_triage(args)
    elif args.command == "budget":
        cmd_budget(args)
    elif args.command == "mcp":
        from tracecinder.mcp_server import main as mcp_main
        mcp_main()
    elif args.command == "replay" or not args.command:
        cmd_replay(args)


if __name__ == "__main__":
    main()
