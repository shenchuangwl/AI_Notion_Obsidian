> **设计 / 规格文档**｜Notion → Obsidian 导出契约｜版本 **v1.3**｜仓库副本，正文未改动（来源：开发沙箱 `notion-obsidian-dev/docs/SPEC.md`）。本文记录设计决策及其演进历史，部分表述反映编写当时的状态；实际行为以 [`notion-obsidian/tools/`](../../notion-obsidian/tools/) 代码和[操作手册](../usage/operations-manual.md)为准。返回[文档索引](../README.md)。

# Notion → Obsidian 导出契约（SPEC，v1.3：v1 + 第 3 节规范父级、第 5.1 节渲染细则、第 12 节敏感串、第 13 节模板/规范 .base/编号列表/本地删除/README/严格校验、第 14 节双向同步配套修订）

导出器、独立校验器和发布器都以本文为准。本文只定义“应该产出什么”，不代表已经执行。

## 1. 路径与角色

| 名称 | 绝对路径 | 说明 |
|---|---|---|
| WORK（任务根，库外） | `/data120/Obsidian/notion-obsidian` | 脚本、快照库、附件缓存、状态库、日志、备份。不进入 Sync |
| 快照库 | `WORK/snapshots/snap_<TS>.db` | nmig rest_inventory 格式（表 meta/work/objects/blocks/comments/files/events），只读打开 |
| 附件缓存 | `WORK/blobs/<sha256>` | 内容寻址；`files` 表的 `sha256` 指向这里 |
| 侧栏证据 | `WORK/evidence/sidebar_<TS>.json` | 5 个团队协作区顺序与 31 个根对象顺序（独立于 API 的源顺序证据） |
| 暂存库 | `WORK/staging/<run_id>/vault/` | 与正式库同构的完整目录树 |
| 正式库根（VAULT） | `/data120/Obsidian/Godcreate` | 用户 2026-11-17 02:31 在客户端新建并连接远程库 “Godcreate” 的库（原指定的 `/data120/Obsdian/Godcreate` 已由用户移入回收站） |

## 2. 库内顶层结构（顺序即展示顺序）

```
VAULT/
  General/
  Quantitative Investing/
  x_to_notion/
  Godcreate Business/
  Godcreate AI工作空间/
  Notion_Obsidian/          任务目录：README、映射清单、目录索引、运行日志摘要、排序配置（sortspec）
  .obsidian/                仅写入：plugins/custom-sort/*、community-plugins.json（发布时按白名单）
```

团队协作区文件夹名必须与上表逐字相同（注意 `Godcreate AI工作空间` 只有一个空格；Notion API 中该名称可能带两个空格，以侧栏为准）。

## 3. 对象 → 路径

对象：页面（page）、数据库（database）、数据库行（row，本质是父为数据源的页面）。规范身份 = Notion ID（带连字符小写）。

- 子对象：页面正文中（含嵌套在 toggle、column、synced_block 等内部的）`child_page` 与 `child_database` 块所指对象，按正文出现顺序；数据库的子对象是其各数据源查询结果中的行，按查询返回顺序。
- **规范父级以对象自身的 API `parent` 字段为准**（v1.1 修订，2026-11-17）：`child_page`/`child_database` 块所指对象，只有当该对象的 `parent` 是 `page_id == 当前页面`（或 `block_id`，即嵌在当前页面的块中）时才算子对象；行只有当 `parent` 是 `data_source_id ∈ 该数据库的数据源`（或 `database_id == 该数据库`）时才算子对象。不满足的（例如导入日志页里指向 4 个根页面的残留 child_page 块）是“引用”：不改变层级，渲染为指向对象实际位置的链接，记入报告。31 个根对象在展开任何子树之前先登记，永远是根。
- **有子对象的页面/行** → 文件夹 `<name>/`，其中 `<name>.md` 是该页面本身（folder note），子对象放在同一文件夹内。
- **无子对象的页面/行** → `<name>.md`。
- **数据库** → 文件夹 `<name>/`，内含 `<name>.md`（数据库说明笔记，kind=database）、`<name>.base`（Bases 视图），以及各行。
- 根对象放在其团队协作区文件夹下，按侧栏顺序。
- 已在回收站（`in_trash`/`archived` 为真）的对象不导出，记入报告。

