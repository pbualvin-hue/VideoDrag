/* vidrag PWA — vanilla JS, no build step (CLAUDE.md rule 8).
 * Three tabs per UX.md: 對話 / 研究庫 / 管理. All maintenance is a button. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const main = $("#main");

// ---------- state ----------
const state = {
  tab: "chat",
  token: localStorage.getItem("vidrag_token") || "",
  sessionId: localStorage.getItem("vidrag_session") || null,
  adminSub: null,          // null=管理主頁 / "settings"=進階設定子頁
  scopeSourceId: null,     // null = 不限單一來源
  scopeCollectionId: null, // null = 不限資料庫(自訂資料庫,2026-07-16)
  scopeTitle: null,
  videos: [],
  pollTimer: null,
};

// ---------- api ----------
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers["X-App-Token"] = state.token;
  const res = await fetch("/api" + path, { ...opts, headers });
  if (res.status === 401) { askToken(); throw new Error("需要 APP_TOKEN"); }
  if (!res.ok) {
    let detail = "發生錯誤";
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// AI 主題短名優先;沒有(舊資料/生成失敗)退回原始標題
const dTitle = s => s.display_title || s.title || s.url_normalized || "";

// Minimal, safe Markdown → HTML for AI answers (rule 8: no deps; rule 12-1:
// transcript-derived content is untrusted, so escape first then apply a fixed
// whitelist of tags — never inject raw HTML from the model/source).
function fmtInline(s) {           // s is already HTML-escaped
  return s
    .replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function mdInline(raw) { return fmtInline(esc(raw)); }

function mdToHtml(src) {
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let inCode = false, list = null;
  const endList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      if (inCode) { out.push("</code></pre>"); inCode = false; }
      else { endList(); out.push("<pre><code>"); inCode = true; }
      continue;
    }
    if (inCode) { out.push(esc(line) + "\n"); continue; }
    let m;
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { endList(); out.push("<hr>"); continue; }
    if ((m = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
      endList(); out.push(`<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`); continue;
    }
    if ((m = line.match(/^\s*>\s?(.*)$/))) {
      endList(); out.push(`<blockquote>${mdInline(m[1])}</blockquote>`); continue;
    }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (list !== "ul") { endList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${mdInline(m[1])}</li>`); continue;
    }
    if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      if (list !== "ol") { endList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${mdInline(m[1])}</li>`); continue;
    }
    if (/^\s*$/.test(line)) { endList(); continue; }
    endList(); out.push(`<p>${mdInline(line)}</p>`);
  }
  if (inCode) out.push("</code></pre>");
  endList();
  return out.join("");
}

function modal(html) {
  const m = $("#modal");
  // Re-entrancy guard: a second modal() while one is open must not throw
  // (showModal on an open dialog = InvalidStateError, which strands the new
  // content with unbound handlers — review Critical 2026-07-14).
  if (m.open) m.close();
  m.innerHTML = html + '<div class="row" style="margin-top:12px;justify-content:flex-end">' +
    '<button class="btn ghost" onclick="document.getElementById(\'modal\').close()">關閉</button></div>';
  m.showModal();
  return m;
}

// ---------- auth / setup ----------
function askToken() {
  const m = modal(`
    <h2>輸入存取權杖</h2>
    <p class="muted">手機與電腦共用同一組 APP_TOKEN:在手機 PWA 的
      管理 → 進階設定 → iPhone 分享捷徑 可以看到;輸入一次即記住。</p>
    <input id="tok-in" placeholder="APP_TOKEN">
    <div class="row" style="margin-top:10px">
      <button class="btn" id="tok-save">儲存</button>
    </div>`);
  $("#tok-save", m).onclick = () => {
    // 寬容解析(回饋 2026-07-14:使用者把整條安裝網址當 token 貼入被鎖):
    // 去空白與零寬字元;貼的是含 token= 的網址就自動抽出參數值
    let v = $("#tok-in", m).value.replace(/[​﻿\s]/g, "");
    const tm = v.match(/[?&]token=([^&\s]+)/);
    // 殘缺的 % 序列會讓 decodeURIComponent 拋錯 → 退回原字串
    if (tm) { try { v = decodeURIComponent(tm[1]); } catch { v = tm[1]; } }
    // token_urlsafe 只含 [A-Za-z0-9_-]:去掉 iOS 複製常見的尾端標點
    v = v.replace(/[^A-Za-z0-9_-]+$/, "");
    state.token = v;
    localStorage.setItem("vidrag_token", state.token);
    m.close(); render();
  };
}

async function maybeRunWizard() {
  try {
    const s = await fetch("/api/setup/status").then(r => r.json());
    if (!s.needs_setup) return false;
    renderWizard(s);
    return true;
  } catch { return false; }
}

function renderWizard(s) {
  main.innerHTML = `
    <h2 class="page-title">👋 首次設定精靈</h2>
    <div class="card">
      <p>三步驟完成:填金鑰 → 設預算 → 產生手機捷徑。</p>
      <label class="muted">Groq API key(語音轉錄,<a href="https://console.groq.com" target="_blank">免費申請</a>)</label>
      <input id="wz-groq" placeholder="${s.has_groq_key ? "已設定,可留空" : "gsk_..."}">
      <label class="muted" style="margin-top:8px;display:block">Anthropic API key(問答,<a href="https://console.anthropic.com" target="_blank">申請</a>)</label>
      <input id="wz-ant" placeholder="${s.has_anthropic_key ? "已設定,可留空" : "sk-ant-..."}">
      <label class="muted" style="margin-top:8px;display:block">每月預算(美元)</label>
      <input id="wz-budget" type="number" value="${s.budget_usd}">
      <div class="row" style="margin-top:12px">
        <button class="btn" id="wz-go">完成設定</button>
      </div>
    </div>`;
  $("#wz-go").onclick = async () => {
    const body = { monthly_budget_usd: parseFloat($("#wz-budget").value) || 5 };
    if ($("#wz-groq").value.trim()) body.groq_api_key = $("#wz-groq").value.trim();
    if ($("#wz-ant").value.trim()) body.anthropic_api_key = $("#wz-ant").value.trim();
    const r = await fetch("/api/setup", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(r => r.json());
    state.token = r.app_token;
    localStorage.setItem("vidrag_token", state.token);
    toast("設定完成!到管理頁安裝 iOS 捷徑");
    state.tab = "admin"; render();
  };
}

// 桌面書籤/外部分享入口:/?share=<url> → 進研究庫自動填入並排入佇列
// (2026-07-14 使用者回饋 5:電腦看影片也要能一鍵入庫)
const _q = new URLSearchParams(location.search);
const _shareParam = _q.get("share");
const _textParam = _q.get("text");   // 書籤帶入的選取文字 → 走手動文字入庫
if (_shareParam || _textParam) {
  state.tab = "library";
  if (_shareParam) state.pendingShare = _shareParam;
  else state.pendingText = _textParam;
  history.replaceState(null, "", location.pathname);  // 不把網址留在網址列
}

// ---------- tab router ----------
$$("nav button").forEach(b => b.onclick = () => {
  state.tab = b.dataset.tab;
  state.adminSub = null;   // 點分頁一律回管理主頁
  $$("nav button").forEach(x => x.classList.toggle("active", x === b));
  render();
});

async function render() {
  clearInterval(state.pollTimer);
  $("#dock").innerHTML = "";   // chat rebuilds its input bar; other tabs none
  if (await maybeRunWizard()) return;
  if (state.tab === "chat") renderChat();
  else if (state.tab === "library") renderLibrary();
  else renderAdmin();
}

// ================= 對話 =================
async function renderChat() {
  main.innerHTML = `
    <div class="masthead"><span class="mt">對話</span></div>
    <div id="chat-log"></div>
    <div id="chat-empty"></div>`;
  // Input lives in the persistent bottom dock (in normal flow above the tab
  // bar) — position:fixed inside a scrolling page misbehaves on iOS.
  // Scope/sessions controls live here too (redesign: 常駐,不隨對話捲走)。
  $("#dock").innerHTML = `
    <div class="row dock-scope">
      <button class="scopebtn grow" id="c-scope" style="text-align:left"></button>
      <button class="scopebtn" id="c-sessions">☰ 紀錄</button>
      <button class="scopebtn" id="c-new">＋ 新對話</button>
    </div>
    <div class="row">
      <input id="c-q" class="grow" placeholder="問庫內內容…" autocomplete="off">
      <button class="btn" id="c-send">送出</button>
    </div>`;
  const sc = $("#c-scope");
  sc.textContent = (state.scopeSourceId || state.scopeCollectionId)
    ? `範圍:${state.scopeTitle?.slice(0, 12)}${(state.scopeTitle || "").length > 12 ? "…" : ""} ▾`
    : "範圍:全部內容 ▾";
  sc.classList.toggle("on", !!(state.scopeSourceId || state.scopeCollectionId));
  sc.onclick = pickScope;
  $("#c-new").onclick = () => { state.sessionId = null;
    localStorage.removeItem("vidrag_session"); renderChat(); };
  $("#c-sessions").onclick = showSessions;
  $("#c-send").onclick = sendQuestion;
  $("#c-q").addEventListener("keydown", e => { if (e.key === "Enter") sendQuestion(); });
  // keep the latest message visible when the iOS keyboard resizes the viewport
  $("#c-q").addEventListener("focus", () => setTimeout(scrollBottom, 300));
  await loadHistory();
  await renderEmptyState();
}

async function renderEmptyState() {
  if ($("#chat-log").children.length) { $("#chat-empty").innerHTML = ""; return; }
  let vids = [];
  try { vids = (await api("/videos")).sources.filter(s => s.status !== "failed"); } catch {}
  if (!vids.length) {
    $("#chat-empty").innerHTML = `<div class="card">
      <h3 style="font-size:16px">三步開始使用</h3>
      <ol><li>到「管理」分頁安裝 iOS 捷徑</li>
      <li>滑到影片 → 分享 → 點「存到 vidrag」</li>
      <li>回來這裡直接問影片內容</li></ol>
      <p class="muted">也可以在「研究庫」分頁按「＋ 新增」貼上連結。</p></div>`;
    return;
  }
  // AI 生成的開場問題(使用者回饋:要有思考,不要「講了什麼」模板);
  // 生不出來(無 key/失敗)就整塊不顯示
  let starters = [];
  try { starters = (await api("/chat/starters")).questions || []; } catch {}
  if (!starters.length) { $("#chat-empty").innerHTML = ""; return; }
  const chips = starters.map(q =>
    `<button data-q="${esc(q)}">${esc(q)}</button>`).join("");
  $("#chat-empty").innerHTML =
    `<p class="muted">可以這樣問:</p><div class="chips">${chips}</div>`;
  $$("#chat-empty .chips button").forEach(b => b.onclick = () => {
    $("#c-q").value = b.dataset.q; sendQuestion();
  });
}

async function loadHistory() {
  if (!state.sessionId) return;
  try {
    const d = await api(`/sessions/${state.sessionId}`);
    for (const m of d.messages)
      appendMsg(m.role, m.content, [], null, false, m.trace);
    scrollBottom();
  } catch { state.sessionId = null; }
}

// 「這則回答的依據」(gap-3):折疊區,列出送進 prompt 的每段 chunk 與其
// 兩路檢索命中(向量名次+距離 / 關鍵字名次),供使用者自查、開發歸因。
// title 源自不受信任內容 → 一律 esc()。點一列沿用引用卡的逐字稿上下文視窗。
function traceDetails(trace) {
  if (!trace || !trace.length) return null;
  const det = document.createElement("details");
  det.className = "trace";
  const sum = document.createElement("summary");
  sum.textContent = `這則回答的依據(檢索到 ${trace.length} 段)`;
  det.appendChild(sum);
  const list = document.createElement("div");
  list.className = "trace-list";
  for (const t of trace) {
    const row = document.createElement("div");
    row.className = "trace-row" + (t.cited ? " cited" : "");
    const paths = [];
    if (t.vec_rank != null) {
      paths.push(`向量#${t.vec_rank + 1}` +
        (t.vec_distance != null ? ` d=${t.vec_distance}` : ""));
    }
    if (t.fts_rank != null) paths.push(`關鍵字#${t.fts_rank + 1}`);
    row.innerHTML = `<span class="tn">S${t.label}</span>
      <span class="tt">${esc(t.title || "(未命名)")}</span>
      <span class="tp">${esc(paths.join(" · "))}</span>
      ${t.cited ? '<span class="tc">已引用</span>' : ""}`;
    row.onclick = () => showCiteContext(
      { source_id: t.source_id, chunk_id: t.chunk_id, start_sec: t.start_sec || 0 });
    list.appendChild(row);
  }
  det.appendChild(list);
  return det;
}

function appendMsg(role, text, citations = [], extra = null, scroll = true,
                   trace = null) {
  const div = document.createElement("div");
  div.className = `msg ${role === "user" ? "user" : "ai"}`;
  if (role === "user") {
    div.textContent = text;
  } else {
    const body = document.createElement("div");
    body.className = "md";
    body.innerHTML = mdToHtml(text);
    div.appendChild(body);
  }
  if (citations.length) {
    for (const c of citations) {
      // 點引用先展開「該段逐字稿上下文」,原片連結收進展開視窗
      // (redesign 2026-07-14;NotebookLM 深層連結模式)
      const a = document.createElement("a");
      a.className = "cite"; a.setAttribute("role", "button"); a.tabIndex = 0;
      // 引用卡＝縮圖＋標題＋時間戳＋發布日期(UX.md 分頁 1);
      // 手動文字沒有時間軸,且其日期是貼上入庫日而非發布日(rule 13 誠實標註)
      const cManual = c.platform === "manual";
      a.innerHTML = `<span class="n">S${c.label}</span>
        <img class="cite-thumb" alt="" loading="lazy"
          src="/api/videos/${c.source_id}/thumbnail?token=${encodeURIComponent(state.token)}">
        <span class="cite-body"><span class="t">${esc(c.display_title || c.title)}</span><br>
        <span class="cm">${cManual ? "📝" : `⏱ ${esc(c.timestamp)}・`}<span class="d">${cManual ? "貼上" : "發布"} ${esc((c.published_at || "未知").slice(0, 10))}</span></span></span>`;
      // onerror 用程式繫結,不走 inline JS-in-attribute(audit 縱深:
      // 字串內插進事件屬性會被 HTML 實體先解碼,esc() 擋不住該情境)
      const cimg = a.querySelector("img.cite-thumb");
      cimg.onerror = () => thumbFallback(cimg, c.platform, "cite-thumb thumb-fallback");
      a.onclick = () => showCiteContext(c);
      a.onkeydown = e => { if (e.key === "Enter") showCiteContext(c); };
      div.appendChild(a);
    }
  }
  const td = traceDetails(trace);
  if (td) div.appendChild(td);
  if (extra) div.appendChild(extra);
  $("#chat-log").appendChild(div);
  if (scroll) scrollBottom();
  return div;
}

function scrollBottom() { main.scrollTop = main.scrollHeight; }

// 點引用 → 展開該段逐字稿前後文,再給「開原片」(不直接跳外部)
async function showCiteContext(c) {
  let d;
  try { d = await api(`/videos/${c.source_id}`); }
  catch (e) { toast("讀取來源失敗:" + e.message); return; }
  const s = d.source;
  const manual = s.platform === "manual";
  const idx = d.chunks.findIndex(x => x.chunk_id === c.chunk_id);
  const win = idx >= 0 ? d.chunks.slice(Math.max(0, idx - 1), idx + 2)
                       : d.chunks.slice(0, 2);
  // 誠實標註(rule 13 精神):找不到被引用的原段落時明說,不假裝命中
  const missNote = idx < 0
    ? '<p class="muted">找不到被引用的原段落(可能已重新分析),以下顯示開頭作參考。</p>' : "";
  const rows = win.map(x => `<div class="ctx${x.chunk_id === c.chunk_id ? " hit" : ""}">
      ${manual ? "" : `<span class="mono muted">[${fmtTs(x.start_sec)}]</span> `}${esc(x.text)}</div>`).join("");
  const canOpen = /^https?:\/\//i.test(s.url_normalized || "");
  const openHref = canOpen ? s.url_normalized +
    (s.platform === "youtube" ? `&t=${Math.floor(c.start_sec || 0)}s` : "") : "";
  const m = modal(`<h2>${esc(dTitle(s))}</h2>
    <p class="muted mono" style="margin-top:-6px">${manual ? "手動貼上" : esc(s.platform)}・${manual ? "入庫" : "發布"} ${esc((s.published_at || "未知").slice(0, 10))}</p>
    ${missNote}${rows}
    <div class="row" style="margin-top:12px">
      ${canOpen ? `<a class="btn" style="text-decoration:none" target="_blank" href="${esc(openHref)}">開原片 ↗</a>` : ""}
      <button class="btn ghost" id="ctx-full">完整逐字稿</button>
    </div>`);
  $("#ctx-full", m).onclick = () => { m.close(); showDetail(s.source_id); };
}

async function sendQuestion() {
  const q = $("#c-q").value.trim();
  if (!q) return;
  $("#c-q").value = "";
  $("#chat-empty").innerHTML = "";
  appendMsg("user", q);
  const sk = appendMsg("ai", "檢索中…");
  sk.classList.add("skeleton");
  try {
    const d = await api("/chat", { method: "POST", body: JSON.stringify({
      question: q, session_id: state.sessionId, source_id: state.scopeSourceId,
      collection_id: state.scopeCollectionId }) });
    state.sessionId = d.session_id;
    localStorage.setItem("vidrag_session", d.session_id);
    sk.remove();
    const vbtn = document.createElement("button");
    vbtn.className = "btn ghost v-btn";
    vbtn.textContent = "🔍 查證(連網比對,另計費)";
    vbtn.onclick = () => runVerify(q, d.answer, vbtn);
    const node = appendMsg("ai", d.answer, d.citations, vbtn, true, d.trace);
    // 追問建議 chips(來自模型、間接源自不受信任內容 → 一律純文字渲染)
    if (d.suggestions?.length) {
      const sd = document.createElement("div");
      sd.className = "chips sugg";
      for (const sug of d.suggestions) {
        const b = document.createElement("button");
        b.textContent = sug;
        b.onclick = () => { $("#c-q").value = sug; sendQuestion(); };
        sd.appendChild(b);
      }
      node.appendChild(sd);
    }
    if (d.budget_warning) {
      const w = document.createElement("div");
      w.className = "badge warn"; w.style.marginTop = "8px";
      w.textContent = d.budget_warning;
      node.appendChild(w);
    }
  } catch (e) { sk.textContent = "❌ " + e.message; sk.classList.remove("skeleton"); }
}

async function runVerify(question, answer, btn) {
  btn.disabled = true; btn.textContent = "查證中…";
  try {
    const d = await api("/chat", { method: "POST", body: JSON.stringify({
      question, session_id: state.sessionId, source_id: state.scopeSourceId,
      collection_id: state.scopeCollectionId, verify: true }) });
    const box = document.createElement("div");
    box.className = "card p2";
    box.textContent = d.verification || "(無查證結果)";
    btn.parentElement.appendChild(box);
    btn.remove();
  } catch (e) { btn.textContent = "查證失敗:" + e.message; btn.disabled = false; }
}

async function pickScope() {
  const [d, cd] = await Promise.all([api("/videos"), api("/collections")]);
  // 細列表而非大按鈕(回饋 2026-07-14);三層範圍:全部/資料庫/單一來源
  const colRows = cd.collections.map(c => `<div class="pick-row" role="button" tabindex="0"
      data-col="${c.collection_id}" data-t="${esc(c.name)}">
      <span class="pt">📁 ${esc(c.name)}<span class="muted">(${c.source_count})</span></span></div>`)
    .join("");
  const items = d.sources.filter(s => ["ready", "enriched"].includes(s.status))
    .map(s => `<div class="pick-row" role="button" tabindex="0"
      data-id="${s.source_id}" data-t="${esc(dTitle(s))}">
      ${pIcon(s.platform, 14)}<span class="pt">${esc(dTitle(s) || s.video_id)}</span></div>`)
    .join("");
  const m = modal(`<h2>對話範圍</h2>
    <div class="pick-row on" id="sc-all" role="button" tabindex="0">
      <span class="pt">全部內容</span></div>
    ${colRows ? `<h2 class="feed-h" style="margin:14px 4px 2px">資料庫</h2>${colRows}` : ""}
    <h2 class="feed-h" style="margin:14px 4px 2px">單一來源</h2>${items}`);
  // div 列取代原生 button:補 Enter/Space 啟動維持鍵盤可用(ARIA 慣例)
  const act = (el, fn) => { el.onclick = fn;
    el.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); }
    }; };
  const setScope = (src, col, title) => {
    state.scopeSourceId = src; state.scopeCollectionId = col;
    state.scopeTitle = title; m.close(); renderChat();
  };
  act($("#sc-all", m), () => setScope(null, null, null));
  $$("[data-col]", m).forEach(b => act(b, () =>
    setScope(null, +b.dataset.col, b.dataset.t)));
  $$("[data-id]", m).forEach(b => act(b, () =>
    setScope(+b.dataset.id, null, b.dataset.t)));
}

async function showSessions() {
  const d = await api("/sessions");
  const items = d.sessions.map(s =>
    `<div class="pick-row" role="button" tabindex="0" data-id="${esc(s.session_id)}">
      <span class="pt">${esc(s.title)}<br><span class="muted">${esc(s.last_at || "")}(${s.message_count} 則)</span></span>
      <button class="row-del" data-del-sess="${esc(s.session_id)}" title="刪除這段對話"
        aria-label="刪除這段對話">✕</button>
    </div>`).join("") || '<p class="muted">尚無歷史對話</p>';
  const m = modal(`<h2>歷史對話</h2>${items}`);
  $$("[data-id]", m).forEach(b => {
    const fn = () => {
      state.sessionId = b.dataset.id;
      localStorage.setItem("vidrag_session", state.sessionId);
      m.close(); renderChat();
    };
    b.onclick = e => { if (e.target.closest(".row-del")) return; fn(); };
    b.onkeydown = e => {
      if (e.target.closest(".row-del")) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); }
    };
  });
  // 刪除小叉叉(回饋 2026-07-15):二次確認 → 刪除 → 就地更新清單
  $$("[data-del-sess]", m).forEach(x => x.onclick = async e => {
    e.stopPropagation();
    const sid = x.dataset.delSess;
    if (!confirm("確定刪除這段對話紀錄?此動作無法復原。")) return;
    try { await api(`/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" }); }
    catch (err) { toast("刪除失敗:" + err.message); return; }
    const wasActive = state.sessionId === sid;
    if (wasActive) {   // 刪的是進行中的對話 → 回到新對話狀態
      state.sessionId = null;
      localStorage.removeItem("vidrag_session");
    }
    x.closest(".pick-row").remove();
    toast("已刪除對話");
    if (!m.querySelector(".pick-row")) m.close();
    // 進行中對話被刪就重繪聊天面板,否則背後殘留已刪內容,下一個
    // 問題會黏在殘影下面(review W;modal 若還開著會蓋在上層,不受影響)
    if (wasActive) renderChat();
  });
}

// ================= 研究庫(週分組時間軸,redesign 2026-07-14)=================
const LIB_FILTERS = [["all", "全部"], ["video", "影片"], ["article", "文章"], ["text", "文字"], ["image", "圖片"]];

async function renderLibrary() {
  // 方案 A(2026-07-16 使用者拍板):首屏只留一列工具列,
  // 貼上框進「＋新增」視窗、搜尋點 🔎 展開、資料庫+型態合併進「篩選」抽屜
  main.innerHTML = `
    <div class="masthead"><span class="mt">研究庫</span>
      <span class="md-r" id="l-count"></span></div>
    <div class="row tools">
      <button class="btn" id="l-add-btn">＋ 新增</button>
      <button class="btn ghost" id="l-search-btn" aria-label="搜尋庫存">🔎</button>
      <button class="btn ghost" id="l-filter-btn">篩選 ▾</button>
      <button class="fchip hidden" id="l-active-chip"></button>
    </div>
    <div class="row hidden" id="l-searchrow">
      <input id="l-search" class="grow" placeholder="關鍵字直查庫存(不經 AI)">
      <button class="btn ghost" id="l-search-close">取消</button>
    </div>
    <div id="l-hits"></div>
    <div id="l-cards"><p class="muted">載入中…</p></div>`;
  $("#l-add-btn").onclick = () => openAddModal();
  $("#l-search-btn").onclick = () => {
    $("#l-searchrow").classList.toggle("hidden");
    if (!$("#l-searchrow").classList.contains("hidden")) $("#l-search").focus();
  };
  $("#l-search-close").onclick = () => {
    $("#l-searchrow").classList.add("hidden");
    $("#l-search").value = ""; $("#l-hits").innerHTML = "";
  };
  $("#l-filter-btn").onclick = openFilterSheet;
  $("#l-active-chip").onclick = () => {   // 一鍵清除套用中的篩選
    state.libCollection = null; state.libFilter = "all";
    state.cardsSig = null; updateActiveChip(); refreshCards();
  };
  let debounce;
  $("#l-search").addEventListener("input", e => {
    clearTimeout(debounce);
    debounce = setTimeout(() => quickSearch(e.target.value.trim()), 300);
  });
  updateActiveChip();
  // 刪除資料庫後殘留的篩選要重置(review W1),用 chips 時代的守護邏輯
  api("/collections").then(cd => {
    state.collections = cd.collections;
    const cur = state.libCollection;
    if (cur != null && cur !== "none" &&
        !cd.collections.some(c => String(c.collection_id) === String(cur))) {
      state.libCollection = null; state.cardsSig = null;
      updateActiveChip(); refreshCards();
    } else updateActiveChip();
  }).catch(() => {});
  // 桌面書籤帶入的分享連結:開新增視窗並「確認一次」再送出(security
  // audit 2026-07-14:防誘導點擊 ?share= 靜默污染)。無 token 先收 token。
  if (state.pendingShare) {
    if (state.token) {
      const url = state.pendingShare;
      state.pendingShare = null;
      openAddModal(url);
      if (confirm(`要把這條連結存進 vidrag 嗎?\n${url}`))
        submitUrls().then(() => {   // 成功=框已清空 → 收掉視窗(review W2)
          const mm = $("#modal");
          if (mm.open && $("#l-paste") && !$("#l-paste").value) mm.close();
        }).catch(e => toast("入庫失敗:" + e.message));
      // 取消:連結留在新增視窗,可自行編輯或關閉
    } else {
      askToken();
      return;
    }
  }
  // 書籤/擴充帶入的選取文字:開新增視窗並帶入,由文字入庫視窗的「入庫」
  // 鈕作為確認(不靜默送出)。無 token 先收 token。
  if (state.pendingText) {
    if (state.token) {
      const t = state.pendingText;
      state.pendingText = null;
      openAddModal(t);
    } else {
      askToken();
      return;
    }
  }
  state.cardsSig = null;  // fresh tab entry always paints
  await refreshCards();
  state.pollTimer = setInterval(refreshCards, 5000);
}

// 週分組標籤:本週/上週/YYYY年M月(依入庫時間——研究誌記「何時收的」)
function weekLabel(iso) {
  if (!iso) return "更早";
  const d = new Date(iso);
  if (isNaN(d)) return "更早";
  const now = new Date();
  const start = new Date(now);
  start.setDate(now.getDate() - ((now.getDay() + 6) % 7));  // 本週一
  start.setHours(0, 0, 0, 0);
  if (d >= start) return "本週";
  const prev = new Date(start); prev.setDate(start.getDate() - 7);
  if (d >= prev) return "上週";
  return `${d.getFullYear()}年${d.getMonth() + 1}月`;
}

// 「＋新增」視窗:原本常駐的貼上大框搬進來(方案 A)
function openAddModal(prefill = "") {
  const m = modal(`<h2>＋ 新增內容</h2>
    <textarea id="l-paste" placeholder="＋貼上連結(可多行批次)或整段文字(DM、Threads 貼文)"></textarea>
    <div class="row" style="margin-top:10px"><button class="btn" id="l-add">加入佇列</button>
      <button class="btn ghost" id="l-podcast">🎙 Podcast RSS</button></div>`);
  $("#l-podcast", m).onclick = openPodcastModal;
  $("#l-paste", m).value = prefill;
  $("#l-paste", m).focus();
  $("#l-add", m).onclick = async () => {
    if (!$("#l-paste", m).value.trim()) { toast("先貼上連結或文字"); return; }
    await submitUrls().catch(e => toast("入庫失敗:" + e.message));
    // 連結流成功會清空框;文字流會換成自己的視窗——只在還停在
    // 本視窗且框已清空時關閉
    const lp = $("#l-paste");
    if (m.open && lp && !lp.value) m.close();
  };
  return m;
}

// 「篩選 ▾」抽屜:資料庫+型態合併一處(方案 A)
async function openFilterSheet() {
  let cd;
  try { cd = await api("/collections"); }
  catch (e) { toast("載入失敗:" + e.message); return; }
  state.collections = cd.collections;
  // 篩選中的資料庫已被刪除 → 重置。次要守護:主要重置在 renderLibrary
  // 載入時已做,此處只防「開著研究庫期間」的極端時序,不需各自演化(S3)
  if (state.libCollection != null && state.libCollection !== "none" &&
      !cd.collections.some(c => String(c.collection_id) === String(state.libCollection))) {
    state.libCollection = null;
  }
  const m = modal(`<h2>篩選</h2>
    <h2 class="feed-h" style="margin:10px 4px 4px">資料庫</h2>
    <div class="filters" id="fs-col"></div>
    <h2 class="feed-h" style="margin:14px 4px 4px">型態</h2>
    <div class="filters" id="fs-type"></div>`);
  const paint = () => {
    const cur = state.libCollection ?? "all";
    const colChip = (key, label) =>
      `<button data-c="${key}" class="${String(cur) === String(key) ? "on" : ""}">${label}</button>`;
    $("#fs-col", m).innerHTML = colChip("all", "全部")
      + colChip("none", `未分類(${cd.unfiled_count})`)
      + cd.collections.map(c =>
          colChip(c.collection_id, `📁 ${esc(c.name)}(${c.source_count})`)).join("");
    $("#fs-type", m).innerHTML = LIB_FILTERS.map(([k, label]) =>
      `<button data-f="${k}" class="${(state.libFilter || "all") === k ? "on" : ""}">${label}</button>`).join("");
    $$("#fs-col button", m).forEach(b => b.onclick = () => {
      state.libCollection = b.dataset.c === "all" ? null : b.dataset.c;
      state.cardsSig = null; refreshCards(); updateActiveChip(); paint();
    });
    $$("#fs-type button", m).forEach(b => b.onclick = () => {
      state.libFilter = b.dataset.f;
      state.cardsSig = null; refreshCards(); updateActiveChip(); paint();
    });
  };
  paint();
}

// 工具列上的「套用中篩選」chip:非預設時顯示,點擊清除
function updateActiveChip() {
  const chip = $("#l-active-chip");
  if (!chip) return;
  const parts = [];
  if (state.libCollection === "none") parts.push("未分類");
  else if (state.libCollection != null) {
    const c = (state.collections || []).find(
      x => String(x.collection_id) === String(state.libCollection));
    parts.push(c ? `📁 ${c.name}` : "📁");
  }
  const typeLabel = (LIB_FILTERS.find(([k]) => k === state.libFilter) || [])[1];
  if (state.libFilter && state.libFilter !== "all") parts.push(typeLabel);
  if (!parts.length) { chip.classList.add("hidden"); return; }
  chip.classList.remove("hidden");
  chip.textContent = parts.join("・") + " ✕";
}

// Podcast RSS 單集入庫(rule 1 podcast, 2026-07-17):貼 feed → 選集 → 排入
function openPodcastModal() {
  const m = modal(`<h2>🎙 Podcast RSS</h2>
    <p class="muted">貼上節目的 RSS 網址,載入後挑選要入庫的集數
      (轉錄走 Groq,一集約數分鐘;一次最多 10 集)。</p>
    <div class="row"><input id="pc-url" class="grow" placeholder="https://…/feed.xml">
      <button class="btn" id="pc-load">載入集數</button></div>
    <div id="pc-list" style="margin-top:10px"></div>
    <div class="row hidden" style="margin-top:10px" id="pc-actions">
      <button class="btn" id="pc-go">入庫選取的集數</button></div>`);
  let feed = null;
  const picked = new Set();
  $("#pc-load", m).onclick = async ev => {
    const url = $("#pc-url", m).value.trim();
    if (!url) return toast("先貼上 RSS 網址");
    ev.target.disabled = true; ev.target.textContent = "載入中…";
    try { feed = await api("/podcast/feed", { method: "POST",
      body: JSON.stringify({ url }) }); }
    catch (e) { toast("載入失敗:" + e.message, 5000);
      ev.target.disabled = false; ev.target.textContent = "載入集數"; return; }
    ev.target.disabled = false; ev.target.textContent = "重新載入";
    picked.clear();
    $("#pc-list", m).innerHTML = `<p class="muted">${esc(feed.feed_title)}・
        ${feed.episodes.length} 集(點選要入庫的)</p>` +
      feed.episodes.map((ep, i) => `<div class="pick-row${ep.in_library ? "" : ""}"
          role="button" tabindex="0" data-ep="${i}" ${ep.in_library ? 'style="opacity:.45"' : ""}>
        <span class="pt">${esc(ep.title)}<br><span class="muted mono">
          ${esc((ep.published_at || "").slice(0, 10) || "日期未知")}
          ${ep.duration_secs ? "・" + fmtTs(ep.duration_secs) : ""}
          ${ep.in_library ? "・已在庫" : ""}</span></span>
        <span class="pc-check" style="margin-left:auto"></span></div>`).join("");
    $("#pc-actions", m).classList.remove("hidden");
    $$("[data-ep]", m).forEach(row => {
      const fn = () => {
        const i = +row.dataset.ep;
        if (feed.episodes[i].in_library) { toast("這集已在庫"); return; }
        if (picked.has(i)) { picked.delete(i); row.querySelector(".pc-check").textContent = ""; }
        else if (picked.size >= 10) { toast("一次最多 10 集"); return; }
        else { picked.add(i); row.querySelector(".pc-check").textContent = "✓"; }
        $("#pc-go", m).textContent = picked.size
          ? `入庫選取的 ${picked.size} 集` : "入庫選取的集數";
      };
      row.onclick = fn;
      row.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); } };
    });
  };
  $("#pc-go", m).onclick = async () => {
    if (!picked.size) return toast("先點選集數");
    try {
      await api("/podcast/ingest", { method: "POST", body: JSON.stringify({
        feed_title: feed.feed_title,
        episodes: [...picked].map(i => feed.episodes[i]),
      }) });
    } catch (e) { toast("排入失敗:" + e.message, 5000); return; }
    toast(`已排入 ${picked.size} 集,轉錄需數分鐘`);
    m.close(); refreshCards();
  };
}

async function submitUrls() {
  const raw = $("#l-paste").value.trim();
  if (!raw) return;
  const lines = raw.split("\n").map(s => s.trim()).filter(Boolean);
  // 規則判斷(rule 1 手動文字,2026-07-13 拍板):每一行都是 URL → 連結入庫;
  // 否則整段視為純文字內文(DM、Threads 貼文),彈出選填標題後入庫。
  if (lines.every(s => /^https?:\/\/\S+$/i.test(s))) return submitLinkUrls(lines);
  const previewLines = raw.split("\n").map(l => l.trim()).filter(Boolean).slice(0, 3);
  const m = modal(`<h2>以文字入庫</h2>
    <p class="muted">貼上的內容不是連結,會直接存成一筆文字來源(${raw.length} 字)。
      入庫日期=今天。</p>
    <div class="tpreview">${previewLines.map(l => esc(l)).join("<br>")}</div>
    <input id="t-title" placeholder="標題(選填,留空由 AI 命名)" maxlength="200">
    <div class="row" style="margin-top:10px"><button class="btn" id="t-go">入庫</button></div>`);
  $("#t-go", m).onclick = async ev => {
    ev.target.disabled = true;
    let d;
    try {
      d = await api("/ingest", { method: "POST", body: JSON.stringify(
        { text: raw, title: $("#t-title", m).value.trim() || null }) });
    } catch (e) { toast("入庫失敗:" + e.message); ev.target.disabled = false; return; }
    toast(d.results[0].status === "duplicate" ? "這段文字已在庫" : "已排入文字入庫");
    const lp = $("#l-paste");
    if (lp) lp.value = "";
    m.close(); refreshCards();
  };
}

async function submitLinkUrls(urls) {
  const d = await api("/ingest", { method: "POST", body: JSON.stringify({ urls }) });
  const queued = d.results.filter(r => r.status === "queued").length;
  const dup = d.results.filter(r => r.status === "duplicate").length;
  const rej = d.results.filter(r => r.status === "rejected");
  toast(`已排入 ${queued} 支${dup ? `,${dup} 支已在庫` : ""}${rej.length ? `,${rej.length} 支被拒` : ""}`);
  if (rej.length) modal(`<h2>未接受的連結</h2>` +
    rej.map(r => `<p class="muted">${esc(r.url)}<br>→ ${esc(r.reason)}</p>`).join(""));
  const lp = $("#l-paste");
  if (lp) lp.value = "";
  refreshCards();
}

async function quickSearch(q) {
  const hitsBox = $("#l-hits");
  if (!hitsBox) return;   // debounce 期間切了分頁(stale-tab)
  if (!q) { hitsBox.innerHTML = ""; return; }
  const d = await api(`/search?q=${encodeURIComponent(q)}`);
  // snippet is transcript-derived (untrusted, rule 12-1) — escape before
  // injecting; highlight markers are 【】 so escaping loses nothing.
  hitsBox.innerHTML = d.hits.length
    ? d.hits.map(h => `<div class="card p2">
        <div class="muted">${esc(h.display_title || h.title)}・${fmtTs(h.start_sec)}</div>${esc(h.snippet)}</div>`).join("")
    : '<p class="muted">庫內找不到這個關鍵字</p>';
}

function fmtTs(s) {
  s = Math.floor(s || 0);
  return s >= 3600 ? `${(s / 3600) | 0}:${String((s % 3600 / 60) | 0).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${(s / 60) | 0}:${String(s % 60).padStart(2, "0")}`;
}


// 單色線條平台圖示(回饋 2026-07-14:要圖示但不要 emoji)。
// 固定字串常數、非使用者資料,可安全 innerHTML。
const PLATFORM_SVG = {
  youtube: '<rect x="2.5" y="5.5" width="19" height="13" rx="3.5"/><path d="M10.2 9.3l4.8 2.7-4.8 2.7z" fill="currentColor" stroke="none"/>',
  instagram: '<rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><circle cx="12" cy="12" r="3.8"/><circle cx="16.8" cy="7.2" r=".9" fill="currentColor" stroke="none"/>',
  tiktok: '<path d="M14.5 4v9.3a3.9 3.9 0 1 1-3.2-3.84"/><path d="M14.5 4.6c.6 2.2 2.1 3.6 4.2 3.9"/>',
  web: '<circle cx="12" cy="12" r="8.5"/><ellipse cx="12" cy="12" rx="3.8" ry="8.5"/><path d="M3.8 12h16.4"/>',
  manual: '<path d="M5 5.5h14M5 9.5h14M5 13.5h14M5 17.5h8"/>',
  podcast: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6"/>',
};
function pIcon(platform, size = 16) {
  const body = PLATFORM_SVG[platform] ||
    '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v5M12 16.2v.1"/>';
  return `<svg class="picon" width="${size}" height="${size}" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}
// 縮圖載入失敗 → 圖示磁磚(inline onerror 太長,抽成全域 helper)
window.thumbFallback = (img, platform, cls) => {
  const div = document.createElement("div");
  div.className = cls;
  div.innerHTML = pIcon(platform, 20);
  img.replaceWith(div);
};

async function refreshCards() {
  let d;
  try { d = await api("/videos"); } catch { return; }
  state.videos = d.sources;
  // The 5s poll must not rebuild the DOM needlessly: it resets an open
  // swipe mid-gesture and re-fetches every thumbnail. Skip when data is
  // unchanged; defer (retry next tick) while a card is swiped open.
  const sig = JSON.stringify([d.queued, d.sources, state.libFilter, state.libCollection]);
  if (sig === state.cardsSig) return;
  if (document.querySelector('.vcard[data-swiped="1"]')) return;
  state.cardsSig = sig;
  // Stale-tab guard: the 5s poll's await may resolve after the user has
  // switched tabs and render() replaced <main> — every DOM write below
  // must bail out, not just the counter (review W1).
  const cardsBox = $("#l-cards");
  if (!cardsBox) return;
  if ($("#l-count")) $("#l-count").textContent = `${d.sources.length} SOURCES`;
  // 處理中/排隊 永遠置頂,不進週分組
  const qhtml = d.queued.map(q => `<div class="card"><div class="row">
      <span class="badge info"><span class="spin"></span>${q.status === "processing" ? "處理中" : `排隊中,前面還有 ${q.queue_position ?? "?"} 支`}</span>
      <span class="muted grow" style="overflow:hidden;text-overflow:ellipsis">${esc(q.url || q.preview || "")}</span>
    </div></div>`).join("");
  const filter = state.libFilter || "all";
  const colSel = state.libCollection;   // null=全部 / "none"=未分類 / id 字串
  const shown = d.sources.filter(s =>
    (filter === "all" || s.type === filter) &&
    (colSel == null || (colSel === "none" ? s.collection_id == null
                                          : String(s.collection_id) === String(colSel))));
  let lastWeek = null;
  const cards = shown.map(s => {
    const badge = s.status === "enriched" ? '<span class="badge ok">已摘要</span>'
      : s.status === "ready" ? '<span class="badge ok">可提問</span>'
      : s.status === "processing" ? '<span class="badge info"><span class="spin"></span>處理中…</span>'
      : '<span class="badge err">失敗</span>';
    const err = s.human_error ? `<div class="fixbar card p2" style="border:1px solid var(--err)">
        ❗ ${esc(s.human_error.message)}<br>
        <span class="muted">→ ${esc(s.human_error.action)}</span>
        <div class="row" style="margin-top:6px">${fixButton(s)}</div></div>` : "";
    // 無縮圖時退回線條圖示磁磚(回饋 2026-07-14:每個來源都要有圖示);
    // onerror 於 innerHTML 寫入後程式繫結(見下),不走 inline 屬性
    const thumb = `<img class="thumb" data-platform="${esc(s.platform)}"
      src="/api/videos/${s.source_id}/thumbnail?token=${encodeURIComponent(state.token)}"
      alt="" loading="lazy">`;
    // 側欄:日期(手動文字=入庫日)+平台代號+時長,Tufte 邊註式;
    // UTC 存、本地顯示(rule 5)——published_at 多為純日期,直接切字串
    const dt = (s.platform === "manual" ? s.ingested_at : s.published_at || s.ingested_at) || "";
    const ld = dt.length > 10 ? new Date(dt) : null;  // 含時間 → 轉本地
    const dateTxt = ld && !isNaN(ld)
      ? `${String(ld.getMonth() + 1).padStart(2, "0")}/${String(ld.getDate()).padStart(2, "0")}`
      : esc(dt.slice(5, 10).replace("-", "/"));
    const dur = s.duration_secs ? `<br>${fmtTs(s.duration_secs)}` : "";
    // 側欄:圖示取代文字代號(回饋 2026-07-14)
    const side = `<div class="side"><span class="k">${dateTxt}</span>
      <br>${pIcon(s.platform, 14)}${dur}</div>`;
    // 週分組標頭(依入庫時間)
    const wk = weekLabel(s.ingested_at);
    const header = wk !== lastWeek ? `<h2 class="feed-h">${esc(wk)}</h2>` : "";
    lastWeek = wk;
    return `${header}<div class="vwrap">
      <div class="vcard" data-id="${s.source_id}">
      ${side}
      <div class="meta">
        <div class="title">${esc(dTitle(s))}</div>
        ${s.summary ? `<div class="summary">${esc(s.summary)}</div>` : ""}
        <div class="thumbrow">${thumb}${badge}</div>
        ${err}
      </div></div>
      <button class="swipe-del" data-del="${s.source_id}" data-title="${esc(dTitle(s))}">刪除</button>
    </div>`;
  }).join("") || `<p class="muted" style="margin-top:14px">${filter === "all"
    ? "庫內還沒有內容——貼上連結、整段文字,或用手機分享。"
    : "這個分類還沒有內容。"}</p>`;
  cardsBox.innerHTML = qhtml + cards;
  // error 事件為非同步(網路),同步迴圈綁定不會漏接
  $$("#l-cards img.thumb").forEach(img => {
    img.onerror = () => thumbFallback(img, img.dataset.platform, "thumb-fallback");
  });
  $$("#l-cards .vcard").forEach(c => {
    c.onclick = e => {
      if (e.target.closest("button")) return;
      if (c.dataset.swiped === "1") { resetSwipe(c); return; }
      showDetail(+c.dataset.id);
    };
    bindSwipe(c);
  });
  $$("#l-cards .swipe-del").forEach(b => b.onclick = async () => {
    // 左滑刪除,二次確認(UX.md 分頁 2):滑開是第一步,這裡再 confirm
    const card = b.previousElementSibling;
    if (!confirm(`確定刪除「${b.dataset.title || "這筆"}」?其逐字稿與向量會一併清除。`)) {
      resetSwipe(card); return;
    }
    // Clear the swiped flag first — a ghost data-swiped on the removed card
    // would trip the poll guard in refreshCards and freeze the list (review 1).
    resetSwipe(card);
    try {
      await api(`/videos/${b.dataset.del}`, { method: "DELETE" });
      toast("已刪除");
    } catch (e) { toast("刪除失敗:" + e.message, 5000); }
    refreshCards();
  });
  bindFixButtons();
}

// Swipe-left on a library card reveals its delete button (touch devices).
// Only horizontal-dominant moves are captured, so vertical scroll stays free.
function bindSwipe(card) {
  let x0 = 0, y0 = 0, dx = 0, active = false;
  card.addEventListener("touchstart", e => {
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    dx = 0; active = true;
    card.style.transition = "none";
  }, { passive: true });
  card.addEventListener("touchmove", e => {
    if (!active) return;
    const mx = e.touches[0].clientX - x0, my = e.touches[0].clientY - y0;
    if (Math.abs(my) > Math.abs(mx)) { active = false; card.style.transform = ""; return; }
    dx = Math.max(-88, Math.min(0, mx));
    card.style.transform = `translateX(${dx}px)`;
  }, { passive: true });
  card.addEventListener("touchend", () => {
    if (!active) return;
    active = false;
    card.style.transition = "transform .18s";
    if (dx < -50) { card.style.transform = "translateX(-88px)"; card.dataset.swiped = "1"; }
    else resetSwipe(card);
  });
  // iOS fires touchcancel (not touchend) when a system gesture interrupts;
  // without this the card sticks mid-swipe (review 2).
  card.addEventListener("touchcancel", () => { active = false; resetSwipe(card); });
}

function resetSwipe(card) {
  card.style.transition = "transform .18s";
  card.style.transform = "";
  delete card.dataset.swiped;
}

function fixButton(s) {
  const k = s.human_error.action_kind;
  // 手動文字來源沒有 URL 可重抓,重試類修復一律退為移除(原文重貼即可)
  if (s.platform === "manual" && (k === "retry" || k === "update_ytdlp"))
    return `<button class="btn danger" data-fix="remove" data-id="${s.source_id}">移除這筆(重貼原文即可重試)</button>`;
  if (k === "retry") return `<button class="btn" data-fix="retry" data-id="${s.source_id}" data-url="${esc(s.url_normalized)}">重試</button>`;
  if (k === "remove") return `<button class="btn danger" data-fix="remove" data-id="${s.source_id}">移除這筆</button>`;
  if (k === "update_ytdlp") return `<button class="btn" data-fix="ytdlp" data-id="${s.source_id}" data-url="${esc(s.url_normalized)}">更新 yt-dlp 後重試</button>`;
  if (k === "billing") return `<a class="btn" href="https://console.anthropic.com" target="_blank">前往儲值</a>`;
  if (k === "cookie") return `<button class="btn" data-fix="admin">→ 管理頁</button>`;
  if (k === "paste") return `<button class="btn" data-fix="paste">改貼內文</button>
    <button class="btn ghost" data-fix="retry" data-id="${s.source_id}" data-url="${esc(s.url_normalized)}">重試</button>
    <button class="btn danger" data-fix="remove" data-id="${s.source_id}">移除這筆</button>`;
  return "";
}

function bindFixButtons() {
  $$("[data-fix]").forEach(b => b.onclick = async () => {
    const kind = b.dataset.fix;
    if (kind === "remove") {
      if (!confirm("確定移除這筆失敗紀錄?")) return;
      await api(`/videos/${b.dataset.id}`, { method: "DELETE" });
      toast("已移除"); refreshCards();
    } else if (kind === "retry" || kind === "ytdlp") {
      if (kind === "ytdlp") { toast("更新 yt-dlp 中…", 8000);
        await api("/admin/update/ytdlp", { method: "POST" }); }
      await api(`/videos/${b.dataset.id}`, { method: "DELETE" });
      await api("/ingest", { method: "POST",
        body: JSON.stringify({ url: b.dataset.url }) });
      toast("已重新排入佇列"); refreshCards();
    } else if (kind === "paste") {
      // 動態載入頁抓不到正文 → 直接開「＋新增」視窗引導手動文字入庫
      // (review W1:貼上框已收進視窗,舊的捲動+聚焦引導失效)
      openAddModal();
      toast("開啟該頁面全選複製,貼進這個視窗即可以文字入庫", 4000);
    } else if (kind === "admin") {
      state.tab = "admin"; state.adminSub = "settings"; render();
      $$("nav button").forEach(x => x.classList.toggle("active", x.dataset.tab === "admin"));
    }
  });
}

// 把來源移到某個資料庫(或移回未分類);完成後重開詳情
async function showCollectionAssign(sourceId) {
  let cd;
  try { cd = await api("/collections"); } catch (e) { toast("載入失敗:" + e.message); return; }
  const rows = [`<div class="pick-row" role="button" tabindex="0" data-assign="null">
      <span class="pt">未分類</span></div>`]
    .concat(cd.collections.map(c => `<div class="pick-row" role="button" tabindex="0"
      data-assign="${c.collection_id}"><span class="pt">📁 ${esc(c.name)}</span></div>`))
    .join("");
  const m = modal(`<h2>移至資料庫</h2>${rows}
    <p class="muted" style="margin-top:10px">要新增資料庫:管理 → 資料庫分類。</p>`);
  $$("[data-assign]", m).forEach(b => {
    const fn = async () => {
      const cid = b.dataset.assign === "null" ? null : +b.dataset.assign;
      try {
        await api(`/videos/${sourceId}/collection`, { method: "PATCH",
          body: JSON.stringify({ collection_id: cid }) });
      } catch (e) { toast("移動失敗:" + e.message); return; }
      toast(cid === null ? "已移回未分類" : "已移入資料庫");
      state.cardsSig = null;
      m.close(); showDetail(sourceId);
    };
    b.onclick = fn;
    b.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); } };
  });
}

async function showDetail(id) {
  const d = await api(`/videos/${id}`);
  const s = d.source;
  // 手動文字來源(platform=manual)沒有原始連結:不掛外連、不給畫面分析,
  // 「發布日期」實為貼上入庫日,標示清楚(rule 13)
  const manual = s.platform === "manual";
  // Image/carousel posts (rule 1: type=image) have no timeline — render their
  // caption + vision chunks as plain text, like manual, and hide the
  // video-only 「重新分析畫面」 (reanalyze downloads a video that isn't there).
  const isImage = s.type === "image";
  const noTs = manual || isImage;
  const chunks = d.chunks.map(c => noTs
    ? `<p>${esc(c.text)}</p>`
    : `<p><a class="t" href="${esc(s.url_normalized)}${s.platform === "youtube" ? `&t=${Math.floor(c.start_sec || 0)}s` : ""}"
       target="_blank" style="color:var(--accent)">[${fmtTs(c.start_sec)}]</a> ${esc(c.text)}</p>`).join("");
  const m = modal(`<h2>${esc(dTitle(s))}</h2>
    <p class="muted">${manual ? "📝 手動貼上" : esc(s.platform)}・${manual ? "入庫" : "發布"} ${esc((s.published_at || "未知").slice(0, 10))}${manual ? "" : `・
      <a href="${esc(s.url_normalized)}" target="_blank" style="color:var(--accent)">開原片</a>`}</p>
    ${s.display_title && s.title ? `<p class="muted" style="margin-top:-6px">資訊源:
      ${manual ? esc(s.title) : `<a href="${esc(s.url_normalized)}" target="_blank" style="color:var(--muted)">${esc(s.title)}</a>`}</p>` : ""}
    ${s.summary ? `<div class="card p2">${esc(s.summary)}</div>` : '<p class="muted">摘要生成中…點開詳情已自動排程。</p>'}
    <div class="row">
      <button class="btn" id="dt-ask">針對這支影片提問</button>
      <button class="btn ghost" id="dt-rename">✏️ 改名</button>
      <button class="btn ghost" id="dt-col">📁 資料庫 ▾</button>
      ${noTs ? "" : '<button class="btn ghost" id="dt-reanalyze" title="Phase 4 提供">重新分析畫面</button>'}
      <button class="btn ghost" id="dt-export">匯出 Markdown</button>
      <button class="btn danger" id="dt-del">刪除</button>
    </div>
    <h2>${manual ? "內文" : isImage ? "貼文內容" : "逐字稿"}</h2>${chunks}`);
  $("#dt-rename", m).onclick = async () => {
    // 只改 AI 顯示名;原始標題(資訊源)不動(2026-07-17 批次)
    const name = prompt("新的顯示名稱(40 字內):", dTitle(s));
    if (!name || !name.trim() || name.trim() === dTitle(s)) return;
    try {
      await api(`/videos/${s.source_id}/title`, { method: "PATCH",
        body: JSON.stringify({ display_title: name.trim() }) });
    } catch (e) { toast("改名失敗:" + e.message); return; }
    toast("已改名");
    state.cardsSig = null;
    m.close(); showDetail(s.source_id);
  };
  $("#dt-col", m).onclick = () => showCollectionAssign(s.source_id);
  $("#dt-ask", m).onclick = () => { state.scopeSourceId = s.source_id;
    state.scopeTitle = dTitle(s); state.tab = "chat"; m.close();
    $$("nav button").forEach(x => x.classList.toggle("active", x.dataset.tab === "chat"));
    render(); };
  if ($("#dt-reanalyze", m)) $("#dt-reanalyze", m).onclick = async (ev) => {
    ev.target.disabled = true; ev.target.textContent = "分析畫面中…";
    try {
      const r = await api(`/videos/${s.source_id}/reanalyze`, { method: "POST" });
      toast(`已加入 ${r.visual_chunks_added} 段畫面內容`);
      m.close(); showDetail(s.source_id);
    } catch (e) { toast("分析失敗:" + e.message); ev.target.disabled = false;
      ev.target.textContent = "重新分析畫面"; }
  };
  $("#dt-export", m).onclick = () => downloadText(
    `/admin/export?source_id=${s.source_id}`, `vidrag-${s.source_id}.md`);
  $("#dt-del", m).onclick = async () => {
    if (!confirm(`確定刪除「${dTitle(s)}」?其逐字稿與向量會一併清除。`)) return;
    await api(`/videos/${s.source_id}`, { method: "DELETE" });
    m.close(); toast("已刪除"); refreshCards();
  };
}

async function downloadText(path, filename) {
  const text = await api(path);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  a.download = filename; a.click();
}

// ================= 管理 =================
// 使用手冊(靜態內容,非技術使用者視角;PWA 內建=零指令原則 rule 15)
const MANUAL_HTML = `
  <div class="card">
    <h3>🚀 快速上手(三步)</h3>
    <p>1. 在 IG/YouTube/TikTok 滑到想留下的影片 → 分享 → 點「<b>存到 vidrag</b>」捷徑,
      看到「已加入處理」就可以關掉繼續滑。<b>長按選取的文字</b>也能用同一個捷徑分享
      (捷徑需照最新安裝資訊設定,接收類型含「文字」)。</p>
    <p>2. 系統在背景抓取、轉錄、入庫,約 1–4 分鐘;研究庫的卡片會從「處理中」變「可提問」,
      並自動取好簡短主題名。</p>
    <p>3. 之後任何時間打開這裡的「對話」分頁直接問,回答會附出處卡
      (標題+時間戳+發布日期),點出處卡可跳回原片該時間點。</p>
    <p class="muted">網頁文章也可以:研究庫按「＋ 新增」貼上連結,一次貼多行=批次。
      沒有連結的內容(創作者 DM、Threads 貼文)把整段文字貼進同一個視窗,
      系統會認出它不是連結,確認後存成文字來源。</p>
  </div>
  <div class="card">
    <h3>💬 對話怎麼用</h3>
    <p>・輸入框上方「範圍:全部內容 ▾」可切換成只問某一筆來源。</p>
    <p>・「☰ 紀錄」看歷史對話,點任一則可續聊;「＋ 新對話」開新話題。</p>
    <p>・每則回答旁的「🔍 查證」會連網交叉比對關鍵主張(另計少量費用,標記
      外部支持/矛盾/查無);平常不會自動連網。</p>
    <p>・回答只根據庫內內容;庫裡沒有的,它會直說沒有,不會編。</p>
  </div>
  <div class="card">
    <h3>🎬 研究庫怎麼用</h3>
    <p>・工具列:「＋ 新增」貼連結或文字(視窗內另有「🎙 Podcast RSS」:
      貼節目 RSS 選集數入庫)、「🔎」直查逐字稿原文(即時、不經 AI,
      適合「找那段在哪」)、「篩選 ▾」按資料庫與型態過濾;
      要「理解與統整」用對話分頁。</p>
    <p>・卡片顯示 AI 取的主題名;點進詳情可看原始標題(資訊源)、三行摘要與完整逐字稿。</p>
    <p>・詳情頁:「針對這支影片提問」「重新分析畫面」(內容在畫面不在旁白時用)、
      「匯出 Markdown」「刪除」。</p>
    <p>・卡片<b>左滑</b>出現紅色刪除鈕;失敗的卡片會寫人話原因並附修復按鈕,照按即可。</p>
    <p>・<b>📁 資料庫分類</b>:管理→資料庫分類 建立自己的分類
      (如「食譜」「AI」);來源詳情頁按「📁 資料庫」歸檔,研究庫上方與
      對話「範圍」都能只看某個資料庫。</p>
    <p>・<b>📝 手動文字</b>:「＋ 新增」視窗也吃整段純文字(DM、Threads 等)——
      貼進去會跳出「以文字入庫」,標題可自己填或留給 AI 命名;
      這類來源顯示的日期是<b>入庫日</b>,不是原發布日。相同內容重貼會自動認出、不重複入庫。</p>
  </div>
  <div class="card">
    <h3>📝 盤點研究庫與精煉筆記</h3>
    <p>要整理庫存時,打開 Claude Code 說「<b>盤點研究庫</b>」:AI 會掃描庫內
      內容、跟你逐項討論利弊,當場決定——值得留的洞見寫進精煉筆記庫、
      可做的專案記到專案備選資料夾、規則想法送進全域設定備選區、
      沒價值的直接刪掉(刪除前一定先問你)。盤點完研究庫就是乾淨的。</p>
    <p>已收錄的筆記在上方「精煉筆記」可瀏覽;在 Claude Code 問
      「我筆記庫裡關於 ○○ 的結論?」即可檢索。</p>
  </div>
  <div class="card">
    <h3>🖥 電腦上使用</h3>
    <p>・<b>直接開網站</b>:這個網址在電腦瀏覽器一樣能用,輸入同一組
      APP_TOKEN(進階設定→iPhone 分享捷徑可查)即可。</p>
    <p>・<b>Chrome 擴充(推薦)</b>:進階設定→「Chrome 擴充」下載並照教學載入一次;
      之後在任何頁面<b>點工具列圖示</b>存整頁,或<b>選一段文字/對連結按右鍵</b>「存到 vidrag」——
      跟手機捷徑一樣好用。</p>
    <p>・<b>電腦書籤(替代)</b>:不想裝擴充或用別的瀏覽器時,進階設定→「電腦書籤」照教學
      加到書籤列;點書籤:有選字存文字、沒選字存整頁網址。</p>
    <p>・<b>Claude Desktop / Claude Code 查庫</b>:進階設定→「Claude Desktop(MCP)」
      產生連接設定照教學貼一次。之後在桌面的 Claude 直接問庫內內容,
      走訂閱額度、不另計 API 費;流量僅限你自己的 Tailscale 內網。</p>
  </div>
  <div class="card">
    <h3>🔧 日常維運(都是按鈕,不用指令)</h3>
    <p>・<b>備份</b>:每天自動一份、保留 7 份,並加密上傳 NAS;「立即備份」隨時可按,
      「還原」有二次確認且還原前會自動再備份現況。</p>
    <p>・<b>IG cookie 過期</b>:IG 影片開始失敗時,重新匯出 cookies.txt 上傳即可
      (詳情卡片的修復按鈕會帶你來)。</p>
    <p>・<b>yt-dlp 更新</b>:平台改版導致抓取失敗時按「一鍵更新」,更新中暫停攝取。</p>
    <p>・<b>重置 token</b>:懷疑外洩時用;重置後手機捷徑與其他裝置要重新設定。</p>
  </div>
  <div class="card">
    <h3>🩺 疑難排解</h3>
    <p>・<b>剛重開機打不開</b>:等 30–40 秒再重整,系統在暖機(載入模型)。</p>
    <p>・<b>文章顯示「動態載入/需要登入,抓不到正文」</b>:Notion 公開頁、表單頁
      這類內容是瀏覽器現場組出來的。兩條路:①管理→進階設定 開啟
      「JS 頁面雲端渲染」後回卡片按「重試」(網址會送第三方渲染);
      ②按「改貼內文」自己複製貼上。開啟①後,<b>Threads 公開貼文連結
      也能直接分享入庫</b>(以文字形式)。</p>
    <p>・<b>影片失敗</b>:卡片上有原因與修復按鈕(重試/更新 yt-dlp/上傳 cookie/移除),
      照按即可,沒有死路。</p>
    <p>・<b>費用</b>:上方進度條看本月累計;超過預算會在回答尾端提醒,但不會擋你使用。</p>
    <p>・<b>系統健康嗎?</b>:看最上面三顆燈(磁碟/佇列/備份),全綠=正常。</p>
  </div>`;

async function renderAdmin() {
  // 管理拆分(2026-07-16 使用者拍板):主頁只留常用;
  // 一次性/維修項目移到「進階設定」子頁(renderSettings)
  if (state.adminSub === "settings") return renderSettings();
  main.innerHTML = '<p class="muted">載入中…</p>';
  let h, st;
  try { [h, st] = await Promise.all([api("/admin/health"), api("/stats")]); }
  catch (e) { main.innerHTML = `<div class="card">❌ ${esc(e.message)}</div>`; return; }
  const diskLight = h.disk.used_pct > 90 ? "r" : h.disk.used_pct > 75 ? "y" : "g";
  const queueLight = h.queue_length > 10 ? "y" : "g";
  const backupAge = h.last_backup_at
    ? (Date.now() - Date.parse(h.last_backup_at)) / 3600e3 : Infinity;
  const backupLight = backupAge < 26 ? "g" : backupAge < 50 ? "y" : "r";
  const pct = Math.min(100, 100 * st.month_cost_usd / (st.budget_usd || 1));
  const backupLabel = h.last_backup_at
    ? (backupAge < 1 ? "1 小時內" : `${Math.round(backupAge)} 小時前`) : "尚未備份";
  main.innerHTML = `
    <h2>系統狀態</h2>
    <div class="card">
      <div class="statgrid">
        <div class="stat"><span class="k"><span class="light ${diskLight}"></span>磁碟</span>
          <span class="v">剩 ${h.disk.free_gb} GB</span></div>
        <div class="stat"><span class="k"><span class="light ${queueLight}"></span>佇列</span>
          <span class="v">${h.queue_length} 待處理</span></div>
        <div class="stat"><span class="k"><span class="light ${backupLight}"></span>備份</span>
          <span class="v">${esc(backupLabel)}</span></div>
      </div>
      ${h.failed_sources ? `<p style="margin:8px 0 0"><span class="badge err">${h.failed_sources} 筆失敗來源</span><span class="muted"> — 到研究庫查看原因</span></p>` : ""}
      ${h.integrity_ok ? "" : '<p style="margin:8px 0 0"><span class="badge err">備份完整性檢查異常!</span></p>'}
      <p class="muted" style="margin:8px 0 0">庫內 ${h.source_count} 個來源・vidrag v${h.version}</p>
    </div>
    <h2>本月費用</h2>
    <div class="card">
      <progress max="100" value="${pct}"></progress>
      <p style="margin:6px 0">US$${st.month_cost_usd} / US$${st.budget_usd}
        ${st.over_budget ? '<span class="badge warn">已超過預算</span>' : ""}</p>
      <details><summary class="muted" style="cursor:pointer">明細</summary>
        <table class="stats" style="margin-top:8px"><tr><th>服務</th><th>次數</th><th>費用</th></tr>
          ${st.breakdown.map(b => `<tr><td>${esc(b.model)}</td><td>${b.calls}</td><td>US$${b.cost_usd}</td></tr>`).join("")}
        </table>
        <p class="muted" style="margin-top:8px">調整每月預算:進階設定 → 每月預算。</p>
      </details>
    </div>
    <h2>功能</h2>
    <details class="group" id="ad-notes-group">
      <summary><span class="g-ico">📝</span>精煉筆記<span class="muted g-hint" id="ad-notes-hint">載入中…</span></summary>
      <div class="card">
        <div id="ad-notes-kept" class="muted">載入中…</div>
        <p class="muted" style="margin-top:8px">筆記=盤點討論中你同意收錄的洞見,
          存在獨立筆記庫(絕不混入原始逐字稿檢索)。要整理研究庫:在 Claude Code
          說「盤點研究庫」——掃描、討論、決定去留一次完成。</p>
      </div>
    </details>
    <details class="group">
      <summary><span class="g-ico">📁</span>資料庫分類<span class="muted g-hint">食譜・AI・自訂</span></summary>
      <div class="card">
        <p class="muted">把來源分進不同資料庫,研究庫與對話範圍都能按庫篩選。
          刪除資料庫時,裡面的來源會回到「未分類」,不會被刪。</p>
        <div id="ad-col-list" class="muted">載入中…</div>
        <div class="row" style="margin-top:8px">
          <input id="ad-col-name" class="grow" placeholder="新資料庫名稱(30 字內)" maxlength="30">
          <button class="btn" id="ad-col-add">新增</button></div>
      </div>
    </details>
    <div class="glink" id="ad-settings-link" role="button" tabindex="0">
      <span class="g-ico">⚙️</span>進階設定<span class="muted g-hint">安裝・金鑰・備份・維修</span></div>
    <h2>說明</h2>
    <details class="group">
      <summary><span class="g-ico">📖</span>使用說明<span class="muted g-hint">完整操作手冊</span></summary>
      ${MANUAL_HTML}
    </details>`;
  loadNotes();
  const goSettings = () => { state.adminSub = "settings"; renderSettings(); };
  $("#ad-settings-link").onclick = goSettings;
  $("#ad-settings-link").onkeydown = e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goSettings(); }
  };
  async function loadCollectionsAdmin() {
    let cd;
    try { cd = await api("/collections"); }
    catch { $("#ad-col-list").textContent = "載入失敗"; return; }
    const box = $("#ad-col-list");
    box.innerHTML = cd.collections.length
      ? cd.collections.map(c => `<div class="pick-row">
          <span class="pt">📁 ${esc(c.name)}<span class="muted">(${c.source_count})</span></span>
          <button class="btn ghost" data-col-ren="${c.collection_id}" data-name="${esc(c.name)}"
            style="padding:4px 10px;font-size:12px">改名</button>
          <button class="row-del" data-col-del="${c.collection_id}" data-name="${esc(c.name)}"
            title="刪除資料庫">✕</button>
        </div>`).join("")
      : '<p class="muted">尚未建立資料庫。</p>';
    $$("[data-col-ren]", box).forEach(b => b.onclick = async () => {
      const name = prompt("新名稱:", b.dataset.name);
      if (!name || !name.trim()) return;
      try {
        await api(`/collections/${b.dataset.colRen}`, { method: "PATCH",
          body: JSON.stringify({ name: name.trim() }) });
        toast("已改名"); loadCollectionsAdmin();
      } catch (e) { toast("改名失敗:" + e.message); }
    });
    $$("[data-col-del]", box).forEach(b => b.onclick = async () => {
      if (!confirm(`刪除資料庫「${b.dataset.name}」?裡面的來源會回到未分類,不會被刪。`)) return;
      try {
        const r = await api(`/collections/${b.dataset.colDel}`, { method: "DELETE" });
        toast(`已刪除,${r.sources_released} 筆來源回到未分類`); loadCollectionsAdmin();
      } catch (e) { toast("刪除失敗:" + e.message); }
    });
  }
  loadCollectionsAdmin();
  $("#ad-col-add").onclick = async () => {
    const name = $("#ad-col-name").value.trim();
    if (!name) return toast("請先輸入名稱");
    try {
      await api("/collections", { method: "POST", body: JSON.stringify({ name }) });
      $("#ad-col-name").value = "";
      toast("已新增資料庫"); loadCollectionsAdmin();
    } catch (e) { toast("新增失敗:" + e.message); }
  };
}

// 進階設定子頁(管理拆分 2026-07-16):一次性設定與維修工具,
// 每項=標題+一句話說明常駐,點開才展開詳細用法與控件
async function renderSettings() {
  state.adminSub = "settings";
  main.innerHTML = '<p class="muted">載入中…</p>';
  let h, st;
  try { [h, st] = await Promise.all([api("/admin/health"), api("/stats")]); }
  catch (e) { main.innerHTML = `<div class="card">❌ ${esc(e.message)}</div>`; return; }
  const item = (ico, title, desc, body) => `
    <details class="group">
      <summary><span class="g-ico">${ico}</span><span class="g-2l"><span>${title}</span>
        <span class="g-desc">${desc}</span></span></summary>${body}
    </details>`;
  main.innerHTML = `
    <div class="masthead">
      <button class="scopebtn" id="st-back">‹ 管理</button>
      <span class="mt" style="margin-left:12px">進階設定</span></div>
    <p class="muted" style="margin:6px 2px 0">一次性設定與維修工具。每項附說明,點開才展開操作。</p>
    <h2>連接與安裝(設一次)</h2>
    ${item("📱", "iPhone 分享捷徑", "手機分享選單一鍵入庫;換機或重置 token 後重裝", `
      <div class="card">
        <p class="muted">在 IG/YouTube 滑到想收的內容 → 分享 → 點「存到 vidrag」。
          安裝資訊含 QR 與逐步教學;電腦登入用的 APP_TOKEN 也在裡面。</p>
        <div class="row"><button class="btn" id="ad-sc-gen">產生安裝資訊</button></div>
      </div>`)}
    ${item("🧩", "Chrome 擴充(電腦捷徑)", "工具列一鍵 + 右鍵選字/連結送進 vidrag", `
      <div class="card">
        <p class="muted">Chrome 建議用這個:下載後在 chrome://extensions 載入一次,
          之後任何頁面右鍵「存到 vidrag」可送選取文字/連結,或點工具列圖示送整頁。
          擴充本身不含 token,首次在選項頁貼一次即可。</p>
        <div class="row"><button class="btn" id="ad-ext-dl">下載擴充</button>
          <button class="btn ghost" id="ad-ext-help">安裝教學</button></div>
      </div>`)}
    ${item("💻", "電腦書籤", "不裝擴充時的替代:點書籤送進 vidrag", `
      <div class="card">
        <p class="muted">其他瀏覽器或不想裝擴充時用。加到書籤列後,在任何頁面點它:
          有選取文字就存文字、沒選取就存整頁網址。書籤本身不含 token。</p>
        <div class="row"><button class="btn" id="ad-bm-gen">產生電腦書籤</button></div>
      </div>`)}
    ${item("🖥", "Claude Desktop(MCP)", "讓電腦上的 Claude 直接查這個知識庫", `
      <div class="card">
        <p id="ad-mcp-status" class="muted">狀態載入中…</p>
        <p class="muted">產生設定、照教學貼進 Claude Desktop / Claude Code 一次即可。
          走訂閱額度、不另計 API 費;流量僅限 Tailscale 內網。</p>
        <div class="row"><button class="btn" id="ad-mcp-gen">產生連接設定</button></div>
      </div>`)}
    ${item("🔑", "重置 APP_TOKEN", "懷疑外洩時換新;所有裝置與捷徑需重新設定", `
      <div class="card"><div class="row"><button class="btn danger" id="ad-token-reset">重置 token</button></div></div>`)}
    <h2>攝取與 AI</h2>
    ${item("🗝", "API 金鑰", "轉錄(Groq)與問答(Anthropic)的鑰匙", `
      <div class="card">
        <p class="muted">目前:Groq <span id="ad-k-g">…</span>・Anthropic <span id="ad-k-a">…</span>。
          更新後立即生效;不回顯、不記錄。</p>
        <input id="ad-key-groq" placeholder="新 Groq key(留空不變)">
        <input id="ad-key-ant" placeholder="新 Anthropic key(留空不變)" style="margin-top:6px">
        <div class="row" style="margin-top:8px"><button class="btn" id="ad-keys-save">更新</button></div>
      </div>`)}
    ${item("💰", "每月預算", "超過時回答會附提醒,不會擋你使用", `
      <div class="card"><div class="row">
        <input id="ad-budget" type="number" value="${st.budget_usd}" style="max-width:110px">
        <button class="btn ghost" id="ad-budget-save">更新預算</button></div></div>`)}
    ${item("🌐", "JS 頁面雲端渲染(Jina)", "救回 Notion/表單這類動態頁;預設關閉", `
      <div class="card">
        <p class="muted">這類頁面的內容由瀏覽器現場組出來,一般抓取拿不到正文。
          開啟後,同類失敗會自動改走 Jina Reader 雲端渲染重抓——
          <b>該頁的公開網址會送到第三方服務</b>。</p>
        <div class="row" style="margin-top:8px"><button class="btn" id="ad-jina-toggle">…</button></div>
      </div>`)}
    ${item("📚", "專屬詞彙表", "自選股、公司名、術語,提升轉錄辨識", `
      <div class="card">
        <p class="muted">一行一詞;儲存後套用於之後的轉錄。</p>
        <textarea id="ad-vocab" rows="6">載入中…</textarea>
        <div class="row" style="margin-top:8px"><button class="btn" id="ad-vocab-save">儲存</button></div>
      </div>`)}
    ${item("📸", "Instagram cookie", "IG 影片開始失敗時,重新匯出上傳", `
      <div class="card" id="ad-igc">
        <p id="ad-igc-status" class="muted">狀態載入中…</p>
        <p class="muted">用「Get cookies.txt LOCALLY」登入 instagram.com 後匯出 Netscape
          cookies.txt 上傳。這是登入憑證,存在 Pi、不進 git;IG 改密碼或登出後需重傳。</p>
        <input type="file" id="ad-igc-file" accept=".txt">
        <div class="row" style="margin-top:8px"><button class="btn" id="ad-igc-save">上傳 cookie</button></div>
      </div>`)}
    ${item("⬆️", "元件更新(yt-dlp)", "平台改版導致抓取失敗時,一鍵更新", `
      <div class="card">
        <p class="muted">目前版本:<span id="ad-ytv">…</span>。更新中會暫停攝取,約一分鐘。</p>
        <div class="row"><button class="btn ghost" id="ad-yt-update">一鍵更新 yt-dlp</button></div>
      </div>`)}
    <h2>備份與資料</h2>
    ${item("🗄", "本機備份", "每天自動一份、保留 7 份;可手動備份與還原", `
      <div class="card">
        <div class="row"><button class="btn" id="ad-backup-now">立即備份</button></div>
        <div id="ad-backups" class="muted" style="margin-top:8px">載入中…</div>
      </div>`)}
    ${item("☁️", "異地加密備份(NAS)", "第二份備份,加密後上傳 NAS", `
      <div class="card" id="ad-offsite">
        <p id="ad-os-status" class="muted">狀態載入中…</p>
        <input id="ad-os-host" placeholder="NAS host / IP">
        <div class="row" style="margin-top:6px">
          <input id="ad-os-port" type="number" value="22" placeholder="port" style="max-width:96px">
          <input id="ad-os-user" class="grow" placeholder="使用者">
        </div>
        <input id="ad-os-pass" type="password" placeholder="NAS 密碼" style="margin-top:6px">
        <input id="ad-os-folder" placeholder="資料夾路徑,如 /vidrag-backups" style="margin-top:6px">
        <input id="ad-os-crypt" type="password" placeholder="加密密碼(自己另存到密碼管理器!)" style="margin-top:6px">
        <div class="row" style="margin-top:8px">
          <button class="btn" id="ad-os-save">儲存並測試連線</button>
        </div>
        <p class="muted">加密密碼是解開 NAS 上備份的唯一鑰匙。它存在 Pi、不隨備份上雲;
          但 Pi 若損壞,沒有這把密碼就無法還原異地備份——務必自己另存一份。</p>
      </div>`)}
    ${item("📤", "匯出全庫 Markdown", "把所有內容打包成一份文字檔", `
      <div class="card"><div class="row"><button class="btn ghost" id="ad-export-all">匯出全庫 Markdown</button></div></div>`)}`;
  $("#st-back").onclick = () => { state.adminSub = null; renderAdmin(); };
  // async fills
  api("/admin/version").then(v => $("#ad-ytv").textContent = v.yt_dlp_version);
  api("/admin/vocabulary").then(t => $("#ad-vocab").value = t);
  api("/admin/keys").then(k => { $("#ad-k-g").textContent = k.groq;
    $("#ad-k-a").textContent = k.anthropic; });
  loadBackups();
  api("/admin/offsite/status").then(s => {
    const el = $("#ad-os-status");
    if (!s.configured) { el.innerHTML = '<span class="light r"></span>尚未設定異地備份'; return; }
    if (!s.last_at) { el.innerHTML = '<span class="light y"></span>已設定,尚未上傳'; return; }
    const ok = s.last_ok === "1";
    el.innerHTML = `<span class="light ${ok ? "g" : "r"}"></span>最近上傳:${esc(s.last_at)}`
      + (ok ? "" : `・<span class="badge err">失敗</span> ${esc(s.last_error || "")}`);
  }).catch(() => {});
  api("/admin/ig-cookie/status").then(s => {
    $("#ad-igc-status").innerHTML = s.present
      ? `<span class="light g"></span>已上傳(${esc(s.updated_at)})`
      : '<span class="light y"></span>尚未上傳 cookie';
  }).catch(() => {});
  api("/admin/mcp/status").then(s => {
    $("#ad-mcp-status").innerHTML = s.enabled
      ? '<span class="light g"></span>已啟用(token 已產生)'
      : '<span class="light y"></span>尚未啟用';
  }).catch(() => {});
  // handlers
  $("#ad-budget-save").onclick = async () => {
    await api("/admin/budget", { method: "POST", body: JSON.stringify({
      monthly_budget_usd: parseFloat($("#ad-budget").value) }) });
    toast("預算已更新");
  };
  $("#ad-os-save").onclick = async ev => {
    ev.target.disabled = true; ev.target.textContent = "設定中…";
    try {
      await api("/admin/offsite/config", { method: "POST", body: JSON.stringify({
        host: $("#ad-os-host").value.trim(),
        port: parseInt($("#ad-os-port").value, 10) || 22,
        user: $("#ad-os-user").value.trim(),
        password: $("#ad-os-pass").value,
        folder: $("#ad-os-folder").value.trim(),
        crypt_password: $("#ad-os-crypt").value,
      }) });
      toast("設定已存,測試連線中…", 8000);
      await api("/admin/offsite/test", { method: "POST" });
      toast("✅ 異地備份連線成功"); renderSettings();
    } catch (e) {
      toast("❌ " + e.message, 6000);
      ev.target.disabled = false; ev.target.textContent = "儲存並測試連線";
    }
  };
  $("#ad-igc-save").onclick = async () => {
    const f = $("#ad-igc-file").files[0];
    if (!f) { toast("請先選擇 cookies.txt"); return; }
    try {
      const content = await f.text();
      await api("/admin/ig-cookie", { method: "POST", body: JSON.stringify({ content }) });
      toast("✅ Instagram cookie 已上傳"); renderSettings();
    } catch (e) { toast("❌ " + e.message, 5000); }
  };
  $("#ad-mcp-gen").onclick = async () => {
    const enabled = $("#ad-mcp-status").textContent.includes("已啟用");
    if (enabled && !confirm("重新產生會換新 token,舊的 Claude Desktop 設定會失效。確定?")) return;
    const r = await api("/admin/mcp/token/reset", { method: "POST" });
    const pre = t => `<pre style="background:var(--surface2);padding:10px;border-radius:8px;
      overflow-x:auto;font-size:12px;user-select:all">${esc(t)}</pre>`;
    modal(`<h2>連接 Claude Desktop</h2>
      <ol style="padding-left:20px">${r.steps.map(x => `<li>${esc(x)}</li>`).join("")}</ol>
      <p class="row"><b class="grow">Windows 設定</b>
        <button class="btn ghost" id="mcp-copy-w">複製</button></p>
      ${pre(r.config_windows)}
      <p class="row"><b class="grow">macOS 設定</b>
        <button class="btn ghost" id="mcp-copy-m">複製</button></p>
      ${pre(r.config_macos)}
      <p class="muted">${esc(r.warning)} 這段 JSON 含 token,請當密碼對待。</p>`);
    const copy = async (txt, btn) => {
      try { await navigator.clipboard.writeText(txt); btn.textContent = "已複製 ✓"; }
      catch { toast("無法自動複製,請長按選取後手動複製"); }
    };
    $("#mcp-copy-w").onclick = ev => copy(r.config_windows, ev.target);
    $("#mcp-copy-m").onclick = ev => copy(r.config_macos, ev.target);
    renderSettings();  // status light -> enabled (modal stays open on top)
  };
  $("#ad-sc-gen").onclick = async () => {
    const s = await api("/admin/shortcut");
    const m = modal(`<h2>安裝「存到 vidrag」捷徑</h2>
      <div class="card p2"><p class="row" style="margin:0 0 6px"><b class="grow">APP_TOKEN(電腦登入用)</b>
        <button class="btn ghost" id="sc-tok-copy">複製 token</button></p>
        <p class="muted mono" style="word-break:break-all;user-select:all;margin:0">${esc(s.app_token)}</p>
        <p class="muted" style="margin:6px 0 0">在電腦開這個網站時,貼的是上面這串,
          <b>不是</b>下方的完整網址。</p></div>
      <div class="qr"><img src="${s.qr_svg_data_uri}" alt="QR"></div>
      <p class="muted" style="word-break:break-all">${esc(s.ingest_url)}</p>
      <ol>${s.steps.map(x => `<li>${esc(x)}</li>`).join("")}</ol>`);
    $("#sc-tok-copy", m).onclick = async ev => {
      try { await navigator.clipboard.writeText(s.app_token); ev.target.textContent = "已複製 ✓"; }
      catch { toast("無法自動複製,請長按選取 token 手動複製"); }
    };
  };
  $("#ad-ext-dl").onclick = async ev => {
    // Zip is binary — bypass api() (JSON helper) and stream it as a blob.
    // Token goes in the header, never the URL (privacy: no secret in URLs).
    ev.target.disabled = true;
    try {
      const res = await fetch("/api/admin/extension.zip",
        { headers: { "X-App-Token": state.token } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "vidrag-extension.zip";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      toast("已下載 vidrag-extension.zip,照「安裝教學」載入");
    } catch (e) { toast("下載失敗:" + e.message, 5000); }
    ev.target.disabled = false;
  };
  $("#ad-ext-help").onclick = () => {
    modal(`<h2>安裝「存到 vidrag」Chrome 擴充</h2>
      <ol style="padding-left:20px">
        <li>按「下載擴充」,把 <b>vidrag-extension.zip</b> 解壓縮到一個固定資料夾(之後別刪或移動)</li>
        <li>Chrome 網址列輸入 <span class="mono">chrome://extensions</span> 開啟</li>
        <li>打開右上角的<b>開發人員模式</b>(Developer mode)</li>
        <li>按<b>載入未封裝項目</b>(Load unpacked)→ 選剛剛解壓、含 manifest.json 的資料夾</li>
        <li>(建議)工具列拼圖圖示 → 把「存到 vidrag」<b>釘選</b>出來</li>
        <li>對圖示按右鍵 → <b>選項</b>,貼上 APP_TOKEN(網址已自動帶入)→ 儲存
          <span class="muted">(token 在「iPhone 分享捷徑」的安裝資訊裡有複製鈕)</span></li>
        <li>之後:任何頁面<b>點圖示</b>存整頁;<b>選一段文字</b>或<b>對連結按右鍵</b>→「存到 vidrag」</li>
      </ol>
      <p class="muted">擴充僅對這個 vidrag 網址發送,走你的 Tailscale 內網;
        Chrome 偶爾會提示「停用開發人員擴充」,按保留即可。</p>`);
  };
  $("#ad-bm-gen").onclick = () => {
    // 書籤=在原頁面開新視窗帶 ?share=<url> 或 ?text=<選取文字> 回 PWA,由前端
    // 排入——不用跨域 fetch(CORS),也不把 token 存進書籤。有選字送文字、
    // 沒選字送整頁網址(與 iOS 捷徑對等)。
    const bm = "javascript:(function(){var s=(window.getSelection?String(window.getSelection()):'').trim();"
      + "var b='" + location.origin + "';"
      + "window.open(b+'/?'+(s?'text='+encodeURIComponent(s):'share='+encodeURIComponent(location.href)),'vidrag');})()";
    const m = modal(`<h2>存到 vidrag(電腦書籤)</h2>
      <p class="muted">Chrome 建議改用「🧩 Chrome 擴充」更順;書籤適合其他瀏覽器或不想裝擴充時。</p>
      <ol style="padding-left:20px">
        <li>按下方「複製」</li>
        <li>電腦瀏覽器:在書籤列按右鍵 → 新增書籤(或網頁)</li>
        <li>名稱填「存到 vidrag」,網址欄貼上剛複製的內容</li>
        <li>之後在任何頁面點這個書籤:<b>有選取一段文字就存文字、沒選取就存整頁網址</b></li></ol>
      <pre style="background:var(--surface2);padding:10px;border-radius:6px;font-size:12px;
        user-select:all;white-space:pre-wrap;word-break:break-all">${esc(bm)}</pre>
      <div class="row"><button class="btn" id="bm-copy">複製</button></div>
      <p class="muted">第一次在該電腦使用會要求輸入 APP_TOKEN,輸入一次即記住。</p>`);
    $("#bm-copy", m).onclick = async ev => {
      try { await navigator.clipboard.writeText(bm); ev.target.textContent = "已複製 ✓"; }
      catch { toast("無法自動複製,請選取灰框內文字手動複製"); }
    };
  };
  $("#ad-token-reset").onclick = async () => {
    if (!confirm("重置後所有裝置需重新輸入 token、重裝捷徑。確定?")) return;
    const r = await api("/admin/token/reset", { method: "POST" });
    state.token = r.app_token;
    localStorage.setItem("vidrag_token", state.token);
    modal(`<h2>新 token 已生效</h2><p style="word-break:break-all">${esc(r.app_token)}</p>
      <p class="muted">${esc(r.warning)}</p>`);
  };
  $("#ad-yt-update").onclick = async ev => {
    ev.target.disabled = true; toast("更新中…", 10000);
    try { const r = await api("/admin/update/ytdlp", { method: "POST" });
      toast("yt-dlp 已更新到 " + r.yt_dlp_version); $("#ad-ytv").textContent = r.yt_dlp_version;
    } catch (e) { toast("更新失敗:" + e.message); }
    ev.target.disabled = false;
  };
  const jbtn = $("#ad-jina-toggle");
  const setJinaBtn = on => {
    jbtn.textContent = on ? "已開啟——點擊關閉" : "已關閉——點擊開啟";
    jbtn.dataset.on = on ? "1" : "0";
  };
  setJinaBtn(!!h.jina_fallback);
  jbtn.onclick = async () => {
    const next = jbtn.dataset.on !== "1";
    try {
      await api("/admin/jina", { method: "POST",
        body: JSON.stringify({ enabled: next }) });
    } catch (e) { toast("切換失敗:" + e.message); return; }
    setJinaBtn(next);
    toast(next ? "已開啟。研究庫中同類失敗的卡片可直接按「重試」" : "已關閉雲端渲染 fallback");
  };
  $("#ad-vocab-save").onclick = async () => {
    await api("/admin/vocabulary", { method: "POST",
      body: JSON.stringify({ text: $("#ad-vocab").value }) });
    toast("詞彙表已儲存,之後的轉錄立即生效");
  };
  $("#ad-keys-save").onclick = async () => {
    const body = {};
    if ($("#ad-key-groq").value.trim()) body.groq_api_key = $("#ad-key-groq").value.trim();
    if ($("#ad-key-ant").value.trim()) body.anthropic_api_key = $("#ad-key-ant").value.trim();
    if (!Object.keys(body).length) return toast("沒有輸入新金鑰");
    await api("/admin/keys", { method: "POST", body: JSON.stringify(body) });
    $("#ad-key-groq").value = ""; $("#ad-key-ant").value = "";
    toast("金鑰已更新"); renderSettings();
  };
  $("#ad-backup-now").onclick = async () => {
    const r = await api("/admin/backup", { method: "POST" });
    toast("備份完成:" + r.created); loadBackups();
  };
  $("#ad-export-all").onclick = () => downloadText("/admin/export", "vidrag-all.md");
}

const NOTE_KIND_LABEL = { skill: "🛠 skill", rule: "📏 規範", project: "💡 專案",
  money: "💰 賺錢", insight: "🔍 洞見" };

async function loadNotes() {
  let kept;
  try { kept = await api("/notes?status=kept"); }
  catch { const h = $("#ad-notes-hint"); if (h) h.textContent = "載入失敗"; return; }
  const hint = $("#ad-notes-hint"), box = $("#ad-notes-kept");
  if (!box) return;
  hint.textContent = kept.notes.length ? `${kept.notes.length} 筆已收錄` : "尚無筆記";
  box.innerHTML = kept.notes.length
    ? kept.notes.slice(0, 20).map(n => `
      <div class="card p2" style="margin:8px 0">
        <p><span class="badge ok">${NOTE_KIND_LABEL[n.kind] || esc(n.kind)}</span>
          <b>${esc(n.title)}</b>
          <span class="muted">${esc((n.decided_at || n.created_at || "").slice(0, 10))}</span></p>
        <p style="font-size:14px;white-space:pre-wrap">${esc(n.content)}</p>
        ${n.application ? `<p class="muted" style="white-space:pre-wrap">應用:${esc(n.application)}</p>` : ""}
        <p class="muted">溯源:${(n.sources || []).map(x =>
          x.title ? `「${esc(x.title)}」` : "(來源已刪除)").join("、") || "—"}</p>
      </div>`).join("")
    : '<p class="muted">還沒有收錄的筆記。在 Claude Code 說「盤點研究庫」開始整理。</p>';
}

async function loadBackups() {
  const d = await api("/admin/backups");
  $("#ad-backups").innerHTML = d.backups.length
    ? d.backups.map(b => `<div class="row" style="margin:4px 0">
        <span class="grow">${esc(b.name)}(${b.size_mb} MB)</span>
        <button class="btn ghost" data-restore="${esc(b.name)}">還原</button></div>`).join("")
    : "尚無備份";
  $$("[data-restore]").forEach(b => b.onclick = async () => {
    if (!confirm(`還原到 ${b.dataset.restore}?\n還原前會自動再備份一次現況。`)) return;
    if (!confirm("第二次確認:目前資料庫會被此備份取代。確定還原?")) return;
    const r = await api("/admin/restore", { method: "POST",
      body: JSON.stringify({ name: b.dataset.restore }) });
    toast("已還原,完整性:" + (r.integrity_ok ? "OK" : "異常!"));
  });
}

// ---------- boot ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
render();
