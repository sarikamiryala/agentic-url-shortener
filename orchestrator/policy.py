"""Policy guardrails: security, compliance and change control.

The policy is data (policy.json) so it can be reviewed and versioned. Agents cannot
edit it: `policy.json` is itself a protected path.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import matches_any


@dataclass
class Violation:
    rule: str
    detail: str
    severity: str = "high"
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class PolicyViolation(Exception):
    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        super().__init__("; ".join(f"{v.rule}: {v.detail}" for v in violations))


DEFAULT_SECRET_PATTERNS = {
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "private_key_block": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "slack_token": r"xox[baprs]-[A-Za-z0-9-]{10,}",
    "hardcoded_credential": r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-/+=]{12,}['\"]",
}


@dataclass
class Policy:
    # Change control
    approval_required_tags: list[str] = field(
        default_factory=lambda: ["new_system", "public_api_change", "schema_change"]
    )
    protected_paths: list[str] = field(
        default_factory=lambda: [
            "Dockerfile",
            ".github/**",
            "requirements*.txt",
            "pyproject.toml",
            "policy.json",
            "render.yaml",
        ]
    )
    change_freeze: bool = False
    # Security
    secret_patterns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SECRET_PATTERNS))
    forbidden_calls: list[str] = field(
        default_factory=lambda: ["eval", "exec", "os.system", "pickle.loads", "pickle.load", "marshal.loads"]
    )
    # Compliance: analytics must not persist raw personal identifiers.
    pii_columns: list[str] = field(
        default_factory=lambda: ["ip", "ip_address", "client_ip", "email", "phone", "full_name"]
    )
    # Budgets (controlled autonomy)
    max_files_per_stage: int = 40
    max_bytes_per_stage: int = 400_000
    max_policy_violations: int = 3
    max_run_seconds: float = 900.0
    default_max_retries: int = 2
    default_max_rework: int = 2

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        p = Path(path)
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text())
        base = cls()
        for k, v in raw.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base

    def to_dict(self) -> dict:
        return asdict(self)

    def is_protected(self, rel_path: str) -> bool:
        return matches_any(rel_path, self.protected_paths)

    def scan_secrets(self, rel_path: str, text: str) -> list[Violation]:
        found: list[Violation] = []
        for name, pattern in self.secret_patterns.items():
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(
                    Violation("secret_detected", f"{name} pattern matched", "high", rel_path, line)
                )
                break  # one finding per pattern per file is enough
        return found

    def requires_approval(self, impact_tags: list[str] | set[str]) -> list[str]:
        return sorted(set(impact_tags) & set(self.approval_required_tags))
