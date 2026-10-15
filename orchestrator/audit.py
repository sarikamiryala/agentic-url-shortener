"""Append-only, hash-chained audit log.

Every event stores the hash of the previous event in the same run, so editing or
deleting a row breaks the chain and `verify` reports where. This gives tamper
*evidence*, not tamper *prevention*: someone with write access to the database
can rewrite the whole chain. Anchoring the head hash externally is the fix.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Database
from .util import canonical_json, now, sha256_text

GENESIS = "GENESIS"


@dataclass
class VerifyResult:
    ok: bool
    events: int
    broken_at: int | None = None
    reason: str = ""
    head: str = GENESIS

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "events": self.events,
            "broken_at": self.broken_at,
            "reason": self.reason,
            "head": self.head,
        }


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(
        self,
        run_id: str,
        type: str,
        actor: str,
        node_id: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        payload = payload or {}
        with self.db.transaction():
            last = self.db.one(
                "SELECT seq, hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            )
            seq = last["seq"] + 1 if last else 1
            prev = last["hash"] if last else GENESIS
            ts = now()
            body = {
                "run_id": run_id,
                "seq": seq,
                "ts": ts,
                "actor": actor,
                "type": type,
                "node_id": node_id,
                "payload": payload,
            }
            h = sha256_text(prev + canonical_json(body))
            self.db.execute(
                "INSERT INTO events (run_id, seq, ts, actor, type, node_id, payload, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, ts, actor, type, node_id, canonical_json(payload), prev, h),
            )
        return {**body, "prev_hash": prev, "hash": h}

    @staticmethod
    def _row(r) -> dict:
        return {
            "run_id": r["run_id"],
            "seq": r["seq"],
            "ts": r["ts"],
            "actor": r["actor"],
            "type": r["type"],
            "node_id": r["node_id"],
            "payload": json.loads(r["payload"]),
            "prev_hash": r["prev_hash"],
            "hash": r["hash"],
        }

    def events(self, run_id: str, since_seq: int = 0) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, since_seq)
        )
        return [self._row(r) for r in rows]

    def counts(self, run_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT type, COUNT(*) AS n FROM events WHERE run_id = ? GROUP BY type", (run_id,)
        )
        return {r["type"]: r["n"] for r in rows}

    def verify(self, run_id: str) -> VerifyResult:
        prev = GENESIS
        count = 0
        for ev in self.events(run_id):
            count += 1
            if ev["seq"] != count:
                return VerifyResult(False, count, ev["seq"], "sequence gap or deleted event", prev)
            if ev["prev_hash"] != prev:
                return VerifyResult(False, count, ev["seq"], "prev_hash does not match", prev)
            body = {k: ev[k] for k in ("run_id", "seq", "ts", "actor", "type", "node_id", "payload")}
            if sha256_text(prev + canonical_json(body)) != ev["hash"]:
                return VerifyResult(False, count, ev["seq"], "event content was modified", prev)
            prev = ev["hash"]
        return VerifyResult(True, count, None, "", prev)
