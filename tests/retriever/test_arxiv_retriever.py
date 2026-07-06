"""Tests for ArxivRetriever."""

import time
from types import SimpleNamespace

import feedparser
from omegaconf import open_dict

from zotero_arxiv_daily.retriever.arxiv_retriever import ArxivRetriever, _run_with_hard_timeout
import zotero_arxiv_daily.retriever.arxiv_retriever as arxiv_retriever


def _sleep_and_return(value: str, delay_seconds: float) -> str:
    time.sleep(delay_seconds)
    return value


def _raise_runtime_error() -> None:
    raise RuntimeError("boom")


def test_arxiv_retriever(config, mock_feedparser, monkeypatch):
    # The RSS fixture gives us paper IDs.  After feedparser, the code calls
    # arxiv.Client().results(search) which makes real HTTP requests.  We mock
    # the arxiv Client so the test stays offline.
    new_entries = [
        e for e in mock_feedparser.entries
        if e.get("arxiv_announce_type", "new") == "new"
    ]
    paper_ids = [e.id.removeprefix("oai:arXiv.org:") for e in new_entries]

    # Build fake ArxivResult-like objects matching each RSS entry
    fake_results = []
    for entry in new_entries:
        pid = entry.id.removeprefix("oai:arXiv.org:")
        fake_results.append(SimpleNamespace(
            title=entry.title,
            authors=[SimpleNamespace(name="Test Author")],
            summary="Test abstract",
            pdf_url=f"https://arxiv.org/pdf/{pid}",
            entry_id=f"https://arxiv.org/abs/{pid}",
            source_url=lambda pid=pid: f"https://arxiv.org/e-print/{pid}",
        ))

    class FakeClient:
        def __init__(self, **kw):
            pass
        def results(self, search):
            return iter(fake_results)

    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", FakeClient)

    # Skip file downloads in convert_to_paper
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_html", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_pdf", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_tar", lambda paper: None)

    retriever = ArxivRetriever(config)
    papers = retriever.retrieve_papers()

    assert len(papers) == len(new_entries)
    assert set(p.title for p in papers) == set(e.title for e in new_entries)


def _make_fake_result(title: str, primary_category: str) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        authors=[SimpleNamespace(name="Test Author")],
        summary="Test abstract",
        pdf_url="https://arxiv.org/pdf/2026.00001",
        entry_id="https://arxiv.org/abs/2026.00001",
        primary_category=primary_category,
        source_url=lambda: "https://arxiv.org/e-print/2026.00001",
    )


def test_arxiv_search_filters_cross_list_when_disabled(config, monkeypatch):
    """When include_cross_list=False, papers whose primary_category doesn't
    match the configured categories must be filtered out even if the arXiv
    search API returned them (e.g. because they are cross-listed)."""
    with open_dict(config.source):
        config.source.arxiv.include_cross_list = False
    config.executor.debug = False

    fake_results = [
        _make_fake_result("Primary category match", "cs.AI"),
        _make_fake_result("Cross-listed paper", "stat.ML"),
    ]

    class FakeClient:
        def __init__(self, **kw):
            pass
        def results(self, search):
            return iter(fake_results)

    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", FakeClient)

    retriever = ArxivRetriever(config)
    raw_papers = retriever._retrieve_via_search(FakeClient(), retrieval_days=7)

    assert [p.title for p in raw_papers] == ["Primary category match"]


def test_arxiv_search_includes_cross_list_when_enabled(config, monkeypatch):
    with open_dict(config.source):
        config.source.arxiv.include_cross_list = True
    config.executor.debug = False

    fake_results = [
        _make_fake_result("Primary category match", "cs.AI"),
        _make_fake_result("Cross-listed paper", "stat.ML"),
    ]

    class FakeClient:
        def __init__(self, **kw):
            pass
        def results(self, search):
            return iter(fake_results)

    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", FakeClient)

    retriever = ArxivRetriever(config)
    raw_papers = retriever._retrieve_via_search(FakeClient(), retrieval_days=7)

    assert {p.title for p in raw_papers} == {"Primary category match", "Cross-listed paper"}


def test_run_with_hard_timeout_returns_value():
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 0.01), timeout=1, operation="test op", paper_title="paper"
    )
    assert result == "done"


def test_run_with_hard_timeout_returns_none_on_timeout(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 1.0), timeout=0.01, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "timed out" in warnings[0]


def test_run_with_hard_timeout_returns_none_on_failure(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _raise_runtime_error, (), timeout=1, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "boom" in warnings[0]
