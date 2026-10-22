// Small, safe math typesetting for chat answers and notes. It works on text that is ALREADY HTML-escaped and only ever adds a few known tags,
// so nothing the model writes can inject markup. Handles $...$ and \(...\) spans, ^ and _ (sup/sub), \frac, \sqrt, Greek letters and common operators.
const SYM = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", theta: "θ", lambda: "λ", mu: "μ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", phi: "φ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Pi: "Π", Sigma: "Σ", Phi: "Φ", Omega: "Ω",
  sum: "∑", prod: "∏", int: "∫", partial: "∂", nabla: "∇", infty: "∞", pm: "±", times: "×", cdot: "·", div: "÷",
  le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠", approx: "≈", equiv: "≡", to: "→", rightarrow: "→", leftarrow: "←", Rightarrow: "⇒", Leftrightarrow: "⇔",
  in: "∈", notin: "∉", cup: "∪", cap: "∩", subset: "⊂", subseteq: "⊆", forall: "∀", exists: "∃", neg: "¬", land: "∧", lor: "∨", ldots: "…", cdots: "⋯",
  log: "log", ln: "ln", sin: "sin", cos: "cos", tan: "tan", lim: "lim", max: "max", min: "min",
};
const BLACKBOARD = { R: "ℝ", N: "ℕ", Z: "ℤ", Q: "ℚ", C: "ℂ" };
const sup = (s) => `<sup>${s}</sup>`, sub = (s) => `<sub>${s}</sub>`;

function group(s, i) {            // the {…} group or single character after ^ or _ starting at s[i]
  if (s[i] === "{") {
    let depth = 0;
    for (let j = i; j < s.length; j++) { if (s[j] === "{") depth++; else if (s[j] === "}" && --depth === 0) return [s.slice(i + 1, j), j + 1]; }
    return [s.slice(i + 1), s.length];
  }
  const m = s.slice(i).match(/^(-?\d+|-?[A-Za-z]|[+-])/);
  return m ? [m[0], i + m[0].length] : ["", i];
}

function convert(s, inMath) {
  let out = "", i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === "\\") {
      const m = s.slice(i + 1).match(/^([A-Za-z]+)/);
      if (m) {
        const name = m[1];
        let j = i + 1 + name.length;
        if (name === "frac" && s[j] === "{") { const [a, k] = group(s, j); const [b, l] = group(s, k); out += `(${convert(a, true)})/(${convert(b, true)})`; i = l; continue; }
        if (name === "sqrt" && s[j] === "{") { const [a, k] = group(s, j); out += `√(${convert(a, true)})`; i = k; continue; }
        if ((name === "mathbb" || name === "mathbf" || name === "text" || name === "mathrm") && s[j] === "{") {
          const [a, k] = group(s, j); out += name === "mathbb" ? (BLACKBOARD[a] || a) : convert(a, true); i = k; continue;
        }
        if (SYM[name] !== undefined) { out += SYM[name]; i = j; continue; }
      }
      out += c; i++; continue;
    }
    if (inMath && (c === "^" || c === "_")) {
      const [g, k] = group(s, i + 1);
      if (g) { out += (c === "^" ? sup : sub)(convert(g, true)); i = k; continue; }
    }
    out += c; i++;
  }
  return out;
}

/** Typeset already-escaped HTML. Code spans are left alone. */
export function typeset(html) {
  return String(html).split(/(<code>[\s\S]*?<\/code>|<pre>[\s\S]*?<\/pre>)/).map((part, idx) => {
    if (idx % 2) return part;                                        // inside code
    return part
      .replace(/\$([^$\n]{1,200})\$/g, (_, m) => `<span class="math">${convert(m, true)}</span>`)
      .replace(/\\\(([^\n]{1,200}?)\\\)/g, (_, m) => `<span class="math">${convert(m, true)}</span>`)
      // outside $…$: a plain exponent like n^2, x^{10} or O(n^2), and LaTeX commands that were not wrapped
      .replace(/([A-Za-z0-9)\]])\^(\{[^}]{1,40}\}|-?\d{1,3}|[A-Za-z])/g, (_, base, e) => base + sup(convert(e.replace(/^\{|\}$/g, ""), true)))
      .replace(/\\(alpha|beta|gamma|delta|theta|lambda|mu|pi|sigma|omega|sum|int|infty|leq?|geq?|neq?|approx|times|cdot|pm|to|rightarrow|in|cup|cap|forall|exists)\b/g, (_, n) => SYM[n]);
  }).join("");
}
