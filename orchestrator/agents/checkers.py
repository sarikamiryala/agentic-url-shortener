"""Checker agents: verification, security review, migration check, release readiness.

Checkers produce *evidence* (test counts, findings, probe results). Gates decide pass or fail
from that evidence, so no stage grades its own work.
"""
from __future__ import annotations

import ast
import json
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ..analysis import parse_schema, scan_codebase
from ..sandbox import ensure_sandbox_ini, run_python
from .base import AgentContext, AgentOutput, decision

_OPENAPI_SCRIPT = """
import sys, json
sys.path.insert(0, sys.argv[1])
from shortener.api import create_app
from shortener.config import Settings
app = create_app(Settings(db_path=sys.argv[2]))
paths = app.openapi()["paths"]
methods = {"get", "post", "put", "delete", "patch"}
print(json.dumps(sorted(f"{m.upper()} {p}" for p, ms in paths.items() for m in ms if m in methods)))
"""


# ---------------------------------------------------------------------------------------
class VerificationAgent:
    """Runs the full test suite and checks the running app against the design's API contract."""

    name = "verifier"

    def run(self, ctx: AgentContext) -> AgentOutput:
        root = ctx.workspace.root
        ini = ensure_sandbox_ini(ctx.run_dir)
        with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
            tmp_path = Path(tmp)
            xml_path = tmp_path / "junit.xml"
            res = run_python(
                ["-m", "pytest", "tests", "-q", f"--junitxml={xml_path}", f"--rootdir={root}", "-c", str(ini)],
                cwd=root, pythonpath=[root], timeout=180,
            )
            tests, failures = self._parse_junit(xml_path, res.returncode)

            contract = ctx.inputs["design"]["contract"]["endpoints"]
            expected = sorted(f"{e['method']} {e['path']}" for e in contract)
            probe = run_python(["-c", _OPENAPI_SCRIPT, str(root), str(tmp_path / "probe.db")],
                               cwd=tmp_path, pythonpath=[], timeout=60)
            try:
                actual = sorted(json.loads(probe.stdout.strip().splitlines()[-1]))
                probe_error = None
            except (ValueError, IndexError):
                actual, probe_error = [], (probe.stderr.strip().splitlines() or ["probe produced no output"])[-1]
        conformance = {
            "expected": expected,
            "actual": actual,
            "missing": sorted(set(expected) - set(actual)),
            "undocumented": sorted(set(actual) - set(expected)),
            "error": probe_error,
        }
        ok = (
            tests["failed"] == 0 and tests["errors"] == 0 and tests["total"] > 0
            and not conformance["missing"] and probe_error is None
        )
        data = {"ok": ok, "tests": tests, "failures": failures, "conformance": conformance}
        evidence = {"duration_s": round(res.duration, 2), "pytest_returncode": res.returncode,
                    "stdout_tail": res.stdout[-1500:]}
        return AgentOutput(
            data=data,
            summary=f"{tests['passed']}/{tests['total']} tests passed; "
                    f"{len(conformance['missing'])} contract endpoint(s) missing",
            evidence=evidence,
        )

    @staticmethod
    def _parse_junit(path: Path, returncode: int) -> tuple[dict, list[dict]]:
        tests = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        failures: list[dict] = []
        if not path.exists():
            tests["errors"] = 1
            failures.append({"name": "pytest", "message": f"no junit report produced (exit {returncode})"})
            return tests, failures
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        for s in suites:
            for key, attr in (("total", "tests"), ("failed", "failures"), ("errors", "errors"), ("skipped", "skipped")):
                tests[key] += int(s.get(attr, 0))
        tests["passed"] = tests["total"] - tests["failed"] - tests["errors"] - tests["skipped"]
        for case in root.iter("testcase"):
            for kind in ("failure", "error"):
                el = case.find(kind)
                if el is not None and len(failures) < 10:
                    failures.append({"name": f"{case.get('classname')}.{case.get('name')}",
                                     "message": (el.get("message") or "")[:300]})
        return tests, failures


# ---------------------------------------------------------------------------------------
def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _string_built(arg: ast.AST) -> bool:
    if isinstance(arg, ast.JoinedStr):
        return True
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Mod)):
        return True
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "format"
    )