`name` 生成规则（`sanitize`）：
1. 取标题纯文本并做 NFC 规范化；空标题 → `Untitled`。先把 `...` 换成 `…`、`%` 换成 `％`（custom-sort 中 `...` 是通配符、行首 `%` 无法列出，见 docs/custom-sort.md）。
2. 替换：`\ / : * ? " < > |` → `＼ ／ ： ＊ ？ ＂ ＜ ＞ ｜`；`#`→`＃`，`^`→`＾`，`[`→`［`，`]`→`］`；去除控制字符；换行/制表符 → 空格；连续空白折叠为一个空格；去掉首尾空格；去掉末尾的 `.`；开头的 `.` → `．`。
3. 名称（不含扩展名）UTF-8 字节数上限 120；超出时按字符边界截断并追加 `…`，再追加 ` (<id前8位>)`。
4. 同一文件夹内去重：比较键 = NFC + casefold；按顺序先到者保留原名，后到者追加 ` (<id前8位>)`。文件夹名与笔记名分开计（`X/` 与 `X.md` 不冲突），但同一文件夹里不同对象不得占用同一个比较键（例如两个都叫 `A` 的子页面，一个是文件夹一个是笔记，也算冲突）。
5. 状态库记住已分配的名称；标题和父级不变时，后续运行沿用旧名称（稳定）。

## 4. 笔记格式

UTF-8、LF 换行、以单个换行结尾。YAML frontmatter（用 YAML 序列化器输出，字符串一律加引号或块样式）：

| 字段 | 说明 |
|---|---|
| `notion_id` | 规范 ID |
| `notion_kind` | `page` / `row` / `database` / `generated` |
| `notion_url` | `https://app.notion.com/p/<32位无连字符ID>` |
| `notion_title` | 原始标题（未清洗） |
| `notion_parent_id` | 父对象 ID；根对象为 null |
| `notion_teamspace` | 所属团队协作区（第 2 节的名称） |
| `notion_order` | 在父对象子序列中的 1 起序号（根对象 = 侧栏序号） |
| `notion_created_time` / `notion_last_edited_time` | API 原值（UTC ISO） |
| `notion_icon` | emoji 字符，或 `attachment:<库内路径>`，或外链 URL；无则省略 |
| `notion_cover` | 同上 |
| `aliases` | 仅当清洗后名称 ≠ 原标题时，列出原标题 |
| 行属性 | 数据库行的属性按原属性名作为键（与 `notion_*`、`aliases`、`tags`、`cssclasses` 冲突时加前缀 `prop_`） |

正文不另加一级标题（Obsidian 用文件名作为标题）。

## 5. 块渲染（顺序 = API children 顺序）

- paragraph → 段落；空段落 → 空行（另在 `notion_blank_blocks` 计数中保留数量）。
- heading_1/2/3（及 heading_4 若存在）→ `#`/`##`/`###`/`####`；可折叠标题（is_toggleable）→ 同级标题，子块照常渲染在其下。
- bulleted_list_item → `- `；numbered_list_item → `1. `（Obsidian 自动编号）；to_do → `- [ ] ` / `- [x] `；子块缩进 4 个空格（列表）。
- toggle → 可折叠 callout `> [!toggle]- <标题>`，子块逐行加 `> `。
- quote → `> `；callout → `> [!note] <icon>`（首行为 callout 文本）。
- code → 围栏代码块，语言取 `code.language`（`plain text` → 空）；围栏长度 = 内容中最长连续反引号 + 1（至少 3）。
- equation → `$$\n...\n$$`；行内 equation → `$...$`。
- divider → `---`（前后空行）。
- table → Markdown 表格（首行作为表头；`has_column_header=false` 时也用首行作表头并在报告中计数）；单元格内 `|` → `\|`，换行 → `<br>`。
- image/video/audio/pdf/file：Notion 托管文件 → 库内附件嵌入 `![[<附件库内路径>]]`（file 类用 `[[<路径>|<名称>]]`）；外部 URL → `![](<url>)` 或 `[名称](<url>)`；有 caption 时下一行写 caption 文本。
- bookmark / embed / link_preview → `[<caption 或 URL>](<url>)`。
- child_page → 在该位置写 `[[<目标库内路径不含扩展名>|<标题>]]`。
- child_database → 在该位置写 `![[<数据库文件夹>/<name>.base]]`，下一行写 `[[<数据库说明笔记>|<标题>]]`。
- link_to_page → `[[<路径>|<标题>]]`（目标不在范围内时用 Notion URL）。
- synced_block → 直接渲染其子块（原始块与引用块都如此）。
- column_list / column → 依次渲染每一列的内容，列之间插入 `%% column %%` 注释行。
- table_of_contents / breadcrumb → 注释行 `%% notion:<type> %%`。
- template / unsupported / 其他未知类型 → 注释行 `%% notion:unsupported <type> <block_id> %%`，计入报告，不静默丢弃。

