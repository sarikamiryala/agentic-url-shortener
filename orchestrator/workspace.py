"""Transactional workspace for generated code.

* Stages write through a `ScopedWorkspace`, which enforces the stage's write scope,
  protected paths, secret scanning and size budgets *before* a byte hits disk.
* `snapshot` / `restore` are scope-aware, so rolling back one stage never touches
  files owned by a stage running in parallel.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Callable

from .policy import Policy, PolicyViolation, Violation
from .util import matches_any, sha256_text

IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", ".venv"}
IGNORED_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm")

EmitFn = Callable[[str, str, str | None, dict], None]  # (type, actor, node_id, payload)


class Workspace:
    def __init__(self, root: Path, checkpoints_dir: Path, policy: Policy) -> None:
        self.root = Path(root).resolve()
        self.checkpoints_dir = Path(checkpoints_dir).resolve()
        self.policy = policy
        self.root.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---- paths -----------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or "\\" in rel or re.match(r"^[A-Za-z]:", rel):
            raise PolicyViolation([Violation("path_traversal", f"invalid path {rel!r}", path=rel)])
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise PolicyViolation([Violation("path_traversal", f"{rel!r} escapes the workspace", path=rel)])
        return target

    @staticmethod
    def _ignored(path: Path, root: Path) -> bool:
        rel = path.relative_to(root)
        return any(p in IGNORED_PARTS for p in rel.parts) or path.name.endswith(IGNORED_SUFFIXES)

    def files(self, patterns: list[str] | None = None) -> list[str]:
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not self._ignored(p, self.root):
                rel = p.relative_to(self.root).as_posix()
                if patterns is None or matches_any(rel, patterns):
                    out.append(rel)
        return out

    def read_text(self, rel: str) -> str:
        return self.resolve(rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return self.resolve(rel).is_file()

    def seed(self, files: dict[str, str]) -> None:
        """Place the pre-existing codebase into the workspace (brownfield baseline)."""
        for rel, content in files.items():
            target = self.resolve(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    # ---- checkpoints -----------------------------------------------------------
    def snapshot(self, scopes: list[str], label: str) -> str:
        with self._lock:
            seq = len([d for d in self.checkpoints_dir.iterdir() if d.is_dir()]) + 1
            safe = re.sub(r"[^A-Za-z0-9_-]", "-", label)
            cp_id = f"cp-{seq:03d}-{safe}"
            cp_dir = self.checkpoints_dir / cp_id
            (cp_dir / "files").mkdir(parents=True)
            files = self.files(scopes)
            for rel in files:
                dest = cp_dir / "files" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / rel, dest)
            (cp_dir / "manifest.json").write_text(json.dumps({"scopes": scopes, "files": files}))
            return cp_id

    def checkpoint_files_dir(self, cp_id: str) -> Path:
        return self.checkpoints_dir / cp_id / "files"

    def _manifest(self, cp_id: str) -> dict:
        return json.loads((self.checkpoints_dir / cp_id / "manifest.json").read_text())

    def restore(self, cp_id: str) -> dict:
        """Return the checkpoint's scope to exactly the snapshotted state."""
        with self._lock:
            m = self._manifest(cp_id)
            keep = set(m["files"])
            deleted, restored = [], []
            for rel in self.files(m["scopes"]):
                if rel not in keep:
                    (self.root / rel).unlink()
                    deleted.append(rel)
            for rel in m["files"]:
                src = self.checkpoints_dir / cp_id / "files" / rel
                dst = self.root / rel
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    restored.append(rel)
            return {"deleted": deleted, "restored": restored}

    def diff_since(self, cp_id: str) -> str:
        """Unified diff between a checkpoint and the current workspace (same scope)."""
        m = self._manifest(cp_id)
        before = set(m["files"])
        after = set(self.files(m["scopes"]))
        chunks: list[str] = []
        for rel in sorted(before | after):
            old = (self.checkpoints_dir / cp_id / "files" / rel).read_text() if rel in before else ""
            new = (self.root / rel).read_text() if rel in after else ""
            if old == new:
                continue
            chunks.extend(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{rel}" if rel in before else "/dev/null",
                    tofile=f"b/{rel}" if rel in after else "/dev/null",
                )
            )
            if chunks and not chunks[-1].endswith("\n"):
                chunks[-1] += "\n"
        return "".join(chunks)

    def scoped(self, stage: str, scopes: list[str], actor: str, emit: EmitFn | None = None) -> "ScopedWorkspace":
        return ScopedWorkspace(self, stage, scopes, actor, emit)


class ScopedWorkspace:
    """What an agent sees: free reads, policy-checked writes inside its scope."""

    def __init__(self, ws: Workspace, stage: str, scopes: list[str], actor: str, emit: EmitFn | None) -> None:
        self._ws = ws
        self.stage = stage
        self.scopes = list(scopes)
        self.actor = actor
        self._emit = emit or (lambda *_: None)
        self.written: dict[str, str] = {}  # path -> sha256
        self._bytes = 0

    @property
    def root(self) -> Path:
        return self._ws.root

    def read_text(self, rel: str) -> str:
        return self._ws.read_text(rel)

    def exists(self, rel: str) -> bool:
        return self._ws.exists(rel)

    def files(self, patterns: list[str] | None = None) -> list[str]:
        return self._ws.files(patterns)

    def _check(self, rel: str, content: str | None) -> None:
        violations: list[Violation] = []
        policy = self._ws.policy
        try:
            self._ws.resolve(rel)
        except PolicyViolation as exc:
            violations.extend(exc.violations)
        if not violations:
            if not matches_any(rel, self.scopes):
                violations.append(
                    Violation("write_scope", f"stage '{self.stage}' may only write {self.scopes}", path=rel)
                )
            if policy.is_protected(rel):
                violations.append(
                    Violation("change_control", "protected path needs a human-approved change", path=rel)
                )
            if content is not None:
                violations.extend(policy.scan_secrets(rel, content))
                if rel not in self.written and len(self.written) + 1 > policy.max_files_per_stage:
                    violations.append(Violation("size_budget", f"more than {policy.max_files_per_stage} files"))
                if self._bytes + len(content.encode()) > policy.max_bytes_per_stage:
                    violations.append(Violation("size_budget", f"more than {policy.max_bytes_per_stage} bytes"))
        if violations:
            self._emit(
                "policy_violation",
                self.actor,
                self.stage,
                {"path": rel, "violations": [v.to_dict() for v in violations]},
            )
            raise PolicyViolation(violations)

    def write(self, rel: str, content: str) -> None:
        self._check(rel, content)
        target = self._ws.resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if rel not in self.written:
            self._bytes += len(content.encode())
        self.written[rel] = sha256_text(content)
        self._emit("file_written", self.actor, self.stage, {"path": rel, "sha256": self.written[rel][:12]})

    def delete(self, rel: str) -> None:
        self._check(rel, None)
        target = self._ws.resolve(rel)
        if target.is_file():
            target.unlink()
            self.written.pop(rel, None)
            self._emit("file_deleted", self.actor, self.stage, {"path": rel})
