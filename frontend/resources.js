// Resources view: what the student gave (files, links, pasted text) and what Aasaan found, with honest per-item status and problems.
import { esc, ICON } from "./util.js";

const fmtSize = (n) => (!n ? "" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
const KIND = { pdf: "PDF", image: "Image", url: "Link", pasted: "Pasted text", text: "Text file" };
const STATUS = { reading: "Reading…", ready: "Ready", partly_readable: "Partly readable", unreadable: "Unreadable" };
const FOUND_GROUPS = [["video", "Videos"], ["article", "Articles"], ["book", "Books"], ["image", "Images"]];

export function createResources(ctx) {
  // ctx: { nid, api, toast, openChapter(id), isVisible() }
  const root = document.getElementById("resview");
  const st = { data: null, adding: null, busy: false, last: 0 };

  const rng = (p) => (p.length > 6 ? `${p[0]} to ${p[p.length - 1]} (${p.length} pages)` : p.join(", "));
  function problems(r) {
    if (!r.issues?.length || r.status === "reading") return "";
    const total = r.issues.reduce((n, i) => n + i.pages.length, 0);
    const kinds = r.issues.map((i) => i.kind);
    const calm = kinds.every((k) => ["drawing_only", "blank", "over_limit"].includes(k));
    const head = r.kind === "pdf" && total
      ? (calm ? `${total} page${total === 1 ? "" : "s"} ${total === 1 ? "has" : "have"} no text to read.` : `${total} page${total === 1 ? "" : "s"} couldn't be read.`) : "This couldn't be read fully.";
    const retry = kinds.includes("failed") || r.status === "unreadable";
    return `<details class="calm"><summary>${head}${r.pages_total ? ` ${r.pages_read} of ${r.pages_total} pages were read.` : ""}</summary>
      ${r.issues.map((i) => `<p>${i.pages.length ? `<strong>Pages ${rng(i.pages)}.</strong> ` : ""}${esc(i.reason)}</p>`).join("")}
      ${retry ? `<p><button class="linkbtn" data-act="clearer" data-id="${r.id}" type="button">Upload clearer pages</button></p>` : ""}</details>`;
  }
  const usedIn = (list) => list?.length ? `<div class="usedin"><span class="muted">Used in</span> ${list.map((c) => `<button class="srcchip" data-ch="${c.id}" type="button">${c.position}. ${esc(c.title)}</button>`).join("")}</div>`
    : `<div class="usedin muted">Not used in any chapter yet</div>`;

  function mineCard(r) {
    const meta = [KIND[r.kind] || r.kind, fmtSize(r.size), r.pages_total ? `${r.pages_total} pages` : "", r.length ? `${r.length.toLocaleString()} characters` : ""].filter(Boolean).join(" · ");
    const thumb = r.kind === "image" || r.kind === "pdf" ? `<img class="rthumb" src="/api/notebooks/${ctx.nid}/resources/${r.id}/thumb" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'rthumb ph',innerHTML:'${KIND[r.kind]}'}))">`
      : `<span class="rthumb ph">${r.kind === "url" ? "Link" : "Text"}</span>`;
    return `<li class="rcard" data-rid="${esc(r.id)}">${thumb}
      <div class="rbody"><div class="rtitle">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>` : esc(r.title)}</div>
        <div class="rmeta"><span>${meta}</span><span class="rstat s-${r.status}">${r.status === "reading" ? `<span class="spin"></span>` : ""}${STATUS[r.status] || r.status}</span></div>
        ${r.summary ? `<p class="rsum">${esc(r.summary)}</p>` : ""}${problems(r)}${usedIn(r.used_in)}
        <div class="ract"><button class="linkbtn" data-act="preview" data-id="${r.id}" type="button">Open preview</button>
          <button class="linkbtn" data-act="reprocess" data-id="${r.id}" type="button">Re-process</button>
          <button class="linkbtn danger" data-act="remove" data-id="${r.id}" type="button">Remove</button></div></div></li>`;
  }
  function foundCard(f) {
    const credit = [f.author, f.license].filter(Boolean).join(" · ");
    return `<li class="rcard found">${f.thumb ? `<img class="rthumb" src="${esc(f.thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="rthumb ph">${f.kind === "video" ? "Video" : f.kind === "book" ? "Book" : "Link"}</span>`}
      <div class="rbody"><div class="rtitle">${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">${esc(f.title || f.url)}</a>` : esc(f.title)}</div>
        <div class="rmeta"><span>${esc(f.source || "")}${credit ? ` · ${esc(credit)}` : ""}</span></div>${usedIn(f.used_in)}</div></li>`;
  }

  function addZone() {
    const form = st.adding === "link" ? `<form class="addform" data-form="link"><input name="u" type="url" placeholder="https://…" aria-label="Link to add" required><button class="btn" type="submit">Add link</button><button class="btn ghost" type="button" data-act="cancel">Cancel</button></form>`
      : st.adding === "text" ? `<form class="addform" data-form="text"><textarea name="t" rows="5" placeholder="Paste notes or text here" aria-label="Text to add" required></textarea><div><button class="btn" type="submit">Add text</button> <button class="btn ghost" type="button" data-act="cancel">Cancel</button></div></form>` : "";
    return `<div class="dropzone" id="dropzone"><div><strong>Add a resource</strong><span class="muted"> Drop files here: PDFs, photos or text files.</span></div>
      <div class="dz-actions"><button class="btn ghost" type="button" data-act="addfiles">${ICON.clip} Add files</button><button class="btn ghost" type="button" data-act="addlink">Add a link</button><button class="btn ghost" type="button" data-act="addtext">Paste text</button></div>${form}
      <input type="file" id="resfile" aria-label="Choose files to add" multiple accept="image/*,application/pdf,.txt,.md,text/plain" hidden></div>`;
  }

  function render() {
    const d = st.data;
    const mine = d ? d.mine : [], found = d ? d.found : [];
    root.innerHTML = `<h2 class="res-h">Resources</h2>${addZone()}
      <h3 class="res-sub">Yours <span class="muted">${mine.length}</span></h3>
      ${d ? (mine.length ? `<ul class="rlist">${mine.map(mineCard).join("")}</ul>` : `<p class="muted">Nothing here yet. Files, links and text you add appear here, and Aasaan reads them.</p>`) : `<div class="bar-s w60"></div><div class="bar-s"></div>`}
      <h3 class="res-sub">Found by Aasaan <span class="muted">${found.length}</span></h3>
      <p class="muted res-note">Videos, articles and images Aasaan picked for your chapters. Images keep their licence and author.</p>
      ${found.length ? FOUND_GROUPS.map(([k, label]) => { const items = found.filter((f) => f.kind === k); return items.length ? `<h4 class="res-grp">${label}</h4><ul class="rlist">${items.map(foundCard).join("")}</ul>` : ""; }).join("") : `<p class="muted">Nothing yet. These appear as chapters are written.</p>`}`;
    wire();
  }

  async function refresh(force) {
    if (!force && Date.now() - st.last < 1500) return;
    st.last = Date.now();
    try { st.data = await ctx.api(`notebooks/${ctx.nid}/resources`); if (!st.adding || !ctx.isVisible()) render(); else patchLists(); } catch { /* keep what we have */ }
  }
  function patchLists() { /* while a form is open, don't wipe what the student is typing */ const d = st.data; const ul = root.querySelector(".rlist"); if (ul && d) ul.innerHTML = d.mine.map(mineCard).join(""); wire(); }

  async function upload(form) {
    if (st.busy) return; st.busy = true;
    try { await ctx.api(`notebooks/${ctx.nid}/resources`, null, form); st.adding = null; ctx.toast("Added. Aasaan is reading it."); await refresh(true); }
    catch (e) { ctx.toast(e.message || String(e)); } finally { st.busy = false; }
  }
  const filesForm = (files) => { const f = new FormData(); [...files].forEach((x) => f.append("files", x)); return f; };

  function dialog(html) {
    const dlg = document.createElement("dialog"); dlg.className = "rdlg"; dlg.innerHTML = html;
    dlg.addEventListener("close", () => dlg.remove()); document.body.appendChild(dlg); dlg.showModal(); return dlg;
  }
  async function preview(id) {
    const r = st.data.mine.find((x) => x.id === id);
    const base = `/api/notebooks/${ctx.nid}/resources/${id}`;
    let body = "";
    if (r.kind === "image") body = `<img src="${base}/file" alt="${esc(r.title)}" class="pv-img">`;
    else if (r.kind === "pdf") body = `<iframe src="${base}/file" title="${esc(r.title)}" class="pv-pdf"></iframe>`;
    else { try { const p = await ctx.api(`notebooks/${ctx.nid}/resources/${id}/preview`); body = `<pre class="pv-text">${esc(p.text || "(nothing read yet)")}</pre>${p.truncated ? `<p class="muted">Showing the start only.</p>` : ""}`; } catch (e) { body = `<p>${esc(e.message)}</p>`; } }
    const dlg = dialog(`<div class="dlg-h"><strong>${esc(r.title)}</strong><button class="iconbtn" data-close aria-label="Close preview">×</button></div>${body}`);
    dlg.querySelector("[data-close]").onclick = () => dlg.close();
  }
  async function remove(id) {
    const r = st.data.mine.find((x) => x.id === id);
    let cited = []; try { cited = (await ctx.api(`notebooks/${ctx.nid}/resources/${id}/impact`)).cited_by; } catch { /* none */ }
    const dlg = dialog(`<div class="dlg-h"><strong>Remove "${esc(r.title.slice(0, 50))}"?</strong></div>
      ${cited.length ? `<p>These chapters use it:</p><ul>${cited.map((c) => `<li>${c.position}. ${esc(c.title)}</li>`).join("")}</ul><p class="muted">They keep their text. You can also have them rebuilt without this resource.</p>` : `<p class="muted">No chapter uses it.</p>`}
      <div class="dlg-act">${cited.length ? `<button class="btn" data-go="regen">Remove and rebuild those chapters</button>` : ""}<button class="btn ghost" data-go="only">${cited.length ? "Remove only" : "Remove"}</button><button class="btn ghost" data-go="cancel">Cancel</button></div>`);
    dlg.querySelectorAll("[data-go]").forEach((b) => b.onclick = async () => {
      const go = b.dataset.go; dlg.close(); if (go === "cancel") return;
      try { await ctx.api(`notebooks/${ctx.nid}/resources/${id}?regenerate=${go === "regen"}`, null, null, "DELETE"); await refresh(true); } catch (e) { ctx.toast(e.message || String(e)); }
    });
  }

  function wire() {
    const file = root.querySelector("#resfile");
    root.querySelectorAll("[data-act]").forEach((b) => b.onclick = async () => {
      const a = b.dataset.act, id = b.dataset.id;
      if (a === "addfiles" || a === "clearer") file.click();
      else if (a === "addlink") { st.adding = "link"; render(); root.querySelector("input[name=u]")?.focus(); }
      else if (a === "addtext") { st.adding = "text"; render(); root.querySelector("textarea[name=t]")?.focus(); }
      else if (a === "cancel") { st.adding = null; render(); }
      else if (a === "preview") preview(id);
      else if (a === "remove") remove(id);
      else if (a === "reprocess") { try { await ctx.api(`notebooks/${ctx.nid}/resources/${id}/reprocess`, {}); await refresh(true); } catch (e) { ctx.toast(e.message || String(e)); } }
    });
    if (file) file.onchange = () => { if (file.files.length) upload(filesForm(file.files)); file.value = ""; };
    root.querySelectorAll("[data-ch]").forEach((b) => b.onclick = () => ctx.openChapter(Number(b.dataset.ch)));
    const f = root.querySelector("form[data-form]");
    if (f) f.onsubmit = (e) => { e.preventDefault(); const fd = new FormData(); if (f.dataset.form === "link") fd.append("urls", f.u.value.trim()); else fd.append("text", f.t.value); upload(fd); };
    const dz = root.querySelector("#dropzone");
    if (dz) {
      ["dragenter", "dragover"].forEach((t) => dz.addEventListener("dragover" === t || t === "dragenter" ? t : t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
      ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
      dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) upload(filesForm(e.dataTransfer.files)); });
    }
  }

  render();
  return { refresh, render, onSnapshot() { if (ctx.isVisible()) refresh(false); } };
}
