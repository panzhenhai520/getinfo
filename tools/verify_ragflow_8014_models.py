import asyncio
import json
import traceback

from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from common import settings
from common.constants import LLMType


def compact_config(cfg):
    return {
        "id": cfg.get("id"),
        "tenant_id": cfg.get("tenant_id"),
        "llm_factory": cfg.get("llm_factory"),
        "llm_name": cfg.get("llm_name"),
        "model_type": cfg.get("model_type"),
        "api_base": cfg.get("api_base"),
        "api_key": cfg.get("api_key"),
        "status": cfg.get("status"),
        "max_tokens": cfg.get("max_tokens"),
    }


async def main():
    settings.init_settings()
    print("settings_defaults", json.dumps({
        "CHAT_CFG": settings.CHAT_CFG,
        "EMBEDDING_CFG": settings.EMBEDDING_CFG,
    }, ensure_ascii=False))

    tenants = TenantService.get_all()
    if not tenants:
        raise RuntimeError("No RAGFlow tenant found")

    tenant = tenants[0]
    print("tenant", tenant.id, tenant.name)
    print("tenant_defaults", json.dumps({
        "llm_id": tenant.llm_id,
        "embd_id": tenant.embd_id,
        "tenant_llm_id": tenant.tenant_llm_id,
        "tenant_embd_id": tenant.tenant_embd_id,
    }, ensure_ascii=False))

    chat_cfg = TenantLLMService.get_model_config(tenant.id, LLMType.CHAT.value)
    emb_cfg = TenantLLMService.get_model_config(tenant.id, LLMType.EMBEDDING.value)
    print("chat_config", json.dumps(compact_config(chat_cfg), ensure_ascii=False))
    print("embedding_config", json.dumps(compact_config(emb_cfg), ensure_ascii=False))

    emb = LLMBundle(tenant.id, emb_cfg)
    vector, emb_tokens = emb.encode_queries("测试本地 bge 向量模型")
    print("embedding_ok", json.dumps({
        "length": int(len(vector)),
        "used_tokens": int(emb_tokens),
        "first3": [float(x) for x in vector[:3]],
    }, ensure_ascii=False))

    chat = LLMBundle(tenant.id, chat_cfg)
    answer = await chat.async_chat(
        "",
        [{"role": "user", "content": "Reply with exactly OK and no explanation."}],
        {"temperature": 0.0, "max_tokens": 128, "think": False},
        with_reasoning=False,
    )
    print("chat_ok", json.dumps({"answer": answer}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        raise
