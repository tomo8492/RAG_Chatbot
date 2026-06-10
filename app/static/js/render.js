/* ============================================================
   メッセージ描画
   ============================================================ */
function renderMessages(messages) {
  const box = $("messages");
  box.innerHTML = "";
  if (!messages.length) {
    box.appendChild(buildWelcome());
    return;
  }
  messages.forEach((m, i) => {
    const isLastAssistant = m.role === "assistant" && i === messages.length - 1;
    box.appendChild(renderMessage(m, isLastAssistant));
  });
  scrollToBottom();
}

function buildWelcome() {
  const w = el("div", "welcome");
  if (State.mode === "code") {
    w.innerHTML = `<div class="brand-big"><span class="dot">${LOGO_SVG}</span></div>
      <h2>コードエージェント</h2>
      <p class="muted">作業フォルダを選び、依頼を入力してください。<br/>
      <strong>計画モード</strong>では、AIがまず調査して<strong>実行計画</strong>を提示し、<strong>承認</strong>すると実行します。<br/>
      ファイル編集は自動適用、コマンド実行など重要操作は都度確認します。</p>`;
  } else {
    w.innerHTML = `<div class="brand-big"><span class="dot">${LOGO_SVG}</span></div>
      <h2>こんにちは</h2>
      <p class="muted">参照資料フォルダを選んで質問するか、そのまま会話を始められます。<br/>
      ファイルを添付して内容について質問することもできます。</p>`;
  }
  return w;
}

function renderMessage(m, isLastAssistant) {
  if (m.role === "user") {
    const row = el("div", "msg-row user");
    const bubble = el("div", "bubble");
    bubble.textContent = m.content;
    if (m.attachments && m.attachments.length) {
      const att = el("div", "attach-chips");
      m.attachments.forEach((a) => {
        if (a && typeof a === "object" && a.type === "image" && a.file) {
          const im = el("img", "msg-img");
          im.src = "/api/uploads/" + encodeURIComponent(a.file);
          att.appendChild(im);
        } else {
          att.appendChild(el("span", "chip", "📎 " + escapeHtml(a)));
        }
      });
      bubble.appendChild(att);
    }
    row.appendChild(bubble);
    if (m.id) row.appendChild(buildUserActions(m, row, bubble));   // 編集/削除
    return row;
  }
  // assistant — Codeの会話で保存済みステップ(計画/ツール/差分/TODO)があれば再現
  if (State.current && (State.current.kind || "chat") === "code" &&
      Array.isArray(m.sources) && m.sources.length && m.sources[0] && m.sources[0].type) {
    const { row, logBox } = buildAgentRow();
    renderCodeSteps(logBox, m.sources);
    return row;
  }
  const { row, refs } = createAssistantRow();
  renderMarkdown(refs.md, m.content, true);
  refs.row.dataset.raw = m.content;
  if (m.sources && m.sources.length) renderSources(refs.src, m.sources);
  buildAssistantActions(refs, isLastAssistant, m);
  return row;
}

function createAssistantRow() {
  const row = el("div", "msg-row assistant");
  row.innerHTML = `
    <div class="avatar">${LOGO_SVG}</div>
    <div class="msg-body">
      <details class="thinking hidden"><summary>💭 思考過程</summary><div class="think-text"></div></details>
      <div class="md"></div>
      <div class="sources"></div>
      <div class="msg-actions"></div>
    </div>`;
  const refs = {
    row,
    avatar: row.querySelector(".avatar"),
    think: row.querySelector(".thinking"),
    thinkText: row.querySelector(".think-text"),
    md: row.querySelector(".md"),
    src: row.querySelector(".sources"),
    actions: row.querySelector(".msg-actions"),
  };
  return { row, refs };
}

function buildAssistantActions(refs, isLast, m) {
  refs.actions.innerHTML = "";
  const copy = el("button", null, "📋 コピー");
  copy.onclick = () => {
    navigator.clipboard.writeText(refs.row.dataset.raw || "").then(() => toast("コピーしました"));
  };
  refs.actions.appendChild(copy);
  refs.actions.appendChild(makeSaveMenu(() => refs.row.dataset.raw || "",
                                        () => (refs.src && refs.src._figures) || []));
  if (isLast && (!State.current || (State.current.kind || "chat") !== "code")) {
    const regen = el("button", null, "↻ 再生成");
    regen.onclick = () => regenerate();
    refs.actions.appendChild(regen);
  }
  if (m && m.id) {
    const del = el("button", null, "🗑 削除");
    del.onclick = () => deleteMessage(m, refs.row);
    refs.actions.appendChild(del);
  }
}

