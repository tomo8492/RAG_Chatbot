/* ============================================================
   Code: コーディングエージェント
   ============================================================ */
async function sendCode() {
  if (State.streaming || !State.current) return;
  const s = State.current.settings || {};
  if (!s.workspace) { toast("先に「フォルダを選択」で作業フォルダを設定してください"); return; }
  const text = $("input").value.trim();
  const images = State.pendingImages.map((i) => i.dataUrl);   // スクショ等(Vision対応モデルで読む)
  if (!text && !images.length) return;
  $("input").value = ""; autoResize();
  State.pendingImages = []; renderAttachChips();

  const welcome = $("messages").querySelector(".welcome");
  if (welcome) welcome.remove();

  const urow = el("div", "msg-row user");
  const bubble = el("div", "bubble"); bubble.textContent = text || "(画像)";
  if (images.length) {
    const att = el("div", "attach-chips");
    images.forEach((src) => { const im = el("img", "msg-img"); im.src = src; att.appendChild(im); });
    bubble.appendChild(att);
  }
  urow.appendChild(bubble);
  $("messages").appendChild(urow);

  await streamAgent({ content: text, images });
  await loadConversations(); // タイトル更新を反映
}

/* ---- エージェントのステップ描画(ライブ/再表示で共用) ---- */
function buildAgentRow() {
  const row = el("div", "msg-row assistant");
  row.innerHTML = `<div class="avatar">${LOGO_SVG}</div>` +
    `<div class="msg-body"><div class="msg-name">Code エージェント</div><div class="agent-log"></div></div>`;
  return { row, logBox: row.querySelector(".agent-log") };
}
const SMALL_DIFF_LINES = 12;   // この変更行数以下の差分は折りたたまず開いておく
// 思考(thinking)の折りたたみボックス(チャット側の .thinking と同じ見た目)
function buildThinkBox() {
  const det = el("details", "thinking");
  det.innerHTML = '<summary>💭 考え中…</summary><div class="think-text"></div>';
  return { el: det, sum: det.querySelector("summary"), text: det.querySelector(".think-text") };
}
// unified diff から +追加 / −削除 の行数を数える
function diffStat(d) {
  let a = 0, r = 0;
  (d || "").split("\n").forEach((l) => {
    if (l.startsWith("+") && !l.startsWith("+++")) a++;
    else if (l.startsWith("-") && !l.startsWith("---")) r++;
  });
  return { a, r };
}
// Claude 風の折りたたみツールステップ。ヘッダ(ツール名・引数・状態)+ 折りたたみ本文。
// 返り値の fill(status,result,diff,opts) を tool_result 受信時に呼ぶ。
function buildToolStep(name, args) {
  const det = el("details", "step");
  const sum = document.createElement("summary");
  sum.className = "step-head";
  sum.appendChild(el("span", "step-name", escapeHtml(name)));
  sum.appendChild(el("span", "step-arg", escapeHtml(agentArgsSummary(name, args || {}))));
  const stat = el("span", "step-stat running", '<span class="spin"></span>');  // 実行中スピナー
  sum.appendChild(stat);
  det.appendChild(sum);
  const body = el("div", "step-body");
  det.appendChild(body);
  const ICON = { ok: "✓", error: "⚠", blocked: "⛔", rejected: "⊘", redirected: "↩" };
  function fill(status, result, diff, opts) {
    opts = opts || {};
    det._filled = true;
    const s = status || "ok";
    stat.className = "step-stat " + s;
    let label = ICON[s] || "✓";
    if (diff) { const { a, r } = diffStat(diff); if (a || r) label += `  +${a} −${r}`; }
    stat.textContent = label;
    if (result) {
      const rl = el("div", "step-result" + (s !== "ok" ? " " + s : ""));
      rl.textContent = trimResult(result);
      body.appendChild(rl);
    }
    let open = (s === "error" || s === "blocked" || s === "rejected");  // 問題は開いて見せる
    if (diff && !opts.skipDiffRender) {
      const { a, r } = diffStat(diff);
      if (a + r <= SMALL_DIFF_LINES) open = true;   // 小さな差分は畳まず開いておく
      const d = renderDiff(diff); d.classList.add("applied"); body.appendChild(d);
    }
    if (opts.undoId) body.appendChild(undoButtonEl(opts.undoId));   // 取り消しボタン
    det.open = open;
  }
  return { el: det, body, fill };
}
// 適用した変更を元に戻すボタン(ファイルを適用前に復元/新規は削除)
function undoButtonEl(undoId) {
  const btn = el("button", "undo-btn", "↶ 元に戻す");
  btn.title = "この変更を取り消す(ファイルを適用前に戻す)";
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await api("/api/code/undo", { method: "POST", body: JSON.stringify({ undo_id: undoId }) });
      if (r.ok) { btn.textContent = "↶ 取り消し済み"; btn.classList.add("done"); toast(r.message || "取り消しました"); }
      else { btn.disabled = false; toast(r.message || "取り消せませんでした"); }
    } catch (e) { btn.disabled = false; toast("取り消し失敗: " + e.message); }
  };
  return btn;
}
// tool_call を伴わない結果(エラー/最大ステップ/停止など)の素朴な通知ボックス
function plainNoticeEl(status, result, diff, undoId) {
  const wrap = el("div", "step-notice");
  const div = el("div", "step-result" + (status && status !== "ok" ? " " + status : ""));
  div.textContent = trimResult(result || "");
  wrap.appendChild(div);
  if (diff) { const d = renderDiff(diff); d.classList.add("applied"); wrap.appendChild(d); }
  if (undoId) wrap.appendChild(undoButtonEl(undoId));
  return wrap;
}
function agentTextEl(text) {
  const d = el("div", "md agent-text");
  renderMarkdown(d, text || "", true);
  return d;
}
function planStaticEl(plan) {
  const card = el("div", "confirm-card plan-card");
  card.appendChild(el("div", "confirm-title", "📋 実行計画(承認済み)"));
  const body = el("div", "plan-body md");
  renderMarkdown(body, plan || "", true);
  card.appendChild(body);
  return card;
}
// TODOパネル。existing を渡すと同じ要素を書き換える(進捗の更新)
function renderTodos(container, todos, existing) {
  const box = existing || el("div", "todo-panel");
  box.innerHTML = "";
  box.appendChild(el("div", "todo-title", "✅ タスク"));
  (todos || []).forEach((t) => {
    const st = t.status || "pending";
    const icon = st === "completed" ? "☑" : st === "in_progress" ? "▣" : "☐";
    box.appendChild(el("div", "todo-item " + st, `${icon} ${escapeHtml(t.content || "")}`));
  });
  if (!existing) container.appendChild(box);
  return box;
}
// 保存済みステップ(message.sources)を静的に再描画。tool_call と直後の
// tool_result を1つの折りたたみステップにまとめる(ライブ表示と同じ見た目)。
function renderCodeSteps(container, steps) {
  let todoEl = null, pend = null;   // pend: 結果待ちステップの fill 関数
  (steps || []).forEach((ev) => {
    switch (ev.type) {
      case "assistant": if (ev.text) container.appendChild(agentTextEl(ev.text)); pend = null; break;
      case "tool_call": { const s = buildToolStep(ev.name, ev.args); container.appendChild(s.el); pend = s.fill; break; }
      case "tool_result":
        if (pend) { pend(ev.status, ev.result, ev.diff, { undoId: ev.undo_id }); pend = null; }
        else container.appendChild(plainNoticeEl(ev.status, ev.result, ev.diff, ev.undo_id));
        break;
      case "plan": container.appendChild(planStaticEl(ev.plan)); pend = null; break;
      case "todos": todoEl = renderTodos(container, ev.todos, todoEl); break;
      case "ask": container.appendChild(askStaticEl(ev)); pend = null; break;
    }
  });
}

