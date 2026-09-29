$Base = 'D:\docker-data\ragflow\ragflow-v0.25.6-upgrade'
$Log = Join-Path $Base 'chat-stuck-diagnose-246.log'
Set-Location $Base
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Set-Content -LiteralPath $Log -Encoding UTF8

function Add-Section {
  param([string]$Name)
  Add-Content -LiteralPath $Log -Value "=== $Name ===" -Encoding UTF8
}

function Run-Capture {
  param(
    [string]$Name,
    [scriptblock]$Script
  )
  Add-Section $Name
  try {
    & $Script 2>&1 | Out-String -Width 400 | Add-Content -LiteralPath $Log -Encoding UTF8
  } catch {
    Add-Content -LiteralPath $Log -Value $_.Exception.ToString() -Encoding UTF8
  }
}

Run-Capture 'compose ps' { docker compose ps }
Run-Capture 'copy ollama test json' { docker cp "$Base\ollama-chat-test.json" 'ragflow-v0256-upgrade-ragflow-cpu-1:/tmp/ollama-chat-test.json' }
Run-Capture 'container curl ollama tags' { docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc 'curl -sS -m 10 http://host.docker.internal:11434/api/tags' }
Run-Capture 'container curl ollama chat' { docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "curl -sS -m 120 -H 'Content-Type: application/json' --data-binary @/tmp/ollama-chat-test.json http://host.docker.internal:11434/api/chat" }
Run-Capture 'recent ragflow_server filtered errors' { docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "tail -n 500 /ragflow/logs/ragflow_server.log | grep -Ei 'chat/completions|completion|ollama|litellm|connection|error|exception|traceback|unauthorized|deepseek|xinference|timeout' || true" }
Run-Capture 'recent docker logs' { docker compose logs --tail=500 ragflow-cpu }
Run-Capture 'active dialog model rows' { docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,tenant_llm_id,rerank_id,tenant_rerank_id,status FROM dialog ORDER BY update_time DESC LIMIT 30;" }
Run-Capture 'session tables' { docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW TABLES LIKE '%session%'; SHOW TABLES LIKE '%conversation%';" }

Add-Content -LiteralPath $Log -Value 'EXIT 0' -Encoding UTF8
