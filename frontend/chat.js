// Chat panel for a notebook: persistent thread (SQLite), code-written progress narration, streamed answers with source chips,
// attachments with upload progress, "Ask about this" context, unread dot. The thread is delivered by cursor (messages?after=id),
// so a dropped stream or a reload never loses a message.
import { esc, ICON } from "./util.js";
import { logoSvg } from "./pixel-mascot.js";

const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
const STATUS = { reading: "Reading…", ready: "Read", partly_readable: "Partly readable", unreadable: "Couldn't read" };

export function createChat(ctx) {
  // ctx: { nid, learner, api, toast, onChip(chip), getChapterId(), getSources(), isOpen(), notifyUnread(), wireMic(btn, onText) }
  const el = { msgs: document.getElementById("msgs"), input: document.getElementById("chatinput"), send: document.getElementById("chatsend"),
               atts: document.getElementById("chatatts"), ctx: document.getElementById("ctxrow"), file: document.getElementById("chatfile"),
               pane: document.getElementById("chatpane") };
  const st = { lastId: 0, seen: new Set(), files: [], context: [], busy: false, stick: true, seq: 0, cards: new Map() };

  // ------------------------------------------------------------------ rendering
  const attHtml = (list) => (list || []).map((a) => {
    const live = a.kind !== "url" && ctx.getSources().find((s) => s.title === a.name);
    const status = a.pending ? (a.progress != null && a.progress < 100 ? `Uploading ${a.progress}%` : "Sending…") : live ? STATUS[live.status] || "" : "";
    return `<span class="attchip" data-n="${esc(a.name)}">${a.kind === "image" ? ICON.doc : ICON.doc}<span class="nm">${esc(String(a.name).slice(0, 36))}</span>
      ${a.size ? `<span class="sz">${fmtSize(a.size)}</span>` : ""}${status ? `<span class="st">${status}</span>` : ""}</span>`;
  }).join("");
  const chipsHtml = (refs) => (refs || []).length ? `<div class="chips">${refs.map((r, i) => `<button class="srcchip" data-chip="${i}" type="button">${r.kind === "chapter" ? "Chapter · " : r.kind === "resource" ? "" : "Web · "}${esc(r.label)}</button>`).join("")}</div>` : "";

  // proposal card: a plain summary, what Apply will do, and buttons that follow its state (pending, applied, undone, dismissed)
  const PSTATE = { applied: "Applied", undone: "Undone", dismissed: "Dismissed" };
  function cardHtml(p) {
    const lines = p.lines.length > 1 ? `<ul class="plines">${p.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : "";
    const title = p.lines.length > 1 ? `${p.lines.length} changes to your course` : esc(p.lines[0] || p.summary);
    const acts = p.status === "pending" ? `<button class="btn" data-pa="apply" type="button">Apply</button><button class="btn ghost" data-pa="dismiss" type="button">Dismiss</button>`
      : p.status === "applied" && p.can_undo ? `<span class="pstate">Applied</span><button class="btn ghost" data-pa="undo" type="button">Undo</button>`
      : `<span class="pstate">${PSTATE[p.status] || p.status}</span>`;
    return `<div class="pcard" data-p="${p.id}" data-s="${p.status}"><div class="ptitle">${title}</div>${lines}<div class="pact">${acts}</div></div>`;
  }
  function wireCard(root, p) {
    root.querySelectorAll("[data-pa]").forEach((b) => b.onclick = async () => {
      const act = b.dataset.pa; root.querySelectorAll("[data-pa]").forEach((x) => (x.disabled = true));
      try { const v = await ctx.api(`notebooks/${ctx.nid}/proposals/${p.id}/${act}`, {}); paintCard(root, v); ctx.afterEdit?.(act); }
      catch (e) { ctx.toast(e.message || String(e)); paintCard(root, p); }
    });
  }
  function paintCard(root, p) { const col = root.querySelector(".pcard"); if (!col) return; col.outerHTML = cardHtml(p); st.cards.set(p.id, p); wireCard(root, p); }

  const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
  const COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>';
  function addCopy(row, text) {   // a quiet copy button on answers (shown on hover or focus)
    const col = row.querySelector(".msgcol"); if (!col || col.querySelector(".copybtn")) return;
    const b = document.createElement("button"); b.className = "copybtn"; b.type = "button"; b.setAttribute("aria-label", "Copy answer"); b.innerHTML = COPY;
    b.onclick = async () => { try { await navigator.clipboard.writeText(text); b.classList.add("done"); setTimeout(() => b.classList.remove("done"), 1300); } catch { ctx.toast("Couldn't copy."); } };
    col.appendChild(b);
  }
  function place(row) {   // consecutive assistant bubbles share one avatar
    const prev = el.msgs.lastElementChild;
    if (prev && prev.classList.contains("bot") && row.classList.contains("bot")) row.classList.add("cont");
    el.msgs.appendChild(row);
  }

  function bubble(m) {
    const row = document.createElement("div");
    row.className = "msgrow " + (m.role === "user" ? "me" : m.kind === "system" ? "sysrow" : "bot");
    row.dataset.id = m.id || "";
    if (m.kind === "proposal" && m.proposal) {   // an edit suggestion: a card, never a silent change
      row.className = "msgrow bot";
      row.innerHTML = `<span class="pul" aria-hidden="true">${logoSvg()}</span><div class="msgcol">${cardHtml(m.proposal)}</div>`;
      st.cards.set(m.proposal.id, m.proposal); wireCard(row, m.proposal);
      return row;
    }
    const avatar = m.role === "user" || m.kind === "system" ? "" : `<span class="pul" aria-hidden="true">${logoSvg()}</span>`;
    if (m.created_at) row.title = clock(m.created_at);
    row.innerHTML = `${avatar}<div class="msgcol"><div class="msg ${m.role === "user" ? "user" : m.kind === "system" ? "sys" : "bot"}">${esc(m.content).replace(/\n/g, "<br>")}${m.role === "user" ? attHtml(m.attachments) : ""}</div>${chipsHtml(m.refs)}</div>`;
    row.querySelectorAll(".srcchip").forEach((b) => b.onclick = () => ctx.onChip(m.refs[Number(b.dataset.chip)]));
    if (m.role === "assistant" && m.kind === "chat" && m.content) addCopy(row, m.content);
    return row;
  }
  const jump = document.getElementById("jump");
  const scrollEnd = (smooth) => {
    if (st.stick) { if (smooth) el.msgs.scrollTo({ top: el.msgs.scrollHeight, behavior: "smooth" }); else el.msgs.scrollTop = el.msgs.scrollHeight; if (jump) jump.hidden = true; }
    else if (jump && smooth) jump.hidden = false;   // something new arrived while the student was reading older messages
  };
  el.msgs.addEventListener("scroll", () => { st.stick = el.msgs.scrollHeight - el.msgs.scrollTop - el.msgs.clientHeight < 60; if (st.stick && jump) jump.hidden = true; });
  if (jump) jump.onclick = () => { st.stick = true; el.msgs.scrollTo({ top: el.msgs.scrollHeight, behavior: "smooth" }); jump.hidden = true; };

  function add(m, quiet) {
    if (m.id) { if (st.seen.has(m.id)) return; st.seen.add(m.id); st.lastId = Math.max(st.lastId, m.id); }
    const row = bubble(m); if (!quiet) row.classList.add("enter");
    if (m.role === "user") st.hasUser = true;
    place(row); scrollEnd(!quiet); renderSuggest();
    if (!quiet && m.role === "assistant" && !ctx.isOpen()) ctx.notifyUnread();
  }

  // quick prompts while the thread is still only progress notes
  const SUGGEST = [["Quiz me on this chapter", "Quiz me on this chapter", true], ["Explain this chapter simply", "Explain this chapter again, simpler", true],
                   ["What should I learn first?", "What should I learn first?", true], ["Add a chapter on…", "Add a chapter on ", false]];
  function renderSuggest() {
    const box = document.getElementById("suggest"); if (!box) return;
    const empty = el.msgs.querySelector(".chat-empty");
    if (el.msgs.querySelector(".msgrow")) empty?.remove();
    else if (!empty && st.loaded) el.msgs.insertAdjacentHTML("beforeend", `<div class="chat-empty"><span class="chat-empty-mark">${logoSvg("big")}</span><p>Ask me anything about this notebook, add a file or link, or ask me to change the course.</p></div>`);
    if (st.hasUser || st.busy) { box.innerHTML = ""; return; }
    if (box.childElementCount) return;
    box.innerHTML = SUGGEST.map(([label], i) => `<button type="button" class="sugg" data-i="${i}">${esc(label)}</button>`).join("");
    box.querySelectorAll(".sugg").forEach((b) => b.onclick = () => { const [, text, send] = SUGGEST[Number(b.dataset.i)]; el.input.value = text; autosize(); sync2(); if (send) sendTurn(); else { el.input.focus(); el.input.setSelectionRange(text.length, text.length); } });
  }

  async function sync() {
    if (st.busy) return;   // the open turn renders its own messages; this runs again when it ends
    try {
      const r = await ctx.api(`notebooks/${ctx.nid}/messages?after=${st.lastId}`);
      const quiet = st.lastId === 0;   // the first load is history, not news
      r.messages.forEach((m) => { if (m.role === "user") st.hasUser = true; });
      r.messages.forEach((m) => add(m, quiet));
      st.loaded = true; renderSuggest();
    } catch { /* the next snapshot retries */ }
  }
  function refreshCards(nb) {   // card buttons follow the notebook snapshot (an Undo is only offered for the latest change)
    (nb.proposals || []).forEach((s) => {
      const p = st.cards.get(s.id); if (!p || (p.status === s.status && p.can_undo === s.can_undo)) return;
      const root = el.msgs.querySelector(`.pcard[data-p="${s.id}"]`)?.closest(".msgrow"); if (root) paintCard(root, { ...p, status: s.status, can_undo: s.can_undo });
    });
  }
  function refreshStatuses() {   // upload chips follow the resource status in the notebook snapshot
    el.msgs.querySelectorAll(".attchip").forEach((c) => {
      const live = ctx.getSources().find((s) => s.title === c.dataset.n);
      const s = c.querySelector(".st");
      if (live && STATUS[live.status]) { if (s) s.textContent = STATUS[live.status]; else c.insertAdjacentHTML("beforeend", `<span class="st">${STATUS[live.status]}</span>`); }
    });
  }

  // ------------------------------------------------------------------ composer
  const autosize = () => { el.input.style.height = "auto"; el.input.style.height = Math.min(el.input.scrollHeight, 150) + "px"; };
  function drawAtts() {
    el.atts.innerHTML = st.files.map((f, i) => `<span class="attchip"><span class="nm">${esc(f.name.slice(0, 30))}</span><span class="sz">${fmtSize(f.size)}</span>
      <button class="x" data-i="${i}" type="button" aria-label="Remove ${esc(f.name)}">×</button></span>`).join("");
    el.atts.querySelectorAll(".x").forEach((b) => b.onclick = () => { st.files.splice(Number(b.dataset.i), 1); drawAtts(); sync2(); });
    el.ctx.innerHTML = st.context.map((c, i) => `<span class="ctxchip">About: ${esc(c.label)}<button class="x" data-i="${i}" type="button" aria-label="Remove context">×</button></span>`).join("");
    el.ctx.querySelectorAll(".x").forEach((b) => b.onclick = () => { st.context.splice(Number(b.dataset.i), 1); drawAtts(); });
    sync2();
  }
  const sync2 = () => { el.send.disabled = st.busy || !(el.input.value.trim() || st.files.length); };
  const addFiles = (list) => { st.files = [...st.files, ...list].slice(0, 6); drawAtts(); };

  function ask(block) {   // "Ask about this"
    if (!st.context.find((c) => c.id === block.id)) st.context = [...st.context, block].slice(-3);
    drawAtts(); ctx.openChat(); el.input.focus();
  }

  // One turn: POST (multipart) and read the server-sent events as they arrive; upload progress for files.
  function sendTurn() {
    const text = el.input.value.trim(); if (st.busy || !(text || st.files.length)) return;
    st.busy = true; sync2();
    const form = new FormData();
    form.append("learner_id", ctx.learner); form.append("text", text);
    const ch = ctx.getChapterId(); if (ch) form.append("chapter_id", String(ch));
    if (st.context.length) form.append("block_ids", st.context.map((c) => c.id).join(","));
    st.files.forEach((f) => form.append("files", f));
    const pending = st.files.map((f) => ({ name: f.name, size: f.size, kind: f.name.toLowerCase().endsWith(".pdf") ? "pdf" : f.type.startsWith("image/") ? "image" : "text", pending: true, progress: 0 }));
    const mine = bubble({ role: "user", content: text, attachments: pending, id: "" }); mine.classList.add("enter"); st.hasUser = true; place(mine); renderSuggest(); st.stick = true; scrollEnd(true);
    el.input.value = ""; st.files = []; st.context = []; autosize(); drawAtts();
    let typing = null, botText = "", botEl = null, done = false, read = 0, errored = false;
    const showTyping = (t) => {
      if (!typing) { typing = bubble({ role: "assistant", kind: "chat", content: "" }); typing.classList.add("typing", "enter"); place(typing); }
      typing.querySelector(".msg").innerHTML = `<span class="dots3" aria-hidden="true"><i></i><i></i><i></i></span><span class="tt">${esc(t)}</span>`; scrollEnd(true);
    };

    const handle = (name, d) => {
      if (name === "user") { mine.dataset.id = d.id; st.seen.add(d.id); st.lastId = Math.max(st.lastId, d.id); mine.querySelectorAll(".attchip .st").forEach((s) => (s.textContent = "Reading…")); }
      else if (name === "status") showTyping(d.text);
      else if (name === "proposal") { typing?.remove(); typing = null; st.seen.add(d.id); st.lastId = Math.max(st.lastId, d.id); const pr = bubble({ role: "assistant", kind: "proposal", id: d.id, proposal: d.proposal }); pr.classList.add("enter"); place(pr); scrollEnd(true); }
      else if (name === "delta") { if (!botEl) { typing?.remove(); typing = null; botEl = bubble({ role: "assistant", kind: "chat", content: "" }); botEl.classList.add("enter"); place(botEl); } botText += d.text; botEl.querySelector(".msg").innerHTML = esc(botText).replace(/\n/g, "<br>"); scrollEnd(); }
      else if (name === "done") { done = true; if (botEl && d.id) { st.seen.add(d.id); st.lastId = Math.max(st.lastId, d.id); botEl.dataset.id = d.id; addCopy(botEl, botText); const col = botEl.querySelector(".msgcol"); col.insertAdjacentHTML("beforeend", chipsHtml(d.refs)); col.querySelectorAll(".srcchip").forEach((b) => b.onclick = () => ctx.onChip(d.refs[Number(b.dataset.chip)])); } scrollEnd(); }
      else if (name === "error") { errored = true; typing?.remove(); typing = null; add({ role: "assistant", kind: "chat", content: d.content, id: d.id }); }
    };
    const finish = () => { st.busy = false; typing?.remove(); sync2(); sync(); el.input.focus(); };

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/notebooks/${ctx.nid}/chat`);
    xhr.upload.onprogress = (e) => { if (!e.lengthComputable) return; const p = Math.round((100 * e.loaded) / e.total); mine.querySelectorAll(".attchip .st").forEach((s) => (s.textContent = p < 100 ? `Uploading ${p}%` : "Sending…")); };
    xhr.onprogress = () => {
      const chunk = xhr.responseText.slice(read); const cut = chunk.lastIndexOf("\n\n"); if (cut < 0) return;
      read += cut + 2;
      chunk.slice(0, cut).split("\n\n").forEach((blk) => { const l = blk.split("\n"); if (l[0]?.startsWith("event: ") && l[1]) { try { handle(l[0].slice(7), JSON.parse(l[1].slice(6))); } catch { /* partial */ } } });
    };
    xhr.onload = () => {
      if (xhr.status >= 400) { let m = "That didn't go through."; try { m = JSON.parse(xhr.responseText).detail || m; } catch { /* keep */ } mine.remove(); st.files = []; ctx.toast(m); el.input.value = text; autosize(); }
      else xhr.onprogress();
      finish();
    };
    xhr.onerror = () => { ctx.toast("Couldn't reach Aasaan. Please try again."); mine.remove(); el.input.value = text; autosize(); finish(); };
    xhr.send(form);
  }

  // ------------------------------------------------------------------ wiring
  el.input.oninput = () => { autosize(); sync2(); };
  el.input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendTurn(); } };
  el.send.onclick = sendTurn;
  document.getElementById("chatattach").onclick = () => el.file.click();
  el.file.onchange = () => { addFiles([...el.file.files]); el.file.value = ""; };
  el.input.addEventListener("paste", (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } });
  ["dragenter", "dragover"].forEach((t) => el.pane.addEventListener(t, (e) => { e.preventDefault(); el.pane.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => el.pane.addEventListener(t, (e) => { e.preventDefault(); el.pane.classList.remove("drag"); }));
  el.pane.addEventListener("drop", (e) => addFiles([...e.dataTransfer.files]));
  const mic = document.getElementById("chatmic");
  if (mic && ctx.wireMic) ctx.wireMic(mic, (t) => { el.input.value = (el.input.value ? el.input.value + " " : "") + t; autosize(); sync2(); el.input.focus(); });
  const DEFAULT_PH = el.input.placeholder;
  const setFocus = (title) => { el.input.placeholder = title ? `Ask about “${title.slice(0, 38)}”, or add a file or link…` : DEFAULT_PH; };
  setFocus(ctx.getChapterTitle?.() || "");
  drawAtts(); sync();

  return { setFocus, sync, refreshStatuses, ask, addFiles, onSnapshot(nb) { if (nb.message_seq > st.lastId) sync(); refreshStatuses(); refreshCards(nb); }, focus() { el.input.focus(); } };
}