/* ---------- メッセージの編集 / 削除 ---------- */
async function reloadCurrentMessages() {
  if (!State.current) return;
  const conv = await api(`/api/conversations/${State.current.id}`);
  State.current = conv;
  renderMessages(conv.messages || []);
}

async function deleteMessage(m, row) {
  if (State.streaming) { toast("生成中は操作できません"); return; }
  if (!m.id || !State.current) return;
  if (!confirm("このメッセージを削除しますか?")) return;
  try {
    await api(`/api/conversations/${State.current.id}/messages/${m.id}`, { method: "DELETE" });
    row.remove();
  } catch (e) { toast("削除に失敗: " + e.message); }
}

function buildUserActions(m, row, bubble) {
  const actions = el("div", "msg-actions user-actions");
  if (m.id && State.mode !== "code") {       // 編集→再生成はチャットのみ
    const edit = el("button", null, "✎ 編集");
    edit.onclick = () => startEditUserMessage(m, row, bubble);
    actions.appendChild(edit);
  }
  if (m.id) {
    const del = el("button", null, "🗑 削除");
    del.onclick = () => deleteMessage(m, row);
    actions.appendChild(del);
  }
  return actions;
}

function startEditUserMessage(m, row, bubble) {
  if (State.streaming) { toast("生成中は編集できません"); return; }
  const ta = el("textarea", "edit-area"); ta.value = m.content;
  const bar = el("div", "edit-bar");
  const save = el("button", "btn primary", "保存して再生成");
  const cancel = el("button", "btn", "キャンセル");
  bar.appendChild(save); bar.appendChild(cancel);
  row.innerHTML = ""; row.appendChild(ta); row.appendChild(bar);
  ta.focus(); ta.style.height = Math.min(ta.scrollHeight + 4, 300) + "px";
  cancel.onclick = () => reloadCurrentMessages();
  save.onclick = async () => {
    const txt = ta.value.trim();
    if (!txt) { toast("空にはできません"); return; }
    save.disabled = cancel.disabled = true;
    try {
      await api(`/api/conversations/${State.current.id}/messages/${m.id}`,
        { method: "PATCH", body: JSON.stringify({ content: txt, truncate_after: true }) });
      await reloadCurrentMessages();           // 編集を反映(以降は削除済み)
      await streamAssistant({ mode: "regenerate" });   // 編集後の依頼で再生成
      await loadConversations();
    } catch (e) { toast("編集に失敗: " + e.message); save.disabled = cancel.disabled = false; }
  };
}

/* ---------- 保存(ファイル出力) ---------- */
const SAVE_FORMATS = [
  ["Markdown (.md)", "md"], ["テキスト (.txt)", "txt"], ["HTML (.html)", "html"],
  ["PDF (.pdf)", "pdf"], ["Word (.docx)", "docx"],
  ["Excel (.xlsx)", "xlsx"], ["CSV (.csv)", "csv"], ["PowerPoint (.pptx)", "pptx"],
];
function currentTitle() { return (State.current && State.current.title) || "回答"; }

function makeSaveMenu(getContent, getFigures) {
  const wrap = el("span", "save-wrap");
  const btn = el("button", null, "⬇ 保存 ▾");
  const menu = el("div", "save-menu hidden");
  SAVE_FORMATS.forEach(([label, fmt]) => {
    const item = el("div", "save-item", label);
    item.onclick = (e) => {
      e.stopPropagation();
      menu.classList.add("hidden");
      exportContent(getContent(), fmt, null, currentTitle(),
                    getFigures ? getFigures() : null);
    };
    menu.appendChild(item);
  });
  btn.onclick = (e) => {
    e.stopPropagation();
    document.querySelectorAll(".save-menu").forEach((m) => { if (m !== menu) m.classList.add("hidden"); });
    menu.classList.toggle("hidden");
  };
  wrap.appendChild(btn);
  wrap.appendChild(menu);
  return wrap;
}

