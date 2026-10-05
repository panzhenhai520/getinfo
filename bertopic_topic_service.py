#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BERTopic 动态主题趋势：复用 bge-m3 预计算嵌入聚类 + LLM 命名 + topics_over_time。

复用 intel_article_embeddings（bge-m3 1024 维，embed_articles_service 写入），
embedding_model=None 不下载模型；中文 c-TF-IDF 用 jieba CountVectorizer；主题名聚类
后用本地 LLM（8106 deepseek-v4-flash，OpenAI 兼容）经 requests 起可读名字。主题元信息
落 intel_topics(topic_source='automatic')，主题×天热度经 trend_detect 算五态落
intel_topic_trends(dimension='bertopic')，前端趋势页“主题趋势”tab 展示。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Dict, List

import config
from intel_database import IntelRepository
from industry_pack_runtime import active_industry_composition_service
from trend_detect import analyze
from utils import get_china_time

logger = logging.getLogger(__name__)
BERTOPIC_DIMENSION = "bertopic"

# 基础中文停用词（可后续改为加载外部词表）
_STOPWORDS = set("""的 了 和 与 及 或 在 是 有 对 为 从 到 把 被 让 这 那 你 我 他 它
们 也 都 就 还 又 再 更 最 很 太 而 但 等 中 上 下 里 个 之 其 此 该 各 每 一 二 三
进行 通过 随着 基于 根据 针对 关于 对于 不仅 而且 不过 因此 所以 因为 如果 虽然
表示 认为 指出 强调 提出 显示 表明 报道 消息 new 视频 图片 链接 点击
责任编辑 声明 版权 未经 不得 转载 作者 发布 时间 网址""".split())


def _zh_tokenize(text: str) -> List[str]:
    import jieba
    return [w for w in jieba.lcut(str(text or "")) if len(w.strip()) > 1 and w not in _STOPWORDS]


def _build_vectorizer():
    from sklearn.feature_extraction.text import CountVectorizer
    return CountVectorizer(tokenizer=_zh_tokenize, token_pattern=None)


def _llm_name_topic(words: List[str], docs_text: str, brands: List[str] = None) -> str:
    """调本地 LLM（8106 deepseek）给主题起 6~14 字中文名。推理模型常先输出思维链，
    后处理提取标题（书名号/引号优先，否则最后一句短句，并去掉"我们分析…："前缀）。
    brands 为行业重点品牌白名单，注入 prompt 锚定真实品牌，杜绝联想编造。"""
    import re
    import requests
    base_url = str(getattr(config, "INTEL_LLM_BASE_URL", "") or "").strip().rstrip("/") or "http://10.88.0.1:8081/v1"
    model = str(getattr(config, "INTEL_LLM_MODEL", "") or "").strip() or "deepseek-v4-flash"
    try:  # 按部署形态自适应：连通 RAGFlow 用那台的 LLM，否则用本地推理机
        from qa_llm_router import static_endpoint
        base_url, model = static_endpoint(base_url, model)
    except Exception:
        pass
    brand_hint = ("本行业重点品牌：" + "、".join((brands or [])[:20])
                  + "。命名若涉及品牌，应优先使用以上真实品牌名。\n") if brands else ""
    prompt = (
        "任务：为中文新闻主题起标题。\n"
        f"关键词：{', '.join(words[:8])}\n"
        f"代表文档：{docs_text[:500]}\n\n"
        + brand_hint
        + "输出要求：只输出一个 6~14 字的中文标题，概括该主题的实际内容。"
        "必须严格基于上面关键词和文档里真实出现的内容，禁止联想、编造文档未提及的品牌、人物、公司、车型。"
        "禁止输出思考过程、分析、解释、前缀，第一行必须是标题本身。"
    )
    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 220, "temperature": 0},
            timeout=40,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = str(msg.get("content") or msg.get("reasoning_content") or "").strip()
        # 推理模型常先输出思考；提取标题：优先书名号/引号包裹内容，否则取最后一句短句
        brk = re.findall(r"[《【「“\"']([^》】」”\"']{3,20})[》】」”\"']", content)
        name = brk[-1] if brk else ""
        if not name:
            parts = [p.strip() for p in re.split(r"[。\n！？!?]", content) if p.strip()]
            cand = [p for p in parts if re.search(r"[一-鿿]", p) and 3 <= len(p) <= 24]
            name = cand[-1] if cand else (parts[-1] if parts else content)
        name = re.sub(r"^(我们|根据|分析|从|该|这个|以上|标题|因此|所以|综上|注意到|被要求)[^：:]{0,8}[：:]", "", name)
        name = name.strip(" 我们：:，,。.;；、\"'“”‘’《》【】")
        if len(name) > 16:
            name = name[:14]
        # 过滤 LLM 思考过程 / prompt 残留 / 纯英文：只接受含中文、无明显思维链词的短名
        if re.search(r"我们|分析|根据|文档|片段|关键词|用户|起标题|为中文|新闻主题|输出要求|任务[：:]|概括|代表文档|代表文本|注意到|被要求|输入|只输出|中文标题|6-?14|名词短语|字的|突出品牌|主体/事件|品牌/主体|概括主体", name):
            return ""
        if not re.search(r"[一-鿿]", name) or len(name) < 3:
            return ""
        # 防联想：命名的中文词至少一个在文档真实出现，否则视为 LLM 编造，回退
        cn_words = re.findall(r"[一-鿿]{2,}", name)
        if cn_words and not any(w in docs_text for w in cn_words):
            return ""
        return name
    except Exception as e:
        logger.warning("LLM 主题命名失败，回退关键词: %s", e)
        return ""


