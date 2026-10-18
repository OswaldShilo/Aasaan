// Shared helpers for the Aasaan UI: escaping, API calls, storage, icons, WAV recording for voice.
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// Theme: light by default (whatever the OS prefers); "dark" only when the student chooses it. The inline script in index.html applies it before first paint.
export const themePref = () => (store.get("aasaan_theme") === "dark" ? "dark" : "light");
export function setTheme(pref) {
  store.set("aasaan_theme", pref === "dark" ? "dark" : "light");
  if (pref === "dark") document.documentElement.setAttribute("data-theme", "dark"); else document.documentElement.removeAttribute("data-theme");
}

export const LANGS = { en: "English", hi: "हिन्दी", kn: "ಕನ್ನಡ", ta: "தமிழ்", te: "తెలుగు", ml: "മലയാളം", bn: "বাংলা", mr: "मराठी" };
export const lang = () => store.get("lang") || "en";

export async function api(path, body, form, method) {
  const opts = form ? { method: method || "POST", body: form } :
    body !== undefined ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } :
    { method: method || "GET" };
  const r = await fetch("/api/" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || "Something went wrong. Please try again.");
  return data;
}

const svg = (d, extra = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;
export const ICON = {
  logo: svg('<circle cx="12" cy="12" r="3"/><circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="20" r="2"/><path d="M7 7l3 3M17 7l-3 3M12 15v3"/>'),
  clip: svg('<path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7L14 4.5a3.3 3.3 0 014.7 4.7l-8.6 8.6a1.7 1.7 0 01-2.4-2.4L15 8.6"/>'),
  up: svg('<path d="M12 19V5M5 12l7-7 7 7"/>', 'stroke-width="2"'),
  globe: svg('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/>', 'stroke-width="2"'),
  chev: svg('<polyline points="6 9 12 15 18 9"/>', 'class="dim" stroke-width="2"'),
  doc: svg('<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/>'),
  mic: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/>'),
  stop: svg('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
  play: svg('<path d="M7 5l12 7-12 7z"/>'),
  edit: svg('<path d="M4 20h4L19 9l-4-4L4 16z"/>'),
  ext: svg('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>'),
  left: svg('<path d="M15 6l-6 6 6 6"/>'),
  expand: svg('<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>'),
  right: svg('<path d="M9 6l6 6-6 6"/>'),
  list: svg('<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>', 'stroke-width="2"'),
  graph: svg('<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8 7l3 9M16 7l-3 9"/>'),
};

export const timeAgo = (ts) => {
  const s = Math.max(1, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
};

export function speak(text, audioB64) {
  if (audioB64) { new Audio("data:audio/wav;base64," + audioB64).play(); return; }
  if ("speechSynthesis" in window) { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text)); }
}

// ---- voice: record from the microphone and return a 16 kHz mono WAV (Sarvam STT wants WAV/MP3/AAC/FLAC/OGG, < 30 s)
export async function startRecording(maxSeconds = 28) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const rec = new MediaRecorder(stream), chunks = [];
  rec.ondataavailable = (e) => chunks.push(e.data);
  const done = new Promise((resolve) => {
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      try { resolve(await toWav(new Blob(chunks))); } catch (e) { resolve(null); }
    };
  });
  rec.start();
  const timer = setTimeout(() => rec.state !== "inactive" && rec.stop(), maxSeconds * 1000);
  return { stop: () => { clearTimeout(timer); if (rec.state !== "inactive") rec.stop(); return done; } };
}

async function toWav(blob) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const buf = await ctx.decodeAudioData(await blob.arrayBuffer());
  await ctx.close();
  const rate = 16000, len = Math.ceil(buf.duration * rate);
  const off = new OfflineAudioContext(1, len, rate);
  const src = off.createBufferSource(); src.buffer = buf; src.connect(off.destination); src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const out = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const w = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); out.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVEfmt "); out.setUint32(16, 16, true);
  out.setUint16(20, 1, true); out.setUint16(22, 1, true); out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true);
  out.setUint16(32, 2, true); out.setUint16(34, 16, true); w(36, "data"); out.setUint32(40, pcm.length * 2, true);
  pcm.forEach((s, i) => out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([out], { type: "audio/wav" });
}