const LANG_EXT = {
  html: "html", xml: "xml", javascript: "js", js: "js", typescript: "ts", ts: "ts",
  python: "py", py: "py", json: "json", css: "css", bash: "sh", sh: "sh", shell: "sh",
  sql: "sql", java: "java", c: "c", cpp: "cpp", "c++": "cpp", csharp: "cs", cs: "cs",
  go: "go", rust: "rs", php: "php", ruby: "rb", yaml: "yaml", yml: "yaml",
  markdown: "md", md: "md", vb: "bas", vba: "bas", vbnet: "bas", basic: "bas", vbscript: "bas",
};
function extForLang(l) { return LANG_EXT[(l || "").toLowerCase()] || "txt"; }

async function exportContent(content, fmt, ext, title, srcFigures) {
  if (!content || !content.trim()) { toast("内容が空です"); return; }
  try {
    let images = null;
    if (fmt === "pdf" || fmt === "docx" || fmt === "pptx") {
      try { images = await collectMermaidImages(content); }   // 図をPNG化して文書に埋め込む
      catch (_) { images = null; }
    }
    // 出典の文書内画像(参考図)を取得して base64 で同送(対応形式のみ)
    let figures = null;
    if (srcFigures && srcFigures.length && ["html", "pdf", "docx", "pptx", "xlsx"].includes(fmt)) {
      figures = [];
      for (const f of srcFigures.slice(0, 8)) {
        try {
          const r = await fetch(f.url);
          if (!r.ok) continue;
          const blob = await r.blob();
          const b64 = await new Promise((resolve, reject) => {
            const fr = new FileReader();
            fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
            fr.onerror = reject;
            fr.readAsDataURL(blob);
          });
          if (b64) figures.push({ data: b64, caption: f.caption || "" });
        } catch (_) { /* 取得できない図はスキップ */ }
      }
      if (!figures.length) figures = null;
    }
    const res = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, format: fmt, ext, title, images, figures }),
    });
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) { let d; try { d = (await res.json()).detail; } catch (_) {} throw new Error(d || "変換に失敗"); }
    const blob = await res.blob();
    let fname = "download." + (ext || fmt);
    const xf = res.headers.get("X-Filename");
    if (xf) { try { fname = decodeURIComponent(xf); } catch (_) { fname = xf; } }
    const url = URL.createObjectURL(blob);
    const a = el("a"); a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    toast("保存しました: " + fname);
  } catch (e) { toast("保存失敗: " + e.message); }
}

function renderSources(container, sources, note) {
  container.innerHTML = "";
  if (!sources || !sources.length) {
    // 参照フォルダは選択済みだが該当が無いとき等の明示(strict-RAG の透明性)
    if (note) container.appendChild(el("span", "src-title src-empty", "🔍 " + note));
    return;
  }
  // 思考(.thinking)と同じく折りたたみ表示。既定は閉じておき、クリックで展開する。
  const details = el("details", "src-details");
  const summary = el("summary", "src-summary", `📎 参照ファイル (${sources.length})`);
  details.appendChild(summary);
  const list = el("div", "src-list");
  sources.forEach((s) => {
    const label = `${s.source}${s.loc ? " " + s.loc : ""}${s.attachment ? " (添付)" : ""}`;
    const item = el("span", "src-item", escapeHtml(label));
    if (s.text && s.text.trim()) {           // 原文(該当チャンク)があればクリックで表示
      item.classList.add("src-clickable");
      item.title = "クリックで該当箇所(原文)を表示";
      item.onclick = (e) => { e.stopPropagation(); showSourcePopover(item, s); };
    }
    list.appendChild(item);
  });
  details.appendChild(list);
  // 出典に紐づく文書内の図(サムネイル)。クリックで原寸を別タブ表示。
  // 収集結果は container._figures に保持し、「⬇ 保存」時の参考図埋め込みにも使う
  const figures = [];
  sources.forEach((s) => (s.images || []).forEach((u) => {
    if (typeof u === "string" && u.startsWith("/api/doc-images/") && !figures.some((f) => f.url === u))
      figures.push({ url: u, caption: `${s.source}${s.loc ? " " + s.loc : ""}` });
  }));
  container._figures = figures;
  if (figures.length) {
    const box = el("div", "src-images");
    figures.slice(0, 8).forEach((f) => {
      const im = el("img", "src-thumb");
      im.src = f.url; im.loading = "lazy"; im.alt = "文書内の図"; im.title = "クリックで原寸表示";
      im.onclick = (e) => { e.stopPropagation(); window.open(f.url, "_blank"); };
      box.appendChild(im);
    });
    details.appendChild(box);
    summary.textContent = `📎 参照ファイル (${sources.length}) ・ 🖼 図 ${figures.length}`;
  }
  container.appendChild(details);
}

