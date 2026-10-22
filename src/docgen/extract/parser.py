"""Structural parsing of PL/SQL package specs and bodies.

This is deliberately *not* a full PL/SQL grammar. It recognises the handful of
structures documentation needs (package header, subprogram headers, parameter
lists, package-level declarations, and block boundaries) on comment- and
string-masked tokens. See docs/DESIGN.md for the trade-off versus ANTLR and the
Oracle data dictionary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from docgen.extract.lexer import Token, mask_source, tokenize
from docgen.models import GlobalDecl, Parameter, ParamMode, SubprogramKind


class ParseError(ValueError):
    """Raised when a file does not look like a PL/SQL package at all."""


@dataclass(frozen=True)
class Source:
    """One source file: original text, masked text and tokens."""

    path: str
    text: str
    masked: str
    tokens: list[Token]

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")

    @property
    def line_count(self) -> int:
        lines = self.lines
        return len(lines) - 1 if lines and lines[-1] == "" else len(lines)

    def fragment(self, start: int, end: int) -> str:
        """Original text between two offsets, whitespace collapsed."""
        return " ".join(self.text[start:end].split())


def normalise_text(raw: str) -> str:
    """Strip a BOM and normalise line endings so hashes are platform-independent."""
    return raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def load_source(path: Path, rel_path: str) -> Source:
    text = normalise_text(path.read_text(encoding="utf-8"))
    return source_from_text(text, rel_path)


def source_from_text(text: str, rel_path: str) -> Source:
    text = normalise_text(text)
    masked = mask_source(text)
    return Source(rel_path, text, masked, tokenize(masked))


# --------------------------------------------------------------------------- header


@dataclass(frozen=True)
class PackageHeader:
    name: str
    is_body: bool
    body_start: int  # token index of first token after IS/AS


def parse_package_header(src: Source) -> PackageHeader:
    toks = src.tokens
    for i, t in enumerate(toks):
        if t.upper != "PACKAGE":
            continue
        j = i + 1
        is_body = j < len(toks) and toks[j].upper == "BODY"
        if is_body:
            j += 1
        if j >= len(toks) or toks[j].kind != "ident":
            break
        name = toks[j].upper
        # schema-qualified: schema.pkg
        if j + 2 < len(toks) and toks[j + 1].text == "." and toks[j + 2].kind == "ident":
            j += 2
            name = toks[j].upper
        for k in range(j + 1, len(toks)):
            if toks[k].upper in ("IS", "AS"):
                return PackageHeader(name, is_body, k + 1)
        break
    raise ParseError(f"{src.path}: no 'PACKAGE [BODY] <name> IS|AS' header found")


# ------------------------------------------------------------- subprogram headers

_RETURN_STOP = {
    "IS",
    "AS",
    ";",
    "DETERMINISTIC",
    "PIPELINED",
    "PARALLEL_ENABLE",
    "RESULT_CACHE",
    "AUTHID",
    "ACCESSIBLE",
    "AGGREGATE",
}


@dataclass(frozen=True)
class SubprogramHeader:
    kind: SubprogramKind
    name: str
    name_index: int
    start_index: int  # PROCEDURE/FUNCTION token
    end_index: int  # IS/AS (definition) or ';' (declaration)
    parameters: list[Parameter]
    return_type: str | None
    is_definition: bool  # header ends in IS/AS (has a body) rather than ';'


def matching_paren(toks: list[Token], open_idx: int) -> int:
    depth = 0
    for k in range(open_idx, len(toks)):
        if toks[k].text == "(":
            depth += 1
        elif toks[k].text == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(toks) - 1


def _split_top_level(toks: list[Token], lo: int, hi: int) -> list[tuple[int, int]]:
    """Split token range [lo, hi) on commas at paren depth 0."""
    parts: list[tuple[int, int]] = []
    depth, start = 0, lo
    for k in range(lo, hi):
        t = toks[k].text
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        elif t == "," and depth == 0:
            parts.append((start, k))
            start = k + 1
    if start < hi:
        parts.append((start, hi))
    return parts


def parse_parameters(src: Source, open_idx: int, close_idx: int) -> list[Parameter]:
    toks = src.tokens
    params: list[Parameter] = []
    for lo, hi in _split_top_level(toks, open_idx + 1, close_idx):
        if lo >= hi:
            continue
        name = toks[lo].upper
        k = lo + 1
        mode: ParamMode = "IN"
        if k < hi and toks[k].upper == "IN":
            k += 1
            if k < hi and toks[k].upper == "OUT":
                mode = "IN OUT"
                k += 1
        elif k < hi and toks[k].upper == "OUT":
            mode = "OUT"
            k += 1
        if k < hi and toks[k].upper == "NOCOPY":
            k += 1
        type_end = hi
        default: str | None = None
        for d in range(k, hi):
            if toks[d].upper in ("DEFAULT", ":="):
                type_end = d
                if d + 1 < hi:
                    default = src.fragment(toks[d + 1].start, toks[hi - 1].end)
                break
        ptype = src.fragment(toks[k].start, toks[type_end - 1].end).upper() if k < type_end else ""
        params.append(Parameter(name=name, mode=mode, type=ptype, default=default))
    return params


def parse_subprogram_header(src: Source, i: int) -> SubprogramHeader:
    toks = src.tokens
    kind: SubprogramKind = "FUNCTION" if toks[i].upper == "FUNCTION" else "PROCEDURE"
    name_idx = i + 1
    name = toks[name_idx].upper
    j = name_idx + 1
    params: list[Parameter] = []
    if j < len(toks) and toks[j].text == "(":
        close = matching_paren(toks, j)
        params = parse_parameters(src, j, close)
        j = close + 1
    return_type: str | None = None
    if kind == "FUNCTION" and j < len(toks) and toks[j].upper == "RETURN":
        k = j + 1
        while k < len(toks) and toks[k].upper not in _RETURN_STOP:
            k += 1
        if k > j + 1:
            return_type = src.fragment(toks[j + 1].start, toks[k - 1].end).upper()
        j = k
    depth = 0
    while j < len(toks):
        t = toks[j]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif depth == 0 and t.upper in ("IS", "AS", ";"):
            break
        j += 1
    j = min(j, len(toks) - 1)
    return SubprogramHeader(
        kind=kind,
        name=name,
        name_index=name_idx,
        start_index=i,
        end_index=j,
        parameters=params,
        return_type=return_type,
        is_definition=toks[j].upper in ("IS", "AS"),
    )


# ------------------------------------------------------------------ block matching

_OPENERS = {"BEGIN", "CASE", "IF", "LOOP"}


@dataclass
class SubprogramSpan:
    header: SubprogramHeader
    end_index: int  # ';' terminating `END [name];`
    closed: bool
    label_indices: list[int] = field(default_factory=list)  # END <name> label tokens
    nested: list[SubprogramSpan] = field(default_factory=list)


def find_subprogram_end(src: Source, header: SubprogramHeader) -> SubprogramSpan:
    """Find the end of a subprogram *definition* by counting block constructs.

    Openers: BEGIN, CASE, IF, LOOP (unless directly preceded by END).
    Closers: END, END IF, END LOOP, END CASE. Nested subprograms in the
    declaration section are handled recursively. The subprogram ends when the
    depth returns to zero after its own BEGIN has been seen.
    """
    toks = src.tokens
    span = SubprogramSpan(header=header, end_index=len(toks) - 1, closed=False)
    depth = 0
    seen_begin = False
    j = header.end_index + 1
    while j < len(toks):
        t = toks[j]
        u = t.upper
        if not seen_begin and depth == 0 and u in ("PROCEDURE", "FUNCTION"):
            nested_hdr = parse_subprogram_header(src, j)
            if nested_hdr.is_definition:
                nested = find_subprogram_end(src, nested_hdr)
                span.nested.append(nested)
                j = nested.end_index + 1
            else:
                j = nested_hdr.end_index + 1
            continue
        if u == "END":
            nxt = toks[j + 1] if j + 1 < len(toks) else None
            if nxt is not None and nxt.upper in ("IF", "LOOP", "CASE"):
                j += 1
            depth -= 1
            if seen_begin and depth == 0:
                k = j + 1
                labels: list[int] = []
                while k < len(toks) and toks[k].text != ";":
                    if toks[k].kind == "ident":
                        labels.append(k)
                    k += 1
                span.end_index = min(k, len(toks) - 1)
                span.label_indices = labels
                span.closed = k < len(toks)
                return span
        elif u in _OPENERS:
            prev = toks[j - 1].upper if j > 0 else ""
            if prev != "END":
                depth += 1
                if u == "BEGIN" and depth == 1:
                    seen_begin = True
        j += 1
    return span


# --------------------------------------------------------------- package members

_SKIP_DECL = {"TYPE", "SUBTYPE", "CURSOR", "PRAGMA"}


@dataclass
class PackageStructure:
    header: PackageHeader
    declarations: list[SubprogramHeader] = field(default_factory=list)  # spec / forward
    definitions: list[SubprogramSpan] = field(default_factory=list)  # body
    globals: list[GlobalDecl] = field(default_factory=list)
    init_section_line: int | None = None
    warnings: list[str] = field(default_factory=list)


def _statement_end(toks: list[Token], i: int) -> int:
    depth = 0
    for k in range(i, len(toks)):
        if toks[k].text == "(":
            depth += 1
        elif toks[k].text == ")":
            depth -= 1
        elif toks[k].text == ";" and depth == 0:
            return k
    return len(toks) - 1


def _parse_global(src: Source, lo: int, hi: int, location: str) -> GlobalDecl | None:
    toks = src.tokens
    if hi - lo < 2 or toks[lo].upper in _SKIP_DECL or toks[lo + 1].upper == "EXCEPTION":
        return None
    k = lo + 1
    constant = toks[k].upper == "CONSTANT"
    if constant:
        k += 1
    type_end = hi
    value: str | None = None
    for d in range(k, hi):
        if toks[d].upper in ("DEFAULT", ":="):
            type_end = d
            if d + 1 < hi:
                value = src.fragment(toks[d + 1].start, toks[hi - 1].end)
            break
    if k >= type_end:
        return None
    gtype = src.fragment(toks[k].start, toks[type_end - 1].end).upper()
    return GlobalDecl(
        name=toks[lo].upper,
        type=gtype,
        constant=constant,
        value=value,
        line=toks[lo].line,
        location=location,  # type: ignore[arg-type]
    )


def parse_package(src: Source) -> PackageStructure:
    header = parse_package_header(src)
    struct = PackageStructure(header=header)
    toks = src.tokens
    location = "body" if header.is_body else "spec"
    i = header.body_start
    while i < len(toks):
        t = toks[i]
        u = t.upper
        if u == "END":
            break
        if u == "BEGIN" and header.is_body:
            struct.init_section_line = t.line
            break
        if u in ("PROCEDURE", "FUNCTION"):
            hdr = parse_subprogram_header(src, i)
            if hdr.is_definition:
                if not header.is_body:
                    struct.warnings.append(
                        f"{src.path}:{t.line}: subprogram body found in package spec"
                    )
                span = find_subprogram_end(src, hdr)
                if not span.closed:
                    struct.warnings.append(
                        f"{src.path}:{t.line}: could not find end of {hdr.name}; "
                        "source may be truncated or use unsupported syntax"
                    )
                struct.definitions.append(span)
                i = span.end_index + 1
            else:
                struct.declarations.append(hdr)
                i = hdr.end_index + 1
            continue
        end = _statement_end(toks, i)
        decl = _parse_global(src, i, end, location)
        if decl is not None:
            struct.globals.append(decl)
        i = end + 1
    return struct


_HEADER_RE = re.compile(
    r"\bPACKAGE\s+(BODY\s+)?(?:\"?\w+\"?\s*\.\s*)?\"?([A-Za-z][\w$#]*)\"?", re.IGNORECASE
)


def quick_package_name(path: Path) -> str | None:
    """Cheap package-name lookup used to build the set of known repo packages."""
    try:
        masked = mask_source(normalise_text(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return None
    m = _HEADER_RE.search(masked)
    return m.group(2).upper() if m else None