async function streamAgent(payload) {
  const { row, logBox } = buildAgentRow();
  const avatar = row.querySelector(".avatar");
  $("messages").appendChild(row);
  if (avatar) avatar.classList.add("thinking");   // 考え中/作業中:アイコンをアニメーション
  scrollToBottom();

  setStreaming(true);
  State.controller = new AbortController();
  let curText = null;   // 連続する assistant テキストの描画先
  let todoEl = null;    // TODOパネル(更新時は同じ要素を書き換え)
  let curFill = null, curBody = null, hasConfirm = false;  // 結果待ちのツールステップ
  let curThink = null;  // 思考の折りたたみボックス(本文/ツールが始まったら畳む)

  const collapseThink = () => {
    if (curThink) { curThink.el.open = false; curThink.sum.textContent = "💭 思考"; curThink = null; }
  };
  const addThink = (t) => {
    if (!curThink) {
      curThink = buildThinkBox(); curThink._raw = "";
      logBox.appendChild(curThink.el); curThink.el.open = true;
    }
    curThink._raw += t; curThink.text.textContent = curThink._raw; scrollToBottom();
  };
  const addStepCall = (name, args) => {
    curText = null; collapseThink();
    const s = buildToolStep(name, args);
    logBox.appendChild(s.el);
    curFill = s.fill; curBody = s.body; hasConfirm = false;
    scrollToBottom();
  };
  const addStepResult = (status, result, diff, undoId) => {
    curText = null; collapseThink();
    if (curFill) {
      // 確認カードが本文にある場合、差分はカード側に表示済みなので二重表示しない
      curFill(status, result, diff, { skipDiffRender: hasConfirm, undoId });
      curFill = null; curBody = null; hasConfirm = false;
    } else {
      logBox.appendChild(plainNoticeEl(status, result, diff, undoId));
    }
    scrollToBottom();
  };
  const addText = (t) => {
    collapseThink();
    if (!curText) { curText = el("div", "md agent-text"); curText._raw = ""; logBox.appendChild(curText); }
    curText._raw += t;
    renderMarkdown(curText, curText._raw, true);
    scrollToBottom();
  };

  let reqId = "";
  try {
    const res = await fetch(`/api/conversations/${State.current.id}/agent`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal: State.controller.signal,
    });
    reqId = res.headers.get("X-Request-ID") || "";
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {} throw new Error(d + reqSuffix(reqId)); }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = chunk.replace(/^data: /, "");
        if (!line) continue;
        let ev; try { ev = JSON.parse(line); } catch (_) { continue; }
        switch (ev.type) {
          case "thinking": if (ev.text) addThink(ev.text); break;
          case "assistant_delta": if (ev.text) addText(ev.text); break;
          case "assistant": if (ev.text) addText(ev.text); break;
          case "tool_call": addStepCall(ev.name, ev.args || {}); break;
          case "tool_result": addStepResult(ev.status, ev.result || "", ev.diff, ev.undo_id); break;
          case "confirm": {
            curText = null;
            const card = buildConfirmCard(ev);
            if (curBody) { curBody.appendChild(card); curBody.parentElement.open = true; hasConfirm = true; }
            else logBox.appendChild(card);
            scrollToBottom(); break;
          }
          case "plan": curText = null; curFill = null; curBody = null; collapseThink(); logBox.appendChild(buildPlanCard(ev)); scrollToBottom(); break;
          case "todos": curText = null; collapseThink(); todoEl = renderTodos(logBox, ev.todos || [], todoEl); scrollToBottom(); break;
          case "ask": curText = null; curFill = null; curBody = null; collapseThink(); logBox.appendChild(buildAskCard(ev)); scrollToBottom(); break;
          case "error": { const m = (ev.error || "エラー") + reqSuffix(reqId); addStepResult("rejected", "⚠ " + m); toast("エラー: " + m); break; }
          case "max_steps": addStepResult("blocked", "最大ステップ数に達しました。続けるには再度指示してください。"); break;
          case "done": case "user_saved": break;
        }
      }
    }
  } catch (e) {
    rejectOpenConfirms(logBox);   // 停止/切断時はサーバ側の承認待ちを解放する
    if (e.name === "AbortError") addStepResult("rejected", "停止しました");
    else { const m = e.message + (e.message.includes("(req:") ? "" : reqSuffix(reqId)); addStepResult("rejected", "⚠ " + m); toast("エラー: " + m); }
  } finally {
    finishConfirmCards(logBox);
    if (avatar) avatar.classList.remove("thinking");   // 完了/停止/エラーでアニメ停止
    setStreaming(false);
    State.controller = null;
  }
}

