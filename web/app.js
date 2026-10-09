"use strict";

// Everything that came from disk (paths, file names, git remotes) goes through
// esc() before it reaches innerHTML. The page can call the Python bridge, so a
// crafted file name must never become markup.
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const $ = (id) => document.getElementById(id);

const ACTION_TEXT = { cache_clean: "直接清理", recycle_suggest: "进回收站", advice_only: "仅建议", readonly: "只读" };
const RISK_TEXT = { low: "低风险", medium: "需留意", high: "别删" };
const PROJECT_TYPE = { node: "Node", python: "Python", rust: "Rust", go: "Go", java: "Java", dotnet: ".NET", git: "Git 仓库", other: "其他" };

const state = {
  disks: [],
  categories: {},
  findings: [],
  projects: [],
  advice: [],
  trees: [],
  scans: [],
  caches: [],
  filter: "all",
  treePath: [],
  projectSort: "regen",
  selectedProject: null,
  scanned: false,
  exported: false,
  busy: false,
};

function bytes(n) {
  const value = Number(n) || 0;
  if (value < 1024) return value + " B";
  if (value < 1024 ** 2) return (value / 1024).toFixed(1) + " KB";
  if (value < 1024 ** 3) return (value / 1024 ** 2).toFixed(1) + " MB";
  return (value / 1024 ** 3).toFixed(value >= 100 * 1024 ** 3 ? 0 : 1) + " GB";
}

const api = () => window.pywebview && window.pywebview.api;

function toast(text) {
  const node = $("toast");
  node.textContent = text;
  node.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 4200);
}

function setBusy(on, text) {
  state.busy = on;
  $("jobbar").classList.toggle("hidden", !on);
  if (text) $("job-text").textContent = text;
  ["scan-btn", "finding-run-btn", "cache-run-btn", "cache-scan-btn", "export-btn"].forEach((id) => { $(id).disabled = on; });
}

function empty(title, body) {
  return `<div class="empty"><strong>${esc(title)}</strong>${esc(body)}</div>`;
}

// ---------------------------------------------------------------- navigation
document.querySelectorAll(".nav-btn").forEach((button) => button.addEventListener("click", () => show(button.dataset.page)));
document.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => show(button.dataset.goto)));

function show(page) {
  document.querySelectorAll(".page").forEach((node) => node.classList.toggle("hidden", node.id !== page));
  document.querySelectorAll(".nav-btn").forEach((node) => node.classList.toggle("active", node.dataset.page === page));
  if (page === "perf") refreshProcesses();
  if (page === "settings") loadSettings();
  if (page === "projects") renderProjects();
  if (page === "clean" && !state.caches.length && !state.busy) startCacheScan();
  if (page === "storage") renderStorage();
}

// ---------------------------------------------------------------- overview
function renderOverview() {
  renderFreshness();
  const steps = [
    { done: state.scanned, title: "盘查", body: "到「存储盘查」选盘符，看每个盘存了什么。" },
    { done: state.findings.some((item) => item.selectable && item.selected) || false, title: "勾选处理", body: "缓存直接清理，安装包和重复文件进回收站。" },
    { done: state.exported, title: "交给 AI", body: "在「项目」导出报告，让 agent 判断项目里的可删目录。" },
  ];
  $("guide").innerHTML = steps.map((step, index) => `
    <div class="step ${step.done ? "done" : ""}"><b>${step.done ? "✓" : index + 1}</b><div><strong>${esc(step.title)}</strong><span>${esc(step.body)}</span></div></div>`).join("");

  $("disk-cards").innerHTML = state.disks.map(diskCard).join("") || empty("没有读到磁盘", "请稍后重试。");

  const entries = Object.entries(state.categories).filter(([, size]) => size > 0).sort((a, b) => b[1] - a[1]).slice(0, 7);
  const max = entries.length ? entries[0][1] : 1;
  $("category-bars").innerHTML = entries.length
    ? `<div class="bars">${entries.map(([name, size]) => `
        <div class="bar-row"><span>${esc(name)}</span><div class="meter"><span style="width:${(size / max * 100).toFixed(1)}%"></span></div><span class="val">${bytes(size)}</span></div>`).join("")}</div>`
    : empty("还没有盘查", "盘查之后，这里会列出视频、压缩包、程序等各占多少。");

  const advice = state.advice.length ? state.advice : (state.tips || []).map((tip) => ({ title: "提示", detail: tip, severity: "info" }));
  $("advice-list").innerHTML = advice.map((item) => `
    <div class="advice ${esc(item.severity)}"><div><strong>${esc(item.title)}</strong><span>${esc(item.detail)}</span></div></div>`).join("") || empty("暂无建议", "盘查后会给出每个盘的建议。");
}

