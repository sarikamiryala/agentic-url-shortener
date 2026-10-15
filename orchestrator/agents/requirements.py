"""Requirements agent: interpret intent, find ambiguity, normalise into an engineering problem.

Interpretation is keyword/pattern based against the feature catalog, plus (for
brownfield) a scan of the real codebase so capabilities that already exist are not
re-requested. Anything it cannot map becomes a blocking question for a human; it never
guesses silently on something that changes behaviour.
"""
from __future__ import annotations

import re

from ..analysis import scan_codebase
from ..catalog import FEATURE_ORDER, FEATURES, ROUTE_FEATURE
from .base import AgentContext, AgentOutput, decision

PATTERNS = {
    "shorten": r"short(?:en|ener)?\s+(?:link|url)|url shortener|shorten",
    "redirect": r"redirect|follow(?:s|ed)?\s+a\s+short",
    "analytics": r"analytics|\bclicks?\b|statistics|\bstats\b",
    "validation": r"validat",
    "rate_limit": r"rate[- ]?limit|throttl",
    "custom_alias": r"alias|vanity|custom (?:short )?(?:code|slug|link)",
    "expiry": r"expir|time[- ]to[- ]live|\bttl\b",
    "link_safety": r"\bsafer\b|\bsafe\b|malicious|phishing|blocklist|denylist",
}
VAGUE_NFR = re.compile(r"\b(scalable|fast|faster|robust|reliable|secure|modern|user[- ]friendly)\b", re.I)

SAFETY_QUESTIONS = [
    {
        "id": "threat",
        "question": "'Safer' can mean several different things. Which threat should this change address?",
        "options": [
            {"value": "malicious_destinations", "label": "Links that point to phishing or malware sites", "supported": True},
            {"value": "service_abuse", "label": "Abuse of the service itself (spam, bulk link creation)", "supported": False},
            {"value": "click_privacy", "label": "Privacy of the click analytics data", "supported": False},
        ],
        "default": "malicious_destinations",
    },
    {
        "id": "enforce",
        "question": "Where should the destination check apply?",
        "options": [
            {"value": "create_only", "label": "Only when a link is created", "supported": True},
            {"value": "create_and_redirect", "label": "When a link is created and every time it is followed", "supported": True},
        ],
        "default": "create_only",
    },
    {
        "id": "match",
        "question": "How should a blocked domain match?",
        "options": [
            {"value": "exact_host", "label": "Exact host only", "supported": True},
            {"value": "host_and_subdomains", "label": "The host and all of its subdomains", "supported": True},
        ],
        "default": "host_and_subdomains",
    },
]


def baseline_features(scan: dict) -> set[str]:
    """Capabilities the existing codebase already has, derived from the code itself."""
    found: set[str] = set()
    for r in scan["routes"]:
        f = ROUTE_FEATURE.get((r["method"], r["path"]))
        if f:
            found.add(f)
    if "validation" in scan["modules"]:
        found.add("validation")
    if "ratelimit" in scan["modules"]:
        found.add("rate_limit")
    if "validate_alias" in scan["defined_in"]:
        found.add("custom_alias")
    if "expires_at" in scan["schema"].get("links", []):
        found.add("expiry")
    if "safety" in scan["modules"]:
        found.add("link_safety")
    return found


def enforce_from_amendments(amendments: list[str]) -> str | None:
    for text in amendments:
        t = text.lower()
        if re.search(r"click|follow(?:ed)?|redirect|visit|resolve", t) and re.search(
            r"check|block|enforce|stop working|refuse", t
        ):
            return "create_and_redirect"
    return None


