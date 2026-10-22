// Aasaan UI: Home -> Loading -> Notebook (roadmap + graph + chapter page). The agents are never shown.
import { esc, store, LANGS, lang, api, ICON, timeAgo, speak, startRecording, themePref, setTheme } from "./util.js";
import { logoSvg, mountHero } from "./pixel-mascot.js";
import { renderMarkdown } from "./markdown.js";
import { renderGraph, legend } from "./graph.js";
import { createChat } from "./chat.js";
import { createResources } from "./resources.js";
import { createActivity } from "./activity.js";

const $app = document.getElementById("app");
const $bar = document.getElementById("bar");
const S = { learner: null, session: null, mode: "live", nb: null, chapter: null, files: [], side: store.get("side") || "roadmap",
            slide: {}, quiz: null, poll: null, es: null, companion: "" };
const PLACEHOLDERS = ["What do you want to learn? e.g. I want to learn DSA", "Paste notes, links, or a chapter...", "Attach a textbook photo or PDF..."];
const GEN_TEXT = { queued: "Queued", writing: "Writing notes", checking: "Checking facts", failed: "Failed" };
const LEARN_TEXT = { not_started: "Not started", in_progress: "In progress", done: "Done", needs_review: "Needs review" };

// ------------------------------------------------------------------------ shell
function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.setAttribute("role", "alert"); document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 5000);
}
const fail = (e) => toast(e.message || String(e));

function cleanup() {
  if (S.hero) { S.hero.destroy(); S.hero = null; }
  clearInterval(S.poll); S.poll = null;
  if (S.es) { S.es.close(); S.es = null; }
  setReconnecting(false);
}

function topbar(right = "") {
  $bar.innerHTML = `${S.modelReady === false ? `<div class="demo" role="alert">No AI model is connected. Add GEMINI_API_KEY to the .env file and restart the server to build notebooks.</div>` : ""}
    <div class="bar"><a class="brand" href="#/" aria-label="Aasaan home"><img class="brand-img" src="/static/mascot/aasaan-avatar.png" alt="" width="32" height="32"><span class="wordmark" aria-label="Aasaan, ஆசான், आसान"><span class="wm" lang="en">Aasaan</span><span class="wm-sep" aria-hidden="true">|</span><span class="wm" lang="ta">ஆசான்</span><span class="wm-sep" aria-hidden="true">|</span><span class="wm" lang="hi">आसान</span></span></a><div class="bar-right">${right}${themeSwitch()}</div></div>`;
  const tgl = $bar.querySelector("#themetoggle");
  if (tgl) tgl.onclick = () => { const dark = themePref() !== "dark"; setTheme(dark ? "dark" : "light"); tgl.setAttribute("aria-checked", String(dark)); };
}

function themeSwitch() {
  const dark = themePref() === "dark";
  return `<button id="themetoggle" class="tgl" type="button" role="switch" aria-checked="${dark}" aria-label="Dark mode" title="Light / dark mode"><span class="knob">
    <svg class="sun" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5" stroke="none"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8" fill="none"/></svg>
    <svg class="moon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M20.5 14.2A8.5 8.5 0 019.8 3.5a.6.6 0 00-.8-.7A9.5 9.5 0 1021.2 15a.6.6 0 00-.7-.8z"/></svg></span></button>`;
}

async function init() {
  let id = store.get("learner_id");
  if (!id) { id = (await api("learners", {})).learner_id; store.set("learner_id", id); }
  S.learner = id;
  try { S.session = (await api("session/open", { learner_id: id })).session_id; } catch { /* offline start is fine */ }
  try { const h = await api("health"); S.mode = h.mode; S.modelReady = h.model_ready; } catch { /* ignore */ }
  window.addEventListener("hashchange", route);
  route();
}

function route() {
  cleanup();
  document.body.classList.toggle("ws-open", /^#\/(?:n|nb|loading)\//.test(location.hash));
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/(?:n|nb|loading)\/(\w+)(?:\/ch\/(\d+))?/))) return workspace(m[1], m[2] ? Number(m[2]) : null);
  return home();
}

// ------------------------------------------------------------------------- home
async function home() {
  topbar();
  $app.className = "";
  let nbs = [];
  try { nbs = await api("notebooks?learner_id=" + S.learner); } catch { /* none yet */ }
  $app.innerHTML = `<div class="hero"><div class="hero-mascot" id="heromascot"></div><h1>What do you want to learn today?</h1><p>Learning made aasaan. Type a goal, paste notes or links, or attach a photo or PDF.</p></div>
    <div class="composer" id="chat">
      <div class="att-row" id="atts"></div>
      <textarea id="msg" rows="1" placeholder="${PLACEHOLDERS[0]}" aria-label="Your goal, notes or links"></textarea>
      <div class="toolbar">
        <button class="pill" id="attach" type="button" aria-label="Attach photos or PDFs">${ICON.clip}<span>Attach</span></button>
        <div class="menu-wrap"><button class="pill" id="langbtn" type="button" aria-haspopup="listbox" aria-label="Select language">
          ${ICON.globe}<span id="langlabel">${esc(LANGS[lang()] || "English")}</span>${ICON.chev}</button>
          <div class="menu" id="langmenu" role="listbox" hidden>${Object.entries(LANGS).map(([k, v]) =>
            `<button role="option" data-l="${k}" class="${k === lang() ? "on" : ""}">${esc(v)}</button>`).join("")}</div></div>
        <span class="spacer"></span>
        <button class="iconbtn" id="mic" type="button" aria-label="Speak your goal">${ICON.mic}</button>
        <button class="send" id="send" aria-label="Build my notebook" disabled>${ICON.up}</button>
        <input id="file" type="file" accept="image/*,application/pdf" multiple hidden>
      </div></div>
    <p class="help">Enter to send · Shift+Enter for a new line · drop files on the box</p><div id="err" class="err" role="alert"></div>
    ${nbs.length ? `<h2 class="section">Notebooks</h2><div class="grid">${nbs.map(nbCard).join("")}</div>` : ""}`;
  wireComposer();
  document.querySelectorAll(".nb-card").forEach((b) => b.onclick = () => {
    location.hash = `#/n/${b.dataset.id}`;
  });
}

function coverHtml(img, title) {
  return `<img src="/static/resources/${esc(img || "thumb01.jpg")}" alt="" loading="lazy"><span class="cover-scrim" aria-hidden="true"></span><span class="cover-title">${esc(title)}</span>`;
}

function nbCard(n) {
  const pct = n.progress.total ? Math.round(100 * n.progress.done / n.progress.total) : 0;
  const busy = n.status !== "complete" && n.status !== "failed";
  return `<button class="nb-card" data-id="${n.id}" data-ready="${n.open_ready ? 1 : 0}">
    <div class="nb-thumb">${coverHtml(n.cover, n.title)}</div>
    <div class="nb-title">${esc(n.title)}</div>
    <div class="meter" aria-hidden="true"><i style="width:${pct}%"></i></div>
    <div class="nb-meta"><span>${n.progress.done} of ${n.progress.total} chapters done</span>
      <span>${n.status === "failed" ? "Needs attention" : busy ? `Generating ${n.readiness}%` : timeAgo(n.last_opened)}</span></div></button>`;
}

const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

