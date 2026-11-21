> **设计 / 规格文档**｜Obsidian → Notion 手动写回功能规格｜版本 **v1.1**｜仓库副本，正文未改动（来源：开发沙箱 `notion-obsidian-dev/docs/WRITEBACK_SPEC.md`）。文首“功能未实现、未启用（`mode = off`）”是规格定稿当时的状态：写回现已实现，生产配置为 `plan` 模式（见[操作手册 §2](../usage/operations-manual.md#sec-2)）。文中引用的 `WRITEBACK_SPEC.review-v1.0.md`、`WRITEBACK_SPEC.v1.0.md` 等审查稿未收入本仓库。实际行为以 [`notion-obsidian/tools/`](../../notion-obsidian/tools/) 代码为准。返回[文档索引](../README.md)。

# Obsidian → Notion 手动写回：功能规格（WRITEBACK_SPEC v1.1）

- 状态：**规格定稿（v1.1），功能未实现、未启用（`mode = off`）**。在第 17 节阶段 0–2 的验收全部通过之前，任何代码路径都不得向 Notion 发出写请求。
- 适用范围：Notion 工作区 **Godcreate**（workspace_id `c893af62-39e0-4e16-bfa8-2ca081395c81`）与本地库 `/data120/Obsidian/Godcreate`（Obsidian Sync 远程库 “Godcreate”）。
- 依据：用户 2026-11-17 的书面规则（第一至五部分，下文以【U-I】…【U-V-3】引用）；设计稿 `docs/BIDIRECTIONAL_DESIGN.md` 及其对抗式审查（【S1】–【S6】、【H1】–【H8】、【M1】–【M12】）；导出契约 `docs/SPEC.md` v1.2；对 v1.0 的三视角对抗式审查（76 条，原文存于 `docs/WRITEBACK_SPEC.review-v1.0.md`，v1.0 原文存于 `docs/WRITEBACK_SPEC.v1.0.md`）。
- 首要目标：**不覆盖、不误改、不误删 Notion 内容**。凡是不能证明安全的操作，一律不执行，只报告。
- 变更记录：v1.0（17:40）→ v1.1：读集成降权与只读客户端；白名单按精确 ID 并处理授权继承；确认改为“用户本人在交互终端 + 现场挑战码 + 范围短语”；块删除单列；进程互斥与计划 CAS；属性/标题预检；块额度改按官方“全工作区终身累计”；删除与改名的三方比较；hold 与命名差异的实现契约；Notion → Obsidian 暂停规则的落地方式；新增第 19 节“对现有契约与代码的修改”；API 事实修正。

---

## 1. 总原则

1. **单向自动，反向手动**：Notion → Obsidian 自动同步（现有管线）；Obsidian → Notion 只能由用户手动发起，先生成预览，用户本人确认后才写入【U-II】。
2. **未确认 = 只读**：没有有效确认时，系统只能读取、比较、生成预览和一致性检查【U-II-5】。写回令牌只在“执行已确认计划”及其回退中加载；所有比较、预检、前像和回读一律使用只读客户端（2.2）。
3. **块级、最小写入**：只修改有变化的块；未变化的块绝不重写；绝不整页覆盖，绝不调用 `erase_content` 或 Markdown 写入端点【S4】。
4. **Notion 权限是写回集成的硬边界**：写回集成只共享给声明的有效范围（2.3），每次 apply 前用 audit-scope 核验；前提是读集成已降为只读（2.2），否则此边界不成立。
5. **删除、改名、移动永不自动**：各自作为单独的高风险项，在预览中标出并经单独确认【U-III、U-IV】。
6. **先证明，再写入；写后必回读**：计划时三方比较；执行时逐页、逐请求实时预检；写后按规范化 JSON 回读校验；校验失败的页面立即冻结，不自动重试、不自动补偿【S6】。
7. **一切可追溯、可回退**：每个写操作发出前把意图与前像落盘；每次执行生成可复核的报告与回退材料；回退本身也要预检，不覆盖他人的新编辑。
8. **自动管线遇异常即暂停相关写入**：Notion → Obsidian 发现冲突、映射异常、缺失、重复、覆盖风险或校验失败时，暂停相关对象（或整轮）的写入并留下审计轨迹【U-V-3】（第 14 节）。

---

## 2. 角色、凭据与权限边界

### 2.1 集成角色

| 项目 | 读集成（现有，须降权） | 写回集成（新建） |
|---|---|---|
| 名称 | `Godcreate`（现有内部集成，`NOTION_TARGET_TOKEN`） | `Godcreate-Writeback`（建议名） |
| 能力 | **仅** Read content；另勾选 User information without email addresses（用于成员数监控，9.2）；取消 Update content、Insert content、Insert comments | Read content、Update content、Insert content；不勾选评论与用户信息 |
| Notion 侧共享 | 五个团队协作区（不变） | 只共享声明的有效范围（2.3）：阶段 2 为 1 个测试页 + 不超过 10 个白名单页面 |
| 凭据文件 | `~/.config/notion-migrate/.env`（600） | `~/.config/notion-obsidian/writeback.env`（600，变量 `NOTION_WRITEBACK_TOKEN`） |
| 使用者 | refresh、snapshot、assets_fetch；writeback 的 plan/show/status/whitelist-check 及 apply 中的全部读取 | 只有 writeback 的 apply、rollback 中“改变 Notion 的请求”和 audit-scope |

- 写回客户端固定 `Notion-Version: 2026-03-11`（只接受 `in_trash`，`archived` 已移除）。
- 令牌永不打印、不进日志或报告；写回工具启动时检查凭据文件权限，不是 600 就拒绝运行。
- 撤销：开发者门户 `https://app.notion.com/developers/connections` → `Godcreate-Writeback` → 危险区“删除”（或在“内容访问权限”中移除页面），并删除 `writeback.env`；撤销后写回工具自动退回只读。

### 2.2 读集成降权与只读客户端【S1 设计审查、v1.0 审查 safety#1、coverage#14】

- 现状：读集成在迁移阶段被授予过写能力，nmig 的 relink/repair/cleanup 曾用它写入 Notion。**阶段 2 开始前，用户须在 Notion 集成设置中把它降为只读（或新建只读集成替代）；完成之前 `mode` 不得高于 `plan`。**写回启用后不得再对 Godcreate 运行 nmig 的写命令。
- `ReadOnlyClient`（本项目新模块，子类化 `NotionClient`，不修改 `notion-migration`）：只允许 `GET`、`POST /v1/search`、`POST /v1/data_sources/{id}/query`，其余方法和路径在发出前抛错。refresh、snapshot、assets_fetch 以及 writeback 的读取一律使用它；阶段 0 用单元测试覆盖。

### 2.3 白名单语义与授权继承【safety#2、coverage#13、api#2】

- 官方：集成获得某页面的访问后，可读写该页面及其全部后代（子页面、内联数据库及其行）（https://developers.notion.com/reference/capabilities ）。因此 Notion 侧的“硬边界”是子树。
- `whitelist_page_ids` **按精确 ID 匹配，不含后代**；写回只能修改列出的页面本身，新建页面只能挂在列出的页面下。需要包含后代时，在 `whitelist_scope` 中声明 `{"<page_id>": "subtree"}`，预览列出全部后代并由用户逐项确认；未列出的后代即使在 Notion 中可写，计划器也判 OUT_OF_WHITELIST。
- 数据库行：只有列入 `whitelist_database_ids` 的数据库，其行才允许更新或新建（新建行的父级为该库的数据源）。
- 阶段 2 只共享“叶子页面”：正文任意深度都不含 child_page、child_database、内联数据库；plan 与 apply 时用快照与实时读取各检查一次。
- `writeback.py whitelist-check`（只读）：列出每个白名单页面的后代页面数、内联数据库数与行数，作为“共享后实际可写范围”提示，用户在 Notion 中共享前运行。
- `writeback.py audit-scope`（只读，使用写回令牌）：分页调用 `POST /v1/search`，列出写回集成可访问的全部页面和数据库，与声明的有效范围逐项比对；多出任何对象或白名单页面不可达，即把 `mode` 强制降为 `plan` 并报警。每次 apply 开始时自动执行。
- W3 中“目标”的定义：块操作取该块经实时 GET 父链得到的最近页面祖先；页面与属性操作取页面本身；新建页面取父页面（新建行取所属数据库）。

### 2.4 允许与禁止的 Notion 操作

| 操作 | API | 阶段 2 | 阶段 3 | 说明 |
|---|---|---|---|---|
| 修改块文字/勾选/代码语言 | `PATCH /v1/blocks/{id}` | 允许 | 允许 | 只发送 `rich_text`（及 `checked`、`language`），不发送块级 `color`（不发送即保持原值）。`rich_text` 整体替换（https://developers.notion.com/reference/update-a-block ），复用的片段须连同全部 annotations、`text.link`、mention 对象原样重发；含带色片段的块按 4.3 归 X 类，不写 |
| 追加新块 | `PATCH /v1/blocks/{id}/children` | 允许 | 允许 | `position` 用 `after_block` 或 `start`（插到父块首位）；锚点须与插入行同父级、且不在 X 区域内部，锚点本身可以是任何类型的同级块（https://developers.notion.com/reference/patch-block-children ）；受块额度约束（第 9 节） |
| 删除叶子块（移入回收站） | `DELETE /v1/blocks/{id}` | **默认禁用**，H1 实测通过后才可开启 | 同左 | 只作为 B 项单独确认（4.4、6.7）；目标块须为 R 类、不是 child_page/child_database/link_to_page/synced_block，发请求前实时 GET 要求 `has_children=false` |
| 更新数据库行属性 | `PATCH /v1/pages/{id}`（properties） | 允许（白名单数据库） | 允许 | 见 2.4.1 的类型规则 |
| 修改页面标题 | `PATCH /v1/pages/{id}`（title） | 仅 T 项单独确认 | 同左 | 第 7 节 |
| 新建页面 / 新建行 | `POST /v1/pages` | 仅 N 项确认、父页面在白名单 | 同左 | 第 5.3 节；受块额度约束 |
| 页面移入回收站 | `PATCH /v1/pages/{id}`（`in_trash: true`） | 仅 D 项单独确认 | 同左 | 第 6 节（https://developers.notion.com/reference/trash-page ） |
| 恢复页面（仅回退） | `PATCH /v1/pages/{id}`（`in_trash: false`） | 允许 | 允许 | 不在团队协作区内的顶层页面 API 无法恢复（400），只能在界面恢复 |
| 移动页面 | `POST /v1/pages/{id}/move` | **禁止** | 仅 MV 项单独确认（可选开启） | 只能移动普通页面，不能移动数据库；新父级只能是 page_id 或 data_source_id，写回 bot 须对新父级有编辑权限；无同级位置参数（https://developers.notion.com/reference/move-page ） |
| 修改页面锁定状态（`is_locked`） | — | **永久禁止** | **永久禁止** | API 不受页面锁定限制（https://developers.notion.com/reference/patch-page ），锁定完全由本工具拦截 |
| 修改数据库结构、视图、模板；新建选项 | — | 禁止 | 禁止 | 只在 Notion 中手工操作 |
| 写同步块、分栏结构、目录块、link_preview | — | 禁止 | 禁止 | API 不支持或有损 |
| 评论、文件上传 | — | 禁止 | 禁止（另行评审） | — |
| `erase_content`、Markdown 写入端点 | — | **永久禁止** | **永久禁止** | `erase_content` 不可撤销；Markdown 端点不保证保留块 ID |

#### 2.4.1 行属性写回的类型规则【safety#8、api#10】

- rich_text：仅当 Notion 原值是单段、无 annotations、无 href、无 mention 的纯文本时可写，否则拒绝。
- select、multi_select、status：每个值都须是数据源 schema 中已存在的选项（按名称精确匹配，区分大小写，不含逗号），否则判 SCHEMA_OPTION_MISSING，只报告；不得新建选项（新选项名会自动加入 schema，https://developers.notion.com/reference/page-property-values ）。
- date：写入完整对象（start、end、time_zone）；原值有 time_zone 或时间部分而库内的值不能无损表达时拒绝。
- number：有限数值；checkbox、url、email、phone_number：按类型校验。
- relation、people、files、formula、rollup、created_*、last_edited_*、unique_id、verification、button：只读。
- 预览显示每个属性的原始 JSON 与拟写入 JSON。

### 2.5 副作用控制【api#11】

- 所有 Create a page、Update page、Append block children 请求都带 `notifications: {mode: "silent"}`。Update a block 不支持该参数：含用户提及的块在阶段 2 实测不会重复通知之前归入 X 类。
- 数据库自动化不受静默模式影响：只有在 `automation_free_database_ids` 中声明“无自动化”的数据库，其行属性才允许写回；其他数据库的行属性项在预览中标“可能触发自动化，未执行”。

### 2.6 自动管线对 Notion 的约束（不变）

refresh、snapshot、assets_fetch 只经 `ReadOnlyClient` 发只读请求；refresh 不导入写回模块、不读取写回凭据。由第 17 节阶段 0 的测试保证。

---

## 3. 数据与状态

新增状态库 `state/writeback.db`：

| 表 | 用途 |
|---|---|
| `base_history(notion_id, seq, rel_path, sha256, page_json_sha, renderer_version, run_id, origin)` | 每篇笔记每次**实际发布或确认一致**的版本链；正文存于 `state/base/<sha256>`。只由发布器在 ADD/REPLACE/MOVE 成功（回读 sha = 暂存 sha）、UNCHANGED、UNCHANGED_SEMANTIC、ADOPT、ADOPT_RENAME 时追加；CONFLICT_*、SKIP_*、hold 中、暂停中的对象不追加【coverage#18】。origin ∈ {REMOTE, WRITEBACK} |
| `block_lines(notion_id, base_sha, block_id, owner_page_id, parent_block_id, ord, depth, type, own_start, own_end, line_start, line_end, prefix, zero_line, klass, json_sha)` | 导出器按 (notion_id, 暂存 sha) 记录“块 ↔ 行范围”。行号含 frontmatter；`own_*` 为块自身行，`line_*` 含子块；`zero_line=1` 表示被合并的空段落；`owner_page_id ≠ notion_id`（同步块引用渲染出的源块）一律 X；不属于任何块的分隔行和空行记为 gap【api#12】 |
| `plans(plan_id, created_at, expires_at, plan_sha256, status, scope, counts_json)` | status：draft → confirmed → applying → applied / partially_applied / aborted / expired / superseded；状态迁移一律用条件更新（CAS） |
| `plan_items(plan_id, item_id, notion_id, rel_path, category, ops_json, risk, decision, reason, l_sha, r_json_sha, verify_status)` | item_id 前缀：N 新增、M 修改、B 块删除、D 页面删除、T 改名、MV 移动、C 冲突；verify_status：pending_next_round → verified / needs_manual |
| `confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, phrase_scope)` | 用户本人确认记录（第 10 节） |
| `journal(seq, plan_id, item_id, op, target_id, state, before_json, request_json, expected_after_json, response_status, committed_ids, after_json, ts)` | 每个写请求：发出前写 intent 并 fsync；state ∈ {intent, sent, done, unknown, reconciled} |
| `pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, status, ttl_until)` | 库内删除的待决项：hold / confirmed / restored / expired |
| `name_overrides(notion_id, old_rel, local_rel, notion_title_at_detect, since, status)` | 未写回的本地改名（7.4） |
| `page_state(notion_id, state, reason, since)` | normal / BLOCKED / PARTIAL_CREATE |
| `creation_state(ts, members, plan, state, source)` | 块额度状态：unlimited / blocked_assumed / blocked_confirmed（第 9 节） |
| `block_budget(ts, plan_id, item_id, created_blocks)` | 新建块审计台账（按请求体递归计数），只作审计 |
| `trash_archive(notion_id, archived_at, reason, last_snapshot, object_json, blocks_json, inbound_refs_json)` | 页面首次从快照消失或被写回移入回收站时的最后一份内容；不参与清理【H2】（与发布器的 TOMBSTONE 报告不同名不同义） |

`state/auto_sync_hold.json`：Notion → Obsidian 自动同步中被暂停的对象（第 14 节）。`state/WRITEBACK_STOP`：急停文件（存在即停止）。

---

## 4. 变更检测与分类（只读）

### 4.0 扫描范围【coverage#4】

写回扫描与第 14 节的映射检查只覆盖五个团队协作区文件夹下的 `.md`，并排除：`.obsidian/**`；`Notion_Obsidian/**`（含 conflicts、运行记录、写回预览）；`*.base`；`*/_attachments/**`；`*/_templates/**`（模板只读）；`notion_kind ∈ {database, generated, template}` 的笔记；`.nmig-tmp-*`；文件名含 “(Conflicted copy ” 的 Obsidian Sync 冲突副本。Sync 冲突副本单列为“Sync 冲突副本”：对应笔记本轮既不写回、也不被自动覆盖，标记人工介入。

### 4.1 三方比较与 JSON 规范化

- **L**：库内当前文件；**B**：`base_history` 最新一条（含 `page_json_sha`）；**R**：Notion 当前内容（经只读客户端实时读取；多值属性分页取全【M2】）。
- 规范化 `json_norm`（比较与计算摘要前）：删除 `created_*`、`last_edited_*`、`request_id`；`type=file` 的块删除 `file.url`、`file.expiry_time`；mention 片段删除 `plain_text`、`href`，只比较 mention 类型与目标 ID；保留 `has_children`、类型对象的全部字段与 annotations。页面对象比较 properties（分页取全）、title、icon、cover、is_locked【api#6】。
- `last_edited_time` 只精确到分钟：它变了只能说明 R 可能变了；没变不能证明 R 没变，因此 R 一律按规范化 JSON 比较（plan 阶段可按 snapshot.py 同一判据跳过“快照后未变”页面的块读取，apply 预检不得跳过）。

### 4.2 分类

| 情形（L 相对 B / R 相对 B） | 分类 | 处理 |
|---|---|---|
| 未变 / 未变 | UNCHANGED | 无操作 |
| 未变 / 已变 | REMOTE_CHANGED | 不写回；由自动同步处理 |
| 已变 / 未变 | LOCAL_MODIFIED | 进入 M（及 B）项，再经 4.3、4.4 检查 |
| 已变 / 已变 | CONFLICT_BOTH | C 项；不写回（第 8 节） |
| 文件消失 / 未变 | LOCAL_DELETED | 按第 6 节处理 |
| 文件消失 / 已变 | CONFLICT_DEL_EDIT | 不进入 hold；下一轮按 Notion 版本恢复并列入运行记录【safety#9】 |
| 新文件（无 notion_id、无映射） | LOCAL_NEW | 按 5.3 判定能否成为 N 项 |
| 同 notion_id 改名 / Notion 标题未变 | LOCAL_RENAMED | T 项（第 7 节） |
| 同 notion_id 改名 / Notion 标题已变 | CONFLICT_RENAME | C 项，不生成可确认的 T 项 |
| 同 notion_id 移到其他文件夹 | LOCAL_MOVED | 阶段 2：UNSUPPORTED_RENAME；阶段 3：MV 项 |
| Notion 已在回收站 / 库内又出现 | REMOTE_TRASHED_LOCAL_PRESENT | 不恢复 Notion、不删库内文件；冲突并人工介入【coverage#22】 |

一律拒绝写回、只报告：

| 分类 | 判定 | 依据 |
|---|---|---|
| STALE_REVERT | B→L 的任一区段（含删除），与 `base_history` 中任一次 REMOTE 发布引入的区段重叠，且 L 在该区段等于那次发布之前的版本 | 【S1】【safety#21】 |
| SYNC_SETTLING | 本机无 Obsidian 进程（客户端未运行，整轮不生成可执行项）；进程启动后 `sync_settle_minutes` 内；或任意 120 秒窗口内扫描范围内 mtime 落入该窗口的文件数 ≥ `sync_burst_files`，这些文件本轮判 SYNC_SETTLING | 【S1】【coverage#27】 |
| UNSUPPORTED_REGION | 改动行落在 X 类块内（4.3） | 【设计 §7.2】【M4】【M5】 |
| PARTIAL_DELETE | 只删了 folder note，子文件仍在 | 【H5】 |
| LINK_REWRITE_NOOP | 只有 wikilink 写法变化，解析到的 notion_id 不变 | 【H3】：不写 Notion（发布器处理见 7.5） |
| LOCAL_KEYS_ONLY | 只改了不在 Notion schema 中的 frontmatter 键 | 【M1】：保留在库内，不写 Notion |
| READONLY_KEY_CHANGED | 改了 `notion_*` 只读键 | 【M1】：记冲突 |
| LOCKED | Notion 页面 `is_locked = true`（API 不拦截，完全由本工具在 plan、预检、回读时拦截） | 【M3】【api#20】 |
| OUT_OF_WHITELIST | 目标不在声明的有效范围 | 【U-II-2】 |
| BLOCKED_PAGE | 页面处于 BLOCKED / PARTIAL_CREATE | 【S6】 |
| RENDERER_MISMATCH | L 的渲染器版本与当前不一致 | 【M7】 |
| INITIAL_BACKLOG | 启用写回之前已存在的本地差异 | 【M8】：只列出，逐篇确认后才可进入计划 |
| POSSIBLE_DUPLICATE | 无映射、但内容 sha 相同或与已建页面相似度 ≥ 90% 的新文件 | 【S3】【safety#15】 |
| DELETE_UNSUPPORTED | 删除对象是数据库、模板、根对象、generated 笔记等（6.1） | 【coverage#16】 |
| UNSUPPORTED_RENAME | 改文件夹名、folder note 改名使其与文件夹不同名、阶段 2 的跨文件夹移动 | 【coverage#2】 |
| SCHEMA_OPTION_MISSING / PROPERTY_LOSSY | 行属性不满足 2.4.1 | 【safety#8】 |
| AUTOMATION_RISK | 行属性所在数据库未声明“无自动化” | 【api#11】 |

### 4.3 可写块（R 类）与只读块（X 类）

- R 类：paragraph、heading_1–4（非可折叠，或可折叠但无子块）、bulleted/numbered_list_item、to_do、quote、code、equation、divider、table_row（列数不变）、callout 的文字（图标、颜色不变）。
- X 类：column_list/column、synced_block、child_page、child_database、table_of_contents、breadcrumb、link_preview、embed、bookmark、image/file/pdf/video/audio、unsupported、template；含带色片段的块；含 link_mention、template_mention、自定义 emoji 提及、用户提及、指向有效范围外页面/数据库的提及的块（实测前）；含 `#blockid` 锚点链接或指向 Notion 页面的文本链接的块；带子块的可折叠标题；`owner_page_id ≠ notion_id` 的块。
- 判定以 JSON 往返为准：每次导出与每次 plan，对相关块做“块 JSON → Markdown → 块 JSON”往返检查，可写字段不一致即归 X，结果写入 `block_lines.klass`【S4】【safety#26】。用户新写入的 wikilink 一律生成文本链接（`text.link`），不生成 mention。

### 4.4 计划生成规则

1. 以整页（编号列表按整列表）为单位，用当前渲染器把“拟写入后的块 JSON”重新渲染，必须与 L 对应行一致；允许 Obsidian 自动重编号【M6】。
2. 只生成 UPDATE、APPEND（M 项）与 TRASH_BLOCK（**单列 B 项**）。块类型变化、缩进层级变化、同页移动：不生成“删除 + 新建”，报告“需在 Notion 中手工调整”【S2】。某个 M 项依赖同位置的块删除（例如用新段落替换旧段落）时，与对应 B 项绑定：B 项未确认，该 M 项整体不执行；`allow_block_trash=false` 时，含块删除的页面整页不生成可执行项，预览说明“未开启块删除，请在 Notion 中手工删除后再写回其余改动”【safety#4】【coverage#8】。
3. 请求切分：每个请求 ≤ 100 个子块、≤ 两层嵌套、≤ 1,000 个块元素、≤ 500 KB；`text.content` 与链接 URL ≤ 2,000 字符（超长按字符边界切成多个片段，每块 ≤ 100 个片段）；equation ≤ 1,000 字符（https://developers.notion.com/reference/request-limits ）。更深层级用上次返回的块 ID 继续 append【api#8】。
4. 熔断（**全库统计，不受 `--scope` 影响**）：单页拟操作块数 > `max_ops_per_page`；本轮出现 LOCAL_MODIFIED 的笔记数 > `max_modified_notes`；本轮新出现的库内删除 > `max_local_deletes`；任一触发即整轮只出预览、不生成可执行项，直到用户运行 `writeback.py ack-anomaly --run <id>` 并重新生成全库计划【S1】【safety#16】。
5. 计算本计划拟新建的块数（请求体递归计数），与块额度状态比较（第 9 节）。

---

## 5. 同步预览

### 5.1 生成方式

- `python3 -I tools/writeback.py plan [--scope 白名单|路径前缀]`：只读；不加载写回令牌；获取 `state/refresh.lock`（与 refresh 互斥）。
- 输出：`reports/writeback/<plan_id>/preview.md`、`plan.json`；库内只读副本 `Notion_Obsidian/写回预览/<plan_id>.md`（**库内文件不作为确认依据**【H6】）。
- `plan_sha256` 对**完整的 plan.json**计算（含每项 ops、影响字段与预览文字）【coverage#26】；计划默认 24 小时过期；生成新计划时，涉及同一 notion_id 或同一本地路径的旧计划自动标为 superseded。

### 5.1a 库内生成文件规范【coverage#19】

写回预览、写回运行记录、命名差异清单、待决删除清单：UTF-8、LF、单个换行结尾；frontmatter 固定为 `notion_kind: generated`、`generated_by: writeback.py`、`plan_id`，不写 notion_id；正文引用笔记一律写成行内代码路径加 Notion 链接，不写 `[[wikilink]]` 或嵌入；文件名只用中文前缀与 `[0-9A-Za-z_-]`；不登记到 published_files，只新增不覆盖；数量超过 100 时在运行记录提示。sortspec 为 `Notion_Obsidian` 及其 `写回预览`、`运行记录` 文件夹增加配置节（最新在上，写法以 docs/custom-sort.md 核实为准），校验器 C05 同步接受。【实施说明 2026-11-21：renderer 1.2.1 不生成这些配置节。导出须与 v1.2 逐字节一致（IMPL_CONTRACT §1.9、§5.7；B-exporter 偏差 6），所以本句暂缓执行，见 SPEC.md v1.3 §14 §9。以后若要增加，须同时提升 RENDERER_VERSION；校验器 C05 已能接受这类配置节。】

### 5.2 预览内容【U-II-3、U-III-2、U-IV-2】

| 区块 | 每项显示 |
|---|---|
| 新增内容（N） | 本地路径、拟建页面标题、Notion 父页面（标题 + 链接）、拟新建块数、是否新建行及属性 JSON |
| 修改内容（M） | 本地路径、Notion 页面、逐块差异（旧 → 新，块 ID 前 8 位）、操作类型（UPDATE/APPEND）、属性的原始 JSON 与拟写入 JSON |
| 待删除 / 移入回收站（高风险） | **D 项**：页面标题与链接；类型（page/row，行所属数据库）；实时读取的子页面与子数据库清单（递归，含数量）；正文块数、附件数；页面内 synced_block 原件及引用它的页面；内联数据库及行数；入站引用（page 提及、link_to_page、正文中的 Notion 链接，按来源页面列出）与入站关系（数据库名、属性名、行数）；是否锁定；Notion 最后编辑时间与 base 时间对比。**B 项**：页面、块 ID 前 8 位、块类型、原文（超过 500 字截断并注明）、是否有子块 |
| 标题或路径变化（高风险，T / MV） | Notion 当前标题、base 中标题、本地新名；是否满足 7.3 可写条件及原因；入站引用数；库内被 Obsidian 改写链接的笔记数；改名后下一轮库内将移动的文件数（folder note 为整个子树）；是否数据库行及其标题属性名；MV 项另列旧父页面、新父页面及两者共享与权限差异 |
| 冲突（C） | 两边各自的改动摘要、Notion 最后编辑时间与编辑者类型、建议处理办法 |
| 拒绝写回 / 只报告 | 4.2 的分类、原因、建议的手工处理办法 |
| 汇总 | 各类数量；拟发请求数与预计耗时；拟新建块数与块额度状态；熔断状态；**剩余风险**：“API 没有条件写，单个请求的预检与写入之间仍有约 1 秒窗口；执行期间请不要在 Notion 中编辑这些页面” |

关联数据来源【api#15、coverage#15】：REST API 没有反向链接端点，入站引用与关系从最近一次完整快照统计，预览注明“依据快照 <文件名>（<时间>）及计划时的实时读取；范围外与快照之后的引用不在此列”；快照早于 24 小时或之后出现过 REMOTE 变化时，D 项须先运行一次 refresh 再确认；统计不了的项显示“未知”，不得显示为 0。

请求数估算【api#17】：每个需读 R 的页面 = 1（GET page）+ 每个有子块的父块 ceil(子块数/100) + 超过 25 项的多值属性分页；apply ≈ 2 × plan 读取量（预检 + 回读）+ 写请求 + 定向更新。按快照实测，阶段 2（测试页 + 10 页）plan 约 22 次（中位）/ 66 次（P90）/ 451 次（P99），按 2.5 次/秒约 9 秒 / 26 秒 / 3 分钟；apply 另需全库导出与两次校验约 4 分钟。

### 5.3 新增页面规则【coverage#11】

1. 只有同时满足以下条件的 `.md` 才成为 N 项：位于扫描范围（4.0）；frontmatter 无 notion_id 且无映射；所在文件夹是某页面的 folder note 文件夹（`X/X.md` 带 notion_id、kind=page、在白名单内），或是 `whitelist_database_ids` 中数据库的文件夹（作为新行，属性按 2.4.1 校验）；文件名主干满足 `sanitize(主干) == 主干`（不含导出时的字符替换、截断或去重后缀）。不满足最后一条的列为“需改名后再新增”。团队协作区根目录、没有 folder note 的新文件夹、叶子笔记 `P.md` 旁新建的 `P/` 文件夹一律拒绝。
2. 标题 = 文件名主干，原样写入，不做反向全角替换（须满足 7.3 的字符条件）。
3. 引用本地附件（`![[…]]`）的新笔记在阶段 2 一律拒绝；阶段 2 拒绝正文超过 100 块的新页面。
4. POST 成功（或 503 返回 `committed_resource_id`）后立即写映射（本地路径、源文件 sha、新 page_id）；只在库内文件 sha 仍等于计划时 L sha 时，先备份再原子写入 frontmatter（notion_id 等）；不移动文件。下一轮导出路径与该文件相同，发布器执行 ADOPT（登记当前 sha），不得 ADD 第二个文件；路径仍不同时，按映射异常处理（该对象暂停，人工介入）。

---

## 6. 删除规则【U-III】

### 6.0 删除检测与处理（唯一规则）【coverage#1、safety#17、api#5】

每轮 refresh 在发布前运行只读库扫描（共享模块 `wb_scan.py`，不加载写回令牌）：已发布文件缺失、且库内找不到同一 notion_id 的文件，判为本地删除，按下列规则**唯一**处理：

| 情形 | 处理 |
|---|---|
| (a) `mode = off` | 沿用 SPEC v1.2 §13：下一轮按 Notion 版本恢复（ADD），列入运行记录 |
| (b) `mode ≥ plan`，本轮新出现的缺失数 ≤ `max_local_deletes`，非 PARTIAL_DELETE，R 相对 B 未变，且对象可删除（6.1） | 写入 `pending_deletes`（hold）；发布器按 notion_id 跳过其 ADD/REPLACE/MOVE；带子页面的页面只有在子树文件也全部缺失时才进入 hold，并标为高风险（不得成批确认） |
| (c) 本轮新出现的缺失数 > `max_local_deletes`（与第 14 节同一阈值、同一比较符） | 整轮暂停（`paused_for_review`），既不恢复也不进入 hold；由用户选择 `refresh.py --approve-run`（恢复）或 `writeback.py hold --run <id>`（转为 hold） |
| (d) PARTIAL_DELETE / CONFLICT_DEL_EDIT / DELETE_UNSUPPORTED | 下一轮按 Notion 版本恢复，记入运行记录，不触发第 14 节暂停 |

### 6.1 可删除对象范围【safety#10、coverage#16、api#19】

只有 `notion_kind ∈ {page, row}`、不是 31 个侧栏根对象、不是模板、不是 generated 笔记、不在 `Notion_Obsidian/**` 下的对象可以成为 D 项。以下对象永不生成 D 项，只报告（DELETE_UNSUPPORTED，提示在 Notion 中手工处理）：数据库与数据源（其说明笔记与 `.base`）；根对象；模板；含 synced_block 原件（被其他块引用）的页面；含 child_database 的页面。被关系属性引用的数据库行只能逐项确认，预览列出全部引用方。

### 6.2 hold 的生命周期

- 写入：只由 refresh 的发布前扫描写入（6.0 (b)）；`writeback.py hold --run <id>` 可把暂停轮次的缺失转为 hold。
- 取消：库内重新出现带同一 notion_id、且不是发布器写入的文件时，自动置为 restored；用户可运行 `writeback.py restore-deleted --item D-xxx`。
- 期限：超过 `pending_delete_ttl_days`（默认 14 天）未决定的，自动置为 expired，下一轮按 Notion 版本恢复。
- 每轮在运行记录与预览中列出全部 hold 项。hold 项不计入第 14 节的“库内大规模删除”。

### 6.3 确认【U-III-3】

- 只能在 `apply` 时用 `--delete D-001,…` 逐项确认，或用 `--delete-all-in-plan` 成批确认。成批范围严格限于本计划 plan.json 中的 D 项（计入 plan_sha256），单次不超过 `max_batch_deletes`（默认 5）；带子页面、被关系引用、已锁定的 D 项不能成批，只能逐项。
- 确认短语必须写明数量：例如“确认移入回收站 3 个页面（含子页面 0 个） <plan_id 后 4 位> <挑战码>”；数字与计划不符即退出（第 10 节）。
- 不属于本计划的 hold 项一律不处理，终端提示需重新生成计划。

### 6.4 执行前复核

Notion 侧：页面仍存在、未锁定、在有效范围内；`last_edited_time` 与全部块 JSON、子树清单与计划时一致（否则跳过并降为 CONFLICT_DEL_EDIT）。库内侧：对应文件及子树文件仍不存在；库内没有同一 notion_id 的文件；`pending_deletes.status` 仍为 hold。任一不符即跳过该项并报告。

### 6.5 删除前存档

把页面对象、全部块 JSON、子树清单、入站引用写入 `trash_archive`，并保留 `base_history` 最后版本【H2】。

### 6.6 完成判据与状态【U-III-4、coverage#22、api#16、api#19】

apply 结束时 D 项 `verify_status = pending_next_round`；下一轮 refresh 发布之后、下一次 plan 时同时满足以下条件才置为 verified：
- Notion：`GET /v1/pages/{id}` 返回 `in_trash: true`；有后代时逐个 GET，其自身 `in_trash` 为真或祖先链上任一对象 `in_trash` 为真（官方未说明级联行为，以阶段 2 实测为准【M10】）；
- 库内：没有该 notion_id 的任何文件（含子树）；
- 映射：`published_files` 中该对象全部文件状态为 `trashed_via_writeback`（发布器计算 TOMBSTONE 与第 14 节源端消失计数时排除此状态）；`objects_map.status = trashed_via_writeback`；`pending_deletes.status = confirmed`；
- 父页面笔记已重新发布且不再包含指向该页面的链接；`trash_archive` 已保存。
不满足则置为 needs_manual，并给出回退命令（`rollback --plan <id>` 恢复 `in_trash: false`）。

### 6.7 块级删除（B 项）

默认关闭（`allow_block_trash=false`）。只有阶段 2 在测试页实测“删块后可用 `in_trash: false` 恢复、位置与子块不变”通过后才可开启【H1】；多成员免费工作区（第 9 节 creation_state ≠ unlimited）强制关闭（重建会被额度挡住）。B 项只能 `--delete-blocks B-…` 确认，不被 `--items` 或 `--all-safe` 覆盖。

---

## 7. 标题、改名与移动【U-IV】

1. **默认不改 Notion 标题**：库内改文件名、移动文件，都不会自动修改 Notion。
2. **单独确认**：改名作为 T 项单独列出（字段见 5.2），只有 `apply --rename T-002` 显式确认才写入。
3. **可写条件**（同时满足）【S5】：新文件名不含导出时的字符替换（全角 `／：＊？＂＜＞｜＃＾［］`、`…`、`％`）、截断后缀 `… (<id8>)`、去重后缀 ` (<id8>)`；Notion 原标题是单段、无格式、无链接、无提及的纯文本；新标题非空、≤ 2,000 字符；Notion 当前标题（完整 rich_text JSON）等于 base 中的标题（否则 CONFLICT_RENAME）。不满足时只能“记录”，预览说明原因，建议在 Notion 中手工改名。
4. **未确认的改名**：Notion 标题保持不变；按 7.4 记录命名差异；Notion → Obsidian 自动同步继续更新该笔记内容并保留本地文件名；运行记录列出“命名差异”；不影响其他内容的同步【U-IV-4】。

### 7.4 命名差异的实现契约【coverage#2、api#5】

1. 检测：refresh 发布前的库扫描发现同一 notion_id 的已发布文件出现在**同一文件夹**内的新文件名下、旧路径消失、且新文件 sha 等于 published sha（或仅链接写法不同，7.5）时，写入 `name_overrides(..., status=open)`；内容也改了的，分别列为 T 项与 M 项。
2. 发布器：对 open 状态的 override，把 published_files 中该对象的路径由 old_rel 改为 local_rel（sha 不变，journal 记 ADOPT_RENAME），此后对该对象的 REPLACE 都写 local_rel，绝不在 old_rel 处 ADD。
3. 导出器：只读地读取 refresh 传入的 `overrides.json`，该对象使用 local_rel 参与同文件夹去重（冲突时该 override 失效并报告）；指向它的链接、sortspec 条目、目录索引、映射清单一律用 local_rel；aliases 仍按 SPEC §4 生成；override 名不写入 `objects_map.name`（清除后回到 sanitize(标题)）。
4. 校验器：接收 `expected_deviations.json`（14.0），清单内对象的 name_not_derived_from_title 降为 INFO 并单列“命名差异”。
5. override 期间 Notion 标题又变化：不改库内名称，在运行记录与下一次预览中把该对象标为“双方改名冲突”。
6. 改文件夹名、改 folder note 名使其与文件夹不同名、阶段 2 的跨文件夹移动：判 UNSUPPORTED_RENAME，不记 override；该对象及其子树从自动发布中排除（不 ADD、不 REPLACE），列为映射异常并标记人工介入，其余对象照常发布。

### 7.5 引用改写的处理【coverage#3、safety#20】

已发布文件 sha ≠ published sha 时，发布器先做链接等价比较：把库内版本与上次发布版本中的全部 wikilink 按 Obsidian 规则解析为 notion_id，换成占位符后比较。除链接写法外完全相同（LINK_REWRITE_ONLY）：先备份到 `backup/vault-before/<run_id>/`，再按暂存版本 REPLACE；不写冲突副本，不计入第 14 节冲突数。只改了本地 frontmatter 键（LOCAL_KEYS_ONLY）：REPLACE 时原样保留这些本地键。`Notion_Obsidian/` 下工具生成的文件（目录索引、映射清单、sortspec、README）归工具所有：被本地改动时先备份再覆盖。阶段 1 验收：改名一篇被引用 ≥ 50 次的笔记后，之后两轮 refresh 这些笔记的 Notion 更新正常进入库内，冲突副本为 0。

### 7.6 移动（MV 项）

阶段 2 不支持（判 UNSUPPORTED_RENAME，见 7.4 第 6 条）。阶段 3 可选开启 `allow_page_move`：单独生成 MV 项，只能 `--move MV-…` 确认；目标父页面须在有效范围内；预览显示旧父、新父及共享与权限差异；回退为“只有页面当前父级仍是新父级时才移回原父级”。

### 7.7 只读键

`notion_title` 等 `notion_*` 键被改：只记冲突，不写回。

---

## 8. 冲突处理

1. **不自动合并**：CONFLICT_BOTH 不写回 Notion，也不覆盖库内文件；Notion 新版本由自动同步写到 `Notion_Obsidian/conflicts/<run_id>/<原路径>`（同一 (notion_id, 暂存 sha) 只写一次），库内保持用户版本。
2. **解决方式**（命令行，带预览）：
   - `resolve --item C-001 --take notion`：下一轮按 Notion 版本覆盖库内文件（先备份到库外）。
   - `resolve --item C-001 --take obsidian`：**只生成一个新计划，不改动 base_history**；计划内各项都要通过 4.2–4.4 的全部检查，STALE_REVERT 按原 base 链判定；其中的块删除按 B 项确认；预览顶部标明“本项将覆盖 Notion 自 <时间> 以来的改动”并逐条列出。写入仍须经 apply【H6】【safety#22】。
3. **BLOCKED**：写后回读不一致（例如 Notion 规范化了文字）、APPEND 结果未知、同页出现计划外变化：该页进入 BLOCKED，不补偿、不重试；用户检查后 `unblock --page <id>`【S6】。
4. **Obsidian Sync 多设备合并**保持“自动合并”。恢复操作（Sync 版本历史、已删除文件、File Recovery、库外 tar）之前，先把 `mode` 设为 `plan`；恢复后运行 `writeback.py rebase --from-notion` 以 Notion 为准重建 base，再恢复原模式【S1 修正第 5 条】。

---

## 9. 免费套餐块上限：监控、告警与降级【U-I、api#1、coverage#7、coverage#20、safety#13】

### 9.1 官方事实（2026-11-17 核实）

- 免费套餐、成员（不含访客）≥ 2 的工作区：按**自注册以来工作区内创建的全部内容**计算终身 1,000 块；额度由全工作区共享，界面、导入、API、MCP 创建的块都计入；删除块或清空回收站不恢复额度（https://developers.notion.com/reference/workspace-block-limits 、https://www.notion.com/help/understanding-block-usage ）。
- 首次触及上限的写入仍会成功，并开启一次性的 3 天宽限期；宽限期结束后，**界面与 API 都无法新建内容**；API 读不到已用块数（只能在 设置 → 升级套餐 查看）。
- 额度耗尽时返回 HTTP 403、`code = restricted_resource` 且 `additional_data.block_limit = "block_creation"`；其他 restricted_resource 是普通权限错误。Update page / Update block 在会新增块时同样被拒。
- **对 Godcreate 的含义**：现有内容约 25.4 万块，远超 1,000。**一旦在免费套餐下增加第二名成员，整个工作区（包括用户在 Notion 界面中）将在 3 天宽限期后无法新建内容。**这不只影响写回，需要用户在增加成员前决定升级套餐或不加成员。

### 9.2 监控

- 成员数：读集成（勾选 User information without email addresses）调用 `GET /v1/users`，分页读到 `has_more=false`，只统计 `type = person`（官方说明不含访客），只保存数量，不保存也不输出姓名（https://developers.notion.com/reference/get-users ）；与 `config.workspace_members` 取较大值。
- 检查时机：每轮 refresh、每次 plan、每次 apply 开始；apply 时的结果与 plan 不同即使计划失效。调用失败或无此能力时成员数视为“未知”，按多成员处理。
- `creation_state`：成员 = 1 或付费套餐 → unlimited；成员 ≥ 2（或未知）且免费 → blocked_assumed；收到额度 403 → blocked_confirmed（跨轮持久，须用户手工解除）。
- `block_budget` 只作审计：按请求体递归计数（新页面计 1，加各层 children 元素总数），不按响应计数（append 响应只返回第一层新块）。

### 9.3 告警

成员数由 1 变为 ≥ 2（或变为未知）时，立即在 refresh 运行记录、命令行与预览中告警：“工作区已超出免费多成员额度：宽限期 3 天后界面与 API 均无法新建内容，请升级套餐或移除成员”；`refresh_status` 标 `warn_block_limit`。

### 9.4 降级

| creation_state | 允许 | 降级 |
|---|---|---|
| unlimited | 全部已开启的操作 | — |
| blocked_assumed / blocked_confirmed | UPDATE、行属性、改标题、页面移入回收站 | 计划器不生成任何 APPEND、N 项；某 M 项依赖 APPEND 时，该项整体降为只报告（不拆开执行其中的 UPDATE/TRASH）；块级删除强制关闭；按 `trash_archive` 或前像重建不可用；预览标“额度受限，需在 Notion 中手工添加或升级套餐” |
| apply 中收到额度 403 | — | 立即置 blocked_confirmed，中止该页剩余操作（不再发 TRASH），回读校验，该页 BLOCKED |

---

## 10. 确认机制【U-II-4、U-II-5、safety#3、coverage#10】

1. **用户本人在交互终端确认**：`apply`、`rollback`、`resolve --take obsidian` 的写入步骤要求 stdin 与 stdout 都是 TTY；检测到管道、here-doc、常见 CI 环境变量即拒绝；不提供 `--yes`、环境变量或文件形式的确认。
2. **每次都要短语**（不设块数门槛）。apply 启动时先打印本次将执行的清单，并现场生成随机挑战码（不出现在 preview.md 或 plan.json 中），再要求输入范围短语，例如：`确认 <plan_id 后 4 位> 修改3 新增1 删除2[D-001,D-004] 改名1[T-002] <挑战码>`；不符即退出。
3. **自动化代理不得确认**：定时器、AI 会话（包括本项目使用的 Claude 会话）、工作流脚本只能运行 plan、show、status、whitelist-check；不得运行 apply、rollback、resolve --take obsidian，不得代为输入短语，也不得用 Notion MCP 等其他途径把库内改动写入 Notion。
4. **选择项**：修改与新增 `--items M-…,N-…` 或 `--all-safe`（只含 M 与 N，永不含 B、D、T、MV、C）；页面删除 `--delete D-…` / `--delete-all-in-plan`；块删除 `--delete-blocks B-…`；改名 `--rename T-…`；移动 `--move MV-…`；冲突只经 `resolve`。
5. **绑定与时效**：确认绑定 `plan_id + plan_sha256`（完整 plan.json）；计划过期或被 superseded 即失效。apply 逐页预检，不一致的页跳过（标 CONFLICT_BOTH，记入 result.md），其余已确认项继续；不一致的页数超过所选项的 20% 时整轮中止。
6. **记录**：写入 `confirmations` 与 `approvals/writeback/<plan_id>.json`（OS 用户、TTY 设备名、时间、所选项、挑战码校验结果），不含令牌。

---

## 11. 执行流程（apply）

0. **互斥与状态**：非阻塞获取 `state/writeback.lock` 与 `state/refresh.lock`（与 refresh 共用），取不到即退出；`UPDATE plans SET status='applying' WHERE plan_id=? AND status='confirmed'` 影响行数不为 1 即退出。applied、partially_applied、aborted 的计划不能再次 apply；中断恢复只能 `apply --resume <plan_id>`，且只执行 journal 中 state=intent（未发出）的操作【safety#6、coverage#17、api#4】。
1. 加载写回令牌；检查凭据权限、计划有效性与 `plan_sha256`；运行 audit-scope（2.3）；检查成员数与 creation_state（第 9 节）。
2. 全局检查：`mode`；全局熔断（4.4）；共享限流（`tools/rate_gate.py`：进程内锁 + 文件锁 `fcntl.flock` 的跨进程令牌桶，状态存 `state/rate_gate.json`；snapshot 与写回共用；refresh 运行期间写回最多 1 次/秒；收到 429/529 或 `rate_limit_reason = public_api_space_request_rate_limit` 时按 Retry-After 设全局暂停）【H7、api#14】。
3. 逐页执行（每页一个逻辑事务），每页开始前与每个写请求前重新读取 `mode` 并检查急停文件 `state/WRITEBACK_STOP`，触发即停止并把计划标 aborted【safety#27】：
   1. **页级预检**（只读客户端）：页面未锁定、不在回收站、在有效范围；涉及的每个块当前规范化 JSON 与计划时一致；涉及属性的逐个属性分页取全并等于计划时的值；改名项的 Notion 当前标题完整 rich_text 等于计划中的旧标题；库内对应文件 sha 等于计划时 L sha；APPEND 的锚点当前下一个同级块与计划时相同、计划以来该父块下没有写回集成新建的块；N 项要求映射中没有该本地路径、父页面下没有写回集成在计划后建的同名页面。任一不符即跳过该项（标 CONFLICT_BOTH）【safety#7、safety#14】。
   2. **顺序固定**：UPDATE → APPEND → TRASH_BLOCK（→ 行属性、标题、页面回收站按项类型单独执行）。任一 APPEND 失败（403、结果未知、校验失败）立即停止本页，不再发 TRASH，页面 BLOCKED【safety#11、M9】。
   3. **请求前单块复核**：每个 UPDATE/TRASH 发出前单独 GET 该块，与计划时规范化 JSON 比较，不一致即停止本页【safety#19】。
   4. **journal**：发出前写 intent（含前像、请求、预期结果）并 fsync。
   5. **发送与重试**【S3、api#7】：读超时 ≥ 75 秒；只对 429、529 按 Retry-After 重试（`rate_limit_reason = public_api_request_blocked` 不重试）；任何一次暂停超过 30 秒后，在本页下一个写请求前重新执行 3.1。503 先读 `additional_data.retry_guidance`：建页返回 `committed_resource_id` 即记为新页面 ID；append 返回 `committed_child_ids` 时逐个 GET 核对，**不得重发**。非幂等请求（APPEND、POST pages）结果未知（超时、无响应）时一律不重发：append 用预检时记录的父块子块 ID 集合求差、建页用父页面 child_page 集合（行则按 created_time ≥ 请求开始时间且 created_by 为写回 bot 查询数据源）求差，结果唯一则记为已生效，否则该页 BLOCKED，由用户核对后 `writeback.py reconcile --journal <seq>` 处理。UPDATE、DELETE 属于幂等请求，核对后可重发。
   6. **新建页面**：状态 created → filling → complete；filling 期间任何失败即 PARTIAL_CREATE（BLOCKED），不自动续写；POST 后立即写映射，再按 5.3 第 4 条回写 frontmatter（先备份，sha 守护）【M12】。
   7. **回读校验**（只读客户端）：被触碰的块，回读规范化 JSON 等于预期 JSON（片段边界、全部 annotations 含 color、href、mention 类型与目标；切分后的片段边界以阶段 2 实测规则为准）；同页未触碰的块前后 JSON 相同；块的 `last_edited_by` 不是写回集成、或页面出现计划外变化，标 BLOCKED【S4】。journal 保存完整回读 JSON（`after_json`）。
4. **定向 Notion → Obsidian 更新与采纳**【api#3、api#13、safety#14】：
   - 基准快照取最近一次成功发布且无暂停的轮次的快照（不用 `latest_snapshot.txt`）；用 SQLite backup API 复制为 `snapshots/wb/snap_<ts>.db`（保留 7 天，不占 refresh 的 14 个配额）。
   - 重抓集合（只读客户端）= 被写页面 ∪ 被改名、新建或移入回收站页面的父页面 ∪ 新建行所在数据源（重新查询 `_row_ids`）∪ 被移动页面的新旧父页面；按 `IncrementalInventory._full` 的逻辑刷新。
   - 对整个快照导出（约 30 秒）、校验（约 100 秒），以 `publish_vault.py --adopt-writeback <plan_id>` 发布：计划内的笔记当前 sha = L sha 时先备份再用暂存版本替换，published_files 与 base_history 记 origin=WRITEBACK；当前 sha ≠ L sha（apply 后又被改）时不替换，Notion 版本写入 conflicts/，该项“待人工处理”。存在未放行的 paused_for_review 轮次时，以 `--only-ids` 只采纳计划内笔记，其余对象不发布。
   - 发布后复验（`verify_vault.py --ids` 与全库检查，见 W6）。
5. 生成 `reports/writeback/<plan_id>/result.md` 与库内 `Notion_Obsidian/运行记录/写回-<plan_id>.md`（按 5.1a）。

---

## 12. 写后一致性检查与完成判据

一次 apply 只有同时满足以下全部条件，才标记为“完成，可验收”；否则标记“部分完成，待人工处理”，并给出回退命令：

| 检查 | 方法 | 通过标准 |
|---|---|---|
| W1 写入结果 | journal 中每个已执行操作的回读 JSON | 规范化后等于预期 JSON |
| W2 未触碰内容 | 被触碰页面中未触碰块的前后 JSON | 完全相同 |
| W3 范围 | 所有写请求的目标（2.3 定义） | 均在声明的有效范围内，均出现在已确认项中；audit-scope 无多余对象 |
| W4 删除 | 已确认的 D 项 | 6.6 的条件（下一轮后置 verified） |
| W5 改名 | 已确认的 T 项 | Notion 标题 = 新标题；库内文件名一致；`name_overrides` 已清除 |
| W6 库内回读 | 采纳发布后 | 采纳后文件 sha = 暂存 sha；暂存版本与 L 的差异只出现在 `notion_last_edited_time` 和报告中列出的 Notion 规范化上；`verify_vault.py --ids <被写对象>` 的 C03/C04/C06/C08/C10 通过；在提供 expected_deviations 的前提下 C01/C02/C05 全库通过 |
| W7 无回环 | 下一次 `writeback.py plan` | 对本次已写回的笔记产生 0 个可执行项 |
| W8 映射 | objects_map、published_files、base_history、name_overrides、pending_deletes、plan_items | 与 Notion、库内状态一致，无悬空记录 |
| W9 额度 | block_budget、creation_state | 台账与实际新建块数一致；未出现额度 403；creation_state 与成员数一致 |
| W10 覆盖率 | 已确认项清单 vs journal | 每个已确认项都已执行且 W1 通过；有“跳过、未执行或结果未知”的项即整次标“部分完成”，result.md 逐项列出原因与下一步【safety#18】 |

---

## 13. 回退

`writeback.py rollback --plan <plan_id> [--items …]`（交互终端 + 挑战码短语，同第 10 节）。比较基准一律用 journal 中保存的完整回读 JSON（`after_json`），不能只用 sha【safety#12】：
- UPDATE：只有块当前规范化 JSON 仍等于 after_json 时，才按前像改回。
- APPEND：只有新块 JSON 等于 after_json、且 `has_children` 与写入时相同（子块逐个比较）时，才移入回收站。
- 新建页面：只有页面 `last_edited_by` 仍是写回集成、全部块 JSON 等于回读 JSON、且没有任何子页面或子数据库时，才移入回收站；否则只报告。
- 行属性：只有属性当前值（分页取全）等于写入后回读值时，才按前像改回。
- 改标题：只有标题仍等于新标题时才改回。
- 页面移入回收站：恢复 `in_trash: false`（回收站 30 天内有效；不在团队协作区内的顶层页面只能在界面恢复）。
- 移动：只有当前父级仍是新父级时才移回。
- 块级删除：仅在 H1 实测确认可恢复时提供。
回退同样生成预览、记录 journal、执行回读校验。超出 API 能力的恢复：超过 30 天时首选联系 Notion 客服（官方可恢复过去 30 天内的工作区快照，不保证逐页恢复）；其次按 `trash_archive` 降级重建（新 ID；入站提及与 link_to_page、关系、评论、页面历史、权限、内联数据库视图、synced_block 引用、link_preview 无法恢复；多成员免费工作区中不可用）【H2】。

---

## 14. Notion → Obsidian 自动同步的检查与暂停【U-V】

### 14.0 预期偏差与校验失败【coverage#6】

- refresh 每轮由只读库扫描生成 `expected_deviations.json`（LOCAL_NEW 路径、hold 中的 notion_id、name_overrides、Sync 冲突副本），传给校验器；清单内对象引起的 C01/C02/C03/C05/C06/C10 问题归为“已登记的本地差异”（INFO，单列计数），清单外的问题照常 FAIL。
- 发布后复验仍有 FAIL：result = `published_post_verify_failed`，把涉及的 notion_id 写入 `state/auto_sync_hold.json`；下一轮对这些对象及其子树不做任何写入、标记人工介入，其余对象照常发布；无法定位到具体对象时整轮暂停。

### 14.1 发布器分类修订【coverage#5】

已发布文件 sha ≠ published sha 时：暂存 sha = published sha（Notion 未变）记 LOCAL_ONLY——不写冲突副本、不计入冲突数，只在运行记录列为“待写回的本地修改”；只有暂存 sha ≠ published sha 才记 CONFLICT_BOTH（同一 (notion_id, 暂存 sha) 的冲突副本只写一次）；LINK_REWRITE_ONLY 与 LOCAL_KEYS_ONLY 按 7.5 处理。

### 14.2 两步发布

refresh 先运行 `publish_vault.py` 只生成计划，按 14.3、14.4 评估；未触发暂停时才以同一计划 `--apply`。

### 14.3 暂停条件

| 条件 | 粒度 | 阈值（默认，可配置） |
|---|---|---|
| 冲突（CONFLICT_BOTH） | 对象级；规模级 | 涉及对象排除；本轮 ≥ 20 个，或任一冲突涉及有子页面的页面 → 整轮 |
| 映射异常 | 对象级 | 同一 notion_id 对应多个文件；多个 notion_id 对应同一路径；文件缺失且不属于 6.0 (a)(b)(d)；UNSUPPORTED_RENAME |
| 内容缺失 / 重复 | 对象级（可定位时） | 校验 C02 / C10 问题（扣除预期偏差）；不可定位 → 整轮 |
| 覆盖风险 | 对象级；执行中即停 | 执行中出现 SKIP_REPLACE_RACE / SKIP_MOVE_RACE / SKIP_ADD_RACE（本轮剩余写入立即停止）；计划中 ADD 的目标路径已存在但不在 published_files；REPLACE/MOVE 的对象处于 hold、name_overrides 或 BLOCKED |
| 发布后复验失败 | 对象级（可定位时） | 见 14.0 |
| 源端大规模变化 | 整轮 | 本轮源端消失 ≥ 10（不含 trashed_via_writeback），或源端改动页面数 ≥ 全部页面的 10% |
| 库内大规模删除 | 整轮 | 本轮新出现的缺失文件数 > `max_local_deletes`（不含已在 pending_deletes 中的）【M11】 |
| 块额度状态变化 | 告警 | 第 9 节 |

### 14.4 处置与审计

- 对象级：该对象及其子树从本轮发布中排除，写入 `state/auto_sync_hold.json`，其余对象照常发布。
- 整轮：result = `paused_for_review`，本轮不向库写入笔记（只写库内提示 `Notion_Obsidian/运行记录/<run_id>-已暂停.md`，列出原因、涉及对象的行内代码路径与处理命令）。
- 证据：被暂停轮次的暂存库、快照与 reports 不参与 prune，直到被批准或用户执行 `refresh.py --discard-run <run_id>`。
- 放行：`refresh.py --approve-run <run_id>` 只接受最近一次被暂停的轮次，并重新运行发布器计划（不复用旧计划）；新计划与暂停时不同即拒绝，要求重新评审。

---

## 15. 配置（`config/writeback.json` 默认值）

```json
{
  "mode": "off",
  "workspace_members": 1,
  "notion_plan": "free",
  "whitelist_page_ids": [],
  "whitelist_scope": {},
  "whitelist_database_ids": [],
  "automation_free_database_ids": [],
  "test_page_id": null,
  "plan_ttl_hours": 24,
  "max_ops_per_page": 50,
  "max_modified_notes": 20,
  "max_local_deletes": 5,
  "max_batch_deletes": 5,
  "pending_delete_ttl_days": 14,
  "allow_block_trash": false,
  "allow_page_move": false,
  "sync_settle_minutes": 15,
  "sync_burst_files": 20,
  "rate_limit_total_rps": 2.5,
  "rate_limit_writeback_during_refresh_rps": 1.0,
  "conflict_pause_threshold": 20,
  "source_trash_pause_threshold": 10,
  "source_change_pause_ratio": 0.10
}
```

`mode`：`off`（默认；写回子命令只打印说明，删除检测按 6.0 (a)）→ `plan`（只允许生成预览）→ `whitelist`（阶段 2）→ `full`（阶段 3）。模式只能由用户手工修改；工具只会自动**降低**模式（audit-scope 不符时降为 plan），不会自动升级。

---

## 16. 命令一览

| 命令 | 写 Notion | 说明 |
|---|---|---|
| `writeback.py plan [--scope …]` | 否 | 生成预览与计划 |
| `writeback.py show --plan <id>` | 否 | 终端显示预览 |
| `writeback.py status` | 否 | 待决删除、命名差异、BLOCKED、creation_state、模式 |
| `writeback.py whitelist-check` | 否 | 白名单页面的后代范围 |
| `writeback.py audit-scope` | 否（只读 search） | 写回集成实际可达范围核验 |
| `writeback.py apply --plan <id> --sha <12位> [选择项]` | 是（仅已确认项） | 交互终端 + 挑战码短语 |
| `writeback.py apply --resume <id>` | 是 | 只执行 journal 中未发出的操作 |
| `writeback.py rollback --plan <id> [--items …]` | 是（回退） | 第 13 节 |
| `writeback.py reconcile --journal <seq>` | 否 | 结果未知的操作人工核对 |
| `writeback.py resolve --item C-xxx --take notion\|obsidian` | 否（take obsidian 只生成新计划） | 第 8 节 |
| `writeback.py restore-deleted --item D-xxx` | 否 | 放弃库内删除，下一轮按 Notion 恢复 |
| `writeback.py hold --run <id>` | 否 | 把暂停轮次的缺失转为 hold |
| `writeback.py ack-anomaly --run <id>` | 否 | 确认熔断异常后才允许重新生成可执行计划 |
| `writeback.py unblock --page <id>` | 否 | 解除 BLOCKED |
| `writeback.py rebase --from-notion` | 否 | 恢复操作后以 Notion 为准重建 base |
| `refresh.py --approve-run <id>` / `--discard-run <id>` | 否（写库） | 第 14 节 |

---

## 17. 分阶段上线与验收标准

| 阶段 | 内容 | 进入下一阶段的验收标准 |
|---|---|---|
| 0 只读准备 | 只读客户端与读集成降权；导出器记录 block_lines（库文件逐字节不变）；发布器写 base_history、LOCAL_ONLY、LINK_REWRITE_ONLY、ADOPT、hold 跳过、命名差异、两步发布与第 14 节暂停；refresh 发布前只读库扫描、expected_deviations、成员数监控、trash_archive；校验器 --expected-deviations 与 --ids；rate_gate；写回客户端（无自动重试、503 指引、journal） | ① 用同一快照以新旧导出器导出，5,566 篇笔记 sha256 逐一相同；② 全库 JSON 往返：R 类块 100% 一致；③ 含子页面容器的删除拒绝测试；④ 模拟超时与 503 不产生重复；⑤ 本地改名一篇、删除一篇无子页面笔记后，连续两轮 refresh 正式库无重复 notion_id，C01–C10 全部 PASS；⑥ 第 14 节每类暂停条件各造一个故意失败样本：相关对象被暂停、其余照常发布、审计文件齐全、库内出现人工介入提示；⑦ `ReadOnlyClient` 拒绝所有写方法；⑧ 设 workspace_members=2：预览中全部 N 项与 APPEND 被降级，告警同时出现在预览、运行记录、命令行；模拟额度 403 与普通 403 得到两种判定 |
| 1 只读演练（≥ 7 天，`mode = plan`） | 每天生成预览，不写入 | 对未改动笔记误报 0；离线设备回放、从版本历史恢复旧文件、只删 folder note 三个场景产生 0 个可执行项；改名一篇被大量引用的笔记：产生 1 个 T 项（若满足 7.3），被改写引用的笔记产生 0 个 M 项，此后两轮 refresh 这些笔记正常更新、冲突副本为 0；模拟 Notion 规范化差异时页面进入 BLOCKED 且不循环；非 TTY 环境调用 apply 被拒绝 |
| 2 白名单写回（≥ 7 天，`mode = whitelist`） | 新建写回集成，只共享测试页与 ≤ 10 个叶子白名单页面；只开 UPDATE、APPEND、行属性（无自动化数据库）、确认后的改名、确认后的页面移入回收站 | 测试页实测：删块后 `in_trash:false` 恢复【H1】、父页面进回收站后子页面状态【M10】、各类提及的可写性与访问权限【M4】、静默通知、长文本切分后的片段边界；audit-scope 无多余对象；每次 apply 的 W1–W10 全部通过；一次真实删除在下一轮 refresh 后置为 verified；回退演练通过 |
| 3 扩大范围（`mode = full`） | 用户逐个把页面加入集成共享与白名单 | 每次扩大后连续 3 次 apply 的 W1–W10 全部通过；无 BLOCKED 残留 |

### 17.1 工作量估算（人日）【api#22】

阶段 0 约 8–10（导出器 block_lines / json_norm / base_history 与逐字节回归 1.5；R 类反解器、片段复用与全库 JSON 往返测试 3–4；写回客户端与跨进程限流 1–1.5；hold 与命名差异的导出器/发布器/校验器改造、采纳模式、第 14 节暂停与 --approve-run 2–2.5；S2、S3 测试 0.5）；阶段 1 约 4–5（另需 ≥ 7 个日历日）；阶段 2 约 5–6（另需 ≥ 7 个日历日）；阶段 3 约 0.5–1。**合计约 18–22 人日，另加至少 14 个日历日演练。**

---

## 18. 用户已确认与仍需确认的事项

已确认（2026-11-17）：
- Notion → Obsidian 全自动；Obsidian → Notion 只能手动、预览、用户确认后写入。
- 当前 1 名成员，将来可能增加。
- 删除：不自动删除；预览中标出；逐项或成批确认后才移入回收站；完成后做三方一致性检查。
- 改名：默认不改 Notion 标题；单独确认；未确认时保留 Notion 标题并记录差异，不影响其他内容同步。
- Notion → Obsidian 同步后检查；异常时暂停相关自动写入并留审计轨迹。

开始实施前需要用户在 Notion 界面完成或决定：
1. ~~把现有读集成降为只读~~ 【已执行】2026-11-17 18:30：连接 `Godcreate` 只保留“读取内容”，用户信息“不包括电子邮件地址”；API 核验写入返回 403、读取正常、成员数 1。
2. ~~新建写回集成~~ 【已执行】2026-11-17 18:29：连接 `Godcreate-Writeback`（开发者门户 `https://app.notion.com/developers/connections`），仅读取/更新/插入内容，无页面访问；令牌已存入 `~/.config/notion-obsidian/writeback.env`（600）并经 `/v1/users/me` 核验。
3. 指定 1 个测试页与不超过 10 个白名单页面（建议选不含子页面和内联数据库的“叶子页面”），只把这些页面共享给写回集成。
4. **在免费套餐下增加第二名成员之前，先决定是否升级套餐**（否则整个工作区 3 天后无法新建内容，见 9.1）。
5. 确认第 15 节默认阈值。

---

## 19. 对现有契约与代码的修改（与本规格同时生效）【coverage#24】

SPEC.md（升为 v1.3）：
- §2：`Notion_Obsidian/` 内容补充 `写回预览/`、`运行记录/`、`conflicts/`。
- §3：命名差异（name_overrides）优先于 sanitize 名称，且不写入 `objects_map.name`。
- §4：override 对象照常生成 aliases。
- §9：~~增加 `Notion_Obsidian` 的配置节；~~使用 override 名称（配置节暂缓，见 5.1a 实施说明）。
- §10：增加 `writeback.db`；base_history 由发布器写入；published_files 增加状态 `trashed_via_writeback`、`renamed`。
- §11：增加 LOCAL_ONLY、LINK_REWRITE_ONLY、ADOPT、ADOPT_RENAME、hold 跳过、工具生成文件可覆盖、两步发布与暂停、`--approve-run`、`--adopt-writeback`。
- §13：“本地删除”改为本规格 6.0（按 mode 区分）；“校验”增加 expected_deviations 与对象级暂停。

代码模块：
- `export_obsidian.py`：block_lines（库文件逐字节不变）、renderer_version、override 的路径/链接/sortspec/目录索引。
- `sortspec.py`：~~`Notion_Obsidian` 配置节、~~override 名称（经导出器 `folder_children()` 传入，sortspec.py 本身未改；配置节暂缓，见 5.1a 实施说明）。
- `publish_vault.py`：上述 §11 各项、写 base_history、tombstone 排除 `trashed_via_writeback`、`--only-ids`、计划/执行分离。
- `refresh.py`：发布前只读库扫描（`wb_scan.py`）、两步发布与第 14 节评估、`paused_for_review`、`--approve-run`/`--discard-run`、prune 豁免、成员数与额度告警、`auto_sync_hold.json`、expected_deviations。
- `verify_vault.py`：`--expected-deviations`、`--ids`、`Notion_Obsidian` 生成文件规则、C05 新配置节（校验器接受；导出器暂不生成，见 5.1a 实施说明）。
- `snapshot.py`：定向抓取指定页面；页面首次消失时写 `trash_archive`；使用 `rate_gate`。
- 新增：`readonly_client.py`（ReadOnlyClient）、`rate_gate.py`、`wb_scan.py`（refresh 可导入，不加载令牌）、`writeback.py`、`writeback_client.py`（非幂等不重试、503 指引、journal）。
- `rollback.py`：识别 ADOPT、ADOPT_RENAME 等新的 journal 操作。
- 不修改 `/data120/Obsidian/notion-migration`（通过子类化注入）。
