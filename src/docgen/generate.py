"""Generate stage: FACTS + line-numbered SOURCE -> validated `UnitDoc` via the LLM.

Small packages are documented in one call. Packages whose body is longer than
`chunk_threshold_lines` are documented per subprogram (each call sees the
package-level declarations plus that subprogram, with original line numbers),
then a synthesis call merges the per-subprogram docs without seeing SOURCE.

Every answer is validated with pydantic. On a validation failure the call is
retried once with the validation errors appended; a second failure fails the
unit. Validated answers are cached on disk, keyed by a hash of the complete
request (model, prompts, FACTS, SOURCE, schema), so re-runs cost nothing.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from docgen.extract import ExtractedPackage
from docgen.llm.base import LLMClient, LLMError
from docgen.models import PackageFacts, Subprogram, UnitDoc
from docgen.prompts import Prompts, number_lines, synthesis_message, unit_message

log = logging.getLogger(__name__)

Mode = Literal["whole", "chunked", "no_body"]


class GenerationError(RuntimeError):
    """A unit could not be documented. Carries the calls made so far for reporting."""

    def __init__(self, message: str, calls: list[LLMCallRecord]) -> None:
        super().__init__(message)
        self.calls = calls


@dataclass(frozen=True)
class LLMCallRecord:
    unit: str
    purpose: Literal["unit", "synthesis"]
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_s: float
    attempts: int  # HTTP attempts including rate-limit retries
    validation_retries: int
    cached: bool


@dataclass
class GenerationResult:
    doc: UnitDoc
    mode: Mode
    calls: list[LLMCallRecord] = field(default_factory=list)
    unit_docs: dict[str, UnitDoc] = field(default_factory=dict)  # chunked mode only


class ResponseCache:
    """Validated LLM answers on disk. Disabled when `directory` is None."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    @staticmethod
    def key(*, model: str, system: str, user: str) -> str:
        h = hashlib.sha256()
        for part in (model, system, user):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        if self.directory is None:
            return None
        path = self.directory / f"{key}.json"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def put(self, key: str, text: str) -> None:
        if self.directory is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{key}.json").write_text(text, encoding="utf-8")


def _validation_summary(exc: ValidationError, limit: int = 15) -> str:
    lines = []
    for err in exc.errors(include_url=False, include_input=False)[:limit]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"- {loc}: {err['msg']}")
    if exc.error_count() > limit:
        lines.append(f"- ... and {exc.error_count() - limit} more")
    return "\n".join(lines)


def call_validated(
    client: LLMClient,
    *,
    system: str,
    user: str,
    unit: str,
    purpose: Literal["unit", "synthesis"],
    cache: ResponseCache,
) -> tuple[UnitDoc, LLMCallRecord]:
    """One logical LLM call: cache lookup, call, validate, retry once on invalid output."""
    key = ResponseCache.key(model=client.model, system=system, user=user)
    if (hit := cache.get(key)) is not None:
        try:
            doc = UnitDoc.model_validate_json(hit)
            log.info("%s (%s): cache hit", unit, purpose)
            return doc, LLMCallRecord(unit, purpose, client.model, 0, 0, 0.0, 0, 0, True)
        except ValidationError:
            log.warning("%s (%s): ignoring invalid cache entry", unit, purpose)

    prompt = user
    input_tokens = output_tokens = 0
    latency = 0.0
    attempts = 0
    for validation_try in range(2):
        resp = client.generate_json(system=system, user=prompt, response_model=UnitDoc)
        input_tokens += resp.input_tokens or 0
        output_tokens += resp.output_tokens or 0
        latency += resp.latency_s
        attempts += resp.attempts
        try:
            doc = UnitDoc.model_validate_json(resp.text)
        except ValidationError as exc:
            summary = _validation_summary(exc)
            log.warning("%s (%s): invalid model output:\n%s", unit, purpose, summary)
            if validation_try == 1:
                record = LLMCallRecord(
                    unit, purpose, client.model, input_tokens, output_tokens, latency,
                    attempts, validation_try, False,
                )  # fmt: skip
                raise GenerationError(
                    f"{unit}: model output failed schema validation twice:\n{summary}", [record]
                ) from None
            prompt = (
                f"{user}\n<previous_answer_errors>\nYour previous answer did not match the "
                f"schema:\n{summary}\nReturn a corrected JSON object only.\n"
                "</previous_answer_errors>"
            )
            continue
        record = LLMCallRecord(
            unit, purpose, client.model, input_tokens, output_tokens, latency,
            attempts, validation_try, False,
        )  # fmt: skip
        log.info(
            "%s (%s): model=%s in=%s out=%s latency=%.1fs attempts=%d validation_retries=%d",
            unit, purpose, client.model, input_tokens, output_tokens, latency,
            attempts, validation_try,
        )  # fmt: skip
        cache.put(key, doc.model_dump_json())
        return doc, record
    raise AssertionError("unreachable")


