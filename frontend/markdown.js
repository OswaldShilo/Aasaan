// Tiny, safe markdown renderer for chapter notes: escape first, then apply a small set of rules.
// Supports headings, bold, italic, inline code, code fences, links (http/https only), bullet and numbered lists, paragraphs.
import { esc } from "./util.js";
import { typeset } from "./math.js";

const inline = (s) => typeset(esc(s)
  .replace(/`([^`]+)`/g, "<code>$1</code>")
  .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
  .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'));

export function renderMarkdown(md) {
  const lines = String(md || "").replace(/\r/g, "").split("\n");
  const out = [];
  let list = null, para = [], code = null, table = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const flushTable = () => {
    if (!table) return;
    const [head, ...rows] = table;
    out.push(`<div class="tbl"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    table = null;
  };
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (code === null) { flushPara(); flushList(); code = []; } else { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) {   // a markdown table row; the |---|---| separator line is skipped
      flushPara(); flushList();
      if (!/^\s*\|[\s:|-]+\|\s*$/.test(line)) (table = table || []).push(cells(line));
      continue;
    }
    flushTable();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushPara(); flushList(); const n = Math.min(4, m[1].length + 1); out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
    else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushPara(); if (list !== "ul") { flushList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (list !== "ol") { flushList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if (!line.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(line.trim()); }
  }
  if (code !== null) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
  flushPara(); flushList(); flushTable();
  return out.join("\n");
}
