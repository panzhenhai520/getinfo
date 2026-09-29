/*!
 * 行业事件时间线（第二阶段）—— 趋势演化区"事件时间线"tab
 * switchIntelTrendTab 切 tab + lazy load 事件；点击事件卡片复用趋势下钻 modal。
 */
(function () {
    "use strict";
    if (typeof window === "undefined") return;

    var STATE_NAMES = { BURSTING: "爆发", RISING: "上升", MATURE: "成熟", DECLINING: "衰退", EMERGING: "新生" };
    var TYPE_NAMES = {
        regulation: "监管", enforcement: "执法", release: "发布",
        market: "市场", transaction: "交易", other: "其他",
    };

    function packId() {
        return (window.state && window.state.intelIndustryPackId) || "";
    }
    function esc(x) { return (window.escapeHtml || String)(x == null ? "" : x); }

    window.switchIntelTrendTab = function (pane) {
        var keywordPane = document.getElementById("intelTrendPaneKeyword");
        var eventPane = document.getElementById("intelTrendPaneEvent");
        var tabs = document.querySelectorAll(".intel-trend-tab");
        tabs.forEach(function (t) {
            t.classList.toggle("active", t.getAttribute("data-pane") === pane);
        });
        if (pane === "topic") {
            // 主题趋势 = 主体+事件时间线（事件主体作为主题）
            if (keywordPane) keywordPane.hidden = true;
            if (eventPane) eventPane.hidden = false;
            window.loadIntelSubjects();
        } else {
            // 品牌趋势（echarts 图表）
            if (eventPane) eventPane.hidden = true;
            if (keywordPane) keywordPane.hidden = false;
            window.loadIntelTrendEvolution("brand");
        }
    };

    window.loadIntelEvents = function () {
        var container = document.getElementById("intelTrendEvents");
        if (!container) return;
        container.innerHTML = '<div class="intel-trend-empty">正在加载事件…</div>';
        var params = new URLSearchParams({
            industry_pack_id: packId(), days: "90", top: "30", min_articles: "1",
        });
        fetch("/api/intel/events?" + params.toString(), { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                window.__intelEventsLoaded = true;
                if (!data.success) throw new Error(data.error || "加载失败");
                renderIntelEvents(container, data.events || []);
            })
            .catch(function (err) {
                container.innerHTML = '<div class="intel-trend-empty">事件加载失败：' + esc(err.message) + "</div>";
            });
    };

    window.switchEventSubTab = function (sub) {
        var flowWrap = document.getElementById("intelEventFlowWrap");
        var subjWrap = document.getElementById("intelSubjectWrap");
        var subtabs = document.querySelectorAll(".intel-trend-subtab");
        subtabs.forEach(function (t) {
            t.classList.toggle("active", t.getAttribute("data-sub") === sub);
        });
        if (sub === "subject") {
            if (flowWrap) flowWrap.hidden = true;
            if (subjWrap) subjWrap.hidden = false;
            window.loadIntelSubjects();
        } else {
            if (subjWrap) subjWrap.hidden = true;
            if (flowWrap) flowWrap.hidden = false;
        }
    };

    window.loadIntelSubjects = function (opts) {
        opts = opts || {};
        var list = document.getElementById("intelSubjectList");
        var detail = document.getElementById("intelSubjectDetail");
        if (!list) return;
        var pack = packId();
        var cache = window.__intelSubjectsCache;
        // 有缓存且同 pack → 直接渲染（切 tab 秒开）
        if (cache && cache.pack === pack && cache.subjects && !opts.force) {
            if (detail) detail.innerHTML = "";
            renderIntelSubjects(list, cache.subjects);
            return;
        }
        // 加载动画（复用 AI 助手历史会话动画）
        list.innerHTML = '<div class="history-load-card"><div class="history-load-animation" aria-hidden="true"><span class="history-load-icon"><i class="fa fa-comments"></i></span></div><strong>正在加载事件时间线</strong><span>聚合主体与事件中…</span></div>';
        if (detail) detail.innerHTML = "";
        var params = new URLSearchParams({
            industry_pack_id: pack, days: "90", top: "20", min_articles: "1",
        });
        fetch("/api/intel/subjects?" + params.toString(), { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || "加载失败");
                window.__intelSubjectsCache = { pack: pack, subjects: data.subjects || [] };
                renderIntelSubjects(list, data.subjects || []);
            })
            .catch(function (err) {
                list.innerHTML = '<div class="intel-trend-empty">主体加载失败：' + esc(err.message) + "</div>";
            });
    };

    var selectedSubjectKey = null;
    var selectedEventHash = "";
    var selectedEventDesc = "";
    var timelineMode = "vertical";

    // —— 横向时间轴节点 hover 命中状态（防抖 + 切主体重置）——
    var hoverIdx = -1;
    var hoverRAF = null;

    // 切主体/重渲染时重置 hover 命中状态
    function hideEventHover() {
        if (hoverRAF) { cancelAnimationFrame(hoverRAF); hoverRAF = null; }
        hoverIdx = -1;
    }

    function renderIntelSubjects(container, subjects) {
        if (!subjects.length) {
            container.innerHTML = '<div class="intel-trend-empty">暂无主体（worker 后台回填中）</div>';
            return;
        }
        var visible = subjects.filter(function (s) { return (s.event_count || 0) > 1; });
        window.__intelSubjects = visible.length ? visible : subjects.slice(0, 5);
        var list = document.getElementById("intelSubjectList");
        var detail = document.getElementById("intelSubjectDetail");
        if (!list || !detail) return;
        list.innerHTML = window.__intelSubjects.map(function (s) {
            var sn = STATE_NAMES[s.state] || s.state;
            var stateCls = (s.state || "EMERGING").toLowerCase();
            var typeBadge = s.subject_type === 'topic' ? '话题' : '主体';
            return '<div class="intel-subject-item' + (s.subject_key === selectedSubjectKey ? " is-active" : "") + '" data-key="' + esc(s.subject_key) + '" onclick="selectSubject(\'' + esc(s.subject_key) + '\')">'
                +   '<span class="intel-trend-chip is-' + stateCls + '">' + esc(sn) + '</span>'
                +   '<span class="intel-subject-type-badge is-' + (s.subject_type || 'entity') + '">' + typeBadge + '</span>'
                +   '<div class="intel-subject-item-name">' + esc(s.subject) + '</div>'
                +   '<div class="intel-event-count">' + s.article_count + ' 篇 · ' + s.event_count + ' 事件</div>'
                + '</div>';
        }).join("");
        var hasSel = subjects.some(function (s) { return s.subject_key === selectedSubjectKey; });
        if (!hasSel) selectedSubjectKey = subjects[0].subject_key;
        list.querySelectorAll(".intel-subject-item").forEach(function (it) {
            it.classList.toggle("is-active", it.getAttribute("data-key") === selectedSubjectKey);
        });
        renderSubjectTimeline(detail, subjects.find(function (s) { return s.subject_key === selectedSubjectKey; }));
    }

    window.selectSubject = function (key) {
        selectedSubjectKey = key;
        var items = document.querySelectorAll(".intel-subject-item");
        items.forEach(function (it) {
            it.classList.toggle("is-active", it.getAttribute("data-key") === key);
        });
        var subjects = window.__intelSubjects || [];
        var s = subjects.find(function (x) { return x.subject_key === key; });
        var detail = document.getElementById("intelSubjectDetail");
        if (s && detail) renderSubjectTimeline(detail, s);
    };

    window.reclusterEvents = function () {
        var hash = selectedEventHash;
        var desc = selectedEventDesc || "";
        if (!hash) {
            alert("请先点击时间线上的一个事件选中它，再点【调整分类】");
            return;
        }
        // 人工调整分类：输入新主体名，直接改事件归属（不走 LLM 算法）
        var newSubject = prompt("调整该事件的归属主体（分类）：\n\n\"" + desc + "\"\n\n请输入新的主体名称：");
        if (newSubject === null) return;
        newSubject = newSubject.trim();
        if (!newSubject) { alert("主体名称不能为空"); return; }
        var box = document.getElementById("intelEventArticles");
        if (box) box.innerHTML = '<div class="intel-trend-empty">正在调整分类…</div>';
        var btn = window.event && window.event.target;
        var orig = btn ? btn.textContent : "调整分类";
        if (btn) { btn.textContent = "调整中…"; btn.disabled = true; }
        fetch("/api/intel/events/adjust", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({industry_pack_id: packId(), event_hash: hash, subject: newSubject})
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (btn) { btn.textContent = orig; btn.disabled = false; }
            if (!data.success) throw new Error(data.error || "失败");
            selectedEventHash = ""; selectedEventDesc = "";
            window.__intelSubjectsCache = null;
            window.loadIntelSubjects({force: true});
            if (box) box.innerHTML = '<div class="intel-trend-empty">已调整到「' + newSubject + '」，点击左侧事件查看相关文章</div>';
        }).catch(function (err) {
            if (btn) { btn.textContent = orig; btn.disabled = false; }
            alert("调整失败：" + (err.message || "未知错误"));
        });
    };

    window.selectEvent = function (hash, desc) {
        selectedEventHash = hash;
        selectedEventDesc = desc || "";
        document.querySelectorAll(".intel-tl-row").forEach(function (r) { r.classList.remove("is-selected"); });
        var el = window.event && window.event.currentTarget;
        if (el && el.classList) el.classList.add("is-selected");
        window.openEventArticles(hash);
    };

    window.switchTimelineMode = function () {
        timelineMode = (timelineMode === "horizontal") ? "vertical" : "horizontal";
        var btn = window.event && window.event.target;
        if (btn) btn.textContent = (timelineMode === "horizontal") ? "返回" : "时间线";
        // 横向时间轴与文章卡片始终保持左右两栏：hover/点击节点即在右侧出文章卡
        var arts = document.getElementById("intelEventArticles");
        if (arts) arts.style.display = "";   // 清除历史 inline 隐藏，确保文章区始终可见
        var subjects = window.__intelSubjects || [];
        var s = subjects.find(function (x) { return x.subject_key === selectedSubjectKey; });
        var detail = document.getElementById("intelSubjectDetail");
        if (s && detail) renderSubjectTimeline(detail, s);
    };

    function renderSubjectTimelineHorizontal(container, subject) {
        if (typeof echarts === "undefined" || !container || !subject) return;
        hideEventHover();  // 切主体/重渲染时重置 hover 命中状态
        var stateCls = (subject.state || "EMERGING").toLowerCase();
        var sn = STATE_NAMES[subject.state] || subject.state;
        var events = (subject.events || []).slice().sort(function (a, b) {
            return (a.last_time || "").localeCompare(b.last_time || "");
        });
        if (!events.length) {
            container.innerHTML = '<div class="intel-trend-empty">该主体暂无事件</div>';
            return;
        }
        // 同一天多个事件围绕中线轻微上下错开，避免重叠（整体仍集中在中部）
        var dayCount = {}, dayIdx = {};
        events.forEach(function (ev) { dayCount[ev.last_time] = (dayCount[ev.last_time] || 0) + 1; });
        var scatterData = events.map(function (ev) {
            var t = ev.last_time || "2000-01-01";
            var n = dayCount[t] || 1;
            var i = dayIdx[t] || 0;
            dayIdx[t] = i + 1;
            var y = n === 1 ? 0 : (i - (n - 1) / 2) * 0.4;
            return {
                value: [t, y],
                event_hash: ev.event_hash,
                description: ev.description || "",
                event_type: ev.event_type || "other",
                // 悬浮摘要卡需要的额外字段（event 级缺省值用主体级补全，避免"0 源/无爆发/恒为新生"）
                state: ev.state || subject.state || "EMERGING",
                is_burst: !!(ev.is_burst || subject.is_burst),
                article_count: ev.article_count || 0,
                distinct_source_count: ev.distinct_source_count || subject.distinct_source_count || 0,
                last_time: ev.last_time || "",
                subject: ev.subject || subject.subject || "",
            };
        });
        // 时间范围 = 该主体事件的自然时间段（首末事件），两侧各留 8% 留白；单事件/同日补最小可视跨度
        var times = events.map(function (ev) { return ev.last_time; }).filter(Boolean).sort();
        var minDate = new Date(times[0]), maxDate = new Date(times[times.length - 1]);
        var span = maxDate.getTime() - minDate.getTime();
        if (span < 2 * 24 * 3600 * 1000) {
            var mid = (minDate.getTime() + maxDate.getTime()) / 2;
            minDate = new Date(mid - 24 * 3600 * 1000);
            maxDate = new Date(mid + 24 * 3600 * 1000);
            span = maxDate.getTime() - minDate.getTime();
        }
        var pad = span * 0.08;
        var minT = minDate.getTime() - pad;
        var maxT = maxDate.getTime() + pad;
        container.innerHTML = '<div class="intel-tl-head">'
            + '<span class="intel-trend-chip is-' + stateCls + '">' + esc(sn) + '</span>'
            + '<span class="intel-tl-title">' + esc(subject.subject) + '</span>'
            + '<span class="intel-event-count">' + subject.article_count + ' 篇 · ' + subject.event_count + ' 事件</span>'
            + '</div>'
            + '<div id="intelTimelineHChart" style="width:100%;height:400px"></div>';
        var dom = document.getElementById("intelTimelineHChart");
        if (!dom) return;
        // 复用已有实例（避免重复 init 叠加 hover 监听），气泡 tooltip 关闭，改用自定义悬浮摘要卡
        var inst = echarts.getInstanceByDom(dom) || echarts.init(dom);
        inst.setOption({
            tooltip: { show: false },
            grid: { left: 16, right: 40, top: 44, bottom: 56, containLabel: true },
            xAxis: {
                type: "time",
                min: minT,
                max: maxT,
                // 时间轴：细线 + 右端箭头（时间方向），无刻度竖线，标签自适应避让
                axisLine: { symbol: ["none", "arrow"], lineStyle: { color: "#9aa6a0", width: 1 } },
                axisTick: { show: false },
                axisLabel: {
                    hideOverlap: true,
                    color: "#6b7280",
                    fontSize: 11,
                    formatter: function (val) {
                        var d = new Date(val);
                        return (d.getMonth() + 1) + "/" + d.getDate();
                    },
                },
                splitLine: { show: false },
            },
            // 对称区间让事件点（y≈0）落在垂直中线，y 轴本身不显示
            yAxis: { show: false, min: -1.4, max: 1.4 },
            series: [{
                type: "effectScatter",
                symbolSize: 14,
                showEffectOn: "emphasis",
                rippleEffect: { period: 4, scale: 5, brushType: "stroke" },
                itemStyle: { color: "#1d7f68", shadowBlur: 8, shadowColor: "rgba(29,127,104,0.4)" },
                emphasis: { scale: 1.3, focus: "self" },
                label: {
                    show: true,
                    formatter: function (p) { return (p.data.description || "").slice(0, 14); },
                    position: "top",
                    fontSize: 12,
                    color: "#374151",
                },
                data: scatterData,
            }],
        });
        // 节点 click → 打开该事件文章列表（echarts 原生事件，由 echarts 命中，比 zrender 手动命中可靠）
        inst.on("click", { seriesIndex: 0 }, function (params) {
            var idx = params.dataIndex;
            if (idx == null) return;
            selectedEventHash = scatterData[idx].event_hash;
            selectedEventDesc = scatterData[idx].description || "";
            openEventArticles(selectedEventHash);
        });
        // 节点 hover → 延迟加载该事件文章卡片到右侧（与首页地图 hover 出卡一致，300ms 防抖）
        var hoverTimer = null;
        inst.on("mouseover", { seriesIndex: 0 }, function (params) {
            var idx = params.dataIndex;
            if (idx == null || idx === hoverIdx) return;
            hoverIdx = idx;
            if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
            var hash = scatterData[idx] && scatterData[idx].event_hash;
            if (hash) {
                hoverTimer = setTimeout(function () {
                    if (hoverIdx === idx && hash) openEventArticles(hash);
                }, 300);
            }
        });
        inst.on("mouseout", { seriesIndex: 0 }, function () {
            if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
        });
        window.__intelTimelineHInst = inst;
        if (window.ResizeObserver && !dom.__intelHRO) {
            dom.__intelHRO = new ResizeObserver(function () {
                var c = echarts.getInstanceByDom(dom);
                if (c) c.resize();
            });
            dom.__intelHRO.observe(dom);
        }
    }

    function renderSubjectTimeline(container, subject) {
        if (timelineMode === "horizontal") {
            renderSubjectTimelineHorizontal(container, subject);
            return;
        }
        if (!subject) { container.innerHTML = '<div class="intel-trend-empty">请选择左侧主体</div>'; return; }
        var stateCls = (subject.state || "EMERGING").toLowerCase();
        var events = (subject.events || []).slice().sort(function (a, b) {
            return (b.last_time || "").localeCompare(a.last_time || "");
        });
        var rows = events.map(function (ev) {
            var evTime = ev.last_time || "";
            var evDate = evTime ? esc(evTime) : "未知时间";
            return '<div class="intel-tl-row">'
                + '<div class="intel-tl-marker"><div class="intel-tl-dot is-' + stateCls + '"></div><div class="intel-tl-stem"></div></div>'
                + '<div class="intel-tl-time">' + evDate + '</div>'
                + '<div class="intel-tl-content intel-trend-clickable" onclick="selectEvent(\'' + ev.event_hash + '\', decodeURIComponent(\'' + encodeURIComponent(ev.description || "") + '\'))">' + esc(ev.description || "") + '</div>'
                + '</div>';
        }).join("");
        // 事件少于 2 个时补灰色占位点（无文章/稀疏时让时间线不空）
        for (var p = events.length; p < 2; p += 1) {
            rows += '<div class="intel-tl-row is-placeholder">'
                + '<div class="intel-tl-marker"><div class="intel-tl-dot"></div><div class="intel-tl-stem"></div></div>'
                + '<div class="intel-tl-time">—</div>'
                + '<div class="intel-tl-content intel-tl-placeholder">暂无更多事件</div>'
                + '</div>';
        }
        container.innerHTML = '<div class="intel-tl-head">'
            +   '<span class="intel-trend-chip is-' + stateCls + '">' + esc(STATE_NAMES[subject.state] || subject.state) + '</span>'
            +   '<span class="intel-tl-title">' + esc(subject.subject) + '</span>'
            +   '<span class="intel-event-count">' + subject.article_count + ' 篇 · ' + subject.event_count + ' 事件 · ' + subject.distinct_source_count + ' 源</span>'
            + '</div>'
            + '<div class="intel-tl">' + rows + '</div>';
    }

    function renderIntelEvents(container, events) {
        if (!events.length) {
            container.innerHTML = '<div class="intel-trend-empty">暂无事件（需先抽取文章事件，worker 后台回填中）</div>';
            return;
        }
        var rows = events.map(function (e) {
            var rep = e.representative_article || {};
            var sn = STATE_NAMES[e.state] || e.state;
            var tn = TYPE_NAMES[e.event_type] || e.event_type;
            var time = e.last_time || e.first_time || "";
            var dateShort = time ? esc(time).slice(5) : "—";
            var fullTime = time ? esc(time) : "未知时间";
            var stateCls = (e.state || "EMERGING").toLowerCase();
            return '<div class="intel-event-row">'
                + '<div class="intel-event-time-col">'
                +   '<div class="intel-event-date">' + dateShort + '</div>'
                +   '<div class="intel-event-dot is-' + stateCls + '"></div>'
                + '</div>'
                + '<div class="intel-event-card intel-trend-clickable" onclick="openEventArticles(\'' + e.event_hash + '\')">'
                +   '<div class="intel-event-card-head">'
                +   '<span class="intel-trend-chip is-' + stateCls + '">' + esc(sn) + '</span>'
                +   '<span class="intel-event-type-chip">' + esc(tn) + '</span>'
                +   '<span class="intel-event-count">' + e.article_count + ' 篇 · ' + e.distinct_source_count + ' 源</span>'
                + (e.is_burst ? '<span class="intel-event-burst">★ 爆发</span>' : '')
                +   '</div>'
                +   '<div class="intel-event-desc">' + esc(e.description || "") + '</div>'
                +   '<div class="intel-event-meta">' + fullTime + ' · 代表：' + (rep.title ? esc(rep.title).slice(0, 40) : "—") + '</div>'
                + '</div>'
                + '</div>';
        }).join("");
        container.innerHTML = '<div class="intel-event-timeline"><div class="intel-event-flow">↑ 最近在上 · 时间向下流动</div>' + rows + '</div>';
    }

    window.openEventArticles = function (eventHash) {
        var box = document.getElementById("intelEventArticles");
        if (!box || !eventHash) return;
        // 横向模式隐藏了文章卡片区，点击事件时恢复显示
        if (box.style.display === "none") {
            box.style.display = "";
            var wrap = box.closest(".intel-event-right");
            if (wrap) wrap.style.gridTemplateColumns = "";
        }
        box.innerHTML = '<div class="history-load-card"><div class="history-load-animation" aria-hidden="true"><span class="history-load-icon"><i class="fa fa-comments"></i></span></div><strong>正在加载文章</strong><span>聚合该事件的文章…</span></div>';
        var params = new URLSearchParams({ industry_pack_id: packId(), days: "90", page: "1", per_page: "20" });
        fetch("/api/intel/events/" + encodeURIComponent(eventHash) + "/articles?" + params.toString(), { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || "加载失败");
                var arts = data.articles || [];
                if (!arts.length) { box.innerHTML = '<div class="intel-trend-empty">该事件暂无文章</div>'; return; }
                box.innerHTML = '<div class="intel-event-article-list">' + arts.map(function (a) {
                    a = Object.assign({ dashboard_category: "event" }, a);
                    return (typeof renderIntelCard === "function")
                        ? renderIntelCard(a)
                        : ('<div class="intel-trend-article" onclick="viewArticle(' + Number(a.article_id || 0) + ')"><div class="intel-trend-article-title">' + esc(a.title || "") + '</div></div>');
                }).join("") + '</div>';
            })
            .catch(function (err) { box.innerHTML = '<div class="intel-trend-empty">加载失败：' + esc(err.message) + '</div>'; });
    };

    window.eventArticlePage = function (eventHash, page) {
        var body = document.getElementById("intelTrendModalBody");
        if (body) body.innerHTML = '<div class="intel-trend-modal-loading">加载中…</div>';
        fetchEventPage(eventHash, page, body);
    };

    function fetchEventPage(eventHash, page, body) {
        var params = new URLSearchParams({
            industry_pack_id: packId(), days: "90",
            page: String(page), per_page: "5",
        });
        fetch("/api/intel/events/" + encodeURIComponent(eventHash) + "/articles?" + params.toString(), { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || "加载失败");
                renderEventArticles(body, data.articles || [], eventHash, page, data.total_pages || 1, data.total || 0);
            })
            .catch(function (err) {
                body.innerHTML = '<div class="intel-trend-modal-loading">加载失败：' + esc(err.message) + "</div>";
            });
    }

    function renderEventArticles(body, articles, eventHash, page, totalPages, total) {
        if (!articles.length) {
            body.innerHTML = '<div class="intel-trend-modal-loading">该事件暂无文章</div>';
            return;
        }
        body.innerHTML = articles.map(function (a) {
            return '<div class="intel-trend-article" onclick="viewArticle(' + Number(a.article_id || 0) + ')">'
                + '<div class="intel-trend-article-title">' + esc(a.title || "无标题") + '</div>'
                + '<div class="intel-trend-article-meta">'
                +   '<span><i class="fa fa-link"></i> ' + esc(a.domain || "-") + '</span>'
                +   '<span><i class="fa fa-calendar"></i> ' + esc(a.publish_date || a.effective_time || "未知") + '</span>'
                + '</div></div>';
        }).join("");
        var foot = document.getElementById("intelTrendModalFoot");
        if (foot) {
            var prev = page > 1
                ? '<button type="button" class="intel-trend-page-btn" onclick="eventArticlePage(\'' + eventHash + '\',' + (page - 1) + ')">上一页</button>'
                : '<button type="button" class="intel-trend-page-btn" disabled>上一页</button>';
            var next = page < totalPages
                ? '<button type="button" class="intel-trend-page-btn" onclick="eventArticlePage(\'' + eventHash + '\',' + (page + 1) + ')">下一页</button>'
                : '<button type="button" class="intel-trend-page-btn" disabled>下一页</button>';
            foot.innerHTML = prev + '<span class="intel-trend-page-info">第 ' + page + ' / ' + totalPages + ' 页 · 共 ' + total + ' 篇</span>' + next;
        }
    }
})();
