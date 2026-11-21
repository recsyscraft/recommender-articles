# Markdown 导出工具

将单篇文章或目录中的 Markdown 导出为 HTML / A4 PDF，适用于 Windows、macOS 和 Linux。工具代码和样式采用 [MIT](../LICENSE-MIT)；导出的文章、图片和图表仍遵循原内容许可（本仓库为 [CC BY 4.0](../LICENSE-CC-BY-4.0)）。工具保留正文中的作者、来源和许可声明，不会自动补充缺失的署名。

## 安装

需要 Python 3.10+、[Pandoc 3+](https://pandoc.org/installing.html)。HTML 不需要 Python 第三方依赖。以下命令均在仓库根目录执行。

安装 Pandoc（或从官网下载安装包）：

| 系统 | 命令 |
| --- | --- |
| Windows PowerShell | `winget install --id JohnMacFarlane.Pandoc -e` |
| macOS（Homebrew） | `brew install pandoc` |
| Ubuntu / Debian | `sudo apt install pandoc`（确认版本为 3+，旧版请用官网安装包） |

执行 `pandoc --version` 确认安装成功；Windows 安装后可能需要重新打开终端。

如需 PDF，建议创建虚拟环境：

```sh
python -m venv .venv
```

macOS / Linux 使用 `python3` 创建环境，然后执行 `source .venv/bin/activate`；Windows PowerShell 执行 `.venv\Scripts\Activate.ps1`。若本机禁止脚本激活，可以直接使用 `.venv\Scripts\python.exe` 执行下面的 Python 命令。

```sh
python -m pip install -r tools/requirements.txt
python -m playwright install chromium
```

Linux 如缺少浏览器系统库，执行 `python -m playwright install --with-deps chromium`（安装系统依赖可能需要管理员权限）。PDF 运行环境须符合 [Playwright 支持的操作系统](https://playwright.dev/python/docs/intro#system-requirements)，并非所有旧系统或 Linux 发行版均受支持。

中文需要系统安装中文字体。Windows / macOS 通常已具备；Ubuntu / Debian 可执行 `sudo apt install fonts-noto-cjk`。字体不同可能导致分页不同。

## 使用

```sh
# 单篇 HTML，默认输出 dist/01-overview.html
python tools/md_export.py zh/architecture/01-overview.md

# 同时导出 HTML 和 PDF
python tools/md_export.py zh/architecture/01-overview.md --format both

# 递归批量导出，保留输入目录内的相对结构
python tools/md_export.py zh --format both --output-dir dist/zh

# 更新已有文件（默认拒绝覆盖）
python tools/md_export.py zh --format html --output-dir dist/zh --force

# 查看参数
python tools/md_export.py --help
```

macOS / Linux 未启用虚拟环境时，可将 `python` 换为 `python3`。路径包含空格时请加双引号。命令不依赖 Bash、PowerShell 特有语法，且可以从任意工作目录通过绝对路径调用。

目录输入会递归处理所有 `.md` 文件，输出目录必须放在输入目录之外。批量 HTML 中，指向本次导出文章的相对 `.md` 链接会改成 `.html`，其他链接保持原样。PDF 中的链接也沿用 HTML 地址，不会改为 PDF 链接。单篇导出不会同时导出所链接的其他文章。

支持标题、目录、表格、代码高亮、LaTeX 数学公式、本地图片和 `![说明|384](图片路径)` 图片宽度。公式通过 Pandoc 转为 MathML，无需 MathJax/CDN；建议使用近期版本 Chrome、Edge、Firefox 或 Safari 阅读 HTML。图片和 CSS 会嵌入 HTML，导出后可单文件分享；超链接指向的其他页面不会被打包。远程图片在转换时仍需联网，缺失资源或 Pandoc 警告会导致转换失败。

Mermaid 代码块暂不渲染成图；请先导出为 PNG / SVG 再引用。工具用于可信 Markdown，Pandoc 可保留原生 HTML；不要直接转换来源不明的文件。PDF 禁用页面 JavaScript。

批量转换遇到错误即停止，已完成的输出保留。建议在独立输出目录生成，确认完成后再发布。`--force` 允许覆盖已有结果。

## 维护

- `md_export.py`：跨平台入口、图片宽度适配、批量处理。
- `style.css`：阅读和打印样式。
- `requirements.txt`：PDF 所需 Python 依赖。

实现依据：[Pandoc 手册](https://pandoc.org/MANUAL.html)、[Playwright PDF API](https://playwright.dev/python/docs/api/class-page#page-pdf)。

