"""Eval result models, persisted as JSON in eval/results/ so runs can be compared."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from docgen.models import UnitDoc


class CallStats(BaseModel):
    unit: str
    purpose: str
    input_tokens: int | None
    output_tokens: int | None
    latency_s: float
    attempts: int
    validation_retries: int
    cached: bool


class FindingRecord(BaseModel):
    check: str
    action: str
    location: str
    message: str


class PackageMetrics(BaseModel):
    subprograms: int = 0
    subprograms_with_note: int = 0
    tables: int = 0
    tables_on_page: int = 0
    rules: int = 0  # as returned by the model, before checks dropped anything
    rules_valid_evidence: int = 0
    faithfulness_violations: int = 0
    dropped_items: int = 0
    open_questions: int = 0


class PackageEval(BaseModel):
    name: str
    ok: bool
    error: str | None = None
    mode: str | None = None
    source_hash: str | None = None
    metrics: PackageMetrics = Field(default_factory=PackageMetrics)
    findings: list[FindingRecord] = Field(default_factory=list)
    calls: list[CallStats] = Field(default_factory=list)
    raw_doc: UnitDoc | None = None  # model output (validated) before checks
    doc: UnitDoc | None = None  # after checks: what would be published
    unit_docs: dict[str, UnitDoc] = Field(default_factory=dict)  # chunked mode
    # Failed because the model was overloaded / out of quota (not a quality failure).
    provider_unavailable: bool = False


class TrapResult(BaseModel):
    id: str
    type: str
    package: str
    procedure: str
    found: bool
    found_in: str | None = None  # doc field where it matched
    matched_text: str | None = None
    # business_rules traps: does the matching rule's evidence cover the anchor line?
    evidence_hit: bool | None = None
    # forbid_in_rules matched: commented-out/forbidden content presented as a rule
    hallucinated: bool = False
    # chunked packages: was it found in a per-procedure doc (diagnoses synthesis loss)?
    found_in_unit_doc: bool | None = None
    package_failed: bool = False


class Summary(BaseModel):
    packages: int
    failed: int
    coverage_pct: float
    notes_coverage_pct: float
    tables_coverage_pct: float
    faithfulness_violations: int
    rules: int
    evidence_valid_pct: float
    traps_found: int
    traps_total: int
    trap_evidence_hits: int
    trap_evidence_total: int
    hallucinations: int
    llm_calls: int
    validation_retries: int
    tokens_in: int
    tokens_out: int
    latency_s: float


class EvalRun(BaseModel):
    run_id: str
    created_at: datetime
    # Completion of a run interrupted by e.g. the daily quota (`run --resume`).
    resumed_at: list[datetime] = Field(default_factory=list)
    label: str | None = None
    commit: str
    model: str
    prompt_version: str
    prompt_fingerprint: str
    settings: dict[str, str | int | float | bool | None]
    ground_truth_version: int
    summary: Summary
    # Set when the run was re-scored with newer checks (no new LLM calls).
    original_summary: Summary | None = None
    rescored_at: datetime | None = None
    traps: list[TrapResult]
    packages: list[PackageEval]


def save_run(run: EvalRun, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run.run_id}.json"
    path.write_text(run.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return path


def load_runs(paths: list[Path]) -> list[EvalRun]:
    runs = [EvalRun.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    return sorted(runs, key=lambda r: r.created_at)
