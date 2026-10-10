#!/bin/bash
# 采集回填值班（A 机常驻）
#   1) 白天 **0 并发**（完全不跑）—— **机器人优先**：两台共用同一台 GPU 机的 llama.cpp（-np 2），
#      2026-10-09 实测：回填占着槽位时机器人语音链「用户说完→LLM 首句」13~44 秒、整轮 45~78 秒；
#      把回填停掉后同一台 27B 的裸时延只要 首 token 0.9~1.7 秒（短 prompt）/ 3.3 秒（2000 字 prompt）。
#      结论：连"白天 1 并发"都会让机器人明显卡顿，所以白天直接不跑。
#   2) 深夜 03:00-06:00 放开到 **1 并发** —— 该时段基本不会有人跟机器人对话；
#      覆盖率是长跑指标，慢一点可以接受，机器人体验不能牺牲。
#      （Ollama 那条"给爬虫单独一个实例"的路已实测走不通：同款 27B 因 8081 已占显存而 OOM，
#        gemma431b-32k 只有 6.3 tok/s，比 27B 还慢。）
#   3) 回填进程挂了自动拉起（断点续跑，不会重复烧 LLM）
#   4) 每天 08:00 自动跑一次检索验收并留档（含覆盖率与图规模快照）
# 全部参数可用环境变量覆盖；要停就 `pkill -f crawl_supervisor.sh`，回填进程不受影响。
set -u

DAY_WORKERS=${DAY_WORKERS:-0}
NIGHT_WORKERS=${NIGHT_WORKERS:-1}
NIGHT_START=${NIGHT_START:-3}
NIGHT_END=${NIGHT_END:-6}
ACCEPT_HOUR=${ACCEPT_HOUR:-8}
QUESTIONS=/app/data/qa_acceptance_questions.json
HISTORY=/app/data/qa_acceptance_history.jsonl
CONTAINER=collectinfo-web

log() { echo "$(date '+%F %T') $*"; }

# 当前回填进程的并发数（空=没在跑）；刷新属性模式不动它，让它自然跑完
current_workers() {
  docker exec "$CONTAINER" ps -eo args 2>/dev/null \
    | grep "backfill_article_events.py --apply" | grep -v grep | head -1 \
    | sed -n 's/.*--workers \([0-9][0-9]*\).*/\1/p'
}
refreshing() {
  docker exec "$CONTAINER" ps -eo args 2>/dev/null \
    | grep "backfill_article_events.py --apply" | grep -v grep | grep -q "refresh-attributes"
}

start_backfill() {
  local workers="$1"
  docker exec -d -e INTEL_LLM_ENABLED=true "$CONTAINER" bash -lc \
    "cd /app && nohup python tools/backfill_article_events.py --apply --workers $workers --max-minutes 240 > /app/data/backfill_events.log 2>&1 < /dev/null &"
  log "已以 $workers 并发拉起主回填"
}

log "值班启动：白天 $DAY_WORKERS 并发 / 夜间($NIGHT_START:00-$NIGHT_END:00) $NIGHT_WORKERS 并发 / $ACCEPT_HOUR:00 自动验收"
last_accept_date=""

while true; do
  hour=$(date +%-H)
  if [ "$hour" -ge "$NIGHT_START" ] && [ "$hour" -lt "$NIGHT_END" ]; then
    want="$NIGHT_WORKERS"
  else
    want="$DAY_WORKERS"
  fi

  if refreshing; then
    log "补属性模式在跑（非主回填），本轮不动它"
  else
    have=$(current_workers)
    if [ -z "$have" ]; then
      log "主回填未在跑，以 $want 并发拉起"
      start_backfill "$want"
    elif [ "$have" != "$want" ]; then
      log "时段切换：并发 $have → $want"
      docker exec "$CONTAINER" pkill -f "tools/backfill_article_events.py" >/dev/null 2>&1
      sleep 3
      start_backfill "$want"
    fi
  fi

  today=$(date +%F)
  if [ "$hour" = "$ACCEPT_HOUR" ] && [ "$last_accept_date" != "$today" ]; then
    log "开始每日验收留档"
    if [ ! -f "$QUESTIONS" ]; then
      # 问题集不存在就先按语料生成（同样在容器内落盘）
      docker exec "$CONTAINER" bash -lc \
        "cd /app && python tools/qa_retrieval_acceptance.py --generate 24 --out $QUESTIONS >> /app/data/acceptance_gen.log 2>&1" || true
    fi
    # 注意：日志重定向必须在**容器内**（宿主机上没有 /app/data）
    if docker exec "$CONTAINER" bash -lc \
        "cd /app && python tools/qa_retrieval_acceptance.py --questions $QUESTIONS --save-history $HISTORY > /app/data/acceptance_${today}.log 2>&1"; then
      last_accept_date="$today"
      log "验收已留档 → $HISTORY（明细 /app/data/acceptance_${today}.log）"
    else
      log "验收失败，见容器内 /app/data/acceptance_${today}.log"
    fi
  fi

  sleep 600
done