function showSourcePopover(anchor, s) {
  const existing = document.querySelector(".src-popover");
  const sameAnchor = existing && existing._anchor === anchor;
  if (existing) existing.remove();
  if (sameAnchor) return;                    // 同じ出典の再クリックは閉じる(トグル)
  const pop = el("div", "src-popover");
  pop._anchor = anchor;
  const head = el("div", "src-pop-head");
  head.appendChild(el("span", "src-pop-title", escapeHtml(`${s.source}${s.loc ? " · " + s.loc : ""}`)));
  const x = el("button", "src-pop-x", "✕"); x.setAttribute("aria-label", "閉じる"); x.onclick = () => pop.remove();
  head.appendChild(x);
  pop.appendChild(head);
  const body = el("div", "src-pop-body"); body.textContent = s.text || "(本文なし)";
  pop.appendChild(body);
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 12)) + "px";
  pop.style.top = (r.bottom + 6 + window.scrollY) + "px";
  setTimeout(() => {
    const close = (ev) => {
      if (!pop.contains(ev.target) && ev.target !== anchor) { pop.remove(); document.removeEventListener("click", close); }
    };
    document.addEventListener("click", close);
  }, 0);
}

/* ---------- Markdown ---------- */
marked.setOptions({ breaks: true, gfm: true });

/* Qwen3 等が content に混入させる <think> をレンダリング前に除去(ストリーミング対応)。
   毎回バッファ全体へ適用するため、開きのみ/閉じのみの不完全タグにも自然対応する。 */
function stripThink(text) {
  if (!text) return text || "";
  let s = text;
  // gpt-oss(harmony): final 以降を本文に、無ければ analysis/commentary を除去
  const fm = s.match(/<\|channel\|>\s*final\b[\s\S]*?<\|message\|>/i);
  if (fm) s = s.slice(s.indexOf(fm[0]) + fm[0].length);
  else s = s.replace(/<\|channel\|>\s*(?:analysis|commentary)\b[\s\S]*?(?=<\|channel\|>|<\|end\|>|<\|return\|>|$)/gi, "");
  s = s.replace(/<think(?:ing)?\b[^>]*>[\s\S]*?<\/think(?:ing)?\s*>/gi, "");   // 完全な <think>/<thinking>
  if (/<\/think(?:ing)?\s*>/i.test(s) && !/<think(?:ing)?\b/i.test(s))         // 閉じのみ → 先頭〜閉じを除去
    s = s.replace(/^[\s\S]*?<\/think(?:ing)?\s*>/i, "");
  if (/<think(?:ing)?\b/i.test(s))                                            // 開きのみ → 開き〜末尾を除去
    s = s.replace(/<think(?:ing)?\b[^>]*>[\s\S]*$/i, "");
  // 漏れたチャットテンプレ特殊トークンを除去
  s = s.replace(/<\|\/?(?:im_start|im_end|eot_id|start_header_id|end_header_id|start|end|message|channel|return|constrain|begin_of_text|end_of_text|assistant|user|system|python|tool)\|>/gi, "");
  return s;
}

