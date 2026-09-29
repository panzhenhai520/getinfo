import asyncio
import inspect
import json

from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from common import settings
from common.constants import LLMType, PAGERANK_FLD, TAG_FLD
from common.doc_store.doc_store_base import OrderByExpr
from rag.nlp.search import index_name
import rag.nlp.search as search_mod


KB_ID = "31af0dfa627611f1b2d61b63bd8d9bb8"
QUESTION = "\u5bb6\u65cf\u4fe1\u6258\u7684\u4e09\u4e2a\u6838\u5fc3\u4ef7\u503c\u662f\u4ec0\u4e48\uff1f"
FIELDS = [
    "docnm_kwd",
    "content_ltks",
    "kb_id",
    "img_id",
    "title_tks",
    "important_kwd",
    "position_int",
    "doc_id",
    "chunk_order_int",
    "page_num_int",
    "top_int",
    "create_timestamp_flt",
    "knowledge_graph_kwd",
    "question_kwd",
    "question_tks",
    "doc_type_kwd",
    "available_int",
    "content_with_weight",
    "mom_id",
    PAGERANK_FLD,
    TAG_FLD,
    "row_id()",
]


def summarize_sres(sres):
    rows = []
    for chunk_id in sres.ids:
        chunk = sres.field.get(chunk_id) or {}
        rows.append(
            {
                "chunk_id": chunk_id,
                "doc_id": chunk.get("doc_id"),
                "docnm_kwd": chunk.get("docnm_kwd"),
                "_score": chunk.get("_score"),
            }
        )
    return rows


def summarize_res(res, fields):
    ids = settings.retriever.dataStore.get_doc_ids(res)
    values = settings.retriever.dataStore.get_fields(res, fields + ["_score"])
    return [
        {
            "chunk_id": chunk_id,
            "doc_id": (values.get(chunk_id) or {}).get("doc_id"),
            "docnm_kwd": (values.get(chunk_id) or {}).get("docnm_kwd"),
            "_score": (values.get(chunk_id) or {}).get("_score"),
        }
        for chunk_id in ids
    ]


async def main():
    if settings.retriever is None:
        settings.init_settings()

    ok, kb = KnowledgebaseService.get_by_id(KB_ID)
    if not ok:
        raise RuntimeError("KB not found")

    if kb.tenant_embd_id:
        embd_model_config = get_model_config_by_id(kb.tenant_embd_id)
    else:
        embd_model_config = get_model_config_by_type_and_name(kb.tenant_id, LLMType.EMBEDDING, kb.embd_id)
    embd_mdl = LLMBundle(kb.tenant_id, embd_model_config)
    idx_names = [index_name(kb.tenant_id)]

    print("module_file", search_mod.__file__)
    source = inspect.getsource(search_mod.Dealer.search)
    print("has_dense_supplement", "dense supplement" in source)
    print("has_loose_supplement", "loose supplement" in source)

    req = {
        "kb_ids": [KB_ID],
        "doc_ids": [],
        "page": 1,
        "size": 30,
        "question": QUESTION,
        "vector": True,
        "topk": 1024,
        "similarity": 0.0,
        "available_int": 1,
    }
    sres = await settings.retriever.search(req, idx_names, [KB_ID], embd_mdl, False, rank_feature={})
    print("raw_search", json.dumps(summarize_sres(sres), ensure_ascii=False, indent=2))

    loose = settings.retriever.dataStore.search(
        FIELDS,
        [],
        {"available_int": 1},
        [],
        OrderByExpr(),
        0,
        64,
        idx_names,
        [KB_ID],
    )
    print("loose_direct", json.dumps(summarize_res(loose, FIELDS), ensure_ascii=False, indent=2))

    ranks = await settings.retriever.retrieval(
        QUESTION,
        embd_mdl,
        [kb.tenant_id],
        [KB_ID],
        1,
        12,
        similarity_threshold=0.0,
        vector_similarity_weight=0.9,
        top=1024,
        doc_ids=[],
        rank_feature={},
    )
    print(
        "retrieval",
        json.dumps(
            [
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "doc_id": chunk.get("doc_id"),
                    "docnm_kwd": chunk.get("docnm_kwd"),
                    "similarity": chunk.get("similarity"),
                    "vector_similarity": chunk.get("vector_similarity"),
                    "term_similarity": chunk.get("term_similarity"),
                }
                for chunk in ranks.get("chunks", [])
            ],
            ensure_ascii=False,
            indent=2,
        ),
    )


asyncio.run(main())
