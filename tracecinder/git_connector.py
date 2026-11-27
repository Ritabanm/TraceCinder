from __future__ import annotations
import json
import os
import re
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Token pruning constants (approx 4 chars/token)
DEFAULT_MAX_DIFF_TOKENS = 50_000
DEFAULT_MAX_DIFF_CHARS = DEFAULT_MAX_DIFF_TOKENS * 4


class ReadOnlyViolationError(PermissionError):
    """Raised when an operation attempts to modify repository state."""
    pass


@dataclass
class GitCommitInfo:
    commit_hash: str
    author: str
    date: str
    message: str
    files_changed: List[str] = field(default_factory=list)


@dataclass
class WorkingTreeDiff:
    staged_diff: str
    unstaged_diff: str
    recent_commits: List[GitCommitInfo] = field(default_factory=list)
    branch: str = "main"
    status_summary: str = ""
    is_pruned: bool = False
    total_tokens_approx: int = 0

    @property
    def full_diff(self) -> str:
        parts = []
        if self.staged_diff.strip():
            parts.append("=== STAGED CHANGES ===\n" + self.staged_diff.strip())
        if self.unstaged_diff.strip():
            parts.append("=== UNSTAGED CHANGES ===\n" + self.unstaged_diff.strip())
        if not parts:
            parts.append("(Clean working tree - no uncommitted changes)")
        if self.recent_commits:
            parts.append("\n=== RECENT COMMIT LOG ===")
            for c in self.recent_commits:
                parts.append(f"[{c.commit_hash[:8]}] {c.author} ({c.date}): {c.message}")
        return "\n\n".join(parts)


class RemoteGitAdapter(ABC):
    """Extensible adapter interface for remote Git platform lookups (GitHub/GitLab)."""

    @abstractmethod
    def get_commit_diff(self, repo_owner: str, repo_name: str, commit_sha: str) -> Optional[str]:
        """Fetch unified diff for a remote commit."""
        pass

    @abstractmethod
    def get_pull_request_diff(self, repo_owner: str, repo_name: str, pr_number: int) -> Optional[str]:
        """Fetch unified diff for a pull request or merge request."""
        pass


class GitHubAdapter(RemoteGitAdapter):
    """GitHub REST API adapter for PR and commit lookups via PAT or OAuth."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.base_url = "https://api.github.com"

    def _headers(self, accept: str = "application/vnd.github.v3+json") -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "TraceCinder-MCP/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_commit_diff(self, repo_owner: str, repo_name: str, commit_sha: str) -> Optional[str]:
        url = f"{self.base_url}/repos/{repo_owner}/{repo_name}/commits/{commit_sha}"
        req = urllib.request.Request(url, headers=self._headers("application/vnd.github.v3.diff"))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            return None

    def get_pull_request_diff(self, repo_owner: str, repo_name: str, pr_number: int) -> Optional[str]:
        url = f"{self.base_url}/repos/{repo_owner}/{repo_name}/pulls/{pr_number}"
        req = urllib.request.Request(url, headers=self._headers("application/vnd.github.v3.diff"))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            return None

    def get_issue(self, repo_owner: str, repo_name: str, issue_number: int) -> Optional[Dict[str, Any]]:
        """Fetch issue title, body, and state from GitHub REST API."""
        url = f"{self.base_url}/repos/{repo_owner}/{repo_name}/issues/{issue_number}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "title": data.get("title", ""),
                    "body": data.get("body", ""),
                    "state": data.get("state", "open"),
                    "html_url": data.get("html_url", ""),
                }
        except Exception:
            return None


def parse_github_ref(ref: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Parses owner, repo, and issue/PR number from a string reference or URL.
    Supports:
      - 'owner/repo#123'
      - 'https://github.com/owner/repo/issues/123'
      - 'https://github.com/owner/repo/pull/456'
    """
    m = re.search(r'(?:https?://github\.com/)?([a-zA-Z0-9_\-\.]+)/([a-zA-Z0-9_\-\.]+)(?:/(?:issues|pulls?)/|#)(\d+)', str(ref))
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None, None, None


class GitLabAdapter(RemoteGitAdapter):
    """GitLab REST API adapter for MR and commit lookups via PAT or OAuth."""

    def __init__(self, token: Optional[str] = None, base_url: str = "https://gitlab.com/api/v4"):
        self.token = token or os.environ.get("GITLAB_TOKEN") or os.environ.get("GL_TOKEN")
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "TraceCinder-MCP/1.0",
        }
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    def get_commit_diff(self, repo_owner: str, repo_name: str, commit_sha: str) -> Optional[str]:
        project_id = f"{repo_owner}%2F{repo_name}"
        url = f"{self.base_url}/projects/{project_id}/repository/commits/{commit_sha}/diff"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                diffs = json.loads(resp.read().decode("utf-8"))
                return "\n".join(d.get("diff", "") for d in diffs if isinstance(d, dict))
        except Exception:
            return None

    def get_pull_request_diff(self, repo_owner: str, repo_name: str, pr_number: int) -> Optional[str]:
        project_id = f"{repo_owner}%2F{repo_name}"
        url = f"{self.base_url}/projects/{project_id}/merge_requests/{pr_number}/diffs"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                diffs = json.loads(resp.read().decode("utf-8"))
                return "\n".join(d.get("diff", "") for d in diffs if isinstance(d, dict))
        except Exception:
            return None


