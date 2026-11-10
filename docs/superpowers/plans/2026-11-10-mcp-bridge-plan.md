# MCP 桥接规划：把盘查结果从「导出文件」升级为「Agent 可调用的只读接口」

> 日期：2026-11-10
> 项目：PC Optimizer v1.2.0（Windows / Python 3.10–3.13 / pywebview）
> 参照实现：Kanso 0.1.10（macOS / Tauri + Bun，逆向分析见对话中的 `Kanso_拆解报告.md`、`core.md`、`shell_ui.md`）
> 结论先行：**不引入 Node、pi、ACP、也不内嵌任何 Agent**。用 **Python 标准库**实现一个 **MCP stdio server**（7 个工具，全部只读或 dry-run），把现有 `scan_cache.json`、`core/classifier.py`、`core/handoff.py` 的能力以标准工具形式暴露给用户已有的 Cursor / Claude Code / Codex；**执行权继续锁在 `core/executor.py`，MCP 不提供删除能力**。

---

## 0. 一页方案

| 项 | 决定 |
|---|---|
| 形态 | `PCOptimizer.exe --mcp` 子命令（开发时 `python -m core.mcp_server`），stdio 传输，换行分隔 JSON-RPC |
| 协议 | MCP（Model Context Protocol）子集：`initialize` / `tools/list` / `tools/call` / `ping`；基线版本 `2024-11-05`，协商回显 |
| 工具 | `drives_list`、`findings_query`、`dir_children`、`projects_list`、`cleanup_plan`（dry-run）、`handoff_export`；P3 追加 `scan_start` / `scan_status` |
| 新增依赖 | **0**（只用 `json`、`sys`、`re`、`pathlib`、`threading`、`time`） |
| 数据来源 | 只读 `scan_cache.json`（`core.scanner.load_audit_cache`）+ `Store`；不重新遍历磁盘（`scan_start` 阶段除外） |
| 执行能力 | **默认没有**。`cleanup_plan` 仅调用 `execute_findings(..., dry_run=True)` 返回计划；真正删除仍由 GUI 里用户勾选 + `core/executor.py` 完成 |
| 安全边界 | 复用 `core.safety.deletion_allowed()` / `is_cache_clean_path()` / `executor.recycle_block_reason()`；不输出文件内容；不返回进程列表；不监听网络端口 |
| 体积/复杂度 | 约 500 行 Python（server 220 + tools 240 + 40 行入口/打包/文档），0 新依赖，不改动 UI 与现有 Python 逻辑 |
| 工作量 | P0+P1+P2 约 3–4 人日；P3 扫描 1–2 人日；P4 文档/打包 0.5–1 人日 |

**成功标准（可验收）**

1. 在 Cursor 的 `.cursor/mcp.json` 配好后，Agent 能调用 `findings_query` 拿到与 GUI「存储盘查」页一致的发现列表；
2. `cleanup_plan` 生成的步骤与 GUI 勾选后的确认弹窗一致，但**不删除任何文件**（用 mock 断言 `execute_findings` 只以 `dry_run=True` 调用）；
3. 协议层 stdout 只输出 JSON-RPC（日志全部走 stderr），`initialize` 握手、版本协商、错误码符合 MCP 规范；
4. 现有 `pytest tests` 全绿（新增测试 20+ 条），CI 无需网络。

---

## 1. 证据：Kanso 的 MCP 桥到底怎么做的

以下事实来自对 Kanso 0.1.10 的逆向（`_work/bun/kanso-core.js`，8.8 MB；偏移为 JS 字节偏移）：

