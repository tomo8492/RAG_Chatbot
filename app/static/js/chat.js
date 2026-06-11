/* ============================================================
   送信 / ストリーミング
   ============================================================ */
async function send() {
  if (State.streaming || !State.current) return;
  const text = $("input").value.trim();
  if (!text && State.pendingImages.length === 0) return;
  $("input").value = "";
  autoResize();

  const attachments = State.pendingAttachments.slice();
  const images = State.pendingImages.map((i) => i.dataUrl);
  const imageThumbs = State.pendingImages.map((i) => i.dataUrl);
  State.pendingAttachments = [];
  State.pendingImages = [];
  renderAttachChips();

  // welcome 除去
  const welcome = $("messages").querySelector(".welcome");
  if (welcome) welcome.remove();

  // ユーザー行(楽観的)
  const urow = el("div", "msg-row user");
  const bubble = el("div", "bubble");
  bubble.textContent = text;
  if (attachments.length || imageThumbs.length) {
    const att = el("div", "attach-chips");
    imageThumbs.forEach((src) => { const im = el("img", "msg-img"); im.src = src; att.appendChild(im); });
    attachments.forEach((a) => att.appendChild(el("span", "chip", "📎 " + escapeHtml(a))));
    bubble.appendChild(att);
  }
  urow.appendChild(bubble);
  $("messages").appendChild(urow);

  await streamAssistant({ content: text, attachments, images, mode: "send" });
  await loadConversations(); // タイトル更新反映
}

async function regenerate() {
  if (State.streaming || !State.current) return;
  // 末尾の assistant 行を削除
  const rows = $("messages").querySelectorAll(".msg-row.assistant");
  if (rows.length) rows[rows.length - 1].remove();
  await streamAssistant({ mode: "regenerate" });
}

async function streamAssistant(payload) {
  const { row, refs } = createAssistantRow();
  $("messages").appendChild(row);
  refs.md.classList.add("cursor-blink");
  refs.avatar.classList.add("thinking");   // 考え中:回答アイコンをアニメーション
  scrollToBottom();

  setStreaming(true);
  State.controller = new AbortController();

  let acc = "", think = "", renderScheduled = false, gotContent = false, finished = false;
  let finalContent = null;   // done で届くサーバ後処理済み(スペル補正・フェンス補完済み)本文
  let clarified = false;     // 選択式の聞き返しカードを表示したか(本文描画と差し替える)
  const scheduleRender = () => {
    if (renderScheduled) return;
    renderScheduled = true;
    requestAnimationFrame(() => {
      renderScheduled = false;
      if (finished) return;
      renderMarkdown(refs.md, acc, false);
      scrollToBottom();
    });
  };

  let reqId = "";
  try {
    const res = await fetch(`/api/conversations/${State.current.id}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: State.controller.signal,
    });
    reqId = res.headers.get("X-Request-ID") || "";
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) {
      let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {}
      throw new Error(d + reqSuffix(reqId));
    }

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
        handleStreamEvent(ev, refs, {
          onThink: (d) => { think += d; refs.think.classList.remove("hidden");
            refs.think.open = false;   // 思考タブは既定で折りたたみ(クリックで展開)
            refs.thinkText.textContent = think; scrollToBottom(); },
          onContent: (d) => {
            if (!gotContent) {
              gotContent = true; refs.think.open = false;
              refs.avatar.classList.remove("thinking");   // 回答開始でアニメ停止
            }
            acc += d; scheduleRender();
          },
          onDone: (msg) => { if (msg && typeof msg.content === "string") finalContent = msg.content; },
          onClarify: (e) => {
            clarified = true; gotContent = true;
            refs.avatar.classList.remove("thinking");
            renderClarifyCard(refs.md, e);
            scrollToBottom();
          },
          getAcc: () => acc,
        });
      }
    }
    // 正常終了: サーバ後処理済み本文があればそれで最終描画(無ければ acc)
    // 聞き返しカード表示中は本文で上書きしない(保存はテキスト版が済んでいる)
    if (!clarified) {
      renderMarkdown(refs.md, finalContent != null ? finalContent : (acc || "*(応答なし)*"), true);
      applyInlineFigures(refs.md, refs.src);   // 本文の「図N」直下に画像を差し込む
    }
    if (finalContent != null) acc = finalContent;
  } catch (e) {
    if (e.name === "AbortError") {
      renderMarkdown(refs.md, acc || "*(停止しました)*", true);  // 部分内容は保持
    } else {
      const msg = e.message + (e.message.includes("(req:") ? "" : reqSuffix(reqId));
      renderMarkdown(refs.md, (acc ? acc + "\n\n" : "") + `⚠️ **エラー:** ${escapeHtml(msg)}`, true);
      toast("エラー: " + msg);
    }
  } finally {
    finished = true;
    refs.md.classList.remove("cursor-blink");
    refs.avatar.classList.remove("thinking");   // 完了/停止/エラーでアニメ停止
    refs.row.dataset.raw = stripThink(acc);
    buildAssistantActions(refs, true);
    setStreaming(false);
    State.controller = null;
  }
}

function handleStreamEvent(ev, refs, cb) {
  switch (ev.type) {
    case "thinking": cb.onThink(ev.delta); break;
    case "content": cb.onContent(ev.delta); break;
    case "sources": renderSources(refs.src, ev.sources, ev.note); break;
    case "clarify": if (cb.onClarify) cb.onClarify(ev); break;
    case "done":
      if (cb.onDone) cb.onDone(ev.message);
      if (ev.message && ev.message.sources && ev.message.sources.length
          && !(ev.message.sources[0] && ev.message.sources[0].clarify))   // 聞き返しの構造は出典ではない
        renderSources(refs.src, ev.message.sources);
      break;
    case "title":   // 初回のLLM自動タイトルを即時反映(サイドバーは送信完了時の loadConversations が更新)
      if (ev.title) {
        if (State.current) State.current.title = ev.title;
        if ($("chat-title")) $("chat-title").value = ev.title;
      }
      break;
    case "error": throw new Error(ev.error || "生成エラー");
    case "user_saved": break;
  }
}

function setStreaming(on) {
  State.streaming = on;
  $("send-btn").classList.toggle("hidden", on);
  $("stop-btn").classList.toggle("hidden", !on);
  const msgs = $("messages");
  if (msgs) msgs.setAttribute("aria-busy", on ? "true" : "false");
  srAnnounce(on ? "生成中です" : "生成が完了しました");
}
function stopGeneration() {
  if (State.controller) State.controller.abort();
}

/* 送信ボタン/Enter のモード振り分け */
function onSend() {
  if (State.mode === "code") sendCode();
  else send();
}