function renderMarkdown(target, text, final) {
  const html = DOMPurify.sanitize(marked.parse(stripThink(text || "")));
  target.innerHTML = html;
  if (final) enhanceCode(target);
}
function enhanceCode(container) {
  container.querySelectorAll("pre code").forEach((code) => {
    if (code.classList.contains("language-mermaid")) return;   // Mermaid は描画側で処理
    try { hljs.highlightElement(code); } catch (_) {}
    const pre = code.parentElement;
    if (pre.querySelector(".code-head")) return;
    let lang = "code";
    code.classList.forEach((c) => { if (c.startsWith("language-")) lang = c.slice(9); });
    const head = el("div", "code-head");
    head.appendChild(el("span", null, lang));
    const right = el("span");
    right.style.display = "flex"; right.style.gap = "10px";
    const dl = el("button", "code-copy", "⬇ ." + extForLang(lang));
    dl.title = "コードをダウンロード";
    dl.onclick = () => exportContent(code.textContent, "code", extForLang(lang), lang || "code");
    const btn = el("button", "code-copy", "コピー");
    btn.onclick = () => navigator.clipboard.writeText(code.textContent).then(() => toast("コピーしました"));
    right.appendChild(dl); right.appendChild(btn);
    head.appendChild(right);
    pre.insertBefore(head, code);
  });
  linkifyFileRefs(container);
  renderMermaidBlocks(container);
}

/* ---- Mermaid 図のレンダリング(sandbox iframe・失敗時は生コードへフォールバック) ---- */

/* デザイントークン(色・角丸・フォント・余白を1か所集約。色変更はここだけ) */
const MERMAID_TOKENS = {
  radius: 8,
  font: '"Yu Gothic UI","Hiragino Kaku Gothic ProN","Noto Sans JP","Meiryo",system-ui,sans-serif',
  // ノード種別: process=通常(既定/青), startend=開始終了(白+濃枠), decision=判定(ピンク),
  //            accent1=特殊A(橙), accent2=特殊B(緑)
  light: {
    process:  { bg: "#E7EEFF", border: "#4C6EF5", text: "#1E3A8A" },
    startend: { bg: "#FFFFFF", border: "#3A3833", text: "#2B2A27" },
    decision: { bg: "#FCE3EC", border: "#D6336C", text: "#7A1F3D" },
    accent1:  { bg: "#FFEFDD", border: "#E8590C", text: "#8A3B0B" },
    accent2:  { bg: "#E4F3E8", border: "#2F9E44", text: "#1B5E2A" },
    line: "#7C776B", edge: "#3A3833", edgeBg: "#F0EEE6",
    cluster: "#F0EEE6", clusterBorder: "#D9D4C7",
  },
  dark: {
    process:  { bg: "#27314F", border: "#5C7CFA", text: "#CDD9FF" },
    startend: { bg: "#3A3833", border: "#CDC8BC", text: "#F5F2EA" },
    decision: { bg: "#422836", border: "#E64980", text: "#FAD1E0" },
    accent1:  { bg: "#3B2A1C", border: "#FD7E14", text: "#FFD8A8" },
    accent2:  { bg: "#22341F", border: "#51CF66", text: "#C3F0CA" },
    line: "#7A7568", edge: "#CFCABD", edgeBg: "#1E1D1A",
    cluster: "#262521", clusterBorder: "#3A3833",
  },
};

