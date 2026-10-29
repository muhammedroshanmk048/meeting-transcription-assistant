const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("err", isError);
}

// Random per-browser ID: the server only shows recordings uploaded from this browser
function getSessionId() {
  const make = () => (crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));
  try {
    let id = localStorage.getItem("meetscribe-session");
    if (!id) {
      id = make();
      localStorage.setItem("meetscribe-session", id);
    }
    return id;
  } catch {
    return make(); // storage blocked: session lasts until the page is closed
  }
}
const SESSION_ID = getSessionId();

function withSession(options) {
  return { ...options, headers: { ...options.headers, "X-Session-Id": SESSION_ID } };
}

async function api(path, options = {}) {
  const res = await fetch(path, withSession(options));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

const ACTIVE = new Set(["queued", "converting", "transcribing", "summarizing"]);
const STEPS = ["queued", "converting", "transcribing", "summarizing", "done"];
// Recordings never stay on the server, but the browser still has the file: play it from memory
const localAudio = new Map();
let currentId = null;
let pollTimer = null;

function clock(seconds) {
  const s = Math.floor(seconds);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
}

function parseClock(text) {
  const parts = (text || "").replace(/[\[\]]/g, "").trim().split(":").map(Number);
  if (!parts.length || parts.some(Number.isNaN)) return null;
  return parts.reduce((total, p) => total * 60 + p, 0);
}

function expiresIn(epochSeconds) {
  const mins = Math.max(0, Math.round((epochSeconds * 1000 - Date.now()) / 60000));
  return mins >= 60 ? `expires in ${Math.round(mins / 60)}h` : `expires in ${mins}m`;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- Recordings list ----------

async function showLimits() {
  const l = await api("/api/limits");
  $("limits").textContent =
    `Private to this browser · up to ${l.max_upload_mb} MB and ${l.max_audio_minutes} min per recording · ` +
    `audio is deleted after processing, results after ${l.job_ttl_minutes} min`;
}

function jobMeta(j) {
  if (j.status === "error") return "failed";
  if (ACTIVE.has(j.status)) return j.status === "queued" && j.position > 1 ? `queued · #${j.position}` : `${j.status}…`;
  return `${j.duration ? clock(j.duration) + " · " : ""}${j.expires_at ? expiresIn(j.expires_at) : ""}`;
}

async function refreshJobs() {
  const jobs = await api("/api/jobs");
  const list = $("jobs");
  list.innerHTML = "";
  if (!jobs.length) {
    list.innerHTML = '<li class="empty">No recordings yet</li>';
    return jobs;
  }
  for (const j of jobs) {
    const li = document.createElement("li");
    li.classList.toggle("active", j.id === currentId);
    li.dataset.state = ACTIVE.has(j.status) ? "active" : j.status === "error" ? "error" : "done";
    li.innerHTML = `<div class="info"><span class="name"></span><span class="meta"></span></div>`;
    li.querySelector(".name").textContent = li.querySelector(".name").title = j.filename;
    const meta = li.querySelector(".meta");
    meta.textContent = jobMeta(j);
    meta.classList.toggle("err", j.status === "error");
    if (!ACTIVE.has(j.status)) {
      const del = document.createElement("button");
      del.title = "Delete";
      del.textContent = "×";
      del.onclick = async (e) => {
        e.stopPropagation();
        await api(`/api/jobs/${j.id}`, { method: "DELETE" }).catch((err) => setStatus(err.message, true));
        if (j.id === currentId) showView("welcome");
        refreshJobs();
      };
      li.appendChild(del);
    }
    li.onclick = () => openJob(j.id);
    list.appendChild(li);
  }
  return jobs;
}

// ---------- Upload ----------

async function upload(file) {
  if (!file) return;
  setStatus(`Uploading ${file.name}…`);
  const form = new FormData();
  form.append("file", file);
  try {
    const job = await api("/api/jobs", { method: "POST", body: form });
    localAudio.set(job.id, URL.createObjectURL(file));
    setStatus("");
    openJob(job.id);
  } catch (e) {
    setStatus(e.message, true);
  }
  $("file").value = "";
}

$("file").onchange = (e) => upload(e.target.files[0]);
// Files can be dropped anywhere on the page; a counter survives dragenter/leave firing on child elements
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
window.ondragenter = (e) => { if (hasFiles(e)) { dragDepth++; document.body.classList.add("dragging"); } };
window.ondragleave = () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } };
window.ondragover = (e) => e.preventDefault();
window.ondrop = (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
};

