"""Design agent: codebase reasoning, impact analysis, API contract, task decomposition, risks.

For brownfield work every statement about the existing system comes from `analysis.scan_codebase`
(AST + schema parsing of the workspace), not from assumptions.
"""
from __future__ import annotations

from ..analysis import scan_codebase, symbol_footprint
from ..catalog import compose_contract, pack_tasks, risks_for, select_pack
from .base import AgentContext, AgentOutput, AgentRefusal, decision

# For each requested feature: which existing symbols a change must touch, and why.
IMPACT_RULES: dict[str, dict] = {
    "custom_alias": {
        "anchors": [
            ("CreateLinkRequest", "request model gains an optional `alias` field"),
            ("create", "service.create must accept and validate an alias"),
            ("insert_link", "an alias collision must surface as a duplicate-code error (-> 409)"),
            ("validate_url", "alias validation belongs beside URL validation"),
        ],
        "schema": None,
    },
    "expiry": {
        "anchors": [
            ("CreateLinkRequest", "request model gains an optional `expires_in_days` field"),
            ("create", "service.create computes expires_at"),
            ("resolve", "must refuse expired links (-> 410) and skip click recording"),
            ("get_link", "SELECT must return expires_at"),
            ("insert_link", "INSERT must persist expires_at"),
        ],
        "schema": {"table": "links", "add": ["expires_at"]},
    },
    "link_safety": {
        "anchors": [
            ("create", "check the destination before inserting"),
            ("resolve", "optionally re-check when the link is followed"),
            ("validate_url", "the blocklist check follows URL validation"),
            ("Settings", "new blocklist settings"),
        ],
        "schema": None,
    },
}

ADRS = {
    "core_service": [
        ("storage", "Storage engine", "SQLite (stdlib)", "Zero operational cost and transactional; single-writer is acceptable for a prototype.", ["Postgres", "Redis"]),
        ("redirect_status", "Redirect status code", "307 Temporary Redirect", "301/308 are cached by browsers, which would hide repeat clicks from analytics.", ["301", "302", "308"]),
        ("code_scheme", "Short code scheme", "7 random base62 characters from `secrets`", "62^7 is about 3.5e12 codes, unguessable, with bounded collision retries.", ["Sequential ids + base62", "Hash of the URL"]),
        ("layering", "Module layering", "api -> service -> storage; only storage contains SQL", "Keeps validation and business rules testable without HTTP or a database.", []),
        ("rate_limit", "Rate limiting", "In-memory sliding window per client address", "Simple and dependency-free; resets on restart and is per process (documented limitation).", ["Redis token bucket", "API gateway"]),
        ("privacy", "What analytics stores", "Timestamp, referrer and user agent only", "No IP addresses are persisted, which keeps the compliance surface small.", ["Store IP"]),
    ],
    "aliases_expiry": [
        ("expiry_column", "How expiry is stored", "Nullable `expires_at` epoch-seconds column on `links`", "Additive and backward compatible; NULL means never expires, so old rows keep working.", ["Separate expiry table", "Boolean flag + job"]),
        ("alias_model", "How aliases are stored", "The alias is the code itself", "One namespace, uniqueness already enforced by the primary key.", ["Separate alias table"]),
        ("reserved", "Alias namespace safety", "Reserved-word list, matched case-insensitively", "Aliases share the URL space with fixed routes such as /api and /healthz.", []),
        ("expired_status", "Status for expired links", "410 Gone, and no click is recorded", "Lets clients tell 'never existed' (404) from 'used to exist' (410).", ["404"]),
        ("migration", "How existing databases are upgraded", "In-place, idempotent column-exists check at startup", "A migration framework is overkill for one column; the check is safe to run on every start.", ["Alembic", "Manual SQL"]),
    ],
    "safer_links": [
        ("source", "Where bad destinations come from", "Static blocklist: built-in demo entries plus env/file extensions", "Operator-owned, no external dependency; cannot detect new threats.", ["External reputation API"]),
        ("fail_fast", "Unreadable blocklist file", "Fail at startup", "Failing open would silently disable the control.", ["Log and continue"]),
    ],
}


