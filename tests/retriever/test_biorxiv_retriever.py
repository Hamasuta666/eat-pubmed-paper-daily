"""Tests for BiorxivRetriever."""

import pytest
from omegaconf import open_dict

from zotero_arxiv_daily.retriever.biorxiv_retriever import BiorxivRetriever
from tests.canned_responses import SAMPLE_BIORXIV_API_RESPONSE


def test_biorxiv_retrieve(config, mock_biorxiv_api, monkeypatch):
    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    papers = retriever.retrieve_papers()
    # The biorxiv API itself scopes results to the requested date range
    # (via retrieval_days), so all matching-category papers in the response
    # are kept — not just the single latest date.
    assert len(papers) == 2
    assert {p.title for p in papers} == {"A biorxiv paper", "Old biorxiv paper"}


def test_biorxiv_empty_response(config, monkeypatch):
    import requests
    from types import SimpleNamespace

    empty = {"messages": [{"status": "ok"}], "collection": []}

    def _patched(url, **kw):
        resp = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        resp.json = lambda: empty
        return resp

    monkeypatch.setattr(requests, "get", _patched)

    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    papers = retriever.retrieve_papers()
    assert papers == []


def test_biorxiv_convert_to_paper(config):
    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    raw = SAMPLE_BIORXIV_API_RESPONSE["collection"][0]
    paper = retriever.convert_to_paper(raw)
    assert paper.title == "A biorxiv paper"
    assert paper.source == "biorxiv"
    assert "biorxiv.org" in paper.pdf_url
    assert paper.authors == ["Smith, J.", "Doe, A.", "Lee, K."]


def test_biorxiv_requires_category(config):
    with open_dict(config.source):
        config.source.biorxiv = {"category": None}
    with pytest.raises(ValueError, match="category must be specified"):
        BiorxivRetriever(config)


def test_biorxiv_paginates_until_total_reached(config, monkeypatch):
    """A day with more papers than one page must be fully fetched, not truncated."""
    import requests
    from types import SimpleNamespace

    page_1 = {
        "messages": [{"status": "ok", "cursor": 0, "total": "3"}],
        "collection": [
            {**SAMPLE_BIORXIV_API_RESPONSE["collection"][0]},
            {**SAMPLE_BIORXIV_API_RESPONSE["collection"][1]},
        ],
    }
    page_2 = {
        "messages": [{"status": "ok", "cursor": 2, "total": "3"}],
        "collection": [{**SAMPLE_BIORXIV_API_RESPONSE["collection"][2]}],
    }
    requested_urls = []

    def _patched(url, **kwargs):
        requested_urls.append(url)
        resp = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        resp.json = lambda: page_2 if url.rstrip("/").endswith("/2") else page_1
        return resp

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.retriever.biorxiv_retriever.sleep", lambda _: None)

    with open_dict(config.source):
        config.source.biorxiv = {"category": ["bioinformatics"]}
    retriever = BiorxivRetriever(config)
    papers = retriever.retrieve_papers()

    assert len(requested_urls) == 2, "must fetch a second page when total exceeds the first page"
    assert {p.title for p in papers} == {"A biorxiv paper", "Old biorxiv paper"}
