# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This project (a customized fork of [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily)) recommends new arXiv/bioRxiv/medRxiv/PubMed papers based on a user's Zotero library and/or a natural-language description of their research interests. It computes embedding similarity between new papers and the user's existing library (and/or keyword query), generates TLDRs via LLM, and delivers results by email. Designed to run entirely as a GitHub Actions workflow at zero cost — no server to maintain.

## Commands

```bash
# Run the application
uv run src/zotero_arxiv_daily/main.py

# Run tests (excludes slow tests by default)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run a single test
uv run pytest tests/test_utils.py::TestGlobMatch -v

# Install/sync dependencies
uv sync
```

No linter or formatter is configured.

## Architecture

The app follows a linear pipeline orchestrated by `Executor` (`src/zotero_arxiv_daily/executor.py`):

1. **Fetch Zotero corpus** — retrieves user's library papers via pyzotero API
2. **Filter corpus** — applies `include_path` glob patterns to select relevant collections
3. **Retrieve new papers** — fetches from configured sources (arXiv RSS/API, bioRxiv/medRxiv REST API, PubMed E-utilities)
4. **Rerank** — scores candidates by weighted similarity to corpus (newer Zotero papers weighted higher)
5. **Generate TLDRs + affiliations** — via OpenAI-compatible LLM API
6. **Render + send email** — HTML email via SMTP

### Plugin Systems

**Retrievers** (`src/zotero_arxiv_daily/retriever/`): Register via `@register_retriever` decorator, discovered by `get_retriever_cls()`. Each retriever implements `_retrieve_raw_papers()` and `convert_to_paper()`.

**Rerankers** (`src/zotero_arxiv_daily/reranker/`): Register via `@register_reranker` decorator, discovered by `get_reranker_cls()`. Two implementations: `local` (sentence-transformers) and `api` (OpenAI-compatible embeddings endpoint).

### Configuration

Uses Hydra + OmegaConf. Config is composed from `config/base.yaml` (defaults) + `config/characters.yaml` (email opening-summary character pool, `executor.character_pool`) + `config/custom.yaml` (user overrides, last so it wins). Environment variables are interpolated via `${oc.env:VAR_NAME,default}` syntax. Entry point uses `@hydra.main`.

### Data Classes

`Paper` and `CorpusPaper` in `src/zotero_arxiv_daily/protocol.py`. `Paper` has LLM-powered methods (`generate_tldr`, `generate_affiliations`) that call the OpenAI API directly.

## Testing

Tests marked `@pytest.mark.slow` require heavy dependencies (e.g., sentence-transformers model download) and are skipped locally by default (`addopts = "-m 'not slow'"` in pyproject.toml). All other tests run with pure Python stubs (no Docker containers needed).

```bash
# Run tests (excludes slow tests)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run with coverage
uv run pytest --cov=src/zotero_arxiv_daily --cov-report=term-missing
```

## Git Workflow

- Single-branch workflow: all development happens on `main`.
