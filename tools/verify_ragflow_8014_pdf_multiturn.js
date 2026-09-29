const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const BASE_URL = "http://192.168.1.246:8014";
const EMAIL = "admin@ragflow.io";
const PASSWORD = "admin";
const PUBLIC_KEY = path.resolve("ragflow_official_v0_25_6_source", "conf", "public.pem");
const WORK_DIR = "C:\\tmp\\ragflow-8014-pdf-multiturn";
const PDF_PATH = path.join(WORK_DIR, "starbridge-governance-note.pdf");
const REPORT_PATH = path.join(WORK_DIR, "result.json");

const ARTICLE_LINES = [
  "Starbridge Governance Note",
  "Date: 2026-06-09",
  "",
  "This short note is designed for a RAGFlow PDF parsing and multi-turn conversation test.",
  "",
  "Project Codename: Starbridge.",
  "",
  "The Starbridge project has three guiding principles:",
  "1. Evidence first: every recommendation must cite a source paragraph from the uploaded PDF.",
  "2. Continuity memory: later answers should remember the named principle from earlier turns.",
  "3. Calm delivery: the assistant should answer with concise, practical language.",
  "",
  "The second principle, Continuity memory, means the assistant should connect the user's later questions to facts already discussed in the same conversation. It should not force the user to repeat the project name, the selected principle, or the earlier answer.",
  "",
  "Operational example: if the first answer says that Continuity memory is the second principle, and the user later asks to expand the second one, the assistant should explain Continuity memory rather than choosing a different principle.",
  "",
  "Owner: Lin Chen.",
  "Review cadence: every Friday afternoon.",
  "Risk rule: do not invent facts that are not present in the PDF.",
];

function escapePdfText(text) {
  return text.replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)");
}

function buildMinimalPdf(lines) {
  const objects = [];
  const add = (body) => {
    objects.push(body);
    return objects.length;
  };

  const fontId = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
  const contentLines = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"];
  lines.forEach((line, index) => {
    if (index > 0) contentLines.push("T*");
    contentLines.push(`(${escapePdfText(line)}) Tj`);
  });
  contentLines.push("ET");
  const content = contentLines.join("\n");
  const contentId = add(`<< /Length ${Buffer.byteLength(content, "ascii")} >>\nstream\n${content}\nendstream`);
  const pageId = add(`<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 ${fontId} 0 R >> >> /Contents ${contentId} 0 R >>`);
  const pagesId = add(`<< /Type /Pages /Kids [${pageId} 0 R] /Count 1 >>`);
  const catalogId = add(`<< /Type /Catalog /Pages ${pagesId} 0 R >>`);

  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((body, index) => {
    offsets.push(Buffer.byteLength(pdf, "ascii"));
    pdf += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf, "ascii");
  pdf += `xref\n0 ${objects.length + 1}\n`;
  pdf += "0000000000 65535 f \n";
  offsets.slice(1).forEach((offset) => {
    pdf += String(offset).padStart(10, "0") + " 00000 n \n";
  });
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root ${catalogId} 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, "ascii");
}