function wireComposer() {
  const hm = document.getElementById("heromascot");
  if (hm) S.hero = mountHero(hm);
  const msg = document.getElementById("msg"), send = document.getElementById("send"), box = document.getElementById("chat");
  const file = document.getElementById("file"), menu = document.getElementById("langmenu"), mic = document.getElementById("mic");
  const sync = () => {
    send.disabled = !(msg.value.trim() || S.files.length);
    msg.style.height = "auto"; msg.style.height = Math.min(msg.scrollHeight, 260) + "px";
    document.getElementById("atts").innerHTML = S.files.map((f, i) => f.type.startsWith("image/")
      ? `<div class="att img"><img src="${URL.createObjectURL(f)}" alt="${esc(f.name)}"><button class="x" data-i="${i}" aria-label="Remove ${esc(f.name)}">×</button></div>`
      : `<div class="att file">${ICON.doc}<span class="nm">${esc(f.name)}</span><span class="sz">${fmtSize(f.size)}</span><button class="x" data-i="${i}" aria-label="Remove ${esc(f.name)}">×</button></div>`).join("");
    document.querySelectorAll(".att .x").forEach((b) => b.onclick = () => { S.files.splice(Number(b.dataset.i), 1); sync(); });
  };
  const add = (list) => { S.files = [...S.files, ...list].slice(0, 6); sync(); };
  msg.oninput = sync;
  msg.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!send.disabled) go(); } };
  document.getElementById("attach").onclick = () => file.click();
  file.onchange = () => { add([...file.files]); file.value = ""; };
  ["dragenter", "dragover"].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.remove("drag"); }));
  box.addEventListener("drop", (e) => add([...e.dataTransfer.files]));
  document.getElementById("langbtn").onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  menu.querySelectorAll("button").forEach((b) => b.onclick = () => {
    store.set("lang", b.dataset.l); document.getElementById("langlabel").textContent = LANGS[b.dataset.l];
    menu.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); menu.hidden = true; msg.focus();
  });
  document.addEventListener("click", () => { menu.hidden = true; });
  let i = 0; const ph = setInterval(() => { if (!document.body.contains(msg)) return clearInterval(ph); msg.placeholder = PLACEHOLDERS[++i % PLACEHOLDERS.length]; }, 3500);
  wireMic(mic, (text) => { msg.value = (msg.value ? msg.value + " " : "") + text; sync(); msg.focus(); }, () => lang());
  send.onclick = go; sync(); msg.focus();

  async function go() {
    const f = new FormData();
    f.append("learner_id", S.learner); f.append("language", lang()); f.append("text", msg.value.trim());
    S.files.forEach((x) => f.append("files", x));
    send.disabled = true;
    try {
      const r = await api("notebooks", null, f);
      S.files = [];
      location.hash = `#/n/${r.notebook_id}`;
    } catch (e) { document.getElementById("err").textContent = e.message; send.disabled = false; }
  }
}

// microphone button: record, convert to WAV, transcribe with the voice service; typed input always stays available
function wireMic(btn, onText, getLang) {
  let rec = null;
  btn.onclick = async () => {
    if (rec) {
      const r = rec; rec = null; btn.classList.remove("rec"); btn.innerHTML = ICON.mic; btn.setAttribute("aria-label", "Speak");
      const wav = await r.stop();
      if (!wav) return toast("Could not read the recording. Please type instead.");
      const f = new FormData(); f.append("language", getLang()); f.append("audio", wav, "speech.wav");
      try {
        const out = await api("voice/stt", null, f);
        if (out.text) onText(out.text); else toast(out.message || "Voice is unavailable right now. Please type instead.");
      } catch (e) { fail(e); }
      return;
    }
    if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Voice input is not supported in this browser. Please type.");
    try { rec = await startRecording(); btn.classList.add("rec"); btn.innerHTML = ICON.stop; btn.setAttribute("aria-label", "Stop recording"); }
    catch { toast("Microphone permission was denied. Please type instead."); }
  };
}

// -------------------------------------------------------------------- workspace
// After submit the student lands here at once: Course on the left (the whiteboard: it fills in live), chat on the right.
const WS_DEFAULT = { w: 65, closed: false };
// first visit on a mid-size screen (861 to 1023 px): the chat starts collapsed, the whiteboard gets the room
const wsDefault = () => ({ ...WS_DEFAULT, closed: innerWidth > 860 && innerWidth < 1024 });
const wsPrefs = (nid) => { try { return { ...wsDefault(), ...JSON.parse(store.get("ws:" + nid) || "{}") }; } catch { return wsDefault(); } };
const wsSave = (nid, p) => store.set("ws:" + nid, JSON.stringify(p));   // UI preference only: pane width and collapsed state

async function workspace(nid, cid) {
  S.nb = null; S.chapter = null; S.quiz = null; S.nid = nid;
  $app.className = "ws-host";
  topbar(`<button class="linkbtn" id="finish">Finish for today</button>`);
  const prefs = wsPrefs(nid);
  $app.innerHTML = `<div class="ws" id="ws" data-tab="course" data-view="${store.get("view:" + nid) || "course"}">
    <section class="board" id="board" aria-label="Course">
      <div class="board-top">
        <div class="vseg" role="group" aria-label="Sections"><button id="vt-course" aria-pressed="true">Course</button><button id="vt-res" aria-pressed="false">Resources</button></div>
        <button class="focusbtn" id="focus" type="button" aria-pressed="false" aria-keyshortcuts="Control+\\" title="Focus: hide the chat (Ctrl+\\)">${ICON.expand}<span>Focus</span></button>
      </div>
      <div class="board-scroll" id="boardscroll">
      <div id="courseview">
        <div class="nbhead"><div class="cover" id="cover"></div><div><h1 id="nbtitle"><span class="bar-s w60"></span></h1><p class="muted" id="nbdesc"></p><div class="scopechips" id="scopechips" aria-label="Course scope"></div></div><div class="ready" id="ready"></div></div>
        <div id="note"></div>
        <div class="nb"><button class="chaps-toggle" id="chaps" aria-expanded="false">${ICON.list} Chapters</button>
          <aside class="side" id="side" aria-label="Chapters"></aside><section class="main" id="main" aria-live="polite"></section></div>
      </div>
      <div id="resview" hidden></div>
      </div>
    </section>
    <div class="divider" id="divider" role="separator" aria-orientation="vertical" aria-label="Resize the chat panel" tabindex="0" aria-valuemin="30" aria-valuemax="80" aria-valuenow="${prefs.w}"></div>
    <aside class="chatpane" id="chatpane" aria-label="Chat with Aasaan">
      <header class="chat-h"><span class="chat-name">${logoSvg("head-logo")} Aasaan</span>
        <div class="vseg chat-tabs" role="group" aria-label="Chat or activity"><button id="ct-chat" aria-pressed="true" type="button">Chat</button><button id="ct-act" aria-pressed="false" type="button">Activity</button></div>
        <button class="iconbtn" id="dissolve" aria-label="Collapse chat" aria-keyshortcuts="Control+\\" title="Collapse chat (Ctrl+\\)">${ICON.right}</button></header>
      <div class="activity" id="activity" hidden aria-label="Activity"></div>
      <div class="msgs" id="msgs" role="log" aria-live="polite" aria-label="Conversation"></div>
      <button class="jump" id="jump" type="button" hidden>New messages ↓</button>
      <div class="chat-compose" id="chatcompose">
        <div class="suggest" id="suggest" aria-label="Suggestions"></div>
        <div class="moderow" id="moderow" role="group" aria-label="How should I answer?"><button type="button" data-m="answer" aria-pressed="true">Just tell me</button><button type="button" data-m="guide" aria-pressed="false">Guide me</button></div>
        <div class="ctxrow" id="ctxrow"></div><div class="att-row" id="chatatts"></div>
        <div class="chat-input"><textarea id="chatinput" rows="2" placeholder="Ask about your course, or add a file or link…" aria-label="Message Aasaan"></textarea>
          <div class="toolbar"><button class="iconbtn" id="chatattach" type="button" aria-label="Attach files">${ICON.clip}</button>
            <button class="iconbtn" id="chatmic" type="button" aria-label="Speak your message">${ICON.mic}</button><span class="spacer"></span>
            <button class="send" id="chatsend" type="button" aria-label="Send" disabled>${ICON.up}</button></div></div>
        <input id="chatfile" type="file" aria-label="Choose files to send" accept="image/*,application/pdf,.txt,.md,text/plain" multiple hidden>
      </div>
    </aside>
    <button class="chatfab" id="chatfab" type="button" aria-label="Open chat with Aasaan" aria-keyshortcuts="Control+\\" title="Open chat (Ctrl+\\)" hidden>${logoSvg()}<i class="dot" id="chatdot" hidden></i></button>
    <nav class="tabbar" id="tabbar" aria-label="Sections"><button data-tab="course" aria-current="true">Course</button><button data-tab="resources">Resources</button><button data-tab="chat">Chat<i class="dot" id="tabdot" hidden></i></button></nav>
  </div>`;
  wirePanes(nid);
  document.getElementById("finish").onclick = finishSession;
  document.getElementById("chaps").onclick = (e) => { const o = document.querySelector(".nb").classList.toggle("side-open"); e.currentTarget.setAttribute("aria-expanded", o); };
  paintShell();
  try { S.nb = await api("notebooks/" + nid); } catch (e) { fail(e); location.hash = "#/"; return; }
  renderHead(); renderSide(); settleCourse(cid);
  renderScope(nid);
  S.res = createResources({ nid, api, toast, openChapter: (id) => { document.getElementById("vt-course").click(); loadChapter(id, false, false); }, isVisible: () => !document.getElementById("resview").hidden });
  S.res.refresh(true);
  S.chat = createChat({
    nid, learner: S.learner, api, toast, getChapterId: () => S.chapter?.id || null, getChapterTitle: () => S.chapter?.title || "", getSources: () => S.nb?.sources || [],
    isOpen: () => !S.panes.closed() || matchMedia("(max-width: 860px)").matches && document.getElementById("ws")?.dataset.tab === "chat",
    notifyUnread: () => { document.getElementById("chatdot").hidden = false; document.getElementById("tabdot").hidden = false; },
    openChat: () => S.panes.open(), onChip: chatChip, afterEdit: () => api("notebooks/" + nid).then(onSnapshot).catch(() => {}), wireMic: (btn, onText) => wireMic(btn, onText, () => lang()),
  });
  S.activity = createActivity({ nid, api, getChapters: () => S.nb?.chapters, openChapter: (id) => { document.getElementById("vt-course")?.click(); loadChapter(id, false, false); } });
  const pane = document.getElementById("chatpane");
  const showTab = (t) => { pane.dataset.tab = t; document.getElementById("ct-chat").setAttribute("aria-pressed", String(t === "chat")); document.getElementById("ct-act").setAttribute("aria-pressed", String(t === "activity")); if (t === "activity") S.activity.draw(); };
  document.getElementById("ct-chat").onclick = () => showTab("chat"); document.getElementById("ct-act").onclick = () => showTab("activity");
  api(`notebooks/${nid}/companion?learner_id=${S.learner}`).then((c) => { S.companion = c.message; S.review = c.review; renderNote(); }).catch(() => {});
  connect(nid);
}