function mermaidConfig(forceDark) {
  const dark = (forceDark !== undefined) ? forceDark
    : (document.documentElement.getAttribute("data-theme") === "dark");
  const t = dark ? MERMAID_TOKENS.dark : MERMAID_TOKENS.light;
  const R = MERMAID_TOKENS.radius, F = MERMAID_TOKENS.font;
  const themeVariables = {
    fontFamily: F, fontSize: "14px",
    primaryColor: t.process.bg, primaryBorderColor: t.process.border, primaryTextColor: t.process.text,
    mainBkg: t.process.bg, nodeBorder: t.process.border, nodeTextColor: t.process.text,
    lineColor: t.line, textColor: t.edge,
    clusterBkg: t.cluster, clusterBorder: t.clusterBorder,
    edgeLabelBackground: t.edgeBg,
  };
  const C = (n) => t[n];
  const themeCSS = `
    .node rect, .node .basic, .node polygon { rx:${R}px; ry:${R}px; stroke-width:1.6px; stroke-linejoin:round; }
    .nodeLabel, .edgeLabel, .label, .cluster .nodeLabel { font-family:${F}; }
    .nodeLabel { font-weight:500; }
    .label foreignObject, .nodeLabel { padding:0 2px; }
    .edgeLabel, .edgeLabel p { background:transparent !important; color:${t.edge} !important; font-weight:600; }
    .edgeLabel rect, .edgeLabel foreignObject { fill:${t.edgeBg}; opacity:.9; }
    .flowchart-link, .edgePath .path { stroke:${t.line}; stroke-width:1.5px; }
    .cluster rect { rx:10px; ry:10px; fill:${t.cluster}; stroke:${t.clusterBorder}; }
    .node polygon { fill:${C("decision").bg} !important; stroke:${C("decision").border} !important; }   /* 安全網: ひし形=判定色 */
    .node.decision polygon { fill:${C("decision").bg} !important; stroke:${C("decision").border} !important; }
    .node.decision .nodeLabel { color:${C("decision").text} !important; }
    .node.startend rect, .node.startend .basic, .node.startend path { fill:${C("startend").bg} !important; stroke:${C("startend").border} !important; stroke-width:2px !important; }
    .node.startend .nodeLabel { color:${C("startend").text} !important; font-weight:700; }
    .node.accent1 rect, .node.accent1 .basic { fill:${C("accent1").bg} !important; stroke:${C("accent1").border} !important; }
    .node.accent1 .nodeLabel { color:${C("accent1").text} !important; }
    .node.accent2 rect, .node.accent2 .basic { fill:${C("accent2").bg} !important; stroke:${C("accent2").border} !important; }
    .node.accent2 .nodeLabel { color:${C("accent2").text} !important; }
  `;
  return { themeVariables, themeCSS };
}

let _mermaidReady = false;
function initMermaid(forceDark) {
  if (!window.mermaid) return false;
  const { themeVariables, themeCSS } = mermaidConfig(forceDark);
  try {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "sandbox",          // 図内の JS 実行を iframe で遮断
      theme: "base",                     // base + themeVariables/themeCSS で完全カスタム
      themeVariables,
      themeCSS,
      fontFamily: MERMAID_TOKENS.font,
      flowchart: { htmlLabels: true, padding: 14, nodeSpacing: 50, rankSpacing: 55, useMaxWidth: true },
    });
    _mermaidReady = true;
    return true;
  } catch (_) { return false; }
}
async function renderMermaidBlocks(container) {
  if (!window.mermaid) return;
  if (!_mermaidReady && !initMermaid()) return;
  const blocks = container.querySelectorAll("pre > code.language-mermaid");
  for (const code of blocks) {
    const pre = code.parentElement;
    if (pre.dataset.mmHandled) continue;
    pre.dataset.mmHandled = "1";
    const src = (code.textContent || "").trim();
    const id = "mmd-" + Math.random().toString(36).slice(2, 9);
    // 生コードのチラ見えを防ぐため、まずプレースホルダへ即置換してから非同期で描画する
    const slot = el("div", "mermaid-fig mermaid-pending");
    slot.textContent = "📊 図を描画中…";
    pre.replaceWith(slot);
    // 一時コンテナで描画(失敗時に mermaid が残すエラー図ごと破棄できる)
    const tmp = el("div"); tmp.style.cssText = "position:absolute;left:-99999px;top:0";
    document.body.appendChild(tmp);
    try {
      const { svg } = await window.mermaid.render(id, src, tmp);
      const fig = el("div", "mermaid-fig");
      fig.dataset.src = src;
      fig.innerHTML = svg;
      addDiagramTools(fig);                        // SVG/PNG 保存ボタン
      slot.replaceWith(fig);                       // 成功 → 図に置換
    } catch (err) {
      // 失敗 → 生コード＋注記を表示(従来のフォールバック)
      const wrap = el("div");
      wrap.appendChild(el("div", "mermaid-error", "⚠ 図の描画に失敗しました(コードを表示)"));
      const p2 = el("pre"); const c2 = el("code", "language-mermaid");
      c2.textContent = src; p2.appendChild(c2); wrap.appendChild(p2);
      slot.replaceWith(wrap);
    } finally {
      tmp.remove();
      [id, "d" + id].forEach((x) => { const n = document.getElementById(x); if (n) n.remove(); });
    }
  }
}
/* テーマ切替時に既存の図を新テーマで再描画 */
async function rerenderMermaidTheme() {
  if (!window.mermaid || !_mermaidReady) return;
  _mermaidReady = false; initMermaid();
  const figs = document.querySelectorAll(".mermaid-fig[data-src]");
  for (const fig of figs) {
    const src = fig.dataset.src; if (!src) continue;
    try {
      const { svg } = await window.mermaid.render("mmd-" + Math.random().toString(36).slice(2, 9), src);
      fig.innerHTML = svg;
      addDiagramTools(fig);
    } catch (_) {}
  }
}