富文本：bold `**`、italic `*`、strikethrough `~~`、code `` ` ``、underline `<u>…</u>`、颜色忽略（报告计数）；对普通文本中会被 Obsidian 解释为格式的字符做最小转义（`\ * _ ` [ ] < $ ~ = # |` 中会触发格式的情况）。mention：page/database → wikilink（不在范围内时 Notion URL）；user → `@<姓名>`；date → 日期文本；link_mention/link_preview → 链接。

### 5.1 渲染细则修订（v1.1，2026-11-17，依据渲染保真度审查）

- callout：`> [!note] <emoji>` 作为标题行，正文另起 `> ` 行（Notion 中是“图标 + 正文”，避免正文被渲染成标题栏）；非 emoji 图标、背景色不保留（计数）。
- 加粗/斜体/删除线：内容首尾是标点时改用 `<strong>`/`<em>`/`<del>`（CommonMark 在中文旁的 `**（x）**` 不闭合）；带格式的行内代码一律用 HTML 标签包裹。
- 行内代码跨行：按行拆成多个代码片段；整个段落只有一个含换行的代码片段时输出为围栏代码块。
- 表格单元格：wikilink 写成 `[[路径\|别名]]`，行内公式与 URL 中的 `|` 转义。
- 空行：块之间至多一个空行，在拼接时去重；不再对全文做空行折叠，代码块内容逐字保留。空段落数量写入 frontmatter `notion_blank_blocks`。
- 列表：正文会与列表符号组成分隔线（如 `- --`）时转义；`-`/`--` 单独成行时转义（防止 setext 标题）；行首 `|` 转义；无序列表与待办列表相邻时以 `<!-- -->` 分隔。
- 同步块：引用块自身无子块时渲染源块（`synced_from.block_id`）的子块；无法读取时输出可见的 `> [!warning]`。
- 外部视频：`![](url)`（Obsidian 可嵌入 YouTube/Vimeo）。附件下载失败：可见的 `> [!warning] 附件未能下载`。
- 页面提及：范围内显示目标当前标题；范围外先用快照中的标题，否则 plain_text。日期提及：`YYYY-MM-DD HH:MM (+偏移)`，区间用 `→`。
- 以 `/` 开头的链接：只有含 32 位十六进制 ID 时才视为 Notion 内链。纯文本中的 URL 原样输出（不转义）。
- 无扩展名附件：按文件头/Content-Type 补扩展名。`.md` 附件保持 `.md`（会被 Obsidian 当作笔记索引，记为已知差异）。

## 6. 链接

- 页面/数据库 ID 在导出范围内 → `[[<目标库内相对路径不含 .md>|<显示文字>]]`，显示文字中的 `|`、`]` 换成全角。指向数据库时目标为其说明笔记。
- `https://(www.)notion.so/...<32hex>` 或 `https://app.notion.com/...<32hex>` 形式的 URL，若 ID 在范围内同样改写；`#<blockid>` 锚点丢弃（报告计数）。
- 范围外目标保留原 URL，并记入 `link_report`。

