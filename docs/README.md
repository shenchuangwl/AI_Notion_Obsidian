# 文档索引

本目录收录 AI_Notion_Obsidian 的全部使用、开发、部署、运维、排障与设计文档，均为简体中文（命令和代码标识保持英文）。返回[项目首页](../README.md)。

## 按角色选读

| 你是 | 建议阅读顺序 |
|---|---|
| 工作区所有者 / 日常使用者 | [操作手册](usage/operations-manual.md) → [故障排查](troubleshooting/troubleshooting.md) |
| 首次部署的开发者 / 维护者 | [部署指南](deployment/deployment-guide.md) → [运维指南](operations/operations-guide.md) → [操作手册](usage/operations-manual.md) |
| 修改代码的开发者 | [开发者指南](development/developer-guide.md) → [导出规格 SPEC](design/SPEC.md) → [写回规格 WRITEBACK_SPEC](design/WRITEBACK_SPEC.md) |
| 想了解设计取舍的读者 | [双向同步设计稿](design/BIDIRECTIONAL_DESIGN.md) → 两份规格 |

## 文档清单

| 分类 | 文档 | 内容 | 读者 |
|---|---|---|---|
| 使用 | [usage/operations-manual.md](usage/operations-manual.md) | 官方操作手册（用户侧）：环境与前置条件、初始化、Notion → Obsidian 自动同步、Obsidian → Notion 写回预览与本人审批、冲突处理、常见问题、维护与后续启用范围 | 所有者、维护者 |
| 部署 | [deployment/deployment-guide.md](deployment/deployment-guide.md) | 在全新 Ubuntu 机器上部署：先决条件、写死的路径与常量（文件:行）、安装、Notion 集成、令牌、配置键说明、首次快照与发布、systemd 定时器、升级 / 回退摘要、检查清单 | 开发者、维护者 |
| 运维 | [operations/operations-guide.md](operations/operations-guide.md) | 日常运维汇总：职责边界、调度与锁、日志 / 报告 / 运行记录位置、轮次结果与退出码、暂停处理、手动刷新、快照保留与磁盘、备份、急停、编辑冻结与升级 | 维护者、所有者 |
| 排障 | [troubleshooting/troubleshooting.md](troubleshooting/troubleshooting.md) | “现象 → 原因 → 处理”对照表，覆盖调度、侧栏采集、校验与暂停、冲突与删除、写回审批、凭据与环境、数据库与磁盘，并链接到手册详细章节 | 所有人 |
| 开发 | [development/developer-guide.md](development/developer-guide.md) | 仓库结构、开发环境、编码约定、单元 / 端到端 / 手动回归测试、演练方法，以及转写自 README-DEV.txt 的部署与回退 Runbook（D1–D5 / R1–R5） | 开发者 |
| 设计 | [design/SPEC.md](design/SPEC.md) | Notion → Obsidian 导出契约 v1.3：路径与角色、目录与命名、渲染规则、校验项、发布与双向同步配套修订 | 开发者 |
| 设计 | [design/WRITEBACK_SPEC.md](design/WRITEBACK_SPEC.md) | Obsidian → Notion 手动写回功能规格 v1.1：角色与权限、计划 / 审批 / 执行、冲突与删除、熔断、额度、配置默认值 | 开发者 |
| 设计 | [design/BIDIRECTIONAL_DESIGN.md](design/BIDIRECTIONAL_DESIGN.md) | 双向同步调研与可落地设计 v0.1（设计稿，含决策过程） | 开发者、评审者 |
| 参考 | [reference/obsidian-bases.md](reference/obsidian-bases.md) | Obsidian Bases（`.base`）语法核实记录，导出器生成数据库视图的依据 | 开发者 |
| 参考 | [reference/obsidian-custom-sort.md](reference/obsidian-custom-sort.md) | custom-sort 3.2.0 插件核实记录，排序规则生成的依据 | 开发者 |

配套的示例文件在 [`examples/`](../examples/) 目录：

| 文件 | 用途 |
|---|---|
| [`notion-migrate.env.example`](../examples/notion-migrate.env.example) | 读连接令牌文件模板（`~/.config/notion-migrate/.env`） |
| [`writeback.env.example`](../examples/writeback.env.example) | 写回连接令牌文件模板（`~/.config/notion-obsidian/writeback.env`） |
| [`writeback.example.json`](../examples/writeback.example.json) | 写回配置（`config/writeback.json`），安全默认值 `mode: "off"` |
| [`roots_sidebar.example.json`](../examples/roots_sidebar.example.json) | 顶层页面根清单格式（`snapshot.py --roots` 的输入） |
| [`standing_daily_refresh.approval.example.md`](../examples/standing_daily_refresh.approval.example.md) | 定时发布所需的长期授权记录模板 |

## 如何阅读与访问

- **在 GitHub 上在线阅读。** 打开仓库 <https://github.com/shenchuangwl/AI_Notion_Obsidian> → `docs/` 目录，点击文档即可渲染。文档之间都用相对链接，章节链接使用文中的显式锚点（如 `operations-manual.md#sec-5-3`）。GitHub 会在文件右上角提供目录（Outline）按钮，长文档可以用它跳转。
- **本地阅读。** `git clone https://github.com/shenchuangwl/AI_Notion_Obsidian.git` 后用任意 Markdown 阅读器打开 `docs/`；也可以把 `docs/` 当作 Obsidian 库打开（在 Obsidian 中，指向 `#sec-*`、`#dep-*` 等显式锚点的链接只会打开文件、不会跳到对应章节；章节跳转请在 GitHub 或支持 HTML 锚点的阅读器中使用）。
- **以代码为准。** 规格与设计文档保留了编写当时的状态和决策历史；命令、参数和配置键以 `notion-obsidian/tools/` 中的代码和各工具的 `--help` 为准，例如 `python3 -I -B notion-obsidian/tools/writeback.py --help`。
- **生产路径。** 文档中的命令按生产部署编写（WORK `/data120/Obsidian/notion-obsidian`，库 `/data120/Obsidian/Godcreate`）。这些路径写死在代码中，详见[部署指南 §2](deployment/deployment-guide.md#dep-2)。
- **文档更新。** 修改用户可见的命令、提示、配置键或流程时，同步更新相关文档；操作手册的变更记录在其末尾的表格中。