// 未応答のカードを解決してサーバ側の待機を解放(停止/切断時)
function rejectOpenConfirms(box) {
  box.querySelectorAll(".confirm-card:not([data-resolved])").forEach((card) => {
    const aid = card.dataset.actionId;
    if (!aid) return;
    if (card.dataset.kind === "ask") {
      api("/api/code/answer", { method: "POST", body: JSON.stringify({ action_id: aid, answer: "" }) }).catch(() => {});
    } else {
      api("/api/code/approve", { method: "POST", body: JSON.stringify({ action_id: aid, approved: false }) }).catch(() => {});
    }
  });
}

// ユーザーへの質問カード(選択肢 + 自由記述)
// 選択肢を {label, description, recommended} に正規化(文字列・オブジェクトの両対応)
function optOf(o) {
  if (o && typeof o === "object") {
    return { label: String(o.label || o.text || o.value || "").trim(),
             description: String(o.description || o.desc || "").trim(),
             recommended: !!(o.recommended || o.default) };
  }
  return { label: String(o == null ? "" : o).trim(), description: "", recommended: false };
}
// 選択肢ボタン(見出し + 説明 + 推奨バッジ)。クリックで label を回答として送る。
function askOptEl(o, onPick) {
  const node = el(onPick ? "button" : "div", "ask-opt" + (o.recommended ? " rec" : "") + (onPick ? "" : " static"));
  if (onPick) node.type = "button";
  const main = el("div", "ask-opt-main");
  main.appendChild(el("span", "ask-opt-label", escapeHtml(o.label)));
  if (o.recommended) main.appendChild(el("span", "ask-opt-rec", "推奨"));
  node.appendChild(main);
  if (o.description) node.appendChild(el("div", "ask-opt-desc", escapeHtml(o.description)));
  if (onPick) node.onclick = () => onPick(o.label);
  return node;
}
// ask イベント / 保存ステップを質問配列に正規化(新形式 questions・旧形式 question/options 両対応)
function normalizeQuestions(ev) {
  let qs = ev.questions;
  if (!Array.isArray(qs) || !qs.length) {
    qs = [{ header: ev.header, question: ev.question, multiSelect: ev.multiSelect, options: ev.options }];
  }
  return qs.map((q) => ({
    header: String((q && q.header) || "").trim(),
    question: String((q && (q.question || q.text)) || "").trim(),
    multiSelect: !!(q && (q.multiSelect || q.multi)),
    options: ((q && q.options) || []).map(optOf).filter((o) => o.label),
  })).filter((q) => q.question || q.options.length);
}
// 1問ぶんのブロック(複数質問・複数選択用)。getSelected() で選択ラベル配列を返す。
function buildQuestionBlock(q, qi) {
  const wrap = el("div", "ask-q");
  const head = el("div", "ask-q-head");
  if (q.header) head.appendChild(el("span", "ask-q-chip", escapeHtml(q.header)));
  head.appendChild(el("span", "ask-q-text", escapeHtml(q.question)));
  if (q.multiSelect) head.appendChild(el("span", "ask-q-multi", "複数選択可"));
  wrap.appendChild(head);
  const opts = el("div", "ask-options");
  const inputs = [];
  q.options.forEach((o) => {
    const lab = el("label", "ask-opt ask-check" + (o.recommended ? " rec" : ""));
    const inp = document.createElement("input");
    inp.className = "ask-cb";
    inp.type = q.multiSelect ? "checkbox" : "radio";
    inp.name = "askq-" + qi;
    inp.value = o.label;
    lab.appendChild(inp);
    const body = el("div", "ask-opt-body");
    const main = el("div", "ask-opt-main");
    main.appendChild(el("span", "ask-opt-label", escapeHtml(o.label)));
    if (o.recommended) main.appendChild(el("span", "ask-opt-rec", "推奨"));
    body.appendChild(main);
    if (o.description) body.appendChild(el("div", "ask-opt-desc", escapeHtml(o.description)));
    lab.appendChild(body);
    opts.appendChild(lab);
    inputs.push(inp);
  });
  wrap.appendChild(opts);
  const free = el("input", "ask-input ask-free-q"); free.type = "text"; free.placeholder = "その他(自由に入力)…";
  wrap.appendChild(free);
  const getSelected = () => {
    const sel = inputs.filter((i) => i.checked).map((i) => i.value);
    const f = free.value.trim();
    if (f) sel.push(f);
    return sel;
  };
  return { el: wrap, getSelected };
}
function summarizeAnswers(questions, answers) {
  return questions.map((q, i) =>
    (q.header || `Q${i + 1}`) + ": " + (answers[i] && answers[i].length ? answers[i].join(", ") : "(なし)")
  ).join(" / ");
}
// 複数質問は Claude と同様に「1問ずつ」のステッパーで提示する。
function buildAskStepper(card, actionId, questions) {
  const answers = questions.map(() => []);     // 質問ごとの選択ラベル配列
  const freeText = questions.map(() => "");     // 質問ごとの自由記述(行き来で保持)
  let step = 0;
  const prog = el("div", "ask-step-prog");
  const body = el("div", "ask-step-body");
  const nav = el("div", "ask-step-nav");
  card.appendChild(prog); card.appendChild(body); card.appendChild(nav);
  const isLast = () => step === questions.length - 1;
  const goNext = () => { if (isLast()) submitAnswers(card, actionId, answers, summarizeAnswers(questions, answers)); else { step++; render(); } };

  function render() {
    const q = questions[step];
    prog.textContent = `質問 ${step + 1} / ${questions.length}`;
    body.innerHTML = "";
    const head = el("div", "ask-q-head");
    if (q.header) head.appendChild(el("span", "ask-q-chip", escapeHtml(q.header)));
    head.appendChild(el("span", "ask-q-text", escapeHtml(q.question)));
    if (q.multiSelect) head.appendChild(el("span", "ask-q-multi", "複数選択可"));
    body.appendChild(head);

    const opts = el("div", "ask-options");
    const checks = [];
    q.options.forEach((o) => {
      if (q.multiSelect) {
        const lab = el("label", "ask-opt ask-check" + (o.recommended ? " rec" : ""));
        const inp = document.createElement("input"); inp.type = "checkbox"; inp.className = "ask-cb"; inp.value = o.label;
        if (answers[step].includes(o.label)) inp.checked = true;
        lab.appendChild(inp);
        const bd = el("div", "ask-opt-body"); const m = el("div", "ask-opt-main");
        m.appendChild(el("span", "ask-opt-label", escapeHtml(o.label)));
        if (o.recommended) m.appendChild(el("span", "ask-opt-rec", "推奨"));
        bd.appendChild(m); if (o.description) bd.appendChild(el("div", "ask-opt-desc", escapeHtml(o.description)));
        lab.appendChild(bd); opts.appendChild(lab); checks.push(inp);
      } else {
        const btn = askOptEl(o, (label) => { answers[step] = [label]; freeText[step] = ""; goNext(); });  // 単一選択はクリックで次へ
        if (answers[step][0] === o.label) btn.classList.add("picked");
        opts.appendChild(btn);
      }
    });
    body.appendChild(opts);

    const free = el("input", "ask-input ask-free-q"); free.type = "text"; free.placeholder = "その他(自由に入力)…";
    free.value = freeText[step] || "";
    body.appendChild(free);
    const gather = () => {
      const sel = q.multiSelect ? checks.filter((c) => c.checked).map((c) => c.value) : answers[step].slice();
      const f = free.value.trim();
      if (f && !sel.includes(f)) sel.push(f);
      return sel;
    };
    const commit = () => { answers[step] = gather(); freeText[step] = free.value.trim(); };
    free.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;  // 日本語IME変換中は送信しない
      e.preventDefault(); commit(); goNext();
    });

    nav.innerHTML = "";
    const back = el("button", "btn ask-back", "← 戻る"); back.type = "button"; back.disabled = step === 0;
    back.onclick = () => { commit(); if (step > 0) { step--; render(); } };
    const next = el("button", "btn primary ask-next", isLast() ? "回答を送信" : "次へ →"); next.type = "button";
    next.onclick = () => { commit(); goNext(); };
    nav.appendChild(back); nav.appendChild(next);
  }
  render();
}
function buildAskCard(ev) {
  const card = el("div", "confirm-card ask-card");
  card.dataset.actionId = ev.action_id;
  card.dataset.kind = "ask";
  if (ev.context) card.appendChild(el("div", "ask-context", escapeHtml(ev.context)));
  const questions = normalizeQuestions(ev);
  if (questions.length > 1) {
    // 複数質問 → 1セクションずつのステッパー
    card.appendChild(el("div", "confirm-title", "❓ いくつか確認させてください"));
    buildAskStepper(card, ev.action_id, questions);
  } else {
    const q = questions[0] || { question: "どう進めますか?", options: [], multiSelect: false };
    if (!q.multiSelect) {
      // 1問・単一選択 → クリックで即送信(軽快なパス)
      card.appendChild(el("div", "confirm-title", "❓ " + escapeHtml(q.header ? q.header + ": " + q.question : q.question)));
      if (q.options.length) {
        const opts = el("div", "ask-options");
        q.options.forEach((o) => opts.appendChild(askOptEl(o, (label) => submitAnswers(card, ev.action_id, [[label]], label))));
        card.appendChild(opts);
      }
      const row = el("div", "ask-free");
      const input = el("input", "ask-input"); input.type = "text"; input.placeholder = "その他(自由に入力)…";
      const send = el("button", "btn ask-send", "送信"); send.type = "button";
      const submitFree = () => { const v = input.value.trim(); if (v) submitAnswers(card, ev.action_id, [[v]], v); };
      send.onclick = submitFree;
      input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
        e.preventDefault(); submitFree();
      });
      row.appendChild(input); row.appendChild(send);
      card.appendChild(row);
    } else {
      // 1問・複数選択 → チェックボックス + 送信
      const block = buildQuestionBlock(q, 0);
      card.appendChild(block.el);
      const send = el("button", "btn primary ask-send-all", "回答を送信"); send.type = "button";
      send.onclick = () => {
        const a = block.getSelected();
        submitAnswers(card, ev.action_id, [a], (q.header || "回答") + ": " + (a.length ? a.join(", ") : "(なし)"));
      };
      card.appendChild(send);
    }
  }
  card.appendChild(el("div", "confirm-status ask-status"));
  return card;
}

