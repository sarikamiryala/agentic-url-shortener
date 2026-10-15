"""Builder agents: implementation, test authoring, documentation."""
from __future__ import annotations

import re

from ..catalog import authored_tests, impl_steps
from .base import AgentContext, AgentOutput, decision

_COVERS_RE = re.compile(r"^#\s*covers:\s*(.+)$", re.M)
_TEST_RE = re.compile(r"^\s*def test_", re.M)


class ImplementationAgent:
    """Materialises reference code for the design's tasks inside `shortener/**`.

    Strategies: `reference` (greenfield, safer links), and for the alias/expiry change
    `direct` (shortest patch) or `migration_aware`. The primary agent uses `direct` when
    the `seeded_defect` fault is on, so the orchestrator's rework path can be exercised
    with a defect that is only visible against a pre-existing database.
    """

    def __init__(self, variant: str = "primary") -> None:
        self.variant = variant
        self.name = f"implementation-{variant}"

    def run(self, ctx: AgentContext) -> AgentOutput:
        design = ctx.inputs["design"]
        pack = design["pack"]
        safety = design.get("safety") or {}
        ctx.fail_transiently_if_configured()

        seeded = bool(ctx.fault("seeded_defect")) and self.variant == "primary"
        if pack == "aliases_expiry":
            strategy = "direct" if seeded else "migration_aware"
        else:
            strategy = "reference"

        decisions = [
            decision(
                "strategy", f"Implementation strategy for {pack}", strategy,
                "Primary agent takes the shortest patch." if strategy == "direct"
                else "Applies the design's risk mitigations (in-place migration) up front.",
                ["direct", "migration_aware"] if pack == "aliases_expiry" else [],
            )
        ]
        if ctx.feedback:
            f = ctx.feedback.get("failure", {})
            decisions.append(
                decision(
                    "rework", "Rework after a downstream failure", f"switch to {strategy}",
                    f"Stage '{ctx.feedback.get('from')}' failed gate '{f.get('gate') or f.get('kind')}': "
                    f"{str(f.get('detail', ''))[:200]}",
                    [],
                )
            )

        tasks_done = []
        for step in impl_steps(pack, strategy, safety, ctx.repo_root):
            for rel, content in step["files"].items():
                ctx.workspace.write(rel, content)
            tasks_done.append({"task": step["task"], "files": sorted(step["files"])})

        leak = ctx.fault("secret_leak")
        if leak and self.variant in leak.get("agents", ["primary", "fallback"]):
            # Fault injection: an agent that hardcodes a credential. The workspace must refuse it.
            ctx.workspace.write("shortener/_debug.py", 'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n')

        written = ctx.workspace.written
        files = [
            {"path": p, "sha256": h[:16], "bytes": len(ctx.workspace.read_text(p).encode())}
            for p, h in sorted(written.items())
        ]
        data = {"pack": pack, "strategy": strategy, "tasks_done": tasks_done, "files": files}
        return AgentOutput(data=data, summary=f"{len(files)} files via '{strategy}' strategy", decisions=decisions)


class AuthorTestsAgent:
    """Writes tests from the contract and requirements only. It never reads the implementation."""

    name = "test-author"

    def run(self, ctx: AgentContext) -> AgentOutput:
        design = ctx.inputs["design"]
        ctx.fail_transiently_if_configured()
        files_out = []
        covers: set[str] = set()
        total = 0
        for rel, content in authored_tests(design["pack"], design.get("safety") or {}, ctx.repo_root).items():
            ctx.workspace.write(rel, content)
            n = len(_TEST_RE.findall(content))
            c = sorted({x.strip() for m in _COVERS_RE.findall(content[:600]) for x in m.split(",") if x.strip()})
            covers.update(c)
            total += n
            files_out.append({"path": rel, "tests": n, "covers": c})
        data = {"files": files_out, "covers": sorted(covers), "test_functions": total}
        return AgentOutput(data=data, summary=f"{total} test functions in {len(files_out)} file(s)")