## 7. 附件

- 只处理 Notion 托管文件（`type == "file"`）：字节来自 `blobs/<sha256>`。
- 库内位置：`<团队协作区>/_attachments/<清洗后的原文件名主干>__<sha256前8位>.<扩展名>`；同一团队协作区内同一 sha256 只存一份。
- 下载失败（`files.status != ok`）→ 保留原链接说明并记入报告，不伪造文件。

## 8. 数据库

- `<name>.md`（kind=database）：数据库描述、属性定义表（属性名、类型、选项）、按查询顺序的行链接列表、`![[<name>.base]]`。
- `<name>.base`（YAML）：`filters` 限定 `file.inFolder("<数据库文件夹库内路径>")` 且 `notion_kind == "row"` 且 `notion_parent_id == "<数据库ID>"`；一个 `table` 视图按 `notion_order` 升序，`order` 列出 `file.name` 与各属性；Notion 视图信息（若取得）写入数据库说明笔记，作为已知差异。
- 公式、汇总、关联：写入取值快照（不是动态计算）。

## 9. 排序呈现

custom-sort 3.2.0 语法（已核实，docs/custom-sort.md）：配置只能放在名为 `sortspec.md` 的笔记中，frontmatter 键 `sorting-spec`（字面块 `|`）；每个文件夹一节 `target-folder: <从库根起的路径>`（库根写 `/`）；文件夹写 `/folders <名称>`，文件写 `/:files. <名称>.<扩展名>`（含扩展名精确匹配，folder note 与同名文件夹不会互相命中）；`%` 表示未列出项的位置；团队协作区文件夹在 `%` 之后写 `/folders _attachments`，使附件目录排最后。每节顺序：该文件夹的 folder note（`.md`），数据库再加 `.base`，然后按 Notion 顺序列出子对象。


- 正文块顺序：按 API children 顺序渲染（第 5 节）。
- 文件列表：用社区插件 custom-sort（用户已批准安装），配置写在 `Notion_Obsidian/sortspec.md` 的 `sorting-spec` frontmatter，对每个文件夹按 Notion 顺序列出子项；`_attachments` 排在最后。插件设置 `.obsidian/plugins/custom-sort/data.json` 中 `suspended: false`（仅首次发布写入，插件会自行改写该文件）。
- 另生成 `Notion_Obsidian/Godcreate 目录索引.md`（kind=generated）：按顺序的完整树形链接。

## 10. 状态与幂等

- 状态库 `WORK/state/export.db`：`objects_map`（notion_id、kind、rel_path、name、parent_id、teamspace、ord、title、rendered_sha256、published_sha256、published_run、status）。
- 同一快照 + 同一状态库重复导出，产出逐字节相同（不写入运行时间等易变内容）。

## 11. 发布规则（暂存 → 正式库）

- 只写入暂存清单中的文件和第 2 节列出的 `.obsidian` 白名单文件；不删除任何文件。
- 新文件：目标不存在才写。已存在：当前 sha256 = 上次发布的 sha256 才替换；否则视为本地编辑冲突，正式库文件保持不动，Notion 版本写入 `Notion_Obsidian/conflicts/<run_id>/<原相对路径>`。
- 替换前把旧文件复制到 `WORK/backup/vault-before/<run_id>/`；每个文件写 `tmp → fsync → os.replace`，记录 before/after sha256 到 `WORK/reports/<run_id>/publish_journal.jsonl`。
- 源端删除：不删除库内文件，只记入 `tombstones` 报告。

## 12. 敏感串（v1.1 修订）

源页面正文中出现的凭据样式字符串（`ntn_`/`secret_` 长串、私钥块）属于用户自己的 Notion 内容，按原样迁移，不擅自删改；校验器 C11 只报告路径与行号（不输出值），作为警告而非阻断项，由用户决定是否轮换或删改源页面。

## 13. v1.2 修订（2026-11-17 08:xx，最终验收后）