async function submitAnswers(card, actionId, answers, displayText) {
  card.querySelectorAll("button, input").forEach((b) => (b.disabled = true));
  const status = card.querySelector(".ask-status");
  try {
    await api("/api/code/answer", { method: "POST", body: JSON.stringify({ action_id: actionId, answers }) });
    if (status) { status.textContent = "回答: " + (displayText || answers.flat().join(" / ") || "(なし)"); status.className = "confirm-status ask-status ok"; }
    card.dataset.resolved = "1";
  } catch (e) {
    if (status) { status.textContent = "送信失敗: " + e.message; status.className = "confirm-status ask-status no"; }
    card.querySelectorAll("button, input").forEach((b) => (b.disabled = false));
  }
}

// 保存済みステップ再表示用:質問(静的・複数質問対応)
function askStaticEl(ev) {
  const card = el("div", "confirm-card ask-card");
  if (ev.context) card.appendChild(el("div", "ask-context", escapeHtml(ev.context)));
  normalizeQuestions(ev).forEach((q) => {
    const wrap = el("div", "ask-q");
    const head = el("div", "ask-q-head");
    if (q.header) head.appendChild(el("span", "ask-q-chip", escapeHtml(q.header)));
    head.appendChild(el("span", "ask-q-text", escapeHtml(q.question)));
    if (q.multiSelect) head.appendChild(el("span", "ask-q-multi", "複数選択可"));
    wrap.appendChild(head);
    if (q.options.length) {
      const opts = el("div", "ask-options");
      q.options.forEach((o) => opts.appendChild(askOptEl(o, null)));
      wrap.appendChild(opts);
    }
    card.appendChild(wrap);
  });
  return card;
}

