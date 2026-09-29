# 异步预取 + 分级缓冲 朗读算法（V2：低延时 · 服务端预生成缓存 · 埋点计时）

> V1 只按"下载速度 / 播放码率"前瞻，实测慢 VPN 下仍会"某段播完、下段 TTS 还没好"→ 卡顿。
> V2 改为：**以"实测生成+网络往返延迟 / 实测单段播放时长"为基准的自适应前瞻** + **服务端预生成持久缓存**（命中即秒回）+ **逐段埋点计时落库**，实现流畅朗读同时省 VPN 算力。

---

## 1. 卡点定位（V2 动机）
- 每段 TTS 是"**生成 + VPN 网络往返**"的一次开环，慢网下 `latency >> 单段播放时长`。
- V1 用 `h`(下载字节/秒) 与固定码率 `v=176000` 的比值定前瞻，**把瓶颈当成"带宽"**，而实际是"**单段回路延迟**"。
- 结果：带宽高但回路延迟高时，超前量仍不足 → 播完一段，下一段还没合成好 → 断裂。

---

## 2. V2 关键概念
| 项 | 含义 |
|---|---|
| `latency_i` | 第 i 段 `请求→blob 就绪` 的毫秒（含生成+网络往返），前端 `_dlTimes` 记录 |
| `dur_i` | 第 i 段音频实际播放时长(ms)，`onended` 用 `audio.duration` 记录 `_playDurs` |
| `_adaptiveAhead` | 超前段数，`= ceil(mean(latency)/mean(dur)) + 1`，范围 **2~5**（封顶省算力） |
| `prebuffer` | 初始预取段数，`max(3, _adaptiveAhead())`（不再固定 2） |
| `audio.preload='auto'` | 下段起步解码，降低 `play()` 解码延迟 |
| 服务端缓存 | `intel_tts_service.synthesize` 按 `(text,language)` 持久缓存；`/speech` 命中秒回 |
| 远程预生成 | VPN enrich 产物 `_remote_audio_items`，`fetch_artifact(job_id, artifact)` 秒回 |
| 预生成/预热 | `/speech/prewarm` 提前把前 K 段合成进缓存 |
| 埋点计时 | `article_speech_timing`：后端 `gen_start/gen_end` + 前端 `req_start/ready/play_start/play_end` |

---

## 3. 核心公式
- **无缝条件（V2）**：下一段必须在当前段播完前就绪。
- 当前段播放 `dur` 时间内，可并行完成 `K = ceil(latency/dur) + 1` 段的开环（并发拉取）。
- `lookahead = clamp(ceil(meanLatency/meanDur) + 1, 2, 5)`。
  - 快网（latency<dur）→ 2；慢网（latency≥dur）→ 自动加大到 5。
  - **封顶 5** 避免同时压给 VPN 过多 TTS（省算力）。
- 无论前瞻如何，`/speech/prewarm` 提前把**将要播放的段**合成进缓存，播放时直接命中 → 近零卡顿。

---

## 4. 前端实现（V2，替换 V1 对应函数）

### 4.1 计时 + 自适应前瞻
```js
const articleSpeechBlobCache = new Map(); let _dlTimes = []; let _playDurs = [];
function _adaptiveAhead() {
    const lat = _dlTimes.length ? _dlTimes.reduce((s, x) => s + x.ms, 0) / _dlTimes.length : 0;
    const dur = _playDurs.length ? _playDurs.reduce((s, x) => s + x, 0) / _playDurs.length : 0;
    if (lat <= 0 || dur <= 0) return 3;
    return Math.max(2, Math.min(5, Math.ceil(lat / dur) + 1));
}
```

### 4.2 记录播放时长 + 下段预解码
```js
const audio = new Audio(url); audio.preload = 'auto'; activeArticleAudio = audio;
await new Promise((res, rej) => {
    audio.onended = () => { _playDurs.push((audio.duration || 4) * 1000); if (_playDurs.length > 10) _playDurs.shift(); res(); };
    audio.onerror = () => rej(new Error('播放失败'));
    audio.play().catch(rej);
});
```

### 4.3 初始预取 ≥3
```js
const prebuffer = Math.min(fragments.length, Math.max(3, _adaptiveAhead()));
```

### 4.4 打开即向后端预热（调用服务端预生成缓存）
```js
async function prewarmArticleSpeech(side) {
    const id = document.getElementById('modalTranslateBtn').dataset.articleId; if (!id) return;
    const btn = side === 'source' ? document.getElementById('speakSourceBtn') : document.getElementById('speakTranslationBtn');
    if (btn) { btn.disabled = true; btn.classList.remove('is-ready'); btn.classList.add('is-loading'); }
    try {
        // 让服务端预生成前 6 段并写入持久缓存 → 后续 /speech 命中秒回
        await fetch(`/api/intel/articles/${id}/speech/prewarm`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ side, limit: 6 }) });
        if (window.speechTiming) speechTiming('prewarm_req_start', side, 0);
        const plan = await getArticleSpeechPlan(id, side), fragments = plan.fragments || [];
        const ctrl = new AbortController(), prebuffer = Math.min(fragments.length, Math.max(3, _adaptiveAhead()));
        let i = 0; const worker = async () => { while (i < prebuffer) { const idx = i++; await fetchArticleSpeech(id, side, idx, ctrl.signal); } };
        await Promise.all(Array.from({ length: Math.min(2, prebuffer) }, worker));
        if (btn) { btn.classList.remove('is-loading'); btn.classList.add('is-ready'); btn.style.display = ''; }
    } catch (e) { if (btn) btn.classList.remove('is-loading'); }
    finally { if (btn) btn.disabled = false; }
}
```