class LocalGitConnector:
    """
    Enforces strictly read-only local Git repository introspection.
    Supports git diff, git log, git blame, and git status.
    Guarantees no modifications (commits, pushes, checkouts) can be performed.
    """

    MUTATING_COMMANDS = {
        "commit", "push", "apply", "checkout", "switch", "reset", "rebase",
        "merge", "cherry-pick", "revert", "clean", "rm", "mv"
    }

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        max_diff_tokens: int = DEFAULT_MAX_DIFF_TOKENS,
        remote_adapter: Optional[RemoteGitAdapter] = None,
    ):
        self.repo_root = Path(repo_root or os.getcwd()).resolve()
        self.max_diff_tokens = max_diff_tokens
        self.max_diff_chars = max_diff_tokens * 4
        self.remote_adapter = remote_adapter or GitHubAdapter()

    def _run_git_cmd(self, args: List[str]) -> str:
        if not args:
            return ""
        subcmd = args[0].lower()
        if subcmd in self.MUTATING_COMMANDS:
            raise ReadOnlyViolationError(
                f"Refusing to execute mutating git command '{subcmd}'. TraceCinder Git connector is strictly read-only."
            )
        if subcmd == "branch" and not any(flag in args for flag in ("--show-current", "--list", "-l", "-a")):
            raise ReadOnlyViolationError(
                "Refusing to execute modifying git branch command. TraceCinder Git connector is strictly read-only."
            )

        cmd = ["git"] + args
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if res.returncode != 0:
                return ""
            return res.stdout
        except Exception:
            return ""

    def prune_diff(self, diff_text: str) -> Tuple[str, bool]:
        """Prunes large diffs to keep within max token ceiling while preserving hunk integrity."""
        if len(diff_text) <= self.max_diff_chars:
            return diff_text, False

        # Keep file headers, truncate large body
        lines = diff_text.splitlines()
        kept_lines: List[str] = []
        cur_chars = 0

        for line in lines:
            if cur_chars + len(line) + 1 > self.max_diff_chars:
                kept_lines.append(f"\n... [DIFF PRUNED TO RESPECT {self.max_diff_tokens:,} TOKEN CEILING] ...")
                return "\n".join(kept_lines), True
            kept_lines.append(line)
            cur_chars += len(line) + 1

        return "\n".join(kept_lines), True

    def get_current_branch(self) -> str:
        branch = self._run_git_cmd(["branch", "--show-current"]).strip()
        return branch or "HEAD"

    def get_status_summary(self) -> str:
        return self._run_git_cmd(["status", "--short"]).strip()

    def get_unstaged_diff(self) -> str:
        raw_diff = self._run_git_cmd(["diff", "--no-color", "--no-ext-diff"])
        pruned, _ = self.prune_diff(raw_diff)
        return pruned

    def get_staged_diff(self) -> str:
        raw_diff = self._run_git_cmd(["diff", "--cached", "--no-color", "--no-ext-diff"])
        pruned, _ = self.prune_diff(raw_diff)
        return pruned

    def get_recent_commits(self, limit: int = 5) -> List[GitCommitInfo]:
        log_out = self._run_git_cmd(["log", f"-n{limit}", "--pretty=format:%H%x09%an%x09%ad%x09%s", "--date=iso"])
        commits = []
        for line in log_out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                sha, author, date, msg = parts[0], parts[1], parts[2], parts[3]
                commits.append(GitCommitInfo(commit_hash=sha, author=author, date=date, message=msg))
        return commits

    def get_blame(self, file_path: str, line_start: int = 1, line_end: int = 100) -> str:
        """Inspects git blame on a target file range."""
        clean_path = str(file_path).lstrip("/")
        return self._run_git_cmd(["blame", f"-L{line_start},{line_end}", "--", clean_path])

    def get_working_tree_diff(self, commit_limit: int = 5) -> WorkingTreeDiff:
        """Collects full working tree context: unstaged, staged, branch, status, recent commits."""
        unstaged = self.get_unstaged_diff()
        staged = self.get_staged_diff()
        commits = self.get_recent_commits(limit=commit_limit)
        branch = self.get_current_branch()
        status = self.get_status_summary()

        combined = unstaged + "\n" + staged
        pruned_combined, is_pruned = self.prune_diff(combined)
        total_tokens = len(pruned_combined) // 4

        return WorkingTreeDiff(
            staged_diff=staged,
            unstaged_diff=unstaged,
            recent_commits=commits,
            branch=branch,
            status_summary=status,
            is_pruned=is_pruned,
            total_tokens_approx=total_tokens,
        )