function agentArgsSummary(name, args) {
  if (name === "write_file") return `${args.path || ""} (${args.length || 0}字)`;
  if (name === "edit_file") return args.path || "";
  if (name === "run_command" || name === "run_background") return args.command || "";
  if (name === "command_output" || name === "stop_command") return "job " + (args.job_id || "");
  if (name === "read_file" || name === "summarize_path") return args.path || "";
  if (name === "glob" || name === "grep") return args.pattern || "";
  return "";
}

// 実行計画の承認カード(承認すると実行フェーズへ)
function buildPlanCard(ev) {
  const card = el("div", "confirm-card plan-card");
  card.dataset.actionId = ev.action_id;
  card.appendChild(el("div", "confirm-title", "📋 実行計画 — 承認すると実行フェーズに進みます"));
  const body = el("div", "plan-body md");
  renderMarkdown(body, ev.plan || "(計画なし)", true);
  card.appendChild(body);
  const actions = el("div", "confirm-actions");
  const ok = el("button", "btn primary", "承認して実行");
  const no = el("button", "btn", "却下");
  const status = el("span", "confirm-status");
  ok.onclick = () => respondConfirm(card, ev.action_id, true, status, [ok, no]);
  no.onclick = () => respondConfirm(card, ev.action_id, false, status, [ok, no]);
  actions.appendChild(ok); actions.appendChild(no); actions.appendChild(status);
  card.appendChild(actions);
  return card;
}
function trimResult(s) {
  s = String(s == null ? "" : s);
  return s.length > 4000 ? s.slice(0, 4000) + "\n…(省略)" : s;
}

