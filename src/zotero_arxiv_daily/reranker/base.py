from abc import ABC, abstractmethod
from omegaconf import DictConfig
from ..protocol import Paper, CorpusPaper
import numpy as np
from typing import Type

# Maps search.mode (1-5) to keyword weight alpha
_MODE_TO_ALPHA = {1: 1.0, 2: 0.75, 3: 0.5, 4: 0.25, 5: 0.0}


class BaseReranker(ABC):
    def __init__(self, config: DictConfig):
        self.config = config

    def rerank(
        self,
        candidates: list[Paper],
        corpus: list[CorpusPaper],
        keyword_query: str | None = None,
        favorite_journals: list[str] | None = None,
    ) -> list[Paper]:
        search_mode = int(self.config.search.mode)
        alpha = _MODE_TO_ALPHA.get(search_mode, 0.0)

        keyword_scores = np.zeros(len(candidates))
        corpus_scores = np.zeros(len(candidates))

        # Build text representations: title + abstract for richer semantic matching
        candidate_texts = [f"{c.title}. {c.abstract}" for c in candidates]

        # --- keyword scoring ---
        if alpha > 0 and keyword_query:
            kw_sim = self.get_similarity_score(
                candidate_texts, [keyword_query]
            )  # shape: (n_candidates, 1)
            keyword_scores = kw_sim[:, 0]  # (n_candidates,)

        # --- corpus (Zotero) scoring ---
        if alpha < 1.0 and len(corpus) > 0:
            corpus_sorted = sorted(corpus, key=lambda x: x.added_date, reverse=True)
            time_decay_weight = 1 / (1 + np.log10(np.arange(len(corpus_sorted)) + 1))
            time_decay_weight = time_decay_weight / time_decay_weight.sum()
            corpus_texts = [f"{c.title}. {c.abstract}" for c in corpus_sorted]
            sim = self.get_similarity_score(
                candidate_texts,
                corpus_texts,
            )  # shape: (n_candidates, n_corpus)
            assert sim.shape == (len(candidates), len(corpus_sorted))
            corpus_scores = (sim * time_decay_weight).sum(axis=1)

        scores = (alpha * keyword_scores + (1.0 - alpha) * corpus_scores) * 10

        # --- journal bonus: boost papers from user's favorite journals ---
        if favorite_journals:
            fav_set = {j.lower().strip() for j in favorite_journals}
            for i, c in enumerate(candidates):
                if c.journal and c.journal.lower().strip() in fav_set:
                    scores[i] *= 1.5

        for s, c in zip(scores, candidates):
            c.score = float(s)
        candidates = sorted(candidates, key=lambda x: x.score, reverse=True)
        return candidates

    @abstractmethod
    def get_similarity_score(self, s1: list[str], s2: list[str]) -> np.ndarray:
        raise NotImplementedError

registered_rerankers = {}

def register_reranker(name:str):
    def decorator(cls):
        registered_rerankers[name] = cls
        return cls
    return decorator

def get_reranker_cls(name:str) -> Type[BaseReranker]:
    if name not in registered_rerankers:
        raise ValueError(f"Reranker {name} not found")
    return registered_rerankers[name]