### 4.5 播放主循环（V2）
```js
const prebuffer = Math.min(fragments.length, Math.max(3, _adaptiveAhead()));
const queue = []; let requested = 0;
const keepBuffered = (until) => { while (requested < fragments.length && requested < until) { queue[requested] = fetchArticleSpeech(id, side, requested, controller.signal); requested++; } };
const readyAt = [];   // 记录每段就绪/播放开始时间（供埋点）
keepBuffered(prebuffer);
setSpeechBufferState(true, 0, prebuffer);
await Promise.all(queue.slice(0, prebuffer).map((it, i) => it.then(v => { setSpeechBufferState(true, i + 1, prebuffer); return v; })));
setSpeechBufferState(false);
for (let index = 0; index < fragments.length && run === articleSpeechRun; index++) {
    keepBuffered(index + _adaptiveAhead() + 1);
    const fragment = fragments[index];
    const node = (side === 'translation' ? document.getElementById('modalTranslation') : document.getElementById('modalContent'))
        .querySelector(`[data-segment-index="${fragment.article_segment_index}"]`);
    node?.classList.add('is-speaking'); node?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    readyAt[index] = performance.now();
    if (window.speechTiming) speechTiming('play_start', side, index, readyAt[index]);
    const blob = await queue[index]; if (run !== articleSpeechRun) break;
    const url = URL.createObjectURL(blob); const audio = new Audio(url); audio.preload = 'auto'; activeArticleAudio = audio;
    await new Promise((res, rej) => { audio.onended = () => { _playDurs.push((audio.duration || 4) * 1000); if (_playDurs.length > 10) _playDurs.shift(); res(); }; audio.onerror = () => rej(new Error('播放失败')); audio.play().catch(rej); });
    if (window.speechTiming) speechTiming('play_end', side, index, performance.now());
    node?.classList.remove('is-speaking'); URL.revokeObjectURL(url);
}
```

### 4.6 前端埋点上报
```js
function speechTiming(event, side, index, ts) {
    const id = document.getElementById('modalTranslateBtn')?.dataset.articleId; if (!id) return;
    try { fetch(`/api/intel/articles/${id}/speech/timing`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ side, fragment_index: index, event, ts: Math.round(ts || performance.now()) }) }); } catch (e) {}
}
```

---

## 5. 服务端实现（V2）

### 5.1 预生成持久缓存 `/speech/prewarm`
```python
@intel_bp.route("/articles/<int:article_id>/speech/prewarm", methods=["POST"])
@login_required
def prewarm_intel_speech(article_id):
    article = intel_repository.get_article(article_id)
    if not article: raise ValueError('文章不存在或已删除')
    data = request.get_json(silent=True) or {}
    side = str(data.get('side') or 'source').casefold()
    plan = _speech_playback_plan(article_id, article, side)
    fragments = plan['fragments']
    limit = coerce_int(data.get('limit'), 6, 1, max(len(fragments), 1))
    done = 0; gen = 0
    for frag in fragments[:limit]:
        text = frag['text']; lang = 'zh' if any('\u4e00' <= c <= '\u9fff' for c in text) else 'en'
        try:
            _p, cache_hit = intel_tts_service.synthesize(text, lang)   # 命中即不重合成
            done += 1; gen += 0 if cache_hit else 1
        except Exception: pass
    return jsonify({'success': True, 'article_id': article_id, 'side': side,
                    'prewarmed': done, 'synthesized': gen, 'total': len(fragments)})
```

### 5.2 埋点计时 表 + 上报
`article_speech_timing`：`id, article_id, side, fragment_index, event(gen_start/gen_end/req_start/ready/play_start/play_end), ts, created_at`。
- 上报端点：`POST /articles/<id>/speech/timing`（前端 4.6 调用）。
- 读取端点：`GET /articles/<id>/speech/timings`（前端/测试用；后端 `/speech` 在生成时另记录 `X-TTS-Gen-Ms` 头，便于对照）。
- 用途：把"每段开始朗读 / 后端开始生成"的时间精确落库，用于校准 `_adaptiveAhead` 的 `latency/dur` 基准，持续优化。

---

## 6. 关键参数
| 参数 | 默认 | 说明 |
|---|---|---|
| `_adaptiveAhead` | 2~5 | `ceil(latency/dur)+1`，封顶 5 省 VPN 算力 |
| `prebuffer` | `max(3, adaptive)` | 初始预取（V1 固定 2） |
| `_dlTimes`/`_playDurs` 窗口 | 10 | 最近 10 段均值 |
| `/speech/prewarm limit` | 6 | 服务端预生成前 6 段进缓存 |
| TTS 缓存 | 持久 | `synthesize` 按 `(text,lang)` 缓存；命中 `X-TTS-Cache: hit` |

---

## 7. 一句话总结
**V2：按"实测回路延迟/播放时长"自适应超前（封顶 5 段）+ 打开即 `/speech/prewarm` 让服务端预生成缓存 + 逐段 `play_start/gen_start` 埋点落库校准。** 命中缓存秒回、慢网不慌、不过度占用 VPN 算力。
