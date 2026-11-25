# TraceCinder 🔥

> **Autonomous, Budget-Enforced CI/Incident Remediation Runtime**  
> Powered by Meta's `muse-ember-1.0` via prefix caching, `<200K` token anomaly chunking, dynamic minimal-to-max reasoning escalation, and automated `ast.parse` / `git apply` patch verification under a strict **$10 API cap**.

---

## ⚡ The Thesis
In modern distributed systems, production incidents leave breadcrumbs across disparate modalities:
1. **Raw Telemetry & Execution Logs** (Gigabytes of noisy stack traces, HTTP 500 bursts, trace IDs)
2. **Git Commit Diffs** (PRs merged minutes before the outage)
3. **Multimodal UI Artifacts** (User bug screenshots, spinning loaders, broken error modals, console screen clips)

Running frontier models (e.g. Claude 3.5 Sonnet or GPT-4o) across continuous telemetry streams costs \$50–\$200/day—making autonomous incident triage cost-prohibitive.

**TraceCinder is NOT a passive dashboard or basic image analyzer.** It is an **autonomous CI and incident remediation runtime** that ingests execution logs under 200K tokens, leverages prefix caching to slash prompt pricing from \$0.10/M down to \$0.025/M tokens, escalates anomalies from minimal to max reasoning, and outputs **syntactically verified, atomic git patches (`remediation.patch` & `artifacts/fix.patch`)** for fractions of a cent (**$0.00047/incident**).

---

## 📐 Architecture & Verification Pipeline

```
                                [Raw Telemetry Stream]
                                          │
                                          ▼
                         ┌──────────────────────────────────┐
                         │   TelemetryChunker (<200K Tokens)│
                         │   Extracts High-Density Windows  │
                         └──────────────────────────────────┘
                                          │
    ┌─────────────────────────────────────┴──────────────────────────────────────┐
    ▼                                     ▼                                      ▼
[Tier 1: Pulse Triage]          [Tier 2: Diff Correlator]             [Tier 3: Multimodal RCA]
Minimal Reasoning               Git PR Diff Mapping                   Full Cross-Modal Synthesis
Fingerprints stack trace        Pinpoints offending lines             Correlates UI screenshot + diff
Cost: ~$0.0001                  Cost: ~$0.00015                       Cost: ~$0.0002
    │                                     │                                      │
    └─────────────────────────────────────┼──────────────────────────────────────┘
                                          ▼
                         ┌──────────────────────────────────┐
                         │    Automated Verification Loop   │
                         │    • ast.parse (Python AST check)│
                         │    • git apply patch validation  │
                         │    • Outputs remediation.patch   │
                         └──────────────────────────────────┘
                                          │
                                          ▼
                         ┌──────────────────────────────────┐
                         │      Budget Controller Ledger    │
                         │   Strict $10 Hard Cap Enforcement│
                         │   Static Prefix Cache Optimizer  │
                         └──────────────────────────────────┘
```

### 1. Dynamic Minimal → Max Reasoning Escalation
* **Tier 1 (Pulse Triage):** Fast fingerprinting. Ingests windowed stack traces, extracts error codes, and classifies severity. Only escalates if unclassified or breaking regression.
* **Tier 2 (Diff Correlator):** Cross-references stack traces with recent git commit hunks to isolate the exact commit hash and line range.
* **Tier 3 (Multimodal RCA & Verification):** Fuses visual failure screenshots/clips with backend telemetry to verify user impact, generates an atomic code patch, runs `ast.parse` syntax verification, and outputs valid `remediation.patch` and `artifacts/fix.patch`.

### 2. Prefix Caching Maximization
Taxonomy rubrics, system prompts, and JSON schema targets are formatted into static prefix blocks, achieving **>35-50% cached token hit rates**, reducing input costs from \$0.10/M to \$0.025/M tokens.

### 3. Context-Bounded `<200K` Chunking
Parses arbitrary megabyte log files, identifies anomaly bursts (panics, HTTP 5xx, unhandled exceptions), and slices context-bounded windows strictly under 180,000 tokens.

---

## 🚀 Quickstart & Primary CLI Interface

TraceCinder requires zero external dependencies (Python 3.9+ standard library) for immediate verification.

### 1. Headless CI Remediation Triage (Primary Interface)
Run direct incident triage from raw telemetry files and git diffs:
```bash
python main.py triage \
  --logs incidents/sample_checkout_break/telemetry.log \
  --diff incidents/sample_checkout_break/recent.patch \
  --screen incidents/sample_checkout_break/failure_screen.svg
```

### 2. Verify Generated Patch Artifacts
Verify the patch with `git apply`:
```bash
git apply --stat remediation.patch
cat artifacts/fix.patch
```

### 3. Launch the Interactive Trace Viewer Companion
```bash
python main.py --ui
```
Open **`http://localhost:8080`** in your browser to inspect the live credit burn rate, select pre-packaged incidents, or paste (<kbd>Cmd+V</kbd> / <kbd>Ctrl+V</kbd>) custom screenshots.

### 4. Inspect the Real-Time Budget Ledger
```bash
python main.py budget
```

---

## 📊 Live Run Economics

| Phase | Input Tokens | Cached Tokens | Output Tokens | Cost (USD) |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1 (Pulse)** | 773 | 219 (28.3%) | 180 | $0.00013 |
| **Tier 2 (Diff)** | 590 | 219 (37.1%) | 260 | $0.00015 |
| **Tier 3 (Multimodal + Verification)**| 373 | 219 (58.7%) | 420 | $0.00019 |
| **Total Incident Remediation** | **1,736** | **657 (37.8%)** | **860** | **$0.00047** |

> At **\$0.00047 per incident remediation**, TraceCinder is **28x cheaper vs. a \$0.0132 naive max-effort baseline**, yielding over **21,000 verified remediation cycles** under the **\$10.00 Meta API Credit Cap**.