// what the board shows before the outline exists: placeholders, never a blank page
function paintShell() {
  document.getElementById("side").innerHTML = `<div class="seg" aria-hidden="true"><button aria-selected="true">Roadmap</button><button>Graph</button></div>` +
    Array.from({ length: 6 }, () => `<div class="rm-skel"><span class="bar-s w90"></span><span class="bar-s w60"></span></div>`).join("");
  document.getElementById("main").innerHTML = `<div class="skel" role="status"><p class="muted"><span class="spin"></span>Starting your notebook…</p>
    <div class="bar-s w80"></div><div class="bar-s"></div><div class="bar-s w60"></div><div class="bar-s w90"></div></div>`;
}

const PHASE_TEXT = { outlining: "Reading what you gave me and planning the chapters…", generating: "Writing the first chapter…" };

// choose what the main area shows for the current snapshot (called when the outline appears or the notebook changes)
function settleCourse(cid) {
  const nb = S.nb, main = document.getElementById("main"); if (!main || !nb) return;
  if (nb.status === "failed" && !nb.open_ready) {
    main.innerHTML = `<div class="skel failed" role="alert"><p><strong>Something went wrong while preparing your notebook.</strong></p><p class="muted">${esc(nb.error || "Please try again.")}</p>
      <button class="btn" id="retry">Try again</button></div>`;
    document.getElementById("retry").onclick = async () => { try { await api(`notebooks/${nb.id}/retry`, {}); paintShell(); } catch (e) { fail(e); } };
    return;
  }
  if (!nb.chapters.length) {
    main.innerHTML = `<div class="skel" role="status"><p class="muted"><span class="spin"></span>${PHASE_TEXT[nb.status] || "Preparing your notebook…"}</p>
      <div class="bar-s w80"></div><div class="bar-s"></div><div class="bar-s w60"></div><div class="bar-s w90"></div></div>`;
    return;
  }
  if (!S.chapter) { const first = cid || nb.next_chapter || nb.chapters[0].id; loadChapter(first, true, true); }
}

function wirePanes(nid) {
  const ws = document.getElementById("ws"), div = document.getElementById("divider"), pane = document.getElementById("chatpane"), chatBtn = document.getElementById("focus"), fab = document.getElementById("chatfab");
  const prefs = wsPrefs(nid);
  const apply = () => {
    ws.style.setProperty("--board", prefs.w + "%"); ws.style.setProperty("--chatw", (100 - prefs.w) + "%");
    ws.classList.toggle("chat-closed", prefs.closed);
    pane.inert = prefs.closed && !matchMedia("(max-width: 860px)").matches; chatBtn.setAttribute("aria-pressed", String(prefs.closed)); chatBtn.querySelector("span").textContent = prefs.closed ? "Show chat" : "Focus";
    fab.hidden = !prefs.closed || matchMedia("(max-width: 860px)").matches;
    div.hidden = prefs.closed; div.setAttribute("aria-valuenow", String(Math.round(prefs.w)));
  };
  S.panes = { closed: () => prefs.closed, open: () => { if (matchMedia("(max-width: 860px)").matches) document.querySelector('#tabbar [data-tab="chat"]').click(); else setClosed(false); } };
  const setClosed = (c) => { prefs.closed = c; wsSave(nid, prefs); apply(); if (c) fab.focus(); else { document.getElementById("chatdot").hidden = true; (document.getElementById("chatinput") || pane).focus?.(); } };
  const setW = (w) => { prefs.w = Math.max(30, Math.min(80, w)); wsSave(nid, prefs); apply(); };
  document.getElementById("dissolve").onclick = () => setClosed(true);
  chatBtn.onclick = () => setClosed(!prefs.closed); fab.onclick = () => setClosed(false);
  div.onpointerdown = (e) => {
    e.preventDefault(); div.setPointerCapture(e.pointerId); ws.classList.add("dragging");
    const move = (ev) => { const r = ws.getBoundingClientRect(); setW(((ev.clientX - r.left) / r.width) * 100); };
    const up = () => { ws.classList.remove("dragging"); div.removeEventListener("pointermove", move); div.removeEventListener("pointerup", up); };
    div.addEventListener("pointermove", move); div.addEventListener("pointerup", up);
  };
  div.onkeydown = (e) => {
    const step = e.shiftKey ? 10 : 3;
    if (e.key === "ArrowLeft") { e.preventDefault(); setW(prefs.w - step); } else if (e.key === "ArrowRight") { e.preventDefault(); setW(prefs.w + step); }
    else if (e.key === "Home") { e.preventDefault(); setW(30); } else if (e.key === "End") { e.preventDefault(); setW(80); }
  };
  const onKey = (e) => { if ((e.ctrlKey || e.metaKey) && (e.key === "\\" || e.key === ".")) { e.preventDefault(); setClosed(!prefs.closed); } };
  if (S.keyHandler) document.removeEventListener("keydown", S.keyHandler);
  S.keyHandler = onKey; document.addEventListener("keydown", onKey);
  // Course | Resources
  const view = (v) => {
    ws.dataset.view = v; store.set("view:" + nid, v);
    document.getElementById("courseview").hidden = v !== "course"; document.getElementById("resview").hidden = v !== "resources";
    document.getElementById("vt-course").setAttribute("aria-pressed", String(v === "course"));
    if (v === "resources") S.res?.refresh(true); document.getElementById("vt-res").setAttribute("aria-pressed", String(v === "resources"));
  };
  document.getElementById("vt-course").onclick = () => view("course"); document.getElementById("vt-res").onclick = () => { view("resources"); S.res?.refresh(true); };
  view(ws.dataset.view === "resources" ? "resources" : "course");
  // phones: one pane at a time
  document.querySelectorAll("#tabbar button").forEach((b) => b.onclick = () => {
    const t = b.dataset.tab; ws.dataset.tab = t;
    document.querySelectorAll("#tabbar button").forEach((x) => x.toggleAttribute("aria-current", x === b));
    if (t === "course" || t === "resources") view(t);
    if (t === "chat") { document.getElementById("tabdot").hidden = true; document.getElementById("chatdot").hidden = true; }
  });
  apply();
}