- **数据库页面模板**：每个数据源的模板（`GET /v1/data_sources/{id}/templates`，模板本身是父为该数据源的页面）一并抓取；快照中数据源对象带 `_template_ids`（按 API 返回顺序），模板页面以 kind='page' 存入 objects/blocks。导出到 `<数据库文件夹>/_templates/`：有子对象的模板同样用 folder note 结构；frontmatter `notion_kind: template`、`notion_parent_id` = 数据库 ID、`notion_order` = 模板序号（1 起）。模板不计入数据库行，不出现在 `.base` 视图（filter 只取 `notion_kind == "row"`）。数据库说明笔记另列“模板”小节。数据库文件夹名称空间保留 `_templates`；sortspec 中数据库文件夹节在 `%` 之后写 `/folders _templates`，并为 `_templates` 文件夹单独写一节按模板顺序列出。
- **`.base` 规范格式**：按 Obsidian 1.15.1 回写格式输出——序列在映射下缩进 2 个空格；`order` 中除 `file.name` 外写裸属性名（如 `Summary`、`notion_order`）；`sort[].property` 写裸名 `notion_order`；`properties` 键保持 `file.name`、`note.<名>`。这样 Obsidian 打开 Bases 视图不会再重写文件。发布器对 `.base` 做语义比较：解析 YAML 后把 `note.` 前缀与缩进差异视为相同，相同即视为未变，不产生冲突。
- **编号列表**：输出显式序号（`1.`、`2.`、`3.`…，遇到非编号列表块重新从 1 开始；`list_start_index` 存在时从该值开始）；列表子内容缩进 = max(4, 标记宽度)（待办 `- [ ] ` 为 6）。
- **本地删除（单向镜像阶段的规则）**：迁移文件在库内被删除而 Notion 中仍存在时，下一轮发布按 Notion 版本重新写回（ADD），并在运行记录中列出。用户 2026-11-17 确认第二台设备的删除不是有意的，按此恢复。双向同步规则另见 `docs/BIDIRECTIONAL_DESIGN.md`（设计中，未启用）。
- **库内说明**：导出器生成 `Notion_Obsidian/README.md`（kind=generated），说明库结构、排序、同步方式与恢复办法。
- **校验**：C10 对任一含文本块未命中即判 FAIL（不再使用 99% 阈值）；refresh 的阻断检查扩大为 C01–C10（C11 仍为警告）；每轮发布后对正式库再跑一次校验并记录结果。

## 14. v1.3 修订（2026-11-18，双向同步实施；对应 WRITEBACK_SPEC v1.1 第 19 节）

本节修订优先于前文同名条款；未提及的条款不变。操作方法见项目根目录唯一操作文档《notion_obsidian+obsidian_notion 同步相通执行文档.md》。