| # | Kanso 的实现事实 | 证据 | 对我们的启示 |
|---|---|---|---|
| 1 | core 内起 MCP server，监听 `127.0.0.1` 的**随机 TCP 端口**，通过 HTTP/WebSocket streams 做 JSON-RPC | `serveMcpTools`（JS@8776208）；`server.listen(0,"127.0.0.1")`；错误串 `serveMcpTools: no TCP port` | 传输层可替换。但桌面单机场景用 **stdio** 更简单：无端口、无防火墙、无需鉴权 |
| 2 | 协议版本按数组协商：`["2025-06-18", "2025-03-26", "2024-11-05"]` | JS@8776208 附近常量 `VP1` | 版本协商必须做；我们基线定为 `2024-11-05`，客户端请求更高版本时按白名单回显 |
| 3 | 工具名强校验：`^[\w-]{1,64}$`，且拒绝与保留名冲突 | `serveMcpTools name ... must match`、`ax` 保留集 | 工具名 = 稳定 API，必须校验并保留命名空间 |
| 4 | 内部工具调用在事件流里被**剥掉 `mcp__kanso__` 前缀并静默**，用户只看到 Agent 的外部动作 | `M52` 事件归一化（JS@8793784） | 如果将来做 GUI 活动流，内部工具调用不应污染用户视野 |
| 5 | 工具按阶段注入：扫描 → `update_map`；审核 → `review_candidate`；执行 → `execute_step` / `report_step`；每阶段附一句 instructions | `A52`（JS@8792976）；`_work/prompts/mcp_instructions*.txt` | 工具应**按用途分组**、按场景提供给 Agent，而不是一次塞 20 个 |
| 6 | 工具返回 MCP 标准形状 `{content:[{type:"text",text}], isError?}` | `v08`（JS@8776208 附近） | 我们照做，便于所有 MCP 客户端兼容 |
| 7 | **执行权不在 Agent**：Agent 只能 `execute_step` 请求，真正删除在 core 的 `fJ0()`；Agent 进程另跑在 seatbelt 只读沙箱 | `fJ0`@8588066、`V28`@8784533、执行 prompt `real deleting can only happen through execute_step` | MCP 只暴露「事实 + 提议」，删除永远回 GUI/执行器——这正是本规划的硬约束 |
| 8 | 大对象不塞给 Agent：全盘索引落成 3 个 TSV（directories / large-files / regenerable）让 Agent 自己 grep | `V52`（JS@8787xxx）、扫描 prompt | 输出要分页/限量；我们的等价物是 `findings_query` 的 `limit/offset` + 体积阈值 |
| 9 | 依赖代价：为支持 ACP + 多 Agent 运行时，打包了 416 个第三方组件、143 MB core | `deps.md`、`core.md` | 我们**不**走 ACP/内置 Agent 路线，因此不背这些依赖 |
| 10 | MCP 版本演进快（2024-11-05 → 2025-03-26 → 2025-06-18），实现需钉版本 | 同 #2 | 协议解析集中在 `core/mcp_server.py`，升级只改一个文件 |

**一句话**：Kanso 的 MCP 设计 = 「标准协议 + 分阶段工具集 + 事实型输出 + 执行权留宿主 + 只读沙箱」。其中**后三条我们完整借用，前两条按 Windows/Python/单进程的实际情况简化**（stdio 取代 HTTP、手写子集取代 SDK、不做 ACP）。

---

## 2. 为什么 PCOptimizer 需要这座桥（而不是继续只导出文件）

现状：`「项目」→「导出给 AI」` 生成 `exports/handoff-*/REPORT.md` + `inventory.json`（`core/handoff.py`），用户手动把文件交给 Cursor。

| 维度 | 现有 handoff 文件 | MCP 只读接口 |
|---|---|---|
| 交互 | 一次性、静态 | Agent 可追问、按条件查询、分页 |
| 数据新鲜度 | 导出时快照 | 每次调用读缓存；带 `stale` 标记 |
| 对接成本 | 用户复制路径 / 手动粘贴 | 一次配置，永久可用 |
| 安全面 | 只读文件，风险最低 | 只读工具 + dry-run 提案，默认零删除 |
| 体积代价 | 0 | **0 新依赖**（Python 标准库） |

