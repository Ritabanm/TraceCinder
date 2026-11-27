---
name: tracecinder
description: Autonomous CI/incident forensic debugger powered by muse-ember-1.0 via MCP tools and resources.
---

# TraceCinder MCP Integration

Use this skill when you need to triage a runtime incident, correlate a stack trace or user bug report against recent git commits, check token telemetry, or generate a verified draft fix patch.

## Tools
- `tracecinder.analyze_incident`: Takes error logs/stack traces, audio transcripts, or UI screenshot paths, correlates against git diff and developer intent, and runs the bounded 3-tier forensic pipeline.
- `tracecinder.propose_fix_diff`: Generates a minimal, syntax-verified unified diff (`git diff` format) targeting the root cause.
- `tracecinder.get_forensic_telemetry`: Emits exact input/output tokens, cache hits, and USD spend against the 200,000-token ceiling.

## Resources
- `git://working-tree/diff`
- `ide://active-context`
- `tracecinder://telemetry/budget`
