FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8003 \
    DATABASE_PATH=/app/data/crawler_articles.db \
    CRAWL_RESULTS_DIR=/app/crawl_results \
    AUTH_STORAGE_DIR=/app/auth_storage \
    LOG_FILE=/app/crawl_logs/app.log

WORKDIR /app

COPY requirements.txt requirements-tradingagents.lock ./
COPY vendor/tradingagents/TradingAgents-0.3.1-01477f9.tar.gz ./vendor/tradingagents/TradingAgents-0.3.1-01477f9.tar.gz
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends poppler-utils tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir --no-build-isolation --require-hashes -r requirements-tradingagents.lock \
    # 阶段5：Scrapling StealthyFetcher 浏览器（反爬本地第一梯队）
    && python -m patchright install chromium \
    # playwright 1.40 需要自己的 chromium-1091（与 patchright 的 chromium-1234 并存，
    # 缺失会导致 Playwright 绕过/动态页抓取报 "Executable doesn't exist"）
    && python -m playwright install chromium \
    && python -m pip check

COPY . .

# --- Agent 信源：Agent-Reach + 上游工具（信源关键词抓取） ---
# 说明：与爬虫无关的 playwright/CUDA 不涉及；这里只装信源抓取所需的 CLI 工具。
#   gh/ffmpeg 走 apt；twitter-cli/rdt-cli/bili-cli 走 pipx（PIPX_BIN_DIR 放到 /usr/local/bin 以进入 PATH）。
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gh ffmpeg git python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pipx \
    && pip install --no-cache-dir -e /app/agent_reach_tool \
    && PIPX_BIN_DIR=/usr/local/bin pipx install twitter-cli \
    && PIPX_BIN_DIR=/usr/local/bin pipx install 'git+https://github.com/public-clis/rdt-cli.git@5e4fb3720d5c174e976cd425ccc3b879d52cac66' \
    && PIPX_BIN_DIR=/usr/local/bin pipx install bilibili-cli \
    && pipx ensurepath

