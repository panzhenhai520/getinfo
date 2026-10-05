/* Unified QA state model.  This file intentionally uses standards-only
 * TypeScript/JavaScript so the same source can be tested directly by Node and
 * emitted without a framework runtime. */

export const QA_UI_STATE_VERSION = "1.0.0";

export const STAGE_DEFINITIONS = Object.freeze([
  { id: "plan", label: "问题规划" },
  { id: "level1_retrieval", label: "一级资料检索" },
  { id: "level1_draft", label: "一级结论" },
  { id: "level2_retrieval", label: "RAGFlow 深度检索" },
  { id: "level2_research", label: "二级研究" },
  { id: "conflict_review", label: "冲突与缺口核验" },
  { id: "synthesis", label: "综合回答" },
  { id: "citation_validation", label: "引用校验" },
]);

export const EVENT_TYPES = Object.freeze([
  "run_started", "stage_started", "stage_completed", "retrieval_result",
  "level1_ready", "ragflow_research_progress", "conflict_detected",
  "answer_delta", "degraded", "error", "done",
]);

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const FINAL_STAGES = new Set(["synthesis", "citation_validation"]);

function clone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function cleanText(value, limit = 10000) {
  return String(value ?? "").replace(/\0/g, " ").slice(0, limit);
}

function stageRecord(definition) {
  return {
    id: definition.id,
    label: definition.label,
    status: "pending",
    startedAt: "",
    endedAt: "",
    durationMs: null,
    error: null,
  };
}

export function createQaState(seed = {}) {
  const stages = {};
  for (const definition of STAGE_DEFINITIONS) stages[definition.id] = stageRecord(definition);
  return {
    version: QA_UI_STATE_VERSION,
    runId: cleanText(seed.runId || seed.id || "", 100),
    sessionId: cleanText(seed.sessionId || seed.session_id || "", 100),
    industryPackId: cleanText(seed.industryPackId || seed.industry_pack_id || "", 100),
    origin: cleanText(seed.origin || "", 40),
    question: cleanText(seed.question || "", 20000),
    mode: cleanText(seed.mode || "standard", 20),
    status: cleanText(seed.status || "idle", 30),
    currentStage: cleanText(seed.currentStage || seed.current_stage || "", 50),
    stages,
    outputs: {},
    answerDeltas: {},
    streamedAnswer: "",
    finalAnswer: seed.finalAnswer || seed.final_answer || null,
    warnings: [],
    errors: [],
    seenEventIds: {},
    lastEventId: 0,
    connected: false,
    reconnecting: false,
    restored: Boolean(seed.restored),
  };
}

function durationMs(start, end) {
  const left = Date.parse(start || "");
  const right = Date.parse(end || "");
  return Number.isFinite(left) && Number.isFinite(right) && right >= left ? right - left : null;
}

function completeStage(state, stage, timestamp, status = "completed") {
  if (!state.stages[stage]) return;
  const item = state.stages[stage];
  if (item.status === "cancelled" || item.status === "failed") return;
  item.status = status;
  item.endedAt = cleanText(timestamp, 80);
  item.durationMs = durationMs(item.startedAt, item.endedAt);
}

function storeOutput(state, stage, payload) {
  const result = payload && typeof payload.result === "object" ? payload.result : payload;
  if (!result || typeof result !== "object") return;
  state.outputs[stage] = result;
  if (FINAL_STAGES.has(stage) && typeof result.answer === "string") state.finalAnswer = result;
}

function mergeStream(state) {
  state.streamedAnswer = Object.entries(state.answerDeltas)
    .sort((left, right) => Number(left[0]) - Number(right[0]))
    .map((entry) => entry[1])
    .join("");
}

export function applyQaEvent(previous, rawEvent) {
  if (!rawEvent || typeof rawEvent !== "object") return previous;
  const eventId = Number(rawEvent.event_id || 0);
  if (!Number.isInteger(eventId) || eventId <= 0 || previous.seenEventIds[eventId]) return previous;
  if (previous.runId && rawEvent.run_id && previous.runId !== String(rawEvent.run_id)) return previous;

  const state = clone(previous);
  state.seenEventIds[eventId] = true;
  state.lastEventId = Math.max(Number(state.lastEventId || 0), eventId);
  state.runId ||= cleanText(rawEvent.run_id, 100);
  const type = cleanText(rawEvent.type, 80);
  const stage = cleanText(rawEvent.stage, 80);
  const timestamp = cleanText(rawEvent.timestamp, 80);
  const payload = rawEvent.payload && typeof rawEvent.payload === "object" ? rawEvent.payload : {};

  if (type === "run_started") {
    state.status = "running";
    state.currentStage = stage;
    state.connected = true;
    state.reconnecting = false;
    const enabled = new Set(Array.isArray(payload.stages) ? payload.stages : []);
    if (enabled.size) {
      for (const item of Object.values(state.stages)) {
        if (!enabled.has(item.id)) item.status = "skipped";
      }
    }
  } else if (type === "stage_started") {
    state.status = "running";
    state.currentStage = stage;
    if (state.stages[stage] && !["completed", "degraded"].includes(state.stages[stage].status)) {
      state.stages[stage].status = "running";
      state.stages[stage].startedAt ||= timestamp;
      state.stages[stage].error = null;
    }
  } else if (["stage_completed", "retrieval_result", "level1_ready", "ragflow_research_progress", "conflict_detected"].includes(type)) {
    storeOutput(state, stage, payload);
    completeStage(state, stage, timestamp);
  } else if (type === "answer_delta") {
    const offset = Number.isFinite(Number(payload.offset)) ? Number(payload.offset) : eventId * 100000;
    state.answerDeltas[String(offset)] = cleanText(payload.delta, 5000);
    mergeStream(state);
  } else if (type === "degraded") {
    completeStage(state, stage, timestamp, "degraded");
    state.warnings.push({ ...payload, stage, eventId });
  } else if (type === "error") {
    if (state.stages[stage]) {
      state.stages[stage].status = "failed";
      state.stages[stage].endedAt = timestamp;
      state.stages[stage].durationMs = durationMs(state.stages[stage].startedAt, timestamp);
      state.stages[stage].error = payload;
    }
    state.status = payload.retryable ? "retry_wait" : "failed";
    state.currentStage = stage;
    state.errors.push({ ...payload, stage, eventId });
  } else if (type === "done") {
    const terminal = cleanText(payload.status || "completed", 30);
    state.status = terminal;
    state.currentStage = terminal === "completed" ? "completed" : stage;
    if (payload.final_answer && typeof payload.final_answer === "object") state.finalAnswer = payload.final_answer;
    if (terminal === "cancelled" && state.stages[stage]) completeStage(state, stage, timestamp, "cancelled");
    state.connected = false;
    state.reconnecting = false;
  }
  return state;
}

