import json
import random
import re as _re
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig, OmegaConf
from .utils import glob_match
from .retriever import get_retriever_cls
from .retriever.pubmed_retriever import PubMedRetriever
from .protocol import CorpusPaper
from datetime import datetime
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from openai import OpenAI
from tqdm import tqdm

# Maps source.mix_mode (1-5) to (pubmed_ratio, arxiv_ratio)
_MIX_MODE_RATIOS = {
    1: (1.00, 0.00),
    2: (0.75, 0.25),
    3: (0.50, 0.50),
    4: (0.25, 0.75),
    5: (0.00, 1.00),
}


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config: DictConfig):
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)

        # Validate search mode vs keywords
        search_mode = int(config.search.mode)
        keywords_raw = config.search.get("keywords", None)
        if search_mode < 5 and not keywords_raw:
            raise ValueError(
                f"config.search.keywords must be set when search.mode < 5 (current mode={search_mode}). "
                "Please provide a natural language description of your research interests."
            )

        self._search_mode = search_mode
        self._keywords_raw = keywords_raw  # natural language, will be expanded by LLM
        self._structured_keywords: str | None = None  # set in run()

        # Validate Zotero config when needed
        if search_mode > 1:
            if not config.zotero.user_id or not config.zotero.api_key:
                raise ValueError(
                    f"config.zotero.user_id and config.zotero.api_key must be set when search.mode > 1 "
                    f"(current mode={search_mode})."
                )

        # Determine source retrieval quotas based on mix_mode
        mix_mode = int(config.source.get("mix_mode", 5))
        pubmed_ratio, arxiv_ratio = _MIX_MODE_RATIOS.get(mix_mode, (0.0, 1.0))
        max_total = int(config.executor.max_paper_num)

        # fetch 2x the target ratio to allow room for relevance filtering,
        # with a floor of 1 so a small max_paper_num doesn't round the quota to 0.
        self._pubmed_quota = max(1, int(max_total * pubmed_ratio * 2)) if pubmed_ratio > 0 else 0

        # Build retrievers based on source config and mix_mode
        self.retrievers: dict = {}

        if pubmed_ratio > 0:
            pubmed_retriever = PubMedRetriever(config)
            pubmed_max = int(config.source.pubmed.get("max_results", 200))
            pubmed_retriever.set_max_results(min(self._pubmed_quota, pubmed_max))
            self.retrievers["pubmed"] = pubmed_retriever

        if arxiv_ratio > 0:
            for source in config.executor.source:
                if source == "pubmed":
                    continue  # pubmed handled separately above
                retriever_cls = get_retriever_cls(source)
                self.retrievers[source] = retriever_cls(config)

        if not self.retrievers:
            raise ValueError(
                "No paper retriever was configured. Check config.source.mix_mode "
                f"(current={mix_mode}) and config.executor.source "
                f"(current={list(config.executor.source)}): at least one source must be active."
            )

    # ------------------------------------------------------------------
    # Keyword extraction
    # ------------------------------------------------------------------

    def _extract_structured_keywords(self, natural_language: str) -> dict:
        """Call LLM to decompose natural language into structured search keywords.

        Returns a dict:
          - "pubmed_strict": focused PubMed query (1-2 AND concept groups)
          - "pubmed_broad":  single concept-group fallback (no AND, just OR synonyms)
          - "semantic":      short English phrase for embedding similarity scoring
        """
        logger.info("Extracting structured keywords from natural language input via LLM...")
        response = self.openai_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a biomedical literature search expert.\n"
                        "Given a research interest description (possibly in Chinese or English), "
                        "output a JSON object with exactly three fields.\n"
                        "PubMed only matches exact terms appearing in the title/abstract, so recall "
                        "matters more than precision here — a downstream semantic reranker will later "
                        "filter out irrelevant results, so prefer casting a wider net over being overly "
                        "specific.\n\n"
                        "1. \"pubmed_strict\": A focused PubMed boolean query. Rules:\n"
                        "   - Only use 2 concept groups joined by AND if the description clearly names "
                        "     TWO distinct topics that must co-occur (e.g., 'deep learning applied to "
                        "     dry eye diagnosis'). If the description is about a SINGLE topic, use just "
                        "     1 concept group with NO AND — do not invent a second concept to narrow it.\n"
                        "   - For each concept, list 4-6 English synonyms/variants joined by OR, including "
                        "     broader/more general parent terms and common abbreviations, not only the "
                        "     narrowest technical phrasing (skip generic terms like 'study', 'research', "
                        "     'method', 'treatment' unless they are the topic itself).\n"
                        "   - Wrap multi-word phrases in double quotes and append [tiab]; "
                        "     single-word technical terms may omit [tiab].\n"
                        "   - NEVER use more than 2 AND groups.\n"
                        "   - Good example for a single topic 'dry eye disease':\n"
                        "     (\"dry eye\"[tiab] OR \"dry eye disease\"[tiab] OR \"dry eye syndrome\"[tiab] "
                        "OR keratoconjunctivitis[tiab] OR DED[tiab])\n"
                        "   - Good example for two co-occurring topics 'AI diagnosis of dry eye':\n"
                        "     (\"dry eye\"[tiab] OR \"dry eye disease\"[tiab] OR keratoconjunctivitis[tiab]) "
                        "AND (\"artificial intelligence\"[tiab] OR \"machine learning\"[tiab] OR "
                        "\"deep learning\"[tiab] OR AI[tiab])\n\n"
                        "2. \"pubmed_broad\": A single-concept fallback query (NO AND operator). Rules:\n"
                        "   - Use only the single most important topic from the description.\n"
                        "   - List 5-8 synonyms/variants joined by OR, favoring broader/more general terms "
                        "     over narrow technical variants to maximize recall.\n"
                        "   - Good example: (\"dry eye\"[tiab] OR \"dry eye disease\"[tiab] OR "
                        "\"ocular surface disease\"[tiab] OR keratoconjunctivitis[tiab] OR DED[tiab])\n\n"
                        "3. \"semantic\": A short English phrase (8-20 words) describing the research "
                        "topic for semantic similarity search. Natural language only, no operators.\n\n"
                        "Output ONLY valid JSON. No markdown fences, no explanation."
                    ),
                },
                {"role": "user", "content": natural_language},
            ],
            **self.config.llm.get("generation_kwargs", {}),
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown code fences if the model adds them
        raw = _re.sub(r"^```\w*\n?", "", raw).rstrip("`").strip()
        try:
            data = json.loads(raw)
            # Support both old single-field format and new two-field format
            pubmed_strict = str(data.get("pubmed_strict") or data.get("pubmed", "")).strip()
            pubmed_broad = str(data.get("pubmed_broad", "")).strip()
            semantic_kw = str(data.get("semantic", natural_language)).strip()
        except Exception:
            logger.warning(f"Keyword LLM returned non-JSON, using raw output as fallback: {raw!r}")
            pubmed_strict = ""
            pubmed_broad = ""
            semantic_kw = raw or natural_language
        logger.info(f"Structured keywords — PubMed strict: {pubmed_strict}")
        logger.info(f"Structured keywords — PubMed broad:  {pubmed_broad}")
        logger.info(f"Structured keywords — Semantic: {semantic_kw}")
        return {"pubmed_strict": pubmed_strict, "pubmed_broad": pubmed_broad, "semantic": semantic_kw}

    # ------------------------------------------------------------------
    # Zotero corpus
    # ------------------------------------------------------------------

    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = zot.everything(zot.collections())
        collections = {c['key']: c for c in collections}
        corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
        corpus = [c for c in corpus if c['data']['abstractNote'] != '']

        def get_collection_path(col_key: str) -> str:
            if p := collections[col_key]['data']['parentCollection']:
                return get_collection_path(p) + '/' + collections[col_key]['data']['name']
            else:
                return collections[col_key]['data']['name']

        for c in corpus:
            paths = [get_collection_path(col) for col in c['data']['collections']]
            c['paths'] = paths
        logger.info(f"Fetched {len(corpus)} zotero papers")
        return [CorpusPaper(
            title=c['data']['title'],
            abstract=c['data']['abstractNote'],
            added_date=datetime.strptime(c['data']['dateAdded'], '%Y-%m-%dT%H:%M:%SZ'),
            paths=c['paths']
        ) for c in corpus]

    def filter_corpus(self, corpus: list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            logger.info(f"Selecting zotero papers matching include_path: {self.include_path_patterns}")
            corpus = [
                c for c in corpus
                if any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.include_path_patterns
                )
            ]
        if self.ignore_path_patterns:
            logger.info(f"Excluding zotero papers matching ignore_path: {self.ignore_path_patterns}")
            corpus = [
                c for c in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.ignore_path_patterns
                )
            ]
        if self.include_path_patterns or self.ignore_path_patterns:
            samples = random.sample(corpus, min(5, len(corpus)))
            samples = '\n'.join([c.title + ' - ' + '\n'.join(c.paths) for c in samples])
            logger.info(f"Selected {len(corpus)} zotero papers:\n{samples}\n...")
        return corpus

    # ------------------------------------------------------------------
    # Email summary
    # ------------------------------------------------------------------

    def generate_email_summary(self, papers: list) -> str:
        """Generate a Chinese opening summary + encouragement for the email.

        Randomly selects one character from config.executor.character_pool
        (see config/characters.yaml) each time. If the pool is empty, the
        opening summary feature is skipped entirely. The sign-off
        "—— 角色名 如是说" is appended in code to guarantee a consistent
        format regardless of LLM output.
        """
        character_pool = self.config.executor.get("character_pool", None)
        if not character_pool:
            return ""
        character = random.choice(character_pool)
        system_prompt = character["prompt"]
        char_name = character["name"]

        paper_list = "\n".join(
            f"- {p.title}：{p.tldr or p.abstract[:100]}"
            for p in papers[:30]  # cap to avoid exceeding token limit
        )
        user_prompt = f"以下是本期推荐的文献列表（共{len(papers)}篇）：\n{paper_list}"

        try:
            response = self.openai_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **self.config.llm.get("generation_kwargs", {}),
            )
            body = response.choices[0].message.content.strip()
            return f"{body}\n\n—— {char_name} 如是说"
        except Exception as e:
            logger.warning(f"Failed to generate email summary: {e}")
            return ""

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def run(self):
        # Step 1: extract structured keywords if needed
        if self._search_mode < 5 and self._keywords_raw:
            kw = self._extract_structured_keywords(self._keywords_raw)
            # kw is {"pubmed_strict": "...", "pubmed_broad": "...", "semantic": "..."}
            self._structured_keywords = kw["semantic"]  # used by reranker
            if "pubmed" in self.retrievers:
                self.retrievers["pubmed"].set_pubmed_query(kw["pubmed_strict"])
                self.retrievers["pubmed"].set_pubmed_broad_query(kw["pubmed_broad"])

        # Step 2: fetch Zotero corpus (skip if mode=1)
        corpus: list[CorpusPaper] = []
        if self._search_mode > 1:
            corpus = self.fetch_zotero_corpus()
            corpus = self.filter_corpus(corpus)
            if len(corpus) == 0:
                logger.error(
                    f"No zotero papers found. Please check your zotero settings:\n{self.config.zotero}"
                )
                return

        # Step 3: retrieve papers from all sources (in parallel)
        all_papers = []
        with ThreadPoolExecutor(max_workers=len(self.retrievers)) as pool:
            future_to_source = {
                pool.submit(retriever.retrieve_papers): source
                for source, retriever in self.retrievers.items()
            }
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    papers = future.result()
                except Exception as exc:
                    logger.error(f"Retriever {source} failed: {exc}")
                    continue
                if len(papers) == 0:
                    logger.info(f"No {source} papers found")
                    continue
                logger.info(f"Retrieved {len(papers)} {source} papers")
                all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")

        reranked_papers = []
        if len(all_papers) > 0:
            # Step 4: rerank with mixed scoring
            logger.info("Reranking papers...")
            fav_journals = self.config.executor.get("favorite_journals", None)
            reranked_papers = self.reranker.rerank(
                all_papers,
                corpus,
                keyword_query=self._structured_keywords,
                favorite_journals=fav_journals,
            )
            reranked_papers = reranked_papers[:self.config.executor.max_paper_num]

            # Step 5: generate TLDRs, affiliations and Chinese title translations
            logger.info("Generating TLDR, affiliations and title translations...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                p.generate_affiliations(self.openai_client, self.config.llm)
                p.generate_title_translation(self.openai_client, self.config.llm)

        elif not self.config.executor.send_empty:
            logger.info("No new papers found. No email will be sent.")
            return 0

        # Step 6: generate opening summary
        summary = ""
        if reranked_papers:
            logger.info("Generating email summary...")
            summary = self.generate_email_summary(reranked_papers)

        # Step 7: render and send email
        logger.info("Sending email...")
        email_content = render_email(reranked_papers, summary=summary or None)
        send_email(self.config, email_content)
        logger.info(f"Email sent successfully ({len(reranked_papers)} papers)")
        return len(reranked_papers)