const STALE_DAYS = 7;

function ageText(iso) {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return { text: "时间未知", days: 0 };
  const minutes = Math.max(0, Math.floor((Date.now() - then.getTime()) / 60000));
  const days = Math.floor(minutes / 1440);
  if (minutes < 1) return { text: "刚刚", days };
  if (minutes < 60) return { text: `${minutes} 分钟前`, days };
  if (minutes < 1440) return { text: `${Math.floor(minutes / 60)} 小时前`, days };
  return { text: `${days} 天前`, days };
}

function renderFreshness() {
  const hosts = [$("freshness"), $("freshness-storage")];
  if (!state.scans.length) { hosts.forEach((host) => host.classList.add("hidden")); return; }
  const oldest = state.scans.map((scan) => scan.scanned_at).sort()[0];
  const age = ageText(oldest);
  const stale = age.days >= STALE_DAYS;
  const drives = state.scans.map((scan) => scan.root.replace(/\\$/, "")).join("、");
  const note = stale ? "文件可能已变化，建议重新盘查。" : "下面显示的是这次盘查的结果。";
  hosts.forEach((host) => {
    host.classList.remove("hidden");
    host.classList.toggle("stale", stale);
    host.innerHTML = `<span>上次盘查 <strong>${esc(age.text)}</strong>（${esc(drives)}）。${esc(note)}</span><button class="primary" data-rescan>重新盘查</button>`;
    host.querySelector("[data-rescan]").onclick = rescan;
  });
}

async function rescan() {
  const drives = state.scans.map((scan) => scan.root.charAt(0));
  if (!drives.length) { show("storage"); return; }
  const result = await api().start_scan(drives, $("dup-toggle").checked);
  if (!result.ok) { toast(result.error); return; }
  setBusy(true, "正在重新盘查，范围与上次相同");
}

function diskCard(disk) {
  const level = disk.percent >= 90 ? "danger" : disk.percent >= 80 ? "warn" : "";
  return `<article class="disk">
    <div class="disk-top"><span class="disk-letter">${esc(disk.drive)}: 盘</span><span class="muted">${esc(disk.percent)}% 已用</span></div>
    <div class="disk-free">${esc(disk.free_gb)} GB<small>可用 / 共 ${esc(disk.total_gb)} GB</small></div>
    <div class="meter ${level}"><span style="width:${Number(disk.percent) || 0}%"></span></div>
  </article>`;
}

function renderDrivePicks() {
  const box = $("drive-picks");
  const checked = new Set([...box.querySelectorAll("input:checked")].map((node) => node.value));
  const initial = !box.childElementCount;
  box.innerHTML = state.disks.map((disk) => {
    const on = initial ? disk.drive === "C" : checked.has(disk.drive);
    return `<label class="chip"><input type="checkbox" value="${esc(disk.drive)}" ${on ? "checked" : ""}/><span>${esc(disk.drive)}: 盘</span></label>`;
  }).join("");
}