目标用户是「已经装了 Cursor / Claude Code / Codex 的开发者」。MCP 是这些客户端共同支持的「工具插座」，所以它比内置 AI（Kanso 路线）便宜一个数量级，也比 handoff 文件更自然。

---

## 3. 设计决策（含备选与理由）

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 传输 | stdio / HTTP+WS(SSE) / 两者 | **stdio** | MCP 客户端最通用；桌面单用户；无端口/防火墙/鉴权问题。Kanso 用 HTTP 是因为它要同时给多个子 Agent 用，我们不需要 |
| 协议实现 | 官方 `mcp` Python SDK / 手写 JSON-RPC 子集 | **手写** | 项目一贯「不新增依赖」；需要的方法只有 4 个；手写可审计、可完全控制 stdout（SDK 的日志可能污染协议流） |
| 协议版本 | 钉死 / 协商 | **`2024-11-05` 基线 + 白名单回显** | 与 Kanso 的做法一致；旧客户端稳定，新客户端也能谈 |
| 工具粒度 | 一个 `query` 大工具 / 7 个小工具 | **7 个语义明确的工具** | Agent 选择更准、参数更少、测试更容易（Kanso 也是按阶段拆 4 个） |
| 扫描数据源 | 每次调用现场扫描 / 读缓存 | **默认读缓存**；`scan_start` 单独提供（P3） | 扫描是重 I/O；缓存是现有功能（`scan_cache.json`），Agent 查询应当快且可重复 |
| 执行能力 | 提供 execute 工具 / 只给 dry-run 计划 | **只给 `cleanup_plan`（dry-run）** | 与 Kanso「执行权留宿主」一致；用户仍需在 GUI 勾选 + 确认。删除路径必须经过 `core/executor.py` 的二次校验 |
| 通信方向 | 单向输出 / 请求-响应 | 请求-响应 | MCP 规范；一次一行 JSON，不并发多请求（单线程足够） |
| 进程模型 | 复用 GUI 进程 / 独立进程 | **独立进程**（`--mcp`） | GUI 关闭不影响 Agent；也避免 pywebview 事件循环干扰协议流 |
| 日志 | stdout / stderr / 文件 | **stderr + 文件** | stdout 必须纯净；文件日志便于排障（`get_data_dir()/mcp.log`） |
| 数据目录 | 只读 / 可写 | 读 cache；`handoff_export` 允许写 `exports/` | 导出与现有按钮等价，属于用户数据目录内的写操作 |
| 打包 | 新入口 / 现有 exe 子命令 | **现有 exe 的 `--mcp` 分支** | 用户只需配置一个 exe 路径；PyInstaller 现有 spec 无需新增产物 |

---

## 4. 工具清单（JSON Schema 级）

命名规则与 Kanso 一致：`^[a-z][a-z0-9_]{2,63}$`，全部小写下划线；不提供带 `kanso` 之类的品牌前缀（工具名属于本产品 API）。

### 4.1 `drives_list`

无参数。返回盘符、容量、可用空间、上次盘查时间与新鲜度。

```json
// result
{ "drives": [
  { "drive": "C", "total_gb": 237, "used_gb": 214, "free_gb": 23, "percent": 90,
    "scanned_at": "2026-11-08T21:10:00", "scan_age_hours": 12.6, "stale": false }
]}
```

### 4.2 `findings_query`

```json
// inputSchema
{ "type": "object", "properties": {
  "action":          { "enum": ["cache_clean", "recycle_suggest", "advice_only", "readonly"] },
  "min_size_mb":     { "type": "number", "minimum": 0 },
  "drive":           { "type": "string", "pattern": "^[A-Za-z]$" },
  "include_locked":  { "type": "boolean", "default": false },
  "limit":           { "type": "integer", "minimum": 1, "maximum": 200, "default": 50 },
  "offset":          { "type": "integer", "minimum": 0, "default": 0 }
}, "additionalProperties": false }
```

