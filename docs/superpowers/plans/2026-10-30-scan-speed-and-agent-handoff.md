# 扫描速度、目录图稳定与 Agent 目录清单 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让清理和盘查少做无用功，盘查时目录图不再闪，并导出一份 Agent 能快速定位的 D、E 盘项目目录清单。

**Architecture:** 进度事件在进页面之前按 0.5 秒节流，目录图数据没变就不重建。清理测量只做目录枚举，可清理项先返回。盘查在系统目录和可重建目录内只累加体积，并把每个可重建目录的路径记在项目上。导出只读 D、E 盘项目上的这些路径，按体积排成一张表，每条带 git 是否有未提交改动和恢复命令，不替 Agent 判断能不能删。

**Tech Stack:** Python 3.10–3.13，现有 `core/` 与 `app/`，界面在 `web/`，测试用 pytest。不新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-30-scan-speed-and-agent-handoff-design.md`

## Global Constraints

- 安全模型不变：系统文件不删，执行前仍走 `deletion_allowed()`。
- 本轮不新增删除能力，导出文件不含删除脚本和文件内容。
- 不新增依赖，支持 Python 3.10 到 3.13。
- 界面文案用简体中文。
- 进度事件间隔 0.5 秒；完成和失败事件不丢。
- 导出只含 D、E 盘上的项目（`EXPORT_DRIVES = ("D", "E")`），不分「可以删」「不可以删」。
- 清单单条下限 `MIN_ENTRY_BYTES = 100 * 1024 * 1024`，最多 `MAX_ENTRIES = 30` 条，按体积从大到小。
- `schema_version` 为 `2`。
- 不跟随重解析点。
- 可重建目录删掉后，到恢复命令完成之前，项目无法运行或构建。

## Review Focus

- 嵌套子项目里的可重建目录也要出现在清单里。
- C 盘上的项目不能出现在清单里，即使盘查过 C 盘。
- `dirty` 为 `true`、`false`、`null` 都原样带在每条记录上，不影响是否展开。
- `node_modules` 里面的文件不能再被识别成项目或大文件。
- 测量缓存体积时不能调用 `is_safe_delete_target`。
- 连续进度事件可以被丢掉，`type == "done"` 不能被丢掉。
- 导出结果里不能出现盘查发现、文件内容或项目源码路径。

---

### Task 1: 进度节流与目录图不再重画

**Files:**
- Create: `app/progress.py`
- Modify: `app/api.py`（构造 `JobRunner` 处）
- Modify: `web/app.js`（`renderTree`、`window.onProgress`）
- Modify: `web/app.css`（`.jobbar`、`.jobbar-track span`、`@keyframes`）
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: `JobRunner` 现有的 `emit(payload: dict) -> None`
- Produces: `throttle(emit: Callable[[dict], None], interval: float = 0.5, now: Callable[[], float] = time.monotonic) -> Callable[[dict], None]`

- [ ] **Step 1: Write the failing test**

```python
def test_throttle_keeps_done_and_drops_burst_progress():
    sent = []
    clock = {"t": 0.0}
    wrapped = throttle(sent.append, interval=0.5, now=lambda: clock["t"])
    wrapped({"type": "progress", "message": "a"})
    wrapped({"type": "progress", "message": "b"})
    wrapped({"type": "done", "ok": True})
    clock["t"] = 0.5
    wrapped({"type": "progress", "message": "c"})
    assert [item["message"] if item["type"] == "progress" else "done" for item in sent] == ["a", "done", "c"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progress.py::test_throttle_keeps_done_and_drops_burst_progress -v`

Expected: FAIL，`throttle` 未定义。

- [ ] **Step 3: Implement `throttle` and stop the treemap redraw**

`app/progress.py` 按上面的签名实现：`progress` 距上次送出不足 `interval` 秒就返回；其他 `type` 原样送出。

`app/api.py` 把 `JobRunner(self._emit)` 改成 `JobRunner(throttle(self._emit))`。

`renderTree` 用「宽度、高度、每个色块的 path 和 size」组成签名。签名与现有 `svg` 都没变时直接返回。空状态要清掉旧签名。

`.jobbar` 使用 `contain: layout paint`。进度条动画只改变内部色块的 `opacity`，不再使用 `translateX`。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progress.py::test_throttle_keeps_done_and_drops_burst_progress -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/progress.py app/api.py web/app.js web/app.css tests/test_progress.py
git commit -m "fix: keep the treemap still while a scan reports progress"
```

### Task 2: 清理页按目录测量，可清理项先返回

**Files:**
- Modify: `core/cleaner.py`（`measure_tree_bytes`、`scan_target`、`scan_all`）
- Modify: `app/api.py`（`start_cache_scan`）
- Modify: `web/app.js`（`window.onProgress`）
- Test: `tests/test_cleaner_advice.py`

**Interfaces:**
- Consumes: 现有 `CleanTarget` 与 `scan_target(target, protected_paths, min_age_days=0) -> int`
- Produces: `scan_all(..., on_batch: Callable[[list[CleanTarget]], None] | None = None) -> list[CleanTarget]`。`on_batch` 在所有 `level == "safe"` 的目标测完后调用一次，参数是这些目标；之后才测量其余目标。界面事件为 `{"type": "caches", "targets": [...]}`，每项含 `id`、`label`、`level`、`size`、`description`、`selected`。

- [ ] **Step 1: Write the failing tests**

```python
def test_measure_does_not_use_delete_check(tmp_path, monkeypatch):
    cache = tmp_path / "Cache"
    cache.mkdir()
    (cache / "part.bin").write_bytes(b"x" * 1200)
    monkeypatch.setattr("core.cleaner.is_safe_delete_target", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("measure called delete check")))
    target = CleanTarget(id="chrome_cache", label="Chrome", level="safe", paths=[str(cache)])
    assert scan_target(target, []) == 1200


def test_safe_targets_are_reported_before_advice(monkeypatch):
    seen = []
    monkeypatch.setattr("core.cleaner.scan_target", lambda target, protected, min_age_days=0: seen.append(target.id) or 0)
    batches = []
    scan_all(build_targets([]), [], on_batch=lambda done: batches.append([item.id for item in done]))
    assert "user_temp" in batches[0]
    assert "windows_temp" not in batches[0]
    assert seen.index("user_temp") < seen.index("windows_temp")
```

```python
def test_user_temp_respects_age_and_skips_named_dirs(tmp_path):
    temp = tmp_path / "Temp"
    temp.mkdir()
    old = temp / "old.tmp"
    old.write_bytes(b"o" * 100)
    (temp / "new.tmp").write_bytes(b"n" * 40)
    nest = temp / "node_modules"
    nest.mkdir()
    (nest / "pkg.bin").write_bytes(b"p" * 999)
    old_time = time.time() - 3 * 86400
    os.utime(old, (old_time, old_time))
    target = CleanTarget(id="user_temp", label="temp", level="safe", paths=[str(temp)])
    assert scan_target(target, [], min_age_days=1) == 100


def test_downloads_report_counts_only_large_files(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "small.bin").write_bytes(b"x" * 1024)
    (downloads / "big.bin").write_bytes(b"x" * (50 * 1024 * 1024))
    target = CleanTarget(id="downloads_report", label="Downloads", level="risk", paths=[str(downloads)])
    assert scan_target(target, []) == 50 * 1024 * 1024
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cleaner_advice.py::test_measure_does_not_use_delete_check tests/test_cleaner_advice.py::test_safe_targets_are_reported_before_advice -v`

Expected: FAIL。当前测量会调用删除检查，且 `scan_all` 没有 `on_batch`。

- [ ] **Step 3: Implement directory measurement and two phases**

`scan_target` 的体积全部改经 `os.scandir`。不要调用 `is_safe_delete_target`、`deletion_allowed` 或 `Path.resolve`。跳过重解析点，以及目录名属于 `node_modules`、`.git`、`.pnpm-store`、`dockerdata` 的子树。`user_temp` 用 `DirEntry.stat(follow_symlinks=False).st_mtime` 判断天数。

`downloads_report` 仍只累计不小于 50 MB 的文件，用同一种枚举，不做保护路径判断。

`scan_all` 先并行测量 `level == "safe"` 的目标（最多 4 个线程），调用 `on_batch`，再并行测量其余目标。返回的列表顺序与传入顺序相同。

`start_cache_scan` 在 `on_batch` 里发出 `type == "caches"`。`onProgress` 收到后写入 `state.caches` 并调用 `renderCaches`。任务结束时仍用 `get_cache_targets` 覆盖最终结果。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cleaner_advice.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/cleaner.py app/api.py web/app.js tests/test_cleaner_advice.py
git commit -m "fix: measure cleanup caches without per-file delete checks"
```

### Task 3: 盘查记录可重建目录路径，重目录只累加体积

**Files:**
- Modify: `core/scanner.py`（`_walk`，新增 `_sum_tree`）
- Modify: `core/projects.py`（`empty_project`、`finalize_projects`）
- Test: `tests/test_scanner.py`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `regenerable_name(dirname, project_type) -> str | None`，`is_system_tree(path) -> bool`
- Produces: 项目字典新增 `regenerable_dirs: list[{"path": str, "kind": str, "bytes": int}]`。`empty_project` 初始化为空列表。原有 `regenerable: dict[str, int]` 仍是按 `kind` 合计。`_sum_tree(path: str, state) -> tuple[int, int]` 返回字节数和文件数。

- [ ] **Step 1: Write the failing tests**

```python
def test_regenerable_dir_keeps_its_path_and_skips_inner_records(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text("{}", encoding="utf-8")
    (app / "src.js").write_bytes(b"s" * 2000)
    modules = app / "node_modules" / "pkg"
    modules.mkdir(parents=True)
    (modules / "index.js").write_bytes(b"m" * 3000)
    (modules / "package.json").write_text("{}", encoding="utf-8")
    extra = app / "extra" / "node_modules"
    extra.mkdir(parents=True)
    (extra / "lib.js").write_bytes(b"e" * 1500)
    result = scan_path(tmp_path, large_min_bytes=1000, store_depth=6)
    project = result.projects[0]
    found = {item["path"]: item for item in project["regenerable_dirs"]}
    top = next(item for path, item in found.items() if path.endswith("node_modules") and "extra" not in path)
    assert top["kind"] == "node_modules"
    assert top["bytes"] >= 3000
    assert any(path.endswith("extra\\node_modules") or path.endswith("extra/node_modules") for path in found)
    assert len(result.projects) == 1
    assert project["subprojects"] == []
    assert any(item.path.endswith("src.js") for item in result.large_files)
    assert not any("node_modules" in item.path for item in result.large_files)


def test_system_tree_sums_without_children(tmp_path, monkeypatch):
    windows = tmp_path / "Windows"
    nested = windows / "System32"
    nested.mkdir(parents=True)
    (nested / "a.dll").write_bytes(b"w" * 5000)
    monkeypatch.setattr("core.scanner.is_system_tree", lambda path: str(path).endswith("Windows"))
    result = scan_path(tmp_path, large_min_bytes=1000, store_depth=6)
    node = next(child for child in result.tree.children if child.name == "Windows")
    assert node.size == 5000
    assert node.children == []
    assert result.large_files == []
```

`tests/test_projects.py` 增加：两个都有 `.git` 的项目，只有带 `regenerable_dirs` 的那个会让传入的 `runner` 被调用。

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scanner.py::test_regenerable_dir_keeps_its_path_and_skips_inner_records tests/test_scanner.py::test_system_tree_sums_without_children -v`

Expected: FAIL，`regenerable_dirs` 不存在；系统目录仍会留下子节点或大文件。

- [ ] **Step 3: Implement size-only walks and selective git status**

`_sum_tree` 用 `os.scandir` 累加，跳过重解析点，响应取消，不写文件记录，不调用进度回调。

遇到可重建目录时调用 `_sum_tree`，写入 `regenerable` 与 `regenerable_dirs`。该目录在目录图上可以出现，但 `children` 为空。其内部的 `package.json` 不再产生项目。

目录自身被判定为系统目录时，用 `_sum_tree` 得到整棵体积，返回没有子节点的 `DirNode`。父目录不是系统目录时，这个系统目录仍出现在目录图上。

`_walk` 的进度最多每 0.5 秒一次，文字是当前目录。`_sum_tree` 不发进度。

`finalize_projects` 只对 `regenerable_dirs` 非空的项目调用会执行 `git status` 的 `runner`，数量仍不超过 `MAX_GIT_STATUS`，按 `size` 从大到小选取。其余项目沿用现在的「不跑 status」路径。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_scanner.py tests/test_projects.py -v`

Expected: PASS。原有「嵌套项目的 `node_modules` 合计至少 3000 字节」仍然成立。

- [ ] **Step 5: Commit**

```bash
git add core/scanner.py core/projects.py tests/test_scanner.py tests/test_projects.py
git commit -m "fix: record rebuildable directories without walking their files"
```

### Task 4: 导出 D、E 盘目录清单并说明项目排名

**Files:**
- Modify: `core/handoff.py`
- Modify: `app/api.py`（`export_handoff` 的调用和守卫）
- Modify: `web/index.html`
- Modify: `web/app.js`（项目详情、总览第三步、`renderDrivePicks` 默认勾选）
- Modify: `web/demo.js`（示例项目补上 `regenerable_dirs`）
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `使用说明.txt`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `regenerable_dirs`、`git.dirty`、`format_bytes`
- Produces:
  - `EXPORT_DRIVES = ("D", "E")`、`MIN_ENTRY_BYTES = 100 * 1024 * 1024`、`MAX_ENTRIES = 30`
  - `restore_command(project_path: str, kind: str) -> str`
  - `export_scope_projects(projects: list[dict]) -> list[dict]`：返回路径盘符是 D 或 E 的项目，不分大小写
  - `export_handoff(projects: list[dict], drives: list[dict], dest_root: Path | None = None) -> Path`。删除原来的 `findings` 参数；内部先调用 `export_scope_projects`，`drives` 也只保留 D、E。

- [ ] **Step 1: Replace the handoff test with the failing contract**

测试名 `test_handoff_lists_rebuildable_dirs_on_d_and_e_only`。每条断言都写成代码：

- 同时传入 `C:\work\a` 和 `D:\work\b` 两个项目，只有 `D:` 的目录出现在 `items`。`drives` 里没有 `C`。
- `D:\work\b` 有一个嵌套子项目，其 `node_modules`（150 MiB）也出现在 `items`。
- 给定 150、120、99 MiB 三个目录：前两个展开，99 MiB 的进 `rolled_up`，`rolled_up == {"count": 1, "bytes": 99 * 1024 * 1024}`。`items` 按 `bytes` 从大到小。
- 31 个都达到 100 MiB 时，`len(items) == 30`，`rolled_up["count"] == 1`。
- `git.dirty` 分别为 `True`、`False`、`None` 的三项，都原样出现在对应记录的 `dirty` 上，都被展开，没有 `reason`。
- 项目根有 `pnpm-lock.yaml` 时，`restore` 为「在项目根目录执行 pnpm install」。同时有 `yarn.lock` 时仍是 pnpm。只有 `package-lock.json` 时是 npm ci。什么都没有时是 npm install。
- JSON 键集合恰为 `{"schema_version", "generated_at", "drives", "rules", "items", "rolled_up"}`；每条 `items` 键集合恰为 `{"path", "bytes", "kind", "project_path", "dirty", "restore"}`；`schema_version == 2`；`len(rules) == 3`。
- 传入的发现、源码路径和任意文件内容都不在 JSON 或 `REPORT.md` 中。
- 没有任何 D、E 项目时，`export_scope_projects` 返回空列表。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_projects.py::test_handoff_lists_rebuildable_dirs_on_d_and_e_only -v`

Expected: FAIL。当前导出仍是 `schema_version == 1`，并写入全部项目和发现。

- [ ] **Step 3: Implement schema 2 and the project-page explanation**

`restore_command` 按规格表的顺序检查项目根目录的锁文件。字段集合、3 条规则原文都按规格，不增加字段。

收集清单时递归遍历每个项目的 `subprojects`，读取各自的 `regenerable_dirs`，`project_path` 是直接拥有该目录的那个（子）项目。

`REPORT.md` 是一张表：体积、路径、种类、未提交改动、恢复命令，表后一行写 `rolled_up` 的条数和合计字节。没有可重建目录时写「这次盘查没有识别到可重建目录。」

`app/api.py` 的守卫改为：`export_scope_projects(self._projects)` 为空就返回 `{"ok": False, "error": "请先盘查 D 盘或 E 盘"}`。调用改为 `export_handoff(self._projects, get_disk_usage())`。

`renderDrivePicks` 首次渲染时勾选 D、E 里实际存在的盘；两个都不存在才勾选 C。

项目页副标题写明：按可重建目录占用排列；这些目录删掉后源码还在，重新安装或编译之前项目无法运行；排名不表示整项目可删。排序项改为「按可重建目录占用」。详情标题改为「可重建目录」，有 `regenerable_dirs` 时逐条显示路径和体积；没有时退回 `regenerable` 的名字合计。总览第三步改为「导出 D、E 盘项目里的可重建目录清单」。

README 两份和 `使用说明.txt` 里关于导出内容的句子改成：清单是 D、E 盘项目里的可重建目录，按体积排列，每条带路径和恢复命令，不含文件内容。

`web/demo.js` 的示例项目补上与现有 `regenerable` 一致的 `regenerable_dirs` 路径。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/handoff.py app/api.py web/index.html web/app.js web/demo.js README.md README.zh-CN.md 使用说明.txt tests/test_projects.py
git commit -m "feat: export a short rebuildable-dir list for D and E drives"
```
