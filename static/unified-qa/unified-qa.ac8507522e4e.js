import {
  EVENT_TYPES, STAGE_DEFINITIONS, applyQaEvent, citationTarget, createQaState,
  currentAnswer, evidenceIndex, groupLevel1Evidence, groupLevel2Evidence,
  markConnection, safeActionHref, safeHttpUrl, stageDurationLabel, stateFromRun,
} from "./qa-state.af2cc3967435.js";

const BUNDLE_VERSION = "1.0.0+4e5160244bdc";
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function button(label, className = "") {
  const node = element("button", className, label);
  node.type = "button";
  return node;
}

function formatDate(value) {
  if (!value) return "日期未提供";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value).slice(0, 30) : date.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(value) {
  return ({
    idle: "准备就绪", queued: "等待执行", running: "研究中", retry_wait: "等待重试",
    completed: "已完成", failed: "失败", cancelled: "已取消",
  })[value] || value || "准备就绪";
}

function parseSse(text) {
  const events = [];
  for (const block of String(text || "").split(/\r?\n\r?\n/)) {
    let data = "";
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) continue;
    try { events.push(JSON.parse(data)); } catch (_error) { /* incomplete frame */ }
  }
  return events;
}

function randomKey(prefix = "qa") {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export class UnifiedQaAssistant extends HTMLElement {
  static get observedAttributes() { return ["industry-pack", "run-id"]; }

  constructor() {
    super();
    this.state = createQaState();
    this.config = { modes: ["standard", "deep", "fast"], default_mode: "standard", providers: [] };
    this.history = [];
    this.legacyHistory = [];
    this.legacyContext = [];
    this.legacyFollowupHint = "";
    this.source = null;
    this.apiBase = "";
    this.apiPrefix = "/api/qa/v1";
    this.origin = "getinfo_ui";
    this.pageContext = {};
    this.refs = {};
  }

  connectedCallback() {
    if (this.shadowRoot) return;
    this.apiBase = String(this.getAttribute("api-base") || "").replace(/\/$/, "");
    this.apiPrefix = `/${String(this.getAttribute("api-prefix") || "api/qa/v1").replace(/^\/+|\/+$/g, "")}`;
    this.origin = this.getAttribute("origin") === "ragflow_ui" ? "ragflow_ui" : "getinfo_ui";
    const shadow = this.attachShadow({ mode: "open" });
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = new URL("./unified-qa.a7981157a24e.css", import.meta.url).href;
    shadow.append(stylesheet, this.buildShell());
    this.loadInitial();
    this.dispatchEvent(new CustomEvent("qa-component-ready", {
      bubbles: true, composed: true, detail: { version: BUNDLE_VERSION, origin: this.origin },
    }));
  }

  disconnectedCallback() { this.closeSource(); }

  setQuestion(value, { append = false, focus = true } = {}) {
    if (!this.refs.question) return;
    const incoming = String(value || "").trim();
    this.refs.question.value = append && this.refs.question.value.trim()
      ? `${this.refs.question.value.trimEnd()}\n\n${incoming}` : incoming;
    this.refs.question.dispatchEvent(new Event("input", { bubbles: true }));
    if (focus) this.refs.question.focus();
  }

  setPageContext(value) {
    const source = value && typeof value === "object" ? value : {};
    const ids = [...new Set([source.article_id, ...(source.article_ids || [])]
      .map((item) => Number(item)).filter((item) => Number.isInteger(item) && item > 0))].slice(0, 20);
    this.pageContext = ids.length ? { article_ids: ids } : {};
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (!this.shadowRoot || oldValue === newValue) return;
    if (name === "run-id" && newValue) this.openRun(newValue);
    if (name === "industry-pack") this.loadHistory();
  }

  buildShell() {
    const shell = element("section", "qa-shell");
    shell.setAttribute("aria-label", "统一研究问答助手");

    const history = element("aside", "qa-history");
    const historyHeader = element("div", "qa-pane-header");
    historyHeader.append(element("div", "qa-brand", "研究会话"));
    const newButton = button("新建", "qa-secondary");
    newButton.addEventListener("click", () => this.newConversation());
    historyHeader.append(newButton);
    const historyList = element("nav", "qa-history-list");
    historyList.setAttribute("aria-label", "问答历史");
    history.append(historyHeader, historyList);

    const main = element("main", "qa-main");
    const toolbar = element("header", "qa-toolbar");
    const identity = element("div", "qa-identity");
    identity.append(element("strong", "", "全新 AI 研究助手"), element("span", "qa-version", `v${BUNDLE_VERSION}`));
    const controls = element("div", "qa-controls");
    const mode = document.createElement("select");
    mode.setAttribute("aria-label", "研究模式");
    const provider = document.createElement("select");
    provider.setAttribute("aria-label", "一级模型");
    const webLabel = element("label", "qa-check");
    const web = document.createElement("input");
    web.type = "checkbox";
    webLabel.append(web, document.createTextNode(" 联网"));
    const settings = button("设置", "qa-secondary qa-settings-open");
    settings.addEventListener("click", () => this.openSettings());
    controls.append(mode, provider, webLabel, settings);
    toolbar.append(identity, controls);

    const runMeta = element("div", "qa-run-meta");
    const timeline = element("ol", "qa-stepper");
    timeline.setAttribute("aria-label", "研究阶段");
    const alerts = element("div", "qa-alerts");
    alerts.setAttribute("aria-live", "polite");
    const answer = element("article", "qa-answer");
    answer.setAttribute("aria-live", "polite");

    const composer = element("form", "qa-composer");
    const question = document.createElement("textarea");
    question.rows = 3;
    question.maxLength = 20000;
    question.placeholder = "输入需要核验、比较或深入研究的问题…";
    question.setAttribute("aria-label", "问题");
    question.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") composer.requestSubmit();
    });
    const actions = element("div", "qa-composer-actions");
    const cancel = button("停止", "qa-danger");
    cancel.hidden = true;
    cancel.addEventListener("click", () => this.cancelRun());
    const submit = element("button", "qa-primary", "开始研究");
    submit.type = "submit";
    actions.append(element("span", "qa-shortcut", "Ctrl/⌘ + Enter"), cancel, submit);
    composer.append(question, actions);
    composer.addEventListener("submit", (event) => { event.preventDefault(); this.startRun(); });
    main.append(toolbar, runMeta, timeline, alerts, answer, composer);

    const evidence = element("aside", "qa-evidence");
    const evidenceHeader = element("div", "qa-pane-header");
    evidenceHeader.append(element("div", "qa-brand", "证据与核验"));
    const tabs = element("div", "qa-tabs");
    const evidenceBody = element("div", "qa-evidence-body");
    for (const [id, label] of [["level1", "一级召回"], ["level2", "二级研究"], ["conflicts", "冲突与缺口"]]) {
      const tab = button(label, id === "level1" ? "active" : "");
      tab.dataset.tab = id;
      tab.setAttribute("aria-pressed", id === "level1" ? "true" : "false");
      tab.addEventListener("click", () => this.selectTab(id));
      tabs.append(tab);
    }
    evidence.append(evidenceHeader, tabs, evidenceBody);
    const settingsPanel = element("section", "qa-settings-overlay");
    settingsPanel.hidden = true;
    const settingsHead = element("header", "qa-settings-overlay-head");
    settingsHead.append(element("strong", "", "AI、代理与研究设置"));
    const closeSettings = button("关闭", "qa-secondary");
    closeSettings.addEventListener("click", () => { settingsPanel.hidden = true; });
    settingsHead.append(closeSettings);
    const settingsBody = element("div", "qa-settings-overlay-body");
    settingsPanel.append(settingsHead, settingsBody);
    shell.append(history, main, evidence, settingsPanel);
    this.refs = { historyList, mode, provider, web, runMeta, timeline, alerts, answer, question, cancel, submit, tabs, evidenceBody, settingsPanel, settingsBody };
    this.activeTab = "level1";
    return shell;
  }

  async loadInitial() {
    await Promise.all([this.loadConfig(), this.loadHistory(), this.loadLegacyHistory()]);
    const runId = this.getAttribute("run-id");
    if (runId) await this.openRun(runId);
    else this.render();
  }

  headers(json = false) {
    const headers = { "X-QA-Origin": this.origin };
    const bridgeToken = this.getAttribute("bridge-token");
    if (bridgeToken) headers["X-QA-Bridge-Token"] = bridgeToken;
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  async request(path, options = {}) {
    const response = await fetch(`${this.apiBase}${path}`, {
      credentials: "include", ...options,
      headers: { ...this.headers(Boolean(options.body)), ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) {
      const failure = new Error(data?.error?.message || `请求失败（${response.status}）`);
      failure.payload = data?.error || { code: "HTTP_ERROR", retryable: response.status >= 500 };
      throw failure;
    }
    return data;
  }

  async loadConfig() {
    try {
      this.config = await this.request(`${this.apiPrefix}/config`);
      this.renderControls();
    } catch (error) { this.showLocalError(error); }
  }

  async loadHistory() {
    const pack = encodeURIComponent(this.getAttribute("industry-pack") || "");
    try {
      const data = await this.request(`${this.apiPrefix}/runs?limit=50${pack ? `&industry_pack_id=${pack}` : ""}`);
      this.history = Array.isArray(data.runs) ? data.runs : [];
      this.renderHistory();
    } catch (error) { this.showLocalError(error); }
  }

  async loadLegacyHistory() {
    const path = this.getAttribute("legacy-history-url");
    if (!path) return;
    try {
      const data = await this.request(path);
      this.legacyHistory = Array.isArray(data.sessions) ? data.sessions : [];
      this.renderHistory();
    } catch (_error) {
      this.legacyHistory = [];
    }
  }

  renderControls() {
    if (!this.refs.mode) return;
    const priorMode = this.refs.mode.value || this.config.default_mode || "standard";
    this.refs.mode.replaceChildren();
    for (const value of this.config.modes || ["standard", "deep", "fast"]) {
      const option = element("option", "", ({ standard: "标准研究", deep: "深度研究", fast: "快速回答" })[value] || value);
      option.value = value;
      option.selected = value === priorMode;
      this.refs.mode.append(option);
    }
    const priorProvider = this.refs.provider.value || "local";
    this.refs.provider.replaceChildren();
    for (const item of this.config.providers || []) {
      const option = element("option", "", item.display_name || item.provider_id || "模型");
      option.value = item.provider_id || "local";
      option.selected = option.value === priorProvider;
      option.disabled = item.configured === false;
      this.refs.provider.append(option);
    }
    if (!this.refs.provider.children.length) {
      const option = element("option", "", "本地/RAGFlow 模型");
      option.value = "local";
      this.refs.provider.append(option);
    }
  }

  renderHistory() {
    if (!this.refs.historyList) return;
    this.refs.historyList.replaceChildren();
    if (!this.history.length) this.refs.historyList.append(element("p", "qa-empty", "还没有研究会话"));
    for (const run of this.history) {
      const item = button("", `qa-history-item${run.id === this.state.runId ? " active" : ""}`);
      item.append(element("strong", "", String(run.question || "未命名问题").slice(0, 80)));
      item.append(element("span", "", `${statusLabel(run.status)} · ${formatDate(run.updated_at || run.created_at)}`));
      item.addEventListener("click", () => this.openRun(run.id));
      this.refs.historyList.append(item);
    }
    if (this.legacyHistory.length) {
      const label = element("div", "qa-history-divider", "旧会话（只读，未标记为深度研究）");
      this.refs.historyList.append(label);
      for (const session of this.legacyHistory) {
        const item = button("", "qa-history-item legacy");
        item.append(element("strong", "", String(session.first_question || "旧会话").slice(0, 80)));
        item.append(element("span", "", `Legacy · ${session.qa_count || 0} 条 · ${formatDate(session.last_at || session.last_time)}`));
        item.addEventListener("click", () => this.openLegacySession(session));
        this.refs.historyList.append(item);
      }
    }
  }

  newConversation({ preserveLegacyHint = false } = {}) {
    this.closeSource();
    this.state = createQaState({ industryPackId: this.getAttribute("industry-pack") || this.config.industry_pack_id || "" });
    this.legacyContext = [];
    if (!preserveLegacyHint) this.legacyFollowupHint = "";
    this.refs.question.value = "";
    this.refs.question.placeholder = "输入需要核验、比较或深入研究的问题…";
    this.removeAttribute("run-id");
    this.render();
    this.refs.question.focus();
  }

  async startRun() {
    const question = this.refs.question.value.trim();
    if (!question || this.refs.submit.disabled) return;
    if (this.legacyFollowupHint && question.length < 8) {
      this.showLocalError({ message: "请填写真实的后续问题，例如政策名称、主体、时间或你要比较的事项。" });
      return;
    }
    this.refs.submit.disabled = true;
    const sessionId = randomKey("session");
    try {
      const data = await this.request(`${this.apiPrefix}/runs`, {
        method: "POST",
        headers: { "Idempotency-Key": randomKey("ask") },
        body: JSON.stringify({
          question,
          session_id: sessionId,
          industry_pack_id: this.getAttribute("industry-pack") || this.config.industry_pack_id || "",
          mode: this.refs.mode.value || "standard",
          draft_provider: this.refs.provider.value || "local",
          web_search: this.refs.web.checked,
          page_context: this.pageContext,
          client_capabilities: ["qa-sse.v1"],
        }),
      });
      this.state = stateFromRun({ ...data, question, session_id: sessionId });
      this.state.status = data.status || "queued";
      this.setAttribute("run-id", data.run_id);
      this.subscribe(data.run_id);
      await this.loadHistory();
      this.render();
    } catch (error) {
      this.showLocalError(error);
      this.refs.submit.disabled = false;
    }
  }

  async openRun(runId) {
    if (!runId || runId === this.state.runId && this.source) return;
    this.closeSource();
    try {
      const run = await this.request(`${this.apiPrefix}/runs/${encodeURIComponent(runId)}`);
      this.state = stateFromRun(run);
      const replay = await fetch(`${this.apiBase}${this.apiPrefix}/runs/${encodeURIComponent(runId)}/events?follow=0`, {
        credentials: "include", headers: this.headers(),
      });
      if (replay.ok) {
        for (const event of parseSse(await replay.text())) this.state = applyQaEvent(this.state, event);
      }
      if (!TERMINAL.has(this.state.status)) this.subscribe(runId);
      if (this.getAttribute("run-id") !== runId) this.setAttribute("run-id", runId);
      this.render();
    } catch (error) { this.showLocalError(error); }
  }

  async openLegacySession(session) {
    const base = this.getAttribute("legacy-history-url");
    if (!base || !session?.session_id) return;
    this.closeSource();
    try {
      const data = await this.request(`${base}/${encodeURIComponent(session.session_id)}/messages`);
      const rows = Array.isArray(data.messages) ? data.messages.slice(-20) : [];
      this.legacyContext = rows;
      const pairs = rows.map((row) => {
        if (row.role) return `${row.role === "user" ? "问" : "答"}：${String(row.content || "").slice(0, 4000)}`;
        return `问：${String(row.question || "").slice(0, 2000)}\n答：${String(row.answer || "").slice(0, 4000)}`;
      });
      this.state = stateFromRun({
        id: `legacy:${session.session_id}`,
        question: session.first_question || "旧会话",
        status: "completed",
        origin: "legacy_readonly",
        industry_pack_id: this.getAttribute("industry-pack") || "",
        final_answer: {
          answer: pairs.join("\n\n") || "该旧会话没有可读取的消息。",
          cutoff_at: session.last_at || session.last_time || "",
          models: { legacy: session.model_id || "旧版助手" },
          evidence: [], citation_map: {}, conflicts: [],
          degraded: true,
          degradation_reasons: ["这是旧版只读会话，未执行统一一级/二级检索，不代表已完成深度研究。"],
        },
      });
      this.render();
    } catch (error) { this.showLocalError(error); }
  }

  continueLegacy() {
    const count = this.legacyContext.length;
    this.newConversation({ preserveLegacyHint: true });
    this.legacyFollowupHint = count
      ? `已选择旧会话作为参考。请输入真实后续问题；系统不会把旧答案模板当成可提交问题。`
      : "请输入真实后续问题。";
    this.refs.question.placeholder = "请输入真实后续问题，例如：财政部和税务总局2026年第21号公告对离岸信托有什么影响？";
    this.showLocalError({ message: this.legacyFollowupHint });
  }

  subscribe(runId) {
    this.closeSource();
    const after = Number(this.state.lastEventId || 0);
    const url = `${this.apiBase}${this.apiPrefix}/runs/${encodeURIComponent(runId)}/events?after=${after}`;
    const source = new EventSource(url, { withCredentials: true });
    this.source = source;
    source.onopen = () => { this.state = markConnection(this.state, true); this.renderMeta(); };
    source.onerror = () => {
      this.state = markConnection(this.state, false);
      this.renderMeta();
      if (TERMINAL.has(this.state.status)) this.closeSource();
    };
    for (const type of EVENT_TYPES) source.addEventListener(type, (event) => {
      try { this.consumeEvent(JSON.parse(event.data)); } catch (_error) { /* invalid frames are ignored */ }
    });
  }

  consumeEvent(event) {
    this.state = applyQaEvent(this.state, event);
    this.render();
    if (event.type === "done" || event.type === "error") {
      this.closeSource();
      this.loadHistory();
    }
  }

  closeSource() {
    if (this.source) this.source.close();
    this.source = null;
  }

  openSettings(section = "providers", provider = "") {
    const panel = this.refs.settingsPanel;
    const body = this.refs.settingsBody;
    if (!panel || !body) return;
    body.replaceChildren();
    const settings = document.createElement("unified-qa-settings");
    settings.setAttribute("api-base", this.apiBase);
    settings.setAttribute("api-prefix", this.apiPrefix);
    settings.setAttribute("section", section || "providers");
    if (provider) settings.setAttribute("provider", provider);
    body.append(settings);
    panel.hidden = false;
  }

  async cancelRun() {
    if (!this.state.runId) return;
    try {
      await this.request(`${this.apiPrefix}/runs/${encodeURIComponent(this.state.runId)}/cancel`, { method: "POST" });
    } catch (error) { this.showLocalError(error); }
  }

  async retryStage(stage) {
    if (!this.state.runId || !stage) return;
    try {
      await this.request(`${this.apiPrefix}/runs/${encodeURIComponent(this.state.runId)}/retry`, {
        method: "POST", headers: { "Idempotency-Key": randomKey("retry") }, body: JSON.stringify({ stage }),
      });
      this.state.errors = this.state.errors.filter((item) => item.stage !== stage);
      this.subscribe(this.state.runId);
      this.render();
    } catch (error) { this.showLocalError(error); }
  }

  showLocalError(error) {
    const payload = error?.payload || { code: "UI_REQUEST_FAILED", message: error?.message || "请求失败", retryable: true, actions: [] };
    this.state.errors = [...this.state.errors, { ...payload, stage: this.state.currentStage || "gateway", eventId: Date.now() }];
    this.renderAlerts();
  }

  render() {
    if (!this.refs.answer) return;
    this.renderHistory();
    this.renderMeta();
    this.renderStages();
    this.renderAlerts();
    this.renderAnswer();
    this.renderEvidence();
    const running = ["queued", "running"].includes(this.state.status);
    this.refs.cancel.hidden = !running;
    this.refs.submit.disabled = running;
  }

  renderMeta() {
    if (!this.refs.runMeta) return;
    this.refs.runMeta.replaceChildren();
    this.refs.runMeta.append(element("span", `qa-status status-${this.state.status}`, statusLabel(this.state.status)));
    const pack = this.state.industryPackId || this.getAttribute("industry-pack") || this.config.industry_pack_id || "默认行业包";
    this.refs.runMeta.append(element("span", "", `行业：${pack}`));
    if (this.state.runId) this.refs.runMeta.append(element("span", "qa-trace", `Run ${this.state.runId}`));
    if (this.state.reconnecting) this.refs.runMeta.append(element("span", "qa-warning-text", "连接中断，正在自动恢复…"));
  }

  renderStages() {
    this.refs.timeline.replaceChildren();
    for (const definition of STAGE_DEFINITIONS) {
      const stage = this.state.stages[definition.id];
      if (stage.status === "skipped") continue;
      const item = element("li", `qa-step status-${stage.status}`);
      item.dataset.stage = stage.id;
      item.append(element("span", "qa-step-dot", ""));
      const text = element("span", "qa-step-text");
      text.append(element("strong", "", stage.label));
      text.append(element("small", "", ({ pending: "待执行", running: "进行中", completed: "已完成", degraded: "降级完成", failed: "失败", cancelled: "已取消" })[stage.status] || stage.status));
      item.append(text);
      const duration = stageDurationLabel(stage.durationMs);
      if (duration) item.append(element("time", "", duration));
      this.refs.timeline.append(item);
    }
  }

  renderAlerts() {
    if (!this.refs.alerts) return;
    this.refs.alerts.replaceChildren();
    for (const warning of this.state.warnings) this.refs.alerts.append(this.alertCard(warning, "warning"));
    for (const error of this.state.errors) this.refs.alerts.append(this.alertCard(error, "error"));
  }

  alertCard(item, kind) {
    const card = element("section", `qa-alert qa-${kind}`);
    card.setAttribute("role", kind === "error" ? "alert" : "status");
    card.append(element("strong", "", kind === "error" ? "本阶段需要处理" : "已降级继续"));
    card.append(element("p", "", item.message || "部分研究能力暂不可用。"));
    const actions = element("div", "qa-alert-actions");
    for (const action of item.actions || []) {
      const href = safeActionHref(action.href);
      if (href && href.startsWith("/my-ai-settings")) {
        const open = button(action.label || "打开设置", "qa-secondary");
        open.addEventListener("click", () => {
          const query = new URL(href, location.origin).searchParams;
          this.openSettings(query.get("section") || "providers", query.get("provider") || "");
        });
        actions.append(open);
      } else if (href) {
        const link = element("a", "qa-secondary", action.label || "打开设置");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener";
        actions.append(link);
      } else if (action.action === "retry_stage") {
        const retry = button(action.label || "重试阶段", "qa-secondary");
        retry.addEventListener("click", () => this.retryStage(item.stage));
        actions.append(retry);
      } else if (action.action === "open_model_picker") {
        const choose = button(action.label || "切换模型", "qa-secondary");
        choose.addEventListener("click", () => this.refs.provider.focus());
        actions.append(choose);
      }
    }
    if (item.trace_id) actions.append(element("code", "qa-trace", `追踪编号 ${String(item.trace_id).slice(0, 100)}`));
    card.append(actions);
    return card;
  }

  renderAnswer() {
    this.refs.answer.replaceChildren();
    const answer = currentAnswer(this.state);
    if (!answer) {
      const empty = element("div", "qa-answer-empty");
      empty.append(element("h2", "", "从一个值得核验的问题开始"));
      empty.append(element("p", "", "系统会先形成一级结论，再用 news 知识库纵向追溯、横向比较和检查冲突，最后给出统一答案。"));
      this.refs.answer.append(empty);
      return;
    }
    const heading = element("div", "qa-answer-heading");
    heading.append(element("h2", "", this.state.finalAnswer ? "综合结论" : "正在形成综合结论"));
    if (this.state.finalAnswer?.degraded) heading.append(element("span", "qa-badge warning", "降级回答"));
    this.refs.answer.append(heading);
    const body = element("div", "qa-answer-body");
    const parts = answer.split(/(\[\d+\])/g);
    for (const part of parts) {
      if (/^\[\d+\]$/.test(part) && this.state.finalAnswer) {
        const target = citationTarget(this.state, part);
        if (target) {
          const cite = button(part, "qa-citation");
          cite.title = target.title || target.evidence_ref;
          cite.addEventListener("click", () => this.focusEvidence(target.evidence_ref));
          body.append(cite);
          continue;
        }
      }
      body.append(document.createTextNode(part));
    }
    this.refs.answer.append(body);
    if (this.state.finalAnswer) {
      const footer = element("footer", "qa-answer-footer");
      footer.append(element("span", "", `资料截止：${formatDate(this.state.finalAnswer.cutoff_at)}`));
      const models = Object.values(this.state.finalAnswer.models || {}).filter(Boolean).join(" / ");
      if (models) footer.append(element("span", "", `模型：${models}`));
      if (this.state.finalAnswer.degradation_reasons?.length) footer.append(element("span", "qa-warning-text", this.state.finalAnswer.degradation_reasons.join("；")));
      this.refs.answer.append(footer);
    }
    if (this.state.origin === "legacy_readonly") {
      const legacyActions = element("div", "qa-legacy-actions");
      legacyActions.append(element("span", "qa-warning-text", "旧答案只读，不能显示为已完成深度研究。"));
      const continueButton = button("用新版逻辑继续追问", "qa-primary");
      continueButton.addEventListener("click", () => this.continueLegacy());
      legacyActions.append(continueButton);
      this.refs.answer.append(legacyActions);
    }
  }

  selectTab(id) {
    this.activeTab = id;
    for (const tab of this.refs.tabs.querySelectorAll("button")) {
      const active = tab.dataset.tab === id;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-pressed", active ? "true" : "false");
    }
    this.renderEvidence();
  }

  renderEvidence() {
    if (!this.refs.evidenceBody) return;
    this.refs.evidenceBody.replaceChildren();
    if (this.activeTab === "level1") this.renderLevel1();
    else if (this.activeTab === "level2") this.renderLevel2();
    else this.renderConflicts();
  }

  renderLevel1() {
    const groups = groupLevel1Evidence(this.state);
    for (const [key, label] of [["article", "库内文章"], ["page_context", "当前页面"], ["web", "联网资料"]]) {
      const section = element("section", "qa-evidence-group");
      section.append(element("h3", "", `${label}（${groups[key].length}）`));
      if (!groups[key].length) section.append(element("p", "qa-empty", "暂无采用的资料"));
      for (const item of groups[key]) section.append(this.evidenceCard(item));
      this.refs.evidenceBody.append(section);
    }
  }

  renderLevel2() {
    const output = this.state.outputs.level2_retrieval || {};
    const trace = Array.isArray(output.queries) ? output.queries : [];
    if (trace.length) {
      const axes = element("section", "qa-evidence-group");
      axes.append(element("h3", "", "研究查询"));
      for (const query of trace) axes.append(element("p", "qa-query", `${query.axis || "original"} · ${query.query || query}`));
      this.refs.evidenceBody.append(axes);
    }
    const documents = groupLevel2Evidence(this.state);
    if (!documents.length) this.refs.evidenceBody.append(element("p", "qa-empty", "尚无 RAGFlow 二级召回"));
    for (const document of documents) {
      const details = element("details", "qa-document");
      const summary = element("summary", "");
      summary.append(element("strong", "", document.title));
      summary.append(element("span", "", `${document.axes.join(" / ")} · ${document.chunks.length} 个片段`));
      details.append(summary);
      for (const item of document.chunks) details.append(this.evidenceCard(item, true));
      this.refs.evidenceBody.append(details);
    }
  }

  renderConflicts() {
    const final = this.state.finalAnswer || {};
    const research = this.state.outputs.level2_research || {};
    const conflicts = final.conflicts || research.conflicts || [];
    const gaps = research.evidence_gaps || this.state.outputs.level1_draft?.gaps || [];
    if (!conflicts.length && !gaps.length) this.refs.evidenceBody.append(element("p", "qa-empty", "尚未发现冲突或证据缺口"));
    for (const conflict of conflicts) {
      const card = element("section", "qa-conflict");
      card.append(element("span", "qa-badge conflict", conflict.conflict_type || conflict.type || "证据冲突"));
      card.append(element("h3", "", conflict.summary || conflict.description || "不同资料存在不一致"));
      if (conflict.resolution) card.append(element("p", "", `裁决：${conflict.resolution}`));
      const refs = conflict.evidence_refs || [];
      for (const ref of refs) {
        const jump = button(String(ref), "qa-ref-link");
        jump.addEventListener("click", () => this.focusEvidence(ref));
        card.append(jump);
      }
      this.refs.evidenceBody.append(card);
    }
    for (const gap of gaps) {
      const card = element("section", "qa-gap");
      card.append(element("strong", "", "证据不足"));
      card.append(element("p", "", typeof gap === "string" ? gap : gap.description || gap.text || "需要更多资料核验"));
      this.refs.evidenceBody.append(card);
    }
  }

  evidenceCard(item, chunk = false) {
    const card = element("article", `qa-source-card${chunk ? " qa-chunk" : ""}`);
    card.id = `evidence-${String(item.evidence_ref || "").replace(/[^a-zA-Z0-9_-]/g, "-")}`;
    card.dataset.evidenceRef = String(item.evidence_ref || "");
    const title = element("h4", "", item.title || "未命名资料");
    const url = safeHttpUrl(item.source_url, globalThis.location?.href);
    if (url) {
      const link = element("a", "", item.title || "打开原文");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      title.replaceChildren(link);
    }
    card.append(title);
    const relationship = ({ supports: "支持", opposes: "反对", qualifies: "限定", context: "背景" })[item.relationship] || "资料";
    card.append(element("div", "qa-source-meta", `${relationship} · ${formatDate(item.published_at)} · 权威级别 ${item.authority_level ?? "-"}`));
    if (item.match_reason) card.append(element("p", "qa-match", `命中：${item.match_reason}`));
    if (item.excerpt) card.append(element("p", "qa-excerpt", item.excerpt));
    card.append(element("code", "qa-evidence-ref", item.evidence_ref || ""));
    return card;
  }

  focusEvidence(ref) {
    const item = evidenceIndex(this.state).get(String(ref));
    const type = String(item?.source_type || "");
    this.selectTab(type === "ragflow_chunk" ? "level2" : "level1");
    requestAnimationFrame(() => {
      const target = [...this.refs.evidenceBody.querySelectorAll("[data-evidence-ref]")]
        .find((node) => node.dataset.evidenceRef === String(ref));
      if (target) {
        target.open = true;
        target.tabIndex = -1;
        target.focus();
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("focused");
        globalThis.setTimeout(() => target.classList.remove("focused"), 1800);
      }
    });
  }
}

if (!customElements.get("unified-qa-assistant")) customElements.define("unified-qa-assistant", UnifiedQaAssistant);

export class UnifiedQaSettings extends HTMLElement {
  constructor() {
    super();
    this.apiBase = "";
    this.apiPrefix = "/api/qa/v1";
    this.data = null;
    this.refs = {};
  }

  connectedCallback() {
    if (this.shadowRoot) return;
    this.apiBase = String(this.getAttribute("api-base") || "").replace(/\/$/, "");
    this.apiPrefix = `/${String(this.getAttribute("api-prefix") || "api/qa/v1").replace(/^\/+|\/+$/g, "")}`;
    const shadow = this.attachShadow({ mode: "open" });
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = new URL("./unified-qa.a7981157a24e.css", import.meta.url).href;
    shadow.append(stylesheet, this.build());
    this.load();
  }

  async request(path, options = {}) {
    const response = await fetch(`${this.apiBase}${path}`, {
      credentials: "include", ...options,
      headers: { "X-QA-Origin": this.getAttribute("origin") || "getinfo_ui", ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) throw new Error(data?.error?.message || data?.message || `请求失败（${response.status}）`);
    return data;
  }

  build() {
    const root = element("section", "qa-settings");
    const tabs = element("nav", "qa-settings-tabs");
    const content = element("div", "qa-settings-content");
    const sections = [
      ["providers", "我的模型"], ["network", "网络与代理"], ["policy", "问答策略"],
      ["ragflow", "RAGFlow"], ["status", "连接状态"],
    ];
    for (const [id, label] of sections) {
      const tab = button(label, "qa-secondary");
      tab.dataset.section = id;
      tab.addEventListener("click", () => this.select(id));
      tabs.append(tab);
      const panel = element("section", "qa-settings-panel");
      panel.dataset.section = id;
      panel.hidden = true;
      content.append(panel);
    }
    const footer = element("footer", "qa-settings-footer");
    const status = element("span", "qa-settings-message", "正在读取安全配置…");
    const save = button("保存设置", "qa-primary");
    save.addEventListener("click", () => this.save());
    footer.append(status, save);
    root.append(tabs, content, footer);
    this.refs = { tabs, content, status, save };
    return root;
  }

  panel(id) { return [...this.refs.content.children].find((item) => item.dataset.section === id); }

  select(id) {
    for (const panel of this.refs.content.children) panel.hidden = panel.dataset.section !== id;
    for (const tab of this.refs.tabs.children) tab.classList.toggle("active", tab.dataset.section === id);
  }

  async load() {
    try {
      this.data = await this.request(`${this.apiPrefix}/settings`);
      this.render();
      this.select(this.getAttribute("section") || "providers");
      this.refs.status.textContent = "API Key 只显示配置状态，不会回显明文。";
    } catch (error) {
      this.refs.status.textContent = error.message || "设置读取失败";
      this.refs.status.className = "qa-settings-message error";
    }
  }

  render() {
    this.renderProviders();
    this.renderNetwork();
    this.renderPolicy();
    this.renderRagflow();
    this.renderHealth();
  }

  renderProviders() {
    const panel = this.panel("providers");
    panel.replaceChildren(element("h2", "", "我的模型"), element("p", "qa-muted", "每个用户的密钥相互隔离；输入新值才会覆盖。"));
    const focusProvider = this.getAttribute("provider") || "";
    for (const provider of this.data.providers || []) {
      const row = element("div", `qa-setting-row${provider.provider_id === focusProvider ? " focus" : ""}`);
      const meta = element("div", "qa-setting-meta");
      meta.append(element("strong", "", provider.display_name || provider.provider_id));
      meta.append(element("small", "", `${provider.model_id || "未选择模型"} · ${provider.has_key ? "Key 已配置" : provider.provider_id === "local" ? "本地模型" : "Key 未配置"}`));
      const key = document.createElement("input");
      key.type = "password";
      key.autocomplete = "new-password";
      key.placeholder = provider.has_key ? "已配置；输入新值覆盖" : "输入 API Key";
      key.dataset.providerKey = provider.provider_id;
      const test = button("测试", "qa-secondary");
      test.addEventListener("click", () => this.probeProvider(provider.provider_id, test));
      const clearLabel = element("label", "qa-check");
      const clear = document.createElement("input");
      clear.type = "checkbox";
      clear.dataset.providerClear = provider.provider_id;
      clearLabel.append(clear, document.createTextNode(" 清除"));
      row.append(meta, key, test, clearLabel);
      panel.append(row);
    }
    const local = element("div", "qa-setting-row");
    const localMeta = element("div", "qa-setting-meta");
    localMeta.append(element("strong", "", "本地模型"), element("small", "", "地址由管理员白名单管理，用户只选择模型与超时。"));
    const model = document.createElement("input");
    model.placeholder = "模型 ID";
    model.value = this.data.local_llm?.model || "";
    model.dataset.setting = "llm_model";
    const timeout = document.createElement("input");
    timeout.type = "number";
    timeout.min = "0";
    timeout.max = "600";
    timeout.value = String(this.data.local_llm?.timeout || "");
    timeout.placeholder = "超时秒数";
    timeout.dataset.setting = "llm_timeout";
    local.append(localMeta, model, timeout);
    panel.append(local);
  }

  renderNetwork() {
    const panel = this.panel("network");
    panel.replaceChildren(element("h2", "", "网络与代理"));
    panel.append(element("p", "qa-muted", this.data.network?.proxy_configured
      ? `代理已配置：${this.data.network.proxy_scheme}://${this.data.network.proxy_host}:${this.data.network.proxy_port}（凭据不回显）`
      : "当前为直连。只有管理员允许的代理主机可保存。"));
    const proxy = document.createElement("input");
    proxy.type = "password";
    proxy.placeholder = this.data.network?.proxy_configured ? "输入新代理地址以覆盖" : "例如 http://127.0.0.1:1080";
    proxy.dataset.setting = "proxy_http";
    const clearLabel = element("label", "qa-check");
    const clear = document.createElement("input");
    clear.type = "checkbox";
    clear.dataset.setting = "clear_proxy";
    clearLabel.append(clear, document.createTextNode(" 清除代理"));
    const test = button("诊断直连/代理", "qa-secondary");
    test.addEventListener("click", () => this.probeNetwork(test));
    panel.append(proxy, clearLabel, test);
  }

  renderPolicy() {
    const panel = this.panel("policy");
    panel.replaceChildren(element("h2", "", "问答策略"));
    const policy = this.data.policy || {};
    panel.append(element("p", "qa-muted", `默认：标准研究；标准 ${policy.standard_max_hops || 1} 跳，深度 ${policy.deep_max_hops || 2} 跳；二级研究超时 ${policy.research_timeout_seconds || 90} 秒。政策法规问题禁止快速模式。`));
  }

  renderRagflow() {
    const panel = this.panel("ragflow");
    panel.replaceChildren(element("h2", "", "RAGFlow（管理员）"));
    const ragflow = this.data.ragflow || {};
    panel.append(element("p", "qa-muted", ragflow.configured ? "Deep Research Assistant 与 news 知识库已绑定。" : "尚未完成 Assistant 或 news 知识库绑定。"));
    if (this.data.can_manage_ragflow) {
      panel.append(element("code", "qa-code", `Assistant: ${ragflow.assistant_id || "未配置"}\nKB: ${ragflow.kb_id || "未配置"}`));
      const link = element("a", "qa-secondary", "打开管理员配置");
      link.href = "/industry-pack-management";
      panel.append(link);
    } else {
      panel.append(element("p", "qa-muted", "普通用户只能查看就绪状态，不能修改全局 RAGFlow 绑定。"));
    }
  }

  renderHealth() {
    const panel = this.panel("status");
    panel.replaceChildren(element("h2", "", "连接状态"));
    const run = button("立即检查", "qa-primary");
    run.addEventListener("click", () => this.loadHealth(run, panel));
    panel.append(element("p", "qa-muted", "检查已启用模型、代理、RAGFlow Assistant、news 知识库和配置漂移，不返回任何密钥。"), run);
  }

  async save() {
    const keys = {};
    const clearKeys = [];
    for (const input of this.shadowRoot.querySelectorAll("[data-provider-key]")) if (input.value.trim()) keys[input.dataset.providerKey] = input.value.trim();
    for (const input of this.shadowRoot.querySelectorAll("[data-provider-clear]")) if (input.checked) clearKeys.push(input.dataset.providerClear);
    const payload = { keys, clear_keys: clearKeys };
    const proxy = this.shadowRoot.querySelector('[data-setting="proxy_http"]');
    const clearProxy = this.shadowRoot.querySelector('[data-setting="clear_proxy"]');
    if (proxy.value.trim() || clearProxy.checked) payload.proxy_http = clearProxy.checked ? "" : proxy.value.trim();
    payload.llm_model = this.shadowRoot.querySelector('[data-setting="llm_model"]')?.value.trim() || "";
    payload.llm_timeout = Number(this.shadowRoot.querySelector('[data-setting="llm_timeout"]')?.value || 0);
    this.refs.save.disabled = true;
    try {
      await this.request(`${this.apiPrefix}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      this.refs.status.textContent = "设置已保存，下一次提问立即生效。";
      await this.load();
    } catch (error) { this.refs.status.textContent = error.message || "保存失败"; }
    finally { this.refs.save.disabled = false; }
  }

  async probeProvider(provider, control) {
    const prior = control.textContent;
    control.disabled = true;
    control.textContent = "测试中…";
    try {
      const result = await this.request(`${this.apiPrefix}/settings/providers/${encodeURIComponent(provider)}/probe`, { method: "POST" });
      this.refs.status.textContent = result.ready ? `${provider} 连接正常（${result.latency_ms || 0}ms）` : (result.message || result.error || `${provider} 暂不可用`);
    } catch (error) { this.refs.status.textContent = error.message || "连接测试失败"; }
    finally { control.disabled = false; control.textContent = prior; }
  }

  async probeNetwork(control) {
    control.disabled = true;
    try {
      const result = await this.request(`${this.apiPrefix}/settings/network/probe`, { method: "POST" });
      this.refs.status.textContent = result.ready ? (result.configured ? "代理端口可达。" : "当前直连模式正常。") : (result.error || "代理不可达。");
    } catch (error) { this.refs.status.textContent = error.message || "网络诊断失败"; }
    finally { control.disabled = false; }
  }

  async loadHealth(control, panel) {
    control.disabled = true;
    try {
      const result = await this.request(`${this.apiPrefix}/health`);
      const summary = element("div", `qa-health-summary ${result.ready ? "ready" : "degraded"}`);
      summary.append(element("strong", "", result.ready ? "统一问答依赖已就绪" : "存在未就绪依赖"));
      for (const provider of result.providers || []) summary.append(element("p", "", `${provider.name || provider.provider_id}：${provider.ready ? "正常" : "未就绪"}`));
      summary.append(element("p", "", `RAGFlow/news：${result.ragflow?.ready ? "正常" : "未就绪"}`));
      if ((result.config_drift || []).length) summary.append(element("p", "qa-warning-text", `配置漂移：${result.config_drift.join("、")}`));
      panel.append(summary);
    } catch (error) { this.refs.status.textContent = error.message || "健康检查失败"; }
    finally { control.disabled = false; }
  }
}

if (!customElements.get("unified-qa-settings")) customElements.define("unified-qa-settings", UnifiedQaSettings);

export { BUNDLE_VERSION, parseSse };