返回字段 = `Finding` dataclass 的只读投影：`id,label,action,risk,size,path,reason,suggestion,selectable,selected`；**不含文件内容**。附加 `{total, stale, scan_age_hours}` 供 Agent 判断。

### 4.3 `dir_children`

```json
// input
{ "drive": "C", "path": "C:\\Users\\A\\AppData", "depth": 1 }
// result
{ "node": { "name": "AppData", "path": "C:\\Users\\A\\AppData", "size": 40894464,
  "protected": "", "children": [
    { "name": "Local", "path": "C:\\Users\\A\\AppData\\Local", "size": 30618419, "protected": "" } ] } }
```

来源：`ScanResult.tree`（`core/scanner.py` 已保留默认 4 层、每层 40 个子目录）。`depth` 上限 2，避免一次返回整棵树。`protected` 取值 `"" | "system" | "project"`。

### 4.4 `projects_list`

```json
// input
{ "sort": "regen", "min_regen_mb": 100 }
// result item
{ "name": "rust-tools", "path": "E:\\code\\rust-tools", "project_type": "rust",
  "size": 5476083302, "regen_bytes": 5046586572,
  "regenerable_dirs": [{ "path": "E:\\code\\rust-tools\\target", "kind": "target", "bytes": 5046586572, "restore": "在项目根目录执行 cargo build" }],
  "git": { "remote": "https://example.com/rust-tools.git", "last_commit": "2026-03-22 09:40", "dirty": false },
  "risk": "low", "risk_note": "200 天未修改，可再生目录占 4.70 GB" }
```

复用 `core/handoff.py` 的 `restore_command()`、`_is_tracked()` 逻辑（不要重写）。

### 4.5 `cleanup_plan`（dry-run，不执行）

```json
// input
{ "candidate_ids": ["c1", "r1"], "min_age_days": 1 }
// result
{ "plan_id": "plan-20261110-093000",
  "steps": [
    { "id": "c1", "label": "用户临时文件", "method": "cache_clean", "paths": ["C:\\Users\\A\\AppData\\Local\\Temp"], "bytes": 3650722201, "would_delete": 0, "reason": "演练：将直接清理" },
    { "id": "r1", "label": "VSCodeSetup.exe", "method": "recycle_suggest", "paths": ["C:\\Users\\A\\Downloads\\VSCodeSetup.exe"], "bytes": 96636764, "would_delete": 0, "reason": "演练：将移入回收站" } ],
  "note": "计划仅在 GUI 中由用户确认后执行；本工具不会删除任何文件。" }
```

实现：把 id 映射回 `Finding`，调用 `core.executor.execute_findings(findings, store.protected_paths, dry_run=True)`；再对每一步跑 `recycle_block_reason()` 预测"会不会永久删除"，作为 `warning` 字段返回。**这是唯一一个和"执行"有关的工具，必须保证 100% 无副作用**（测试用 mock 断言）。

### 4.6 `handoff_export`

```json
// input
{ "dest": null }
// result
{ "folder": "C:\\Users\\A\\AppData\\Local\\PCOptimizer\\exports\\handoff-20261110-093000",
  "report": "...\\REPORT.md", "inventory": "...\\inventory.json", "items": 12 }
```

直接调用 `core/handoff.export_handoff(projects, drives)`；仅写 `get_data_dir()/exports`，`dest` 参数只允许在 `exports` 之下的相对子目录（默认 `handoff-<时间戳>`）。与现有「导出给 AI」按钮等价，没有任何扩大权限。

### 4.7 P3 可选：`scan_start` / `scan_status`

```json
// scan_start input
{ "drives": ["C"], "duplicates": false, "timeout_seconds": 120 }
// result
{ "job_id": "scan-20261110-093000", "state": "running", "note": "结果写入 scan_cache.json；用 scan_status 轮询。" }
```

