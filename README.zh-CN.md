# Inspect Dependency Source

> 不再猜测依赖库的行为，让编码 Agent 基于正在调试的准确源码开展分析。

编码 Agent 往往很擅长理解当前仓库，但一旦跨过第三方依赖边界，可靠性就会明显下降：它可能查看上游最新分支而不是项目实际使用的版本；在多个工作区反复下载同一份大型源码；丢失包版本与源码提交之间的关联；或者只依赖无法解释实现细节的文档。

Inspect Dependency Source 是一个用户级 Agent Skill，内置由同一台机器上所有项目和编码 Agent 共享的源码目录。它将依赖解析到可复用的本地源码树，记录每个版本的选择依据，在准确 ref 不存在时明确失败，并提供美观的 localhost 仪表盘，让用户查看已经缓存的内容及其可信程度。

它不会让模型本身变得更聪明，而是通过更可靠、证据更充分的源码分析流程提升结果质量。

![Inspect Dependency Source 仪表盘，展示目录健康状态、已验证源码清单和仓库来源依据](docs/images/dashboard-overview.png)

*只读的 localhost 仪表盘让源码清单、准确版本依据和操作健康状态始终可见，且不暴露任何变更控件。*

[English README](README.md)

## 解决的问题

- **准确版本调试：** 将包版本或指定 ref 绑定到 tag 或 commit，绝不静默替换为默认分支。
- **可复用的源码上下文：** 下载一次后，即可由 Codex、Claude 及不同仓库复用同一份已验证源码。
- **可观察的来源依据：** 明确区分 `exact_commit`、`exact_tag`、`heuristic_tag` 和 `unresolved`。
- **更安全的缓存：** 在临时目录下载并验证后原子提升；替换失败时保留上一份可用版本。
- **本地可观测性：** 通过可离线使用的仪表盘查看仓库、工件、包绑定、时效性、完整性、磁盘占用和操作历史。
- **无需耦合内部实现的自动化：** 通过稳定的 `resolve --json` 契约集成，无需直接读取存储文件。

运行时仅使用 Python 标准库。访问公开 Git 和 GitHub 仓库不强制依赖 GitHub CLI。

## 面向实现级调试的本地、源码优先 Context7 替代方案