// ---------- Job view ----------

function showView(name) {
  for (const v of ["welcome", "progress", "result"]) $(v).classList.toggle("hidden", v !== name);
  if (name === "welcome") currentId = null;
}

async function openJob(id) {
  clearTimeout(pollTimer);
  const changed = id !== currentId;
  currentId = id;
  let job;
  try {
    job = await api(`/api/jobs/${id}`);
  } catch (e) {
    setStatus(e.message, true);
    showView("welcome");
    refreshJobs();
    return;
  }
  if (id !== currentId) return; // user opened another job meanwhile
  refreshJobs();
  if (ACTIVE.has(job.status) || job.status === "error") {
    renderProgress(job);
    if (ACTIVE.has(job.status)) pollTimer = setTimeout(() => openJob(id), 1500);
  } else if (changed || $("result").classList.contains("hidden")) {
    renderResult(job);
  }
}

function renderProgress(job) {
  showView("progress");
  $("progressTitle").textContent = job.filename;
  const failed = job.status === "error";
  const current = failed ? STEPS.indexOf(lastStep[job.id] || "converting") : STEPS.indexOf(job.status);
  if (!failed) lastStep[job.id] = job.status;
  $("steps").classList.toggle("failed", failed);
  $("steps").querySelectorAll("li").forEach((li, i) => {
    li.classList.toggle("done", i < current);
    li.classList.toggle("current", i === current);
  });
  const msg = $("progressMsg");
  msg.textContent = failed ? job.error : job.status === "queued" && job.position > 1 ? `${job.position - 1} recording(s) ahead of yours` : job.message;
  msg.classList.toggle("err", failed);
}
const lastStep = {};

function list(el, items, render) {
  el.innerHTML = "";
  if (!items.length) {
    el.innerHTML = '<li class="none">None</li>';
    return;
  }
  for (const item of items) el.appendChild(render(item));
}

function textItem(text) {
  const li = document.createElement("li");
  li.textContent = text;
  return li;
}

function loadChecked(id) {
  try { return new Set(JSON.parse(localStorage.getItem(`meetscribe-done-${id}`) || "[]")); } catch { return new Set(); }
}

function saveChecked(id, set) {
  try { localStorage.setItem(`meetscribe-done-${id}`, JSON.stringify([...set])); } catch {}
}

function tsButton(seconds, label) {
  const b = document.createElement("button");
  b.className = "ts";
  b.textContent = label ?? clock(seconds);
  b.onclick = () => seek(seconds);
  return b;
}

let segments = [];