约束：单实例锁（`get_data_dir()/mcp_scan.lock`）；同一时间只允许一个扫描；`timeout_seconds` 上限 300，超时终止并返回部分结果；只在 calls 之间轮询，不推送。**默认不开启**，需要 `--mcp-allow-scan` 显式打开（与 Kanso 的"用户主动点扫描"一致）。

---

## 5. 安全模型（对齐 Kanso 的思路，落到本项目的实际边界）

Kanso 的安全栈是「seatbelt 沙箱 + 唯一删除通道 + 审核签名 + 路径/命令白名单」。我们**没有外部 Agent 子进程**，所以威胁模型更简单：**MCP 客户端本身是用户配置的可信程序，但工具必须只读，且所有数据出口都不能变成攻击面**。

| 层 | 做法 | 复用/新增 |
|---|---|---|
| ① 能力面最小化 | server 进程不 import `execute_caches`、不调用任何非 dry-run 执行函数；没有 shell 工具 | 新增约束 |
| ② 路径校验 | 返回前对每个 path 调 `core.safety.deletion_allowed()`，标注 `selectable`；`dir_children` 拒绝越出已扫描根路径的输入 | 复用 `core/safety.py` |
| ③ 缓存来源信任 | 只读 `load_audit_cache()`；不信任 cache 里可能被手改的数据 → 输出前重新计算 `selectable` | 复用 `core/classifier.py` + `safety` |
| ④ 输出不含内容 | 只返回元数据（路径/体积/原因/建议）；不读文件字节、不返回进程列表/系统快照 | 新增约束 |
| ⑤ 输入校验 | JSON Schema 白名单校验；`limit ≤ 200`；字符串长度 ≤ 32768；未知字段拒绝（`-32602`） | 新增 |
| ⑥ 审计 | 每次调用写一行 `mcp.log`（时间、工具、id、耗时；默认不记录完整路径） | 新增 |
| ⑦ 无网络 | stdio 不监听端口；不发起任何网络请求 | 新增（与项目"纯本地"承诺一致） |
| ⑧ 并发 | 读缓存无锁；`handoff_export` 与 GUI 同时写 exports 用时间戳子目录天然隔离；`scan_start` 用锁文件 | 新增（P3） |

**与 Kanso 的对应**：Kanso 防的是「Agent 在真实文件系统上乱写」，我们用「不提供写能力 + 复用现有删除校验」达到同等效果；Kanso 的审核签名机制在这里不必要，因为根本不允许执行。

**明确的风险声明**：用户把 `PCOptimizer.exe --mcp` 配置进客户端后，该客户端可读到你 GUI 里扫描出的**路径与体积信息**（不含文件内容）。这与现在手动交出 `REPORT.md` 的信息量相同，但更实时。配置文档必须写清这一点。

---

## 6. 实现结构（文件级）

```
core/
  mcp_server.py     # 新增 ~220 行：stdio 传输、JSON-RPC 分发、版本协商、错误码、日志、stdout 保护
  mcp_tools.py      # 新增 ~260 行：7 个工具 schema + handler（纯函数，便于测试）
  scanner.py        # 复用 load_audit_cache / result_from_dict，不改
  classifier.py     # 复用 Finding / classify_scan，不改
  safety.py         # 复用 deletion_allowed / is_cache_clean_path，不改
  executor.py       # 只调用 execute_findings(..., dry_run=True)（P2）
  handoff.py        # 复用 export_handoff / restore_command（P2）
  store.py          # 复用 protected_paths / history，不改
main.py             # 改 ~8 行：--mcp 分支放在 WebView2 检查之前
tests/
  test_mcp_server.py # 新增 ~180 行：协议级测试（内存 stdin/stdout，不起子进程、不联网）
  test_mcp_tools.py  # 新增 ~160 行：工具逻辑 + 安全回归（mock 断言不执行）
tools/
  smoke_mcp.ps1      # 新增 ~30 行：CI/发布前用 exe 跑一次 initialize + drives_list 握手
docs/
  mcp-clients.md     # 新增：Cursor / Claude Code / Codex 的配置与示例对话
```

