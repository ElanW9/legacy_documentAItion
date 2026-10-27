# legacy_documentAItion

AI-generated documentation for legacy PL/SQL, published to Confluence and kept
in sync with the code.

A proof of concept on a small, fake legacy codebase (`plsql/`, four packages).
For every package it produces one Confluence page that a functional analyst can
read, and it regenerates only the pages whose code changed on each push to `main`.

The documentation has to be:

- **grounded in the code:** every business rule cites the source lines that
  implement it, and automated checks drop or flag what cannot be traced back;
- **in sync with the code:** pages are regenerated from CI when the code changes,
  and each page records the commit, model and prompt version it was made from;
- **honest about what it does not know:** dead code, magic numbers, swallowed
  errors and cryptic names become open questions instead of confident guesses.

How well it does that is measured, not assumed: an eval harness scores the
output against traps seeded in the code. See [docs/RESULTS.md](docs/RESULTS.md)
for the numbers and [docs/DESIGN.md](docs/DESIGN.md) for the design.

> **Data notice.** The LLMs used here run on free tiers (Gemini API free tier,
> OpenRouter free models). Inputs sent to free tiers may be used by the provider
> to improve its models. That is acceptable here only because the PL/SQL in this
> repository is fake. Do not point this setup at real proprietary code; see
> "Path to production" in [docs/DESIGN.md](docs/DESIGN.md).

## How it works

```
plsql/*.pks, *.pkb
  -> extract   deterministic static analysis: signatures, tables, calls, errors (FACTS)
  -> generate  LLM: FACTS + line-numbered source in, schema-validated JSON out
  -> check     deterministic: unknown identifiers, evidence lines, coverage
  -> render    deterministic: Confluence storage format (the LLM never writes markup)
  -> publish   Confluence Cloud REST API, or a dry run to local files
```

Each page separates what comes from static analysis (authoritative) from what the
model wrote (marked "AI-generated, needs review"), and ends with the results of
the automated checks.

## Repository layout

| Path | Contents |
|---|---|
| `plsql/` | The fake legacy codebase: 4 packages with seeded traps |
| `src/docgen/` | The generator (`extract/`, `generate.py`, `checks.py`, `render.py`, `publish/`, `pipeline.py`) |
| `src/docgen/prompts/` | System and synthesis prompts, versioned (`VERSION`) |
| `src/docgen/llm/` | LLM clients behind one interface: Gemini, OpenRouter, and a fake for offline runs |
| `src/docgen/eval/` | Eval harness: run, resume, rescore, compare |
| `eval/ground_truth.yaml` | The seeded traps and how each is detected |
| `eval/results/` | Saved eval runs (JSON), the source for docs/RESULTS.md |
| `scripts/select-packages.sh` | CI: decides which packages a run processes |
| `.github/workflows/` | `test` and `generate-docs` workflows |
| `tests/` | Offline tests (no network, no credentials) |

## Setup

Requires Python 3.12 or newer and git.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows (Git Bash): source .venv/Scripts/activate
python -m pip install -e ".[dev]"
pre-commit install                 # gitleaks + ruff before every commit
cp .env.example .env               # then fill in what you need (see below)
```

Dependencies are pinned in `pyproject.toml`.

## Configuration

Settings come from environment variables; locally they can also come from `.env`
(an environment variable wins over `.env`). Empty values count as "not set".
Each command checks at startup that everything it needs is set, and exits with
code 2 and the names of the missing variables otherwise. Secret values are never
printed.

| Variable | Secret | Needed for | Notes |
|---|:-:|---|---|
| `LLM_PROVIDER` | | | `gemini` (default), `openrouter` or `fake` (offline, no API calls) |
| `GEMINI_API_KEY` | yes | `gemini` | From Google AI Studio |
| `GEMINI_MODEL` | | `gemini` | e.g. `gemini-3.8-flash` |
| `OPENROUTER_API_KEY` | yes | `openrouter` | From openrouter.ai |
| `OPENROUTER_MODEL` | | `openrouter` | Must support structured outputs, e.g. `nvidia/nemotron-3.5-super-vl:free` |
| `ATLASSIAN_EMAIL` | yes | publishing | The Atlassian account the API token belongs to |
| `ATLASSIAN_TOKEN` | yes | publishing | Atlassian API token |
| `CONFLUENCE_SITE` | | publishing | e.g. `yourname.atlassian.net` |
| `CONFLUENCE_SPACE` | | publishing | Space key |
| `CONFLUENCE_PARENT_ID` | | publishing | Numeric id of the parent page |
| `PUBLISH_MODE` | | | `rest` (default). `mcp` is reserved and not implemented |
| `SOURCE_BASE_URL` | | | e.g. `https://github.com/<owner>/<repo>/blob`; turns evidence lines into links |
| `GEMINI_THINKING_LEVEL` | | | `minimal`, `low`, `medium` or `high`; default: the model's default |
| `LLM_TEMPERATURE` | | | Default: the model's default (recommended for Gemini 3) |
| `CHUNK_THRESHOLD_LINES` | | | Bodies longer than this are documented per procedure (default 150) |

A dry run needs no Confluence settings, and the fake LLM needs no API key, so
`LLM_PROVIDER=fake` with `--dry-run` runs with no credentials at all.

## Running locally