function buildConfirmCard(ev) {
  const card = el("div", "confirm-card");
  card.dataset.actionId = ev.action_id;
  const isCmd = ev.name === "run_command" || ev.name === "run_background";
  const title = ev.name === "run_background" ? "⚠ このコマンドをバックグラウンド実行しますか?"
    : ev.name === "run_command" ? "⚠ このコマンドを実行しますか?"
    : "⚠ このファイル変更を適用しますか?";
  card.appendChild(el("div", "confirm-title", title));
  if (isCmd) {
    card.appendChild(el("div", "confirm-cmd", "$ " + escapeHtml(ev.command || "")));
  } else {
    card.appendChild(el("div", "confirm-meta",
      `${escapeHtml(ev.path || "")} ・ ${ev.exists ? "上書き" : "新規作成"}`));
    card.appendChild(renderDiff(ev.diff || ""));
  }
  const actions = el("div", "confirm-actions");
  const ok = el("button", "btn primary", isCmd ? "実行する" : "適用する");
  const no = el("button", "btn", "拒否");
  const status = el("span", "confirm-status");
  const btns = [ok, no];
  // ファイル編集のみ「以後自動適用」(acceptEdits)を提示。コマンドは安全のため毎回確認のまま。
  let always = null;
  if (!isCmd) {
    always = el("button", "btn", "以後自動適用");
    always.title = "このセッションは以後、ファイル編集を確認なしで適用(Claude Code の acceptEdits 相当)";
    btns.push(always);
  }
  ok.onclick = () => respondConfirm(card, ev.action_id, true, status, btns, "once");
  no.onclick = () => showRejectForm(card, ev.action_id, status, btns);   // 理由を書いて拒否
  if (always) always.onclick = () => respondConfirm(card, ev.action_id, true, status, btns, "always");
  actions.appendChild(ok);
  if (always) actions.appendChild(always);
  actions.appendChild(no);
  actions.appendChild(status);
  card.appendChild(actions);
  return card;
}

