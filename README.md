# PC Optimizer

中文 | [English](README.en.md)

Windows 本地磁盘盘查工具：看清每个盘存了什么，说明哪些可以清理、为什么，并把按项目拆解的清单导出给 Cursor 等 AI agent。纯本地运行，不联网，不上传任何数据。

**系统文件只统计，从不删除。** 项目源码也不会被本工具清理。

## 下载使用

1. 到 Releases 页面下载 `PCOptimizer-v*-win64.zip`
2. 解压，双击 `PCOptimizer.exe`
3. 在「存储盘查」选盘符，点「开始盘查」

首次运行如果出现 SmartScreen「未知发布者」，点「更多信息」→「仍要运行」（程序没有代码签名）。
需要 Microsoft Edge WebView2 运行时；Windows 11 和较新的 Windows 10 自带，没有的话程序会给出下载链接。

可以用 Releases 里的 `CHECKSUMS.txt` 校验下载文件的 SHA256。

## 功能

| 页面 | 说明 |
|------|------|
| 总览 | 各盘剩余空间、文件类型分布、上次盘查是什么时候，并可一键重新盘查 |
| 存储盘查 | 可点击下钻的目录占用图，每条发现都带原因和建议 |
| 项目 | 按项目拆开硬盘，显示可重建目录（node_modules、target 等）和 git 状态，可导出给 AI |
| 清理 | 浏览器、npm、pip 等已知缓存 |
| 性能 | 进程与内存建议 |
| 设置 | 保护路径、进程白名单、历史、桌面/开始菜单快捷方式 |

## 安全策略

**永不删除：** `Windows`、`Program Files`、`ProgramData`；盘符根目录，以及根目录下的 `System Volume Information`、`$Recycle.Bin`、`Recovery`、`Boot`；`pagefile.sys`、`hiberfil.sys`、`swapfile.sys`；带系统属性的文件；junction 和符号链接；项目目录；conda/anaconda、`node_modules`、`.pnpm-store`、`DockerData`；以及你在设置里添加的路径。

| 处理方式 | 含义 |
|----------|------|
| 直接清理 | 可再生缓存，删除后软件会重新生成，不能从回收站还原 |
| 进回收站 | 旧安装包、重复文件、空文件夹，需要逐项勾选，可还原。该盘关闭了回收站或文件超过回收站容量时会拒绝，避免被永久删除 |
| 仅建议 / 只读 | 虚拟机磁盘、大视频、聊天文件、系统目录等，软件不会动 |

执行前会对每个路径重新检查一遍，不信任之前的盘查结果。

## 交给 AI agent

「项目」页点「导出给 AI」，在数据目录下的 `exports/handoff-时间/` 得到：

- `REPORT.md`：给 agent 的操作规则、按盘汇总、每个项目一节
- `inventory.json`：路径、类型、大小、可再生目录、git 状态、风险说明

只有路径和元数据，没有文件内容，也不含删除脚本。

## 从源码运行

需要 Python 3.10 到 3.13。

```bat
pip install -r requirements.txt
python main.py
```

## 自己构建 exe

```bat
build_release.bat
```

脚本会在项目内创建 `.venv`，安装依赖、跑测试、打包、检查窗口能否打开，最后打印压缩包的完整路径。不依赖特定的 Python 安装位置。
产物在 `release\`，版本号来自 `VERSION` 文件。

## 开发

```bat
pip install -r requirements-dev.txt
python -m pytest tests
```

浏览器里预览界面（不会读写你的磁盘）：

```bat
python -m http.server 8770
```

然后打开 `http://127.0.0.1:8770/web/index.html?demo=1`。

目录结构：`core/` 业务逻辑（扫描、分类、安全、执行），`app/` 桌面窗口的桥接层，`web/` 界面，`tests/` 测试。

## 发布

在 GitHub 上推送 `v*` 标签（例如 `v1.1.0`），`.github/workflows/release.yml` 会构建并把压缩包挂到 Release。Gitee 没有同样的自动流程，把本地构建出的 zip 手动上传到 Gitee 发行版即可。

## 下载（Gitee 用户）

在本仓库的「发行版」页面下载 `PCOptimizer-v*-win64.zip`，解压后双击 `PCOptimizer.exe`。

## 许可

MIT，见 `LICENSE`。
