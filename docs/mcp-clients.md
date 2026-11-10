# 把 PC Optimizer 接到 AI 客户端（MCP）

`PCOptimizer.exe --mcp` 是一个本地 stdio MCP server，协议基线 `2024-11-05`。
客户端（Cursor、Claude Code、Codex 等）连上以后，AI 就能直接读取你在界面里盘查出的
结果、生成演练用清理计划、导出清单，而不需要手动复制路径或 `REPORT.md`。

它和 GUI 是**两个独立进程**：GUI 可以关着，MCP 也不依赖 WebView2、不开窗口、不开端口、
不联网，只通过标准输入输出说 JSON-RPC。

## 安全边界

- **只读**：工具不会删除、移动、修改被盘查的文件，也没有提供执行删除的接口。
  真正清理仍需你在 GUI 里勾选并确认。
- **默认禁止扫描**：`scan_start` / `scan_status` 只有启动时加 `--mcp-allow-scan`
  才可见；默认客户端只能读上一次盘查的缓存。
- **`cleanup_plan` 只是演练**：返回“如果执行会怎样”和“回收站会不会变成永久删除”的提醒，
  不会执行任何操作。
- **`handoff_export` 只写数据目录**：`exports/` 下的 `REPORT.md` 和 `inventory.json`，
  `dest` 只接受 `exports` 下的相对子目录，路径逃逸会被拒绝。
- **stdout 纯净**：协议回复只走 stdout；日志走 stderr 和同目录 `mcp.log`（每次调用一行，不含完整路径）。

## 前置条件

1. Windows 10/11，拿到 `PCOptimizer.exe`（解压后的发布包，或
   `%LOCALAPPDATA%\PCOptimizer\PCOptimizer.exe` 这份可运行副本）。
2. 客户端支持 MCP stdio。
3. 先用 GUI 盘查一次；或者启动时加 `--mcp-allow-scan`，让 AI 自己触发盘查。

Windows 路径在 JSON 里要写双反斜杠，或用正斜杠；含空格的路径必须整体加引号。

## Cursor

项目里建 `.cursor/mcp.json`（或全局 `%USERPROFILE%\.cursor\mcp.json`）：

```json
{
  "mcpServers": {
    "pcoptimizer": {
      "command": "C:\\Users\\A\\AppData\\Local\\PCOptimizer\\PCOptimizer.exe",
      "args": ["--mcp"]
    }
  }
}
```

需要让 AI 触发盘查时，把参数改成 `["--mcp", "--mcp-allow-scan"]`。

## Claude Code

命令行添加（路径按实际改）：

```bat
claude mcp add pcoptimizer -- "C:\Users\A\AppData\Local\PCOptimizer\PCOptimizer.exe" --mcp
```

或写进项目的 `.mcp.json`，结构同上面的 Cursor 示例。

## Codex

`~/.codex/config.toml`：

```toml
[mcp_servers.pcoptimizer]
command = 'C:\Users\A\AppData\Local\PCOptimizer\PCOptimizer.exe'
args = ["--mcp"]
```

## 工具一览（默认 6 个，加 `--mcp-allow-scan` 后 8 个）

| 工具 | 作用 | 参数 |
|------|------|------|
| `drives_list` | 盘符容量、剩余空间、上次盘查时间与新鲜度 | 无 |
| `findings_query` | 查上次盘查的发现项，按体积排序、分页 | `action`、`min_size_mb`、`drive`、`include_locked`、`limit`（≤200）、`offset` |
| `dir_children` | 查缓存目录树里某个目录的子目录 | `drive`、`path`、`depth`（1–2） |
| `projects_list` | 项目、可重建目录、恢复命令、git 状态 | `sort`（regen/size/idle）、`min_regen_mb` |
| `cleanup_plan` | **演练**清理计划，标出永久删除风险 | `candidate_ids`、`min_age_days` |
| `handoff_export` | 导出与「导出给 AI」相同的清单 | `dest`（可选，exports 下相对子目录） |
| `scan_start` | 后台盘查指定盘（需 `--mcp-allow-scan`） | `drives`、`duplicates`、`timeout_seconds`（1–300） |
| `scan_status` | 轮询盘查进度（需 `--mcp-allow-scan`） | `job_id`（可选，默认最近一次） |

`findings_query` 默认不带只读/仅建议项；想和 GUI「存储盘查」页逐条对齐时传
`"include_locked": true`。返回字段与界面一致：id、label、action、risk、size、path、
reason、suggestion、selectable、selected，外加 `total/stale/scan_age_hours`。

## 典型对话

1. **先看现状**
   “用 pcoptimizer 看看 C 盘还剩多少，上次盘查是什么时候。” → `drives_list`
2. **找出可清理项**
   “列出 100 MB 以上、可以清理的发现。” → `findings_query`（`min_size_mb: 100`）
3. **只出计划，不动手**
   “给这几个候选生成清理计划，并告诉我哪些会永久删除。” → `cleanup_plan`
4. **回 GUI 执行**
   用户在「存储盘查」页勾选同样的项，确认执行。
5. **需要新数据时**
   用 `--mcp-allow-scan` 启动，AI 调 `scan_start`，再用 `scan_status` 轮询到 `done`，
   之后 `findings_query` 就能读到新缓存；超时会返回 `timeout` 且不覆盖缓存。
6. **交接给别的 agent**
   “把项目清单导出来。” → `handoff_export`，文件在 `exports/` 下。

## 数据暴露说明

把 exe 配置进客户端，等于允许该客户端及其背后的模型读取你在界面里盘查出的
**路径、体积、项目名和 git 状态**。不含文件内容，也不需要管理员权限；信息量与手动
导出 `REPORT.md` 相同，只是更实时。是否接受由你决定。

`mcp.log` 只记录时间、工具名、是否成功和耗时，不记录路径。

## 故障排查

- **客户端提示 server 启动失败**：确认 exe 路径存在、参数是 `--mcp`；路径用引号或 JSON
  双反斜杠转义。不要在 `command` 里套 `cmd /c`。
- **工具返回“no audit cache found”**：先在 GUI 盘查一次，或用 `--mcp-allow-scan` 让 AI 扫。
- **`scan_start` 一直 busy**：同一时间只允许一个扫描。检查 exe 同目录的
  `mcp_scan.lock`；确认没有另一个 MCP 进程，异常退出留下的旧锁会在超过扫描上限后自动回收。
- **想看调用记录**：exe 同目录 `mcp.log`；stderr 里也有同样内容。
- **从 agent 沙箱工作区直接运行报“无法创建临时目录”**：exe 被打了 Windows 低完整性标签，
  用发布包解压后的副本或 `%LOCALAPPDATA%\PCOptimizer\PCOptimizer.exe`。
- **客户端连的是 GUI 而不是 MCP**：`--mcp` 进程不弹窗，Windows 任务管理器里会看到
  同名进程但无窗口，属于正常。

## 给维护者

`tools/smoke_mcp.ps1` 用重定向的 stdio 对构建产物跑一次
`initialize` + `tools/list` + `drives_list` 握手；`build_release.bat` 已把它接在窗口冒烟之后
（`SKIP_SMOKE=1` 时一起跳过）。