`mcp_server.py` 关键骨架（伪代码，确保评审时能看清约束）：

```python
PROTOCOL_VERSIONS = ("2024-11-05",)

def serve(stdin=sys.stdin, stdout=sys.stdout) -> int:
    for line in stdin:                      # MCP stdio = 换行分隔的 JSON-RPC
        msg = _parse(line)                  # 解析失败 -> -32700
        reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()

def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    if method == "initialize":  return _initialize(msg["params"])
    if method == "ping":        return _result(msg["id"], {})
    if method == "tools/list":  return _result(msg["id"], {"tools": TOOLS})
    if method == "tools/call":  return _call(msg)
    if "id" in msg:             return _error(msg["id"], -32601, f"method not found: {method}")
    return None                             # 通知类消息不回包

def _call(msg):
    params = msg.get("params") or {}
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in TOOL_NAMES:
        return _error(msg["id"], -32602, f"unknown tool: {name}")
    try:
        content = call_tool(name, args)     # 所有 handler 均为只读/dry-run
        return _result(msg["id"], {"content": content, "isError": False})
    except ToolError as exc:
        return _result(msg["id"], {"content": [{"type": "text", "text": str(exc)}], "isError": True})
```

日志一律 `print(..., file=sys.stderr)`；启动时把 `sys.stdout` 包一层，防止第三方库误写（本规划没有第三方库，仍保留保护）。

`main.py` 的改动（保持最小）：

```python
def main() -> None:
    if "--mcp" in sys.argv:
        from core.mcp_server import serve
        raise SystemExit(serve())           # 无窗口、不检查 WebView2
    ...                                      # 原有 pywebview 流程
```

---

## 7. 分阶段任务（含验收标准）

### P0 协议骨架（0.5–1 人日）

- [ ] `core/mcp_server.py`：读取行、解析 JSON、`initialize`（回显协议版本 + `serverInfo` + `capabilities.tools`）、`tools/list`（先只放 `drives_list`）、`ping`、未知方法 `-32601`、解析错误 `-32700`
- [ ] `core/mcp_tools.py`：`drives_list`（复用 `core.cleaner.get_disk_usage` + `load_audit_cache`）
- [ ] `main.py` 增加 `--mcp`
- [ ] `tests/test_mcp_server.py`：用 `io.StringIO` 喂请求，断言响应；**断言 stdout 无非法输出**
- **验收**：`echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | python main.py --mcp` 返回合法 JSON；`tools/list` 含 `drives_list`

### P1 只读工具（1–2 人日）

- [ ] `findings_query`（过滤/分页/大小阈值/stale 标记）
- [ ] `dir_children`（深度限制、越界拒绝、protected 标注）
- [ ] `projects_list`（复用 `handoff.restore_command` 与 git 元数据）
- [ ] `tools/list` 的 inputSchema 完整、`additionalProperties: false`
- [ ] 测试：空缓存、缓存不含指定 drive、offset 越界、limit 超上限被 clamp、非法 action → 错误
- **验收**：Agent 通过 `findings_query` 得到的列表与 GUI「存储盘查」页逐条一致（用同一份 `scan_cache.json` 对比断言）

### P2 提议与导出（1 人日）

- [ ] `cleanup_plan`：id → `Finding` → `execute_findings(dry_run=True)` → 展开为步骤；`recycle_block_reason` 预测永久删除风险
- [ ] `handoff_export`：复用 `export_handoff`；`dest` 限制在 exports 下
- [ ] 安全测试：mock `os.remove` / `shutil.rmtree` / `send_to_recycle_bin`，断言 **零调用**；`deletion_allowed` 拒绝的路径不能出现在 plan 的 `paths` 里
- **验收**：`cleanup_plan` 返回的步骤与 GUI 确认弹窗一致；执行前后 `history.json` 不变