class DesignAgent:
    name = "design-architect"

    def run(self, ctx: AgentContext) -> AgentOutput:
        req = ctx.inputs["requirements"]
        mode = req["mode"]
        requested = set(req["features"])
        baseline = set(req.get("baseline_features", []))
        safety = req.get("safety") or {}
        all_features = requested | baseline

        scan = scan_codebase(ctx.baseline_dir) if mode == "brownfield" else None
        pack = select_pack(requested, mode)
        if pack is None:
            raise AgentRefusal(
                f"No available implementation capability for features {sorted(requested)} in {mode} mode. "
                "This needs an engineer or an LLM-backed agent."
            )

        tags: set[str] = {"public_api_change"}
        codebase: dict
        impact: list[dict] = []
        if mode == "greenfield":
            tags.add("new_system")
            codebase = {
                "existing": False,
                "planned_modules": sorted({t["module"] for t in pack_tasks(pack, safety) if t["stage"] == "implementation"}),
            }
        else:
            assert scan is not None
            codebase = {
                "existing": True,
                "modules": {k: {"path": v["path"], "loc": v["loc"], "imports": v["imports"]} for k, v in scan["modules"].items()},
                "routes": [f"{r['method']} {r['path']} -> {r['handler']}" for r in scan["routes"]],
                "schema": scan["schema"],
                "flows": scan["flows"],
                "tests": scan["tests"],
            }
            for feature in sorted(requested):
                rule = IMPACT_RULES.get(feature)
                if not rule:
                    continue
                entry = {"feature": feature, "touches": [], "modules": set(), "schema_change": None}
                for symbol, why in rule["anchors"]:
                    fp = symbol_footprint(scan, symbol)
                    entry["touches"].append({**fp, "why": why})
                    entry["modules"].update(fp["defined_in"])
                if rule["schema"]:
                    table = rule["schema"]["table"]
                    existing = scan["schema"].get(table)
                    missing = [c for c in rule["schema"]["add"] if existing is not None and c not in existing]
                    if existing is None:
                        raise AgentRefusal(f"Table '{table}' not found in the codebase; cannot plan a schema change.")
                    if missing:
                        entry["schema_change"] = {"table": table, "existing_columns": existing, "add_columns": missing}
                        tags.add("schema_change")
                entry["modules"] = sorted(entry["modules"])
                impact.append(entry)

        tasks = pack_tasks(pack, safety)
        contract = compose_contract(all_features, safety)
        risks = risks_for(requested if mode == "brownfield" else all_features, tags, safety)

        adrs = ADRS.get(pack, [])
        decisions = [decision(f"adr:{k}", title, choice, why, alts) for k, title, choice, why, alts in adrs]
        if pack == "safer_links":
            decisions.append(decision("adr:enforce", "Where the check applies", safety.get("enforce", "create_only"),
                                      "Taken from the human answer, or from a reviewer amendment.", ["create_only", "create_and_redirect"]))
            decisions.append(decision("adr:match", "Blocklist match mode", safety.get("match", "host_and_subdomains"),
                                      "Taken from the human answer.", ["exact_host", "host_and_subdomains"]))
        decisions.append(
            decision("impact", "Impact classification", ", ".join(sorted(tags)),
                     "Derived from the codebase scan and the requested features; drives approval and extra checks.", [])
        )

        impl_modules = sorted({t["module"] for t in tasks if t["stage"] == "implementation"})
        high_risks = [r["risk"] for r in risks if r["impact"] == "high"]
        data = {
            "pack": pack,
            "mode": mode,
            "features": sorted(all_features),
            "requested_features": sorted(requested),
            "safety": safety or None,
            "architecture": [{"key": k, "title": t, "choice": c, "rationale": w, "alternatives": a} for k, t, c, w, a in adrs],
            "contract": contract,
            "codebase": codebase,
            "impact": impact,
            "tasks": tasks,
            "risks": risks,
            "impact_tags": sorted(tags),
            "review": {
                "what_is_being_approved": f"{len(tasks)} tasks across {len(impl_modules)} module(s): {', '.join(impl_modules)}",
                "impact_tags": sorted(tags),
                "high_risks": high_risks,
                "contract_endpoints": [f"{e['method']} {e['path']}" for e in contract["endpoints"]],
            },
        }
        return AgentOutput(
            data=data,
            summary=f"{len(tasks)} tasks, {len(risks)} risks, tags: {', '.join(sorted(tags))}",
            decisions=decisions,
        )