// after Apply or Undo: the affected chapters glow in the roadmap for about two seconds (not at all with reduced motion)
function flashEdits(nb) {
  const h = nb.highlight; if (!h || h.key === S.lastHl) return;
  S.lastHl = h.key;
  requestAnimationFrame(() => {
    h.ids.forEach((id) => { const el = document.querySelector(`.rm[data-id="${id}"], .gnode[data-id="${id}"]`); if (el) { el.classList.add("flashrow"); setTimeout(() => el.classList.remove("flashrow"), 2200); } });
  });
}

function chatChip(c) {
  const ws = document.getElementById("ws");
  if (matchMedia("(max-width: 860px)").matches) document.querySelector('#tabbar [data-tab="course"]').click();
  if (c.kind === "chapter" && !S.nb.chapters.some((x) => x.id === Number(c.id))) { toast("That chapter is no longer in the course."); return; }
  if (c.kind === "chapter") {
    document.getElementById("vt-course").click();
    loadChapter(Number(c.id), false, false).then(() => { if (c.quiz) document.getElementById("quizblk")?.scrollIntoView({ block: "center", behavior: "smooth" }); });
  }
  else if (c.kind === "resource") {
    document.getElementById("vt-res").click();
    S.res.refresh(true).then(() => {
      const item = document.querySelector(`#resview [data-rid="${c.id}"]`);
      if (item) { item.scrollIntoView({ block: "center", behavior: "smooth" }); item.classList.add("flash"); setTimeout(() => item.classList.remove("flash"), 2200); }
    });
  } else if (c.kind === "link" && c.url) window.open(c.url, "_blank", "noopener");
}

function setReconnecting(on) {
  let el = document.getElementById("reconnecting-badge");
  if (!el && on) {
    el = document.createElement("div");
    el.id = "reconnecting-badge";
    el.className = "reconnecting-badge";
    el.textContent = "Reconnecting…";
    document.body.appendChild(el);
  }
  if (el) el.classList.toggle("on", on);
}

function connect(nid) {
  S.lastEventId = null;
  let reconnectTimer = null;
  const apply = (nb) => onSnapshot(nb);
  const poll = () => {
    if (!S.poll) {
      S.poll = setInterval(async () => {
        try { apply(await api("notebooks/" + nid)); } catch { /* retry */ }
      }, 3000);
    }
  };

  const clearReconnect = () => {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    setReconnecting(false);
  };

  if (window.EventSource) {
    S.es = new EventSource(`/api/notebooks/${nid}/stream`);
    S.es.onopen = () => {
      clearReconnect();
      if (S.poll) { clearInterval(S.poll); S.poll = null; }
    };
    S.es.onmessage = (e) => {
      clearReconnect();
      if (S.poll) { clearInterval(S.poll); S.poll = null; }
      if (e.lastEventId) {
        const id = parseInt(e.lastEventId, 10);
        if (!isNaN(id)) {
          if (S.lastEventId !== null && id <= S.lastEventId) return;
          S.lastEventId = id;
        }
      }
      try { apply(JSON.parse(e.data)); } catch { /* ignore malformed */ }
    };
    S.es.addEventListener("resync", async () => {
      clearReconnect();
      try {
        const nb = await api("notebooks/" + nid);
        apply(nb);
      } catch { /* will catch up */ }
    });
    S.es.onerror = () => {
      // Reconnecting state is standard EventSource behavior on server close (every ~200s).
      // Only show visual indicator if reconnecting takes longer than 3.5 seconds.
      if (S.es && S.es.readyState === EventSource.CONNECTING) {
        if (!reconnectTimer) {
          reconnectTimer = setTimeout(() => setReconnecting(true), 3500);
        }
      } else if (S.es && S.es.readyState === EventSource.CLOSED) {
        clearReconnect();
        poll();
      }
    };
  } else {
    poll();
  }
}

function onSnapshot(nb) {
  const cur = S.chapter, hadChapters = !!(S.nb && S.nb.chapters.length);
  S.nb = nb; renderHead(); renderSide(); S.chat?.onSnapshot(nb); S.res?.onSnapshot(); S.activity?.refresh(nb.decision_seq);
  if (!S.chapter || !hadChapters || nb.status === "failed") settleCourse();
  flashEdits(nb);
  const crumb = document.querySelector(".crumb");   // the chapter count can change under the reader (an applied or undone edit)
  if (crumb && cur) { const i = nb.chapters.findIndex((c) => c.id === cur.id); if (i >= 0) crumb.textContent = `Chapter ${i + 1} of ${nb.chapters.length}${cur.kind === "review" ? " · review" : ""}`; }
  const now = cur && nb.chapters.find((c) => c.id === cur.id);
  if (cur && !now) { S.chapter = null; settleCourse(); return; }   // the chapter being read was removed (or an add was undone)
  if (!now) return;
  const becameReady = cur.status !== "ready" && now.status === "ready";
  const statusChanged = cur.status !== now.status && now.status !== "ready";
  if (becameReady) { if (!(S.quiz && S.quiz.cid === cur.id)) loadChapter(cur.id, true, true); }
  else if (statusChanged) { cur.status = now.status; cur.error = now.error; renderChapter(); }
}

function renderNote() {
  const n = document.getElementById("note"); if (!n) return;
  const rv = S.review && S.review.available ? `<button class="btn" id="startreview">Start ${S.review.minutes}-minute review</button>` : "";
  n.innerHTML = S.companion ? `<div class="notice"><span>${esc(S.companion)} ${rv}</span><button class="x2" aria-label="Dismiss">×</button></div>` : "";
  const x = n.querySelector(".x2"); if (x) x.onclick = () => { S.companion = ""; renderNote(); };
  const sr = n.querySelector("#startreview"); if (sr) sr.onclick = startReview;
}

// scope chips: depth and level. Changing one is a PROPOSAL in the chat (what would be rewritten, how long); nothing changes until it is applied
async function renderScope(nid) {
  const box = document.getElementById("scopechips"); if (!box) return;
  let sc; try { sc = await api(`notebooks/${nid}/scope`); } catch { return; }
  const sel = (name, label, opts, cur) => `<label class="scopechip"><span class="muted">${label}</span><select name="${name}" aria-label="${label}">${opts.map((o) => `<option value="${o}" ${o === cur ? "selected" : ""}>${o[0].toUpperCase() + o.slice(1)}</option>`).join("")}</select></label>`;
  box.innerHTML = sel("depth", "Depth", sc.depths, sc.depth) + sel("level", "Level", sc.levels, sc.level);
  box.querySelectorAll("select").forEach((el) => el.onchange = async () => {
    const f = new FormData(); f.append(el.name, el.value);
    try {
      const r = await api(`notebooks/${nid}/scope`, null, f);
      toast(r.proposal ? "I made a suggestion in the chat. Nothing changes until you apply it." : "That is already the setting.");
      if (r.proposal) S.panes?.open();
    } catch (e) { fail(e); }
    el.value = sc[el.name];                                   // the setting only moves when the proposal is applied
  });
}

