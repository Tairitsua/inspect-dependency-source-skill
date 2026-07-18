# Inspect Dependency Source

[English](README.md)

[![Release readiness](https://github.com/Tairitsua/inspect-dependency-source-skill/actions/workflows/release-readiness.yml/badge.svg)](https://github.com/Tairitsua/inspect-dependency-source-skill/actions/workflows/release-readiness.yml)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-5b45ff)](SKILL.md)
[![skills.sh](https://skills.sh/b/tairitsua/inspect-dependency-source-skill)](https://skills.sh/tairitsua/inspect-dependency-source-skill)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 只在需要时，为编码 Agent 提供准确的第三方依赖源码。

编码 Agent 往往很擅长理解当前仓库，但一旦跨过第三方依赖边界，可靠性就会明显下降：它可能只根据文档推断，查看上游最新分支而不是项目实际使用的版本，或者在多个工作区反复下载同一个大型仓库。

Inspect Dependency Source 是一个用户级 Agent Skill，它把准确依赖源码获取到可复用的本地目录中。它将包版本或 ref 解析到 commit，在使用前验证已缓存源码树，并在准确 ref 不存在时明确失败，绝不会静默替换为其他分支。Agent 会得到稳定的本地 `source_path`，以及检查该源码树所需的版本、commit、来源类别和验证状态。

运行时代码仅使用 Python 标准库。访问公开 Git 和 GitHub 仓库不强制依赖 GitHub CLI。

## 用户级安装一次

推荐按当前 OS 用户安装，让每个项目都能使用这个 Skill：

```bash
npx skills add Tairitsua/inspect-dependency-source-skill --global
```

公开包只包含一个 Skill，也可在 [skills.sh](https://skills.sh/tairitsua/inspect-dependency-source-skill) 查看。只有明确需要项目级安装时，才去掉 `--global`。

Claude Code marketplace 安装方式：

```bash
claude plugin marketplace add Tairitsua/inspect-dependency-source-skill
claude plugin install inspect-dependency-source@inspect-dependency-source
```

如果无法使用 `npx`，可将主 checkout 放在 Codex 用户级 Skill 目录，再让 Claude 链接到同一份内容：

```bash
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
git clone https://github.com/Tairitsua/inspect-dependency-source-skill.git \
  "$HOME/.agents/skills/inspect-dependency-source"
ln -s "$HOME/.agents/skills/inspect-dependency-source" \
  "$HOME/.claude/skills/inspect-dependency-source"
```

- Codex 用户级 Skill：`$HOME/.agents/skills/inspect-dependency-source`
- Claude 用户级 Skill：`~/.claude/skills/inspect-dependency-source`

如果环境不支持符号链接，也可以分别将仓库 clone 或复制到两个目录。运行时目录与 Skill 安装目录和当前项目相互独立，因此所有安装仍会复用同一个用户级源码目录。

环境要求：

- Python 3.11 或更高版本，并包含标准 `sqlite3` 模块。
- 处理 Git 仓库的注册和下载时需要 Git。
- 只有远程刷新、下载或 NuGet 解析需要网络。
- 私有 GitHub 仓库或更高 API 限额可选用 `gh`、`GH_TOKEN` 或 `GITHUB_TOKEN`。

## 在每个项目中启用自动调用

将下面这段简短的路由规则加入项目的 `AGENTS.md`：

```md
## 依赖源码检查

当分析或调试依赖第三方库或 SDK 的内部实现时，即使用户没有明确点名，也要自动使用用户级 `inspect-dependency-source` Skill。解析项目实际使用的准确版本或 ref，获取前先复用目录中匹配的源码，只读检查返回的 `source_path`，并且绝不使用上游默认分支代替目标版本。
```

用户级安装让 Skill 处于可用状态；`AGENTS.md` 规则则告诉项目中的 Agent：任务一旦跨过依赖边界，就应主动调用它，而不需要用户在每个提示词里重复点名。

[Claude Code 读取的是 `CLAUDE.md`，而不是 `AGENTS.md`](https://code.claude.com/docs/en/memory#agentsmd)。如果项目也使用 Claude Code，请在仓库根目录添加一个 `CLAUDE.md` 来导入共用规则，不要复制两份内容：

```md
@AGENTS.md
```

新增或修改这些项目指导文件后，请开启一个新的 Agent 会话，让更新后的规则被重新加载。

## 直接询问真正的问题

加入路由规则后，直接问你关心的问题即可：

```text
为什么 Newtonsoft.Json 13.0.3 会用这种方式序列化这个值？
```

Agent 应识别出答案依赖第三方实现，获取项目实际使用的准确源码，只读检查它，并在结论中注明包版本和已解析 commit。你不需要在提示词里指定目录命令。

## 自动工作流程

1. 读取项目已经解析的依赖数据，确定准确包版本或仓库 ref。
2. 下载前先对用户级目录执行 `resolve --json`。
3. 如果没有匹配且可用的源码树，使用对应的 NuGet、GitHub、Git 或本地源码命令获取。
4. 再次解析，确认预期 commit、实际 commit、来源类别和完整性状态。
5. 只读检查返回的 `source_path`，不修改目录托管的文件。
6. 回答原始问题，并注明实际检查的依赖版本和 commit。

同一台机器上的项目与 Agent 都会复用这个目录，因此某个工作区已经获取的依赖无需再次下载。明确指定的 ref 不存在就是阻断条件：Skill 绝不会换用上游默认分支。

稳定的机器可读契约是：

```bash
python3 scripts/inspect_dependency_source.py \
  resolve Package.Id --ref 1.2.3 --json
```

成功结果包含 `source_path`、`verification_state` 和 `resolution_kind`，以及仓库、工件和可选的包来源信息。读取路径前必须检查进程退出码与结果状态。完整 schema 和确定性错误结构请参阅[稳定的本地数据契约](references/schema.md)。

[`Newtonsoft.Json 13.0.3` 回放](examples/newtonsoft-json-13.0.3/README.md)演示如何获取准确 NuGet 源码，并解析到 commit `0a2e291c0d9c0c7675d445703e51750363a549ef`，同时不把机器相关的本地路径提交进仓库。

## 仪表盘与可观测性

![Inspect Dependency Source 仪表盘，展示目录健康状态、已验证源码清单和仓库来源信息](docs/images/dashboard-overview.png)

响应式、只读的 localhost 仪表盘让用户级目录保持可观测。它展示：

- 仓库、工件、包绑定和验证数量。
- 支持搜索的源码清单、别名和已清理敏感信息的远程地址。
- 准确 ref/commit 来源以及首选源码路径。
- 已缓存 tag、本地 Git 快照、manifest 和新鲜度。
- 正在执行或失败的操作及可展开事件时间线。
- 完整性告警、缓存磁盘占用和剩余空间。

除非指定 `--no-dashboard`，`init` 会启动或复用仪表盘。也可单独管理：

```bash
python3 scripts/inspect_dependency_source.py dashboard start
python3 scripts/inspect_dependency_source.py dashboard status
python3 scripts/inspect_dependency_source.py dashboard stop
```

UI 支持英文与简体中文、跟随系统的亮/暗色主题、减少动态效果、键盘操作，以及 360/768/1440 像素响应式布局。语言、主题、筛选条件、选中仓库和展开的时间线会在两秒刷新周期中保持不变。

服务只绑定 `127.0.0.1`，不加载 CDN 资源，不启用 CORS，仅开放 GET/HEAD，也不提供下载、删除或其他写操作控件。

## 支持的源码类型

- GitHub 仓库。
- 通用 Git 远程仓库。
- 已存在的本地源码树。
- 准确的 NuGet 包版本；当包元数据提供仓库 commit 时，优先保留该来源信息。

首个版本不包含 npm、PyPI、Maven 和 Cargo 包解析器，但仍可通过 Git 或本地源码方式注册这些包的仓库。

[opensrc](https://github.com/vercel-labs/opensrc) 适合通用包源码获取；[Context7](https://context7.com/docs/overview) 提供版本相关的文档和示例。本 Skill 聚焦准确、可复用的源码树，以及找不到 ref 时明确失败的解析方式。

## 高级 CLI 与目录管理

从已安装的 Skill 目录运行命令：

```bash
cd "$HOME/.agents/skills/inspect-dependency-source"

# 初始化目录和仪表盘，并检查本地前置条件。
python3 scripts/inspect_dependency_source.py init
python3 scripts/inspect_dependency_source.py doctor

# 获取并解析准确 NuGet 依赖。
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --json

# 获取并解析准确 GitHub ref。
python3 scripts/inspect_dependency_source.py repo add-github owner/repository --alias example
python3 scripts/inspect_dependency_source.py repo fetch example --ref v1.2.3
python3 scripts/inspect_dependency_source.py resolve example --ref v1.2.3 --json

# 注册已经存在的本地源码。
python3 scripts/inspect_dependency_source.py repo add-local /absolute/path/to/source --alias example
python3 scripts/inspect_dependency_source.py resolve example --json

# 核对已缓存源码并检查目录健康状态。
python3 scripts/inspect_dependency_source.py verify --all --json
python3 scripts/inspect_dependency_source.py status
```

所有命令请参阅 [CLI 参考](references/cli.md)；准确 ref、NuGet、本地、离线、恢复和删除流程请参阅[源码检查工作流](references/workflows.md)。仓库删除必须先预览，再使用准确仓库 ID 与匹配的计划令牌，并获得明确授权；清理托管缓存还有额外确认门。

### 目录位置

按以下优先级选择当前目录根路径：

1. 单次命令使用的 `--catalog-root <absolute-path>`。
2. `INSPECT_DEPENDENCY_SOURCE_HOME`。
3. `config set-root` 保存的路径。
4. 操作系统标准用户数据目录。

默认路径在 Linux 上是 `${XDG_DATA_HOME:-$HOME/.local/share}/inspect-dependency-source`，在 macOS 上是 `~/Library/Application Support/Inspect Dependency Source`，在 Windows 上是 `%LOCALAPPDATA%\Inspect Dependency Source`。

```bash
python3 scripts/inspect_dependency_source.py config show
python3 scripts/inspect_dependency_source.py config set-root /absolute/path/to/catalog
```

修改设置不会迁移现有数据。运行时目录绝不会根据 Skill 安装目录或当前项目推断。文件系统卷根目录以及包含无关文件的目录会被拒绝；在 POSIX 系统上，所选目录将由工具管理并设为 `0700` 权限。

元数据存储在启用 WAL 模式的 SQLite 中。托管压缩包、临时下载、已提升源码树、操作锁、仪表盘进程元数据以及已缓存的核对指标都位于同一根目录下。使用方必须通过 CLI 或只读仪表盘 API 集成，不能依赖内部 schema。存储模型、HTTP 端点、事务式工件处理和安全边界请参阅[架构与安全](references/architecture.md)。

## 安全边界

- 不收集遥测数据；目录元数据和源码树保留在用户本机。
- 在 POSIX 系统上，目录仅当前用户可访问（`0700`），目录状态文件使用 `0600`。
- 只有显式执行 GitHub 注册、远程元数据刷新、源码下载或包解析时才会产生出站流量。
- 远程地址在持久化和展示前会移除凭据；错误、事件、JSON 和 API 输出也会脱敏。
- 解压过程拒绝路径穿越、符号链接逃逸、过多成员和过大体积。
- 下载先进入临时目录，验证通过后再原子提升；替换失败时保留上一份已验证工件。
- 托管删除操作会检查路径范围，绝不删除用户注册的本地源码树。
- 仪表盘不暴露任意源码文件、环境变量、机密或写操作端点。
- 执行任何 `repo remove` 前，Agent 必须预览准确目标并暂停等待明确授权；清理托管缓存还需要另外的明确同意。

仪表盘与 CLI 可能显示仓库名、本地路径、包版本和操作历史。请将其视为本地开发数据，在把日志或截图发送到机器外部之前先检查内容。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `SKILL.md` | Agent 触发条件、准确源码工作流、失败规则和破坏性操作暂停点。 |
| `scripts/` | 仅依赖标准库的 CLI、目录、provider、完整性检查和仪表盘。 |
| `references/` | CLI、稳定的本地 schema、架构和 provider 工作流契约。 |
| `examples/` | 可复现的准确依赖源码回放。 |
| `assets/dashboard/` | 离线仪表盘 HTML、CSS 和 JavaScript。 |
| `tests/` | 单元、集成、安全、打包和真实浏览器验证。 |

## 开发

运行时代码必须保持仅依赖 Python 标准库；开发与浏览器验证可以使用可选工具。

```bash
# 标准库单元与集成测试。
python3 -m unittest discover -s tests -p 'test_*.py' -v

# 验证 Skill 元数据与结构。
python3 /path/to/skill-creator/scripts/quick_validate.py .

# 验证发布元数据、文档链接、泄漏规则、CI 与素材。
python3 scripts/validate_release.py

# 可选浏览器验证（先执行 `pip install playwright` 和
# `playwright install chromium`）。
python3 tests/browser_validation.py
```

浏览器测试覆盖双语状态持久化、仓库切换、增量操作时间线、响应式布局、系统主题、可访问性、安全响应头，以及控制台和网络错误。

发布前请确认：完整测试套件和 Skill 验证已通过；工作树干净；安装到 Codex 与 Claude 的副本都通过同一套 Skill 验证。

发布记录见 [CHANGELOG.md](CHANGELOG.md)，发布叙事见 [v1.0.0 草案](docs/release-notes/v1.0.0.md)。

## 致谢

- 跨 Agent 包结构遵循 [Agent Skills specification](https://agentskills.io/specification)。
- [opensrc](https://github.com/vercel-labs/opensrc) 是通用包源码获取的重要参考。
- [Context7](https://context7.com/docs/overview) 可以为源码检查补充版本相关文档与示例。
- Claude marketplace 结构参考 [Anthropic 公开 Agent Skills marketplace](https://github.com/anthropics/skills/blob/main/.claude-plugin/marketplace.json)。

## 许可证

[MIT](LICENSE) © 2026 Momean。
