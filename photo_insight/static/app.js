const $ = (s, el = document) => el.querySelector(s);
const state = {
  config: null, uncategorized: "Uncategorized", photos: new Map(), filter: null, polling: false,
  selected: new Set(), lastPicked: null, starters: {}, source: null, geo: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Photo-Insight": "1", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data.detail || data || res.statusText);
  return data;
}

function notice(msg) {
  const n = $("#notice");
  n.textContent = msg;
  n.hidden = !msg;
}

const tagText = (tags) => tags.map((t) => "#" + t).join(" ");
const parseTags = (text) => text.split(/[\s,]+/).map((t) => t.replace(/^#+/, "")).filter(Boolean);
const categoryNames = () => [...state.config.categories.map((c) => c.name), state.uncategorized];
const catLabel = (name) => (name === state.uncategorized ? t("uncategorized") : name);

// Actions apply to the selected photos, or to every photo when nothing is selected.
const scope = () => (state.selected.size ? [...state.selected].map((p) => state.photos.get(p)) : [...state.photos.values()]);
const scopePaths = () => (state.selected.size ? [...state.photos.keys()].filter((p) => state.selected.has(p)) : null);
const visibleCards = () => [...document.querySelectorAll(".card:not([hidden])")];

// ---------- status / model ----------
async function loadStatus() {
  const s = await api("/api/status");
  state.config = s.config;
  state.uncategorized = s.uncategorized;
  state.starters = s.starter_categories;
  state.geo = s.geo;
  state.exiftool = s.exiftool;
  const pill = $("#ollama");
  pill.dataset.i18n = s.ollama.ok ? "ollama_ok" : "ollama_bad";
  pill.textContent = t(pill.dataset.i18n);
  pill.className = s.ollama.ok ? "pill ok" : "pill bad";
  pill.title = s.ollama.ok ? "" : s.ollama.error;
  const sel = $("#model");
  sel.innerHTML = "";
  const models = s.ollama.models.filter((m) => m.vision);
  if (!models.some((m) => m.name === s.config.settings.model)) models.unshift({ name: s.config.settings.model });
  for (const m of models) sel.add(new Option(m.name, m.name, false, m.name === s.config.settings.model));
  if (s.folder) {
    $("#folderPath").value = s.folder;
    await loadPhotos();
  }
  refreshAll();
  if (["running", "cancelling"].includes(s.job.status)) startPolling();
}

$("#model").addEventListener("change", async (e) => {
  const cfg = structuredClone(state.config);
  cfg.settings.model = e.target.value;
  try {
    state.config = await api("/api/config", { method: "PUT", body: cfg });
    refreshAll();
    notice(t("model_set", { model: e.target.value }));
  } catch (err) {
    notice(err.message);
  }
});

// ---------- folder ----------
$("#browse").addEventListener("click", async () => {
  try {
    const { path } = await api("/api/pick-folder", { method: "POST" });
    if (path) {
      $("#folderPath").value = path;
      openFolder();
    }
  } catch (err) {
    notice(err.message);
  }
});

$("#folderForm").addEventListener("submit", (e) => {
  e.preventDefault();
  openFolder();
});

$("#choosePhotos").addEventListener("click", () => choosePhotos(false));
$("#addPhotos").addEventListener("click", () => choosePhotos(true));

async function choosePhotos(append) {
  try {
    const data = await api("/api/files", { method: "POST", body: { append } });
    if (data.cancelled) return;
    setPhotos(data.photos, data, append);
    notice(data.skipped ? t("skipped", { n: data.skipped }) : "");
  } catch (err) {
    notice(err.message);
  }
}

async function openFolder() {
  const path = $("#folderPath").value.trim();
  if (!path) return;
  try {
    const data = await api("/api/folder", { method: "POST", body: { path, recursive: $("#recursive").checked } });
    setPhotos(data.photos, data);
    notice(data.photos.length ? "" : t("no_photos"));
  } catch (err) {
    notice(err.message);
  }
}

async function loadPhotos() {
  const data = await api("/api/photos");
  setPhotos(data.photos, data, true);
}

function setPhotos(list, info = {}, keepSelection = false) {
  state.photos = new Map(list.map((p) => [p.path, p]));
  state.selected = keepSelection ? new Set([...state.selected].filter((p) => state.photos.has(p))) : new Set();
  state.lastPicked = null;
  if (!keepSelection) state.filter = null;
  if (info.folder) $("#dest").value = info.folder.replace(/\/$/, "") + "/_sorted";
  state.source = list.length ? info.source : null;
  if (state.source === "files" || !list.length) $("#folderPath").value = "";
  else if (info.folder) $("#folderPath").value = info.folder;
  renderFolderPlaceholder();
  $("#addPhotos").hidden = list.length === 0;
  if (state.geo && !state.geo.available && list.some((p) => p.gps)) notice(t("geo_hint"));
  $("#grid").innerHTML = "";
  for (const p of list) $("#grid").append(card(p));
  refreshAll();
}

function renderFolderPlaceholder() {
  $("#folderPath").placeholder = state.source === "files" ? t("picked_ph", { n: state.photos.size }) : t("folder_ph");
}

// ---------- cards ----------
function card(p) {
  const el = $("#cardTpl").content.firstElementChild.cloneNode(true);
  applyI18n(el);
  el.dataset.path = p.path;
  $("img", el).src = "/api/thumb?path=" + encodeURIComponent(p.path);
  $(".name", el).textContent = p.name;
  $(".name", el).title = p.path;
  $("img", el).addEventListener("click", () => openPreview(p.path));
  $(".sel", el).addEventListener("click", (e) => pick(p.path, e.target.checked, e.shiftKey));

  $(".category", el).addEventListener("change", (e) => edit(p.path, { category: e.target.value }));
  $(".caption", el).addEventListener("change", (e) => edit(p.path, { caption: e.target.value }));
  $(".tags", el).addEventListener("change", (e) => edit(p.path, { hashtags: parseTags(e.target.value) }));
  $(".reset", el).addEventListener("click", () => edit(p.path, { reset: true }));
  $(".copy", el).addEventListener("click", async (e) => {
    const cur = state.photos.get(p.path);
    await navigator.clipboard.writeText([cur.caption, tagText(cur.hashtags)].filter(Boolean).join("\n\n"));
    e.target.textContent = t("copied");
    e.target.classList.add("done");
    setTimeout(() => { e.target.textContent = t("copy"); e.target.classList.remove("done"); }, 1500);
  });
  fillCard(el, p);
  return el;
}

function fillCard(el, p) {
  const sel = $(".category", el);
  const done = p.status === "done";
  sel.innerHTML = "";
  if (!done) sel.add(new Option(t(p.status === "error" ? "failed" : "not_analyzed"), ""));
  for (const n of categoryNames()) sel.add(new Option(catLabel(n), n, false, n === p.category));
  // Result from a category that has since been renamed or removed: show it rather than a wrong fallback.
  if (done && !categoryNames().includes(p.category)) sel.add(new Option(t("old_cat", { name: p.category }), p.category, true, true));
  sel.disabled = !done;

  const badge = $(".badge", el);
  badge.className = "badge" + (p.status === "error" ? " error" : "");
  badge.textContent = p.status === "error" ? t("badge_error") : p.stale ? t("badge_outdated") : p.edited ? t("badge_edited") : "";
  badge.title = p.error || (p.stale ? t("stale_title") : "");

  const conf = $(".conf", el);
  conf.hidden = !done;
  conf.classList.toggle("low", done && p.confidence < 0.6);
  $("div", conf).style.width = done ? Math.round(p.confidence * 100) + "%" : "0";
  conf.title = done ? t("conf_title", { pct: Math.round(p.confidence * 100), cat: catLabel(p.model_category) }) : "";

  for (const [cls, val] of [[".caption", p.caption], [".tags", tagText(p.hashtags)]]) {
    const ta = $(cls, el);
    if (document.activeElement !== ta) ta.value = val;
    ta.disabled = !done;
  }
  $(".copy", el).disabled = !done;
  $(".reset", el).hidden = !p.edited;
  const loc = $(".loc", el);
  loc.hidden = !p.gps;
  loc.classList.toggle("nogeo", !!p.gps && !p.location);
  loc.textContent = p.gps ? "📍 " + (p.location ? placeLabel(p.location) : `${p.gps[0].toFixed(4)}, ${p.gps[1].toFixed(4)}`) : "";
  loc.title = !p.gps ? "" : p.location
    ? [p.location.landmark && t("near", { name: placeName(p.location.landmark) }), `GPS ${p.gps.join(", ")}`].filter(Boolean).join(" · ")
    : t("gps_only", { lat: p.gps[0], lon: p.gps[1] });
  el.hidden = state.filter !== null && p.category !== state.filter;
  const selected = state.selected.has(p.path);
  el.classList.toggle("selected", selected);
  $(".sel", el).checked = selected;
}

const placeName = (n) => (LANG === "yue" && n.zh ? n.zh : n.en);
const placeLabel = (loc) => (LANG === "yue" ? loc.label_zh : loc.label_en);

// ---------- selection ----------
function pick(path, on, range) {
  let paths = [path];
  if (range && state.lastPicked) {
    const order = visibleCards().map((c) => c.dataset.path);
    const [a, b] = [order.indexOf(state.lastPicked), order.indexOf(path)].sort((x, y) => x - y);
    if (a >= 0) paths = order.slice(a, b + 1);
  }
  for (const p of paths) on ? state.selected.add(p) : state.selected.delete(p);
  state.lastPicked = path;
  refreshAll();
}

$("#selAll").addEventListener("change", (e) => {
  for (const c of visibleCards()) e.target.checked ? state.selected.add(c.dataset.path) : state.selected.delete(c.dataset.path);
  refreshAll();
});
$("#selNone").addEventListener("click", () => { state.selected.clear(); refreshAll(); });
$("#removeSel").addEventListener("click", () => removeFromList(scopePaths()));
$("#clearAll").addEventListener("click", () => removeFromList(null));

async function removeFromList(paths) {
  try {
    const data = await api("/api/remove", { method: "POST", body: { paths } });
    setPhotos(data.photos, data, true);
    notice(paths ? t("removed", { n: data.removed }) : t("cleared"));
  } catch (err) {
    notice(err.message);
  }
}

function renderSelection() {
  const n = state.selected.size;
  const visible = visibleCards().map((c) => c.dataset.path);
  const picked = visible.filter((p) => state.selected.has(p)).length;
  $("#selbar").hidden = state.photos.size === 0;
  $("#selAll").checked = visible.length > 0 && picked === visible.length;
  $("#selAll").indeterminate = picked > 0 && picked < visible.length;
  $("#selText").textContent = n ? t("n_selected", { n }) : t("select_all");
  $("#selNone").hidden = n === 0;
  $("#removeSel").hidden = n === 0 || state.polling;
  $("#removeSel").textContent = t("remove_sel", { n });
  $("#clearAll").hidden = state.polling;
}

async function edit(path, body) {
  try {
    const rec = await api("/api/photo", { method: "PATCH", body: { path, ...body } });
    update([rec]);
  } catch (err) {
    notice(err.message);
  }
}

function update(records) {
  for (const r of records) {
    state.photos.set(r.path, r);
    const el = document.querySelector(`.card[data-path="${CSS.escape(r.path)}"]`);
    if (el) fillCard(el, r);
  }
  renderCategories();
  renderButtons();
}

function refreshAll() {
  for (const el of document.querySelectorAll(".card")) fillCard(el, state.photos.get(el.dataset.path));
  renderCategories();
  renderButtons();
}

// ---------- sidebar ----------
function renderCategories() {
  if (!state.config) return;
  const counts = {};
  for (const p of state.photos.values()) if (p.category) counts[p.category] = (counts[p.category] || 0) + 1;
  const list = $("#catList");
  list.innerHTML = "";
  const items = [[null, t("all_photos"), state.photos.size], ...categoryNames().map((n) => [n, catLabel(n), counts[n] || 0])];
  for (const [key, label, n] of items) {
    if (key === state.uncategorized && !n) continue;
    const li = document.createElement("li");
    li.className = state.filter === key ? "active" : "";
    li.innerHTML = `<span></span><span class="count">${n}</span>`;
    li.firstChild.textContent = label;
    li.addEventListener("click", () => { state.filter = key; refreshAll(); });
    list.append(li);
  }
  $("#empty").hidden = state.photos.size > 0;
}

function renderButtons() {
  const running = state.polling;
  const n = state.selected.size;
  const photos = scope();
  const analysed = photos.filter((p) => p.status === "done").length;
  const todo = photos.filter((p) => p.status !== "done" || p.stale).length;
  $("#analyze").disabled = running || !todo;
  $("#analyze").textContent = !photos.length ? t("analyze")
    : !todo ? t(n ? "sel_done" : "all_done")
    : n ? t("analyze_sel", { todo, n }) : t("analyze_n", { n: todo });
  $("#reanalyze").disabled = running || !analysed;
  $("#reanalyze").textContent = t(n ? "reanalyze_sel" : "reanalyze_all");
  $("#cancel").hidden = !running;
  $("#organize").disabled = running || !photos.length;  // sorting by location works before analysis
  $("#organize").textContent = n ? t("sort_sel", { n }) : t("sort");
  $("#sidecars").disabled = running || !analysed;
  $("#sidecars").textContent = n ? t("xmp_sel", { n }) : t("xmp");
  $("#csv").disabled = !analysed;
  $("#csv").textContent = n ? t("csv_sel", { n }) : t("csv");
  $("#choosePhotos").disabled = $("#addPhotos").disabled = $("#browse").disabled = running;
  renderSelection();
  $("#editCats").disabled = running;
  $("#model").disabled = running;
}

// ---------- batch job ----------
async function analyze(force) {
  const n = state.selected.size;
  if (force && !confirm(t("confirm_reanalyze", { n }))) return;
  try {
    await api("/api/analyze", { method: "POST", body: { force, paths: scopePaths() } });
    startPolling();
  } catch (err) {
    notice(err.message);
  }
}
$("#analyze").addEventListener("click", () => analyze(false));
$("#reanalyze").addEventListener("click", () => analyze(true));
$("#cancel").addEventListener("click", () => api("/api/cancel", { method: "POST" }));

function fmtTime(s) {
  if (s == null) return "";
  return s >= 60 ? t("left_m", { m: Math.floor(s / 60), s: s % 60 }) : t("left_s", { s });
}

async function startPolling() {
  if (state.polling) return;
  state.polling = true;
  $("#progressWrap").hidden = false;
  notice("");
  renderButtons();
  let job;
  try {
    while (true) {
      const data = await api("/api/job");
      job = data.job;
      update(data.updated);
      document.querySelectorAll(".card.working").forEach((c) => c.classList.remove("working"));
      const n = job.done + job.skipped + job.errors;
      $("#bar").style.width = job.total ? (100 * n) / job.total + "%" : "0";
      $("#progressText").textContent =
        `${n} / ${job.total}` + (job.current ? ` · ${job.current}` : "") +
        (job.eta_seconds != null ? ` · ${fmtTime(job.eta_seconds)}` : "");
      if (!["running", "cancelling"].includes(job.status)) break;
      await new Promise((r) => setTimeout(r, 800));
    }
  } finally {
    state.polling = false;
    renderButtons();
  }
  const parts = [t("done_analyzed", { n: job.done })];
  if (job.skipped) parts.push(t("done_cached", { n: job.skipped }));
  if (job.errors) parts.push(t("done_failed", { n: job.errors, err: job.last_error }));
  if (job.status === "failed") notice(t("batch_failed", { msg: job.message }));
  else notice(t(job.status === "cancelled" ? "cancelled" : "done") + parts.join(" · "));
}

// ---------- categories dialog ----------
function catRow(c = { name: "", description: "", hashtags: [] }) {
  const row = document.createElement("div");
  row.className = "catrow";
  row.innerHTML = `<input class="n" data-i18n-ph="cat_name_ph" required>
    <input class="d" data-i18n-ph="cat_desc_ph">
    <input class="h" data-i18n-ph="cat_tags_ph" spellcheck="false">
    <button type="button" data-i18n-title="remove">✕</button>`;
  applyI18n(row);
  $(".n", row).value = c.name;
  $(".d", row).value = c.description;
  $(".h", row).value = tagText(c.hashtags);
  $("button", row).addEventListener("click", () => row.remove());
  return row;
}

$("#editCats").addEventListener("click", () => {
  const s = state.config.settings;
  const rows = $("#catRows");
  rows.innerHTML = "";
  state.config.categories.forEach((c) => rows.append(catRow(c)));
  $("#maxTags").value = s.max_hashtags;
  $("#general").checked = s.include_general_hashtags;
  $("#minConf").value = s.min_confidence;
  const lang = $("#language");
  if (![...lang.options].some((o) => o.value === s.language)) lang.add(new Option(s.language, s.language));
  lang.value = s.language;
  $("#chineseTags").checked = s.chinese_hashtags;
  $("#locTags").checked = s.location_tags;
  $("#lmTags").checked = s.landmark_tags;
  renderGeo();
  $("#imageSize").value = s.image_size;
  $("#catDialog").showModal();
});
for (const b of document.querySelectorAll(".starter")) {
  b.addEventListener("click", () => {
    if (!confirm(t("starter_confirm"))) return;
    $("#catRows").innerHTML = "";
    for (const c of state.starters[b.dataset.set] || []) $("#catRows").append(catRow(c));
    if (b.dataset.set === "yue") $("#chineseTags").checked = true;
  });
}
$("#addCat").addEventListener("click", () => { const r = catRow(); $("#catRows").append(r); $(".n", r).focus(); });

$("#catForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const cfg = structuredClone(state.config);
  cfg.categories = [...document.querySelectorAll(".catrow")].map((r) => ({
    name: $(".n", r).value.trim(), description: $(".d", r).value.trim(), hashtags: parseTags($(".h", r).value),
  })).filter((c) => c.name);
  Object.assign(cfg.settings, {
    max_hashtags: Math.min(30, Math.max(1, +$("#maxTags").value || 20)),
    include_general_hashtags: $("#general").checked,
    min_confidence: Math.min(1, Math.max(0, +$("#minConf").value || 0)),
    language: $("#language").value,
    chinese_hashtags: $("#chineseTags").checked,
    location_tags: $("#locTags").checked,
    landmark_tags: $("#lmTags").checked,
    image_size: Math.min(4096, Math.max(512, +$("#imageSize").value || 1920)),
  });
  try {
    state.config = await api("/api/config", { method: "PUT", body: cfg });
    $("#catDialog").close();
    await loadPhotos();
  } catch (err) {
    alert(err.message);
  }
});

// ---------- place data (offline reverse geocoding) ----------
function renderGeo() {
  const g = state.geo;
  if (!g) return;
  const d = g.download;
  $("#geoStatus").textContent = d.status === "running" ? t("geo_running", { step: d.step })
    : d.status === "failed" ? t("geo_failed", { err: d.error })
    : g.available ? t("geo_ready", { countries: g.info.countries.join(" "), built: g.info.built })
    : t("geo_none");
  $("#geoDownload").textContent = t(g.available ? "geo_update" : "geo_download");
  $("#geoDownload").disabled = d.status === "running";
  if (document.activeElement !== $("#geoCountries"))
    $("#geoCountries").value = (g.info ? g.info.countries : g.default_countries).join(" ");
}

$("#geoDownload").addEventListener("click", async () => {
  const countries = $("#geoCountries").value.split(/[\s,]+/).filter(Boolean);
  try {
    state.geo = await api("/api/geo/download", { method: "POST", body: { countries } });
    renderGeo();
    while (state.geo.download.status === "running") {
      await new Promise((r) => setTimeout(r, 1000));
      state.geo = await api("/api/geo");
      renderGeo();
    }
    if (state.geo.available) await loadPhotos();
  } catch (err) {
    alert(err.message);
  }
});

// ---------- local data ----------
function fmtBytes(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1e3 ? `${Math.round(n / 1e3)} KB` : `${n} B`;
}

const CLEAR_ITEMS = [["results", "#clrResults", "data_results"], ["thumbs", "#clrThumbs", "data_thumbs"],
  ["settings", "#clrSettings", "data_settings"], ["geo", "#clrGeo", "data_geo"]];

async function renderData() {
  const d = await api("/api/data");
  $("#dataHome").textContent = d.home;
  $("#dataResults").textContent = t("data_results_info", { ...d.results, size: fmtBytes(d.results.bytes) });
  $("#dataThumbs").textContent = t("data_thumbs_info", { files: d.thumbs.files, size: fmtBytes(d.thumbs.bytes) });
  $("#dataSettings").textContent = t("data_settings_info", { n: d.settings.categories });
  $("#dataGeo").textContent = d.geo.available ? t("data_geo_info", { size: fmtBytes(d.geo.bytes) }) : t("data_geo_none");
  $("#clrGeo").disabled = !d.geo.available;
  if (!d.geo.available) $("#clrGeo").checked = false;
}

$("#openData").addEventListener("click", async () => {
  try {
    await renderData();
    $("#dataDialog").showModal();
  } catch (err) {
    notice(err.message);
  }
});

$("#clearData").addEventListener("click", async () => {
  const body = Object.fromEntries(CLEAR_ITEMS.map(([k, sel]) => [k, $(sel).checked]));
  const what = CLEAR_ITEMS.filter(([k]) => body[k]).map(([, , label]) => t(label)).join(LANG === "yue" ? "、" : ", ");
  if (!what || !confirm(t("confirm_clear", { what }))) return;
  try {
    await api("/api/data/clear", { method: "POST", body });
    $("#dataDialog").close();
    await loadStatus();
    await loadPhotos();
    notice(t("data_cleared", { what }));
  } catch (err) {
    alert(err.message);
  }
});

// ---------- apply ----------
const ORG_PREFS = "photo-insight.sort";

function sortOptions() {
  return { group: $("#orgGroup").value, level: $("#orgLevel").value, names: $("#orgNames").value,
    embed: $("#orgEmbed").checked && !$("#orgEmbed").disabled };
}

function renderEmbed() {
  const move = document.querySelector('input[name="mode"]:checked').value === "move";
  const box = $("#orgEmbed");
  box.disabled = move || !state.exiftool;
  $("#embedHint").textContent = !state.exiftool ? t("embed_missing") : move ? t("embed_move") : t("embed_hint");
}
for (const r of document.querySelectorAll('input[name="mode"]')) r.addEventListener("change", renderEmbed);
$("#orgEmbed").addEventListener("change", () => {
  try { localStorage.setItem(ORG_PREFS, JSON.stringify(sortOptions())); } catch {}
});

async function renderPlan() {
  const opts = sortOptions();
  for (const el of document.querySelectorAll("#orgDialog .locopt")) el.hidden = opts.group === "category";
  try { localStorage.setItem(ORG_PREFS, JSON.stringify(opts)); } catch {}
  const plan = await api("/api/organize/plan", { method: "POST", body: { ...opts, paths: scopePaths() } });
  const list = $("#orgPlan");
  list.innerHTML = "";
  const MAX = 30;
  for (const [folder, n] of plan.folders.slice(0, MAX)) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><span class="count">${n}</span>`;
    li.firstChild.textContent = "📁 " + folder;
    list.append(li);
  }
  if (plan.folders.length > MAX) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = t("plan_more", { n: plan.folders.length - MAX });
    list.append(li);
  }
  if (!plan.folders.length) list.innerHTML = `<li class="muted">${t("plan_empty")}</li>`;
  $("#orgSkipped").hidden = !plan.skipped || !plan.folders.length;
  $("#orgSkipped").textContent = t("plan_skipped", { n: plan.skipped });
  $("#orgGo").disabled = !plan.folders.length;
}

for (const id of ["#orgGroup", "#orgLevel", "#orgNames"]) $(id).addEventListener("change", () => renderPlan().catch((e) => notice(e.message)));

$("#organize").addEventListener("click", async () => {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(ORG_PREFS)); } catch {}
  $("#orgGroup").value = saved?.group || "category";
  $("#orgLevel").value = saved?.level || "city";
  $("#orgNames").value = saved?.names || (LANG === "yue" ? "zh" : "en");
  $("#orgEmbed").checked = saved?.embed ?? state.exiftool;
  renderEmbed();
  $("#orgDialog").showModal();
  renderPlan().catch((e) => notice(e.message));
});
$("#orgDialog").addEventListener("close", async () => {
  if ($("#orgDialog").returnValue !== "go") return;
  const mode = document.querySelector('input[name="mode"]:checked').value;
  if (mode === "move" && !confirm(t("confirm_move"))) return;
  try {
    const r = await api("/api/organize", {
      method: "POST", body: { mode, dest: $("#dest").value.trim() || null, paths: scopePaths(), ...sortOptions() },
    });
    const e = r.embedded;
    notice(t(mode === "copy" ? "copied_to" : "moved_to", { n: r.count, dest: r.dest }) +
      (r.skipped ? t("sort_skipped", { n: r.skipped }) : "") +
      (e ? t("embedded", { n: e.embedded }) + (e.unsupported ? t("embed_unsupported", { n: e.unsupported }) : "") +
        (e.failed ? t("embed_failed", { n: e.failed, err: e.errors.join("; ") }) : "") : ""));
    if (mode === "move") await loadPhotos();
  } catch (err) {
    notice(err.message);
  }
});

$("#sidecars").addEventListener("click", async () => {
  try {
    const r = await api("/api/sidecars", { method: "POST", body: { paths: scopePaths() } });
    notice(t("xmp_done", { n: r.written }) + (r.skipped_existing ? t("xmp_skipped", { n: r.skipped_existing }) : ""));
  } catch (err) {
    notice(err.message);
  }
});

$("#csv").addEventListener("click", async () => {
  try {
    const csv = await api("/api/export.csv", { method: "POST", body: { paths: scopePaths() } });
    const a = Object.assign(document.createElement("a"), {
      // fetch() strips the BOM; put it back so Excel reads Chinese text as UTF-8
      href: URL.createObjectURL(new Blob(["\ufeff", csv], { type: "text/csv" })), download: "photo-insight.csv",
    });
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    notice(err.message);
  }
});

// ---------- preview ----------
let previewPath = null;

function openPreview(path) {
  previewPath = path;
  const p = state.photos.get(path);
  const order = visibleCards().map((c) => c.dataset.path);
  const i = order.indexOf(path);
  $("#pvImg").src = "/api/thumb?size=1600&path=" + encodeURIComponent(path);
  $("#pvName").textContent = p.name;
  $("#pvPos").textContent = `${i + 1} / ${order.length}`;
  $("#pvMeta").textContent = p.status === "done"
    ? `${catLabel(p.category)} · ${t("pv_conf", { pct: Math.round(p.confidence * 100) })} · ${p.path}`
    : `${p.status === "error" ? t("pv_failed", { err: p.error }) : t("not_analyzed")} · ${p.path}`;
  if (p.location) {
    $("#pvMeta").textContent += " · 📍 " + placeLabel(p.location) +
      (p.location.landmark ? ` (${t("near", { name: placeName(p.location.landmark) })})` : "");
  }
  $("#pvCaption").textContent = p.caption || "";
  $("#pvTags").textContent = tagText(p.hashtags);
  $("#pvSel").checked = state.selected.has(path);
  $("#pvPrev").disabled = i <= 0;
  $("#pvNext").disabled = i >= order.length - 1;
  if (!$("#preview").open) $("#preview").showModal();
}

function stepPreview(delta) {
  const order = visibleCards().map((c) => c.dataset.path);
  const next = order[order.indexOf(previewPath) + delta];
  if (next) openPreview(next);
}

$("#pvPrev").addEventListener("click", () => stepPreview(-1));
$("#pvNext").addEventListener("click", () => stepPreview(1));
$("#pvClose").addEventListener("click", () => $("#preview").close());
$("#pvSel").addEventListener("change", (e) => pick(previewPath, e.target.checked, false));
$("#preview").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
$("#preview").addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") stepPreview(-1);
  else if (e.key === "ArrowRight") stepPreview(1);
  else if (e.key === " " && e.target.id !== "pvSel") {
    e.preventDefault();
    $("#pvSel").checked = !$("#pvSel").checked;
    pick(previewPath, $("#pvSel").checked, false);
  } else return;
});

// ---------- language ----------
$("#uiLang").value = LANG;
$("#uiLang").addEventListener("change", (e) => {
  setLang(e.target.value);
  renderFolderPlaceholder();
  refreshAll();
  if (LANG === "yue" && state.config && state.config.settings.language === "English") notice(t("caption_hint"));
});

setLang(LANG);
loadStatus().catch((err) => notice(err.message));
