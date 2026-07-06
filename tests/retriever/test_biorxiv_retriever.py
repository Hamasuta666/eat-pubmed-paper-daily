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