function renderHead() {
  const nb = S.nb; if (!document.getElementById("nbtitle")) return;
  document.getElementById("nbtitle").textContent = nb.title;
  const cv = document.getElementById("cover");   // one of the four thumbnails, with the title on top
  if (cv) cv.innerHTML = coverHtml(nb.cover, nb.title);
  document.getElementById("nbdesc").innerHTML = esc(nb.description || "") + ((nb.sources || []).length
    ? `<span class="srcs">Based on: ${nb.sources.map((x) => x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc((x.title || x.url).slice(0, 40))}</a>` : `<span>${esc(x.title.slice(0, 40))}</span>`).join(" · ")}</span>` : "")
    + ((nb.warnings || []).length ? `<span class="srcs">${nb.warnings.map(esc).join(" · ")}</span>` : "");

  const yp = nb.your_progress, pr = nb.preparing;
  document.getElementById("ready").innerHTML = `<span>Your progress</span>
    <div class="meter" role="progressbar" aria-label="Your progress" aria-valuenow="${yp.pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${yp.pct}%"></i></div>
    <span class="muted">${yp.done} of ${yp.total} chapters done</span>
    ${pr.complete ? "" : `<span class="prep">Preparing your notebook</span>
    <div class="meter prep" role="progressbar" aria-label="Preparing your notebook" aria-valuenow="${pr.pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pr.pct}%"></i></div>
    <span class="muted">${pr.ready} of ${pr.total} chapters ready</span>`}`;
}

function chip(c) {
  // Two separate things: how far the student is (learning) and whether the chapter is built yet (generation, shown only until it is ready).
  if (c.generation === "failed") return `<span class="chip s-failed">Failed</span>`;
  if (c.generation !== "ready") return `<span class="chip gen">${GEN_TEXT[c.generation] || "Preparing"}${c.generation === "queued" ? "" : "…"}</span>`;
  return `<span class="chip s-${c.learning}">${LEARN_TEXT[c.learning]}</span>`;
}

function renderSide() {
  const side = document.getElementById("side"); if (!side) return;
  const nb = S.nb, cur = S.chapter?.id;
  if (!nb.chapters.length) return;   // placeholders from paintShell stay until the outline arrives
  const body = S.side === "graph"
    ? `<div class="graph-wrap">${renderGraph(nb, cur)}</div><div class="legend">${legend()}</div>`
    : `<ol class="roadmap">${nb.chapters.map((c, i) => `<li><button class="rm n-${c.state}${c.id === cur ? " cur" : ""}" data-id="${c.id}">
        <span class="num">${i + 1}</span><span class="rt">${esc(c.title)}${c.kind === "review" ? ` <em>review</em>` : ""}</span><span class="rchip">${chip(c)}</span></button></li>`).join("")}</ol>`;
  document.querySelector(".nb")?.classList.toggle("gmode", S.side === "graph");
  side.innerHTML = `<div class="seg" role="tablist"><button role="tab" id="t-roadmap" aria-selected="${S.side !== "graph"}">${ICON.list} Roadmap</button>
      <button role="tab" id="t-graph" aria-selected="${S.side === "graph"}">${ICON.graph} Graph</button></div>${body}`;
  side.querySelector("#t-roadmap").onclick = () => { S.side = "roadmap"; store.set("side", "roadmap"); renderSide(); };
  side.querySelector("#t-graph").onclick = () => { S.side = "graph"; store.set("side", "graph"); renderSide(); };
  const open = (id) => { document.querySelector(".nb")?.classList.remove("side-open"); loadChapter(Number(id)); };
  side.querySelectorAll(".rm").forEach((b) => b.onclick = () => open(b.dataset.id));
  side.querySelectorAll(".gnode").forEach((g) => { g.onclick = () => open(g.dataset.id); g.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(g.dataset.id); } }; });
}

// ---------------------------------------------------------------------- chapter
async function loadChapter(cid, keepScroll = false, auto = false) {
  const main = document.getElementById("main"); if (!main) return;
  try {
    const ch = await api("chapters/" + cid);
    S.chapter = ch; S.quiz = null; S.chat?.setFocus(ch.title);
    history.replaceState(null, "", `#/n/${ch.notebook_id}/ch/${cid}`);
    if (ch.status !== "ready") api(`chapters/${cid}/prioritize`, {}).catch(() => {});  // jump the queue
    else if (!auto) api(`chapters/${cid}/open`, {}).catch(() => {});   // landing on a chapter automatically is not "starting" it
    renderSide(); renderChapter();
    if (!keepScroll) document.getElementById("boardscroll")?.scrollTo({ top: document.querySelector(".nbhead")?.offsetHeight || 0, behavior: "smooth" });
  } catch (e) { fail(e); }
}

