"""Persistent memory.

Storage is SQLite (see ``livium.store``); retrieval is SQLite FTS5 with BM25
ranking, which the runtime provides natively. No vector database, no embedding
service, no external memory product: those would add a dependency and a
credential for a capability that plain ranked full-text search already delivers
at this scale. If real usage ever shows FTS is inadequate, that will be a
recorded capability gap with evidence behind it -- not a guess made now.

Two ideas this module takes seriously:

*Provenance and trust.* A record carries where it came from, how confident we
were, how it was verified, and when. Retrieval can then weight a
human-approved decision differently from an unverified model assertion. Memory
is evidence, not truth, so the record keeps the distinction rather than
flattening it.

*Protected records.* Foundational constraints cannot be updated or forgotten by
the agent. The rule lives in the row (``protected=1``), and the write path
refuses. Deleting memory is available; deleting *these* memories is not.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from typing import Any, Sequence

from .store import Store, now

KINDS = ("foundational", "capability", "experience", "decision", "failure", "task")

# Ordered weakest -> strongest. Reports render these literally.
VERIFICATION_STATES = ("unverified", "claimed", "executed", "tested", "verified", "refuted")

CONFIDENCE_BANDS = ((0.85, "high"), (0.6, "medium"), (0.0, "low"))

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_FTS_SPECIAL = re.compile(r'["\'*^:()\-]')

# Retrieval quality depends on this list more than on the ranking function.
# Without it, a query like "summarise the README into notes.md" ORs the token
# "the", which matches almost every English record -- measured behaviour, not a
# theory: it injected six unrelated records into a context that needed one.
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "there", "here", "is", "are", "was", "were", "be", "been",
    "being", "am", "of", "to", "in", "on", "at", "by", "for", "with", "from",
    "as", "it", "its", "into", "onto", "over", "under", "about", "after",
    "before", "not", "no", "nor", "do", "does", "did", "done", "so", "such",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "just", "also", "more", "most", "other", "others", "some", "any", "all",
    "each", "every", "one", "two", "how", "what", "when", "where", "which",
    "who", "whom", "why", "you", "your", "yours", "we", "our", "ours", "they",
    "them", "their", "he", "she", "his", "her", "him", "i", "me", "my", "mine",
    "get", "got", "make", "made", "use", "used", "using", "need", "needs",
    "have", "has", "had", "very", "much", "many", "few", "own", "same", "out",
    "up", "down", "off", "again", "further", "only", "both", "because", "while",
    "during", "through", "between", "against", "across", "via", "per",
})


def significant_terms(text: str) -> list[str]:
    """Query terms that carry meaning: lowercased, 3+ characters, not stopwords.

    Deduplicated and order-preserving, so the caller can also ask "how many of
    these did this record actually contain?".
    """

    seen: list[str] = []
    for token in _PUNCT.sub(" ", (text or "").lower()).split():
        if len(token) < 3 or token in STOPWORDS or token.isdigit():
            continue
        if token not in seen:
            seen.append(token)
    return seen


def required_overlap(term_count: int) -> int:
    """Minimum number of *discriminating* terms a record must share.

    One. The interesting question is not "how many terms" but "which terms", and
    that is answered by :meth:`Memory._discriminating_terms`, which drops terms
    that appear in most records. A single rare term is strong evidence; a single
    common one is not, so it is removed from the query rather than counted here.
    """

    return 1 if term_count else 0


class ProtectedMemoryError(PermissionError):
    """Raised when the agent attempts to modify a protected memory record."""


@dataclasses.dataclass
class MemoryRecord:
    id: int
    kind: str
    scope: str
    key: str | None
    content: str
    detail: str | None
    tags: list[str]
    status: str
    confidence: float
    verification: str
    evidence: str | None
    source: str | None
    protected: bool
    created_at: str
    updated_at: str
    supersedes: int | None
    score: float = 0.0
    match_explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "scope": self.scope, "key": self.key,
            "content": self.content, "detail": self.detail, "tags": self.tags,
            "status": self.status, "confidence": self.confidence,
            "confidence_band": band(self.confidence), "verification": self.verification,
            "evidence": self.evidence, "source": self.source, "protected": self.protected,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "supersedes": self.supersedes, "score": round(self.score, 4),
            "match": self.match_explanation,
        }

    def render(self, *, full: bool = True) -> str:
        """Text form for the model's context. Deliberately explicit about trust."""

        head = (
            f"[mem:{self.id}] {self.kind}"
            + (f"/{self.key}" if self.key else "")
            + f" | confidence={band(self.confidence)}({self.confidence:.2f})"
            + f" | verification={self.verification}"
            + (f" | updated={self.updated_at[:10]}" if full else "")
        )
        body = f"{head}\n  {self.content}"
        if full and self.detail:
            body += f"\n  detail: {self.detail}"
        if full and self.evidence:
            body += f"\n  evidence: {self.evidence}"
        if full and self.source:
            body += f"\n  source: {self.source}"
        if full and self.tags:
            body += f"\n  tags: {', '.join(self.tags)}"
        return body