class RequirementsAgent:
    name = "requirements-analyst"

    def run(self, ctx: AgentContext) -> AgentOutput:
        decisions: list[dict] = []
        answers = dict(ctx.answers)
        free_text = " ".join([ctx.requirement, *ctx.amendments, str(answers.get("intent", ""))]).strip()

        scan = scan_codebase(ctx.baseline_dir) if ctx.mode == "brownfield" else None
        baseline = baseline_features(scan) if scan else set()
        detected = {f for f, pat in PATTERNS.items() if re.search(pat, free_text, re.I)}
        features = detected - baseline
        already = sorted(detected & baseline)

        questions: list[dict] = []
        assumptions: list[dict] = []
        out_of_scope: list[str] = []

        def assume(text: str, why: str) -> None:
            assumptions.append({"id": f"A-{len(assumptions) + 1}", "text": text, "rationale": why})

        if not features:
            note = (
                f"Already implemented in the codebase: {', '.join(already)}." if already else
                "No known capability matched this text."
            )
            questions.append(
                {
                    "id": "intent",
                    "question": f"{note} What should change? (describe the capability in a sentence)",
                    "options": [], "default": None, "blocking": True,
                    "answered": False, "answer": None, "note": None,
                }
            )

        safety: dict | None = None
        if "link_safety" in features:
            amended_enforce = enforce_from_amendments(ctx.amendments)
            safety = {}
            for qd in SAFETY_QUESTIONS:
                qid = qd["id"]
                value = answers.get(qid)
                source = ctx.answer_sources.get(qid, "human")
                if qid == "enforce" and amended_enforce:
                    if value != amended_enforce:
                        decisions.append(
                            decision(
                                "answer:enforce",
                                "Enforcement point changed by a reviewer amendment",
                                amended_enforce,
                                "A reviewer amendment asked for the check to also apply when a link is followed; "
                                "it overrides the earlier answer.",
                                [str(value)],
                            )
                        )
                    value, source = amended_enforce, "amendment"
                q = {
                    "id": qid, "question": qd["question"], "options": qd["options"], "default": qd["default"],
                    "blocking": True, "answered": False, "answer": None, "note": None,
                }
                if value is not None:
                    opt = next((o for o in qd["options"] if o["value"] == value), None)
                    if opt is None:
                        q["note"] = f"'{value}' is not one of the offered options."
                    elif not opt["supported"]:
                        q["note"] = (
                            "The available agents cannot build this. Choose a supported option or hand it to an engineer."
                        )
                    else:
                        q["answered"], q["answer"], q["source"] = True, value, source
                        safety[qid] = value
                        if qid != "enforce" or source != "amendment":
                            decisions.append(
                                decision(
                                    f"answer:{qid}", f"Clarification: {qd['question']}", value,
                                    {"human": "Answered by a human reviewer.", "default": "Human accepted the proposed default.",
                                     "amendment": "Set by a reviewer amendment."}.get(source, "Answered."),
                                    [o["value"] for o in qd["options"] if o["value"] != value],
                                )
                            )
                questions.append(q)
            if safety.get("threat") == "malicious_destinations":
                assume(
                    "'Safer' is interpreted as refusing destinations on a known-bad list.",
                    "Confirmed by the human answer to the 'threat' question.",
                )
                assume(
                    "The blocklist is static and operator-managed (built-in demo entries plus env/file extensions).",
                    "An external reputation service is out of scope for this prototype.",
                )
                out_of_scope.append("Real-time reputation lookups (Safe Browsing-style services)")
                out_of_scope.append("Detection of newly registered malicious domains")

        if ctx.mode == "brownfield" and features:
            assume(
                "Existing links, stored data and current API behaviour must keep working.",
                "Stated or implied for any change to a running service.",
            )
        for m in sorted({m.group(0).lower() for m in VAGUE_NFR.finditer(free_text)}):
            assume(
                f"'{m}' has no measurable target; treated as: single node, SQLite, parameterised SQL, tests per requirement.",
                "Vague quality words are recorded as assumptions rather than silently ignored.",
            )

        ordered = [f for f in FEATURE_ORDER if f in features]
        functional = []
        for i, f in enumerate(ordered, 1):
            acceptance = list(FEATURES[f]["acceptance"])
            if f == "link_safety" and safety:
                if safety.get("enforce") == "create_and_redirect":
                    acceptance.append(
                        "A link whose destination is blocklisted after creation returns 403 on redirect and stops counting clicks"
                    )
                if safety.get("match") == "host_and_subdomains":
                    acceptance.append("Subdomains of a blocklisted domain are also refused")
                elif safety.get("match") == "exact_host":
                    acceptance.append("Only the exact blocklisted host is refused")
            functional.append(
                {"id": f"FR-{i}", "feature": f, "title": FEATURES[f]["title"], "text": FEATURES[f]["fr"],
                 "priority": "must", "acceptance": acceptance}
            )

        non_functional = [
            {"id": "NFR-1", "text": "All SQL is parameterised; no string-built queries."},
            {"id": "NFR-2", "text": "Analytics must not persist raw IP addresses or other direct identifiers."},
            {"id": "NFR-3", "text": "No secrets or credentials in source."},
            {"id": "NFR-4", "text": "Every functional requirement is covered by at least one automated test."},
        ]
        if ctx.mode == "brownfield":
            non_functional.append(
                {"id": "NFR-5", "text": "Backward compatible: existing links, data and API responses keep working."}
            )

        titles = ", ".join(FEATURES[f]["title"].lower() for f in ordered) or "(nothing identified yet)"
        if ctx.mode == "greenfield":
            problem = f"Build a new URL shortener service providing: {titles}."
        else:
            problem = (
                f"Change the existing URL shortener (already has: {', '.join(sorted(baseline)) or 'n/a'}) "
                f"to add: {titles}, without breaking current behaviour."
            )

        blocking_open = [q for q in questions if q["blocking"] and not q["answered"]]
        decisions.insert(
            0,
            decision(
                "interpretation",
                "How the request was interpreted",
                ", ".join(ordered) or "no buildable capability identified",
                "Feature catalog pattern match on the request text"
                + (" minus capabilities the codebase scan shows already exist." if ctx.mode == "brownfield" else "."),
                [],
            ),
        )
        data = {
            "problem": problem,
            "mode": ctx.mode,
            "raw": ctx.requirement,
            "amendments": list(ctx.amendments),
            "features": ordered,
            "baseline_features": sorted(baseline),
            "functional": functional,
            "non_functional": non_functional,
            "out_of_scope": out_of_scope,
            "assumptions": assumptions,
            "open_questions": questions,
            "safety": safety,
            "ok": not blocking_open,
        }
        summary = (
            f"{len(functional)} functional requirement(s), {len(assumptions)} assumption(s), "
            f"{len(blocking_open)} blocking question(s)"
        )
        return AgentOutput(data=data, summary=summary, decisions=decisions)