// ---------------------------------------------------------------- storage
function renderStorage() {
  renderDrivePicks();
  renderTree();
  const filters = ["all", "cache_clean", "recycle_suggest", "advice_only", "readonly"];
  const counts = Object.fromEntries(filters.map((key) => [key, key === "all" ? state.findings.length : state.findings.filter((item) => item.action === key).length]));
  $("filters").innerHTML = filters.map((key) => `<button role="tab" aria-selected="${state.filter === key}" class="${state.filter === key ? "active" : ""}" data-filter="${key}">${key === "all" ? "全部" : ACTION_TEXT[key]} ${counts[key]}</button>`).join("");
  $("filters").querySelectorAll("button").forEach((button) => { button.onclick = () => { state.filter = button.dataset.filter; renderStorage(); }; });

  const rows = state.findings.filter((item) => state.filter === "all" || item.action === state.filter).sort((a, b) => b.size - a.size);
  $("finding-count").textContent = state.findings.length ? `共 ${state.findings.length} 条` : "";
  $("finding-list").innerHTML = rows.length ? rows.map(findingRow).join("") : (state.scanned ? empty("这一类没有发现", "换个分类看看。") : empty("还没有盘查", "选好盘符，点右上角「开始盘查」。"));
  $("finding-list").querySelectorAll("input[data-id]").forEach((box) => {
    box.onchange = () => {
      const found = state.findings.find((item) => item.id === box.dataset.id);
      if (found) found.selected = box.checked;
      updateSelectedSum();
    };
  });
  updateSelectedSum();
}

function findingRow(item) {
  const control = item.selectable
    ? `<input type="checkbox" data-id="${esc(item.id)}" ${item.selected ? "checked" : ""} aria-label="勾选 ${esc(item.label)}"/>`
    : `<svg class="lock" aria-label="不可执行"><use href="#i-shield"/></svg>`;
  return `<li class="finding">
    ${control}
    <div>
      <div class="finding-title"><strong>${esc(item.label)}</strong><span class="badge ${esc(item.action)}">${esc(ACTION_TEXT[item.action] || item.action)}</span></div>
      <p>${esc(item.reason)} ${esc(item.suggestion)}</p>
      <div class="path">${esc(item.path)}</div>
    </div>
    <span class="size">${bytes(item.size)}</span>
  </li>`;
}

function updateSelectedSum() {
  const chosen = state.findings.filter((item) => item.selectable && item.selected);
  $("selected-sum").textContent = chosen.length ? `已勾选 ${chosen.length} 项，约 ${bytes(chosen.reduce((sum, item) => sum + item.size, 0))}` : "没有勾选";
  $("finding-run-btn").disabled = state.busy || !chosen.length;
}

function currentTreeNode() {
  let node = { name: "全部", children: state.trees };
  for (const index of state.treePath) node = (node.children || [])[index] || node;
  return node;
}