/* 図(Mermaid)を SVG / PNG で保存するツール(Claude風の図エクスポート) */
function addDiagramTools(fig) {
  const tools = el("div", "diagram-tools");
  for (const kind of ["SVG", "PNG"]) {
    const btn = el("button", "diagram-tool", kind);
    btn.title = `図を ${kind} で保存`;
    btn.onclick = (e) => { e.stopPropagation(); downloadDiagram(fig, kind.toLowerCase()); };
    tools.appendChild(btn);
  }
  fig.appendChild(tools);
}

function _bg() {
  return (getComputedStyle(document.documentElement).getPropertyValue("--bg") || "#ffffff").trim() || "#ffffff";
}

// 図のSVG文字列を取り出す。sandbox描画では svg は iframe(data URL)の中にあるので復元する。
function _figSvgString(fig) {
  const direct = fig.querySelector("svg");
  if (direct) return new XMLSerializer().serializeToString(direct);
  const ifr = fig.querySelector("iframe");
  if (ifr && ifr.src && ifr.src.startsWith("data:")) {
    try {
      const comma = ifr.src.indexOf(",");
      const meta = ifr.src.slice(0, comma);
      let html = ifr.src.slice(comma + 1);
      html = meta.includes("base64") ? decodeURIComponent(escape(atob(html))) : decodeURIComponent(html);
      const m = html.match(/<svg[\s\S]*?<\/svg>/i);
      return m ? m[0] : null;
    } catch (e) { return null; }
  }
  return null;
}

// svg文字列 → PNG dataURL(寸法つき)。bg は背景色。
function svgToPngDataUrl(xml, scale, bg) {
  scale = scale || 2;
  return new Promise((resolve, reject) => {
    let w = 800, h = 600;
    const probe = el("div"); probe.innerHTML = xml;
    const ps = probe.querySelector("svg");
    if (ps) {
      const vb = ps.viewBox && ps.viewBox.baseVal;
      w = (vb && vb.width) || parseFloat(ps.getAttribute("width")) || 800;
      h = (vb && vb.height) || parseFloat(ps.getAttribute("height")) || 600;
    }
    const img = new Image();
    img.onload = () => {
      const canvas = el("canvas"); canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = bg || "#ffffff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.scale(scale, scale); ctx.drawImage(img, 0, 0, w, h);
      resolve({ dataUrl: canvas.toDataURL("image/png"), w, h });
    };
    img.onerror = () => reject(new Error("svg→png 失敗"));
    img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(xml)));
  });
}

async function downloadDiagram(fig, kind) {
  const xml = _figSvgString(fig);
  if (!xml) { toast("図の取得に失敗しました"); return; }
  const name = "diagram-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "");
  if (kind === "svg") {
    const blob = new Blob(['<?xml version="1.0" encoding="UTF-8"?>\n' + xml], { type: "image/svg+xml" });
    triggerDownload(URL.createObjectURL(blob), name + ".svg");
    return;
  }
  try {
    const { dataUrl } = await svgToPngDataUrl(xml, 2, _bg());   // 単体保存はテーマ背景
    triggerDownload(dataUrl, name + ".png");
  } catch (e) { toast("PNG化に失敗しました"); }
}

// Mermaid ソース → 生SVG文字列(sandbox iframe からも復元)
async function renderMermaidSvg(src) {
  const { svg } = await window.mermaid.render("pdf-" + Math.random().toString(36).slice(2, 9), src);
  const tmp = el("div"); tmp.innerHTML = svg;
  return _figSvgString(tmp);
}