export function stateFromRun(run) {
  const state = createQaState({
    ...run,
    runId: run?.run_id || run?.id,
    sessionId: run?.session_id,
    industryPackId: run?.industry_pack_id,
    currentStage: run?.current_stage,
    finalAnswer: run?.final_answer,
    restored: true,
  });
  const status = cleanText(run?.status || "idle", 30);
  state.status = status;
  state.connected = !TERMINAL.has(status);
  if (status === "completed") {
    for (const item of Object.values(state.stages)) item.status = "completed";
  }
  if (status === "cancelled" && state.stages[state.currentStage]) state.stages[state.currentStage].status = "cancelled";
  if (status === "failed" && state.stages[state.currentStage]) state.stages[state.currentStage].status = "failed";
  return state;
}

export function markConnection(previous, connected) {
  const state = clone(previous);
  state.connected = Boolean(connected);
  state.reconnecting = !connected && !TERMINAL.has(state.status) && state.status !== "idle";
  return state;
}

export function currentAnswer(state) {
  return cleanText(state?.finalAnswer?.answer || state?.streamedAnswer || "", 80000);
}

export function safeHttpUrl(value, base = "") {
  try {
    const url = new URL(String(value || ""), base || "http://localhost");
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_error) {
    return "";
  }
}

export function safeActionHref(value) {
  const raw = String(value || "");
  if (!raw.startsWith("/") || raw.startsWith("//")) return "";
  return raw.replace(/[\u0000-\u001f]/g, "").slice(0, 1000);
}

export function groupLevel1Evidence(state) {
  const retrieval = state?.outputs?.level1_retrieval || {};
  const draft = state?.outputs?.level1_draft || {};
  const source = [...(retrieval.evidence || []), ...(draft.evidence || [])];
  const groups = { article: [], page_context: [], web: [] };
  const seen = new Set();
  for (const item of source) {
    const ref = cleanText(item?.evidence_ref, 200);
    if (!ref || seen.has(ref)) continue;
    seen.add(ref);
    const type = cleanText(item?.source_type, 80).toLowerCase();
    if (type.includes("page")) groups.page_context.push(item);
    else if (type.includes("web")) groups.web.push(item);
    else groups.article.push(item);
  }
  return groups;
}

export function groupLevel2Evidence(state) {
  const retrieval = state?.outputs?.level2_retrieval || {};
  const documents = new Map();
  for (const item of retrieval.evidence || []) {
    const key = cleanText(item.document_id || item.evidence_ref, 300);
    if (!key) continue;
    if (!documents.has(key)) documents.set(key, {
      documentId: key,
      title: cleanText(item.title || "RAGFlow 文档", 500),
      sourceUrl: safeHttpUrl(item.source_url),
      publishedAt: item.published_at || null,
      axes: new Set(),
      chunks: [],
    });
    const document = documents.get(key);
    document.axes.add(cleanText(item?.metadata?.axis || "original", 40));
    document.chunks.push(item);
  }
  return [...documents.values()].map((document) => ({ ...document, axes: [...document.axes] }));
}

export function evidenceIndex(state) {
  const result = new Map();
  for (const output of Object.values(state?.outputs || {})) {
    for (const item of output?.evidence || []) {
      if (item?.evidence_ref) result.set(String(item.evidence_ref), item);
    }
  }
  for (const item of state?.finalAnswer?.evidence || []) {
    if (item?.evidence_ref) result.set(String(item.evidence_ref), item);
  }
  return result;
}

export function citationTarget(state, label) {
  const ref = state?.finalAnswer?.citation_map?.[label] || "";
  return evidenceIndex(state).get(ref) || null;
}

export function stageDurationLabel(value) {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0)} s`;
}