function renderChapter() {
  const main = document.getElementById("main"), ch = S.chapter; if (!main || !ch) return;
  const order = S.nb.chapters, i = order.findIndex((c) => c.id === ch.id), prev = order[i - 1], next = order[i + 1];
  const head = `<header class="ch-head"><span class="crumb">Chapter ${i + 1} of ${order.length}${ch.kind === "review" ? " · review" : ""}</span>
    <h2>${esc(ch.title)}</h2>${ch.summary ? `<p class="sum">${esc(ch.summary)}</p>` : ""}
    ${ch.objectives?.length ? `<ul class="objs">${ch.objectives.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>` : ""}</header>`;
  const nav = `<nav class="ch-nav">${prev ? `<button class="btn ghost" data-go="${prev.id}">${ICON.left} ${esc(prev.title.slice(0, 28))}</button>` : "<span></span>"}
    ${next ? `<button class="btn ghost" data-go="${next.id}">${esc(next.title.slice(0, 28))} ${ICON.right}</button>` : ""}</nav>`;
  if (ch.status !== "ready") { main.innerHTML = head + skeleton(ch) + nav; wireNav(main); return; }
  const issues = ch.review_issues || [];
  const cl = ch.claims;
  const verifiedLine = cl && cl.total ? `<p class="claimline ${ch.unverified ? "partial" : "ok"}" role="status">${ch.unverified ? `Checked ${cl.verified} of ${cl.total} claims. The rest could not be verified.` : `Verified ${cl.verified} of ${cl.total} claims.`}</p>` : "";
  const notices = [verifiedLine, ch.unverified ? `<div class="flag"><p>A few claims couldn't be fully verified.</p>
      <div class="flag-actions">${issues.length ? `<button class="linkbtn" id="seeclaims" aria-expanded="false">See which claims</button>` : ""}<button class="linkbtn" id="recheck">Re-check</button></div>
      <ul class="claims" id="claims" hidden>${issues.map((i) => `<li>${esc(i.claim)} <span class="muted">(${esc(i.source_status)})</span></li>`).join("")}</ul></div>` : "",
                   !ch.grounded ? `<p class="flag">Written from general knowledge; no source material was supplied.</p>` : "",
                   ...(ch.curator_notes || []).map((n) => `<p class="flag">${esc(n)}</p>`)].join("");
  main.innerHTML = head + notices + ch.blocks.map(blockWithAsk).join("") + doubtHtml() + nav;
  wireNav(main); wireBlocks(main); wireClaims(main); wireAsk(main); wireFlow(main);
}

function skeleton(ch) {
  if (ch.status === "failed") return `<div class="skel failed"><p><strong>This chapter could not be built.</strong></p><p class="muted">${esc(ch.error || "Unknown error")}</p>
    <button class="btn" id="retry-ch">Try again</button></div>`;
  return `<div class="skel" role="status"><p class="muted"><span class="spin"></span>${esc(({ queued: "Queued", drafting: "Writing notes", checking: "Checking facts" })[ch.status] || "Queued")}. This chapter fills in on its own; you can read others meanwhile.</p>
    <div class="bar-s w80"></div><div class="bar-s"></div><div class="bar-s w60"></div><div class="bar-s w90"></div></div>`;
}

const ASKABLE = { notes: "Notes", slides: "Slides", summary: "Summary", visual: "Diagram", video: "Video", image: "Image", link: "Link" };
function blockWithAsk(b) {
  let h = blockHtml(b);
  if (!ASKABLE[b.type] || !h) return h;
  const label = ASKABLE[b.type] + (b.type === "visual" || b.type === "video" ? ` · ${(b.content.caption || b.content.title || "").slice(0, 30)}` : "");
  h = h.replace("<section", `<section data-bid="${b.id}"`);
  return h.replace(/<\/section>\s*$/, `<button class="askbtn" type="button" data-ask="${b.id}" data-label="${esc(label)}" aria-label="Ask about this ${esc(ASKABLE[b.type].toLowerCase())}">Ask about this</button></section>`);
}
function wireAsk(main) {
  main.querySelectorAll(".askbtn").forEach((b) => b.onclick = () => S.chat?.ask({ id: Number(b.dataset.ask), label: b.dataset.label }));
}

function blockHtml(b) {
  const c = b.content;
  switch (b.type) {
    case "notes": return `<section class="blk" data-b="${b.id}"><div class="blk-h"><h3>Notes</h3><button class="linkbtn" data-edit="${b.id}">${ICON.edit} Edit</button></div>
      <div class="md" id="md-${b.id}">${renderMarkdown(c.markdown)}</div></section>`;
    case "slides": return `<section class="blk" data-b="${b.id}"><div class="blk-h"><h3>Slides</h3></div><div class="slides" id="sl-${b.id}" tabindex="0" aria-label="Slides"></div></section>`;
    case "video": return `<section class="blk"><figure class="video"><div class="ratio"><iframe src="https://www.youtube.com/embed/${encodeURIComponent(c.video_id)}${Number(c.start) > 0 ? "?start=" + Math.floor(Number(c.start)) : ""}" title="${esc(c.title)}"
      loading="lazy" allow="accelerometer; encrypted-media; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
      <figcaption>${esc(c.title)} <span class="muted">· ${esc(c.channel)}</span></figcaption></figure></section>`;
    case "image": { const a = c.attribution || {}; return `<section class="blk"><figure class="img"><img src="${esc(c.url)}" alt="${esc(c.title)}" loading="lazy" referrerpolicy="no-referrer">
      <figcaption>${esc(c.title)} · ${esc(a.author || "Unknown")} · ${esc(a.license || "")} · <a href="${esc(a.page_url || c.url)}" target="_blank" rel="noopener noreferrer">${esc(a.source || "source")}</a></figcaption></figure></section>`; }
    case "visual": {
      const a = c.attribution, p = c.payload || {};
      const lv = p.levels ? `<div class="vz-tabs" role="group" aria-label="Detail level">${["high", "detailed"].filter((k) => p.levels[k]).map((k) => `<button type="button" data-lv="${k}" aria-pressed="${k === p.level}">${k === "high" ? "High level" : "Detailed"}</button>`).join("")}</div>` : "";
      const codeBox = p.code ? `<details class="vz-codewrap"><summary>Show the code</summary><pre class="vz-code">${p.code.split("\n").map((l, i) => `<span class="cl" data-l="${i + 1}">${esc(l) || " "}</span>`).join("\n")}</pre></details>` : "";
      const body = p.svg ? `${lv}<div class="vz-wrap" role="img" aria-label="${esc(c.alt_text)}">${p.svg}</div>${codeBox}`
        : `<img src="${esc(p.url)}" alt="${esc(c.alt_text)}" loading="lazy" referrerpolicy="no-referrer">`;
      const credit = a ? ` · ${esc(a.author || "Unknown")} · ${esc(a.license || "")} · <a href="${esc(a.page_url || p.url)}" target="_blank" rel="noopener noreferrer">${esc(a.source || "source")}</a>` : "";
      return `<section class="blk"><figure class="img visual">${body}<figcaption>${esc(c.caption)}${credit}</figcaption></figure></section>`; }
    case "practice": return `<section class="blk"><div class="blk-h"><h3>Practice</h3></div><p class="muted">${esc(c.note || "")}</p>
      <ul class="plist">${(c.items || []).map((i) => `<li><a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.title)}</a> <span class="muted">${esc(i.detail || "")}</span></li>`).join("")}</ul><p class="muted">Source: ${esc(c.source || "")}</p></section>`;
    case "link": return `<section class="blk"><a class="linkcard" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer"><span class="lt">${esc(c.title)}</span>
      <span class="ls">${esc(c.source)} ${ICON.ext}</span><span class="lr">${esc(c.reason)}</span></a></section>`;
    case "quiz": return `<section class="blk" id="quizblk"><div class="blk-h"><h3>Check yourself</h3></div><div id="quiz"></div></section>`;
    case "summary": return `<section class="blk summary"><h3>Summary</h3><p>${esc(c.text)}</p>${(c.key_points || []).length ? `<ul>${c.key_points.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}</section>`;
    default: return "";
  }
}

function doubtHtml() {
  return `<section class="blk"><div class="blk-h"><h3>Ask a doubt</h3></div><div class="doubt-row"><input id="dq" placeholder="Type your doubt and press Enter" aria-label="Your doubt">
    <button class="iconbtn" id="dmic" aria-label="Ask by voice">${ICON.mic}</button></div><div id="dans"></div></section>`;
}

// code flowcharts: High level / Detailed toggle, and a node lights up the code lines it stands for
function wireFlow(main) {
  main.querySelectorAll(".vz-tabs").forEach((tabs) => {
    const sec = tabs.closest("section"), blk = S.chapter.blocks.find((b) => String(b.id) === sec?.dataset.bid);
    const pl = blk?.content?.payload; if (!pl?.levels) return;
    const wrap = sec.querySelector(".vz-wrap"); let level = pl.level;
    const clear = () => sec.querySelectorAll(".cl.hl").forEach((l) => l.classList.remove("hl"));
    const light = (id) => { clear(); const r = pl.levels[level].lines[id]; if (!r) return; sec.querySelector(".vz-codewrap")?.setAttribute("open", ""); sec.querySelectorAll(".cl").forEach((l) => { const n = Number(l.dataset.l); if (n >= r[0] && n <= r[1]) l.classList.add("hl"); }); };
    const wireNodes = () => wrap.querySelectorAll(".fn").forEach((g) => { g.tabIndex = 0; g.onmouseenter = g.onfocus = () => light(g.dataset.n); g.onmouseleave = g.onblur = clear; });
    tabs.querySelectorAll("button").forEach((b) => b.onclick = () => { level = b.dataset.lv; wrap.innerHTML = pl.levels[level].svg; tabs.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); clear(); wireNodes(); });
    wireNodes();
  });
}

function wireNav(main) {
  main.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => loadChapter(Number(b.dataset.go)));
  const r = main.querySelector("#retry-ch");
  if (r) r.onclick = async () => { try { await api(`chapters/${S.chapter.id}/retry`, {}); toast("Trying again..."); } catch (e) { fail(e); } };
}

function wireClaims(main) {
  const see = main.querySelector("#seeclaims"), list = main.querySelector("#claims"), re = main.querySelector("#recheck");
  if (see && list) see.onclick = () => { list.hidden = !list.hidden; see.setAttribute("aria-expanded", String(!list.hidden)); };
  if (re) re.onclick = async () => {
    re.disabled = true; re.textContent = "Re-checking…";
    try {
      const r = await api(`chapters/${S.chapter.id}/recheck`, {});
      toast(r.verified ? "All claims check out now." : "Some claims still couldn't be verified.");
      await loadChapter(S.chapter.id, true, true);
    } catch (e) { fail(e); re.disabled = false; re.textContent = "Re-check"; }
  };
}

function wireBlocks(main) {
  main.querySelectorAll("[data-edit]").forEach((btn) => btn.onclick = () => editNotes(Number(btn.dataset.edit)));
  S.chapter.blocks.filter((b) => b.type === "slides").forEach((b) => renderSlides(b));
  const quiz = S.chapter.blocks.find((b) => b.type === "quiz");
  if (quiz) startQuiz(quiz.content.questions);
  const dq = main.querySelector("#dq");
  if (dq) dq.onkeydown = (e) => { if (e.key === "Enter" && dq.value.trim()) sendDoubt(dq.value.trim()); };
  const dm = main.querySelector("#dmic");
  if (dm) wireMic(dm, (t) => { dq.value = t; sendDoubt(t); }, () => S.nb.language || lang());
}

// --- notes editing (markdown textarea with save/cancel)
function editNotes(bid) {
  const b = S.chapter.blocks.find((x) => x.id === bid), el = document.getElementById("md-" + bid);
  if (!b || !el) return;
  el.innerHTML = `<textarea class="md-edit" id="ed-${bid}" aria-label="Edit notes (markdown)">${esc(b.content.markdown)}</textarea>
    <div class="actions"><button class="btn" id="save-${bid}">Save</button><button class="btn ghost" id="cancel-${bid}">Cancel</button></div>`;
  const ta = el.querySelector("textarea"); ta.style.height = Math.min(500, ta.scrollHeight + 10) + "px"; ta.focus();
  el.querySelector(`#cancel-${bid}`).onclick = () => { el.innerHTML = renderMarkdown(b.content.markdown); };
  el.querySelector(`#save-${bid}`).onclick = async () => {
    try { await api(`blocks/${bid}`, { markdown: ta.value }, undefined, "PUT"); b.content.markdown = ta.value; el.innerHTML = renderMarkdown(ta.value); toast("Saved"); }
    catch (e) { fail(e); }
  };
}