// PDF用: 本文中の ```mermaid 図を順に PNG 化して [{data,w,h}|null] を返す(白背景・明テーマ)
async function collectMermaidImages(content) {
  if (!window.mermaid) return null;
  if (!_mermaidReady && !initMermaid()) return null;
  const re = /```[ \t]*mermaid[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```/gi;
  const blocks = []; let m;
  while ((m = re.exec(content)) !== null) blocks.push(m[1].trim());
  if (!blocks.length) return null;
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const out = [];
  try {
    if (dark) initMermaid(false);                 // 白背景の文書に合わせ明テーマで描画
    for (const src of blocks) {
      try {
        const xml = await renderMermaidSvg(src);
        if (!xml) { out.push(null); continue; }
        const r = await svgToPngDataUrl(xml, 2, "#ffffff");
        out.push({ data: r.dataUrl.split(",")[1], w: Math.round(r.w), h: Math.round(r.h) });
      } catch (_) { out.push(null); }
    }
  } finally {
    if (dark) initMermaid();                       // 元のテーマに戻す
  }
  return out;
}

function triggerDownload(href, name) {
  const a = el("a"); a.href = href; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 4000);
}

/* 本文中の `相対パス:行番号` をクリック可能なリンクにする(Codeモードのみ) */
const FILE_REF_EXTS = new Set(
  ("bas cls frm vb vba py js mjs cjs ts tsx jsx java kt c cc cpp cxx h hpp cs go rs rb php " +
   "swift scala lua r pl sql sh bash zsh bat cmd ps1 html htm css scss sass less json yaml yml " +
   "toml ini cfg conf xml md markdown txt text csv tsv log gradle properties dockerfile makefile " +
   "xlsx xlsm docx pptx pdf").split(" ")
);
function linkifyFileRefs(container) {
  if (State.mode !== "code" || !State.current) return;
  const re = /^([\w./-]+?\.([A-Za-z0-9]{1,6}))(?::(\d+))?$/;
  container.querySelectorAll("code:not(pre code)").forEach((code) => {
    if (code.dataset.fileref) return;
    const tok = (code.textContent || "").trim();
    const m = re.exec(tok);
    if (!m) return;
    if (tok.includes(" ") || tok.startsWith("/") || /^[A-Za-z]:[\\/]/.test(tok)) return; // 相対パスのみ
    if (!FILE_REF_EXTS.has(m[2].toLowerCase())) return;
    const a = el("a", "file-ref");
    a.textContent = tok;
    a.href = "#";
    a.title = "クリックして開く";
    a.dataset.fileref = "1";
    const path = m[1], line = m[3] ? parseInt(m[3], 10) : 0;
    a.onclick = (e) => { e.preventDefault(); openFileViewer(path, line); };
    code.replaceWith(a);
  });
}

async function openFileViewer(path, line) {
  if (!State.current) return;
  $("fv-title").textContent = path + (line ? ":" + line : "");
  const body = $("fv-body");
  body.innerHTML = '<div class="fv-note">読み込み中…</div>';
  $("file-modal").classList.remove("hidden");
  let data;
  try {
    data = await api(`/api/conversations/${State.current.id}/file?path=${encodeURIComponent(path)}`);
  } catch (e) {
    body.innerHTML = ""; body.appendChild(el("div", "fv-note", "開けませんでした: " + e.message));
    return;
  }
  if (data.binary || data.too_large || data.content == null) {
    body.innerHTML = ""; body.appendChild(el("div", "fv-note", data.note || "表示できません。"));
    return;
  }
  body.innerHTML = "";
  const wrap = el("div", "fv-wrap");
  data.content.replace(/\n$/, "").split("\n").forEach((ln, i) => {
    const n = i + 1;
    const row = el("div", "fv-row" + (n === line ? " target" : ""));
    row.appendChild(el("span", "fv-ln", String(n)));
    const c = el("span", "fv-lc"); c.textContent = ln.length ? ln : " ";
    row.appendChild(c);
    wrap.appendChild(row);
  });
  body.appendChild(wrap);
  if (line) {
    const t = wrap.querySelector(".fv-row.target");
    if (t) setTimeout(() => t.scrollIntoView({ block: "center" }), 30);
  }
}
function closeFileViewer() { $("file-modal").classList.add("hidden"); }

function scrollToBottom() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