- **§2 任务目录**：`Notion_Obsidian/` 另含 `运行记录/`（每轮发布记录，仅在本轮有变化或“待处理事项”集合变化时生成）、`写回预览/`（写回计划的只读副本，仅供阅读，不作为审批输入）、`conflicts/`（冲突副本）。
- **§3 命名差异**：用户在库内同一文件夹内改名的笔记，经扫描登记为 name override 后，路径、指向它的链接、sortspec 条目、目录索引、映射清单一律使用本地名称（`local_rel`）；override 名称不写入 `objects_map.name`（清除后回到 sanitize(标题)）。跨文件夹移动、folder note、数据库、模板的改名不支持，按“暂停该对象”处理。
- **§4 aliases**：按清洗后的标题名称（sanitize 结果，含截断/去重后缀）判断，与 override 文件名无关（WRITEBACK_SPEC 7.4.3、D-22）；已登记 override 的笔记，如果清洗后的名称与标题相同（导出器本来就不写 aliases），校验器不因缺 alias 判失败（实施决定 L-11）。如果标题需要清洗（如 `A/B` → `A／B`），缺 alias 仍判 FAIL。用户自己在笔记里写的 `aliases` 视为本地键，保留。
- **§9 排序**：override 对象使用本地名称。sortspec 的配置节与 v1.2 相同：renderer 1.2.1 须与 v1.2 逐字节一致（IMPL_CONTRACT §1.9、§5.7）。根目录一节照常列出 `/folders Notion_Obsidian`，但 sortspec 不为 `Notion_Obsidian` 及其 `写回预览`、`运行记录`、`conflicts` 文件夹增加配置节。WRITEBACK_SPEC 5.1a 与第 19 节的这一条暂缓执行；以后若要增加，须同时提升 RENDERER_VERSION。校验器 C05 已能接受这类配置节。代码已备好、默认不生效：RENDERER_VERSION 一旦不再是 1.2.1，导出器就写入 `sortspec.TASK_SECTIONS`（`Notion_Obsidian` 一节，另为 `运行记录`、`写回预览` 各写一节，用 `> a-z` 让最新的排在最上面），README 同时换成 `README_LINES_NEXT`（写明实际调度：每 6 小时增量、周日 05:30 全量；写回须人工确认；唯一操作文档的路径，R-048）。是否升级版本由负责人决定（升级后，写回会拒绝旧版本渲染的笔记，直到下一轮刷新）。
- **§10 状态**：新增 `WORK/state/writeback.db`（base_history、block_lines、name_overrides、pending_deletes、trash_archive、写回计划/确认/日志等）；base_history 由发布器写入（首轮为全部笔记建立基线）。`published_files` 增加 `staged_sha256`（上次发布的 Notion 渲染）与状态 `trashed_via_writeback`、`renamed`；export.db 增加 `publish_intents`（崩溃恢复）、`conflict_copies`（冲突副本去重）。
- **§11 发布**（替换原“已存在且被本地修改 → 冲突副本”一条）：
  - 分类：UNCHANGED / ADD / REPLACE / MOVE / LOCAL_ONLY（Notion 未变、本地改过：不写、不出冲突副本，列入“待写回的本地修改”）/ LINK_REWRITE_ONLY / LOCAL_KEYS_ONLY（仅链接写法或本地附加键不同：备份后按 Notion 版本更新并保留本地键）/ ADOPT、ADOPT_RENAME（采纳写回结果或本地改名）/ SKIP_HELD（对象级暂停）/ GENERATED_OVERWRITE（工具生成文件，备份后覆盖）/ CONFLICT_*（双方都改：库内文件不动）。
  - 冲突副本：`Notion_Obsidian/conflicts/<run_id>/<原文件夹>/<原名> (Notion 版本 <run_id>).md`，frontmatter 为 `notion_kind: generated`、`generated_by: publish_vault conflict-copy`、`conflict_of_notion_id`、`conflict_run`、`conflict_vault_path`，不含 `notion_id`/`notion_url`/`aliases`（避免重复 ID 与链接歧义）；同一 (notion_id, Notion 版本) 只写一次（实施决定 L-07）。
  - 两步发布：先只生成计划，经 WRITEBACK_SPEC 第 14 节评估（对象级暂停 → `state/auto_sync_hold.json`；整轮暂停 → `paused_for_review` 与 `运行记录/<run>-已暂停.md`），再按计划 sha 执行；`refresh.py --approve-run/--discard-run` 由用户在终端决定。
  - 写回后的采纳发布（WRITEBACK_SPEC §11 第 4 步，实施决定 L-16）：先对整个重抓快照导出并校验，再用 `--adopt-writeback` 正常发布整个快照，hold 与 refresh 相同。只有存在未放行的 `paused_for_review` 轮次时，才加 `--only-ids`，只采纳计划内笔记。若正常发布会写入计划内笔记、其父笔记和生成的索引文件以外的计划外对象，则改用 `--only-ids` 只发布计划内笔记及其父笔记；目录索引等生成文件留到下一次 refresh 再更新。
- **§13 本地删除**：改按 WRITEBACK_SPEC 6.0（`mode=off` 时下一轮按 Notion 版本恢复；`mode≥plan` 时进入 pending_deletes 待决，超出上限整轮暂停），库内与 Notion 都绝不自动删除。**校验**：增加 `--expected-deviations`（已登记的本地差异降为 INFO，未登记的照常 FAIL；条目先与库内实况核对，不符即作废）、`--ids` 范围校验、`--snapshot-immutable`，以及对象级暂停。