function renderResult(job) {
  showView("result");
  const notes = job.notes;
  const t = job.transcript;
  segments = t.segments;
  $("title").textContent = notes?.title || job.filename;
  const lang = t.language ? ` · ${t.language}` : "";
  $("meta").textContent = `${job.filename} · ${clock(job.duration || 0)} · ${t.engine}${lang} · ${expiresIn(job.expires_at)}`;

  const player = $("player");
  const src = localAudio.get(job.id);
  player.classList.toggle("hidden", !src);
  if (src && player.src !== src) player.src = src;
  if (!src) player.removeAttribute("src");

  $("summaryError").textContent = job.summary_error || "";
  $("summaryError").classList.toggle("hidden", !job.summary_error);
  $("cards").classList.toggle("hidden", !notes);
  if (notes) {
    $("summary").textContent = notes.summary;
    list($("decisions"), notes.key_decisions, textItem);
    list($("questions"), notes.open_questions, textItem);
    const checked = loadChecked(job.id);
    let i = 0;
    list($("actions"), notes.action_items, (a) => {
      const index = i++;
      const li = document.createElement("li");
      li.innerHTML = '<input type="checkbox" /><span class="task"></span>';
      const box = li.querySelector("input");
      const task = li.querySelector(".task");
      task.textContent = a.task;
      if (a.owner) task.insertAdjacentHTML("beforeend", `<span class="chip owner">${escapeHtml(a.owner)}</span>`);
      if (a.due) task.insertAdjacentHTML("beforeend", `<span class="chip">due ${escapeHtml(a.due)}</span>`);
      const at = parseClock(a.timestamp);
      if (at !== null) li.appendChild(tsButton(at));
      box.checked = checked.has(index);
      li.classList.toggle("checked", box.checked);
      box.onchange = () => {
        box.checked ? checked.add(index) : checked.delete(index);
        li.classList.toggle("checked", box.checked);
        saveChecked(job.id, checked);
      };
      return li;
    });
  }
  $("search").value = "";
  renderTranscript("");
}

function renderTranscript(query) {
  const ol = $("transcript");
  ol.innerHTML = "";
  const q = query.trim().toLowerCase();
  const pattern = q ? new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi") : null;
  segments.forEach((s, i) => {
    if (q && !s.text.toLowerCase().includes(q) && !(s.speaker || "").toLowerCase().includes(q)) return;
    const li = document.createElement("li");
    li.dataset.i = i;
    li.appendChild(tsButton(s.start));
    const text = document.createElement("span");
    const body = pattern ? escapeHtml(s.text).replace(pattern, "<mark>$1</mark>") : escapeHtml(s.text);
    text.innerHTML = (s.speaker ? `<span class="speaker">${escapeHtml(s.speaker)}</span>` : "") + body;
    li.appendChild(text);
    ol.appendChild(li);
  });
  if (!ol.children.length) ol.innerHTML = `<li class="hint">${segments.length ? "No matches" : "No speech detected"}</li>`;
}

$("search").oninput = (e) => renderTranscript(e.target.value);

function segmentAt(seconds) {
  let found = -1;
  for (let i = 0; i < segments.length && segments[i].start <= seconds + 0.01; i++) found = i;
  return found;
}

function seek(seconds) {
  const player = $("player");
  if (player.src) {
    player.currentTime = seconds;
    player.play().catch(() => {});
  }
  const li = $("transcript").querySelector(`li[data-i="${segmentAt(seconds)}"]`);
  if (li) {
    li.scrollIntoView({ behavior: "smooth", block: "center" });
    li.classList.remove("flash");
    void li.offsetWidth; // restart the animation
    li.classList.add("flash");
  }
}

let nowIndex = -1;
$("player").ontimeupdate = () => {
  const i = segmentAt($("player").currentTime);
  if (i === nowIndex) return;
  $("transcript").querySelector(".now")?.classList.remove("now");
  $("transcript").querySelector(`li[data-i="${i}"]`)?.classList.add("now");
  nowIndex = i;
};

// ---------- Export ----------

document.querySelectorAll(".exports button").forEach((b) => {
  b.onclick = async () => {
    try {
      const res = await fetch(`/api/jobs/${currentId}/export?format=${b.dataset.format}`, withSession({}));
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Export failed");
      const name = res.headers.get("Content-Disposition")?.match(/filename="(.+)"/)?.[1] || `meeting.${b.dataset.format}`;
      const url = URL.createObjectURL(await res.blob());
      const a = Object.assign(document.createElement("a"), { href: url, download: name });
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setStatus(e.message, true);
    }
  };
});

// Reopen whatever is running or most recent after a reload
refreshJobs()
  .then((jobs) => {
    const running = jobs.find((j) => ACTIVE.has(j.status));
    if (running) openJob(running.id);
  })
  .catch((e) => setStatus(e.message, true));
showLimits().catch(() => {});