[Context7](https://context7.com/docs/overview) 通过 MCP、CLI 和 Skills 等集成方式提供版本相关的文档与代码示例，适合让 Agent 在工作流中获得权威的 API 使用指导。

Inspect Dependency Source 解决的是相关但不同的上下文问题：它在用户本机管理完整源码树，将源码固定到准确 ref 或 commit，记录从包版本到源码的来源关系，并支持离线复用，从而服务于实现级调试。

| 需求 | Context7 | Inspect Dependency Source |
| --- | --- | --- |
| 主要上下文 | 版本相关的文档与示例 | 完整的本地源码树 |
| 典型问题 | “这个 API 应该如何使用？” | “这个准确依赖版本内部是如何工作的？” |
| 使用方式 | MCP、CLI 和 Skills | 用户级 Skill、CLI 和 localhost 仪表盘 |
| 离线复用 | 取决于集成方式和已获取上下文 | 已缓存源码可持续在本机复用 |
| 来源依据重点 | 库文档版本 | 包/ref 到工件和 commit 的映射 |

本项目可以作为源码上下文场景中的本地、源码优先 Context7 alternative，但它与 Context7 API 不兼容，不模拟其 MCP 服务，也不替代面向文档的检索。两者也可以组合使用。Context7 的最新能力请参考其[官方仓库](https://github.com/upstash/context7)。

## 支持的源码类型

- GitHub 仓库。
- 通用 Git 远程仓库。
- 已存在的本地源码树。
- 准确的 NuGet 包版本；当包元数据提供仓库 commit 时，优先保留该来源依据。

首个版本不包含 npm、PyPI、Maven 和 Cargo 包解析器，但仍可通过 Git 或本地源码方式注册这些包的仓库。

## 用户级安装

请按 OS 用户安装一次，不要将 Skill 安装到某个项目中。下面的示例将主 checkout 放在 Codex 用户级 Skill 目录，再让 Claude 链接到同一份内容：

```bash
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
git clone https://github.com/Tairitsua/inspect-dependency-source-skill.git \
  "$HOME/.agents/skills/inspect-dependency-source"
ln -s "$HOME/.agents/skills/inspect-dependency-source" \
  "$HOME/.claude/skills/inspect-dependency-source"
```

- Codex 用户级 Skill：`$HOME/.agents/skills/inspect-dependency-source`
- Claude 用户级 Skill：`~/.claude/skills/inspect-dependency-source`

如果环境不支持符号链接，也可以分别将仓库 clone 或复制到两个目录。运行时目录与安装目录、当前项目相互独立，因此所有安装仍会共享同一个用户级源码目录。

环境要求：

- Python 3.11 或更高版本，并包含标准 `sqlite3` 模块。
- 处理 Git 仓库的注册和下载时需要 Git。
- 只有远程刷新、下载或 NuGet 解析需要网络。
- 私有 GitHub 仓库或更高 API 限额可选用 `gh`、`GH_TOKEN` 或 `GITHUB_TOKEN`。

## 快速开始

从已安装的 Skill 目录运行命令：

```bash
cd "$HOME/.agents/skills/inspect-dependency-source"

# 创建全局目录，并启动或复用仪表盘。
python3 scripts/inspect_dependency_source.py init

# 检查本地 Git、GitHub 认证和运行时前置条件（不会探测网络）。
python3 scripts/inspect_dependency_source.py doctor

# 缓存一个准确源码 ref。
python3 scripts/inspect_dependency_source.py repo add-github owner/repository --alias example
python3 scripts/inspect_dependency_source.py repo fetch example --ref v1.2.3

# 获取供 Agent 使用的稳定机器可读证据。
python3 scripts/inspect_dependency_source.py resolve example --ref v1.2.3 --json

# 输出当前 localhost 仪表盘地址。
python3 scripts/inspect_dependency_source.py dashboard status
```

处理准确 NuGet 依赖：

```bash
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --json
```

注册已经存在的本地源码：

```bash
python3 scripts/inspect_dependency_source.py repo add-local /absolute/path/to/source --alias example
python3 scripts/inspect_dependency_source.py resolve example --json
```

所有命令请参阅 [CLI 参考](references/cli.md)；准确 ref、NuGet、本地、离线和恢复流程请参阅[源码检查工作流](references/workflows.md)。

## 仪表盘

除非指定 `--no-dashboard`，`init` 会为当前全局目录启动或复用一个仪表盘。也可单独管理：

```bash
python3 scripts/inspect_dependency_source.py dashboard start
python3 scripts/inspect_dependency_source.py dashboard status
python3 scripts/inspect_dependency_source.py dashboard stop
```

响应式只读 UI 展示：

- 仓库、工件、包绑定和验证数量。
- 支持搜索的源码清单、别名和已清理敏感信息的远程地址。
- 准确 ref/commit 来源以及首选源码路径。
- 已缓存 tag、本地 Git 快照、manifest 和新鲜度。
- 正在执行或失败的操作及可展开事件时间线。
- 完整性告警、缓存磁盘占用和剩余空间。

UI 支持英文与简体中文、跟随系统的亮/暗色主题、减少动态效果、键盘操作，以及 360/768/1440 像素响应式布局。语言、主题、筛选条件、选中仓库和展开的时间线会在两秒刷新周期中保持不变。

服务只绑定 `127.0.0.1`，不加载 CDN 资源，不启用 CORS，仅开放 GET/HEAD，也不提供下载、删除或其他写操作控件。

## Agent 如何使用

`SKILL.md` 指导 Agent 先解析、后下载，并将 `resolve --json` 作为稳定集成契约。结果包含仓库、请求版本或 ref、选中工件、已解析 commit、验证状态、源码路径、包来源关系以及确定性的失败信息。准确字段、错误、增强 manifest 和操作事件请参阅[公共数据契约](references/schema.md)。

源码目录提供的是证据，并不会自动成为结论。Agent 应当：

1. 匹配项目实际解析出的依赖版本。
2. 优先使用 `exact_commit` 或 `exact_tag`。
3. 将 `heuristic_tag` 视为需要进一步验证的线索。
4. 当指定 ref 不存在时停止，不要分析其他分支。
5. 在分析结果中注明 ref、commit、来源类别和路径。
6. 将托管源码视为只读内容，因为其他项目和 Agent 会共同复用。

## 全局目录位置

按以下优先级选择当前目录：

1. 单次命令使用的 `--catalog-root <absolute-path>`。
2. `INSPECT_DEPENDENCY_SOURCE_HOME`。
3. `config set-root` 保存的路径。
4. 操作系统标准用户数据目录。

默认数据目录在 Linux 上是 `${XDG_DATA_HOME:-$HOME/.local/share}/inspect-dependency-source`，在 macOS 上是 `~/Library/Application Support/Inspect Dependency Source`，在 Windows 上是 `%LOCALAPPDATA%\Inspect Dependency Source`。

```bash
python3 scripts/inspect_dependency_source.py config show
python3 scripts/inspect_dependency_source.py config set-root /absolute/path/to/catalog
```

修改设置不会迁移现有数据。运行时目录绝不会根据 Skill 安装目录或当前项目推断。

请选择专用目录作为目录根路径。文件系统卷根目录以及包含无关文件的目录会被拒绝；在 POSIX 系统上，所选目录将由本工具管理并设为 `0700` 权限。

元数据存储在启用 WAL 模式的 SQLite 中。托管压缩包、临时下载、已提升源码树、操作锁、仪表盘进程元数据以及已缓存的核对指标都位于同一目录下。仓库、本地源码、工件、包绑定、tag、操作和事件分别建模。使用方必须通过 CLI 或只读仪表盘 API 集成，不能依赖内部 schema。

存储模型、HTTP 端点、事务式工件处理和安全边界请参阅[架构与安全](references/architecture.md)。

## 隐私与安全

- 不收集遥测数据。
- 目录元数据和源码树保留在用户本机。
- 在 POSIX 系统上，目录权限限制为仅当前用户可访问（`0700`），目录状态文件使用仅所有者可读写权限（`0600`）。
- 只有显式执行 GitHub 注册、远程元数据刷新、源码下载或包解析时才会产生出站流量。
- 远程地址在持久化和展示前会移除凭据；错误、事件、JSON 和 API 输出也会脱敏。
- 解压过程拒绝路径穿越、符号链接逃逸、过多成员和过大体积。
- 托管删除操作会检查路径范围，绝不删除用户注册的本地源码树。
- 仪表盘不暴露任意源码文件、环境变量、机密或写操作端点。

仅限 localhost 并不代表可以安全公开。分享日志或仪表盘画面前，请检查仓库名、本地路径、包版本和操作历史。

## 开发

运行时代码必须保持仅依赖 Python 标准库；开发与浏览器验证可以使用可选工具。

```bash
# 标准库单元与集成测试。
python3 -m unittest discover -s tests -p 'test_*.py' -v

# 验证 Skill 元数据与结构。
python3 /path/to/skill-creator/scripts/quick_validate.py .

# 可选浏览器验证（先执行 `pip install playwright` 和
# `playwright install chromium`）。
python3 tests/browser_validation.py
```

浏览器测试应覆盖：双语状态持久化、仓库切换、增量操作时间线、响应式布局、系统主题、可访问性、安全响应头，以及控制台和网络错误。

发布前请确认：完整测试套件和 Skill 验证已通过；工作树干净；安装到 Codex 与 Claude 的副本都通过同一套 Skill 验证。

## 许可证

[MIT](LICENSE) © 2026 Momean。