class BertopicTopicService:
    def __init__(self, repository: IntelRepository = None, composition=None):
        self.repository = repository or IntelRepository()
        self.composition = composition or active_industry_composition_service

    def run(self, *, pack_id: str = "", days_back: int = None, limit: int = 2000) -> Dict:
        """对激活 pack 跑一次 BERTopic 主题聚类 + 命名 + 动态趋势落库。返回统计摘要。"""
        snap = self.composition.snapshot()
        pack_id = str(pack_id or snap.get("active_industry_pack_id") or "").strip()
        activation_id = str(snap.get("active_industry_activation_id") or "")
        brands = (snap.get("primary_pack") or {}).get("brands") or []
        if not pack_id:
            return {"pack_id": "", "topics": 0, "rows": 0}
        days_back = int(days_back if days_back is not None else getattr(config, "INTEL_TREND_WINDOW_DAYS", 90))
        min_topic_size = int(getattr(config, "INTEL_BERTOPIC_MIN_TOPIC_SIZE", 3))
        min_articles = int(getattr(config, "INTEL_BERTOPIC_MIN_ARTICLES", 30))

        # 1. 读 pack 已向量化文章（嵌入 + 正文 + 按天）
        items = self.repository.load_pack_embeddings(
            pack_id=pack_id, days_back=days_back, limit=limit, max_chars=4000
        )
        if len(items) < min_articles:
            logger.info("bertopic: pack=%s 文章不足(%d<%d)，跳过", pack_id, len(items), min_articles)
            return {"pack_id": pack_id, "topics": 0, "rows": 0, "articles": len(items)}

        import numpy as np
        docs = [str(x.get("content") or "") for x in items]
        embeddings = np.vstack([x["embedding"] for x in items])
        timestamps = [str(x.get("day") or "") for x in items]

        # 2. BERTopic 聚类（复用预计算嵌入，不下载模型）
        from bertopic import BERTopic
        topic_model = BERTopic(
            embedding_model=None,
            vectorizer_model=_build_vectorizer(),
            min_topic_size=min_topic_size,
            language="chinese (simplified)",
        )
        topics, _ = topic_model.fit_transform(docs, embeddings)

        # 3. 每主题：LLM 命名 + c-TF-IDF 关键词
        topic_to_docidx: Dict[int, List[int]] = {}
        for i, t in enumerate(topics):
            topic_to_docidx.setdefault(t, []).append(i)
        name_by_topic: Dict[int, str] = {}
        keywords_by_topic: Dict[int, List[str]] = {}
        for tid in topic_to_docidx:
            if tid == -1:
                continue
            kws = [w for w, _ in (topic_model.get_topic(tid) or [])][:10]
            keywords_by_topic[tid] = kws
            idxs = topic_to_docidx[tid][:3]
            docs_text = "\n---\n".join(docs[i][:300] for i in idxs)
            import re as _re
            _n = _llm_name_topic(kws, docs_text, brands)
            if not _n:  # LLM 失败/被过滤 → 回退前 3 个中文关键词拼接；无中文词则用编号（避免英文噪声如 the）
                _cn = [w for w in kws if _re.search(r"[一-鿿]", w) and len(w) >= 2]
                _n = "、".join(_cn[:3]) if _cn else f"主题{tid}"
            name_by_topic[tid] = _n
        if not name_by_topic:
            logger.info("bertopic: pack=%s 未聚出主题", pack_id)
            return {"pack_id": pack_id, "topics": 0, "rows": 0, "articles": len(items)}

        # 4. 主题元信息落 intel_topics（automatic）+ 主题→文章映射（bertopic，供下钻）
        now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
        tid_to_topic_id = self._upsert_topics(pack_id, name_by_topic, keywords_by_topic, now)
        written_topics = len(tid_to_topic_id)
        self._upsert_topic_articles(pack_id, tid_to_topic_id, topics, items)

        # 5. 主题×天热度：直接用每篇文章的主题归属 + 实际发布日计数（比 topics_over_time 时间桶更准）
        per_topic_day: Dict[str, Dict[str, int]] = {}
        for i, tid in enumerate(topics):
            if tid == -1 or tid not in name_by_topic:
                continue
            day = str(timestamps[i] or "")[:10]
            if not day:
                continue
            name = name_by_topic[tid]
            per_topic_day.setdefault(name, {})
            per_topic_day[name][day] = per_topic_day[name].get(day, 0) + 1
        if not per_topic_day:
            return {"pack_id": pack_id, "topics": written_topics, "rows": 0, "articles": len(items)}

        today = get_china_time().date()
        days_sorted = sorted((today - timedelta(days=i)).isoformat() for i in range(days_back))
        window = int(getattr(config, "INTEL_TREND_BURST_WINDOW", 7))
        rows: List[Dict] = []
        for name, daymap in per_topic_day.items():
            series = [daymap.get(d, 0) for d in days_sorted]
            analysis = analyze(series, window)
            for d in days_sorted:
                rows.append({
                    "bucket_date": d,
                    "keyword": name,
                    "article_count": daymap.get(d, 0),
                    "distinct_source_count": 0,
                    "is_burst": 1 if analysis["is_burst"] else 0,
                    "burst_score": analysis["burst_score"],
                    "state": analysis["state"],
                })
        # 先清本包旧的 bertopic 主题趋势，避免历史主题名累积（每次重算主题名会变）
        with self.repository.db.lock:
            cur = self.repository.db.connection.cursor()
            try:
                cur.execute(
                    "DELETE FROM intel_topic_trends WHERE industry_pack_id=? AND dimension=?",
                    (pack_id, BERTOPIC_DIMENSION),
                )
                self.repository.db.connection.commit()
            finally:
                cur.close()
        written = self.repository.upsert_topic_trend_rows(
            industry_pack_id=pack_id, dimension=BERTOPIC_DIMENSION,
            activation_id=activation_id, rows=rows,
        )
        logger.info("bertopic: pack=%s topics=%d trend_rows=%d", pack_id, written_topics, written)
        return {
            "pack_id": pack_id, "topics": written_topics, "rows": written,
            "articles": len(items), "topic_names": list(per_topic_day.keys())[:30],
        }

    def _upsert_topics(self, pack_id, name_by_topic, keywords_by_topic, now) -> Dict[int, int]:
        """主题元信息写入 intel_topics（automatic），重算前清掉本包旧的 automatic 主题。
        返回 {bertopic_tid: intel_topics.id}，供主题→文章映射使用。"""
        tid_to_topic_id: Dict[int, int] = {}
        with self.repository.db.lock:
            cur = self.repository.db.connection.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "DELETE FROM intel_topics WHERE industry_pack_id=? AND topic_source='automatic'",
                    (pack_id,),
                )
                for tid, name in name_by_topic.items():
                    topic_key = f"bertopic-{tid}"
                    cur.execute(
                        """
                        INSERT INTO intel_topics (
                            industry_pack_id, topic_key, topic_name, topic_source,
                            keywords_json, last_clustered_at, created_at, updated_at
                        ) VALUES (?, ?, ?, 'automatic', ?, ?, ?, ?)
                        ON CONFLICT(industry_pack_id, topic_key) DO UPDATE SET
                            topic_name=excluded.topic_name,
                            keywords_json=excluded.keywords_json,
                            last_clustered_at=excluded.last_clustered_at,
                            updated_at=excluded.updated_at
                        """,
                        (pack_id, topic_key, name,
                         json.dumps(keywords_by_topic.get(tid, []), ensure_ascii=False),
                         now, now, now),
                    )
                    row = cur.execute(
                        "SELECT id FROM intel_topics WHERE industry_pack_id=? AND topic_key=?",
                        (pack_id, topic_key),
                    ).fetchone()
                    if row:
                        tid_to_topic_id[tid] = int(row[0])
                self.repository.db.connection.commit()
            except Exception:
                self.repository.db.connection.rollback()
                raise
            finally:
                cur.close()
        return tid_to_topic_id

    def _upsert_topic_articles(self, pack_id, tid_to_topic_id, topics, items) -> int:
        """存主题→文章映射（BERTopic 文档归属），先清本包旧的 bertopic 映射。供主题下钻看文章。"""
        n = 0
        with self.repository.db.lock:
            cur = self.repository.db.connection.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "DELETE FROM intel_topic_articles WHERE assignment_method='bertopic' "
                    "AND topic_id IN (SELECT id FROM intel_topics "
                    "WHERE industry_pack_id=? AND topic_source='automatic')",
                    (pack_id,),
                )
                for i, tid in enumerate(topics):
                    topic_id = tid_to_topic_id.get(tid)
                    if topic_id is None:
                        continue
                    aid = int(items[i].get("article_id") or 0)
                    if aid <= 0:
                        continue
                    cur.execute(
                        "INSERT OR IGNORE INTO intel_topic_articles"
                        "(topic_id, article_id, assignment_method) VALUES (?,?,'bertopic')",
                        (topic_id, aid),
                    )
                    n += 1
                self.repository.db.connection.commit()
            except Exception:
                self.repository.db.connection.rollback()
                raise
            finally:
                cur.close()
        return n


bertopic_topic_service = BertopicTopicService()