def _env_table(features: set[str]) -> list[tuple[str, str, str]]:
    rows = [
        ("SHORTENER_DB", "shortener.db", "SQLite file path"),
        ("PUBLIC_BASE_URL", "http://localhost:8000", "Base used to build `short_url`"),
        ("SHORTENER_CODE_LENGTH", "7", "Length of generated codes"),
        ("SHORTENER_RATE_LIMIT", "30", "Link creations per client per minute"),
        ("TRUST_PROXY", "0", "Set to `1` to read the client address from X-Forwarded-For"),
    ]
    if "link_safety" in features:
        rows += [
            ("SHORTENER_BLOCKLIST", "", "Comma-separated extra blocked domains"),
            ("SHORTENER_BLOCKLIST_FILE", "", "File with one blocked domain per line"),
        ]
    return rows


def render_api_md(contract: dict) -> str:
    lines = ["# API reference", "", "_Generated from the design contract._", ""]
    for ep in contract["endpoints"]:
        lines += [f"## `{ep['method']} {ep['path']}`", "", ep["summary"], ""]
        if ep["request"]:
            lines += ["| Request field | Description |", "|---|---|"]
            lines += [f"| `{k}` | {v} |" for k, v in sorted(ep["request"].items())]
            lines.append("")
        lines += ["| Status | Meaning |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(ep["responses"].items())]
        lines.append("")
    lines += [
        "## Errors",
        "",
        "Errors are JSON: `{\"detail\": \"<reason>\"}`. Validation failures from the framework use a list in `detail`.",
        "",
    ]
    return "\n".join(lines)


def render_readme(features: set[str], contract: dict) -> str:
    eps = "\n".join(f"- `{e['method']} {e['path']}`: {e['summary']}" for e in contract["endpoints"])
    env = "\n".join(f"| `{k}` | `{d}` | {desc} |" for k, d, desc in _env_table(features))
    return (
        "# URL Shortener\n\n"
        "Create short links, redirect, and read click analytics.\n\n"
        "## Run\n\n```bash\npip install -r requirements.txt\nuvicorn shortener.main:app --port 8000\n```\n\n"
        "## Endpoints\n\n" + eps + "\n\nFull details: [docs/API.md](docs/API.md).\n\n"
        "## Configuration\n\n| Variable | Default | Purpose |\n|---|---|---|\n" + env + "\n\n"
        "## Test\n\n```bash\npytest\n```\n"
    )


class DocsAgent:
    """Generates the API reference, README and (for changes) a changelog entry from the design."""

    name = "docs-writer"

    def run(self, ctx: AgentContext) -> AgentOutput:
        design = ctx.inputs["design"]
        req = ctx.inputs["requirements"]
        contract = design["contract"]
        features = set(design["features"])

        ctx.workspace.write("docs/API.md", render_api_md(contract))
        # Injected transient failure lands after a write on purpose: the retry must start from a clean rollback.
        ctx.fail_transiently_if_configured()
        ctx.workspace.write("README.md", render_readme(features, contract))

        if design["mode"] == "brownfield":
            lines = [f"# Change: {req['problem']}", "", "## Requirements", ""]
            for fr in req["functional"]:
                lines.append(f"- **{fr['id']} {fr['title']}**: {fr['text']}")
                lines += [f"  - {a}" for a in fr["acceptance"]]
            if req["assumptions"]:
                lines += ["", "## Assumptions", ""] + [f"- {a['text']}" for a in req["assumptions"]]
            if req["out_of_scope"]:
                lines += ["", "## Out of scope", ""] + [f"- {o}" for o in req["out_of_scope"]]
            if "schema_change" in design["impact_tags"]:
                lines += ["", "## Upgrade notes", "",
                          "This change adds a database column. Existing databases are upgraded in place on startup; no manual step is needed."]
            ctx.workspace.write("docs/CHANGELOG.md", "\n".join(lines) + "\n")

        documented = sorted(f"{e['method']} {e['path']}" for e in contract["endpoints"])
        files = [{"path": p, "sha256": h[:16]} for p, h in sorted(ctx.workspace.written.items())]
        data = {"files": files, "documented_endpoints": documented}
        return AgentOutput(data=data, summary=f"{len(files)} doc file(s), {len(documented)} endpoints documented")