// --- slides carousel
function renderSlides(b) {
  const el = document.getElementById("sl-" + b.id); if (!el) return;
  const cards = b.content.cards, i = Math.min(S.slide[b.id] || 0, cards.length - 1), c = cards[i];
  const TYPE = { title: "Objectives", concept: "Concept", example: "Example", diagram: "Diagram", comparison: "Comparison", code: "Code", mistake: "Common mistake", recap: "Recap", check: "Check yourself" };
  el.innerHTML = `<div class="slide ${c.type ? "t-" + c.type : ""}"><span class="badge">Slide ${i + 1} of ${cards.length}${c.type && TYPE[c.type] ? ` · ${TYPE[c.type]}` : ""}</span><h4>${esc(c.title)}</h4>
    <ul>${c.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>${c.note ? `<details class="slide-note"><summary>Speaker notes</summary><p>${esc(c.note)}</p></details>` : ""}</div>
    <div class="slide-nav"><button class="iconbtn" data-full aria-label="Full screen" title="Full screen">${ICON.expand}</button><button class="iconbtn" data-d="-1" aria-label="Previous slide" ${i === 0 ? "disabled" : ""}>${ICON.left}</button>
      <span class="dotrow">${cards.map((_, k) => `<i class="${k === i ? "on" : ""}"></i>`).join("")}</span>
      <button class="iconbtn" data-d="1" aria-label="Next slide" ${i === cards.length - 1 ? "disabled" : ""}>${ICON.right}</button></div>`;
  const go = (d) => { S.slide[b.id] = Math.max(0, Math.min(cards.length - 1, i + d)); renderSlides(b); document.getElementById("sl-" + b.id)?.focus(); };
  el.querySelectorAll("[data-d]").forEach((x) => x.onclick = () => go(Number(x.dataset.d)));
  el.onkeydown = (e) => { if (e.key === "ArrowRight") go(1); else if (e.key === "ArrowLeft") go(-1); else if (e.key === "f" || e.key === "F") el.requestFullscreen?.(); };
  el.querySelector("[data-full]").onclick = () => (document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen?.());
}

// a short spaced review from what this learner is weak at: the same quiz screen, questions from several chapters, practice only
async function startReview() {
  try {
    const r = await api(`notebooks/${S.nb.id}/review?learner_id=${S.learner}`);
    if (!r.questions.length) return toast("Nothing needs a review right now.");
    document.getElementById("vt-course")?.click();
    S.companion = ""; renderNote();
    const host = document.getElementById("main"); host.innerHTML = `<section class="blk" id="quizblk"><div class="blk-h"><h3>${r.summary.minutes}-minute review</h3></div><div id="quiz"></div></section>`;
    S.chapter = S.chapter || { id: r.questions[0].chapter_id, blocks: [] }; S.quiz = null;
    startQuiz(r.questions, true);
  } catch (e) { fail(e); }
}

// --- quiz: one question at a time, hint first, misconception feedback, marks, a mascot that reacts, then the course may change
const MASCOT_SAY = {
  idle: ["Take your time. I'm here if you need a hint."], thinking: ["Let me check that..."],
  cheer: ["Yes! That's right.", "Nicely done.", "Exactly."], oops: ["Close. Check the part about it in the notes.", "Not quite. Try again with the hint."],
  proud: ["You've got this chapter. I'll bring back the tricky bits in your review."], learn: ["I'll remember this for your review."],
};
const say = (state) => { const l = MASCOT_SAY[state]; return l[Math.floor(Math.random() * l.length)]; };
function mascot(state, text) {
  const m = document.getElementById("qmascot"); if (!m) return;
  m.dataset.state = state; m.querySelector(".qbubble").textContent = text || say(state);
}
const mascotHtml = () => `<div class="qmascot" id="qmascot" data-state="idle" role="status" aria-live="polite"><span class="qm-fig" aria-hidden="true">${logoSvg()}</span><span class="qbubble">${esc(say("idle"))}</span></div>`;

function startQuiz(questions, retake = false) {
  S.quiz = { cid: S.chapter.id, qs: questions, i: 0, right: 0, retake };
  renderQuestion(1, "");
}

function inputs(q) {
  if (q.type === "multi") return `<p class="muted q-tip">Select all that apply.</p><div id="opts">${q.options.map((o, k) => `<button class="option toggle" aria-pressed="false" data-k="${k}">${esc(o)}</button>`).join("")}</div><button class="btn block" id="check">Check answer</button>`;
  if (q.type === "order") return `<p class="muted q-tip">Put the steps in order.</p><ol id="ord" class="ordlist">${q.options.map((o) => `<li data-v="${esc(o)}"><span>${esc(o)}</span><span class="ordbtns"><button type="button" class="iconbtn up" aria-label="Move up">&uarr;</button><button type="button" class="iconbtn dn" aria-label="Move down">&darr;</button></span></li>`).join("")}</ol><button class="btn block" id="check">Check order</button>`;
  if (q.type === "fill_blank") return `<input id="fillin" class="fillin" type="text" autocomplete="off" aria-label="Your answer" placeholder="Type your answer"><button class="btn block" id="check">Check answer</button>`;
  return `<div id="opts">${q.options.map((o, k) => `<button class="option" data-k="${k}">${esc(o)}</button>`).join("")}</div>`;
}

