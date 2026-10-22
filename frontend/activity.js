// Activity: what each agent decided and what came out of it, in plain words, as the notebook builds. Data comes from
// GET /api/notebooks/<id>/decisions (replayed from 0 on load, then only what is new when the snapshot's decision_seq moves).
import { esc, store, timeAgo } from "./util.js";

const NAMES = { intake: "Intake", scoper: "Scoper", architect: "Architect", curator: "Curator", teacher: "Teacher", critic: "Critic", quizzer: "Quizzer", illustrator: "Illustrator",
  slides: "Slides", diagnoser: "Diagnoser", rewriter: "Rewriter", editor: "Editor", companion: "Companion", doubt: "Doubt answerer", practice: "Practice Curator", referee: "Aasaan" };
const WORKING = { drafting: "Writing", checking: "Checking facts in", queued: "Queued:" };

export function createActivity({ nid, api, getChapters, openChapter }) {
  const root = document.getElementById("activity");
  const st = { items: [], last: 0, problems: false, show: store.get("feed") !== "off", busy: false };
  const seen = new Set();

  const chapterTitle = (cid) => { const c = (getChapters() || []).find((x) => x.id === cid); return c ? `${c.position}. ${c.title}` : "Notebook"; };
  const group = () => { const g = new Map(); for (const d of st.items) { if (st.problems && d.severity === "info") continue; const k = d.chapter_id || 0; (g.get(k) || g.set(k, []).get(k)).push(d); } return g; };

  function working() {
    const c = (getChapters() || []).find((x) => WORKING[x.status] && x.status !== "queued");
    return c ? `${WORKING[c.status]} chapter ${c.position}: ${c.title}` : "";
  }

  function draw() {
    if (!root) return;
    const g = group();
    const live = working();
    const body = !st.show ? `<p class="muted act-off">The feed is hidden. Turn it back on to see how Aasaan works.</p>` : !st.items.length
      ? `<p class="muted act-empty">Nothing yet. Decisions appear here as your notebook builds.</p>`
      : [...g.entries()].sort((a, b) => a[0] - b[0]).map(([cid, list]) => `<details class="act-group" open><summary>${esc(cid ? chapterTitle(cid) : "Your notebook")} <span class="muted">${list.length}</span></summary>
          <ol class="act-list">${list.map((d) => `<li class="act-item sev-${d.severity}"><button class="act-row" type="button" data-cid="${d.chapter_id || ""}">
            <span class="act-dot" aria-hidden="true"></span><span class="act-body"><span class="act-who">${esc(NAMES[d.agent] || d.agent)}</span> ${esc(d.summary)}
            <span class="act-time muted">${esc(timeAgo(d.ts))}</span></span></button></li>`).join("")}</ol></details>`).join("");
    root.innerHTML = `<div class="act-head"><label class="act-sw"><input type="checkbox" id="actshow" ${st.show ? "checked" : ""}> Show how Aasaan works</label>
        <label class="act-sw"><input type="checkbox" id="actprob" ${st.problems ? "checked" : ""}> Problems only</label></div>
      ${live && st.show ? `<p class="act-live" role="status"><span class="spin" aria-hidden="true"></span>${esc(live)}</p>` : ""}
      <div class="act-scroll" aria-live="polite">${body}</div>`;
    root.querySelector("#actshow").onchange = (e) => { st.show = e.target.checked; store.set("feed", st.show ? "on" : "off"); draw(); };
    root.querySelector("#actprob").onchange = (e) => { st.problems = e.target.checked; draw(); };
    root.querySelectorAll(".act-row").forEach((b) => b.onclick = () => { const cid = Number(b.dataset.cid); if (cid) openChapter(cid); });
  }

  async function refresh(seq) {
    if (st.busy || (seq !== undefined && seq <= st.last)) { if (seq === undefined) draw(); return; }
    st.busy = true;
    try {
      const r = await api(`notebooks/${nid}/decisions?since=${st.last}`);
      for (const d of r.decisions || []) if (!seen.has(d.id)) { seen.add(d.id); st.items.push(d); st.last = Math.max(st.last, d.id); }
    } catch { /* the feed is a nicety: never an error toast */ }
    st.busy = false;
    draw();
  }
  draw();
  refresh();
  return { refresh, draw };
}