ARG DEV_SKIP_ACCEPTANCE=false
RUN if [ "${DEV_SKIP_ACCEPTANCE}" = "true" ]; then \
        sed -i 's/\r$//' /app/docker-entrypoint.sh \
        && chmod +x /app/docker-entrypoint.sh \
        && mkdir -p /app/data /app/crawl_results /app/auth_storage /app/crawl_logs ; \
    else \
        ( python tools/check_tradingagents_supply_chain.py --runtime --output /tmp/tradingagents-acceptance.json \
    && python tools/check_tradingagents_llm_adapter.py --runtime --output /tmp/tradingagents-llm-adapter-acceptance.json \
    && python tools/check_tradingagents_cn_data_adapter.py --runtime --output /tmp/tradingagents-cn-data-adapter-acceptance.json \
    && python tools/check_stock_research_graph.py --runtime --output /tmp/stock-research-graph-acceptance.json \
    && python tools/check_index_market_research_graph.py --runtime --output /tmp/index-market-research-graph-acceptance.json \
    && python tools/check_fund_etf_research.py --runtime --output /tmp/fund-etf-research-acceptance.json \
    && python tools/check_financial_research_persistence.py --runtime --output /tmp/financial-research-persistence-acceptance.json \
    && python tools/check_financial_worker_jobs.py --runtime --output /tmp/financial-worker-jobs-acceptance.json \
    && python tools/check_financial_market_scheduler.py --runtime --output /tmp/financial-market-scheduler-acceptance.json \
    && python tools/check_financial_stage2_integration.py --runtime --output /tmp/financial-stage2-integration-acceptance.json \
    && python tools/check_chat_sse_compatibility.py --runtime --output /tmp/chat-sse-compatibility-acceptance.json \
    && python tools/check_chat_server_time_context.py --runtime --output /tmp/chat-server-time-context-acceptance.json \
    && python tools/check_financial_intent_classifier.py --runtime --output /tmp/financial-intent-classifier-acceptance.json \
    && python tools/check_financial_target_resolver.py --runtime --output /tmp/financial-target-resolver-acceptance.json \
    && python tools/check_financial_chat_market_scope.py --runtime --output /tmp/financial-chat-market-scope-acceptance.json \
    && python tools/check_financial_realtime_query.py --runtime --output /tmp/financial-realtime-query-acceptance.json \
    && python tools/check_financial_full_research.py --runtime --output /tmp/financial-full-research-acceptance.json \
    && python tools/check_financial_sse.py --runtime --output /tmp/financial-sse-acceptance.json \
    && python tools/check_financial_chat_history.py --runtime --output /tmp/financial-chat-history-acceptance.json \
    && python tools/check_financial_gcd_review.py --runtime --output /tmp/financial-gcd-review-acceptance.json \
    && python tools/check_financial_synthesis_review.py --runtime --output /tmp/financial-synthesis-review-acceptance.json \
    && python tools/check_financial_conflict_adjudication.py --runtime --output /tmp/financial-conflict-adjudication-acceptance.json \
    && python tools/check_financial_feed.py --database /app/data/crawler_articles.db --industry-pack-id family_office --time-range 7d \
    && python tools/check_financial_report_view.py --runtime --output /tmp/financial-report-view-acceptance.json \
    && python tools/check_financial_feature_gate.py --runtime --output /tmp/financial-feature-gate-acceptance.json \
    && python tools/check_financial_stage3_gate.py --runtime --output /tmp/financial-stage3-acceptance.json \
    && python tools/check_financial_claim_extractor.py --runtime --output /tmp/financial-claim-extractor-acceptance.json \
    && python tools/check_financial_temporal_judge.py --runtime --output /tmp/financial-temporal-judge-acceptance.json \
    && python tools/check_financial_conflict_judge.py --runtime --output /tmp/financial-conflict-judge-acceptance.json \
    && python tools/check_financial_answer_composer.py --runtime --output /tmp/financial-answer-composer-acceptance.json \
    && python tools/check_financial_rag_gate.py --runtime --output /tmp/financial-rag-gate-acceptance.json \
    && python tools/check_financial_stage4_gate.py --runtime --output /tmp/financial-stage4-gate-acceptance.json \
    && python tools/check_financial_simulation_gate.py --runtime --output /tmp/financial-simulation-gate-acceptance.json \
    && python tools/check_financial_paper_trading.py --runtime --output /tmp/financial-paper-trading-acceptance.json \
    && python tools/check_financial_backtest.py --runtime --output /tmp/financial-backtest-acceptance.json \
    && python tools/check_financial_backtest_metrics.py --runtime --output /tmp/financial-backtest-metrics-acceptance.json \
    && python tools/check_financial_simulation_view.py --runtime --output /tmp/financial-simulation-view-acceptance.json \
    && python tools/check_financial_stage5_gate.py --runtime --output /tmp/financial-stage5-gate-acceptance.json \
    && python tools/check_financial_source_licenses.py --runtime --output /tmp/financial-source-license-acceptance.json \
    && python tools/check_financial_dashboard_xss.py --output /tmp/financial-dashboard-xss.json \
    && python tools/check_financial_security.py --runtime --browser-report /tmp/financial-dashboard-xss.json --output /tmp/financial-security-acceptance.json \
    && python tools/check_financial_resource_isolation.py --runtime --output /tmp/financial-resource-isolation-acceptance.json \
    && python tools/check_financial_health.py --runtime --output /tmp/financial-health-acceptance.json \
    && python tools/check_financial_rollout.py --runtime --output /tmp/financial-rollout-acceptance.json \
    && python tools/check_financial_recovery.py --runtime --output /tmp/financial-recovery-acceptance.json \
    && python tools/check_financial_latest_information.py --fixture tests/fixtures/financial_latest_information_golden.json --network none --output /tmp/financial-latest-information-acceptance.json \
    && python tools/check_financial_latest_release_readiness.py --fixture tests/fixtures/financial_latest_information_golden.json --network none --output /tmp/financial-latest-release-readiness.json \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/data /app/crawl_results /app/auth_storage /app/crawl_logs \
    ) > /tmp/docker-acceptance.log 2>&1 \
    || { tail -n 120 /tmp/docker-acceptance.log; exit 1; } ; \
    fi

EXPOSE 8003

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "start_with_schedule.py"]