def band(confidence: float) -> str:
    for threshold, name in CONFIDENCE_BANDS:
        if confidence >= threshold:
            return name
    return "low"


def normalize(text: str) -> str:
    """Normalisation used only for duplicate detection."""

    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def fingerprint(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}:{normalize(text)}".encode("utf-8")).hexdigest()[:16]


def _fts_query(text: str) -> str:
    """Turn a natural-language query into a safe FTS5 MATCH expression.

    Tokens are sanitised (so operator characters in model input cannot produce a
    malformed query), stopwords are removed, and the final term gets prefix
    matching so partial words still retrieve. The caller filters the results by
    term overlap, so this deliberately stays recall-oriented.
    """

    tokens = significant_terms(_FTS_SPECIAL.sub(" ", text))
    if not tokens:
        # Every term was a stopword or too short: fall back to raw tokens so a
        # query like "the the" still searches for something rather than nothing.
        raw = [t for t in _FTS_SPECIAL.sub(" ", text).split() if len(t) > 1]
        tokens = raw
    if not tokens:
        return ""
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " OR ".join(quoted)


class Memory:
    """Persistent, searchable, provenance-carrying memory."""

    def __init__(self, store: Store):
        self.store = store

    # -- writes -----------------------------------------------------------
    def remember(
        self,
        kind: str,
        content: str,
        *,
        key: str | None = None,
        scope: str = "global",
        detail: str | None = None,
        tags: Sequence[str] = (),
        confidence: float = 0.5,
        verification: str = "unverified",
        evidence: str | None = None,
        source: str | None = None,
        protected: bool = False,
        task_id: str | None = None,
    ) -> tuple[int, str]:
        """Store a record. Returns (id, action) where action is one of
        'created', 'updated', 'superseded'.

        A keyed write to an existing active record supersedes it: the old row is
        retained with ``status='superseded'`` so history survives, and the new row
        points back at it via ``supersedes``. Contradictory records therefore do
        not silently accumulate.
        """

        if kind not in KINDS:
            raise ValueError(f"unknown memory kind {kind!r}; expected one of {', '.join(KINDS)}")
        if verification not in VERIFICATION_STATES:
            raise ValueError(
                f"unknown verification state {verification!r}; expected one of "
                f"{', '.join(VERIFICATION_STATES)}"
            )
        if not content.strip():
            raise ValueError("refusing to store an empty memory record")
        confidence = max(0.0, min(1.0, float(confidence)))
        tag_json = json.dumps(sorted({t.strip() for t in tags if t.strip()}))

        with self.store.transaction() as conn:
            existing = None
            if key:
                existing = conn.execute(
                    "SELECT * FROM memory WHERE kind=? AND scope=? AND key=? AND status='active'",
                    (kind, scope, key),
                ).fetchone()

            if existing is not None:
                if existing["protected"] and not protected:
                    raise ProtectedMemoryError(
                        f"mem:{existing['id']} is a protected {kind} record; the agent may not "
                        f"overwrite it. Propose the change and let a human apply it."
                    )
                if (
                    existing["content"].strip() == content.strip()
                    and abs(existing["confidence"] - confidence) < 1e-9
                    and existing["verification"] == verification
                ):
                    conn.execute(
                        "UPDATE memory SET updated_at=? WHERE id=?",
                        (now(), existing["id"]),
                    )
                    return int(existing["id"]), "unchanged"
                conn.execute(
                    "UPDATE memory SET status='superseded', updated_at=? WHERE id=?",
                    (now(), existing["id"]),
                )
                conn.execute("DELETE FROM memory_fts WHERE rowid=?", (existing["id"],))
                supersedes = int(existing["id"])
                action = "superseded"
            else:
                supersedes = None
                action = "created"

            cursor = conn.execute(
                "INSERT INTO memory(kind, scope, key, content, detail, tags, status, confidence, "
                "verification, evidence, source, protected, created_at, updated_at, supersedes) "
                "VALUES(?,?,?,?,?,?, 'active', ?,?,?,?,?,?,?,?)",
                (
                    kind, scope, key, content.strip(), detail, tag_json, confidence,
                    verification, evidence, source, 1 if protected else 0, now(), now(), supersedes,
                ),
            )
            memory_id = int(cursor.lastrowid or 0)
            conn.execute(
                "INSERT INTO memory_fts(rowid, content, tags) VALUES(?,?,?)",
                (memory_id, content.strip(), " ".join(json.loads(tag_json))),
            )

        self.store.append_event(
            "memory_write",
            {"id": memory_id, "kind": kind, "key": key, "action": action,
             "protected": protected, "verification": verification},
            task_id=task_id,
        )
        return memory_id, action

    def update(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        confidence: float | None = None,
        verification: str | None = None,
        evidence: str | None = None,
        detail: str | None = None,
        tags: Sequence[str] | None = None,
        task_id: str | None = None,
    ) -> MemoryRecord:
        """Correct an existing record in place, preserving history in the journal."""

        record = self.get(memory_id)
        if record is None:
            raise KeyError(f"no memory record with id {memory_id}")
        if record.protected:
            raise ProtectedMemoryError(
                f"mem:{memory_id} is protected; the agent may not modify it."
            )
        if verification is not None and verification not in VERIFICATION_STATES:
            raise ValueError(f"unknown verification state {verification!r}")

        fields = {
            "content": content if content is not None else record.content,
            "confidence": max(0.0, min(1.0, confidence)) if confidence is not None
            else record.confidence,
            "verification": verification or record.verification,
            "evidence": evidence if evidence is not None else record.evidence,
            "detail": detail if detail is not None else record.detail,
            "tags": json.dumps(sorted(tags)) if tags is not None else json.dumps(record.tags),
        }
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE memory SET content=?, confidence=?, verification=?, evidence=?, detail=?, "
                "tags=?, updated_at=? WHERE id=?",
                (fields["content"], fields["confidence"], fields["verification"],
                 fields["evidence"], fields["detail"], fields["tags"], now(), memory_id),
            )
            conn.execute("DELETE FROM memory_fts WHERE rowid=?", (memory_id,))
            conn.execute(
                "INSERT INTO memory_fts(rowid, content, tags) VALUES(?,?,?)",
                (memory_id, fields["content"], " ".join(json.loads(fields["tags"]))),
            )
        self.store.append_event(
            "memory_update",
            {"id": memory_id, "verification": fields["verification"],
             "confidence": fields["confidence"]},
            task_id=task_id,
        )
        updated = self.get(memory_id)
        assert updated is not None
        return updated

    def forget(self, memory_id: int, *, reason: str, task_id: str | None = None) -> MemoryRecord:
        """Retire a record. The row and its history remain; it stops being
        retrieved. Hard deletion is deliberately not exposed to the agent."""

        record = self.get(memory_id)
        if record is None:
            raise KeyError(f"no memory record with id {memory_id}")
        if record.protected:
            raise ProtectedMemoryError(
                f"mem:{memory_id} is protected and cannot be retired by the agent."
            )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE memory SET status='retired', updated_at=? WHERE id=?", (now(), memory_id)
            )
            conn.execute("DELETE FROM memory_fts WHERE rowid=?", (memory_id,))
        self.store.append_event(
            "memory_retire", {"id": memory_id, "reason": reason}, task_id=task_id
        )
        retired = self.get(memory_id)
        assert retired is not None
        return retired

    # -- reads ------------------------------------------------------------
    def get(self, memory_id: int) -> MemoryRecord | None:
        row = self.store.one("SELECT * FROM memory WHERE id=?", (memory_id,))
        return _row_to_record(row) if row else None

    def by_key(self, kind: str, key: str, *, scope: str = "global") -> MemoryRecord | None:
        row = self.store.one(
            "SELECT * FROM memory WHERE kind=? AND scope=? AND key=? AND status='active'",
            (kind, scope, key),
        )
        return _row_to_record(row) if row else None

    def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] | None = None,
        limit: int = 10,
        scope: str | None = None,
        include_retired: bool = False,
        min_confidence: float = 0.0,
    ) -> list[MemoryRecord]:
        """Ranked retrieval. BM25 over FTS5, then a light, explainable re-rank.

        The re-rank is intentionally simple and each record carries the reason it
        ranked where it did, so retrieval behaviour can be inspected rather than
        merely trusted.
        """

        terms = significant_terms(query)
        terms = self._discriminating_terms(terms)
        match = _fts_query(query) if not terms else _fts_query(" ".join(terms))
        if not terms and not _fts_query(query):
            match = ""
        need = required_overlap(len(terms))
        results: list[MemoryRecord] = []
        seen: set[int] = set()

        if match:
            sql = (
                "SELECT m.*, bm25(memory_fts) AS rank FROM memory_fts "
                "JOIN memory m ON m.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ?"
            )
            params: list[Any] = [match]
            if not include_retired:
                sql += " AND m.status='active'"
            if kinds:
                sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
                params.extend(kinds)
            if scope:
                sql += " AND m.scope=?"
                params.append(scope)
            sql += " ORDER BY rank LIMIT ?"
            params.append(max(limit * 4, 20))
            try:
                rows = self.store.query(sql, params)
            except sqlite3.OperationalError:
                # A MATCH expression the tokenizer still rejects: fall back to a
                # plain scan rather than returning nothing or raising at the caller.
                rows = []
            for row in rows:
                record = _row_to_record(row)
                overlap, matched = _overlap(record, terms)
                if overlap < need:
                    # A match on stopword-ish or single incidental terms is not a hit.
                    continue
                raw_rank = float(row["rank"] or 0.0)
                record.score = -raw_rank  # bm25 returns lower-is-better
                record.score += overlap / max(len(terms), 1)  # reward real term coverage
                record.match_explanation = (
                    f"fts5 bm25 match, {overlap}/{len(terms)} query terms"
                    + (f" ({', '.join(matched[:4])})" if matched else "")
                )
                results.append(record)
                seen.add(record.id)

        if len(results) < limit:
            # Substring fallback catches short tokens and code identifiers that
            # FTS tokenisation splits away, and keeps recall honest when a query
            # has no terms FTS can index.
            needle = query.strip()
            if len(needle) >= 3:
                sql = (
                    "SELECT * FROM memory WHERE (content LIKE ? OR IFNULL(detail,'') LIKE ? "
                    "OR IFNULL(key,'') LIKE ?)"
                )
                params = [f"%{needle}%"] * 3
                if not include_retired:
                    sql += " AND status='active'"
                if kinds:
                    sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                    params.extend(kinds)
                sql += " ORDER BY updated_at DESC LIMIT ?"
                params.append(max(limit * 2, 10))
                for row in self.store.query(sql, params):
                    record = _row_to_record(row)
                    if record.id in seen:
                        continue
                    overlap, matched = _overlap(record, terms)
                    if terms and overlap < need:
                        continue
                    record.score = 0.01 + (overlap / max(len(terms), 1))
                    record.match_explanation = (
                        f"substring fallback, {overlap}/{len(terms)} query terms"
                    )
                    results.append(record)
                    seen.add(record.id)

        for record in results:
            record.score += _trust_weight(record)
        results = [r for r in results if r.confidence >= min_confidence]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _discriminating_terms(self, terms: Sequence[str]) -> list[str]:
        """Drop query terms that appear in most records, because they carry no signal.

        This is the cheap version of weighting a query term by how rare it is: a
        term present in over half the active records cannot distinguish anything,
        so it is removed rather than allowed to pull in the whole corpus. It is a
        deliberate approximation -- substring-based document frequency, applied
        only once there are enough records for "most" to mean something - and it
        is recorded in each hit's explanation so retrieval can be inspected.
        """

        if not terms:
            return []
        total = self.store.one("SELECT COUNT(*) AS n FROM memory WHERE status='active'")
        total_active = int(total["n"]) if total else 0
        if total_active < 4:  # too few records for "ubiquitous" to mean anything
            return list(terms)
        kept: list[str] = []
        for term in terms:
            row = self.store.one(
                "SELECT COUNT(*) AS n FROM memory WHERE status='active' "
                "AND (LOWER(content) LIKE ? OR LOWER(IFNULL(detail,'')) LIKE ?)",
                (f"%{term}%", f"%{term}%"),
            )
            frequency = int(row["n"]) if row else 0
            if frequency * 2 <= total_active:
                kept.append(term)
        # If every term looked ubiquitous the corpus is dominated by one repeated
        # phrase; fall back to the original terms rather than searching nothing.
        return kept or list(terms)

    def recent(
        self, *, kinds: Sequence[str] | None = None, limit: int = 10, scope: str | None = None
    ) -> list[MemoryRecord]:
        sql = "SELECT * FROM memory WHERE status='active'"
        params: list[Any] = []
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [_row_to_record(row) for row in self.store.query(sql, params)]

    def protected(self) -> list[MemoryRecord]:
        return [
            _row_to_record(row)
            for row in self.store.query(
                "SELECT * FROM memory WHERE protected=1 AND status='active' ORDER BY id"
            )
        ]

    def history(self, memory_id: int) -> list[MemoryRecord]:
        """Every version of a keyed record, oldest first."""

        chain: list[MemoryRecord] = []
        current = self.get(memory_id)
        while current is not None:
            chain.append(current)
            current = self.get(current.supersedes) if current.supersedes else None
        chain.reverse()
        for record in chain:
            children = self.store.query(
                "SELECT id FROM memory WHERE supersedes=? ORDER BY id", (record.id,)
            )
            if children:
                for child in children:
                    kid = self.get(int(child["id"]))
                    if kid and kid.id not in {c.id for c in chain}:
                        chain.append(kid)
        return chain

    # -- quality ----------------------------------------------------------
    def find_duplicates(self, *, min_kind: str | None = None) -> list[dict[str, Any]]:
        """Report records whose normalised text is identical.

        A real, explainable duplicate check rather than an embedding-similarity
        guess: identical normalised text is defensible evidence, and the caller
        decides what to do about it.
        """

        groups: dict[str, list[MemoryRecord]] = {}
        for row in self.store.query(
            "SELECT * FROM memory WHERE status='active' ORDER BY id"
        ):
            record = _row_to_record(row)
            if min_kind and record.kind != min_kind:
                continue
            groups.setdefault(fingerprint(record.kind, record.content), []).append(record)

        duplicates = []
        for print_, records in groups.items():
            if len(records) > 1:
                duplicates.append(
                    {
                        "fingerprint": print_,
                        "kind": records[0].kind,
                        "count": len(records),
                        "ids": [r.id for r in records],
                        "contents": [r.content[:160] for r in records],
                        "suggested_action": (
                            "same kind: keep the highest-verified record and retire the rest"
                            if len({r.kind for r in records}) == 1
                            else "different kinds: confirm this is duplication before merging"
                        ),
                    }
                )
        return duplicates

    def consolidate_duplicates(self, *, task_id: str | None = None) -> dict[str, Any]:
        """Actual consolidation of exact duplicates.

        Keeps the record with the strongest verification/confidence, refreshes it
        with the newest timestamp, and retires the others with a reason. Returns
        what changed so it can be reviewed rather than taken on faith.
        """

        report = self.find_duplicates()
        retired: list[int] = []
        kept: list[int] = []
        for group in report:
            # Only consolidate when kinds agree; cross-kind merging is a judgement
            # call that should be made explicitly, with the evidence in view.
            records = [self.get(i) for i in group["ids"]]
            live = [r for r in records if r is not None]
            if len({r.kind for r in live}) > 1 or len({r.scope for r in live}) > 1:
                continue
            if any(r.protected for r in live):
                continue
            live.sort(
                key=lambda r: (VERIFICATION_STATES.index(r.verification), r.confidence, r.id),
                reverse=True,
            )
            winner, losers = live[0], live[1:]
            exact = {normalize(r.content) for r in live}
            if len(exact) > 1:
                continue
            kept.append(winner.id)
            for loser in losers:
                self.forget(loser.id, reason=f"duplicate of mem:{winner.id}", task_id=task_id)
                retired.append(loser.id)
            self.store.append_event(
                "memory_consolidated",
                {"kept": winner.id, "retired": [l.id for l in losers]},
                task_id=task_id,
            )
        return {"groups": len(report), "kept": kept, "retired": retired}

    def mark_stale(self, *, older_than_days: int, kinds: Sequence[str], task_id: str | None = None) -> int:
        """Downgrade verification on unverified records that have aged out.

        Prevents an old, never-checked assertion from being retrieved later with
        the same apparent standing as a fresh observation.
        """

        import datetime as dt

        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=older_than_days))
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        rows = self.store.query(
            "SELECT id FROM memory WHERE status='active' AND protected=0 "
            "AND verification IN ('unverified','claimed') AND updated_at < ?",
            (cutoff_iso,),
        )
        touched = 0
        for row in rows:
            record = self.get(int(row["id"]))
            if record is None or (kinds and record.kind not in kinds):
                continue
            self.update(
                record.id,
                verification="unverified",
                detail=(record.detail or "")
                + f"\n[stale] not re-checked since {record.updated_at[:10]}; treat with lower trust.",
                task_id=task_id,
            )
            touched += 1
        return touched

    # -- foundational bootstrap -------------------------------------------
    def seed_foundational(self, prompt_dir) -> dict[str, Any]:
        """Load the protected instruction files into memory as protected records.

        Runs on every start: if a protected prompt file changes, the memory record
        follows it, which keeps memory and the file it summarises from silently
        diverging. Protected records are written with the `protected` store flag,
        which the write path refuses to let the agent change.
        """

        import pathlib

        changes: dict[str, Any] = {}
        for name, key in (
            ("FOUNDATIONAL.md", "foundational_rules"),
            ("GROWTH.md", "growth_guardian_rules"),
            ("CONTRACT.md", "execution_contract"),
        ):
            path = pathlib.Path(prompt_dir) / name
            if not path.exists():
                changes[name] = "missing"
                continue
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            existing = self.by_key("foundational", key)
            if existing is not None and include_hash(existing.detail) == digest:
                changes[name] = "unchanged"
                continue
            if existing is not None and existing.protected:
                with self.store.transaction() as conn:
                    conn.execute(
                        "UPDATE memory SET content=?, detail=?, updated_at=? WHERE id=?",
                        (
                            f"{name} — protected operating instructions (full text in the system prompt)",
                            f"sha256[:12]={digest}; path={path}",
                            now(),
                            existing.id,
                        ),
                    )
                changes[name] = "refreshed (protected: content update only)"
            else:
                self.remember(
                    "foundational",
                    f"{name} — protected operating instructions (full text in the system prompt)",
                    key=key,
                    detail=f"sha256[:12]={digest}; path={path}",
                    tags=["rules", "protected", name.replace(".md", "").lower()],
                    confidence=1.0,
                    verification="verified",
                    evidence=f"file {path}",
                    source="livium/prompts/" + name,
                    protected=True,
                )
                changes[name] = "seeded"
        return changes