### P3 扫描工具（1–2 人日，可选）

- [ ] `--mcp-allow-scan` 开关；`scan_start` / `scan_status`；锁文件；超时与部分结果
- [ ] 测试：并发两次 start → 第二次返回 busy；超时返回 `state:"timeout"`；扫描中 GUI 仍可用（只读缓存）
- **验收**：Cursor 里让 Agent 扫 C 盘，可轮询到进度并在完成后立即查询 findings

### P4 文档与打包（0.5–1 人日）

- [ ] `docs/mcp-clients.md`：三份配置 + 一段示例对话 + 信息暴露声明
- [ ] `tools/smoke_mcp.ps1` 接入 `build_release.bat`（可 `SKIP_SMOKE=1` 跳过，与现有 smoke 一致）
- [ ] README / 使用说明加一句 MCP 介绍（不喧宾夺主）
- **验收**：解压 release zip，按文档配置 Cursor，5 分钟内跑通首次调用

---

## 8. 测试计划

| 层 | 用例 | 方式 |
|---|---|---|
| 协议 | `initialize` 版本协商（旧/新/缺失）、`ping`、未知 method、坏 JSON、通知无回包 | `io.StringIO` 直接调 `serve()` |
| 工具 | 每个工具的正常/边界/非法参数；`findings_query` 分页与过滤；`dir_children` 越界；`projects_list` 排序 | fixture 构造 `scan_cache.json`（复用 `tests/test_scanner.py` 的写法） |
| 安全回归 | `cleanup_plan` 不产生任何删除副作用；`handoff_export` 目标受限；受保护路径不可 selectable | `monkeypatch` + mock |
| 兼容 | 用真 exe（PyInstaller）跑 `tools/smoke_mcp.ps1` 握手 | PowerShell + Release zip |
| CI | 全部 pytest；无网络、无窗口、无 WebView2 依赖 | GitHub Actions 现有 job |

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| MCP 协议演进 | 新客户端连不上 | 基线 `2024-11-05` + 白名单协商；协议解析集中在 `mcp_server.py`；文档写清支持版本 |
| 缓存过期/不存在 | Agent 给出过时结论 | 每个响应带 `scan_age_hours/stale`；工具描述里提示"先让用户重新盘查" |
| Agent 误以为能执行 | 用户以为已删除 | `cleanup_plan` 的 note 明确"不会删除"；工具描述第一句写 "read-only, does not delete" |
| 输出过大 | 客户端截断/慢 | `limit ≤ 200`、`min_size_mb` 过滤、`dir_children` 深度 ≤ 2 |
| 与 GUI 同时写 exports | 目录冲突 | `handoff-<时间戳>` 子目录；写失败返回结构化错误 |
| PyInstaller 路径 | frozen 下 import 失败 | 复用现有 `get_bundle_dir()`；`smoke_mcp.ps1` 用 release exe 验证 |
| 用户隐私认知 | 以为"纯本地"矛盾 | 文档声明：数据只在本机进程间传递；客户端是否上传取决于客户端自身策略 |

---

## 10. Non-goals（明确不做）

- 不做 ACP 客户端、不内置 pi / Claude Code / Codex / Grok 等 Agent 运行时（Kanso 的 143 MB 代价我们不背）；
- 不提供任何删除/结束进程/改设置的 MCP 工具；
- 不监听网络端口（stdio only；未来如要 HTTP，必须 `--mcp-http --port 0` 且只绑 127.0.0.1 + 一次性 token，另立设计）；
- 不改现有 UI 交互（最多在「项目」页加一行"也可通过 MCP 提供给 Agent"的提示，P4 可选）；
- 不新增第三方依赖，不引入 Node/TypeScript。