// Claude Code 風の差分表示。unified diff を行番号つき・行ごと色分けで描画する。
function renderDiff(diffText) {
  const wrap = el("div", "cc-diff");
  const body = el("div", "cc-diff-body");
  let oldLn = 0, newLn = 0, adds = 0, dels = 0, firstHunk = true, rows = 0;
  (diffText || "").split("\n").forEach((ln) => {
    if (ln.startsWith("+++") || ln.startsWith("---")) return;     // ファイルヘッダ行は隠す
    const m = ln.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/); // ハンクヘッダ → 行番号を復元
    if (m) {
      oldLn = parseInt(m[1], 10); newLn = parseInt(m[2], 10);
      if (!firstHunk) body.appendChild(el("div", "cc-diff-gap", "⋯"));
      firstHunk = false;
      return;
    }
    if (ln === "" ) return;
    let cls, gutter, mark, text;
    if (ln.startsWith("+")) { cls = "add"; gutter = newLn++; mark = "+"; text = ln.slice(1); adds++; }
    else if (ln.startsWith("-")) { cls = "del"; gutter = oldLn++; mark = "-"; text = ln.slice(1); dels++; }
    else { cls = "ctx"; gutter = newLn++; oldLn++; mark = " "; text = ln.startsWith(" ") ? ln.slice(1) : ln; }
    const row = el("div", "cc-diff-row " + cls);
    row.appendChild(el("span", "cc-ln", String(gutter)));
    row.appendChild(el("span", "cc-mark", mark === " " ? "&nbsp;" : mark));
    const code = el("span", "cc-code");
    code.textContent = text;                                       // textContent で自動エスケープ
    row.appendChild(code);
    body.appendChild(row);
    rows++;
  });
  const head = el("div", "cc-diff-head");
  head.appendChild(el("span", "cc-add-cnt", "+" + adds));
  head.appendChild(el("span", "cc-del-cnt", "−" + dels));
  wrap.appendChild(head);
  if (rows) wrap.appendChild(body);
  else wrap.appendChild(el("div", "cc-diff-empty", "(差分なし)"));
  return wrap;
}

