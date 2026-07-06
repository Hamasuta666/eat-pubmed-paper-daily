"""Tests for PubMedRetriever."""

from types import SimpleNamespace

from zotero_arxiv_daily.retriever.pubmed_retriever import PubMedRetriever, _parse_affiliation
from tests.canned_responses import (
    SAMPLE_PUBMED_ESEARCH_RESPONSE,
    SAMPLE_PUBMED_MEDLINE_TEXT,
)


def _patch_ncbi(monkeypatch, esearch_result=None, medline_text=None):
    """Patch requests.get so esearch.fcgi/efetch.fcgi return canned data."""
    import requests

    esearch_result = esearch_result if esearch_result is not None else SAMPLE_PUBMED_ESEARCH_RESPONSE
    medline_text = medline_text if medline_text is not None else SAMPLE_PUBMED_MEDLINE_TEXT

    def _patched(url, params=None, timeout=None, **kwargs):
        resp = SimpleNamespace(raise_for_status=lambda: None)
        if "esearch.fcgi" in url:
            resp.json = lambda: esearch_result
        elif "efetch.fcgi" in url:
            resp.text = medline_text
        return resp

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.retriever.pubmed_retriever.sleep", lambda _: None)


def test_pubmed_retrieve_end_to_end(config, monkeypatch):
    _patch_ncbi(monkeypatch)
    retriever = PubMedRetriever(config)
    retriever.set_pubmed_query('"dry eye"[tiab]')
    papers = retriever.retrieve_papers()

    assert len(papers) == 2
    titles = {p.title for p in papers}
    assert "A sample PubMed paper on AI diagnostics" in titles
    assert all(p.source == "pubmed" for p in papers)


def test_pubmed_convert_to_paper_full_record(config):
    retriever = PubMedRetriever(config)
    raw = {
        "PMID": "30000001",
        "TI": "A sample PubMed paper",
        "AB": "Sample abstract text.",
        "AU": ["Smith J", "Doe A"],
        "AD": [
            "Department of Ophthalmology, Harvard Medical School, Boston, MA 02115, USA.",
            "Department of Ophthalmology, Harvard Medical School, Boston, MA 02115, USA.",
        ],
        "JT": "Nature Medicine",
        "LID": "10.1038/s41591-026-00001-1 [doi]",
    }
    paper = retriever.convert_to_paper(raw)

    assert paper.title == "A sample PubMed paper"
    assert paper.authors == ["Smith J", "Doe A"]
    assert paper.url == "https://pubmed.ncbi.nlm.nih.gov/30000001/"
    assert paper.pdf_url == "https://doi.org/10.1038/s41591-026-00001-1"
    assert paper.journal == "Nature Medicine"
    # Duplicate AD lines must be deduplicated while preserving order
    assert paper.affiliations == ["Department of Ophthalmology, Harvard Medical School"]


def test_pubmed_convert_to_paper_skips_missing_title(config):
    retriever = PubMedRetriever(config)
    raw = {"PMID": "1", "AB": "abstract only, no title"}
    assert retriever.convert_to_paper(raw) is None


def test_pubmed_convert_to_paper_skips_missing_abstract(config):
    retriever = PubMedRetriever(config)
    raw = {"PMID": "1", "TI": "title only, no abstract"}
    assert retriever.convert_to_paper(raw) is None


def test_pubmed_convert_to_paper_no_affiliation(config):
    retriever = PubMedRetriever(config)
    raw = {"PMID": "2", "TI": "No affiliation paper", "AB": "abstract", "AU": "Lee K"}
    paper = retriever.convert_to_paper(raw)
    assert paper.affiliations is None
    assert paper.authors == ["Lee K"]


def test_parse_affiliation_strips_postal_code_and_electronic_address():
    ad = "Department of Chemistry, MIT, Cambridge, MA 02139, USA. Electronic address: x@x.edu."
    assert _parse_affiliation(ad) == "Department of Chemistry, MIT"


def test_pubmed_fallback_escalates_when_strict_query_too_narrow(config, monkeypatch):
    """Strict query returns too few hits (< _MIN_RESULTS) -> escalate to broad query."""

    def _fake_fetch_pmids(self, query):
        if "strict" in query:
            return ["1"]  # below _MIN_RESULTS
        return [str(i) for i in range(20)]  # broad query: plenty of results

    monkeypatch.setattr(PubMedRetriever, "_fetch_pmids", _fake_fetch_pmids)
    monkeypatch.setattr(PubMedRetriever, "_fetch_details", lambda self, pmids: [{"pmids": pmids}])

    retriever = PubMedRetriever(config)
    retriever.set_pubmed_query("strict query")
    retriever.set_pubmed_broad_query("broad query")
    result = retriever._retrieve_raw_papers()

    assert result == [{"pmids": [str(i) for i in range(20)]}]


def test_pubmed_max_results_override(config):
    retriever = PubMedRetriever(config)
    assert retriever._max_results == config.source.pubmed.max_results
    retriever.set_max_results(10)
    assert retriever._max_results == 10
