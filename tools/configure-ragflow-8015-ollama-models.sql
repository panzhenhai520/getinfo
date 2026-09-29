SET @now_ms = FLOOR(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(3)) * 1000);
SET @api_base = 'http://host.docker.internal:11434';
SET @chat_model = 'deepseek-r1:1.5b';
SET @embed_model = 'bge-m3:latest';
SET @chat_model_id = CONCAT(@chat_model, '@Ollama');
SET @embed_model_id = CONCAT(@embed_model, '@Ollama');

INSERT INTO llm_factories
  (name, create_time, create_date, update_time, update_date, logo, tags, `rank`, status)
VALUES
  ('Ollama', @now_ms, NOW(), @now_ms, NOW(), '', 'LLM,Embedding', 90, '1')
ON DUPLICATE KEY UPDATE
  update_time = VALUES(update_time),
  update_date = VALUES(update_date),
  status = '1';

INSERT INTO llm
  (create_time, create_date, update_time, update_date, llm_name, model_type, fid, max_tokens, tags, is_tools, status)
VALUES
  (@now_ms, NOW(), @now_ms, NOW(), @chat_model, 'chat', 'Ollama', 8192, '', 0, '1'),
  (@now_ms, NOW(), @now_ms, NOW(), @embed_model, 'embedding', 'Ollama', 8192, '', 0, '1')
ON DUPLICATE KEY UPDATE
  update_time = VALUES(update_time),
  update_date = VALUES(update_date),
  model_type = VALUES(model_type),
  max_tokens = VALUES(max_tokens),
  status = '1';

INSERT INTO tenant_llm
  (create_time, create_date, update_time, update_date, tenant_id, llm_factory, model_type, llm_name, api_key, api_base, max_tokens, used_tokens, status)
SELECT
  @now_ms, NOW(), @now_ms, NOW(), id, 'Ollama', 'chat', @chat_model, 'x', @api_base, 8192, 0, '1'
FROM tenant
ON DUPLICATE KEY UPDATE
  update_time = VALUES(update_time),
  update_date = VALUES(update_date),
  model_type = VALUES(model_type),
  api_key = VALUES(api_key),
  api_base = VALUES(api_base),
  max_tokens = VALUES(max_tokens),
  status = '1';

INSERT INTO tenant_llm
  (create_time, create_date, update_time, update_date, tenant_id, llm_factory, model_type, llm_name, api_key, api_base, max_tokens, used_tokens, status)
SELECT
  @now_ms, NOW(), @now_ms, NOW(), id, 'Ollama', 'embedding', @embed_model, 'x', @api_base, 8192, 0, '1'
FROM tenant
ON DUPLICATE KEY UPDATE
  update_time = VALUES(update_time),
  update_date = VALUES(update_date),
  model_type = VALUES(model_type),
  api_key = VALUES(api_key),
  api_base = VALUES(api_base),
  max_tokens = VALUES(max_tokens),
  status = '1';

UPDATE tenant t
JOIN tenant_llm chat
  ON chat.tenant_id = t.id
 AND chat.llm_factory = 'Ollama'
 AND chat.llm_name = @chat_model
 AND chat.model_type = 'chat'
JOIN tenant_llm emb
  ON emb.tenant_id = t.id
 AND emb.llm_factory = 'Ollama'
 AND emb.llm_name = @embed_model
 AND emb.model_type = 'embedding'
SET
  t.llm_id = @chat_model_id,
  t.tenant_llm_id = chat.id,
  t.embd_id = @embed_model_id,
  t.tenant_embd_id = emb.id,
  t.update_time = @now_ms,
  t.update_date = NOW();

UPDATE knowledgebase kb
JOIN tenant_llm emb
  ON emb.tenant_id = kb.tenant_id
 AND emb.llm_factory = 'Ollama'
 AND emb.llm_name = @embed_model
 AND emb.model_type = 'embedding'
SET
  kb.embd_id = @embed_model_id,
  kb.tenant_embd_id = emb.id,
  kb.update_time = @now_ms,
  kb.update_date = NOW()
WHERE
  kb.tenant_embd_id IS NULL OR kb.embd_id IS NULL OR kb.embd_id = '';

UPDATE dialog d
JOIN tenant_llm chat
  ON chat.tenant_id = d.tenant_id
 AND chat.llm_factory = 'Ollama'
 AND chat.llm_name = @chat_model
 AND chat.model_type = 'chat'
SET
  d.llm_id = @chat_model_id,
  d.tenant_llm_id = chat.id,
  d.update_time = @now_ms,
  d.update_date = NOW()
WHERE
  d.tenant_llm_id IS NULL OR d.llm_id IS NULL OR d.llm_id = '';

SELECT id, name, llm_id, embd_id, tenant_llm_id, tenant_embd_id
FROM tenant;

SELECT id, tenant_id, llm_factory, llm_name, model_type, api_base, api_key, status, max_tokens
FROM tenant_llm
WHERE llm_factory = 'Ollama'
ORDER BY tenant_id, model_type, llm_name;