async function respondConfirm(card, actionId, approved, statusEl, btns, scope, reason) {
  btns.forEach((b) => (b.disabled = true));
  try {
    await api("/api/code/approve", {
      method: "POST",
      body: JSON.stringify({ action_id: actionId, approved, scope: scope || null, reason: reason || null }),
    });
    statusEl.textContent = scope === "always" ? "承認しました(以後この会話の編集は自動適用)"
      : approved ? "承認しました"
      : (reason ? "拒否しました(理由を伝えました)" : "拒否しました");
    statusEl.className = "confirm-status " + (approved ? "ok" : "no");
    card.dataset.resolved = "1";
    if (scope === "always") setCodeAutoAccept(true);   // 設定に保存し、コードバーのトグルも更新
  } catch (e) {
    statusEl.textContent = "送信失敗: " + e.message;
    statusEl.className = "confirm-status no";
    btns.forEach((b) => (b.disabled = false));
  }
}

// 拒否時に「理由・どう直すか」を任意で添えて送る(Claude Code の "No, tell Claude what to do" 相当)
function showRejectForm(card, actionId, statusEl, mainBtns) {
  const actions = card.querySelector(".confirm-actions");
  if (!actions || card.querySelector(".reject-form")) return;
  mainBtns.forEach((b) => (b.style.display = "none"));
  const form = el("div", "reject-form");
  const input = el("input", "reject-reason");
  input.type = "text";
  input.placeholder = "拒否の理由・どう直してほしいか(任意)";
  const send = el("button", "btn primary", "拒否を送信");
  const back = el("button", "btn", "戻る");
  const submit = () =>
    respondConfirm(card, actionId, false, statusEl, [send, back, input], null, input.value.trim());
  send.onclick = submit;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); submit(); }  // IME変換中は送信しない
  });
  back.onclick = () => { form.remove(); mainBtns.forEach((b) => (b.style.display = "")); };
  form.appendChild(input); form.appendChild(send); form.appendChild(back);
  actions.appendChild(form);
  input.focus();
}

function finishConfirmCards(box) {
  box.querySelectorAll(".confirm-card:not([data-resolved])").forEach((card) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const st = card.querySelector(".confirm-status");
    if (st && !st.textContent) { st.textContent = "(終了)"; st.className = "confirm-status no"; }
  });
}

