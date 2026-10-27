# Results

Measured results from real runs. Every number here comes from a run file in
`eval/results/` (regenerate the tables with `python -m docgen.eval compare`) or
from a logged run; nothing is estimated. How the eval works is described in
[DESIGN.md](DESIGN.md#evaluation-design).

> **Status (2026-10-27):** two complete runs (Gemini 3.8 Flash with prompt v1,
> Nemotron 3.5 Super with prompt v2) and one partial run (Gemini with prompt v2,
> 2 of 4 packages; stopped by overload and the daily quota). The Gemini v2
> completion and a "thinking level low" run are pending and will be added here.

## Summary

| Run | Model | Prompt | Coverage | Faithfulness violations | Evidence valid | Traps found | Hallucinations | Failed pkgs | LLM calls | Tokens in / out | LLM time |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Gemini v1 baseline | `gemini-3.8-flash` | v1 | 100% | 0 (originally 7) | 100% (35 rules) | **8/9** | 0 | 0/4 | 9 | 38,266 / 26,827 | 876 s |
| Gemini v2 (partial) | `gemini-3.8-flash` | v2 | 100% | 0 | 100% (16 rules) | 4/9 (4 of 4 scored) | 0 | 2/4 | 2 | 10,225 / 4,600 | 242 s |
| Nemotron v2 | `nvidia/nemotron-3.5-super-vl:free` | v2 | 100% | 0 (originally 4) | 100% (46 rules) | **7/9** | 0 | 0/4 | 9 | 36,405 / 75,059 | 1,091 s |

All runs: default temperature and thinking level, chunk threshold 150 lines (so
`ORD_PROC`, 154 lines, is documented per procedure: 5 calls plus 1 synthesis).
"Originally" is the score before a checker calibration (see below); both
numbers are kept in the run files.

## Traps

| Trap | What is seeded | Gemini v1 | Gemini v2 (partial) | Nemotron v2 |
|---|---|---|---|---|
| T1 hidden rule | 5% discount only in December, inside a CASE | found, evidence on the line | not run | found, evidence on the line |
| T2a dead procedure | private procedure never called | found | found | found |
| T2b unreachable branch | branch guarded by a constant `FALSE` | found | not run | found |
| T3 cross-package call | `SUBMIT_ORDER` creates the invoice via `BILL_CALC` | found | not run | **missed** (lost in synthesis) |
| T4 magic number | late fee only after 47 days | found | found | found |
| T5a swallowed exception | invoice errors ignored (`WHEN OTHERS THEN NULL`) | found | not run | found |
| T5b swallowed exception | logging errors ignored | found | not run | found |
| T6 cryptic naming | flag `xfl` with values `Y`/`P`/`N`, type `K` | **missed** | found | **missed** |
| T7 commented-out logic | disabled "KMO discount" block | found, not documented as a rule | found, not documented as a rule | found, not documented as a rule |

What the misses show:

- **T6, cryptic naming.** Gemini v1 named the flag a "cross-border flag" in a
  business rule and the procedure note: a plausible guess presented as fact,
  since nothing in the code says what `xfl` means. Prompt v2 added rule 9: never
  expand or guess abbreviations, codes or flag values; describe them literally
  and ask what they mean. With v2, Gemini asked. Nemotron with the same prompt
  followed half of the rule: it described the conditions with the literal names
  ("p_xfl is set to 'Y' when typ_cd is 'K' ..."), with no guess, but never asked
  what the flag and codes mean. The rule removed the guessing in both models; the
  question is not reliably asked.
- **T3, cross-package call (Nemotron).** The per-procedure document for
  `SUBMIT_ORDER` did mention `BILL_CALC.CREATE_INVOICE`, but the synthesis call
  that merges the per-procedure documents dropped it from both the flow and the
  procedure note. The eval records this separately (`found_in_unit_doc: true`),
  which is what made the cause visible. A fix (the synthesis prompt must keep
  calls to other packages) is proposed but not yet evaluated; FACTS still list
  the call on the page under "Dependencies".
- **No hallucinations.** No run documented the commented-out discount as an
  active rule, and every business rule in every run cited lines inside the
  procedure it named.

## Beyond the seeded traps

Nemotron reported a real defect that was not seeded. In
`plsql/customers/cust_mgmt.pkb`, `GET_CST_LVL` selects `SUM(...)` and `MIN(...)`
without `GROUP BY`. An aggregate query without `GROUP BY` always returns exactly
one row, so its `NO_DATA_FOUND` handler (return level `'B'`) can never run; for
a customer without recent history the function computes with `v_yrs` NULL
instead. The model raised it twice:

> get_cst_lvl returns 'B' when NO_DATA_FOUND is raised, but the aggregate SELECT
> without GROUP BY always returns a row, so this exception handler may never
> execute.

> Is the NO_DATA_FOUND exception handler in get_cst_lvl (lines 24-25)
> intentional, given that the aggregate query cannot raise NO_DATA_FOUND?

This is the kind of finding the approach is for: a question a reviewer can
answer, grounded in specific lines, about behaviour the original authors
probably did not intend.

## Comparing the two models

| | Gemini 3.8 Flash (v1) | Nemotron 3.5 Super (v2) |
|---|---:|---:|
| Business rules | 35 | 46 |
| Open questions | 20 | 48 (30 for `ORD_PROC` alone) |
| Output tokens | 26,827 | 75,059 (mostly reasoning) |
| Latency per call | 69–136 s | 46–181 s |
| Validation retries | 0 | 0 |

Both models produced schema-valid JSON on every call. Nemotron is more thorough
and more verbose: more rules and more than twice the open questions, which also
means more for a reviewer to read. Its output is dominated by reasoning tokens,
which makes it slow. The two runs also differ in prompt version, so this is a
comparison of two configurations, not a controlled model comparison; the pending
Gemini v2 run will make it one.

## Checker calibration

The automated checks were wrong twice, and both times in the same direction:
correct text flagged as unfaithful. Each fix was applied to all saved runs with
`docgen.eval rescore`, which keeps the original summary next to the new one.

| When | False positives | Fix |
|---|---|---|
| After Gemini v1 | 7 flags on Oracle built-in names (`NO_DATA_FOUND`, `TOO_MANY_ROWS`, `DDL`, ...) | Allow Oracle predefined exceptions and built-ins |
| After Nemotron v2 | 4 flags on SQL keywords (`COMMIT`, `ROLLBACK`, `GROUP` from "GROUP BY") | Allow SQL and PL/SQL keywords; reserved words cannot be object names, so this hides no invented identifier |

The ground truth was corrected once as well (v2): T6 also accepts "represent",
after a correct answer ("What do ... represent?") was scored as a miss.

After calibration, no faithfulness violation remains in any run. That is
expected for these models on this code, but it also means the check has not yet
caught a real invented identifier in a live run; it is covered by unit tests
only.

## Running on free tiers

Getting complete runs was the hardest practical part. What happened:

| Date | Provider | Event | Effect |
|---|---|---|---|
| 2026-10-24 | Gemini | Prompt v1: complete run, 9 calls | baseline |
| 2026-10-24 | Gemini | Prompt v2: `ORD_PROC` hit 503 "model overloaded" repeatedly, then `UTIL_LOG` hit the daily quota (20 requests per day for this model) | partial run |
| 2026-10-25 | Gemini | Resume attempts: a storm of 503s, then the daily quota | no progress; rejected 503 requests count against the daily quota |
| 2026-10-25 | OpenRouter | First free model chosen (`nemotron-3-super-120b-a12b:free`) had 0% uptime on its only provider and returned 404 | switched to `nemotron-3.5-super-vl:free`; the failed request did not count against the quota |
| 2026-10-25 | OpenRouter | Complete run, 9 calls; one 504 "upstream idle timeout" retried successfully | complete run |
| 2026-10-27 | Gemini | Resume: four 504 `DEADLINE_EXCEEDED` after ~180 s each (our request timeout), then 503 | stopped early, as designed |

What this changed in the code:

- **503 gets few, long retries** (at least 90 s apart, at most 2) instead of
  fast exponential backoff, because every rejected request costs daily quota.
- **A daily quota is never retried**, and once the model is unavailable the run
  stops calling it and marks the remaining packages "not attempted".
- **`--resume`** finishes an interrupted eval run. It reuses only packages that
  succeeded and whose source is unchanged, and refuses when the model, prompts or
  generation settings differ, so a resumed run stays one consistent measurement.
- **A second provider** behind the same interface, so a run is not blocked by one
  provider's bad day.

## Live publishing

First live publish on 2026-10-27, package `UTIL_LOG`, with the fake LLM (to test
publishing separately from generation) against a Confluence Cloud free site:

| Run | Result |
|---|---|
| Publish | page created, 2.2 s for the whole run |
| Same run again | skipped: the page's cache key matched, no model call |
| `--force` | same page updated as a new version, no duplicate |

## Pending

- Gemini 3.8 Flash with prompt v2, complete (resume the partial run).
- Gemini 3.8 Flash with prompt v2 and thinking level `low`: does less thinking
  cost trap detection, and how much latency does it save?
- The synthesis fix for T3, if evaluated.
- A first publish with real model output.
