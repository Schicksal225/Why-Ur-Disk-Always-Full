# 全盘存储盘查与清理建议

日期：2026-10-09

## 目标

把 PC Optimizer 从「只清理 C 盘若干固定缓存目录」升级为「盘查整台电脑存了什么，并说明该删什么、为什么、怎么处理」的本地工具。

成功标准：

- 用户可选任意盘符扫描，看到目录占用、文件类型分布、大文件和长期未访问文件。
- 每条发现都带原因和建议，并标明处理方式。
- 缓存类可以直接清理；其余可处理项只移入回收站，且必须逐项勾选后再确认。
- 系统文件、重解析点、项目目录在任何路径下都不会进入删除。

## 已确认的决策

- 缓存（浏览器、pip、npm、conda、用户临时文件、缩略图、崩溃转储等）直接删除。
- 旧安装包、重复文件、空文件夹移入回收站。
- 虚拟机磁盘、镜像、大视频、微信/QQ 接收文件只给建议，不提供执行。
- 数据盘参与分析和建议。删除前逐项确认。含 `.git`、`package.json`、`pyproject.toml`、`*.sln`、`Cargo.toml`、`go.mod`、`pom.xml`、`composer.json` 的目录整体保护。
- 界面：最初用 tkinter，后来改为 pywebview 加本地 HTML（`web/`）。

## 处理方式

| action | 含义 | 界面 | 执行 |
| --- | --- | --- | --- |
| `readonly` | 系统或已安装程序，只统计 | 不可勾选 | 拒绝 |
| `advice_only` | 可能占空间，但内容可能重要 | 不可勾选 | 拒绝 |
| `cache_clean` | 可再生缓存 | 可勾选，默认勾选 | 直接删除 |
| `recycle_suggest` | 可恢复的清理候选 | 可勾选，默认不勾选 | 移入回收站 |

## 安全模型

`deletion_allowed()` 是删除前的最终判断，执行器不得信任扫描结果。

硬拒绝（配置不能放开）：

- 系统目录：`%SystemRoot%`、`Program Files`、`Program Files (x86)`、`ProgramData`
- 每个盘根目录本身，以及根目录下的 `System Volume Information`、`$Recycle.Bin`、`Recovery`、`Boot`
- `pagefile.sys`、`hiberfil.sys`、`swapfile.sys`
- 带系统属性的文件
- junction 与 symlink（扫描不进入，删除直接拒绝）
- 路径规范化之后落入上述位置的路径（大小写、`..`、`\\?\` 前缀、解析后的重解析点）

额外保护（同样不可删，但不是「系统文件」）：

- 项目目录整棵子树
- 用户在设置里添加的路径
- `node_modules`、`.git`、`.pnpm-store`、`DockerData`
- 本机 conda/anaconda 安装根目录、pnpm store

数据盘不再整盘保护。

`C:\Windows\Temp` 与 `C:\Windows\SoftwareDistribution\Download` 只统计并建议使用系统「存储感知 / 磁盘清理」，本工具不删除。它们不再出现在安全清理白名单里。

缓存直接删除仍要求路径落在缓存白名单内。回收站操作不要求缓存白名单，但必须通过 `deletion_allowed()`。

## 模块

- `core/safety.py`：SystemGuard、项目识别、缓存白名单、`deletion_allowed()`
- `core/scanner.py`：`os.scandir` 遍历，产出目录树（默认保留 4 层、每层最多 40 个子目录）、类型分布、大文件、长期未访问文件、缓存目录体积、重复文件候选。支持进度与取消。不进入重解析点。
- `core/classifier.py`：规则表，把扫描结果变成 `Finding`
- `core/duplicates.py`：大于 1MB 的文件先比大小，再比前 64KB 哈希，最后比完整哈希。每组保留路径最短的一份，其余为回收站候选。
- `core/advisor.py`：盘级结论。系统盘剩余低于 15% 时建议迁移用户文件夹，并给出 `powercfg -h off` 的说明，不代为执行。
- `core/executor.py`：执行前再次调用 `deletion_allowed()`。缓存走直接删除，其余走 `SHFileOperationW` + `FOF_ALLOWUNDO`。结果写入 `history.json`。
- `ui/tabs/`：原有标签页从 `main.py` 拆出；新增「存储分析」页。

扫描结果缓存到数据目录的 `scan_cache.json`（不含完整重复候选列表）。重复扫描由用户勾选后才进行。

## 首批分类规则

- 系统目录、Program Files：`readonly`，建议到「应用和功能」卸载软件
- 项目目录：`readonly`
- 用户缓存白名单：`cache_clean`
- Windows 临时目录、Windows 更新缓存：`advice_only`
- Downloads 中超过 90 天且大于 20MB 的安装包和压缩包：`recycle_suggest`
- `.iso`、`.vhdx`、`.vhd`、`.vmdk`：`advice_only`
- 微信 / QQ 接收目录：`advice_only`
- 大于 500MB 的视频：`advice_only`
- 空文件夹：`recycle_suggest`，默认不勾选，最多 30 个
- 重复文件：`recycle_suggest`

文件同时命中多条规则时，优先级为：系统 > 项目 > 聊天目录 > 镜像 > 旧安装包 > 大视频 > 长期未访问（只建议）。

## 测试

pytest 覆盖：系统路径拒绝（大小写、`..`、扩展路径前缀）、盘符根目录、数据盘不再整盘保护、项目目录、junction 不删除、分类规则、临时目录扫描、重复文件、执行器 dry-run 不会删除受保护路径。

## 非目标

- 不卸载软件，不关闭休眠，不迁移用户文件夹，只给出操作说明。
- 不上传任何扫描结果。
