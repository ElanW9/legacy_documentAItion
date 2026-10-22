"""Extract stage entry point: .pks/.pkb pair -> PackageFacts + sources."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from docgen.extract.analyze import analyze_range
from docgen.extract.parser import (
    PackageStructure,
    Source,
    SubprogramHeader,
    SubprogramSpan,
    load_source,
    parse_package,
    quick_package_name,
)
from docgen.models import PackageFacts, Subprogram, TableAccess

SPEC_SUFFIX = ".pks"
BODY_SUFFIX = ".pkb"
PLSQL_SUFFIXES = (SPEC_SUFFIX, BODY_SUFFIX)


@dataclass(frozen=True)
class PackageFiles:
    """A spec/body pair sharing directory and file stem. Either may be absent."""

    spec: Path | None
    body: Path | None

    @property
    def label(self) -> str:
        p = self.body or self.spec
        assert p is not None
        return str(p.with_suffix(""))


@dataclass(frozen=True)
class ExtractedPackage:
    facts: PackageFacts
    spec: Source | None
    body: Source | None


def source_hash(spec_text: str, body_text: str) -> str:
    h = hashlib.sha256()
    h.update(b"spec\0")
    h.update(spec_text.encode("utf-8"))
    h.update(b"\0body\0")
    h.update(body_text.encode("utf-8"))
    return h.hexdigest()


def pair_package_files(paths: Iterable[Path]) -> list[PackageFiles]:
    """Group changed paths into spec/body pairs, pulling in the unchanged sibling.

    Pairs where neither file exists on disk (both deleted) are dropped.
    """
    stems: dict[Path, None] = {}
    for p in paths:
        if p.suffix.lower() in PLSQL_SUFFIXES:
            stems.setdefault(p.with_suffix(""), None)
    pairs: list[PackageFiles] = []
    for stem in stems:
        spec = stem.with_suffix(SPEC_SUFFIX)
        body = stem.with_suffix(BODY_SUFFIX)
        pair = PackageFiles(spec if spec.is_file() else None, body if body.is_file() else None)
        if pair.spec or pair.body:
            pairs.append(pair)
    return sorted(pairs, key=lambda pf: pf.label)


def find_all_packages(root: Path) -> list[PackageFiles]:
    return pair_package_files(p for s in PLSQL_SUFFIXES for p in root.rglob(f"*{s}"))


def known_package_names(root: Path) -> set[str]:
    names = {quick_package_name(p) for s in PLSQL_SUFFIXES for p in root.rglob(f"*{s}")}
    names.discard(None)
    return names  # type: ignore[return-value]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _excluded_indices(spans: list[SubprogramSpan]) -> set[int]:
    """Token indices that name a subprogram (header name, END label): not calls."""
    out: set[int] = set()
    for span in spans:
        out.add(span.header.name_index)
        out.update(span.label_indices)
        out.update(_excluded_indices(span.nested))
    return out


def _merge_tables(groups: Iterable[list[TableAccess]]) -> list[TableAccess]:
    order = {"SELECT": 0, "INSERT": 1, "UPDATE": 2, "DELETE": 3, "MERGE": 4}
    merged: dict[str, tuple[set[str], set[str], set[int]]] = {}
    for group in groups:
        for t in group:
            ops, aliases, lines = merged.setdefault(t.table, (set(), set(), set()))
            ops.update(t.operations)
            aliases.update(t.aliases)
            lines.update(t.lines)
    return [
        TableAccess(
            table=name,
            operations=sorted(ops, key=order.__getitem__),  # type: ignore[arg-type]
            aliases=sorted(aliases),
            lines=sorted(lines),
        )
        for name, (ops, aliases, lines) in sorted(merged.items())
    ]


def extract_package(
    files: PackageFiles, *, root: Path, known_packages: set[str] | None = None
) -> ExtractedPackage:
    spec = load_source(files.spec, _rel(files.spec, root)) if files.spec else None
    body = load_source(files.body, _rel(files.body, root)) if files.body else None
    return extract_from_sources(spec, body, known_packages=known_packages or set())


def extract_from_sources(
    spec: Source | None, body: Source | None, *, known_packages: set[str]
) -> ExtractedPackage:
    if spec is None and body is None:
        raise ValueError("at least one of spec/body is required")
    warnings: list[str] = []
    spec_struct: PackageStructure | None = parse_package(spec) if spec else None
    body_struct: PackageStructure | None = parse_package(body) if body else None

    name = (spec_struct or body_struct).header.name  # type: ignore[union-attr]
    if spec_struct and body_struct and spec_struct.header.name != body_struct.header.name:
        warnings.append(
            f"spec declares package {spec_struct.header.name} but body declares "
            f"{body_struct.header.name}"
        )
    if spec is None:
        warnings.append("no package spec (.pks) found; public interface is unknown")
    if body is None:
        warnings.append("no package body (.pkb) found; behaviour is undocumented")
    for struct in (spec_struct, body_struct):
        if struct:
            warnings.extend(struct.warnings)
    if body_struct and body_struct.init_section_line:
        warnings.append(
            f"package body has an initialisation section at line "
            f"{body_struct.init_section_line} (runs once per session)"
        )

    spec_decls: dict[str, list[SubprogramHeader]] = {}
    for d in spec_struct.declarations if spec_struct else []:
        spec_decls.setdefault(d.name, []).append(d)
    spec_seen: dict[str, int] = {}

    definitions = body_struct.definitions if body_struct else []
    own_names = {d.header.name for d in definitions} | set(spec_decls)
    excluded = _excluded_indices(definitions)
    known = {k.upper() for k in known_packages}

    drafts: list[tuple[Subprogram, list[str]]] = []
    for span in definitions:
        hdr = span.header
        assert body is not None
        analysis = analyze_range(
            body,
            hdr.start_index,
            span.end_index,
            own_package=name,
            known_packages=known,
            own_subprograms=own_names,
            excluded_indices=excluded,
        )
        idx = spec_seen.get(hdr.name, 0)
        decls = spec_decls.get(hdr.name, [])
        spec_hdr = decls[idx] if idx < len(decls) else None
        spec_seen[hdr.name] = idx + 1
        sub = Subprogram(
            name=hdr.name,
            kind=hdr.kind,
            visibility="public" if spec_hdr else "private",
            parameters=hdr.parameters,
            return_type=hdr.return_type,
            spec_line=spec.tokens[spec_hdr.start_index].line if spec_hdr and spec else None,
            start_line=body.tokens[hdr.start_index].line,
            end_line=body.tokens[span.end_index].line,
            tables=analysis.tables,
            calls=analysis.calls,
            internal_calls=[r for r in analysis.internal_refs if r != hdr.name],
            sequences=analysis.sequences,
            raises=analysis.raises,
            dynamic_sql=analysis.dynamic_sql,
        )
        drafts.append((sub, sub.internal_calls))
        if analysis.dynamic_sql:
            warnings.append(
                f"{hdr.name} uses dynamic SQL; tables accessed through it are not listed"
            )

    # Spec declarations without a body implementation.
    for dname, decls in spec_decls.items():
        for extra in decls[spec_seen.get(dname, 0) :]:
            assert spec is not None
            drafts.append(
                (
                    Subprogram(
                        name=extra.name,
                        kind=extra.kind,
                        visibility="public",
                        parameters=extra.parameters,
                        return_type=extra.return_type,
                        spec_line=spec.tokens[extra.start_index].line,
                    ),
                    [],
                )
            )
            if body is not None:
                warnings.append(f"{extra.name} is declared in the spec but has no body")

    referenced_by: dict[str, set[str]] = {}
    for sub, refs in drafts:
        for r in refs:
            referenced_by.setdefault(r, set()).add(sub.name)

    subprograms: list[Subprogram] = []
    for sub, _ in drafts:
        refs = sorted(referenced_by.get(sub.name, set()))
        subprograms.append(sub.model_copy(update={"referenced_by": refs}))
        if sub.visibility == "private" and not refs:
            warnings.append(
                f"{sub.name} is private and never called within the package (possible dead code)"
            )

    deps = sorted({c.package for s in subprograms for c in s.calls})
    facts = PackageFacts(
        name=name,
        spec_path=spec.path if spec else None,
        body_path=body.path if body else None,
        source_hash=source_hash(spec.text if spec else "", body.text if body else ""),
        spec_line_count=spec.line_count if spec else 0,
        body_line_count=body.line_count if body else 0,
        globals=[
            *(spec_struct.globals if spec_struct else []),
            *(body_struct.globals if body_struct else []),
        ],
        subprograms=subprograms,
        tables=_merge_tables(s.tables for s in subprograms),
        dependencies=deps,
        warnings=warnings,
    )
    return ExtractedPackage(facts=facts, spec=spec, body=body)