def include_hash(detail: str | None) -> str:
    if not detail:
        return ""
    for part in detail.replace(";", " ").split():
        if part.startswith("sha256[:12]="):
            return part.split("=", 1)[1]
    return ""


def _overlap(record: MemoryRecord, terms: Sequence[str]) -> tuple[int, list[str]]:
    """How many of the query's significant terms this record contains."""

    if not terms:
        return (0, [])
    haystack = " ".join(
        filter(None, [record.content, record.detail or "", record.key or "",
                      " ".join(record.tags)])
    ).lower()
    matched = [term for term in terms if term in haystack]
    return (len(matched), matched)


def _trust_weight(record: MemoryRecord) -> float:
    """Small, explainable adjustment on top of BM25.

    Verification strength and confidence matter, but only as a nudge: the lexical
    match still dominates, so a well-verified record about a different topic never
    outranks a direct hit.
    """

    verification_bonus = {
        "verified": 0.30, "tested": 0.22, "executed": 0.16,
        "claimed": 0.08, "unverified": 0.03, "refuted": -0.05,
    }[record.verification]
    kind_bonus = {
        "foundational": 0.25, "decision": 0.12, "failure": 0.10,
        "experience": 0.08, "capability": 0.06, "task": 0.05,
    }[record.kind]
    return verification_bonus + kind_bonus + (record.confidence * 0.1)


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    keys = row.keys()
    return MemoryRecord(
        id=int(row["id"]),
        kind=row["kind"],
        scope=row["scope"],
        key=row["key"],
        content=row["content"],
        detail=row["detail"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        status=row["status"],
        confidence=float(row["confidence"]),
        verification=row["verification"],
        evidence=row["evidence"],
        source=row["source"],
        protected=bool(row["protected"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        supersedes=row["supersedes"],
        score=float(row["score"]) if "score" in keys and row["score"] is not None else 0.0,
    )
