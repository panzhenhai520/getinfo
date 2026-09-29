SET NAMES utf8mb4;

SELECT 'before_tenant_llm' AS section;
SELECT id, tenant_id, llm_factory, model_type, llm_name, api_base, status
FROM tenant_llm
ORDER BY id;

SELECT 'before_tenant' AS section;
SELECT id, name, llm_id, tenant_llm_id, embd_id, tenant_embd_id, rerank_id, tenant_rerank_id, status
FROM tenant
ORDER BY id;

SELECT 'before_bad_dialogs' AS section;
SELECT id, tenant_id, name, llm_id, tenant_llm_id, rerank_id, tenant_rerank_id, status
FROM dialog
WHERE llm_id IN ('/home/xsuper/models/DeepSeek-R1-Distill-Llama-70B@Xinference', 'glm4:9b@Ollama')
   OR rerank_id = 'bge-reranker-large@Xinference'
ORDER BY update_time DESC;

UPDATE tenant_llm
SET api_base = 'http://host.docker.internal:11434'
WHERE llm_factory = 'Ollama'
  AND api_base = 'http://host.docker.internal:11434/v1';
SELECT ROW_COUNT() AS updated_ollama_api_base;

UPDATE tenant AS tenant_row
JOIN tenant_llm AS chat_model
  ON chat_model.tenant_id = tenant_row.id
 AND chat_model.llm_factory = 'Ollama'
 AND chat_model.model_type = 'chat'
 AND chat_model.llm_name = 'deepseek-r1:1.5b'
SET tenant_row.tenant_llm_id = chat_model.id
WHERE tenant_row.llm_id = 'deepseek-r1:1.5b@Ollama'
  AND (tenant_row.tenant_llm_id IS NULL OR tenant_row.tenant_llm_id <> chat_model.id);
SELECT ROW_COUNT() AS updated_tenant_chat_fk;

UPDATE dialog AS dialog_row
JOIN tenant_llm AS chat_model
  ON chat_model.tenant_id = dialog_row.tenant_id
 AND chat_model.llm_factory = 'Ollama'
 AND chat_model.model_type = 'chat'
 AND chat_model.llm_name = 'deepseek-r1:1.5b'
SET dialog_row.llm_id = 'deepseek-r1:1.5b@Ollama',
    dialog_row.tenant_llm_id = chat_model.id
WHERE dialog_row.status = '1'
  AND dialog_row.llm_id IN ('/home/xsuper/models/DeepSeek-R1-Distill-Llama-70B@Xinference', 'glm4:9b@Ollama');
SELECT ROW_COUNT() AS updated_active_dialog_chat_model;

UPDATE dialog
SET rerank_id = '',
    tenant_rerank_id = NULL
WHERE status = '1'
  AND rerank_id = 'bge-reranker-large@Xinference';
SELECT ROW_COUNT() AS cleared_active_dialog_rerank;

UPDATE tenant
SET rerank_id = '',
    tenant_rerank_id = NULL
WHERE rerank_id = 'bge-reranker-large@Xinference';
SELECT ROW_COUNT() AS cleared_tenant_rerank;

SELECT 'after_tenant_llm' AS section;
SELECT id, tenant_id, llm_factory, model_type, llm_name, api_base, status
FROM tenant_llm
ORDER BY id;

SELECT 'after_tenant' AS section;
SELECT id, name, llm_id, tenant_llm_id, embd_id, tenant_embd_id, rerank_id, tenant_rerank_id, status
FROM tenant
ORDER BY id;

SELECT 'after_bad_dialogs' AS section;
SELECT id, tenant_id, name, llm_id, tenant_llm_id, rerank_id, tenant_rerank_id, status
FROM dialog
WHERE llm_id IN ('/home/xsuper/models/DeepSeek-R1-Distill-Llama-70B@Xinference', 'glm4:9b@Ollama')
   OR rerank_id = 'bge-reranker-large@Xinference'
ORDER BY update_time DESC;
