"""Token-level analysis of a subprogram: tables, calls, sequences, errors.

Works on masked tokens, so SQL keywords inside comments or string literals
(including dynamic SQL text) are never matched. Dynamic SQL is flagged instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docgen.extract.lexer import Token
from docgen.extract.parser import Source, matching_paren
from docgen.models import Call, RaisedError, TableAccess

# Tokens that can never be a table name or a table alias.
_RESERVED = {
    "SELECT",
    "FROM",
    "WHERE",
    "GROUP",
    "ORDER",
    "HAVING",
    "CONNECT",
    "START",
    "UNION",
    "INTERSECT",
    "MINUS",
    "EXCEPT",
    "FOR",
    "INTO",
    "ON",
    "USING",
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "CROSS",
    "NATURAL",
    "OUTER",
    "SET",
    "VALUES",
    "RETURNING",
    "RETURN",
    "WHEN",
    "THEN",
    "LOOP",
    "MODEL",
    "PIVOT",
    "UNPIVOT",
    "SAMPLE",
    "FETCH",
    "OFFSET",
    "LOG",
    "BULK",
    "LIMIT",
    "PARTITION",
    "AS",
    "WITH",
    "AND",
    "OR",
    "NOT",
    "IS",
    "IN",
    "BY",
    "END",
    "BEGIN",
    "TABLE",
    "LATERAL",
    "APPLY",
    "NOWAIT",
    "WAIT",
    "SKIP",
    "OF",
}
# Functions whose argument list may legally contain FROM (not a table reference).
_FROM_FUNCTIONS = {"EXTRACT", "TRIM", "SUBSTRING", "OVERLAY"}
_PSEUDO_TABLES = {"DUAL"}
_BUILTIN_PKG_RE = re.compile(r"^(DBMS_\w+|UTL_\w+|APEX_\w+|OWA_\w+|CTX_\w+|HTP|HTF|SYS)$")
_OP_ORDER: dict[str, int] = {"SELECT": 0, "INSERT": 1, "UPDATE": 2, "DELETE": 3, "MERGE": 4}


@dataclass
class _TableAcc:
    ops: set[str] = field(default_factory=set)
    aliases: set[str] = field(default_factory=set)
    lines: set[int] = field(default_factory=set)


@dataclass
class Analysis:
    tables: list[TableAccess]
    calls: list[Call]
    internal_refs: list[str]
    sequences: list[str]
    raises: list[RaisedError]
    dynamic_sql: bool


class _TableCollector:
    def __init__(self) -> None:
        self.acc: dict[str, _TableAcc] = {}

    def add(self, name: str, op: str, line: int, alias: str | None) -> None:
        if name in _PSEUDO_TABLES:
            return
        a = self.acc.setdefault(name, _TableAcc())
        a.ops.add(op)
        a.lines.add(line)
        if alias:
            a.aliases.add(alias)

    def result(self) -> list[TableAccess]:
        return [
            TableAccess(
                table=name,
                operations=sorted(a.ops, key=_OP_ORDER.__getitem__),  # type: ignore[arg-type]
                aliases=sorted(a.aliases),
                lines=sorted(a.lines),
            )
            for name, a in sorted(self.acc.items())
        ]


def _read_object(toks: list[Token], i: int, hi: int) -> tuple[str, int] | None:
    """Read `[schema.]name[@dblink]` at i. Returns (UPPER name, next index)."""
    if i >= hi or toks[i].kind != "ident" or toks[i].upper in _RESERVED:
        return None
    name = toks[i].upper
    j = i + 1
    if j + 1 < hi and toks[j].text == "." and toks[j + 1].kind == "ident":
        name = f"{name}.{toks[j + 1].upper}"
        j += 2
    if j < hi and toks[j].text == "@":
        j += 1
        while j < hi and (toks[j].kind == "ident" or toks[j].text == "."):
            j += 1
    return name, j


def _read_alias(toks: list[Token], j: int, hi: int) -> tuple[str | None, int]:
    if j < hi and toks[j].kind == "ident" and toks[j].upper not in _RESERVED:
        return toks[j].upper, j + 1
    return None, j


def _from_list(toks: list[Token], i: int, hi: int, out: _TableCollector) -> None:
    """Parse a comma-separated FROM list starting at token i (after FROM)."""
    j = i
    while j < hi:
        if toks[j].text == "(":
            j = matching_paren(toks, j) + 1  # inline view: inner FROM is scanned separately
            _, j = _read_alias(toks, j, hi)
        else:
            line = toks[j].line
            obj = _read_object(toks, j, hi)
            if obj is None:
                return
            name, j = obj
            alias, j = _read_alias(toks, j, hi)
            out.add(name, "SELECT", line, alias)
        if j < hi and toks[j].text == ",":
            j += 1
            continue
        return


def analyze_range(
    src: Source,
    lo: int,
    hi: int,
    *,
    own_package: str,
    known_packages: set[str],
    own_subprograms: set[str],
    excluded_indices: set[int],
) -> Analysis:
    """Analyse tokens [lo, hi] of one subprogram."""
    toks = src.tokens
    hi = min(hi + 1, len(toks))
    tables = _TableCollector()
    calls: dict[tuple[str, str], tuple[str, set[int]]] = {}
    internal: list[str] = []
    sequences: set[str] = set()
    raises: list[RaisedError] = []
    dynamic = False
    consumed_from: set[int] = set()
    paren_owner: list[str] = []

    for i in range(lo, hi):
        t = toks[i]
        u = t.upper
        prev = toks[i - 1] if i > 0 else None
        nxt = toks[i + 1] if i + 1 < hi else None

        if t.text == "(":
            paren_owner.append(prev.upper if prev is not None and prev.kind == "ident" else "")
            continue
        if t.text == ")":
            if paren_owner:
                paren_owner.pop()
            continue
        if t.kind != "ident":
            continue

        if u == "FROM" and i not in consumed_from:
            if not (paren_owner and paren_owner[-1] in _FROM_FUNCTIONS):
                _from_list(toks, i + 1, hi, tables)
        elif u == "JOIN":
            obj = _read_object(toks, i + 1, hi)
            if obj:
                alias, _ = _read_alias(toks, obj[1], hi)
                tables.add(obj[0], "SELECT", t.line, alias)
        elif u == "INSERT" and nxt is not None and nxt.upper == "INTO":
            obj = _read_object(toks, i + 2, hi)
            if obj:
                tables.add(obj[0], "INSERT", t.line, None)
        elif u == "UPDATE":
            is_lock = prev is not None and prev.upper == "FOR"  # SELECT ... FOR UPDATE
            is_merge = nxt is not None and nxt.upper == "SET"  # MERGE ... UPDATE SET
            if is_lock or is_merge:
                continue
            obj = _read_object(toks, i + 1, hi)
            if obj:
                alias, _ = _read_alias(toks, obj[1], hi)
                tables.add(obj[0], "UPDATE", t.line, alias)
        elif u == "DELETE":
            if nxt is not None and nxt.upper == "WHERE":
                continue  # MERGE ... DELETE WHERE
            j = i + 1
            if j < hi and toks[j].upper == "FROM":
                consumed_from.add(j)
                j += 1
            obj = _read_object(toks, j, hi)
            if obj:
                alias, _ = _read_alias(toks, obj[1], hi)
                tables.add(obj[0], "DELETE", t.line, alias)
        elif u == "MERGE" and nxt is not None and nxt.upper == "INTO":
            obj = _read_object(toks, i + 2, hi)
            if obj:
                alias, j = _read_alias(toks, obj[1], hi)
                tables.add(obj[0], "MERGE", t.line, alias)
                if j < hi and toks[j].upper == "USING":
                    src_obj = _read_object(toks, j + 1, hi)
                    if src_obj:
                        s_alias, _ = _read_alias(toks, src_obj[1], hi)
                        tables.add(src_obj[0], "SELECT", toks[j].line, s_alias)
        elif u == "EXECUTE" and nxt is not None and nxt.upper == "IMMEDIATE":
            dynamic = True
        elif u == "RAISE_APPLICATION_ERROR" and nxt is not None and nxt.text == "(":
            raises.append(_parse_raise(src, i + 1))

        # Qualified references: pkg.member / seq.NEXTVAL
        if nxt is not None and nxt.text == "." and i + 2 < hi and toks[i + 2].kind == "ident":
            member = toks[i + 2].upper
            if prev is not None and prev.text == ".":
                pass  # middle of a.b.c; handled when the scan reaches b
            elif member in ("NEXTVAL", "CURRVAL"):
                sequences.add(u)
            elif u == own_package and member in own_subprograms:
                internal.append(member)
            elif u in known_packages and u != own_package:
                calls.setdefault((u, member), ("repo", set()))[1].add(t.line)
            elif _BUILTIN_PKG_RE.match(u):
                if u == "DBMS_SQL":
                    dynamic = True
                calls.setdefault((u, member), ("builtin", set()))[1].add(t.line)
            continue

        # Unqualified reference to a sibling subprogram.
        if (
            u in own_subprograms
            and i not in excluded_indices
            and not (prev is not None and prev.text == ".")
        ):
            internal.append(u)

    return Analysis(
        tables=tables.result(),
        calls=[
            Call(package=p, member=m, kind=kind, lines=sorted(lines))  # type: ignore[arg-type]
            for (p, m), (kind, lines) in sorted(calls.items())
        ],
        internal_refs=sorted(set(internal)),
        sequences=sorted(sequences),
        raises=raises,
        dynamic_sql=dynamic,
    )


def _parse_raise(src: Source, open_idx: int) -> RaisedError:
    toks = src.tokens
    close = matching_paren(toks, open_idx)
    code = 0
    j = open_idx + 1
    sign = -1 if j < close and toks[j].text == "-" else 1
    if sign == -1:
        j += 1
    if j < close and toks[j].kind == "number":
        code = sign * int(float(toks[j].text))
    message = ""
    depth = 0
    msg_start: int | None = None
    msg_end = close
    for k in range(j, close):
        text = toks[k].text
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif text == "," and depth == 0:
            if msg_start is None:
                msg_start = k + 1
            else:
                msg_end = k  # optional third argument (keep_errors)
                break
    if msg_start is not None and msg_start < msg_end:
        message = src.fragment(toks[msg_start].start, toks[msg_end - 1].end)
    return RaisedError(code=code, message=message, line=toks[open_idx - 1].line)
