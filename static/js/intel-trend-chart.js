/*!
 * 行业趋势演化图（echarts 封装）—— 行业信息演化发现系统第一阶段
 * 由 mapindex.html 的 loadIntelTrendEvolution() 调用，渲染关键词×天的命中量折线。
 * 复用 dashboard-theme.css 的 :root 色板（--accent 等）。
 */
(function () {
    "use strict";
    if (typeof window === "undefined") return;

    var COLORS = [
        "#1d7f68", "#2b6cb0", "#c05621", "#6b46c1", "#b7791f",
        "#2c7a7b", "#9b2c2c", "#38a169", "#d53f8c", "#2d3748"
    ];

    /**
     * 渲染趋势折线。
     * @param dom         挂载容器（需已有宽高）
     * @param series      后端 series：[{keyword, state, is_burst, burst_score, points:[{date,count}]}]
     * @param burstWindow 爆发窗（保留参数）
     */
    window.renderIntelTrendChart = function (dom, series, burstWindow) {
        if (typeof echarts === "undefined" || !dom) return;
        var dark = (typeof document !== "undefined") && document.body.classList.contains("skin-darkblue");
        var axisText = dark ? "#93a9cc" : "#6b7280";
        var gridLine = dark ? "rgba(96,165,250,0.16)" : "#e0e0e0";
        var axisLine = dark ? "rgba(96,165,250,0.30)" : "#ccc";
        var legendText = dark ? "#c7d7ee" : "#374151";
        var tooltipBg = dark ? "#0e1d3a" : "#fff";
        var tooltipText = dark ? "#e6f0ff" : "#333";
        var tooltipBorder = dark ? "rgba(96,165,250,0.40)" : "rgba(0,0,0,0.1)";

        // 收集所有日期并排序（不同关键词的 points 日期可能不一致）
        // 只含有数据的日期（count>0），避免 x 轴 180 天密密麻麻
        var dateSet = {};
        (series || []).forEach(function (s) {
            (s.points || []).forEach(function (p) {
                if ((p.count || 0) > 0) dateSet[p.date] = true;
            });
        });
        var dates = Object.keys(dateSet).sort();

        var echartSeries = (series || []).map(function (s, i) {
            var pointMap = {};
            (s.points || []).forEach(function (p) { pointMap[p.date] = p.count; });
            return {
                name: s.keyword,
                type: "line",          // 折线（非柱状）
                smooth: true,
                symbol: "circle",
                symbolSize: 6,
                showSymbol: true,
                connectNulls: true,
                lineStyle: { width: s.is_burst ? 3.5 : 2 },
                itemStyle: { color: COLORS[i % COLORS.length] },
                emphasis: { focus: "series" },
                z: s.is_burst ? 10 : 2,
                data: dates.map(function (d) { return d in pointMap ? pointMap[d] : 0; })
            };
        });
        // 波纹节点（复用横向时间轴 effectScatter ripple，hover 时涟漪扩散）
        var effectSeries = (series || []).map(function (s, i) {
            var pm = {};
            (s.points || []).forEach(function (p) { pm[p.date] = p.count; });
            return {
                name: s.keyword,
                type: "effectScatter",
                showEffectOn: "emphasis",
                rippleEffect: { period: 4, scale: 4, brushType: "stroke" },
                symbolSize: 7,
                itemStyle: { color: COLORS[i % COLORS.length] },
                z: 12,
                data: dates.map(function (d) { return d in pm ? pm[d] : null; })
            };
        });

        var inst = echarts.getInstanceByDom(dom);
        if (inst) {
            inst.dispose();
        }
        inst = echarts.init(dom);
        // 图例默认只加亮第一个趋势词（桌面/手机一致）；点击图例或下方趋势词标签可切换
        var legendSelected = {};
        (series || []).forEach(function (s, i) {
            legendSelected[s.keyword] = (i === 0);
        });
        inst.setOption(
            {
                tooltip: {
                    trigger: "axis",
                    confine: true,
                    backgroundColor: tooltipBg,
                    borderColor: tooltipBorder,
                    textStyle: { color: tooltipText },
                    formatter: function (params) {
                        // line + effectScatter 同名会重复，按 seriesName 去重
                        var seen = {}, out = [];
                        (params || []).forEach(function (p) {
                            if (seen[p.seriesName]) return;
                            seen[p.seriesName] = true;
                            out.push(p.marker + " " + p.seriesName + "：" + p.value);
                        });
                        return out.join("<br/>");
                    },
                    position: function (point, params, dom, rect, size) {
                        // 左半屏→提示框在鼠标右侧，右半屏→在左侧，避免遮挡数据点
                        var x = point[0] < size.viewSize[0] / 2
                            ? point[0] + 18
                            : point[0] - size.contentSize[0] - 18;
                        var y = Math.max(
                            8,
                            Math.min(point[1] - size.contentSize[1] / 2, size.viewSize[1] - size.contentSize[1] - 8)
                        );
                        return [x, y];
                    }
                },
                // plain=折行排列（去掉 scroll 的 <1/5> 翻页），默认只选第一个趋势词
                legend: {
                    type: "plain",
                    top: 0,
                    left: 0,
                    itemWidth: 12,
                    itemHeight: 8,
                    itemGap: 8,
                    textStyle: { fontSize: 11, color: legendText },
                    selected: legendSelected
                },
                grid: { left: 44, right: 20, top: 48, bottom: 28, containLabel: false },
                xAxis: {
                    type: "category",
                    boundaryGap: false,
                    data: dates,
                    axisLabel: { fontSize: 10, hideOverlap: true, interval: Math.ceil(dates.length / 10), color: axisText },
                    axisLine: { lineStyle: { color: axisLine } },
                    axisTick: { lineStyle: { color: axisLine } }
                },
                yAxis: {
                    type: "value",
                    minInterval: 1,
                    axisLabel: {
                        fontSize: 10,
                        color: axisText,
                        formatter: function (v) { return Number(v).toFixed(0); }
                    },
                    splitLine: { lineStyle: { color: gridLine } }
                },
                series: echartSeries.concat(effectSeries)
            },
            true
        );
        inst.resize();

        // 监听容器尺寸变化（section 从隐藏→显示时 echarts 自动 resize，避免 0 尺寸渲染）
        if (window.ResizeObserver && !dom.__intelTrendRO) {
            dom.__intelTrendRO = new ResizeObserver(function () {
                var cur = echarts.getInstanceByDom(dom);
                if (cur) cur.resize();
            });
            dom.__intelTrendRO.observe(dom);
        }
        if (!window.__intelTrendResizeBound) {
            window.addEventListener("resize", function () {
                var cur = echarts.getInstanceByDom(dom);
                if (cur) cur.resize();
            });
            window.__intelTrendResizeBound = true;
        }
        // 供页面上的趋势词标签联动：切换某条曲线的显示/隐藏
        window.__intelTrendInst = inst;
        window.toggleIntelTrendSeries = function (name) {
            var chart = window.__intelTrendInst;
            if (!chart || !name) return false;
            var opt = chart.getOption();
            var sel = (opt.legend && opt.legend[0] && opt.legend[0].selected) || {};
            var next = !(sel[name] === true);
            chart.dispatchAction({ type: "legendToggleSelect", name: name });
            return next;
        };
    };
})();