# ------------------------------------------------------------------------ units


def _facts_for_llm(facts: PackageFacts) -> dict:
    # The hash and line counts are bookkeeping, not documentation input.
    return facts.model_dump(mode="json", exclude={"source_hash"})


def _facts_for_subprogram(facts: PackageFacts, sub: Subprogram) -> dict:
    return {
        "package": facts.name,
        "package_subprograms": facts.subprogram_names(),
        "globals": [g.model_dump(mode="json") for g in facts.globals],
        "subprograms": [sub.model_dump(mode="json")],
        "warnings": [w for w in facts.warnings if sub.name in w],
    }


def _chunk_source(lines: list[str], facts: PackageFacts, sub: Subprogram) -> str:
    """Package-level declarations (before the first subprogram) + this subprogram."""
    assert sub.start_line is not None and sub.end_line is not None
    starts = [s.start_line for s in facts.subprograms if s.start_line is not None]
    header_end = min(starts) - 1
    parts = []
    if header_end >= 1:
        parts.append(number_lines(lines, 1, header_end))
    if sub.start_line - 1 > header_end:
        parts.append(f"(lines {header_end + 1}-{sub.start_line - 1} omitted: other subprograms)")
    parts.append(number_lines(lines, sub.start_line, sub.end_line))
    if sub.end_line < len(lines):
        parts.append(f"(lines {sub.end_line + 1}-{len(lines)} omitted: other subprograms)")
    return "\n".join(parts)


def _no_body_doc() -> UnitDoc:
    return UnitDoc(
        source_complete=False,
        purpose="",
        procedure_notes=[],
        business_rules=[],
        main_flow=[],
        edge_cases=[],
        open_questions=[],
    )


def generate_package_doc(
    extracted: ExtractedPackage,
    client: LLMClient,
    prompts: Prompts,
    *,
    chunk_threshold_lines: int,
    cache: ResponseCache,
) -> GenerationResult:
    facts = extracted.facts
    body = extracted.body
    if body is None:
        return GenerationResult(_no_body_doc(), "no_body")
    lines = body.lines
    if lines and lines[-1] == "":
        lines = lines[:-1]
    calls: list[LLMCallRecord] = []

    def run(unit: str, purpose: Literal["unit", "synthesis"], system: str, user: str) -> UnitDoc:
        try:
            doc, rec = call_validated(
                client, system=system, user=user, unit=unit, purpose=purpose, cache=cache
            )
        except GenerationError as exc:
            raise GenerationError(str(exc), calls + exc.calls) from None
        except LLMError as exc:
            raise GenerationError(f"{unit}: {exc}", calls) from None
        calls.append(rec)
        return doc

    chunkable = [s for s in facts.subprograms if s.start_line is not None]
    if len(lines) <= chunk_threshold_lines or len(chunkable) < 2:
        user = unit_message(
            unit_name=facts.name,
            kind="package",
            facts=_facts_for_llm(facts),
            source=number_lines(lines, 1, len(lines)),
            model=UnitDoc,
        )
        return GenerationResult(run(facts.name, "unit", prompts.system, user), "whole", calls)

    unit_docs: dict[str, UnitDoc] = {}
    for sub in chunkable:
        unit = f"{facts.name}.{sub.name}"
        user = unit_message(
            unit_name=unit,
            kind=f"{sub.kind.lower()} of package {facts.name}",
            facts=_facts_for_subprogram(facts, sub),
            source=_chunk_source(lines, facts, sub),
            model=UnitDoc,
        )
        unit_docs[sub.name] = run(unit, "unit", prompts.system, user)

    user = synthesis_message(
        unit_name=facts.name,
        facts=_facts_for_llm(facts),
        unit_docs=[
            {"subprogram": name, **d.model_dump(mode="json")} for name, d in unit_docs.items()
        ],
        model=UnitDoc,
    )
    doc = run(facts.name, "synthesis", prompts.synthesis, user)
    return GenerationResult(doc, "chunked", calls, unit_docs)