function encryptPassword(password) {
  const keyPem = fs.readFileSync(PUBLIC_KEY, "utf8");
  const key = crypto.createPublicKey({ key: keyPem, passphrase: "Welcome" });
  const payload = Buffer.from(Buffer.from(password, "utf8").toString("base64"), "utf8");
  const encrypted = crypto.publicEncrypt(
    { key, padding: crypto.constants.RSA_PKCS1_PADDING },
    payload,
  );
  return encrypted.toString("base64");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function requestJson(session, url, options, step) {
  const resp = await fetch(url, {
    ...options,
    headers: {
      ...(options && options.headers ? options.headers : {}),
      ...(session.cookie ? { Cookie: session.cookie } : {}),
    },
  });

  const setCookie = resp.headers.get("set-cookie");
  if (setCookie) {
    const cookie = setCookie
      .split(/,(?=[^;,]+=)/)
      .map((part) => part.split(";")[0].trim())
      .join("; ");
    session.cookie = session.cookie ? `${session.cookie}; ${cookie}` : cookie;
  }

  const text = await resp.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (err) {
    throw new Error(`${step} failed: HTTP ${resp.status} non-json ${text.slice(0, 300)}`);
  }
  if (resp.status !== 200 || payload.code !== 0) {
    throw new Error(`${step} failed: HTTP ${resp.status} ${JSON.stringify(payload).slice(0, 1000)}`);
  }
  return payload.data;
}

async function login() {
  const session = { cookie: "" };
  await requestJson(
    session,
    `${BASE_URL}/api/v1/auth/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: EMAIL, password: encryptPassword(PASSWORD) }),
    },
    "login",
  );
  return session;
}

async function createDataset(session) {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const body = {
    name: `codex-pdf-multiturn-${stamp}`,
    description: "Temporary PDF parsing and multi-turn context verification dataset.",
    embedding_model: "bge-m3:latest@Ollama",
    chunk_method: "naive",
    parser_config: {
      chunk_token_num: 256,
      delimiter: "\n\n",
      layout_recognize: "Plain Text",
      html4excel: false,
      raptor: { use_raptor: false },
      graphrag: { use_graphrag: false },
    },
    permission: "me",
  };
  return requestJson(
    session,
    `${BASE_URL}/api/v1/datasets`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    "create dataset",
  );
}

async function uploadDocument(session, datasetId) {
  const form = new FormData();
  const pdf = new Blob([fs.readFileSync(PDF_PATH)], { type: "application/pdf" });
  form.append("file", pdf, path.basename(PDF_PATH));
  const data = await requestJson(
    session,
    `${BASE_URL}/api/v1/datasets/${datasetId}/documents`,
    { method: "POST", body: form },
    "upload pdf",
  );
  return data.map((doc) => ({ id: doc.id, name: doc.name }));
}

async function parseDocuments(session, datasetId, documentIds) {
  await requestJson(
    session,
    `${BASE_URL}/api/v1/datasets/${datasetId}/documents/parse`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds }),
    },
    "parse pdf",
  );
}

async function listDocuments(session, datasetId) {
  const data = await requestJson(
    session,
    `${BASE_URL}/api/v1/datasets/${datasetId}/documents?page=1&page_size=30`,
    { method: "GET" },
    "list documents",
  );
  return (data && (data.docs || data.documents || data.items)) || [];
}

async function pollParse(session, datasetId) {
  const deadline = Date.now() + 8 * 60 * 1000;
  let lastDocs = [];
  while (Date.now() < deadline) {
    lastDocs = await listDocuments(session, datasetId);
    const done = lastDocs.length > 0 && lastDocs.every((doc) => {
      const run = String(doc.run || "").toLowerCase();
      const progress = Number(doc.progress || 0);
      const chunks = Number(doc.chunk_num || doc.chunk_count || 0);
      return (run === "3" || run === "done") && progress >= 1 && chunks > 0;
    });
    if (done) return lastDocs;
    await sleep(5000);
  }
  throw new Error(`document parsing timeout: ${JSON.stringify(lastDocs).slice(0, 1500)}`);
}

async function searchDataset(session, datasetId, question) {
  const body = {
    question,
    doc_ids: [],
    page: 1,
    size: 8,
    top_k: 1024,
    similarity_threshold: 0,
    vector_similarity_weight: 0.9,
    keyword: false,
  };
  const data = await requestJson(
    session,
    `${BASE_URL}/api/v1/datasets/${datasetId}/search`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    "search dataset",
  );
  return ((data && data.chunks) || []).map((chunk) => ({
    document_name: chunk.document_name || chunk.docnm_kwd || chunk.doc_name,
    similarity: chunk.similarity,
    vector_similarity: chunk.vector_similarity,
    term_similarity: chunk.term_similarity,
    content: String(chunk.content || chunk.content_with_weight || "").replace(/\s+/g, " ").slice(0, 260),
  }));
}

async function createChat(session, datasetId) {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const body = {
    name: `codex-pdf-multiturn-chat-${stamp}`,
    dataset_ids: [datasetId],
    llm_id: "deepseek-r1:1.5b@Ollama",
    llm_setting: { model_type: "chat", temperature: 0.05, top_p: 0.4, max_tokens: 360 },
    top_n: 8,
    top_k: 1024,
    similarity_threshold: 0,
    vector_similarity_weight: 0.9,
    prompt_config: {
      system:
        "You are testing RAGFlow multi-turn PDF QA. Answer only from the Knowledge Base. " +
        "For this test, answer in concise Chinese. Hide internal reasoning. " +
        "When the user asks about a previous answer, use the conversation context.\n\n" +
        "Knowledge Base:\n{knowledge}\nEnd Knowledge Base.",
      parameters: [{ key: "knowledge", optional: true }],
      empty_response: "",
      quote: true,
      refine_multiturn: true,
      cross_languages: ["Chinese", "English"],
    },
  };
  return requestJson(
    session,
    `${BASE_URL}/api/v1/chats`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    "create chat",
  );
}

async function readSse(resp) {
  if (!resp.ok) {
    throw new Error(`chat failed: HTTP ${resp.status} ${await resp.text()}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let final = {};
  let eventCount = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() || "";
    for (const raw of lines) {
      if (!raw.startsWith("data:")) continue;
      const data = raw.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      const obj = JSON.parse(data);
      eventCount += 1;
      if (obj && obj.data && typeof obj.data === "object") final = obj.data;
    }
  }
  return { final, eventCount };
}

async function askChat(session, chatId, history, sessionId, question) {
  const msg = { role: "user", content: question, id: crypto.randomUUID() };
  const body = {
    chat_id: chatId,
    session_id: sessionId,
    messages: [...history, msg],
    pass_all_history_messages: true,
    quote: true,
    reasoning: false,
    internet: false,
    stream: true,
  };
  const resp = await fetch(`${BASE_URL}/api/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: session.cookie },
    body: JSON.stringify(body),
  });
  const { final, eventCount } = await readSse(resp);
  const answer = String(final.answer || "").trim();
  const nextSessionId = final.session_id || final.conversation_id || sessionId || "";
  const nextHistory = [
    ...history,
    msg,
    { role: "assistant", content: answer, id: final.id || crypto.randomUUID() },
  ];
  const reference = final.reference || {};
  const docNames = [];
  for (const chunk of reference.chunks || []) {
    const name = chunk.document_name || chunk.docnm_kwd || chunk.doc_name;
    if (name && !docNames.includes(name)) docNames.push(name);
  }
  return {
    result: {
      question,
      answer,
      event_count: eventCount,
      session_id: nextSessionId,
      reference_doc_names: docNames,
      reference_chunk_count: (reference.chunks || []).length,
    },
    history: nextHistory,
    sessionId: nextSessionId,
  };
}

function checkReport(report) {
  const answers = report.chat_turns.map((turn) => turn.answer).join("\n");
  return {
    pdf_created: fs.existsSync(PDF_PATH) && fs.statSync(PDF_PATH).size > 500,
    parsed_has_chunks: report.parsed_documents.some((doc) => Number(doc.chunk_num || 0) > 0),
    retrieval_mentions_starbridge: report.search_top_chunks.some((chunk) => /Starbridge|Continuity memory|Evidence first/.test(chunk.content)),
    turn1_mentions_three_principles: /Evidence first|证据|Continuity memory|上下文|记忆|Calm delivery|简洁|冷静/i.test(report.chat_turns[0].answer),
    turn2_identifies_second_principle: /Continuity memory|连续|上下文|记忆|第二/i.test(report.chat_turns[1].answer),
    turn3_uses_prior_second_principle: /Continuity memory|连续|上下文|记忆|不需要.*重复|前文/i.test(report.chat_turns[2].answer),
    no_visible_reasoning_tags: !/<think>|<\/think>/i.test(answers),
  };
}

async function main() {
  fs.mkdirSync(WORK_DIR, { recursive: true });

  const session = await login();
  let dataset;
  let documents;
  let parsedDocuments;
  if (fs.existsSync(REPORT_PATH)) {
    const prior = JSON.parse(fs.readFileSync(REPORT_PATH, "utf8"));
    dataset = prior.dataset;
    documents = prior.documents;
    parsedDocuments = prior.parsed_documents || await listDocuments(session, dataset.id);
  } else {
    fs.writeFileSync(PDF_PATH, buildMinimalPdf(ARTICLE_LINES));
    dataset = await createDataset(session);
    documents = await uploadDocument(session, dataset.id);
    await parseDocuments(session, dataset.id, documents.map((doc) => doc.id));
    parsedDocuments = await pollParse(session, dataset.id);
  }
  const searchTopChunks = await searchDataset(session, dataset.id, "What does the Starbridge note say about Continuity memory?");
  const chat = await createChat(session, dataset.id);

  let history = [];
  let sessionId = "";
  const chatTurns = [];
  for (const question of [
    "这篇PDF里，Starbridge 项目的三个指导原则是什么？请用中文列出。",
    "刚刚你列出的第二个原则叫什么？只回答名称和一句解释。",
    "请把第一轮答案里的第二点展开说明，并说明为什么这能证明多轮对话接住了上下文。",
  ]) {
    const response = await askChat(session, chat.id, history, sessionId, question);
    chatTurns.push(response.result);
    history = response.history;
    sessionId = response.sessionId;
  }

  const report = {
    time: new Date().toISOString(),
    base_url: BASE_URL,
    pdf_path: PDF_PATH,
    dataset: { id: dataset.id, name: dataset.name },
    documents,
    parsed_documents: parsedDocuments.map((doc) => ({
      id: doc.id,
      name: doc.name,
      run: doc.run,
      progress: doc.progress,
      chunk_num: doc.chunk_num || doc.chunk_count,
      status: doc.status,
    })),
    search_top_chunks: searchTopChunks.slice(0, 5),
    chat: { id: chat.id, name: chat.name },
    chat_turns: chatTurns,
  };
  report.checks = checkReport(report);
  fs.writeFileSync(REPORT_PATH, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(report, null, 2));
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
