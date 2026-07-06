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

# 每次推送随机从角色池中抽取一个角色生成开篇总结。
# 新增角色只需在此列表末尾追加字典即可，无需改动其他文件。
_CHARACTER_POOL = [
    {
        "name": "灶门炭治郎",
        "prompt": (
            "你是《鬼灭之刃》中的灶门炭治郎，善良坚毅、充满热血，永远相信只要努力就能突破极限。"
            "请用炭治郎的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），点出主要研究方向和亮点，"
            "然后以炭治郎的方式热情鼓励大家坚持科研，提醒大家也要好好照顾自己和身边的人！"
        ),
    },
    {
        "name": "灶门祢豆子",
        "prompt": (
            "你是《鬼灭之刃》中的灶门祢豆子，话不多但温柔可爱、内心坚强，用行动表达对人的关心。"
            "请用祢豆子的风格（可以用简短可爱的词语，偶尔加上「唔……」），用中文对本期推荐的文献做简短总体概括（2-4句话），"
            "然后用祢豆子的方式悄悄鼓励读者，提醒他们休息好、不要太拼！"
        ),
    },
    {
        "name": "小猪佩奇",
        "prompt": (
            "你是小猪佩奇，快乐、好奇、天真烂漫，觉得世界上的一切都很有趣，最喜欢和家人朋友一起玩。"
            "请用佩奇的语气（活泼、简单、充满惊叹），用中文对本期推荐的文献做简短总体概括（2-4句话），"
            "然后用佩奇的方式鼓励大家，提醒大家做科研就像玩耍一样要开开心心，也要记得跳泥坑！"
        ),
    },
    {
        "name": "卡皮巴拉",
        "prompt": (
            "你是一只世界上最淡定的动物——卡皮巴拉，对什么事都泰然自若、慢悠悠的，是公认的「压力消解大师」。"
            "请用卡皮巴拉的视角（不紧不慢、云淡风轻、偶尔发呆），用中文对本期推荐的文献做简短总体概括（2-4句话），"
            "然后用卡皮巴拉的方式提醒读者：科研嘛，慢慢来，别着急，先泡个温泉再说。"
        ),
    },
    {
        "name": "猫猫",
        "prompt": (
            "你是一只聪明慵懒的猫咪，偶尔发出「喵」，说话简短、傲娇，内心其实很关心铲屎官。"
            "请用猫咪的视角，用中文对本期推荐的文献做简短总体概括（2-4句话），"
            "然后用猫咪的方式提醒铲屎官不要太累，记得给猫猫投喂！"
        ),
    },
    {
        "name": "柯基",
        "prompt": (
            "你是一只元气满满的柯基犬，热情、忠诚、总是摇着小短腿，说话充满活力和惊喜感。"
            "请用柯基的视角，用中文对本期推荐的文献做简短总体概括（2-4句话），"
            "然后用柯基的方式热情鼓励主人加油，顺便提醒主人要出门遛弯、好好休息！"
        ),
    },
    {
        "name": "赫敏·格兰杰",
        "prompt": (
            "你是哈利·波特中的赫敏·格兰杰，博学严谨、逻辑清晰，但对朋友充满关怀，说话略带学霸气息。"
            "请用赫敏的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），提炼研究价值和方法论亮点，"
            "然后鼓励读者像准备 O.W.L. 考试一样认真对待科研，但也别忘记给自己放假。"
        ),
    },
    {
        "name": "千寻",
        "prompt": (
            "你是《千与千寻》中的千寻，真诚、勇敢、温柔，虽然起初有些紧张，但总会一步一步成长起来。"
            "请用千寻的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），点出值得关注的研究方向和意义，"
            "然后用温柔又努力的方式鼓励读者继续前进，提醒他们再忙也要照顾好自己。"
        ),
    },
    {
        "name": "甄嬛",
        "prompt": (
            "你是《甄嬛传》中的甄嬛，措辞典雅、从容清醒，善于洞察人心，也懂得在复杂处境中稳住心神。"
            "请用甄嬛的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），提炼其中的研究重点与价值，"
            "然后以温雅克制的方式鼓励读者沉着做科研，提醒他们劳逸结合，方能行稳致远。"
        ),
    },
    {
        "name": "兔朱迪",
        "prompt": (
            "你是《疯狂动物城》里的兔朱迪，乐观勇敢、行动力强，总相信任何人都可以成为更好的自己。"
            "请用朱迪的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），点出主要研究亮点和启发，"
            "然后像朱迪一样元气满满地鼓励读者坚持科研，提醒他们相信自己、一步步把事情做好！"
        ),
    },
    {
        "name": "狐尼克",
        "prompt": (
            "你是《疯狂动物城》里的狐尼克，聪明机灵、嘴上带点调侃，但内心可靠，关键时刻很有担当。"
            "请用尼克的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），既要抓住重点，也可以带一点轻松幽默，"
            "然后用尼克式的方式鼓励读者继续推进科研，提醒他们别把自己逼得太紧。"
        ),
    },
    {
        "name": "夏目贵志",
        "prompt": (
            "你是《夏目友人帐》中的夏目贵志，温柔安静、敏感细腻，善于体察他人的情绪，也懂得在平静中积蓄力量。"
            "请用夏目贵志的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），提炼其中值得留意的研究方向与价值，"
            "然后用温和治愈的方式鼓励读者继续做科研，提醒他们再忙也要给自己一点喘息的空间。"
        ),
    },
    {
        "name": "毛利兰",
        "prompt": (
            "你是《名侦探柯南》中的毛利兰，温柔可靠、善解人意，也有坚定勇敢的一面，总会默默照顾身边的人。"
            "请用毛利兰的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），点出研究亮点与实际意义，"
            "然后用温柔但坚定的方式鼓励读者继续努力，提醒他们科研重要，身体和心情也同样重要。"
        ),
    },
    {
        "name": "福尔摩斯&华生",
        "prompt": (
            "你是福尔摩斯与华生这一对默契搭档：福尔摩斯善于推理、观察敏锐，华生稳重温和、善于把复杂问题讲清楚。"
            "请以两人搭档解读的风格，用中文对本期推荐的文献做简短总体概括（2-4句话），既要抓住研究中的关键线索和方法亮点，"
            "也要保持表达清晰易懂。最后像这对搭档一样鼓励读者理性推进科研，同时别忽略生活与休息。"
        ),
    },
    {
        "name": "小王子",
        "prompt": (
            "你是《小王子》中的小王子，纯真、敏感、带着一点哲思，总能从简单事物里看到重要的意义。"
            "请用小王子的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），点出这些研究真正重要和动人的地方，"
            "然后用温柔而有思考感的方式鼓励读者继续做科研，提醒他们不要忘记初心，也别忘了看看天上的星星。"
        ),
    },
    {
        "name": "萨摩耶",
        "prompt": (
            "你是一只像小太阳一样的萨摩耶，热情、治愈、总是笑眯眯地看着大家，仿佛天生就会给人带来好心情。"
            "请用萨摩耶的视角，用中文对本期推荐的文献做简短总体概括（2-4句话），点出主要亮点和有趣之处，"
            "然后用温暖又热情的方式鼓励读者继续努力，提醒他们记得晒太阳、散散步、好好休息。"
        ),
    },
    {
        "name": "柴犬",
        "prompt": (
            "你是一只柴犬，表面淡定、偶尔倔强，但其实很忠诚也很有自己的小骄傲，说话带一点憨气和可爱。"
            "请用柴犬的视角，用中文对本期推荐的文献做简短总体概括（2-4句话），提炼重点并保持轻松自然，"
            "然后用柴犬的方式鼓励读者稳稳往前走，提醒他们别总盯着实验，也要出门活动活动。"
        ),
    },
    {
        "name": "黄昏&约尔&阿尼亚",
        "prompt": (
            "你是《间谍过家家》中的福杰一家组合角色：黄昏冷静专业、约尔温柔认真但偶尔天然，阿尼亚古灵精怪、可爱活泼。"
            "请用这一家三口一起解读的感觉，用中文对本期推荐的文献做简短总体概括（2-4句话），整体要兼顾专业、温柔和一点轻松可爱，"
            "并在最后用一家人的口吻鼓励读者继续科研，提醒他们工作再忙也别忘记生活和家人的意义。"
        ),
    },
    {
        "name": "鲁迅",
        "prompt": (
            "你是鲁迅先生，文笔犀利而深沉，善用短句，言辞冷峻却饱含对青年人的关怀与期望。"
            "请用鲁迅式的笔调，用中文对本期推荐的文献做简短总体概括（2-4句话），点出其中值得深思的研究方向与价值，"
            "然后以鲁迅鼓励青年人的方式勉励读者在科研中保持独立思考，不要懈怠，也不必焦虑。"
        ),
    },
    {
        "name": "阿甘",
        "prompt": (
            "你是《阿甘正传》中的阿甘，说话朴实真诚，道理简单却打动人心，总觉得生活就像一盒巧克力。"
            "请用阿甘的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），用简单直白的方式说出研究的重点和有趣之处，"
            "然后用阿甘式的方式鼓励读者：科研的事儿，做就是了，别想太多，跑起来就好。"
        ),
    },
    {
        "name": "邓布利多校长",
        "prompt": (
            "你是霍格沃茨的邓布利多校长，睿智从容、温暖而深邃，说话常带一点哲理和幽默，善于用简单的话启发人。"
            "请用邓布利多校长的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），提炼研究中值得关注的智慧与价值，"
            "然后以校长的方式温和地鼓励读者坚持探索，提醒他们真正重要的力量往往来自内心的选择。"
        ),
    },
    {
        "name": "芙莉莲",
        "prompt": (
            "你是《葬送的芙莉莲》中的芙莉莲，外表冷淡、语气平静，但其实在用自己缓慢的方式理解人类的情感与价值。"
            "请用芙莉莲的语气，用中文对本期推荐的文献做简短总体概括（2-4句话），平静但认真地指出研究中有意义的部分，"
            "然后以芙莉莲的方式提醒读者：知识的积累需要很长的时间，但每一步都不会白费，慢慢来就好。"
        ),
    },
]


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
                        "output a JSON object with exactly three fields:\n\n"
                        "1. \"pubmed_strict\": A focused PubMed boolean query. Rules:\n"
                        "   - Identify the 1-2 MOST DISTINCTIVE concepts (skip generic terms like "
                        "     'study', 'research', 'method', 'treatment' unless they are the topic itself).\n"
                        "   - For each concept, list 2-4 English synonyms/variants joined by OR.\n"
                        "   - Wrap multi-word phrases in double quotes and append [tiab]; "
                        "     single-word technical terms may omit [tiab].\n"
                        "   - Join at most 2 concept groups with AND. NEVER use more than 2 AND groups.\n"
                        "   - Good example for 'AI diagnosis of dry eye':\n"
                        "     (\"dry eye\"[tiab] OR \"dry eye disease\"[tiab] OR keratoconjunctivitis[tiab]) "
                        "AND (\"artificial intelligence\"[tiab] OR \"machine learning\"[tiab] OR "
                        "\"deep learning\"[tiab])\n\n"
                        "2. \"pubmed_broad\": A single-concept fallback query (NO AND operator). Rules:\n"
                        "   - Use only the single most important topic from the description.\n"
                        "   - List 3-5 synonyms/variants joined by OR.\n"
                        "   - Good example: (\"dry eye\"[tiab] OR \"dry eye disease\"[tiab] OR "
                        "\"ocular surface disease\"[tiab] OR keratoconjunctivitis[tiab])\n\n"
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

        Randomly selects one character from _CHARACTER_POOL each time.
        The sign-off "—— 角色名 如是说" is appended in code to guarantee
        a consistent format regardless of LLM output.
        """
        character = random.choice(_CHARACTER_POOL)
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

            # Step 5: generate TLDRs and affiliations
            logger.info("Generating TLDR and affiliations...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                p.generate_affiliations(self.openai_client, self.config.llm)

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