---

## 11. 与 Kanso 的取舍对照

| 维度 | Kanso 的做法 | 本规划 | 理由 |
|---|---|---|---|
| 传输 | HTTP/WS + 临时端口 | stdio | 单机单用户，简单即安全 |
| 身份 | 外部 Agent 子进程 + seatbelt | 用户自己的 MCP 客户端 | 不内嵌 Agent，风险与成本最低 |
| 工具数 | 4 个（按阶段注入） | 7 个（按用途分组，默认 6 个启用） | Agent 可按需选择；扫描工具默认关闭 |
| 执行 | `execute_step` 请求 → core 执行 | 无执行工具；只给 dry-run 计划 | 执行必须经 GUI 用户确认（现有产品语义） |
| 事实输出 | 全盘索引 3 个 TSV | `findings_query` 分页 + 阈值 | 等价目标：不把全盘数据一次性塞进上下文 |
| 协议实现 | 打包多版本协议处理 | 手写子集 + 版本白名单 | 0 依赖、可审计 |
| 安全 | 沙箱 + 签名 + 唯一通道 | 只读能力面 + 复用删除校验 | 威胁模型不同，等价结论 |

---

## 附录 A：客户端配置示例

**Cursor**（项目根 `.cursor/mcp.json`，或用户级 `~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "pcoptimizer": {
      "command": "C:\\Program Files\\PCOptimizer\\PCOptimizer.exe",
      "args": ["--mcp"]
    }
  }
}
```

**Claude Code**：

```bash
claude mcp add pcoptimizer -- "C:\Program Files\PCOptimizer\PCOptimizer.exe" --mcp
```

**Codex**：在其 `config.toml` 的 `[mcp_servers.pcoptimizer]` 段配置 `command` + `args`（以 OpenAI Codex 文档为准；实现不改）。

**开发期**（不打包）：

```json
{ "mcpServers": { "pcoptimizer-dev": { "command": "python", "args": ["main.py", "--mcp"], "cwd": "C:\\Users\\Administrator\\PCOptimizer" } } }
```

## 附录 B：示例对话（配置完成后）

> 用户：C 盘快满了，先看看能清什么，别动我的项目。
> Agent：`drives_list` → 发现 C: 90% 已用、缓存 12 小时前扫描过。
> Agent：`findings_query {action:"cache_clean", min_size_mb:100}` → 3 条：Temp 3.4 GB、Chrome 缓存 1.1 GB、崩溃转储 0.6 GB。
> Agent：`findings_query {action:"recycle_suggest"}` → 1 条旧安装包 96 MB。
> Agent：`cleanup_plan {candidate_ids:["user_temp","chrome_cache","r1"]}` → 返回 3 步 dry-run 计划，其中旧安装包"回收站可用，可还原"。
> Agent：建议先清缓存两项；旧安装包由用户在 GUI 勾选后移入回收站。
> 用户回到 PC Optimizer 的「清理」页勾选 → 确认 → 执行（真正的删除只发生在这一步）。

## 附录 C：Kanso 证据索引（供复核）

| 证据 | 位置 |
|---|---|
| MCP server 实现 `serveMcpTools` | `kanso-core.js` JS@8776208 起 |
| 工具名校验 / 版本协商数组 / `mcp__kanso__` 前缀 | JS@8776208、JS@8793784 |
| 4 个工具 schema 原文 | `_work/prompts/tools_update_map.js`、`tools_review_candidate.js`、`tools_execute_step.js` |
| 执行权在 core、Agent 只读沙箱 | `core.md` §5/§7；`fJ0`@8588066；`V28`@8784533 |
| 事实型交接（TSV 索引、handoff 规则） | `core.md` §2、`_work/prompts/scan_system.txt` |
| 依赖代价（ACP/416 组件/143 MB） | `deps.md` §0、`shell_ui.md` §2 |
