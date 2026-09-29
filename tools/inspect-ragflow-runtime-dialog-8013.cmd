@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\runtime-dialog-snippet.log
cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% runtime dialog_service grep ==== > "%LOG%"
docker compose exec -T ragflow-cpu grep -n "stream_prefix.strip\|_strip_transient_refusal_prefix" /ragflow/api/db/services/dialog_service.py >> "%LOG%" 2>&1

echo ==== %date% %time% runtime search.py grep ==== >> "%LOG%"
docker compose exec -T ragflow-cpu grep -n "dense supplement\|loose supplement\|_merge_es_hits" /ragflow/rag/nlp/search.py >> "%LOG%" 2>&1

echo ==== %date% %time% runtime settings ==== >> "%LOG%"
docker compose exec -T ragflow-cpu python3 -c "from common import settings; print('DOC_ENGINE_INFINITY=', settings.DOC_ENGINE_INFINITY); print('DOC_ENGINE_OCEANBASE=', settings.DOC_ENGINE_OCEANBASE); print('DOC_ENGINE_ES=', getattr(settings, 'DOC_ENGINE_ES', None)); print('DOC_ENGINE=', getattr(settings, 'DOC_ENGINE', None))" >> "%LOG%" 2>&1

echo ==== %date% %time% runtime dataset route grep ==== >> "%LOG%"
docker compose exec -T ragflow-cpu grep -R -n "datasets/<dataset_id>/search\|datasets/search" /ragflow/api/apps/restful_apis /ragflow/internal >> "%LOG%" 2>&1

echo EXIT %ERRORLEVEL% >> "%LOG%"
endlocal
