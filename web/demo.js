// Fake bridge for previewing the UI in a browser: open index.html?demo=1
// over a local HTTP server. Not loaded by the desktop app.
(() => {
  const GB = 1024 ** 3;
  const findings = [
    { id: "c1", path: "C:\\Users\\A\\AppData\\Local\\Temp", label: "用户临时文件", category: "缓存", action: "cache_clean", risk: "low", size: 3.4 * GB, reason: "可再生缓存，清理后软件会重新生成。", suggestion: "可直接清理。", selectable: true, selected: true },
    { id: "c2", path: "C:\\Users\\A\\AppData\\Local\\npm-cache", label: "npm 缓存", category: "缓存", action: "cache_clean", risk: "low", size: 1.8 * GB, reason: "npm 下载缓存，不影响已安装的包。", suggestion: "可直接清理。", selectable: true, selected: true },
    { id: "r1", path: "C:\\Users\\A\\Downloads\\VSCodeSetup.exe", label: "VSCodeSetup.exe", category: "安装包", action: "recycle_suggest", risk: "medium", size: 0.09 * GB, reason: "下载目录里超过 90 天的安装包。", suggestion: "可移入回收站。", selectable: true, selected: false },
    { id: "r2", path: "D:\\备份\\<img src=x onerror=alert(1)>.zip", label: "<img src=x onerror=alert(1)>.zip", category: "重复文件", action: "recycle_suggest", risk: "medium", size: 2.2 * GB, reason: "与 D:\\资料\\photos.zip 内容相同。", suggestion: "可移入回收站。", selectable: true, selected: false },
    { id: "a1", path: "D:\\VM\\ubuntu.vhdx", label: "ubuntu.vhdx", category: "镜像", action: "advice_only", risk: "medium", size: 42 * GB, reason: "虚拟机磁盘，直接删除可能导致虚拟机无法启动。", suggestion: "确认不用后自行处理。", selectable: false, selected: false },
    { id: "o1", path: "C:\\Windows", label: "Windows", category: "系统", action: "readonly", risk: "high", size: 31 * GB, reason: "系统目录，本工具不删除。", suggestion: "用系统「存储感知」清理。", selectable: false, selected: false },
  ];
  const tree = (name, path, size, protectedKind, children = []) => ({ name, path, size, protected: protectedKind, children });
  const trees = [tree("C:", "C:\\", 180 * GB, "", [
    tree("Users", "C:\\Users", 82 * GB, "", [tree("A", "C:\\Users\\A", 80 * GB, "", [tree("AppData", "C:\\Users\\A\\AppData", 38 * GB, ""), tree("Downloads", "C:\\Users\\A\\Downloads", 22 * GB, ""), tree("Videos", "C:\\Users\\A\\Videos", 20 * GB, "")])]),
    tree("Windows", "C:\\Windows", 31 * GB, "system"),
    tree("Program Files", "C:\\Program Files", 28 * GB, "system"),
    tree("work", "C:\\work", 24 * GB, "project"),
    tree("ProgramData", "C:\\ProgramData", 9 * GB, "system"),
    tree("pagefile", "C:\\pagefile.sys", 6 * GB, "system"),
  ])];
  const now = Date.now() / 1000;
  const projects = [
    { name: "studio", path: "D:\\work\\studio", project_type: "node", size: 6.2 * GB, file_count: 18233, source_mtime: now - 12 * 86400, regenerable: { node_modules: 3.1 * GB, dist: 0.4 * GB }, git: { remote: "https://example.com/studio.git", last_commit: "2026-09-27 21:04", dirty: true }, risk: "high", risk_note: "有未提交改动，不建议删除", subprojects: [{ name: "packages/ui", size: 0.8 * GB }] },
    { name: "old-crawler", path: "D:\\archive\\old-crawler", project_type: "python", size: 2.4 * GB, file_count: 4120, source_mtime: now - 410 * 86400, regenerable: { ".venv": 1.9 * GB, __pycache__: 0.02 * GB }, git: { remote: "", last_commit: "2025-08-02 10:11", dirty: false }, risk: "medium", risk_note: "无远程仓库，删除后无法恢复；410 天未修改，可再生目录占 1.92 GB", subprojects: [] },
    { name: "rust-tools", path: "E:\\code\\rust-tools", project_type: "rust", size: 5.1 * GB, file_count: 9020, source_mtime: now - 200 * 86400, regenerable: { target: 4.7 * GB }, git: { remote: "https://example.com/rust-tools.git", last_commit: "2026-03-22 09:40", dirty: false }, risk: "low", risk_note: "200 天未修改，可再生目录占 4.70 GB", subprojects: [] },
  ];
  const done = (job, result) => setTimeout(() => window.onProgress({ type: "done", job, ok: true, result }), 600);
  let scanned = false;
  window.pywebview = {
    api: {
      get_version: async () => "1.1.0",
      get_overview: async () => ({ disks: [{ drive: "C", used_gb: 214, total_gb: 237, free_gb: 23, percent: 90 }, { drive: "D", used_gb: 512, total_gb: 931, free_gb: 419, percent: 55 }, { drive: "E", used_gb: 120, total_gb: 465, free_gb: 345, percent: 26 }], snapshot: {}, tips: ["C 盘剩余不足 10%，建议先盘查 C 盘。"] }),
      load_cached: async () => ({ ok: false }),
      start_scan: async () => { scanned = true; window.onProgress({ type: "progress", message: "C:\\ 正在盘查 C:\\Users\\A\\AppData\\Local" }); done("scan", { findings: findings.length, projects: projects.length }); return { ok: true }; },
      cancel_scan: async () => ({ ok: true }),
      get_results: async () => (scanned ? { findings, categories: { 视频: 64 * GB, 镜像: 42 * GB, 程序: 33 * GB, 压缩包: 21 * GB, 文档: 9 * GB, 图片: 7 * GB, 其他: 88 * GB }, advice: [{ title: "系统盘空间不足", detail: "C 盘剩余 23 GB（低于 15%）。可以把桌面、文档、下载迁到 D 盘；若仍不够，可在管理员终端执行 powercfg -h off 关闭休眠。本工具不会代为执行。", severity: "warning" }, { title: "可处理空间", detail: "缓存可直接清理约 5.2 GB。建议移入回收站约 2.3 GB（可撤销）。另有约 42 GB 只作提醒。", severity: "info" }], trees, scans: [{ root: "C:\\", scanned_at: new Date(Date.now() - 9 * 86400000).toISOString() }] } : { findings: [], categories: {}, advice: [], trees: [], scans: [] }),
      get_projects: async () => ({ projects: scanned ? projects : [] }),
      start_cache_scan: async () => { done("cache", {}); return { ok: true }; },
      get_cache_targets: async () => ({ targets: [{ id: "user_temp", label: "用户临时文件 (%TEMP%)", level: "safe", size: 3.4 * GB, description: "超过设定天数的用户临时文件", selected: true }, { id: "chrome_cache", label: "Chrome 浏览器缓存", level: "safe", size: 1.1 * GB, description: "仅 Cache，不清理 Cookie/书签", selected: true }, { id: "windows_temp", label: "Windows 临时目录", level: "advice", size: 0.6 * GB, description: "位于系统目录，只统计并建议使用「存储感知」", selected: false }] }),
      execute: async () => { done("execute", { ok_count: 1, fail_count: 0, freed: "3.40 GB" }); return { ok: true }; },
      execute_caches: async () => { done("clean", { freed: "4.50 GB" }); return { ok: true }; },
      export_handoff: async () => ({ ok: true, path: "C:\\PCOptimizer\\exports\\handoff-20261009-180000\\REPORT.md" }),
      get_processes: async () => ({ snapshot: { cpu_percent: 18, memory_percent: 71, memory_used_gb: 11.4, memory_total_gb: 16, processes: [{ pid: 4120, name: "chrome.exe", cpu_percent: 6.2, memory_mb: 2140, can_kill: true, suspicious: true }, { pid: 920, name: "Code.exe", cpu_percent: 3.1, memory_mb: 980, can_kill: true }, { pid: 1, name: "explorer.exe", cpu_percent: 0.4, memory_mb: 160, can_kill: false, kill_block_reason: "系统关键进程" }] }, advice: [{ title: "浏览器占用较多", detail: "Chrome 约 2.1 GB，关掉不用的标签页即可释放。", severity: "warning" }] }),
      kill_processes: async () => ({ killed: [] }),
      get_settings: async () => ({ protected_paths: ["D:\\照片"], process_whitelist: ["code.exe"], clean_history: [{ time: "2026-10-09T17:20:00", summary: "3 项，释放 4.50 GB" }], storage_history: [] }),
      save_settings: async () => ({ ok: true }),
      create_shortcut: async () => ({ ok: true, paths: [] }),
    },
  };
})();