function renderTree() {
  const node = currentTreeNode();
  const children = (state.treePath.length ? node.children : state.trees.length === 1 ? state.trees[0].children : state.trees) || [];
  const crumbs = ["全部"];
  let walk = { children: state.trees };
  for (const index of state.treePath) { walk = walk.children[index]; crumbs.push(walk.name); }
  $("tree-crumb").textContent = state.treePath.length ? crumbs.join(" / ") : (state.trees.length === 1 ? state.trees[0].path : "");
  $("tree-back").classList.toggle("hidden", !state.treePath.length);
  const host = $("treemap");
  if (!children.length) {
    host.innerHTML = empty(state.scanned ? "这一层没有更细的目录" : "还没有盘查", state.scanned ? "返回上一层看看。" : "盘查完成后按目录大小画出色块。");
    return;
  }
  const width = host.clientWidth || 600;
  const height = host.clientHeight || 360;
  const items = children.map((child, index) => ({ ...child, index })).filter((child) => child.size > 0);
  const rects = layoutTreemap(items, 0, 0, width, height);
  const palette = ["#3b5bdb", "#1c7ed6", "#0c8599", "#2b8a3e", "#e67700", "#5f3dc4", "#c2255c", "#495057"];
  host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="目录占用图">${rects.map((rect, order) => {
    const fill = rect.protected === "system" ? "#868e96" : rect.protected === "project" ? "#7048e8" : palette[order % palette.length];
    const label = rect.w > 80 && rect.h > 40;
    return `<g data-index="${rect.index}"><title>${esc(rect.name)} ${bytes(rect.size)}</title>
      <rect x="${rect.x + 1}" y="${rect.y + 1}" width="${Math.max(rect.w - 2, 0)}" height="${Math.max(rect.h - 2, 0)}" rx="6" fill="${fill}"></rect>
      ${label ? `<text x="${rect.x + 10}" y="${rect.y + 22}">${esc(truncate(rect.name, rect.w))}</text><text class="sub" x="${rect.x + 10}" y="${rect.y + 38}">${bytes(rect.size)}</text>` : ""}
    </g>`;
  }).join("")}</svg>`;
  host.querySelectorAll("g[data-index]").forEach((group) => {
    group.onclick = () => {
      const index = Number(group.dataset.index);
      const target = children[index];
      if (!target || !(target.children || []).length) { toast(`${target ? target.name : ""} 没有更细的子目录记录`); return; }
      if (!state.treePath.length && state.trees.length === 1) state.treePath.push(0);
      state.treePath.push(index);
      renderTree();
    };
  });
}

function truncate(text, width) {
  const max = Math.max(4, Math.floor((width - 20) / 8));
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

function layoutTreemap(items, x, y, w, h) {
  const data = items.slice().sort((a, b) => b.size - a.size);
  const out = [];
  (function split(list, left, top, width, height) {
    if (!list.length || width < 1 || height < 1) return;
    if (list.length === 1) { out.push({ ...list[0], x: left, y: top, w: width, h: height }); return; }
    const total = list.reduce((sum, item) => sum + item.size, 0);
    let used = 0;
    let cut = 1;
    for (let index = 0; index < list.length - 1; index += 1) {
      used += list[index].size;
      cut = index + 1;
      if (used >= total / 2) break;
    }
    const frac = list.slice(0, cut).reduce((sum, item) => sum + item.size, 0) / total;
    if (width >= height) {
      split(list.slice(0, cut), left, top, width * frac, height);
      split(list.slice(cut), left + width * frac, top, width * (1 - frac), height);
    } else {
      split(list.slice(0, cut), left, top, width, height * frac);
      split(list.slice(cut), left, top + height * frac, width, height * (1 - frac));
    }
  })(data, x, y, w, h);
  return out;
}

// ---------------------------------------------------------------- projects
const regen = (project) => Object.values(project.regenerable || {}).reduce((sum, size) => sum + size, 0);
const idleDays = (project) => (project.source_mtime ? Math.floor((Date.now() / 1000 - project.source_mtime) / 86400) : null);

function sortedProjects() {
  const key = state.projectSort;
  return state.projects.slice().sort((a, b) => {
    if (key === "size") return b.size - a.size;
    if (key === "idle") return (idleDays(b) ?? -1) - (idleDays(a) ?? -1);
    return regen(b) - regen(a);
  });
}

function renderProjects() {
  const rows = sortedProjects();
  $("project-list").innerHTML = rows.length ? rows.map((project, index) => {
    const share = project.size ? Math.min(100, (regen(project) / project.size) * 100) : 0;
    const idle = idleDays(project);
    return `<li class="project ${state.selectedProject === project.path ? "active" : ""}" data-index="${index}" tabindex="0">
      <div><strong>${esc(project.name)}</strong> <span class="badge ${esc(project.risk)}">${esc(RISK_TEXT[project.risk] || project.risk)}</span></div>
      <span class="size">${bytes(project.size)}</span>
      <div class="meta">${esc(PROJECT_TYPE[project.project_type] || project.project_type)} · 可再生 ${bytes(regen(project))}${idle !== null ? ` · ${idle} 天未改` : ""}</div>
      <div class="split-meter" aria-hidden="true"><i style="width:${100 - share}%"></i><i class="regen" style="width:${share}%"></i></div>
    </li>`;
  }).join("") : empty(state.scanned ? "没有识别到项目" : "还没有盘查", state.scanned ? "含 package.json、pyproject.toml、.sln 或 .git 的文件夹会算作项目。" : "先到「存储盘查」盘查一次。");
  $("project-list").querySelectorAll(".project").forEach((row) => {
    const pick = () => { state.selectedProject = rows[Number(row.dataset.index)].path; renderProjects(); };
    row.onclick = pick;
    row.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); } };
  });
  const chosen = rows.find((project) => project.path === state.selectedProject) || rows[0];
  $("project-detail").innerHTML = chosen ? projectDetail(chosen) : empty("选择一个项目", "左侧点一个项目查看细节。");
}

function projectDetail(project) {
  const git = project.git || {};
  const dirty = git.dirty === true ? "有未提交改动" : git.dirty === false ? "干净" : "未知";
  const regenRows = Object.entries(project.regenerable || {}).sort((a, b) => b[1] - a[1]);
  const subs = project.subprojects || [];
  return `<div class="card-head"><h2>${esc(project.name)}</h2><span class="badge ${esc(project.risk)}">${esc(RISK_TEXT[project.risk] || project.risk)}</span></div>
    <p class="muted">${esc(project.risk_note)}</p>
    <dl class="detail-grid">
      <dt>路径</dt><dd>${esc(project.path)}</dd>
      <dt>类型</dt><dd>${esc(PROJECT_TYPE[project.project_type] || project.project_type)}</dd>
      <dt>大小</dt><dd>${bytes(project.size)}，${esc(project.file_count)} 个文件</dd>
      <dt>远程仓库</dt><dd>${esc(git.remote || "无")}</dd>
      <dt>最近提交</dt><dd>${esc(git.last_commit || "未知")}</dd>
      <dt>工作区</dt><dd>${esc(dirty)}</dd>
    </dl>
    <h2>可再生目录</h2>
    ${regenRows.length ? `<ul class="regen-list">${regenRows.map(([name, size]) => `<li><span>${esc(name)}</span><span class="size">${bytes(size)}</span></li>`).join("")}</ul>` : `<p class="muted">没有发现 node_modules、dist 这类可以重建的目录。</p>`}
    ${subs.length ? `<h2 class="gap">子项目 ${subs.length} 个</h2><ul class="regen-list">${subs.map((sub) => `<li><span>${esc(sub.name)}</span><span class="size">${bytes(sub.size)}</span></li>`).join("")}</ul>` : ""}`;
}

// ---------------------------------------------------------------- clean
function renderCaches() {
  $("cache-list").innerHTML = state.caches.length ? state.caches.map((item) => {
    const action = item.level === "safe" ? "cache_clean" : item.level === "advice" ? "advice_only" : "readonly";
    const control = item.level === "safe"
      ? `<input type="checkbox" data-id="${esc(item.id)}" ${item.selected ? "checked" : ""} aria-label="勾选 ${esc(item.label)}"/>`
      : `<svg class="lock" aria-label="不可执行"><use href="#i-shield"/></svg>`;
    return `<li class="finding">${control}<div><div class="finding-title"><strong>${esc(item.label)}</strong><span class="badge ${action}">${esc(ACTION_TEXT[action])}</span></div><p>${esc(item.description)}</p></div><span class="size">${bytes(item.size)}</span></li>`;
  }).join("") : empty("正在准备", "稍等，正在统计缓存大小。");
  $("cache-list").querySelectorAll("input[data-id]").forEach((box) => {
    box.onchange = () => { const found = state.caches.find((item) => item.id === box.dataset.id); if (found) found.selected = box.checked; };
  });
}

async function startCacheScan() {
  const result = await api().start_cache_scan();
  if (!result.ok) toast(result.error);
  else setBusy(true, "正在统计缓存大小");
}

// ---------------------------------------------------------------- perf / settings
async function refreshProcesses() {
  const payload = await api().get_processes();
  const snap = payload.snapshot || {};
  $("perf-metrics").innerHTML = [
    { label: "CPU", value: `${Number(snap.cpu_percent || 0).toFixed(0)}%`, pct: snap.cpu_percent },
    { label: "内存", value: `${Number(snap.memory_percent || 0).toFixed(0)}%`, pct: snap.memory_percent, sub: `${snap.memory_used_gb ?? 0} / ${snap.memory_total_gb ?? 0} GB` },
  ].map((item) => `<article class="disk"><div class="disk-top"><span class="disk-letter">${item.label}</span><span class="muted">${esc(item.sub || "")}</span></div><div class="disk-free">${item.value}</div><div class="meter ${item.pct >= 90 ? "danger" : item.pct >= 75 ? "warn" : ""}"><span style="width:${Number(item.pct) || 0}%"></span></div></article>`).join("");
  $("memory-advice").innerHTML = (payload.advice || []).slice(0, 3).map((item) => `<div class="advice ${esc(item.severity)}"><div><strong>${esc(item.title)}</strong><span>${esc(item.detail)}</span></div></div>`).join("");
  $("proc-rows").innerHTML = (snap.processes || []).map((proc) => `<tr>
    <td>${proc.can_kill ? `<input type="checkbox" data-pid="${esc(proc.pid)}" aria-label="勾选 ${esc(proc.name)}"/>` : ""}</td>
    <td>${esc(proc.name)}${proc.suspicious ? ' <span class="badge medium">占用偏高</span>' : ""}</td>
    <td class="num">${Number(proc.cpu_percent).toFixed(1)}%</td>
    <td class="num">${Math.round(proc.memory_mb)} MB</td>
    <td>${proc.can_kill ? "可以" : esc(proc.kill_block_reason)}</td></tr>`).join("");
}

async function loadSettings() {
  const payload = await api().get_settings();
  $("protected-paths").value = (payload.protected_paths || []).join("\n");
  $("proc-whitelist").value = (payload.process_whitelist || []).join("\n");
  const rows = []
    .concat((payload.storage_history || []).map((item) => ({ kind: "盘查处理", ...item })))
    .concat((payload.clean_history || []).map((item) => ({ kind: "缓存清理", ...item })))
    .sort((a, b) => String(b.time).localeCompare(String(a.time)));
  $("history").innerHTML = rows.length ? rows.map((item) => `<li><strong>${esc(item.kind)}</strong> · ${esc(String(item.time || "").replace("T", " "))} · ${esc(item.summary)}</li>`).join("") : `<li class="muted">还没有记录</li>`;
}

// ---------------------------------------------------------------- dialogs and events
function ask(title, html) {
  $("confirm-title").textContent = title;
  $("confirm-text").innerHTML = html;
  const dialog = $("confirm");
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true }));
}

window.onProgress = (payload) => {
  if (payload.type === "progress") { setBusy(true, payload.message); return; }
  if (payload.type !== "done") return;
  setBusy(false);
  if (!payload.ok) { toast("出错：" + (payload.error || "未知错误")); return; }
  const result = payload.result || {};
  if (payload.job === "scan") { state.treePath = []; hydrate().then(() => toast(`盘查完成：${result.findings ?? 0} 条发现，${result.projects ?? 0} 个项目`)); }
  if (payload.job === "cache") api().get_cache_targets().then((data) => { state.caches = data.targets || []; renderCaches(); });
  if (payload.job === "clean") { toast(`缓存清理完成，释放约 ${result.freed}`); startCacheScan(); refreshDisks(); }
  if (payload.job === "execute") {
    toast(`处理完成：成功 ${result.ok_count ?? 0} 项，跳过 ${result.fail_count ?? 0} 项，释放约 ${result.freed}`);
    hydrate();
  }
};

async function refreshDisks() {
  const overview = await api().get_overview();
  state.disks = overview.disks || [];
  state.tips = overview.tips || [];
  renderOverview();
}

async function hydrate() {
  const data = await api().get_results();
  state.findings = data.findings || [];
  state.categories = data.categories || {};
  state.advice = data.advice || [];
  state.trees = data.trees || [];
  state.scans = data.scans || [];
  state.projects = (await api().get_projects()).projects || [];
  state.scanned = state.trees.length > 0 || state.findings.length > 0;
  await refreshDisks();
  renderStorage();
  renderProjects();
}

$("scan-btn").onclick = async () => {
  const drives = [...document.querySelectorAll("#drive-picks input:checked")].map((node) => node.value);
  if (!drives.length) { toast("请至少选择一个盘"); return; }
  const result = await api().start_scan(drives, $("dup-toggle").checked);
  if (!result.ok) { toast(result.error); return; }
  setBusy(true, "正在盘查，C 盘一般需要几分钟");
};
$("job-cancel").onclick = () => api().cancel_scan();
$("tree-back").onclick = () => {
  state.treePath.pop();
  if (state.treePath.length === 1 && state.trees.length === 1) state.treePath = [];
  renderTree();
};
$("project-sort").onchange = (event) => { state.projectSort = event.target.value; renderProjects(); };

$("finding-run-btn").onclick = async () => {
  const chosen = state.findings.filter((item) => item.selected && item.selectable);
  if (!chosen.length) return;
  const caches = chosen.filter((item) => item.action === "cache_clean");
  const recycle = chosen.filter((item) => item.action === "recycle_suggest");
  const lines = [];
  if (caches.length) lines.push(`<li>直接删除 ${caches.length} 个缓存，约 ${bytes(caches.reduce((s, i) => s + i.size, 0))}，不能还原</li>`);
  if (recycle.length) lines.push(`<li>移入回收站 ${recycle.length} 项，约 ${bytes(recycle.reduce((s, i) => s + i.size, 0))}，可以还原</li>`);
  const ok = await ask("确认处理", `<ul>${lines.join("")}</ul><p class="hint" style="margin-top:12px">执行前会再检查一遍，系统文件和项目目录会被拦下。回收站放不下的文件会跳过，不会被永久删除。</p>`);
  if (!ok) return;
  const result = await api().execute(chosen.map((item) => item.id));
  if (!result.ok) toast(result.error); else setBusy(true, "正在处理勾选项");
};

$("cache-scan-btn").onclick = startCacheScan;
$("cache-run-btn").onclick = async () => {
  const ids = state.caches.filter((item) => item.selected && item.level === "safe").map((item) => item.id);
  if (!ids.length) { toast("没有勾选缓存"); return; }
  if (!(await ask("清理缓存", `<p>将直接删除 ${ids.length} 类缓存。它们会由软件重新生成，但不能从回收站还原。</p>`))) return;
  const result = await api().execute_caches(ids);
  if (!result.ok) toast(result.error); else setBusy(true, "正在清理缓存");
};

$("export-btn").onclick = async () => {
  const result = await api().export_handoff();
  if (!result.ok) { toast(result.error); return; }
  state.exported = true;
  $("export-result").innerHTML = `<div class="export-ok"><div><strong>已导出。</strong>把这个文件交给 Cursor 或其他 agent：<br/><code>${esc(result.path)}</code></div><button id="copy-path" class="ghost">复制路径</button></div>`;
  $("copy-path").onclick = () => navigator.clipboard.writeText(result.path).then(() => toast("路径已复制"), () => toast("复制失败，请手动选中路径"));
  renderOverview();
};

$("proc-refresh").onclick = refreshProcesses;
$("proc-kill").onclick = async () => {
  const pids = [...document.querySelectorAll("#proc-rows input:checked")].map((node) => Number(node.dataset.pid));
  if (!pids.length) { toast("没有勾选进程"); return; }
  if (!(await ask("结束进程", `<p>确定结束 ${pids.length} 个进程？未保存的内容会丢失。</p>`))) return;
  const result = await api().kill_processes(pids);
  toast(`已结束 ${(result.killed || []).length} 个进程`);
  refreshProcesses();
};
$("save-settings").onclick = async () => {
  const lines = (id) => $(id).value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  await api().save_settings({ protected_paths: lines("protected-paths"), process_whitelist: lines("proc-whitelist") });
  toast("设置已保存");
};
$("shortcut-btn").onclick = async () => {
  try {
    const result = await api().create_shortcut();
    toast(result.ok ? "已在桌面和开始菜单创建快捷方式" : "创建失败");
  } catch (error) {
    toast("创建失败：" + error);
  }
};

window.addEventListener("resize", () => { clearTimeout(renderTree.timer); renderTree.timer = setTimeout(renderTree, 120); });

async function boot() {
  const version = await api().get_version();
  if (version) $("version").textContent = "本地存储盘查 v" + version;
  await refreshDisks();
  renderDrivePicks();
  const cached = await api().load_cached();
  if (cached.ok) await hydrate();
}

window.addEventListener("pywebviewready", boot);
if (new URLSearchParams(location.search).has("demo")) {
  const script = document.createElement("script");
  script.src = "demo.js";
  script.onload = boot;
  document.body.appendChild(script);
}