`docgen run` takes either `--all` or `--files` with a text file of changed paths
(one per line, as `git diff --name-only` prints them; non-PL/SQL paths are ignored).

```bash
# Offline: fake LLM, pages written to out/ (open the .html files in a browser)
LLM_PROVIDER=fake python -m docgen run --all --dry-run --out out --report report.md

# One package with the real model, still not published
echo plsql/util/util_log.pkb > changed.txt
python -m docgen run --files changed.txt --dry-run --report report.md

# Publish to Confluence
python -m docgen run --files changed.txt --report report.md
```

| Option | Effect |
|---|---|
| `--dry-run` | Write pages to `--out` (default `out/`) instead of publishing |
| `--force` | Regenerate even when the page is up to date |
| `--no-cache` | Do not use the local cache of LLM answers (`.cache/docgen`) |
| `--commit SHA` | Commit to show on the pages (default: `GITHUB_SHA`, else `HEAD`) |
| `-v` | Debug logging |

Exit codes: `0` all packages succeeded, `1` at least one package failed (the
others are still processed and published), `2` configuration or usage error.

**When is a page regenerated?** Each published page stores a cache key: a hash of
the package source, the prompts, the model, the renderer version and the
chunking threshold. A package whose page has the same key is skipped without an
LLM call. Changing the model or a prompt therefore makes every page out of date.

## Evaluation

The eval runs the same extract, generate, check and render path as `docgen run`,
publishes nothing, and scores the result against `eval/ground_truth.yaml`.

```bash
python -m docgen.eval run --label my-change                # all packages, live model
python -m docgen.eval run --resume eval/results/<run>.json # finish a run cut short by the quota
python -m docgen.eval rescore eval/results/*.json          # re-apply the current checks, no LLM calls
python -m docgen.eval compare                              # Markdown table of all saved runs
```

What is measured, per run:

- **Coverage:** every procedure has a note and every table appears on the page.
- **Faithfulness violations:** identifiers in the model's text that exist
  nowhere in the source or the static analysis.
- **Evidence valid:** business rules whose cited lines lie inside the procedure
  they name.
- **Traps found:** seeded problems (hidden rule, dead code, unreachable branch,
  cross-package call, magic number, swallowed exceptions, cryptic naming,
  commented-out logic) reported in the expected section.
- **Hallucinations:** commented-out logic documented as an active business rule.
- Cost: LLM calls, tokens, latency, validation retries.

`rescore` keeps a run's original summary next to the new one, so a change to
the checks stays visible in the comparison. Results are in
[docs/RESULTS.md](docs/RESULTS.md).

## CI

Two workflows in `.github/workflows/`:

- **`test`** (every push and pull request): ruff, pytest on Python 3.12 and 3.14,
  and a gitleaks scan of the full git history.
- **`generate-docs`**:
  - push to `main` that changes `plsql/**` or `scripts/**`: regenerate the
    packages changed since the previous push (falls back to every out-of-date
    page when the previous commit is unknown, e.g. after a force push);
  - manual run (Actions tab, "Run workflow"): every out-of-date page, or every
    page with `full_run`;
  - pull request: a dry run with the fake LLM, with the rendered pages as a
    downloadable artifact. It uses no secrets and spends no quota.

  Publishing only starts after the `test` jobs pass. The run report appears on
  the run's summary page.

Set these in the repository settings (Settings > Secrets and variables > Actions):

- **Secrets:** `GEMINI_API_KEY`, `ATLASSIAN_EMAIL`, `ATLASSIAN_TOKEN`, and
  `OPENROUTER_API_KEY` if you use OpenRouter.
- **Variables:** `GEMINI_MODEL`, `CONFLUENCE_SITE`, `CONFLUENCE_SPACE`,
  `CONFLUENCE_PARENT_ID`; optionally `LLM_PROVIDER`, `OPENROUTER_MODEL`,
  `PUBLISH_MODE`. `SOURCE_BASE_URL` is set by the workflow.

**Changing a prompt or the model.** This regenerates every page, so it does not
happen automatically on merge. Run the eval with the change, compare it with the
previous runs, merge, and then start a manual `generate-docs` run, which
regenerates exactly the pages that are now out of date.

## Secrets

No secret or environment file may ever reach GitHub.

- `.env` and `.env.*` are gitignored; only `.env.example` (empty values) is
  committed. Check with `git check-ignore -v .env`.
- Stage files by path, never with `git add -A` or `git add .`, and review
  `git diff --staged` before committing.
- gitleaks runs as a pre-commit hook (staged changes) and in the `test`
  workflow (full history), so a committed key fails the build.
- In workflows, secrets are referenced only as `${{ secrets.NAME }}` and passed
  only to the step that publishes. Non-sensitive settings are `${{ vars.NAME }}`.
- Turn on GitHub **secret scanning** and **push protection** (Settings > Code
  security). Push protection blocks a push that contains a known key format
  before it reaches the repository.
- The code never prints secrets: they are `SecretStr` values, read only where
  they are sent, and error messages contain status codes and provider messages
  only.

**If a secret is committed or pushed:** rotate it first (revoke the key at the
provider and create a new one). Removing it from the history afterwards does not
make the old key safe again, because it may already have been copied.

## Development

```bash
python -m pytest -q          # offline: fake LLM, mocked HTTP
ruff check . && ruff format --check .
pre-commit run --all-files
```

The tests need no network and no credentials.
