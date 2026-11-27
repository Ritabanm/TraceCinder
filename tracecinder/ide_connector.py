from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DiagnosticItem:
    file_path: str
    line: int
    column: int = 1
    severity: str = "ERROR"  # ERROR, WARNING, INFO
    message: str = ""
    source: str = "linter"
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "code": self.code,
        }


@dataclass
class IDEActiveContext:
    active_files: List[str] = field(default_factory=list)
    diagnostics: List[DiagnosticItem] = field(default_factory=list)
    developer_intent: str = ""
    workspace_root: str = ""
    cursor_position: Optional[Dict[str, Any]] = None
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_files": self.active_files,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "developer_intent": self.developer_intent,
            "workspace_root": self.workspace_root,
            "cursor_position": self.cursor_position,
            "last_updated": self.last_updated,
        }

    def to_text_summary(self) -> str:
        parts = [
            f"# IDE Active Context ({self.last_updated})",
            f"**Workspace Root:** `{self.workspace_root or os.getcwd()}`",
        ]

        if self.developer_intent:
            parts.append(f"\n### 🎯 Developer Intent\n{self.developer_intent}")
        else:
            parts.append("\n### 🎯 Developer Intent\n*(No specific task intent specified)*")

        parts.append("\n### 📂 Active / Open Files")
        if self.active_files:
            for f in self.active_files:
                parts.append(f"- `{f}`")
        else:
            parts.append("- *(No active files tracked)*")

        if self.cursor_position:
            parts.append(
                f"\n**Cursor:** `{self.cursor_position.get('file', 'unknown')}` at L{self.cursor_position.get('line', 1)}:C{self.cursor_position.get('column', 1)}"
            )

        parts.append("\n### ⚠️ Active Diagnostics / Linter Errors")
        if self.diagnostics:
            for d in self.diagnostics:
                code_str = f" [{d.code}]" if d.code else ""
                parts.append(f"- **[{d.severity}]** `{d.file_path}:{d.line}` - {d.message}{code_str} ({d.source})")
        else:
            parts.append("- *(Zero active diagnostics or linter warnings)*")

        return "\n".join(parts)


class IDEConnector:
    """
    Manages active IDE context (open files, linter errors, active intent).
    Exposes and syncs with `.tracecinder/active_context.json` so IDE extensions
    (Cursor, VS Code, Antigravity) can seamlessly communicate state.
    """

    def __init__(self, workspace_root: Optional[Path] = None):
        self.workspace_root = Path(workspace_root or os.getcwd()).resolve()
        self.context_file = self.workspace_root / ".tracecinder" / "active_context.json"
        self._cached_context: Optional[IDEActiveContext] = None

    def get_context(self) -> IDEActiveContext:
        """Loads current active context from storage or returns default."""
        if self.context_file.exists():
            try:
                data = json.loads(self.context_file.read_text(encoding="utf-8"))
                diags = [
                    DiagnosticItem(
                        file_path=d.get("file_path", ""),
                        line=d.get("line", 1),
                        column=d.get("column", 1),
                        severity=d.get("severity", "ERROR"),
                        message=d.get("message", ""),
                        source=d.get("source", "linter"),
                        code=d.get("code"),
                    )
                    for d in data.get("diagnostics", [])
                ]
                self._cached_context = IDEActiveContext(
                    active_files=data.get("active_files", []),
                    diagnostics=diags,
                    developer_intent=data.get("developer_intent", ""),
                    workspace_root=data.get("workspace_root", str(self.workspace_root)),
                    cursor_position=data.get("cursor_position"),
                    last_updated=data.get("last_updated", datetime.now(timezone.utc).isoformat()),
                )
                return self._cached_context
            except Exception:
                pass

        if not self._cached_context:
            self._cached_context = IDEActiveContext(
                workspace_root=str(self.workspace_root),
                developer_intent="Debugging recent test/runtime anomaly in current workspace.",
            )
        return self._cached_context

    def update_context(
        self,
        active_files: Optional[List[str]] = None,
        diagnostics: Optional[List[Dict[str, Any]]] = None,
        developer_intent: Optional[str] = None,
        cursor_position: Optional[Dict[str, Any]] = None,
    ) -> IDEActiveContext:
        """Updates active IDE context and persists to .tracecinder/active_context.json."""
        ctx = self.get_context()
        if active_files is not None:
            ctx.active_files = active_files
        if diagnostics is not None:
            ctx.diagnostics = [
                DiagnosticItem(
                    file_path=d.get("file_path", ""),
                    line=d.get("line", 1),
                    column=d.get("column", 1),
                    severity=d.get("severity", "ERROR"),
                    message=d.get("message", ""),
                    source=d.get("source", "linter"),
                    code=d.get("code"),
                )
                for d in diagnostics
            ]
        if developer_intent is not None:
            ctx.developer_intent = developer_intent
        if cursor_position is not None:
            ctx.cursor_position = cursor_position

        ctx.last_updated = datetime.now(timezone.utc).isoformat()

        # Persist
        try:
            self.context_file.parent.mkdir(parents=True, exist_ok=True)
            self.context_file.write_text(json.dumps(ctx.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass

        self._cached_context = ctx
        return ctx