def scan_python(rel: str, tree: ast.AST, forbidden: list[str]) -> list[dict]:
    out: list[dict] = []

    def add(node: ast.AST, rule: str, severity: str, detail: str) -> None:
        out.append({"path": rel, "line": getattr(node, "lineno", 0), "rule": rule, "severity": severity, "detail": detail})

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in forbidden:
                add(node, "dangerous_call", "high", f"call to {name}()")
            if name and name.startswith("subprocess.") and any(
                k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords
            ):
                add(node, "dangerous_call", "high", "subprocess with shell=True")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "executemany", "executescript"}:
                if node.args and _string_built(node.args[0]):
                    add(node, "sql_injection", "high", "SQL built with string formatting; use parameters")
            if any(k.arg == "verify" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords):
                add(node, "tls_verification_disabled", "medium", "verify=False")
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            add(node, "bare_except", "low", "bare except hides errors")
    return out


class SecurityReviewAgent:
    """Static review of the generated code: dangerous calls, SQL construction, secrets, PII columns."""

    name = "security-reviewer"

    def run(self, ctx: AgentContext) -> AgentOutput:
        ws = ctx.workspace
        policy = ctx.policy
        findings: list[dict] = []
        py_files = ws.files(["shortener/**/*.py"])

        validate_called = False
        for rel in py_files:
            text = ws.read_text(rel)
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                findings.append({"path": rel, "line": exc.lineno or 0, "rule": "syntax_error", "severity": "high", "detail": str(exc.msg)})
                continue
            findings += scan_python(rel, tree, policy.forbidden_calls)
            for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create"):
                for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
                    if getattr(call.func, "id", None) == "validate_url":
                        validate_called = True
            for table, cols in parse_schema(text).items():
                for c in cols:
                    if c.lower() in policy.pii_columns:
                        findings.append({"path": rel, "line": 0, "rule": "pii_in_schema", "severity": "high",
                                         "detail": f"column {table}.{c} stores a direct identifier"})
        for rel in ws.files():
            for v in policy.scan_secrets(rel, ws.read_text(rel)):
                findings.append({"path": rel, "line": v.line or 0, "rule": v.rule, "severity": "high", "detail": v.detail})
        if py_files and not validate_called:
            findings.append({"path": "shortener/service.py", "line": 0, "rule": "missing_input_validation",
                             "severity": "high", "detail": "service.create does not call validate_url"})

        findings.sort(key=lambda f: (f["path"], f["line"], f["rule"]))
        by_rule = {f["rule"] for f in findings if f["severity"] == "high"}
        checks = [
            {"check": "sql_parameterised", "ok": "sql_injection" not in by_rule},
            {"check": "no_dangerous_calls", "ok": "dangerous_call" not in by_rule},
            {"check": "no_secrets", "ok": "secret_detected" not in by_rule},
            {"check": "no_pii_columns", "ok": "pii_in_schema" not in by_rule},
            {"check": "input_validation_present", "ok": "missing_input_validation" not in by_rule},
        ]
        high = [f for f in findings if f["severity"] == "high"]
        data = {"ok": not high, "findings": findings, "checks": checks, "files_scanned": len(py_files)}
        return AgentOutput(data=data, summary=f"{len(high)} high, {len(findings) - len(high)} other finding(s)")


# ---------------------------------------------------------------------------------------
_BASELINE_SCRIPT = """
import sys, json
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from shortener.api import create_app
from shortener.config import Settings
app = create_app(Settings(db_path=sys.argv[2], rate_limit_per_minute=1000))
codes = []
with TestClient(app) as c:
    for i in range(2):
        r = c.post("/api/links", json={"url": f"https://legacy{i}.example.com/page"})
        assert r.status_code == 201, r.text
        codes.append(r.json()["code"])
    c.get(f"/{codes[0]}", follow_redirects=False)
app.state.storage.close()
print(json.dumps({"codes": codes}))
"""

_UPGRADE_SCRIPT = """
import sys, json
sys.path.insert(0, sys.argv[1])
db, codes, features = sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4])
checks = []
def check(name, fn):
    try:
        ok, detail = fn()
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    except Exception as exc:
        checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
state = {}
def open_new_code_on_old_db():
    from fastapi.testclient import TestClient
    from shortener.api import create_app
    from shortener.config import Settings
    app = create_app(Settings(db_path=db, rate_limit_per_minute=1000))
    state["app"], state["c"] = app, TestClient(app)
    return True, "opened a database created by the previous version"
check("open_previous_version_database", open_new_code_on_old_db)
if "c" in state:
    c = state["c"]
    check("old_link_still_redirects", lambda: ((r := c.get(f"/{codes[0]}", follow_redirects=False)).status_code == 307, f"status {r.status_code}"))
    check("old_clicks_preserved", lambda: ((s := c.get(f"/api/links/{codes[0]}/stats").json())["clicks"] == 2, f"clicks={s.get('clicks')} (1 before upgrade, 1 now)"))
    check("new_link_creation_works", lambda: ((r := c.post("/api/links", json={"url": "https://new.example.com"})).status_code == 201, f"status {r.status_code}"))
    if "custom_alias" in features:
        check("new_alias_link_works", lambda: ((r := c.post("/api/links", json={"url": "https://a.example.com", "alias": "post-upgrade"})).status_code == 201, f"status {r.status_code}"))
    if "expiry" in features:
        check("new_expiring_link_works", lambda: ((r := c.post("/api/links", json={"url": "https://e.example.com", "expires_in_days": 1})).status_code == 201, f"status {r.status_code}"))
        check("old_link_has_no_expiry", lambda: ((s := c.get(f"/api/links/{codes[1]}/stats").json()).get("expires_at") is None, f"expires_at={s.get('expires_at')}"))
print(json.dumps(checks))
"""