function renderQuestion(attempt, hint) {
  const el = document.getElementById("quiz"); if (!el || !S.quiz) return;
  const { qs, i } = S.quiz, q = qs[i];
  el.innerHTML = `${mascotHtml()}<div class="crumb">${S.quiz.retake ? "Retake · " : ""}Question ${i + 1} of ${qs.length}${q.difficulty > 1 ? ` · ${q.difficulty === 3 ? "hard" : "medium"}` : ""}</div><h4 class="q-title">${esc(q.text)}</h4>
    ${inputs(q)}<div id="fb">${hint ? `<div class="feedback bad">Hint: ${esc(hint)}</div>` : ""}</div>`;
  if (hint) mascot("oops", "Not quite. Try again with the hint.");
  const type = q.type || "mcq";
  if (type === "mcq") el.querySelectorAll(".option").forEach((b) => b.onclick = () => submitAnswer(q, b.textContent, attempt, [b]));
  else if (type === "multi") {
    el.querySelectorAll(".option").forEach((b) => b.onclick = () => b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")));
    el.querySelector("#check").onclick = () => { const picked = [...el.querySelectorAll('.option[aria-pressed="true"]')]; if (!picked.length) return toast("Pick at least one answer."); submitAnswer(q, picked.map((b) => b.textContent).join(" || "), attempt, picked); };
  } else if (type === "order") {
    const list = el.querySelector("#ord");
    list.onclick = (e) => { const li = e.target.closest("li"), up = e.target.closest(".up"), dn = e.target.closest(".dn"); if (!li) return; if (up && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling); if (dn && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li); };
    el.querySelector("#check").onclick = () => submitAnswer(q, [...list.children].map((li) => li.dataset.v).join(" || "), attempt, []);
  } else {
    const box = el.querySelector("#fillin"); box.focus();
    const go = () => { if (box.value.trim()) submitAnswer(q, box.value.trim(), attempt, [box]); };
    el.querySelector("#check").onclick = go; box.onkeydown = (e) => { if (e.key === "Enter") go(); };
  }
}

async function submitAnswer(q, answer, attempt, els) {
  el_disable(true); els.forEach((b) => b.classList?.add("picked")); mascot("thinking");
  try {
    const r = await api(`chapters/${q.chapter_id || S.chapter.id}/answer`, { learner_id: S.learner, session_id: S.session, question_id: q.id, answer, attempt });
    if (r.follow_up) return renderQuestion(attempt + 1, r.hint);
    const fb = document.getElementById("fb");
    if (r.result === "correct") {
      if (attempt === 1) S.quiz.right++;
      els.forEach((b) => b.classList?.add("right")); mascot("cheer"); fb.innerHTML = `<div class="feedback ok">Correct!${r.explanation ? ` ${esc(r.explanation)}` : ""}</div>`; return nextQuestion();
    }
    els.forEach((b) => b.classList?.add("wrong"));
    const why = r.misconception ? `<p>It looks like: <strong>${esc(r.misconception)}</strong></p>` : "";
    const partial = r.result === "partial" ? `<p>Partly right.</p>` : "";
    if (attempt === 1) {
      mascot("oops");
      fb.innerHTML = `<div class="feedback bad">${partial}${why}<p>Hint: ${esc(r.hint)}</p></div><button class="btn block" id="retry">Try again</button>`;
      document.getElementById("retry").onclick = () => renderQuestion(2, r.hint);
    } else {
      document.querySelectorAll(".option").forEach((b) => { if (String(r.correct_answer).split(", ").includes(b.textContent) || b.textContent === r.correct_answer) b.classList.add("right"); });
      mascot("oops", r.misconception ? "I'll bring this one back in your review." : undefined);
      fb.innerHTML = `<div class="feedback bad">${partial}${why}<p>Answer: ${esc(r.correct_answer)}</p>${r.explanation ? `<p>${esc(r.explanation)}</p>` : ""}</div>`; nextQuestion();
    }
  } catch (e) { fail(e); el_disable(false); }
}
const el_disable = (d) => document.querySelectorAll("#quiz .option, #quiz #check, #quiz #fillin, #quiz .ordbtns button").forEach((b) => (b.disabled = d));

function nextQuestion() {
  const last = S.quiz.i === S.quiz.qs.length - 1;
  document.getElementById("fb").insertAdjacentHTML("beforeend", `<button class="btn block" id="nq">${last ? (S.quiz.retake ? "See result" : "Finish chapter") : "Next question"}</button>`);
  document.getElementById("nq").onclick = () => { if (last) (S.quiz.retake ? finishRetake() : finishQuiz()); else { S.quiz.i++; renderQuestion(1, ""); } };
  document.getElementById("nq").focus();
}

function finishRetake() {
  const { right, qs } = S.quiz, el = document.getElementById("quiz");
  el.innerHTML = `${mascotHtml()}<div class="card"><div class="score">${right} of ${qs.length} right this time</div><p class="muted">Practice only: your chapter result is unchanged.</p></div>`;
  mascot(right === qs.length ? "proud" : "idle", right === qs.length ? "That's the lot. Well done." : "Getting there. The notes have what you need.");
}

async function finishQuiz() {
  const el = document.getElementById("quiz");
  el.innerHTML = `<p class="muted"><span class="spin"></span>Checking how it went...</p>`;
  try {
    const r = await api(`chapters/${S.chapter.id}/finish`, { learner_id: S.learner, session_id: S.session });
    S.nb = r.notebook; renderHead(); renderSide();
    const m = r.marks;
    const proposed = (r.proposed || []).length ? `<p>${esc(r.reason)}</p>` : "";
    const added = r.added.length ? `<p>Your course changed: ${esc(r.reason)}</p><ul>${r.added.map((a) => `<li><button class="linkbtn" data-go="${a.id}">${esc(a.title)}</button></li>`).join("")}</ul>` : "<p>Your course stays the same.</p>";
    const rows = m ? `<ul class="marks">${m.by_concept.map((c) => `<li><span>${esc(c.concept)}</span><span class="bar" role="img" aria-label="${c.percent} percent"><i style="width:${c.percent}%"></i></span><span class="pc">${c.percent}%</span></li>`).join("")}</ul>` : "";
    const retake = m && m.missed.length ? `<button class="btn" id="retake">Retake only the ${m.missed.length} you missed</button>` : "";
    el.innerHTML = `${mascotHtml()}<div class="card"><div class="score">${m ? `${m.percent}%` : `${r.score.correct} of ${r.score.total}`} <span class="muted">${m ? (m.passed ? "· passed" : "· not yet at 70%") : "right first time"}</span></div>
      ${rows}${m && m.weakest.length ? `<p class="muted">Worth another look: ${m.weakest.map(esc).join(", ")}</p>` : ""}${retake}${proposed || added}</div>`;
    if ((r.proposed || []).length) S.chat?.refresh?.();
    mascot(m && m.passed ? "proud" : "oops", m && m.passed ? undefined : "Let's look at the tricky parts again. I'll remember them.");
    el.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => loadChapter(Number(b.dataset.go)));
    const rb = el.querySelector("#retake");
    if (rb) rb.onclick = () => { const all = S.chapter.blocks.find((b) => b.type === "quiz").content.questions; startQuiz(all.filter((q) => m.missed.includes(q.id)), true); };
  } catch (e) { fail(e); }
}

// --- doubts
async function sendDoubt(text) {
  const box = document.getElementById("dans");
  box.innerHTML = `<p class="muted"><span class="spin"></span>Thinking...</p>`;
  const f = new FormData(); f.append("learner_id", S.learner); f.append("text", text);
  try {
    const r = await api(`chapters/${S.chapter.id}/doubt`, null, f);
    if (r.needs_text) { box.innerHTML = `<p class="muted">${esc(r.message)}</p>`; return; }
    box.innerHTML = `<div class="card"><p style="margin:0 0 10px">${esc(r.answer)}</p><button class="btn ghost" id="hear">${ICON.play} Listen</button></div>`;
    document.getElementById("hear").onclick = () => speak(r.answer, r.audio_b64);
    if (r.audio_b64) speak(r.answer, r.audio_b64);
  } catch (e) { box.innerHTML = ""; fail(e); }
}

async function finishSession() {
  try {
    const r = await api("session/close", { learner_id: S.learner, session_id: S.session, notebook_id: S.nb?.id });
    S.session = (await api("session/open", { learner_id: S.learner })).session_id;
    S.companion = r.message + (r.revisit.length ? ` To revisit: ${r.revisit.join(", ")}.` : ""); renderNote();
  } catch (e) { fail(e); }
}

init();
