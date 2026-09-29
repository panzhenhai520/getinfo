const fs = require("fs");
const path = require("path");
process.env.NODE_PATH = [
  "C:\\Users\\19605\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules",
  "C:\\Users\\19605\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\.pnpm\\node_modules",
].join(";");
require("module").Module._initPaths();
const { chromium } = require("playwright");

const outDir = "C:\\tmp\\ragflow-native-8015-compare";
const htmlPath = path.join(outDir, "星桥项目治理说明.html");
const pdfPath = path.join(outDir, "星桥项目治理说明.pdf");

fs.mkdirSync(outDir, { recursive: true });

const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>星桥项目治理说明</title>
  <style>
    @page { size: A4; margin: 22mm 20mm; }
    body {
      font-family: "Microsoft YaHei", "SimSun", "Noto Sans CJK SC", sans-serif;
      color: #1f2933;
      font-size: 15px;
      line-height: 1.82;
    }
    h1 {
      font-size: 26px;
      margin: 0 0 18px;
      color: #0f172a;
      letter-spacing: 0;
    }
    h2 {
      font-size: 18px;
      margin: 22px 0 8px;
      color: #16324f;
    }
    p { margin: 7px 0; }
    ul { margin: 6px 0 10px 22px; padding: 0; }
    li { margin: 4px 0; }
    .meta {
      padding: 10px 12px;
      border: 1px solid #d8dee8;
      background: #f8fafc;
      margin-bottom: 16px;
    }
    .note {
      margin-top: 18px;
      padding-top: 12px;
      border-top: 1px solid #d8dee8;
      color: #52616f;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <h1>星桥项目治理说明</h1>
  <div class="meta">
    <p><strong>文档编号：</strong>XQ-GOV-2026-0609</p>
    <p><strong>负责人：</strong>林晨</p>
    <p><strong>复盘时间：</strong>每周五下午三点</p>
  </div>

  <h2>一、项目目标</h2>
  <p>星桥项目用于验证知识库问答、多轮上下文和跨语言检索能力。系统回答问题时，必须优先依据上传 PDF 中可以找到的证据，不得编造文档之外的事实。</p>

  <h2>二、三条治理原则</h2>
  <ul>
    <li><strong>证据优先：</strong>回答必须引用或概括知识库中能够检索到的内容。</li>
    <li><strong>连续记忆：</strong>第三轮对话可以使用第一轮已经确认的项目名称、负责人和原则，用户不需要重复完整背景。</li>
    <li><strong>平静交付：</strong>当材料不足时，应明确说明缺少哪些信息，并给出下一步需要补充的材料。</li>
  </ul>

  <h2>三、多轮对话测试线索</h2>
  <p>如果第一轮用户问“星桥项目是谁负责的”，正确答案是林晨。第二轮用户问“它的复盘时间是什么时候”，正确答案是每周五下午三点。第三轮用户问“刚才那个负责人要遵守哪三条原则”，系统应结合第一轮和本文内容回答：证据优先、连续记忆、平静交付。</p>

  <h2>四、跨语言检索说明</h2>
  <p>本文的核心事实可以被翻译成英文或粤语版本，但含义保持一致。中文提问时，如果英文 PDF 或粤语 PDF 中也包含同一事实，系统应该尽量检索并合并这些证据；如果只上传了本中文 PDF，则答案应以本中文 PDF 为准。</p>

  <h2>五、风险规则</h2>
  <p>当用户询问 PDF 未提到的人物观点、未记录的会议结论或没有出现的数字时，系统必须回答“文档中没有找到相关依据”，而不是猜测。</p>

  <p class="note">本 PDF 是 RAGFlow 原生 8015 对比环境的中文检索测试文档，生成时间：2026-06-09。</p>
</body>
</html>`;

fs.writeFileSync(htmlPath, html, "utf8");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.goto("file:///" + htmlPath.replace(/\\/g, "/"));
  await page.pdf({
    path: pdfPath,
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,
  });
  await browser.close();
  console.log(pdfPath);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