class MigrationCheckAgent:
    """Opens a database written by the *pre-change* code with the *new* code and exercises it.

    Inserted into the graph by the re-planner whenever the design reports a schema change on an
    existing system. Fresh-database unit tests cannot catch a missing in-place migration; this can.
    """

    name = "migration-checker"

    def run(self, ctx: AgentContext) -> AgentOutput:
        baseline = ctx.baseline_dir
        if baseline is None or not (baseline / "shortener").is_dir():
            return AgentOutput(
                data={"ok": False, "checks": [{"name": "baseline_available", "ok": False,
                                               "detail": "the starting codebase has no shortener/ package to build a legacy database from"}]},
                summary="no baseline snapshot",
            )
        features = ctx.inputs["design"]["requested_features"]
        with tempfile.TemporaryDirectory(prefix="migrate-") as tmp:
            tmp_path = Path(tmp)
            db = str(tmp_path / "legacy.db")
            legacy = run_python(["-c", _BASELINE_SCRIPT, str(baseline), db], cwd=tmp_path, pythonpath=[], timeout=60)
            try:
                codes = json.loads(legacy.stdout.strip().splitlines()[-1])["codes"]
            except (ValueError, IndexError, KeyError):
                tail = (legacy.stderr.strip().splitlines() or ["no output"])[-1]
                return AgentOutput(
                    data={"ok": False, "checks": [{"name": "build_legacy_database", "ok": False, "detail": tail}]},
                    summary="could not build the legacy database",
                )
            upgraded = run_python(
                ["-c", _UPGRADE_SCRIPT, str(ctx.workspace.root), db, json.dumps(codes), json.dumps(features)],
                cwd=tmp_path, pythonpath=[], timeout=60,
            )
        try:
            checks = json.loads(upgraded.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            tail = (upgraded.stderr.strip().splitlines() or ["no output"])[-1]
            checks = [{"name": "upgrade_probe", "ok": False, "detail": tail}]
        ok = all(c["ok"] for c in checks)
        failed = [c for c in checks if not c["ok"]]
        return AgentOutput(
            data={"ok": ok, "checks": checks},
            summary="legacy database upgrades cleanly" if ok else f"{len(failed)} check(s) failed: {failed[0]['detail']}",
            decisions=[decision("migration_probe", "Migration probe", "legacy database -> new code",
                                "A database created by the pre-change code is opened by the new code and exercised.", [])],
        )


# ---------------------------------------------------------------------------------------
def render_summary(ctx: AgentContext, final: bool, bundle: dict | None = None) -> str:
    i = ctx.inputs
    req, design = i["requirements"], i["design"]
    facts = ctx.run_facts
    ver, sec = i["verification"], i["security_review"]
    mig = i.get("migration_check")
    out = [f"# Engineering summary{'' if final else ' (draft for release approval)'}", ""]
    out += ["## 1. Requirement and interpretation", "", f"> {req['raw']}", ""]
    for a in req["amendments"]:
        out.append(f"> Amendment: {a}")
    out += ["", f"**Normalised problem:** {req['problem']}", ""]
    out += [f"- **{fr['id']} {fr['title']}**: {fr['text']}" for fr in req["functional"]]
    out += ["", "## 2. Plan and rationale", ""]
    for t in design["tasks"]:
        dep = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
        out.append(f"- `{t['id']}` [{t['stage']}] {t['title']}{dep}")
    out += ["", "Key design decisions:", ""]
    out += [f"- **{a['title']}:** {a['choice']}. {a['rationale']}" for a in design["architecture"]]
    out += ["", "## 3. Artifacts", ""]
    impl = i["implementation"]
    out += [f"- `{f['path']}`" for f in impl["files"]]
    out += [f"- `{f['path']}` ({f['tests']} tests, covers: {', '.join(f['covers'])})" for f in i["test_authoring"]["files"]]
    out += [f"- `{f['path']}`" for f in i["docs"]["files"]]
    out += ["", "## 4. Validation", ""]
    t = ver["tests"]
    out.append(f"- Tests: {t['passed']}/{t['total']} passed ({t['failed']} failed, {t['errors']} errors)")
    c = ver["conformance"]
    out.append(f"- API contract: {len(c['expected']) - len(c['missing'])}/{len(c['expected'])} endpoints present")
    out.append(f"- Security review: {sum(1 for f in sec['findings'] if f['severity'] == 'high')} high finding(s), {sec['files_scanned']} files scanned")
    if mig:
        out.append(f"- Migration check: {'passed' if mig['ok'] else 'FAILED'} ({len(mig['checks'])} probes against a pre-change database)")
    out += ["", "## 5. Risks and trade-offs", ""]
    for r in design["risks"]:
        out.append(f"- **{r['risk']}** ({r['impact']}). Mitigation: {r['mitigation']}. Validated by: `{r['validated_by']}`." + (f" {r['note']}" if r["note"] else ""))
    out += ["", "## 6. Assumptions", ""]
    out += [f"- {a['text']}" for a in req["assumptions"]] or ["- None recorded."]
    if req["out_of_scope"]:
        out += ["", "Out of scope:", ""] + [f"- {o}" for o in req["out_of_scope"]]
    out += ["", "## 7. Orchestration record", ""]
    out.append(
        f"- Retries: {facts['retries']}, reworks: {facts['reworks']}, rollbacks: {facts['rollbacks']}, "
        f"re-plans: {facts['replans']}, policy violations blocked: {facts['policy_violations']}"
    )
    for a in facts["approvals"]:
        out.append(f"- Approval `{a['node']}`: {a['status']}" + (f" by {a['by']}" if a.get("by") else ""))
    if bundle:
        out.append(f"- Release bundle: `{bundle['path']}` ({bundle['files']} files)")
    out += [
        "", "## 8. Limitations", "",
        "- Agents are deterministic stand-ins for an LLM: they apply reference code, they do not invent new designs.",
        "- Generated code runs in a subprocess with a timeout; that is isolation from crashes, not a security sandbox.",
        "- State, queue and audit chain live in one SQLite file on one node.",
        "",
    ]
    return "\n".join(out)


class ReleaseReadinessAgent:
    """Aggregates evidence into a release checklist and writes the engineering summary.

    `run` has no side effects outside the run's report directory. `commit` (packaging the release
    bundle) happens only after a human approves.
    """

    name = "release-manager"

    def run(self, ctx: AgentContext) -> AgentOutput:
        i = ctx.inputs
        design = i["design"]
        features = set(design["requested_features"])
        covered = set(i["test_authoring"]["covers"]) | {
            c for t in scan_codebase(ctx.workspace.root)["tests"] for c in t["covers"]
        }
        documented = set(i["docs"]["documented_endpoints"])
        expected = {f"{e['method']} {e['path']}" for e in design["contract"]["endpoints"]}
        checklist = [
            {"item": "All tests pass", "ok": i["verification"]["tests"]["failed"] == 0 and i["verification"]["tests"]["errors"] == 0},
            {"item": "Running app matches the API contract", "ok": not i["verification"]["conformance"]["missing"]},
            {"item": "No high-severity security findings", "ok": i["security_review"]["ok"]},
            {"item": "Every requested feature has tests", "ok": features <= covered},
            {"item": "Every endpoint is documented", "ok": expected <= documented},
        ]
        if "migration_check" in i:
            checklist.append({"item": "Existing data upgrades cleanly", "ok": i["migration_check"]["ok"]})
        ok = all(c["ok"] for c in checklist)
        draft = render_summary(ctx, final=False)
        (ctx.report_dir / "engineering_summary.draft.md").write_text(draft, encoding="utf-8")
        data = {"ok": ok, "checklist": checklist, "tasks": len(design["tasks"]), "files": len(i["implementation"]["files"])}
        return AgentOutput(data=data, summary="release checklist " + ("green" if ok else "has failures"))

    def commit(self, ctx: AgentContext, data: dict) -> dict:
        """Package the release bundle. Called by the engine only after approval."""
        bundle_dir = ctx.run_dir / "release"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        path = bundle_dir / f"release-{ctx.run_id}.zip"
        files = ctx.full_workspace.files()
        info = {"path": f"release/{path.name}", "files": len(files)}
        summary = render_summary(ctx, final=True, bundle=info)
        (ctx.report_dir / "engineering_summary.md").write_text(summary, encoding="utf-8")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in files:
                z.write(ctx.full_workspace.root / rel, rel)
            z.writestr("ENGINEERING_SUMMARY.md", summary)
        info["bytes"] = path.stat().st_size
        return info
