> **设计 / 调研文档**｜Notion ⇄ Obsidian 双向同步调研与可落地设计｜版本 **v0.1（设计稿）**｜仓库副本，正文未改动（来源：开发沙箱 `notion-obsidian-dev/docs/BIDIRECTIONAL_DESIGN.md`）。本文是 2026-11-17 的设计与决策记录，后续定稿见 [WRITEBACK_SPEC.md](WRITEBACK_SPEC.md) 与 [SPEC.md](SPEC.md)；文中“未启用”等表述反映当时状态。返回[文档索引](../README.md)。

# Notion ⇄ Obsidian 双向同步：调研与可落地设计（v0.1，设计稿，未启用）

- 编写日期：2026-11-17；官方文档核实日期：2026-11-17（下文表格中的 URL 均于当日访问）
- 适用范围：Notion 工作区 Godcreate 的 5 个团队协作区 ⇄ 库 `/data120/Obsidian/Godcreate`（远程库 “Godcreate”，Obsidian Sync Plus）
- 契约基础：`docs/SPEC.md`（v1 + v1.1 + v1.2 §13）。本文不修改 SPEC，需要改动 SPEC 的地方列在 §15，等用户确认后再写入 SPEC v1.3。
- 状态：**仅为设计**。编写本文时没有调用 Notion API，没有写入正式库，也没有操作 Obsidian 界面。文中的统计来自只读打开的快照库 `snapshots/snap_20261117T0417.db`（`mode=ro&immutable=1`）、状态库 `state/export.db`（`mode=ro`）以及运行日志。

---

## 0. 结论摘要

1. **四条需求都可以部分做到，但都不能完整做到。**
   - 需求 1（Notion → Obsidian）：已经实现，目前每天运行。可以提速到“近实时”。
   - 需求 2（Obsidian → Notion）：可以做，但只适用于能无损往返的内容，例如段落、标题、列表、待办、引用、代码、公式、分隔线、表格单元格和数据库行的简单属性。约 94% 的笔记（5,245 篇）正文没有有损元素。其余 311 篇（约 5.6%）含分栏、同步块、内嵌数据库、目录、颜色或特殊提及。改动落在这些区域时，**拒绝写回，记为待人工处理**，不做有损覆盖。
   - 需求 3（实时一致）：**做不到真正的实时，也做不到强一致**，只能做到“近实时 + 最终一致 + 冲突显式化”。原因有三：Notion 免费套餐限流 180 次/分钟；API 没有条件写（没有 If-Match）；Webhook 至多投递一次，延迟最多 5 分钟。
   - 需求 4（误删找回）：可以做到，并且应当先于写回上线。方案分 6 层：Notion 回收站 30 天、Notion 页面历史 7 天（免费）、Obsidian Sync 版本历史 12 个月（附件 2 周）、File Recovery 7 天、本管线库外备份与快照，以及写回日志中的前像（不过期）。
2. **延迟估计**（基于本库规模和实测速率；2026-11-17 增量快照用 34 分钟发出 5,530 次 GET，约 2.7 次/秒）：
   - Obsidian → Notion：通常 **2–5 分钟**。这包括 Obsidian Sync 传到本机、静默期 120 秒和写入用时。前提是本机 Obsidian 客户端一直运行。
   - Notion → Obsidian：
     - 用轮询时，热点页面（近 7 天编辑过的约 464 页）**约 10 分钟**；其余页面要等全量巡检，每 6 小时一次，每次约 35–45 分钟。
     - 加 Webhook 后，通常 **1–3 分钟**，最坏约 6 分钟。但这需要一个公网 HTTPS 端点，而且仍要保留巡检兜底。
3. **绝不整页覆盖写回。**整页覆盖有四个后果：
   - 所有块 ID 会变，块级链接、评论锚点、同步块引用随之失效；
   - 颜色、分栏、同步块、内嵌数据库都会丢失；
   - 删除 `child_page` 块会把子页面送进回收站；
   - `erase_content` 无法通过 API 撤销。

   写回只做**块级差异**：导出时记录“块 ↔ 行范围”映射，只对改动的块调用 update/append/delete，没改动的块不碰。
4. **推荐方案 B：“Notion 为主、受控块级回写”，分阶段上线。**
   - 阶段 0/1 只读：补齐块映射，在全库上测试往返；连续 7 天演练，只出报告，不写 Notion。
   - 阶段 2：另建一个**独立的写回集成**，在 Notion 里只授权给测试页和白名单页，用 Notion 自身的权限把写入范围硬性限死。
   - 阶段 3：全量开放。删除传播设宽限期、熔断阈值，并要求人工确认。
   - 阶段 4（可选）：接入 Webhook。

   主体工作量估计 **14–17 人日**，另需约 2–3 周日历时间做演练。更保守的折中方案 C 约 4–6 人日：Notion 区域在库内只读，只有专门的“本地笔记区”回写。
5. **启用写回前必须由用户确认的关键事项**（完整清单见 §14）：
   - 工作区成员数：免费套餐多成员工作区的 API 有 1,000 块终身上限，本工作区已有约 25.4 万块，若是多成员则无法通过 API 新建任何块；
   - 是否创建带写权限的独立集成；
   - 删除传播策略；
   - Obsidian Sync 冲突处理是否改为“创建冲突文件”；
   - 本机 Obsidian 客户端能否常驻；
   - 是否允许暴露公网 Webhook 端点。

---

## 1. 需求映射

| # | 用户原话（摘要） | 本设计的落点 | 能做到的程度 |
|---|---|---|---|
| 1 | Notion 的变动、新增同步到 Obsidian | 现有管线：增量快照 → 导出 → 校验 → 发布。另加热点轮询（可选 Webhook） | 能做到。热点页面约 10 分钟，全量页面 ≤ 6 小时，接入 Webhook 后 1–3 分钟 |
| 2 | Obsidian 的变动、新增同步到 Notion | 新增写回链路：库扫描 → 三方比较 → 块级差异 → 受控写入 → 回读校验 | 部分能做到。可往返内容能写回；不可往返区域拒绝写回并提示 |
| 3 | 实时互通，增删改一致 | 单一调度器串行执行双向同步，配合防回环、冲突显式化和删除熔断 | 近实时、最终一致。两边同时修改时不自动合并 |
| 4 | 误删找回、版本回退 | 6 层恢复体系（§11），另有每日库快照、写回前像日志、`notion_rollback.py` 和定期演练 | 能做到。Notion 侧不依赖 7 天页面历史，写回前像不过期 |

---

## 2. 现状（只读核实）

| 项目 | 数值 / 事实 | 来源 |
|---|---|---|
| 库内笔记 | 5,556 篇（page 5,421 + row 117 + database 18），另有附件 277 个、`.base` 18 个 | `state/export.db` objects_map / published_files |
| 快照对象 | page 5,538、database 18、data_source 17；块 253,944 个，其中有子块的 14,635 个 | `snap_20261117T0417.db` |
| 每页块数 | 中位数 8，P90 111，P99 597，最大 6,093 | 同上 |
| 评论 | 0 条（快照 comments 表为空） | 同上 |
| 近期编辑 | 近 1 天 35 页，近 7 天 464 页（截至快照） | 同上 `last_edited_time` |
| `last_edited_time` 精度 | 实测为整分钟（秒位恒为 00），snapshot.py 已用 120 秒安全余量 | 快照数据；`tools/snapshot.py` |
| 全量快照耗时 | 2 小时 14 分（2026-11-16 18:44→20:58 UTC） | 快照 meta |
| 增量巡检耗时 | 5,530 次请求，34 分钟，0 次重试（每页 1 次 GET，比较 last_edited_time） | `logs/refresh_20261117T0417-T1.log` |
| Notion search 覆盖率 | 只返回库内 5,556 个对象中的 1,025 个（18.4%），不能用于变更检测 | `reports/20261117T0417-T1/acceptance/completeness_probe.md` |
| API 版本 | `Notion-Version: 2026-03-11` | 快照 meta；`nmig/client.py` |
| 现有写方法 | `nmig/client.py` 已有 create_page / update_page / append_children（使用 `position.after_block`）/ update_block / trash_block / upload_bytes，但现有管线从未调用 | 代码阅读 |
| 集成 | 内部集成，Token 在 `~/.config/notion-migrate/.env`；读写能力**未核实**（按要求不实测）；内部集成只能看到授权时勾选的页面及其子孙 | 任务说明；项目记忆 |
| 发布规则 | 不删除；本地改动 → 冲突（库内文件不动，Notion 版本写到 `Notion_Obsidian/conflicts/<run_id>/`）；替换前备份到 `backup/vault-before/<run_id>/`；日志写入 `publish_journal.jsonl`；可用 `rollback.py` 回滚 | `tools/publish_vault.py`、`tools/rollback.py` |
| Obsidian Sync | Plus 套餐，10 GB；客户端冲突处理当前为“自动合并”（02:34 截图） | `approvals/standing_daily_refresh.approval.md` |
| 已发生的事故 | 第二台设备的改动不是用户有意为之，已按用户确认恢复（SPEC §13；`reports/restore_20261117T081336.jsonl`）。**说明“库内删除 = 用户意图”这个假设不成立** | SPEC §13 |
| 磁盘 | `/data120` 共 118G，已用 83%，剩余 20G；单个快照库约 456 MB；库 147 MB | `df`、`du` |
| 本机工具 | Python 有 `markdown_it`、`yaml`、`pyinotify`；inotify 上限 524,288；库内目录 606 个 | 本机核查 |

### 2.1 本库的往返损失量化（来自快照统计）

| 不可往返或有损的元素 | 数量 | 涉及页面 | 写回时的处理 |
|---|---|---|---|
| 块级颜色（`color` ≠ default） | 476 块 | 207 页 | 只改文字时，PATCH 不带 `color` 字段，颜色得以保留（官方说明：未提供的字段保持不变）。但不能重建这些块 |
| 富文本颜色 | 71 段 | 8 页 | 原样复用未改动的片段；改动触及带色片段时拒绝 |
| column_list / column | 66 / 132 块 | 66 页 | 区域只读，改动即冲突 |
| synced_block | 6 块 | 5 页 | 只读。官方说明 API 不支持更新同步块内容 |
| child_database | 11 块 | 2 页 | 只读（显示为 `.base` 嵌入） |
| table_of_contents | 117 块 | 约 110 页 | 注释行只读 |
| link_preview（块） | 102 块 | 36 页 | 只读。官方说明 API 不能创建 link_preview |
| 带子块的段落（展开渲染） | 2 块 | — | 只读 |
| unsupported | 1 块 | 1 页 | 只读 |
| 提及：日期 26 / link_preview 68 / 页面 105 / link_mention 231 | — | — | 片段原样复用；改动触及时页面提及可以反解，日期和 link_preview 提及则拒绝 |
| 行内公式 | 75 段 | — | `$…$` 可往返 |
| 恰好 2,000 字的富文本片段 | 3,971 段 | — | 写回时按 ≤ 2,000 字切分（官方上限） |
| 含上述结构性只读块的页面（含带子块段落） | **224 页（约 4.0%）** | | 页面其余部分仍可写回 |
| 含任意颜色的页面 | **212 页（约 3.8%）** | | 同上（只改文字时颜色保留） |
| 含日期、link_preview 或用户提及的页面 | 66 页 | | 片段不动即可写回 |
| **含以上任一元素的页面（并集）** | **311 页（约 5.6%）** | | 其余 **5,245 篇（约 94%）** 的正文没有任何有损元素 |

---

## 3. 官方事实核实（2026-11-17）

### 3.1 Notion API 写能力

| 能力 | 官方结论（要点，引号内为原文） | 来源 URL | 对设计的影响 |
|---|---|---|---|
| 追加子块 | `PATCH /v1/blocks/{id}/children`。每次最多 “100 block children”；一次请求内最多 “two levels of nesting”；默认追加到末尾；`position` 可取 `start`、`end`、`after_block`。**“The `after` parameter is deprecated”**，不能与 `position` 同时使用。“Existing blocks can't be moved”，块追加后不能用 API 挪动 | https://developers.notion.com/reference/patch-block-children | 插入新块用 `position.after_block`。块的移动和重排只能删除后重建，块 ID 会变 |
| 2026-03-11 破坏性变更 | 三项：`after` → `position`；`archived` → `in_trash`（请求和响应都改）；`transcription` 块更名为 `meeting_notes` | https://developers.notion.com/guides/get-started/upgrade-guide-2026-03-11 | 固定使用 `Notion-Version: 2026-03-11`，只用 `in_trash` |
| 更新块 | `PATCH /v1/blocks/{id}`。按类型可改 `rich_text`、`checked`、`language`、`caption`、`expression`、`color`、`is_toggleable`、表格行 `cells` 等。“A block's children CANNOT be directly updated with this endpoint”；“The update replaces the entire value for a given field”，未提供的字段保持不变；`child_page` 要改页面，`child_database` 要改数据库 | https://developers.notion.com/reference/update-a-block | 只发生变化的字段，从而保留颜色和可折叠标记。块类型不能原地改（例如段落改标题），只能删除后重建 |
| 删除块 | `DELETE /v1/blocks/{id}` 把块（含页面块）设为 `in_trash: true`，进入回收站后“can still be accessed and restored”。恢复方式：块用 Update a block，页面用 Update page | https://developers.notion.com/reference/delete-a-block | 删除可以恢复。**删除 `child_page` 块等于把子页面送进回收站**，所以写回永远不删 `child_page` 块 |
| 页面进出回收站 | Update page 设 `in_trash: true` 移入回收站，设为 `false` 恢复。“The API can't restore a top-level page that isn't in a teamspace”。“The API does not support permanently deleting pages” | https://developers.notion.com/reference/trash-page | 删除传播只做“移入回收站”，可以撤销 |
| 更新页面 | `PATCH /v1/pages/{id}` 可改属性、icon、cover、`in_trash`、`is_locked`、模板。“a page's `parent` cannot be changed”。rollup 不可改。`erase_content` 会删除全部子块，“cannot be reversed using the API” | https://developers.notion.com/reference/patch-page | 属性写回走这个端点；**永不使用 `erase_content`** |
| 移动页面 | `POST /v1/pages/{id}/move`，父级只能是 `page_id` 或 `data_source_id`；“Moving databases or other block types in the API is not currently supported”；机器人必须对新父级有编辑权限；**没有同级位置参数** | https://developers.notion.com/reference/move-page | 库内跨文件夹移动可以写回（仅限普通页面）。同级顺序调整无法写回 |
| 创建页面 | `POST /v1/pages`，父级可以是 page / data_source（内部集成**必须**指定 page 或 data source，不能建在工作区顶层）；可带 `children`（≤ 100）或 `markdown`（v2），两者互斥；`position` 支持 `after_block`、`page_start`、`page_end`；formula、rollup、ID、创建时间、编辑时间类属性必须省略 | https://developers.notion.com/reference/post-page | 新笔记必须能找到父页面，团队协作区根目录下的新笔记拒绝创建 |
| Markdown 写入端点 | `PATCH /v1/pages/{id}/markdown`，支持 `update_content`（search/replace，每次 ≤ 100 条）和 `replace_content`。默认拒绝删除子页面和数据库。“Synced pages cannot be updated”。文档**没有说明未改动内容的块 ID 是否保留** | https://developers.notion.com/reference/update-page-markdown | 不用于编辑（块 ID 保留无保证，方言也和 SPEC §5 不同）。项目记忆里还有 toggle 内代码块解析的坑 |
| 文件上传 | `POST /v1/file_uploads`，模式有 `single_part`（≤ 20 MiB）、`multi_part`、`external_url`；用 `type: file_upload` 加 id 挂到块、页面或属性上；**免费工作区单文件 5 MiB**；未完成的上传按 `expiry_time` 过期（文档未给时长） | https://developers.notion.com/docs/working-with-files-and-media ；https://developers.notion.com/reference/file-upload ；https://developers.notion.com/reference/create-a-file-upload | 新附件写回放到阶段 3；超过 5 MiB 的拒绝 |
| 能力（capabilities） | 内容能力分 Read / Update / Insert 三项，可任意组合；Insert “does not give the connection access to read full objects” | https://developers.notion.com/reference/capabilities | 写回集成需要 Read + Update + Insert，评论能力不需要 |
| 视图 | 有 views API（create / retrieve / update / delete / list / query）；“A database must always have at least one view”；文档不保证无损往返 | https://developers.notion.com/guides/data-apis/working-with-views | Bases（`.base`）与 Notion 视图模型不同，**不写回视图** |
| 数据源查询的时间过滤 | “Use a timestamp filter condition to filter results based on `created_time` or `last_edited_time` values” | https://developers.notion.com/reference/filter-data-source-entries | 数据库行（117 行，17 个数据源）可以廉价地增量检测 |

### 3.2 块类型可写性

来源：https://developers.notion.com/reference/block 、https://developers.notion.com/reference/update-a-block 、https://developers.notion.com/reference/patch-block-children

| 块类型 | 创建 | 更新 | 说明 / 设计分类 |
|---|---|---|---|
| paragraph、heading_1–4、bulleted/numbered_list_item、to_do、quote、toggle、callout、code、equation、divider | 可以 | 可以（按字段） | 可往返（R），toggle 和 callout 部分可往返（P） |
| table / table_row | 可以（建表时至少一行，`cells` 长度等于 `table_width`） | 行 `cells` 可改；**“`table_width` … cannot be changed using the API once a table is created”** | P：改单元格、增删行可以，增删列算冲突 |
| image / video / pdf / audio / file | 外链或 file_upload 可以 | 可改 `caption`、`external`、`file_upload` | P：阶段 2 只写回 caption |
| bookmark / embed | 可以 | 可改 `url`、`caption` | P |
| column_list / column | 可以（“at least two `column`s, and each `column` must have at least one child”） | column 只有 `width_ratio` | **X**：线性化渲染无法可靠反解 |
| synced_block | 原始块要先于副本创建 | **“The API does not supported updating synced block content”**（原文如此） | **X** |
| child_page / child_database | 不能用 append 创建，要用 create page / database | 走页面或数据库端点 | **X**（只是链接行）。删除链接行**不等于**删除页面 |
| link_preview | **“does not support creating or appending `link_preview` blocks”** | 无 | **X** |
| template | 2023-03-27 起不能创建 | — | **X** |
| table_of_contents / breadcrumb | 可以 | toc 只能改 `color` | **X**（注释行原样保留） |
| unsupported / meeting_notes 等 | 不能 | 只能改 `in_trash` | **X** |
| 数据库视图 | views API | views API | 不在写回范围 |

### 3.3 Webhooks

| 事实 | 原文 / 要点 | 来源 |
|---|---|---|
| 配置方式 | 在集成设置的 **Webhooks** 标签页 “+ Create a subscription”；端点 “must be a secure (SSL) and publicly available endpoint”，“Endpoints in localhost are not reachable”；首次会 POST 一个 `verification_token`，需要回填完成验证；验证后 URL 不可再改 | https://developers.notion.com/reference/webhooks |
| 签名 | `X-Notion-Signature` 是用 `verification_token` 对请求体做的 HMAC-SHA256，必须用原始请求体做常量时间比较 | 同上 |
| 负载 | 只是信号，不含正文：实体 ID、事件类型、时间戳、`authors`、`attempt_number` 等。`page.content_updated` 带 `data.updated_blocks`（发生变化的块 ID） | 同上；https://developers.notion.com/reference/webhooks-events-delivery |
| 事件类型 | `page.*`（content_updated、created、deleted、undeleted、moved、properties_updated、locked、unlocked…）、`data_source.*`、`database.*`（部分已弃用）、`comment.*`、`view.*`、`file_upload.*`；**没有 `block.*` 事件** | https://developers.notion.com/reference/webhooks-events-delivery |
| 时效 | 高频事件 “aggregated … within a time window of about one minute”；“Events should be delivered within 5 minutes”，大多数在 1 分钟内送达；可能乱序（按 `timestamp` 重排）；可能不是最新状态，需要回查 API | 同上 |
| 可靠性 | “We aim for at-most-once event delivery”；端点不确认时最多重试 8 次，最后一次约在 24 小时后 | 同上 |
| 访问范围 | 只有集成有权访问的对象才会触发事件 | https://developers.notion.com/reference/webhooks |

**结论**：Webhook 是“近实时的提示”，不是可靠的变更流，必须保留巡检兜底。本机没有公网 HTTPS 端点，要用就得新开反向代理或隧道，这是用户需要决定的安全事项。我抓取的 webhooks 参考页没有明文写出“内部连接可用”；搜索摘要显示可用。需要用户在集成设置里确认有没有 Webhooks 标签页（见 §3.7）。

### 3.4 限流与尺寸（https://developers.notion.com/reference/request-limits）

- 每个连接每 60 秒有固定预算：Business 和 Enterprise 为 600 次/分钟；**其他套餐（含免费）为 180 次/分钟（平均 3 次/秒）**，窗口内可以突发；超限返回 429，`Retry-After` 不超过 60 秒。数据源查询另有约 3 次/秒的独立限额。**整个工作区还有一个共享限额**，所有连接共用，可能在单连接未超限时就被限流。529 按 429 处理。
- 尺寸：`text.content` 和链接 URL ≤ 2,000 字；`equation.expression` ≤ 1,000 字；块数组、富文本数组 ≤ 100 个元素；整个请求 ≤ 1,000 个块元素且 ≤ 500 KB。
- **免费工作区块上限**（https://developers.notion.com/reference/workspace-block-limits）：“Free workspaces with more than one member have a limit of 1,000 lifetime blocks.” 付费工作区和**单成员**免费工作区不限，访客不算成员；REST API 对内部连接执行这一上限。达到上限后“requests that create blocks fail”，返回 HTTP 403 `restricted_resource`，`additional_data.block_limit = block_creation`；读取、删除和非创建类编辑仍然可用；“Deleting blocks does not restore capacity”。request-limits 页提到该执行从 2026-09-08 起生效。**本工作区已有约 25.4 万块。如果是多成员免费工作区，写回只能改现有块，不能新建任何块或页面。**

### 3.5 Notion 恢复能力（https://www.notion.com/help/duplicate-delete-and-restore-content）

| 项目 | 官方结论 |
|---|---|
| 回收站 | “By default, pages will remain in Trash for 30 days before they are permanently deleted from Trash.” 之后再保留 30 天，但“inaccessible to all users, even workspace owners”。只有 Enterprise 的所有者可以自定义；页面没有按套餐区分。误删后可以联系客服，客服能从“past 30 days”的备份恢复快照 |
| 页面历史 | 免费 **7 天**，Plus 30 天，Business 90 天，Enterprise 不限。活跃编辑时每 10 分钟记录一个版本，最后一次编辑 2 分钟后再记录一个。可以 Restore，恢复后还能回到恢复前的状态；也可以从旧版本复制块 |
| 数据库历史 | 恢复数据库会恢复行和属性，但“contents of the database pages … won't be restored”，视图可能改变 |
| 块级撤销 | 没有单块撤销，只能从历史版本复制粘贴 |

### 3.6 Obsidian Sync 与 File Recovery

| 项目 | 官方结论 | 来源 |
|---|---|---|
| Plus 版本历史 | Version history：Standard 1 个月，**Plus 12 个月**；“On the Standard plan, notes are retained for 1 month, while on the Plus plan, they are kept for 12 months” | https://obsidian.md/help/sync/plans ；https://github.com/obsidianmd/obsidian-help/blob/master/en/Obsidian%20Sync/Version%20history.md |
| 附件版本 | 旧版本“stored for two weeks” | 同上 |
| 已删除文件 | 设置 → 核心插件 → 同步 → “Deleted files” → View → 选版本 → Restore，文件回到“its original location”；支持批量恢复（批量时不能逐个预览） | https://obsidian.md/help/sync/version-history |
| 冲突处理 | Markdown 用 diff-match-patch 自动合并，可能产生重复文本或格式问题；其他文件（含 canvas）“last modified wins”；设置 JSON 按键合并。可选“Create conflict file”，生成 `原名 (Conflicted copy 设备名 YYYYMMDDHHMM).md`，原文件保留远程版本。**该设置按设备生效，每台设备都要单独设** | https://github.com/obsidianmd/obsidian-help/blob/master/en/Obsidian%20Sync/Troubleshoot%20Obsidian%20Sync.md （网页版 https://obsidian.md/help/sync/troubleshoot ） |
| File Recovery | 默认快照间隔“a minimum of 5 minutes”，默认保留 **7 天**，可调；快照存在库外，“device-specific and remain local to each device”；“Only `.md` and `.canvas` files can be restored” | https://github.com/obsidianmd/obsidian-help/blob/master/en/Plugins/File%20recovery.md （网页版 https://obsidian.md/help/plugins/file-recovery ） |

### 3.7 未能在官方文档逐字核实、需要实测或由用户确认的点

1. 通过 API 把父页面移入回收站时，子页面是否随之进回收站：文档没写，界面行为一般如此。阶段 2 在测试页上实测。
2. 内部连接能否订阅 Webhook：参考页未明写，请用户在集成设置里看有没有 Webhooks 标签页。
3. 文件上传过期时长：文档只给出 `expiry_time` 字段。
4. 在 Notion 新建子页面会不会更新父页面的 `last_edited_time`：这决定热点轮询能否及时发现新页面。阶段 1 只读观察。
5. 更新 select / multi_select 属性时，用不存在的选项名会不会自动建选项：阶段 2 只允许已有选项。
6. Notion 免费套餐能否在界面里导出整个工作区为 Markdown：未核实，作为可选的离线备份。
7. 本工作区的成员数、现有集成的能力勾选：按要求未实测，由用户确认。

---

## 4. 可行性结论

### 4.1 能做到的

- 段落、标题、列表、待办（含勾选）、引用、代码（含语言）、公式、分隔线的**文字改动、新增、删除和同页内移动**。移动时删除后重建，块 ID 会变，但用原块 JSON 重建可以保留颜色。
- 表格单元格改动和增删行。
- 数据库行的简单属性：title、number、checkbox、select/status（限已有选项）、multi_select、date、url、email、phone、relation（目标在映射内时），以及“原值是单段无格式文本”的 rich_text。
- 在已映射的文件夹里新建笔记，对应地新建 Notion 页面或数据库行。
- 普通页面跨父级移动（`/move`）和改标题。
- Obsidian 删除笔记后，Notion 对应页面**移入回收站**（30 天内可恢复）；Notion 删除页面后，库内文件**移到库外可恢复区**。两个方向都有宽限期和熔断。
- 冲突（两边同时改）可以可靠地**检测**并保留双方版本。

### 4.2 有损或做不到的

| 项目 | 原因 | 处理 |
|---|---|---|
| 真正实时 | 免费套餐 180 次/分钟；5,556 页全量巡检至少约 31 分钟（实测 34 分钟）；Webhook 聚合约 1 分钟、送达 ≤ 5 分钟，而且至多一次 | 近实时：热点页面加 Webhook，配合全量兜底 |
| 强一致 / 原子写 | API 没有条件写（没有 ETag / If-Match），一页的多个块操作不是事务 | 写前紧邻预检、写后回读校验，失败时按前像补偿，并显式报告冲突 |
| 分栏、同步块、内嵌数据库、目录、link_preview、模板块、带子块的段落 | 渲染时线性化，或者 API 不支持写（§3.2） | 区域只读，改动就拒绝该页写回，记为 `CONFLICT_UNSUPPORTED` |
| 颜色（块级、富文本） | 渲染时丢弃（SPEC §5） | 只改文字时保留，触及带色片段时拒绝 |
| 用户提及、日期提及、link_preview 提及 | 渲染成纯文本或链接，无法反解 | 片段未改就原样复用，改了就拒绝 |
| 块级评论、块锚点链接 | 块删除重建后 ID 改变 | 当前评论数为 0，影响小；仍然只在必要时重建 |
| Notion 子页面的同级顺序、custom-sort 顺序 | `/move` 没有位置参数，块不能挪动 | 不写回顺序，以 Notion 为准 |
| 数据库结构、视图、`.base` | Bases 与 Notion 视图模型不同 | 不写回，以 Notion 为准 |
| 公式、汇总、创建/编辑时间、unique_id、people、files 属性 | API 只读，或名称到 ID 的映射有损 | 不写回 |
| 新附件 > 5 MiB | 免费工作区单文件上限 | 拒绝，并提示 |
| 团队协作区根目录下新建页面 | 内部集成不能建工作区顶层页面；侧栏根对象由用户在 Notion 里维护 | 拒绝，提示放到某个页面下 |
| Obsidian 中 `==高亮==`、脚注、块引用 `^id`、`![[笔记嵌入]]`、HTML、Dataview 等 | Notion 没有对应结构，渲染器也产生不出这些写法 | 拒绝该处写回（新增内容含这些写法时，整段拒绝） |

### 4.3 为什么不能整页覆盖写回

1. **块 ID 全部改变**：Notion 内部的块链接（`#blockid`）、评论锚点、同步块副本的 `synced_from`、页面历史的可读性都会受影响。
2. **结构性丢失**：约 4% 的页面含分栏、同步块、内嵌数据库，约 4% 的页面含颜色。覆盖后这些全部被抹平，而且没有报错。
3. **误删子页面**：页面正文里的 `child_page` 块就是子页面本身，删掉就把子页面连同子树送进回收站。`erase_content` 还“cannot be reversed using the API”。
4. **成本与历史**：一页的全部块都要删除重写，P99 页面有 597 块。按 3 次/秒算，一页要几分钟，页面历史也会被大量机器版本淹没。
5. **免费套餐块上限**：如果是多成员免费工作区，删除后无法重建（403）。整页覆盖会让页面直接变空。

### 4.4 延迟与容量估算

| 场景 | 请求数估算 | 耗时估算（≈ 2.7 次/秒） |
|---|---|---|
| 全量巡检（每页 1 次 GET） | ≈ 5,556（实测 5,530） | 34 分钟（实测）。如果写回同时占用预算，按 2 次/秒算约 46 分钟 |
| 热点轮询（近 7 天编辑过的约 464 页）加 17 个数据源的时间过滤查询 | ≈ 481 / 轮 | ≈ 3 分钟 / 轮，每 10 分钟一轮，约占 0.8 次/秒 |
| 拉取一页的变更块 | 每 100 块 1 次，另加每个有子块的块 1 次；中位页 1–2 次，P99 页约 10–30 次 | 1–10 秒 |
| 写回中位页里的 3 段改动 | 预检 2 + PATCH 3 + 回读 2 ≈ 7 | ≈ 3 秒 |
| 新建一页（50 块，嵌套 ≤ 2 层） | 1 次 create，回读 1–2 次 | ≈ 1–2 秒 |
| 一次改动 1,000 块（分布在 100 页） | ≈ 1,000 PATCH + ≈ 400 GET | ≈ 9 分钟，而且会触发熔断审批（§8.3） |
| Obsidian → Notion 端到端 | Obsidian Sync 传到本机（双方在线时通常为秒级）+ 静默期 120 秒 + 写入数秒 | **≈ 2–5 分钟**。前提是本机 Obsidian 客户端运行中，否则要等客户端下次启动 |
| Notion → Obsidian 端到端（只用轮询） | 热点页面 ≤ 10 分钟，其他页面 ≤ 全量巡检间隔（建议 6 小时）加 45 分钟 | 10 分钟到 7 小时 |
| Notion → Obsidian 端到端（加 Webhook） | 聚合约 1 分钟 + 送达 ≤ 5 分钟 + 拉取渲染发布数秒 + Obsidian Sync 分发 | **通常 1–3 分钟，最坏约 6 分钟**。事件丢失时由巡检兜底 |

---

## 5. 总体架构

```
            ┌──────────── 单一调度器 bidi_sync.py（flock：state/refresh.lock，与现有 refresh 共用） ────────────┐
            │                                                                                       │
 Obsidian   │  ① 库扫描 vault_scan        ② Notion 变更收集 notion_poll         ③ 三方比较 classify        │   Notion
 (本机客户端 │   inotify 去抖 + 定时全扫     热点 GET / 数据源时间过滤 /         base / local / remote      │  (2026-03-11)
  + Sync)   │   sha256 ↔ published_sha    增量快照 / 可选 webhook 队列       → 动作计划 plan.json        │
     ▲      │                                                                                       │     ▲
     │      │  ④a Notion→库：export_obsidian + verify + publish_vault（现有，扩展：删除→库外回收区）       │     │
     └──────┤  ④b 库→Notion：writeback（块级差异 → 预检 → 执行 → 回读校验 → 提交新 base）───────────────┼─────┘
            │  ⑤ 报告：WORK/reports/<run>/…，库内 Notion_Obsidian/同步状态.md（只含计数和路径）             │
            └───────────────────────────────────────────────────────────────────────────────────────┘
状态：state/export.db（现有）+ state/bidi.db（新增）+ state/base/<sha>（已发布笔记的内容寻址副本）
开关：config/bidi.json（mode = off | plan | whitelist | full）；急停文件 state/BIDI_STOP
```

要点：

- **一个进程、一把锁、串行执行。**一轮按 ①→②→③→④a→④b→⑤ 顺序跑，两个方向不会交错写。现有每日 `refresh.py` 改为调度器的“全量巡检”模式，共用同一把锁。
- **两个 Notion 集成（推荐）**：现有集成保持只读，供快照使用；新建“写回集成”，只开 Read + Update + Insert，Token 单独存放（chmod 600，不打印、不写日志）。好处有三：
  - 阶段 2 只把写回集成授权给白名单页面，由 Notion 权限**硬性限定**可写范围；
  - 写回集成的 bot 用户 ID 可以区分“自己写的”，用于防回环；
  - Token 泄露时影响面最小。
- 新增状态库 `state/bidi.db`（DDL 见附录 A）：
  - `block_lines`：块 ↔ 行范围映射；
  - `bases`：每篇笔记最后一次两边一致时的内容（base）；
  - `wb_journal`：写回日志，含前像；
  - `pending_deletes`：待删除队列；
  - `conflicts`：冲突；
  - `echo`：防回环记录。

---

## 6. 变更检测

### 6.1 Obsidian 侧

| 识别对象 | 方法 |
|---|---|
| 改动 | 当前文件 sha256 ≠ `published_files.sha256`（base）。用 pyinotify 监听 `IN_CLOSE_WRITE`、`IN_MOVED_*`、`IN_DELETE`、`IN_CREATE`，每个文件做**静默期去抖**：最后一次变化后 120 秒内没有新变化才处理（可配置）。另外每 10 分钟做一次全库 stat 扫描（约 5,600 个文件，用 mtime+size 预筛，再算 sha），防止漏掉 inotify 事件 |
| 身份 | frontmatter `notion_id`（权威）；没有 frontmatter 时用 `published_files.rel_path` 反查 |
| 新建 | `.md` 文件既没有 `notion_id` 也不在 `published_files` 里，并且位于已映射文件夹内（§7.5） |
| 删除 | `published_files` 中 status=active 的文件消失，同时按 `notion_id` 全库查找也找不到（排除改名或移动） |
| 改名 / 移动 | 同一个 `notion_id` 出现在新路径，旧路径消失。同目录内为改名，跨目录为移动。整个文件夹移动时，folder note 跟着移，按一次页面移动处理 |
| 排除 | `.obsidian/**`；`Notion_Obsidian/**`（生成文件、冲突副本、运行记录）；`*.base`；`kind ∈ {database, generated, template}`；Obsidian Sync 冲突副本（文件名含 `(Conflicted copy `）；本工具的临时文件 `.nmig-tmp-*`；`_attachments/**`（只在被新内容引用时处理） |
| 重复身份 | 两个文件带同一个 `notion_id`（复制粘贴或同步冲突副本）时，两者都不推送，记为冲突 |

### 6.2 Notion 侧

| 层级 | 机制 | 频率 | 成本 |
|---|---|---|---|
| 行（17 个数据源） | `POST /v1/data_sources/{id}/query`，用时间过滤 `last_edited_time on_or_after (游标 − 2 分钟)` | 每 10 分钟 | 17 次 |
| 热点页面 | 近 7 天任一侧改过的页面、近期新建页面的父页面、白名单页面：`GET /v1/pages/{id}` 比较 `last_edited_time` | 每 10 分钟 | 约 460 次 |
| 全量巡检 | 现有 `snapshot.py --incremental-from`（每页 GET 一次，变化的页重抓块） | 每 6 小时一次增量，每周一次全量（保留现有周日任务） | 约 5,560 次 |
| Webhook（阶段 4，可选） | 收到 `page.*` / `data_source.*` 事件后，校验签名，把实体 ID 入队，由调度器回查 | 事件驱动 | 按事件数 |
| 删除确认 | 某对象在巡检中消失时，直接 `GET` 它：`in_trash=true` 算删除；404 / 403 算**访问丢失**（例如授权被撤），**不算删除**，记为异常 | 按需 | 少量 |

### 6.3 三方比较与分类

对每篇笔记，取三个版本：base = 上次两边一致时的已发布内容（`state/base/<sha>`），L = 库内当前文件，R = Notion 当前内容按 SPEC 渲染的结果（与 base 用同一个渲染器、同一个状态库）。

| base→L | base→R | 分类 | 动作 |
|---|---|---|---|
| 未变 | 未变 | UNCHANGED | 无 |
| 未变 | 变 | REMOTE_ONLY | Notion → 库（现有 REPLACE / MOVE / ADD） |
| 变 | 未变 | LOCAL_ONLY | 库 → Notion 写回（§7），受模式和白名单限制 |
| 变 | 变，且 L == R | CONVERGED | 只更新 base |
| 变 | 变，且 L ≠ R | **CONFLICT_BOTH** | 不自动合并（§9） |
| 删除 | 未变 | LOCAL_DELETE | 进入待删除队列（§8.1） |
| 未变 | 删除（in_trash） | REMOTE_DELETE | 库内文件移到库外回收区（§8.2） |
| 删除 | 变 | CONFLICT_DEL_EDIT | Notion 版本按 ADD 写回库，并提示（与 SPEC §13 现行规则一致） |
| 变 | 删除 | CONFLICT_EDIT_DEL | 库内文件保留，不推送，提示 |
| 新文件 | — | LOCAL_NEW | 新建页面（§7.5） |

判定“R 未变”用两个条件：页面 `last_edited_time` 等于 base 对应快照中的值；**并且**写回前对受影响父块的子块做紧邻预检（§7.4）。只看分钟级时间戳不够。

---

## 7. 写回策略（库 → Notion）

### 7.1 块 ↔ 行范围映射（导出器扩展）

- `export_obsidian.py` 在 `render_blocks` / `render_block` 外层记录每个块的行范围，写入 `bidi.db.block_lines`，以 `(notion_id, base_sha)` 为键。记录的字段有：
  - 块 ID、父块 ID（或页面 ID）、序号、深度、类型；
  - 自身行范围 `own_start..own_end`（不含子块），以及整体行范围 `line_start..line_end`（含子块）；
  - 行前缀（列表缩进、`> ` 引用前缀）；
  - 往返分类 R/P/X；
  - 可写字段的规范化哈希 `src_hash`。

  行号从文件第 1 行开始算，包含 frontmatter；`dump_note` 去掉的首部空行要计入偏移。
- **硬性要求**：加映射之后，导出的库文件必须与现在**逐字节相同**。映射只写进状态库，不进库文件。导出本来就是确定性的（SPEC §10），所以同一快照加同一状态库可以重算映射，便于审计。
- 空段落折叠：连续空段落渲染成一个空行。映射里把空段落记为“零行块”，挂在前一个块之后。用户删掉那个空行时，按删除最后一个空段落处理。
- base 内容存档：每次发布或写回成功后，把笔记字节存到 `state/base/<sha[:2]>/<sha>`。暂存目录只保留 3 份，不能当 base 用。

### 7.2 往返分类

| 分类 | 块 / 片段 | 写回规则 |
|---|---|---|
| R 可往返 | paragraph（无子块）、heading_1–4、bulleted/numbered_list_item、to_do、quote、code、equation、divider、table_row；纯文本、粗体、斜体、删除线、行内代码、下划线 `<u>`、链接、行内公式；页面提及（wikilink 能在 objects_map 里反解） | 可以 UPDATE、APPEND、DELETE |
| P 部分可往返 | toggle（`> [!toggle]-`）、callout（只有 emoji 图标）、table（只改单元格、增删行）、媒体块 caption、bookmark/embed 的 url 和 caption、带块级颜色或 `is_toggleable` 的块（只改文字） | 只允许列出的字段变化，其余变化按 X 处理 |
| X 不可往返 | column_list/column 及其内部、synced_block 及其内部、child_database、child_page 链接行、link_to_page、table_of_contents、breadcrumb、template、unsupported、link_preview、带子块的段落；**片段级**：带颜色的片段、日期提及、link_preview 提及、用户提及 | 行范围内有任何改动，**拒绝该页全部写回**，记为 `CONFLICT_UNSUPPORTED`，附行号和原因 |

带 X 片段的块：只要这些片段渲染出的字符串在新文本中原样出现（片段复用，§7.3），该块的其他部分仍可写回；X 片段本身被改动时拒绝。

### 7.3 差异计算与操作生成

1. 分别拆开 base 和 L 的 frontmatter 与正文。frontmatter 的差异生成标题和属性操作（§7.6）。
2. 正文按行做差异（`difflib.SequenceMatcher(autojunk=False)`；可选 patience diff），得到以 base 行号表示的 replace / delete / insert 区段。
3. 定位：
   - replace / delete 区段：找出与之相交的**最内层块的自身行范围**，得到“块编辑”；
   - insert 区段：锚点是插入位置之前结束的最近一个块；父级按插入行的缩进和前缀推断（列表子项、引用内）。
4. 块编辑：
   - 用**已知原类型的反解器**解析新行。例如原来是 `- [ ] ` 待办，就按待办的格式反解；按 SPEC §5 / §5.1 逆向处理转义、显式序号、缩进宽度、`<strong>` 等 HTML 包裹、`<br>`、`\|`。
   - 富文本**片段复用**：把原 `rich_text` 的每个片段按渲染器渲染成字符串，在新文本里做最长匹配。匹配上的片段原样复用（保留颜色、提及类型、href），只有新增或改动的部分才生成新片段。
   - **规范化校验（必须通过）**：用导出器的同一个渲染函数渲染反解结果，结果必须与 L 中对应行**逐字节相同**；不同就拒绝。这样任何“渲染器产生不出来”的写法都会被挡住，不会悄悄丢内容。
   - 生成 `PATCH /v1/blocks/{id}`，**只带变化的字段**（例如 `{"to_do": {"rich_text": […], "checked": true}}`），不带 `color` 和 `is_toggleable`，这两项因此保持原值。
   - 类型改变（例如段落改为标题），或者行前缀变化导致层级改变：先 APPEND 新块（原块是 P 类时带上 color），再 DELETE 原块。原块有子块时，阶段 2 拒绝；阶段 3 只允许子块全是 R 类时按前像重建子树。
5. 整块删除（自身行和全部子块行都被删掉）：`DELETE /v1/blocks/{id}`，即移入回收站。**`child_page` 和 `child_database` 的链接行被删时，不删除页面**，记为冲突，提示用户在 Notion 中操作，或者在 Obsidian 中删除该笔记文件（走 §8.1）。
6. 插入：用 markdown-it-py（本机已装）解析插入行，得到带行号映射的块 token，再转换成 Notion 块规格，同样经过“渲染 == 原文”的校验。用 `PATCH /children` 加 `position.after_block`（锚点）或 `start` 写入，每次 ≤ 100 块、嵌套 ≤ 2 层，更深的层级用返回的 ID 继续追加。富文本按 ≤ 2,000 字、≤ 100 段切分。
7. 同页内移动（diff 表现为一处删除加一处插入，且内容相同）：用原块 JSON 的可写字段（含 color）在新位置重建，然后删除原块。块 ID 会变，记入报告。
8. 执行顺序（按父块分组，按文档顺序）：先做 UPDATE；再做 APPEND，锚点要先于删除使用；最后做 DELETE。
9. 表格：
   - 改单元格：`PATCH table_row.cells`；
   - 增加行：在前一行后面 APPEND table_row；
   - 删除行：DELETE 该行；
   - 列数变化：拒绝（官方说明建表后列数不能改），记为冲突。
10. 媒体块：阶段 2 只写回 caption；阶段 3 允许替换外链 URL，或者用 file_upload 替换文件（≤ 5 MiB）。

### 7.4 执行、预检与回读校验（每页一个“逻辑事务”）

1. **紧邻预检**：开始写这一页之前，`GET /v1/pages/{id}`，要求 `last_edited_time` 等于 base 快照中的值。再对每个受影响的父块 `GET /children`，要求受影响块的 JSON 可写字段与 base 快照一致（比较 `src_hash`）。任何一项不一致都判为 R 已变，转 CONFLICT_BOTH，本页不写。
2. **写前像**：每个操作执行前，先把块的完整 JSON 写入 `wb_journal`（fsync），再发请求；响应也记录下来。
3. **执行**：用写回集成 Token，限速 ≤ 1 次/秒（与轮询共享 3 次/秒的预算，§4.4）；429 和 529 按 `Retry-After` 退避。
4. **回读校验**：本页操作全部完成后，重新拉取本页的块，用导出器渲染，与 L 比较正文和 frontmatter（`notion_last_edited_time` 除外）：
   - 相同：提交。新 base 等于渲染结果；`block_lines` 更新为新块 ID；登记 echo（§10）。如果渲染结果与 L 只差 frontmatter 时间戳，安排一次受 sha 守护的库内 REPLACE（L 的 sha 不变才替换）。
   - 不同：本页标记 `WB_VERIFY_FAILED`。若期间 Notion 没有被他人修改（再次预检通过），按前像**自动补偿**：恢复原 rich_text，把删掉的块 `in_trash:false` 恢复，把新建的块移入回收站。否则只报告，保留前像，由用户决定。
5. **单页和单轮上限**（超过就整页或整轮转入待审批，见 §8.3）：每页 ≤ 50 个块操作；每页改动比例 ≤ 50%；每轮 ≤ 200 个块操作。

### 7.5 新建页面

- **父页面识别**：新文件 `D/新笔记.md` 所在文件夹 `D/` 中有 folder note `D/D.md`，或者同级有 `D.md`，且它带有 `notion_id`：
  - folder note 是 page 时，在该页下新建子页面，`position: page_end`；
  - folder note 是 database 时，在其数据源下新建行，frontmatter 属性按 schema 校验（§7.6）；
  - 在团队协作区根目录下（例如 `General/新笔记.md`）：拒绝，因为内部集成不能建工作区顶层页面，侧栏根对象也由用户维护；
  - 在没有 folder note 的普通新文件夹里：拒绝，提示补一个 folder note，或者移到某个页面的文件夹里。
- **范围开关**：新笔记默认需要 opt-in，在 frontmatter 写 `notion_sync: push` 才推送。阶段 3 可以改为“已映射文件夹内默认推送，`notion_sync: local` 排除”（需用户确认，§14）。
- **创建**：标题取文件名主干（不做反向全角替换）。正文按 §7.3 第 6 步转换，前 100 块随 `POST /v1/pages` 的 `children` 写入，其余用 append 补齐。
- **收尾**：回读并渲染，与 L 的正文比较：
  - 相同：在 sha 守护下用规范化版本（加上 frontmatter `notion_id` 等）原子替换库内文件，先备份到 `backup/vault-before/<run>/`。如果规范路径与当前路径不同（sanitize 结果不同），按 MOVE 处理，并登记 echo。
  - 不同：保留库内文件不动，把规范化版本写到 `Notion_Obsidian/conflicts/<run>/`，并提示。
- 新文件引用的本地附件（`![[x.png]]`）：阶段 2 拒绝；阶段 3 用 file_upload 上传，≤ 5 MiB。

### 7.6 属性、标题、改名与移动

- **行属性**：frontmatter 键按 schema 反映射，处理 `prop_` 前缀和 `键（结束）` 日期终点。
  - 可写：title、number、checkbox、select、status（限已有选项）、multi_select、date（含 end）、url、email、phone_number、relation（wikilink 能在 objects_map 里反解；超过 100 条时拒绝）、rich_text（只限原值是单段无格式文本）。
  - 只读（改了就记冲突）：formula、rollup、created_*、last_edited_*、unique_id、verification、button、people、files。
- **标题**：改 frontmatter `notion_title` 时，`PATCH /pages` 更新标题属性（页面用 `title`，行用 schema 中的 title 属性名）。
- **文件改名**（`notion_title` 未改）：
  - 阶段 2 只报告；
  - 阶段 3 按用户确认的规则执行：新标题 = 新文件名主干。sanitize 对全角字符不做改变，所以下一轮导出的名字会保持稳定。
- **跨文件夹移动**：新父级按 §7.5 的规则识别。
  - page → page：`POST /v1/pages/{id}/move`，带 `parent.page_id`；
  - row → 同一数据库：不需要移动；
  - page ↔ database、数据库 → 任何位置：拒绝（官方说明不支持移动数据库，跨类型移动还会改变属性模型）。
  - 移动后，同级顺序以 Notion 为准，导出器在下一轮重算 `notion_order` 和 sortspec。
- **同级排序**：不写回（API 不支持），以 Notion 为准。

---

## 8. 删除传播

### 8.1 Obsidian 删除 → Notion 移入回收站

1. 检测到 LOCAL_DELETE 后，写入 `pending_deletes`（状态 pending），开始**宽限期**（默认 24 小时）。宽限期内每次扫描都确认文件仍然不存在，并且没有出现带同一 `notion_id` 的文件（排除改名、Sync 延迟、第二台设备误删后又恢复的情况）。文件重新出现就取消删除。
2. 宽限期满，满足以下全部条件才执行 `PATCH /v1/pages/{id} {"in_trash": true}`：
   - Notion 页面自 base 以来未被修改；
   - 不是数据库，也不是 31 个根对象之一；
   - 子树页面数在阈值以内（§8.3）；
   - 当前模式允许（阶段 2 只限白名单，阶段 3 由 `deletion.obsidian_to_notion` 设为 `confirm` 或 `auto` 决定）。
3. 执行前，把页面 JSON、全部块 JSON 和子树清单写入 `wb_journal`，作为前像（不过期）。执行后回查 `in_trash=true`，并在报告和库内 `Notion_Obsidian/同步状态.md` 中列出页面标题、路径和恢复办法。
4. **默认模式为 `confirm`**：宽限期满后生成 `approvals/pending_deletes_<run>.json`，用户在库内 `Notion_Obsidian/待确认删除.md` 中勾选（frontmatter 或复选框），或者在命令行确认后才执行。这一点源于 2026-11-17 第二台设备误删事故：在库内删除不一定是用户本意。
5. **恢复**：页面在 Notion 回收站里保留 30 天。可以在界面中从回收站恢复，也可以用 `notion_restore.py --page <id>`（`in_trash:false`）。下一轮管线会按 ADD 把文件写回库。超过 30 天时，用 `recreate_from_snapshot.py` 从快照库和前像重建页面；会得到新 ID，并重新映射（§11）。

### 8.2 Notion 删除 → 库内移到可恢复区域

1. 某对象在巡检中消失，或者收到 `page.deleted` 事件后，直接 GET 确认 `in_trash=true`，才判定为 REMOTE_DELETE。404 / 403 视为**访问丢失**，不删除，记为异常。对象移出 5 个团队协作区（`page.moved` 且新父级不在范围内）时，判为 OUT_OF_SCOPE，按删除流程处理，但始终需要确认。
2. 库内文件 sha 等于 base（用户没改过）时：移到**库外** `WORK/vault-trash/<run_id>/<原相对路径>`，不自动清理。同时记入 `publish_journal.jsonl`（新操作 `TRASH`），`rollback.py` 可以撤销。Obsidian Sync 会把这次删除同步到其他设备，并在 Sync 的“Deleted files”中保留 12 个月（附件 2 周），作为第二层保障。
3. 用户改过该文件时（CONFLICT_EDIT_DEL）：文件留在库内，不推送，提示“Notion 中已删除，本地有改动”。
4. 有子对象的页面（文件夹）：整个文件夹按同样规则移走。文件夹里有用户本地新建、不属于 Notion 的文件时，保留这些文件，只移走 Notion 来源的文件，并提示。
5. 这一条会替换现行“源端删除只记 tombstone”的规则（SPEC §11），需要写进 SPEC v1.3（§15）。双向模式未启用前，现行规则不变。

### 8.3 熔断阈值（默认值，可在 `config/bidi.json` 调整）

| 熔断 | 默认阈值 | 触发后 |
|---|---|---|
| Obsidian → Notion 移入回收站 | 单轮 > 3 个页面（子树页面一起算），或涉及任何数据库 / 根对象 | 本轮所有删除暂停，生成待审批清单，其他非删除操作照常 |
| Notion → 库移到回收区 | 单轮 > 20 个文件，或超过已发布文件数的 1% | 同上 |
| 单页大改动 | 单页删除 > 30 块，或改动 > 50% 的块 | 本页写回暂停，待审批 |
| 单轮写回总量 | > 200 个块操作 | 超出部分顺延，连续 3 轮超限时报警 |
| 全局异常（**整轮中止，任何方向都不写**） | 库内 md 数比已发布数少 2% 以上；任一团队协作区文件夹消失；Notion 巡检对象数比上次少 2% 以上；Token 所属工作区 ID 不符；库根是符号链接或不存在 | 写 `state/BIDI_STOP`，等人工处理 |

---

## 9. 冲突处理

- **原则**：两边同时改了同一篇笔记（CONFLICT_BOTH）时，**不自动合并，也不选边**。
  - 库内文件保持用户版本不动；
  - Notion 当前版本渲染后写到 `Notion_Obsidian/conflicts/<run_id>/<原路径>`（现有机制）；
  - 本地改动**不推送**；
  - 冲突写入 `bidi.db.conflicts`，并汇总到库内 `Notion_Obsidian/同步状态.md`，列出路径、原因、两边的修改时间和建议操作。
- **解决办法**（任选一种，都能在手机上完成）：
  1. 在笔记 frontmatter 写 `notion_resolve: take-notion`：下一轮把库内版本备份到库外，再用 Notion 版本覆盖。
  2. 写 `notion_resolve: take-obsidian`：下一轮以当前 Notion 内容作为新的 base，重新计算块级差异再写回。只有 R 类改动生效，仍须通过预检和规范化校验。
  3. 手工合并：在 Notion 或 Obsidian 中把两边改成一致（L == R），工具判为 CONVERGED，自动消除冲突。
- **与 Obsidian Sync 的关系**：
  - Obsidian Sync 负责“多台设备 ⇄ 远程库 ⇄ 本机库目录”，本管线只看本机磁盘上的结果，两者是串联的。其他设备的修改必须先由 Sync 落到本机磁盘，本管线才能看到。**所以本机 Obsidian 客户端必须常驻运行。**本设计不运行 obsidian CLI，也不操作界面。
  - 当前 Sync 冲突处理是“自动合并”（diff-match-patch），可能产生重复文本。合并后的结果会被本管线当作本地改动推送到 Notion。**建议把所有设备都改为“创建冲突文件”**（按设备设置），由本管线把冲突副本列为冲突，不推送。改动量熔断（§8.3）也能拦住异常大的合并结果。
  - 本管线写库也走 `tmp → fsync → os.replace`，并在替换前紧邻做 sha 校验（现有发布器逻辑），避免覆盖用户正在编辑的文件。
- **预检之后的竞态**：预检完成到写入之间约有数秒窗口，若有人在 Notion 改同一个块，可能被覆盖。API 没有条件写，这个窗口无法消除。缓解办法有三：预检紧贴写入；写后回读校验；前像可以补偿。这个剩余风险要写入报告并让用户知情。

---

## 10. 防回环

| 场景 | 机制 |
|---|---|
| 本工具写库后，下一轮扫描看到“库内变化” | 发布和写回提交时更新 `published_files.sha256` 和 base；扫描只把 sha ≠ base 的文件视为本地改动（现有机制） |
| 本工具写 Notion 后，下一轮巡检看到“Notion 变化” | 1) `echo` 表登记 (page_id, 写后回读的 `last_edited_time`, 渲染 sha, 写回 bot ID)。2) 巡检发现变化时，若页面 `last_edited_by` 是写回 bot、`last_edited_time` 等于登记值、渲染结果等于 base，判为 ECHO，只更新 base。3) Webhook 事件的 `authors` 只有写回 bot 时直接忽略。4) 无论哪种情况，最终都以“渲染结果 == base”为准，内容相同就不产生任何操作 |
| 写回成功后 Notion 时间戳变化，导出的 frontmatter 随之变化 | 安排一次受 sha 守护的库内 REPLACE（库内 sha 仍等于刚推送的 L 才替换），一轮后收敛；登记 echo，防止它再次被当作本地改动 |
| 两个方向在同一轮操作同一篇笔记 | 三方分类保证一篇笔记一轮只有一个方向的动作 |
| 全局 | 双向同步共用 `state/refresh.lock`；急停文件 `state/BIDI_STOP`；`config/bidi.json` 的 `mode` 可以立刻设为 `off` |

验收要求：每次写回之后，下一轮计划中该页的操作数必须为 0（阶段 1 用模拟写入验证，阶段 2 和 3 用真实写入验证）。

---

## 11. 误删找回与版本回退计划

### 11.1 分层恢复体系

| 层 | 保护对象 | 保留期 | 恢复方式 | 适用场景 |
|---|---|---|---|---|
| L1 Notion 回收站 | 被移入回收站的页面和块 | 30 天，之后 30 天内仅客服可协助（官方） | Notion 侧栏 → 回收站 → 恢复；或 `notion_restore.py --page <id>`（`in_trash:false`；不在团队协作区的顶层页面只能在界面恢复） | Obsidian 删除传播导致的误删；在 Notion 中误删 |
| L2 Notion 页面历史 | 页面内容版本 | **免费 7 天**（Plus 30 天），每 10 分钟 / 停止编辑 2 分钟后各记一版 | 页面 ⋯ → 页面历史 → Restore，或复制块 | 7 天内写回把内容改坏；Notion 中手工误改 |
| L3 写回日志前像 | 本管线每次写 Notion 前的块和页面 JSON | **不过期**（体积很小） | `notion_rollback.py --run <run_id> [--page <id>]`：恢复 rich_text 和属性，恢复被删的块，把新建的块移入回收站，把移动过的页面移回原父级。执行前检查页面在写回后没有被他人修改，否则只出计划 | 写回出错且已超过 7 天；批量撤销某一轮写回 |
| L4 本管线快照库 | Notion 全部对象和块 JSON | 保留最近 14 个，首个全量基线永久保留（现有 prune 规则）；建议把旧快照压缩后增加周、月归档（§11.3） | `recreate_from_snapshot.py --page <id> --snap <db> --parent <id>`：按快照重建页面和子树（新 ID，重新映射） | 回收站超过 30 天；整棵子树丢失 |
| L5 Obsidian Sync | 笔记版本、已删除文件 | **Plus：笔记 12 个月，附件 2 周** | 文件 → 打开版本历史 → Restore；设置 → 同步 → Deleted files → View → Restore（可批量） | 任意设备上误删或误改笔记 |
| L6 File Recovery | 本机 `.md` / `.canvas` 快照 | 默认间隔 5 分钟，默认保留 7 天，只在本机；建议用户把本机的保留期调到 30 天 | 设置 → 核心插件 → 文件恢复 → 查看 | Sync 之外的本机误改；Sync 下载覆盖了本地新建内容 |
| L7 本管线库外备份 | 每次替换前的旧文件；库外回收区；整库 tar | 替换备份和回收区不自动清理；整库 tar 按 §11.3 保留 | `rollback.py --run-id <id> [--apply]`（现有，扩展支持 `TRASH`）；从 `backup/vault-before/<run>/`、`WORK/vault-trash/<run>/` 复制回去；整库从 tar 解到暂存后用发布器带审批写回 | 某一轮发布出错；大面积损坏 |

### 11.2 典型场景的恢复步骤

1. **在 Obsidian 误删了笔记，删除已传播到 Notion**：
   - 先在 Notion 回收站恢复该页面，或用 `notion_restore.py`；
   - 下一轮管线按 ADD 把文件写回库；
   - 如果是在宽限期内发现的，在 `待确认删除.md` 中取消即可，或者用 Sync 的“Deleted files”恢复，文件出现后删除自动取消；
   - 超过 30 天：走 L4 重建。
2. **在 Notion 误删了页面，库内文件已移到回收区**：在 Notion 回收站恢复后，下一轮自动写回库。不想等时，也可以从 `WORK/vault-trash/<run>/` 拷回库里，工具会以 Notion 状态为准重新判定。
3. **写回把 Notion 内容改错**：
   - 7 天内可以用 L2 页面历史；
   - 任何时间都可以用 L3 `notion_rollback.py --run <id> --page <id>`；
   - 回滚后，下一轮把库同步为回滚后的内容。
4. **某轮发布把库改错**：`rollback.py --run-id <id>` 先出计划，确认后加 `--apply`。
5. **大面积异常**（库被清空或被批量改乱，例如同步故障或设备误操作）：
   - 全局熔断会自动停机（§8.3）；
   - 用户确认原因后，从最近的整库 tar 解压到 `WORK/staging/restore-<ts>/vault`，用 `verify_vault.py` 校验；
   - 用发布器的恢复模式在审批后写回（只写缺失或被改动的文件，先备份）；
   - 其他设备上可以用 Sync 的“Deleted files”批量恢复。
6. **第二台设备误删**（已经发生过一次）：宽限期加确认模式让删除不会传播到 Notion。用户确认不是本意后，按 SPEC §13 的现行规则 ADD 写回。

### 11.3 定期备份建议

- **每日整库快照**：每天 03:10（早于现有 03:30 的刷新）打包 `vault_<YYYYMMDD>.tar.gz` 到 `WORK/backup/vault-daily/`，附 `.sha256`。保留 14 个日备份、8 个周备份（每周日）和 6 个月备份（每月 1 日），共约 28 份。库目前 147 MB（附件约 53 MB，已压缩，压缩收益很小），估计每份 80–110 MB，总计约 2.5–3 GB。
- **快照库归档**：现有每个快照约 456 MB，保留 14 个约 6.4 GB。建议把 7 天前的快照 gzip 压缩，周归档保留 8 个。
- **磁盘守护**：`/data120` 剩余 20 GB（已用 83%）。剩余低于 10 GB 时停止生成新快照并报警，**不自动删除备份**。
- **可选**：每月在 Notion 界面手动导出工作区（Markdown & CSV），作为与本管线无关的独立副本（免费套餐是否支持未核实）。

### 11.4 恢复演练

- **每周自动演练**（只读，不碰正式库）：把最近的日备份解压到 `WORK/drill/<date>/`，对它运行 `verify_vault.py`，与 `published_files` 比对文件数和 sha。结果写入 `reports/drill_<date>.json`，失败时报警。
- **每月人工演练**（阶段 2 起，在白名单测试页上做）：
  1. 在 Obsidian 删除测试笔记，等宽限期、确认，Notion 中页面进入回收站；
  2. 用 `notion_restore.py` 恢复，库内文件被写回；
  3. 对测试页做一次错误写回，再用 `notion_rollback.py` 撤销；
  4. 用 `rollback.py` 撤销一轮发布。

  每一步都记录用时和结果。

---

## 12. 分阶段上线

| 阶段 | 内容 | Notion 写入 | 验收标准 | 回退方法 |
|---|---|---|---|---|
| **0 准备**（只读） | 导出器增加块映射和 base 存档；实现反解器、片段复用、规范化校验；**全库往返测试**：对 5,556 篇笔记做 `render(parse(file)) == file`，统计 R/P/X 覆盖率；建立每日整库 tar 和每周演练 | 无 | ① 加映射后导出逐字节不变（5,556/5,556）；② R 类块往返 100% 相等；③ X 类块计数与 §2.1 一致；④ 第一份日备份和第一次演练通过 | 删除新增文件即可，现有管线不受影响 |
| **1 只读演练**（≥ 7 天） | `bidi_sync.py --mode plan` 每 10 分钟运行一次：扫描、分类，计算将要对 Notion 执行的全部操作（`reports/<run>/writeback_plan.json` 和 `.md`），不写 Notion。库内照旧由每日刷新单向更新。用户在 3–5 篇测试笔记上做各种改动（改字、加段、删段、改属性、新建、改名、移动、删除），对照计划检查 | 无 | ① 7 天内对未改动笔记的误报为 0（含 Obsidian 重写 `.base`、Sync 合并、导出器时间戳变化）；② 每个测试改动都生成预期的操作；③ 删除、移动、改名识别正确；④ 模拟写入后下一轮操作数为 0；⑤ 观察并记录 §3.7 中的第 4 项 | 关闭定时器 |
| **2 白名单写回**（≥ 7 天） | 用户新建“写回集成”，**只授权**给 Notion 中新建的测试页（例如 `Godcreate Business / Obsidian 回写测试`）和用户指定的 ≤ 10 个页面；`config/bidi.json` 设 `mode: whitelist`；允许 UPDATE、APPEND、DELETE（R/P 类块）、行属性、白名单内新建页面；**不允许**删除页面和移动页面（只报告）；保持上限：每页 ≤ 50 个块操作、每轮 ≤ 200 个 | 有（限白名单） | ① 所有写回的回读校验相等；② 非白名单页面中，`last_edited_by` 等于写回 bot 的数量为 0（由全量快照审计）；③ 无回环操作；④ 完成一次 `notion_rollback.py` 演练和一次页面回收站恢复演练；⑤ 冲突全部被正确识别，没有覆盖 | `mode: off` 或创建 `BIDI_STOP`；用 `notion_rollback.py` 撤销全部写回；在 Notion 中撤销写回集成的授权 |
| **3 全量** | 写回集成授权扩大到 5 个团队协作区的全部根对象；`mode: full`；启用已映射文件夹内的新建、改名（按用户确认的规则）、页面移动；删除传播按用户选择的 `confirm` 或 `auto`，加宽限期和熔断；热点轮询每 10 分钟，增量巡检每 6 小时，全量每周 | 有 | 连续 14 天：回读校验失败率 < 0.5%，且全部自动补偿或已报告；无未解释的写入；每周演练通过；冲突平均在 48 小时内解决（运营指标） | 同阶段 2；可退回 `whitelist` 或 `plan` |
| **4 Webhook（可选）** | 部署接收端（公网 HTTPS，例如反向代理或隧道），验证签名，把事件写入 `bidi.db.events` 队列，调度器按队列回查。巡检照常保留 | 不变 | 事件到可见更新 P50 ≤ 3 分钟；签名校验失败的请求被丢弃并计数；端点故障时由巡检兜底，不丢更新 | 删除订阅，关闭端点 |

---

## 13. 方案比较、工作量与风险

### 13.1 方案

| 方案 | 描述 | 满足需求 | 工作量 | 风险 |
|---|---|---|---|---|
| **A 完全双向** | 在 B 的基础上，再做附件写回、Webhook、尽量多的 P 类结构（分栏等用自定义标记双向编码），删除默认自动传播 | 1、2、3 最接近原话；4 满足 | 20–25 人日 | 高。标记编码脆弱，自动删除风险大 |
| **B 受控块级回写（推荐）** | 本文 §5–§12 的设计：R/P 类写回，X 类只读并提示；删除默认需确认；轮询实现近实时，Webhook 可选 | 1 完全满足；2 约 94% 的笔记（5,245 篇）正文没有有损元素，可以完整写回，其余 311 篇只有可往返部分能写回；3 近实时、最终一致；4 完全满足 | 14–17 人日，另需 2–3 周演练 | 中，由白名单集成、前像、熔断和回读校验控制 |
| **C 以 Notion 为主 + 本地笔记区回写** | 库内 Notion 区域保持单向只读（本地改动照旧记冲突），另设 `Obsidian 笔记/`（或每个团队协作区下的 `_inbox/`）。该区的笔记**单向推送**到 Notion 中专门的 “Obsidian 回写” 页面，这些页面由 Obsidian 独占，可以安全地按块级差异更新 | 1 满足；2 只覆盖本地区；3 本地区近实时；4 满足 | 4–6 人日 | 低 |
| D 保持现状 + 只做恢复体系 | 只实现 §11（每日 tar、演练、库外回收区） | 只满足 4 | 1–2 人日 | 最低 |

**推荐**：先做 §11 的恢复体系（无论选哪个方案都需要），再按 B 推进阶段 0→1。阶段 1 演练报告出来后，如果用户发现常改的页面大多落在 X 类区域，或者不愿承担剩余竞态风险，可以停在 C，前期工作不浪费：块映射、反解器、扫描器 C 方案同样要用。

### 13.2 工作量明细（方案 B）

| 工作项 | 人日 |
|---|---|
| 导出器块映射、base 存档、逐字节不变回归测试 | 1.5 |
| 反解器（SPEC §5/§5.1 的逆）、片段复用、规范化校验、全库往返测试 | 3.5 |
| 库扫描（pyinotify + 定时）、Notion 热点轮询和数据源时间过滤、三方分类 | 2 |
| 计划器：块级差异、插入、移动、表格、属性、新建、改名和移动 | 3 |
| 执行器：限速、预检、前像日志、回读校验、自动补偿、`notion_rollback.py`、`notion_restore.py` | 2 |
| 删除传播：宽限期、熔断、审批文件、库内状态页、库外回收区、`rollback.py` 扩展 | 1.5 |
| 调度器、systemd 单元、急停、监控、每日 tar 和每周演练 | 1 |
| 阶段 1、2 演练的分析与修正 | 1.5–2.5 |
| **合计** | **约 16（14–17）** |
| 可选：Webhook 接收端（不含公网基础设施） | +2 |
| 可选：附件写回（file_upload） | +1 |

### 13.3 主要风险

| # | 风险 | 可能性 / 影响 | 缓解 |
|---|---|---|---|
| R1 | 反解有损，错误内容写入 Notion | 中 / 高 | 规范化校验（渲染 == 原文）；只写 R/P 类；回读校验；前像补偿；白名单阶段 |
| R2 | 预检到写入之间的竞态，覆盖他人在 Notion 的修改 | 低（单人使用）/ 中 | 紧邻预检；回读校验；前像；报告 |
| R3 | 同步故障或第二台设备误删，被传播成 Notion 删除 | 中（已发生过一次）/ 高 | 宽限期 24 小时；默认需确认；熔断；只移入回收站；L1–L7 恢复 |
| R4 | 免费工作区多成员，API 无法新建块 | 未知 / 高 | 用户确认成员数；出现 403 `restricted_resource` 时立即停写并报告；只保留 UPDATE 和删除 |
| R5 | 限流导致延迟上升，或与每日巡检争用预算 | 中 / 低 | 预算分配（轮询约 1.5 次/秒、写回 ≤ 1 次/秒）；写回积压时推迟巡检 |
| R6 | Obsidian Sync 自动合并产生重复文本，并被推送 | 中 / 中 | 改为“创建冲突文件”；静默期；单页改动比例熔断 |
| R7 | 回环和抖动 | 低 / 中 | echo 表、bot ID、sha；验收要求下一轮操作数为 0 |
| R8 | Notion 页面历史只有 7 天 | 确定 / 中 | L3 前像不过期；L4 快照 |
| R9 | 写权限 Token 泄露 | 低 / 高 | 独立集成，最小授权，chmod 600，不打印、不写日志，泄露时只需撤销该集成 |
| R10 | 磁盘不足（已用 83%） | 中 / 中 | §11.3 的配额与守护；压缩旧快照 |
| R11 | 本机 Obsidian 客户端没运行，其他设备的改动不落盘 | 中 / 中（只影响延迟） | 用户保持常驻；状态页显示本机最后同步时间（可从客户端索引只读获取，或以库目录 mtime 近似） |
| R12 | Notion API 版本演进 | 低 / 中 | 固定 `2026-03-11`，升级前先只读回归 |

---

## 14. 需要用户确认的事项

1. **工作区成员数**（设置 → 成员；访客不算）：只有 1 名成员，还是多名？多名成员的免费工作区不能通过 API 新建任何块或页面（§3.4），方案只能退化为“只改现有块”，或者升级套餐。
2. **是否新建独立的“写回集成”**（推荐），并按阶段授权：阶段 2 只授权给测试页和白名单页，阶段 3 才扩大。能力勾选 Read、Update、Insert content。请勿把写权限加到现有只读集成上。
3. **选择方案**：B（推荐）、C（保守），或者先只做 D（恢复体系）。
4. **删除传播**：
   - Obsidian → Notion：选 `off`（只报告）、`confirm`（默认推荐：宽限 24 小时后在库内勾选确认）或 `auto`（宽限期后自动，受熔断限制）；
   - Notion → 库：是否同意移到库外回收区（推荐）；
   - 熔断阈值是否沿用 §8.3 的默认值。
5. **冲突策略**：同意“不自动合并、保留双方、frontmatter `notion_resolve` 解决”的方式吗？
6. **Obsidian Sync 冲突处理**：是否在**每台设备**上把“自动合并”改为“创建冲突文件”？只能由用户在界面中操作。
7. **本机 Obsidian 客户端常驻**：是否保证 `/data120/Obsidian/Godcreate` 所在的这台机器上，客户端持续运行并登录 Sync？
8. **Webhook**：是否愿意暴露一个公网 HTTPS 端点（反向代理或隧道）？不愿意时，Notion → 库的延迟按轮询计算：热点约 10 分钟，其余 ≤ 6 小时加 45 分钟。
9. **阶段 2 白名单**：在 Notion 新建哪一个测试页（建议放在 `Godcreate Business` 下），另外加入哪些（≤ 10 个）常用页面？
10. **新建笔记的推送规则**：默认需要 `notion_sync: push` 才推送（推荐），还是已映射文件夹内默认推送？
11. **改名语义**：在 Obsidian 改文件名是否等于改 Notion 标题？
12. **轮询频率**：热点 10 分钟、增量巡检 6 小时、全量每周，是否可以？是否保留 03:30 的每日任务（由调度器接管）？
13. **备份配额**：每日 tar 保留 14 日 + 8 周 + 6 月（约 2.5–3 GB），旧快照压缩，可以接受吗？是否考虑扩容 `/data120`？
14. **File Recovery**：是否在本机把保留期从 7 天调到 30 天（界面操作）？
15. **Notion 页面历史**：只有 7 天（免费）。是否考虑升级到 Plus（30 天）？非必须，L3 前像不过期。
16. **SPEC §13 现行规则的变更**：双向模式启用后，“本地删除 → 下一轮写回”改为“本地删除 → 进入待删除队列”（§8.1）；“源端删除只记 tombstone”改为“移到库外回收区”（§8.2）。是否同意（§15）？

---

## 15. 对 SPEC 的修改建议（v1.3 草案，待确认后写入 SPEC）

- §10 状态与幂等：新增 `state/bidi.db`（block_lines、bases、wb_journal、pending_deletes、conflicts、echo、events）和 `state/base/` 内容寻址存档；导出器必须保证“加映射后库文件逐字节不变”。
- §11 发布规则：新增 `TRASH` 操作，把文件移到库外 `WORK/vault-trash/<run>/`，写入日志，`rollback.py` 可以撤销；新增受 sha 守护的 echo REPLACE；新增 `ADOPT`，即新建页面后对本地文件做规范化替换。
- §13 本地删除规则：双向模式下改为 §8.1 的流程；`mode: off` 或 `plan` 时维持现行规则。
- 新增 §16 写回契约：R/P/X 分类表；规范化校验；只发送变化的字段；不删除 `child_page` 和 `child_database` 块；不使用 `erase_content` 和 markdown 写入端点做编辑；单页和单轮上限；熔断；前像日志格式（附录 B）。

---

## 附录 A：`state/bidi.db` 草案 DDL

```sql
CREATE TABLE block_lines(
  notion_id TEXT, base_sha TEXT, block_id TEXT, parent_id TEXT, ord INT, depth INT, type TEXT,
  rt_class TEXT CHECK(rt_class IN ('R','P','X')), line_start INT, line_end INT, own_start INT, own_end INT,
  prefix TEXT, src_hash TEXT, PRIMARY KEY(notion_id, base_sha, block_id));
CREATE TABLE bases(notion_id TEXT PRIMARY KEY, rel_path TEXT, base_sha TEXT, snapshot TEXT,
  notion_last_edited TEXT, updated_run TEXT);
CREATE TABLE wb_journal(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, page_id TEXT, op TEXT,
  target_id TEXT, parent_id TEXT, after_id TEXT, pre_image TEXT, request TEXT, response TEXT,
  phase TEXT, ok INT, ts TEXT);              -- pre_image = 完整块/页面 JSON，永不删除
CREATE TABLE pending_deletes(notion_id TEXT PRIMARY KEY, direction TEXT, rel_path TEXT, first_seen TEXT,
  grace_until TEXT, subtree_pages INT, status TEXT, approval TEXT, decided_ts TEXT);
CREATE TABLE conflicts(id INTEGER PRIMARY KEY AUTOINCREMENT, notion_id TEXT, rel_path TEXT, kind TEXT,
  detail TEXT, run_id TEXT, status TEXT, resolved_run TEXT);
CREATE TABLE echo(page_id TEXT, last_edited TEXT, rendered_sha TEXT, bot_id TEXT, run_id TEXT,
  PRIMARY KEY(page_id, last_edited));
CREATE TABLE events(event_id TEXT PRIMARY KEY, type TEXT, entity_id TEXT, ts TEXT, authors TEXT,
  received TEXT, processed_run TEXT);         -- 阶段 4
```

## 附录 B：写回日志示例（一行一条 JSON，不含正文以外的敏感信息）

```json
{"seq": 101, "run_id": "20261201T1010-B", "page_id": "…", "op": "UPDATE_BLOCK", "target_id": "…",
 "pre_image": {"type": "paragraph", "paragraph": {"rich_text": ["…原片段…"], "color": "default"}},
 "request": {"paragraph": {"rich_text": ["…新片段…"]}}, "phase": "done", "ok": 1, "ts": "…"}
```

## 附录 C：`config/bidi.json` 默认值草案

```json
{
  "mode": "off",
  "notion_version": "2026-03-11",
  "whitelist_roots": [],
  "quiet_seconds": 120,
  "scan_interval_seconds": 600,
  "poll": {"hot_minutes": 10, "incremental_hours": 6, "full_weekday": "Sun", "poll_rps": 1.5},
  "write": {"rps": 1.0, "block_ops_per_page": 50, "block_ops_per_round": 200, "max_changed_ratio_per_page": 0.5},
  "new_pages": {"require_frontmatter_push": true, "allow_attachments": false, "max_attachment_mib": 5},
  "deletion": {"obsidian_to_notion": "confirm", "grace_hours": 24, "max_pages_per_round": 3,
               "notion_to_vault": "vault_trash", "max_files_per_round": 20, "max_ratio": 0.01},
  "global_abort": {"vault_drop_ratio": 0.02, "notion_drop_ratio": 0.02},
  "backup": {"daily": 14, "weekly": 8, "monthly": 6, "min_free_gb": 10}
}
```

---

## 审查意见（对抗式）

- 审查日期：2026-11-17。立场：用户的 Notion 知识库不能因双向同步而丢失或被破坏。本节只追加，不改前文。
- 方法：通读本文与 `docs/SPEC.md`（含 §13 v1.2）；只读核对代码 `tools/refresh.py`、`tools/snapshot.py`、`/data120/Obsidian/notion-migration/nmig/client.py`、`nmig/rest_inventory.py`；只读统计快照 `snapshots/snap_20261117T0417.db`（`mode=ro&immutable=1`）、状态库 `state/export.db`（`mode=ro`），并只读比对库内文件哈希与库设置 `.obsidian/app.json`；联网核对官方文档（URL 见各条）。没有调用 Notion API，没有写库，也没有操作界面。
- 严重度：**S 严重**（按现设计实现会直接造成 Notion 内容被覆盖、回退或删除），**H 高**（恢复不可行、安全机制可被绕过，或设计目标不成立），**M 中**（会导致静默降级、冲突粘滞或误判），**L 低**（事实或表述修正）。

### 结论

设计方向（块级差异、不整页覆盖、独立写回集成、白名单、前像日志、删除需确认）是对的，官方事实大部分核对无误。但有 6 个严重问题会直接伤到 Notion 内容：S1 基于过期 base 的回写会把 Notion 上的新内容改回旧版；S2 删除容器块会连带删除其中的子页面；S3 非幂等写请求被自动重试，会产生重复块和重复页面；S4 两道校验都只比较渲染结果，看不见颜色、锚点和链接类型的损坏；S5 改名写回会截断或改写 4,000 多个页面的标题（文件名与原标题不同的有 4,096 个）；S6 自动补偿本身是无条件写，而且可能形成写入和撤销的循环。**S1–S6 和 H1–H2 处理完之前，不应进入阶段 2（真实写入）。**

### 已核实无误的事实（不再重复列为问题）

request-limits（每个连接 180 / 600 次/分钟，另有工作区共享限额和数据源查询独立限额，429 / 529 和 Retry-After）；workspace-block-limits（多成员免费工作区 1,000 块终身上限，403 `restricted_resource`，删除不恢复额度）；webhooks（“We aim for at-most-once event delivery”、约 1 分钟聚合、重试 8 次、验证后 URL 不可改、HMAC-SHA256、没有 `block.*` 事件）；move-page 没有位置参数，也不能移动数据库；append children 的 `position`、`after` 已弃用、已有块不能移动；markdown 端点默认拒绝删除子页面和数据库，并且“Synced pages cannot be updated”；页面历史 Free 7 / Plus 30 / Business 90 天；回收站 30 天；Obsidian Sync Plus 笔记版本 12 个月、附件“older versions are stored for two weeks”；File Recovery 默认 5 分钟、7 天。

---

### S 严重

**S1 基于过期 base 的本地版本会被当作“本地改动”推送，把 Notion 的新内容改回去**

- 场景 A（离线设备）：第二台设备离线时编辑了笔记 N；在此期间，管线已把 Notion 上的新版本发布到本机，并把 base 更新为这个新版本。该设备上线后：
  - 若按 §9 的建议改为“创建冲突文件”，而冲突恰好在本机被检测到：官方原文是 “The original file keeps the remote version”，冲突副本保存检测到冲突那台设备（本机）的改动（https://obsidian.md/help/sync/troubleshoot ）。于是原文件变成旧设备基于旧 base 的版本；管线刚发布的 Notion 新版本被挪进 `(Conflicted copy …)`，而 §6.1 排除冲突副本。
  - 若保持“自动合并”，diff-match-patch 也可能丢掉一侧的改动。
- 两种情况的分类结果相同：base→L 变，base→R 未变，判为 LOCAL_ONLY。块级回写随即把 Notion 上新增的块删掉，把改过的文字改回旧文。
- 场景 B（恢复操作）：§11.2 推荐的恢复手段（从 Sync 版本历史或 “Deleted files” 恢复、从 File Recovery 恢复、从每日 tar 恢复）都会让一个旧版本以“本地改动”的身份出现，同样被推送，把 Notion 回退到旧状态。
- 场景 C：2026-11-17 的事故正是“第二台设备的非本意改动”。在双向模式下，这类改动会变成对 Notion 的批量回退。§8.3 对“修改”没有硬熔断：单轮 200 个块操作只是“顺延”，连续 3 轮超限才报警，报警时已经写入约 600 个块操作。
- 本机客户端没有运行时（R11），管线仍在发布 Notion 变更；其他设备的改动积压在远端。客户端一启动，就是场景 A 的最大化版本。
- 修正：
  1. 为每篇笔记保存 base 历史链。`state/base/` 已经内容寻址，只需加一张表 `base_history(notion_id, seq, sha, run, origin)`。
  2. 分类时若 L 的改动区段与“最近一次 REMOTE 发布引入的区段”重叠，并且 L 在这些行上等于更早的某个 base，判为 `STALE_REVERT`：不推送，列为冲突，默认按 Notion 版本处理。
  3. 新增“修改量熔断”：单轮出现 LOCAL 改动的笔记数超过 N（例如 20），或大量文件在同一时间窗内由 Sync 落盘时，整轮暂停所有 Notion 写入，等待审批，而不是顺延。
  4. 本机客户端启动后的一段“Sync 稳定期”（例如 15 分钟）内不推送。客户端未运行时，暂停 Notion→库的发布，或至少暂停写回。
  5. §9 的“创建冲突文件”建议必须和第 2 条一起实施；单独实施反而更危险。§11.2 的每个恢复步骤都要先把 `mode` 设为 `off`，恢复完成后以 Notion 为准重新建立 base，再恢复写回。

**S2 删除或重建容器块会连带删除其中的子页面和内联数据库**

- 设计只规定“不删除 child_page / child_database 的链接行”（§3.1、§7.3 第 5 步），没有规定删除或重建它们的**祖先块**时怎么办。类型变化、层级变化（§7.3 第 4 步）和同页移动（第 7 步）都会删除原块，原块的后代随之进入回收站。
- 快照实测：有 8 个 child_page 块嵌在容器块里，分布在 6 页。其中 7 个在分栏（column_list / column）里，属于 X 区，按 §7.2 只读，因此受到保护；另 1 个挂在一个**待办（to_do，R 类）块**下面，没有任何保护。SPEC §3 也明确子页面可以嵌在 toggle、column、synced_block 里，今后同类结构还会增加。
- 后果：用户在 Obsidian 删掉一个待办及其缩进内容，或者把这个待办改成普通段落，或者改了它的缩进，就会触发 `DELETE` 待办块，块下的子页面连同整棵子树进入回收站。
  - §8.3 的熔断按块操作数计，一个 DELETE 不会触发；
  - §8.1 为页面删除设的 24 小时宽限期和确认步骤也被完全绕过。
- 修正：执行器加一条硬规则：DELETE 或重建的目标块，只要其后代（递归，包括 synced_block 的源）中有 child_page、child_database 或内联数据库，就拒绝执行。检查在发请求前用 `GET children` 实时完成，不能只信 `block_lines`。把这条加进阶段 0 的测试和 §15 的写回契约。

**S3 非幂等写请求会被客户端自动重试，产生重复块和重复页面**

- `nmig/client.py` 的 `request()` 对所有方法（包括 `POST /pages`、`PATCH /blocks/{id}/children`、`POST /file_uploads`）都会重试：遇到网络错误或超时（`timeout=90`）、409、500/502/503/504（`RETRY_STATUS = {409, 429, 500, 502, 503, 504}`）时最多重试 8 次。
- 服务器已经执行、只是响应丢失时，重试会再追加一遍段落，或再建一个页面。Notion API 不提供幂等键（patch-block-children 文档没有相关说明）。
- §7.5 的 ADOPT 也有同类问题：如果用户正在编辑，sha 守护会让 ADOPT 失败，`notion_id` 写不回文件；下一轮这篇笔记仍被判为 LOCAL_NEW，于是再建一页，每轮重复一次。
- 修正：
  - 写回专用客户端对非幂等请求不做自动重试，只对 429 / 529（明确未执行）重试。
  - 超时或 5xx 时，先 `GET` 父级子块列表核对写入是否已生效，再决定是否重发。
  - 发请求前先把意图写进 journal 并 fsync。
  - 新页面的映射（路径 → page_id）在 POST 返回后立即写入 `bidi.db`，不依赖库内文件 ADOPT 成功。下一轮先按映射识别，不再判为 LOCAL_NEW。

**S4 两道校验都只比较渲染结果，看不见渲染时被丢弃的属性**

- §7.3 的规范化校验和 §7.4 的回读校验都比较 Markdown 渲染结果，但渲染器会丢掉或混淆几类信息：
  - 颜色；
  - 指向 Notion 页面链接里的 `#blockid` 锚点（快照中有 17 处）；
  - “page 提及”和“指向 Notion 页面的文本链接”的区别（SPEC §5 和 §6 都把两者渲染成 wikilink）；
  - 标题是否 `is_toggleable`。
- 片段复用用最长匹配。同一字符串在块里出现多次时，可能把带颜色或带链接的片段挂到错误位置；改字后，带锚点的链接可能被重建成无锚点链接，文本链接也可能变成 mention。这些情况下两道校验都会通过。
- 阶段 0 的验收 `render(parse(file)) == file` 只测了“文本 → 文本”方向；而真正决定 Notion 会不会被改坏的是“块 JSON → 文本 → 块 JSON”方向。
- 修正：
  - 校验改到 JSON 级：对每个被改的块，比较回读的 `rich_text` JSON 与预期 JSON，包括片段边界、annotations 全部字段（含 color）、`href`、mention 类型和目标；对没碰过的块，要求前后 JSON 完全相同。
  - 阶段 0 增加全库测试：`parse(render(block))` 的可写字段与原块 JSON 相等。R 类按 JSON 往返来定义，不按文本往返。

**S5 改名写回会截断或改写 Notion 标题，标题里的格式也会被抹掉**

- §7.6 阶段 3 规定“新标题 = 新文件名主干”。`state/export.db` 实测，5,556 个对象中：
  - 3,924 个因字符替换或空白折叠，文件名与原标题不同（原因可重叠：含 `/` 的 3,446 个、空白被折叠的 1,112 个、含 `:` 的 270 个，其余为 `* | # ? …` 等；10 个空标题变成 `Untitled` 也计在内）；
  - 130 个被截断到 120 字节，并追加了 `… (<id8>)`；
  - 42 个带去重后缀。
- 用户只要改其中任何一个文件名，哪怕只改一个字，原标题就会被替换成截断后的、带全角替换和 ID 后缀的文本。原来的长标题就此丢失，而免费版页面历史只保留 7 天。
- 快照中还有 524 个标题含粗体等格式、275 个是多片段、33 个含链接。无论是改名还是改 `notion_title`，只要用纯文本 PATCH 标题，这些格式和链接都会被抹掉。
- §7.5 新建页面“不做反向全角替换”，同样会把 `／`、`：` 写进 Notion 标题。
- 修正：
  - 只有同时满足以下条件，才允许改名写回：`name == sanitize(title)`；没有截断、去重或字符替换；原标题是单段无格式文本。其他改名只报告。
  - 写回 `notion_title` 同样要求原标题是单段无格式文本；否则做片段级差异（复用没改的片段），或者拒绝。

**S6 自动补偿本身是无条件写，并且可能形成写入和撤销的循环**

- §7.4 第 4 步规定“再次预检通过”就按前像补偿。但写回之后，`last_edited_time` 已经被自己改了，“再次预检”用什么判据没有定义。
  - 如果改用“`last_edited_by` 是写回 bot”来判断：`last_edited_time` 只精确到分钟，预检又只看受影响的块，同一分钟内用户在其他块上的编辑会被当成“没人改过”，补偿会盖掉它。
- 校验失败更常见的原因是 Notion 自己的规范化：合并相邻的同格式片段、规范化 URL、改写代码语言别名、处理空白。这时每一轮都会重复同一个过程：写入 → 校验失败 → 补偿 → 下一轮仍判 LOCAL_ONLY → 再写入。页面历史被持续污染，限额也被持续消耗。
- 修正：
  - 出现 WB_VERIFY_FAILED 后，该页立即进入 BLOCKED 状态，等人工解除，后续轮次不再自动重试。
  - 补偿前逐块 `GET`，与“我方写入后的 JSON”比较，不一致的块不补偿。
  - 校验只比较本页被触碰的块（JSON 级）。其他差异按 REMOTE 变化处理，不判为写回失败。

---

### H 高

**H1 对“非页面块”的 API 恢复没有经过证实，官方文档自相矛盾**

- delete-a-block 页说可以用 Update a block 恢复；update-a-block 页的错误说明却是 “Returns a 404 HTTP response if the block doesn't exist, is in the trash, or if the connection doesn't have access…”（https://developers.notion.com/reference/update-a-block ）。
- 帮助中心只说明页面会出现在回收站，非页面块没有界面上的恢复入口；官方也没有给出块在回收站里保留多久。
- 如果对块设 `in_trash:false` 不起作用，以下三项都不可行：§7.4 的自动补偿、`notion_rollback.py` 的“恢复被删的块”、§11.1 L1 的“块恢复”。剩下的办法只有按前像重建：块 ID 会变，评论和块链接随之丢失；在多成员免费工作区还会被 1,000 块上限挡住。
- 修正：
  - 阶段 2 之前，在测试页上实测三件事：删除块后能否用 `PATCH in_trash:false` 恢复；恢复后的位置是否不变；子块是否一起恢复。
  - 如果不成立，删除类块操作默认禁用；或者只在页面历史仍在 7 天内且用户确认时才允许。
  - L1 表中“页面和块 … 侧栏 → 回收站 → 恢复”这一说法对块不成立，需要更正。

**H2 L4 快照覆盖不了“回收站超过 30 天”的场景，重建也不等于恢复**

- `tools/refresh.py` 的 prune 规则是 `KEEP_SNAPSHOTS = 14`，只永久保留首个基线 `snap_20261117T0145`。增量快照从根对象开始遍历，进了回收站的页面遍历不到，所以页面被删之后生成的快照里就没有它了。
- 按 §6.2 / §12 改成每 6 小时一次增量巡检后，14 个快照只覆盖约 3.5 天；即使每天一次，也只有约 14 天，都远短于回收站的 30 天。删除 30 天后再去 L4 找，包含删除前内容的快照早已被清理，只剩 11-17 的基线：基线之后新建或改过的内容一概没有。§11.3 的“周归档”只是建议，没有和 prune 代码对齐。
- 用快照重建（新 ID）恢复不了以下内容：
  - 入站的 mention 和 link_to_page；
  - 关系属性：把被删页当作 relation 目标的行会永久失去关联；
  - 评论、页面历史、权限与分享设置、创建者和创建时间；
  - 内联数据库的视图；
  - synced_block 引用；
  - link_preview 块（API 不能创建）、template 块、unsupported 块。
- 附件需要重新上传。当前 `blobs/` 中没有超过 5 MiB 的文件，所以暂时不受免费版单文件上限影响。
- 修正：
  - 页面第一次从快照中消失时，把它最后一份对象和块 JSON，连同引用它的 relation 列表，另存到不参与 prune 的 `state/tombstones/`（体积很小）。
  - L4 的保留期要和“回收站 30 天 + 客服 30 天”对齐。
  - §11.1 写明 L4 只是“降级重建”，并列出上面这些不可恢复项。
  - 超过 30 天时，首选方案写成“联系 Notion 客服”：官方说法是可以恢复“a snapshot of your content in the past 30 days”，这是工作区级快照，不保证逐页恢复。

**H3 Obsidian 内改名会改写大量其他笔记的链接，引发“链接改写风暴”，并让冲突粘住不散**

- 在 Obsidian 里改一个文件名，Obsidian 会自动或在提示后改写所有引用它的 wikilink。本库 `.obsidian/app.json` 为 `{}`，全部是默认设置；改写或新插入的链接格式（默认“尽可能短路径”）可能和导出器的完整路径格式不同。
- 后果：
  - 几十到上百篇笔记同时变成 LOCAL_ONLY；
  - 改名本身还没处理时，新路径在 objects_map 里反解不出来，可能被当成新的文本链接（mention 变成普通链接），也可能解析到另一篇同名笔记；
  - 规范化校验不通过的笔记全部变成 CONFLICT_UNSUPPORTED，此后它们在 Notion 的更新都变成 CONFLICT_BOTH，这部分笔记的 Notion→库同步实际停摆。
- 用户手工插入的 `[[X]]` 这类短路径链接也会遇到同样的问题。
- 修正：
  - 同一轮内先处理 MOVE / RENAME，更新路径映射，再处理其他笔记。
  - wikilink 按 Obsidian 的规则反解（短路径；同名有歧义时拒绝），用 `notion_id` 判断链接目标是否变化。目标没变的链接改写视为无操作：不写 Notion；库内的写法接受，或在下一轮按导出器格式规范化。
  - 阶段 1 加一个测试用例：改名一篇被大量引用的笔记。

**H4 删除宽限期与现行 SPEC §13 的“本地删除 → 下一轮 ADD”规则冲突**

- §8.1 规定，宽限期内“文件重新出现就取消删除”。§15 又说 `mode=off/plan` 时维持现行规则，即每日刷新会把被删的文件按 Notion 版本 ADD 回来。
- 结果是待删除项被取消，删除永远传播不出去；阶段 1 的验收项③“删除识别正确”也就得不出结论。
- 修正：
  - `pending_deletes` 里有某个 `notion_id` 时，发布器跳过它的 ADD（plan 模式也一样）。
  - “取消删除”的判据改为：出现带同一 `notion_id`、并且不是发布器写入的文件。

**H5 只删父页面的 folder note、保留子文件时，会在 Notion 里删掉用户没删的子树**

- §6.1 判定删除只看单个文件是否消失；§8.1 对子树只限制数量（“子树页面数在阈值以内”）。
- 用户只删了 `X/X.md`，`X/` 下的子笔记都还在，或者 Sync 故障只删了这一个文件：管线会把 X 移入回收站，X 的子页面也随之进入回收站。帮助中心说归档父页面会归档其下页面；§3.7 第 1 项也承认这一点没有实测。
- 修正：
  - 页面有子对象时，只有它的全部后代文件也已从库中删除，或者用户明确确认删除整棵子树，才允许 LOCAL_DELETE。
  - 否则判为 `CONFLICT_PARTIAL_DELETE`，并按 Notion 版本恢复 folder note。

**H6 库内审批文件和 frontmatter 指令可能被陈旧设备或同步合并“伪造”**

- 设计用三处库内状态驱动写操作：
  - §8.1：在 `待确认删除.md` 里勾选，确认删除；
  - §9：`notion_resolve: take-obsidian`，强推本地版本；
  - §7.5：`notion_sync: push`，推送新笔记。
- 2026-11-17 的事故就是第二台设备的非本意改动。同样的设备也能带回旧的勾选状态或旧的 `take-obsidian`，Sync 自动合并也可能拼出一个勾选。
- `take-obsidian` 的语义（以当前 Notion 内容为 base 重算差异）实际上就是用 Obsidian 版本覆盖 Notion 中的 R 类内容，会删除 Notion 侧新加的块。
- 修正：
  - 审批项绑定一次性 nonce（`run_id` + `notion_id` + 前像哈希），并设过期时间。删除超过 1 页，或涉及有子页面的页面时，只接受命令行确认。
  - `take-obsidian` 执行前生成预览，列出将在 Notion 中删除或改写的块，要求二次确认，并受 S1 的修改量熔断约束。

**H7 限流加上“单进程、单锁、串行”，延迟目标在很长时间里不成立**

- §5 规定一轮内 ①→⑤ 串行执行，并与 refresh 共用 `state/refresh.lock`。以下任务都在锁内运行，期间写回和热点轮询完全停摆：
  - §6.2 每 6 小时一次增量巡检：实测 34 分钟，写回同时运行时估计 46 分钟；
  - 每周全量：实测 2 小时 14 分。
- 合计每天约 2–3 小时，每周再多 2 个多小时。这些时段里 Obsidian→Notion 的延迟从“2–5 分钟”变成 40 分钟到 2.5 小时。
- 预算也没有真正受控：
  - 官方限额是每个连接 180 次/分钟，另有“工作区所有连接共享”的限额。用户同时在用的其他集成（例如 Notion MCP）也会占用工作区预算。
  - 现有 RateLimiter 是进程内令牌桶，默认 2.5 次/秒。读集成和写集成各起一个实例时，合计可达 5 次/秒；§5 的“轮询 1.5 + 写 1.0”没有跨进程强制。
- 热点轮询 481 次按 1.5 次/秒约需 5.3 分钟，不是表中的 3 分钟。
- 修正：
  - 巡检拆成可中断的小批次（每批几十页），批次之间释放锁，写回优先。
  - 跨进程共享令牌桶（用文件锁或 SQLite 计数实现）；429 和 Retry-After 计入全局退避。
  - 按实测重算延迟表，并在 §0 写明巡检和全量期间的延迟上限。

**H8 Webhook 的“最坏延迟”写错了**

- 官方在写“at-most-once”的同一页还说：“retry delivery up to 8 times … The final retry attempt occurs approximately 24 hours after the initial event trigger”；“Events should be delivered within 5 minutes” 是目标，不是保证（https://developers.notion.com/reference/webhooks-events-delivery ）。
- 所以 §0 第 2、3 条和 §4.4 的“最坏约 6 分钟”“延迟最多 5 分钟”都不成立：最坏情况是约 24 小时后才送达，或者根本不送达。
- webhooks 参考页既没说内部集成能不能订阅，也没说集成自己的写入会不会触发事件。§10 第 3 条依赖 `authors` 过滤，需要实测。
- 修正：
  - 改写延迟表。
  - 事件按 `timestamp` 重排，以回查 API 的结果为准；迟到的事件不得覆盖更晚的状态。

---

### M 中

- **M1 本地 frontmatter 与 Obsidian 属性面板**：用户新增的、Notion 中没有对应项的键（`tags`、追加的 `aliases`、插件写入的 `updated` 等），以及属性面板对 YAML 的重排、改引号、改类型（属性类型按名称在全库统一，见 `.obsidian/types.json`），会让 §7.4 的回读校验永远失败（它比较 frontmatter），或者把被强制转换过的值写回行属性。
  - 修正：frontmatter 按语义比较（解析后比较值，忽略引号和键顺序）；`notion_*` 全部定为只读键，改了就记冲突；不在 schema 里的键定为“本地键”，保留在库内、不写 Notion、校验时排除；行属性写回前按 Notion schema 的类型校验，类型不符就拒绝。
- **M2 预检和前像中的属性截断**：快照已经通过 `retrieve_page_property` 补全了多值属性（`rest_inventory.py` 处理 `has_more`；实测没有超过 25 条的关系）。但 §7.4 的预检用 `GET /v1/pages/{id}`，该响应里 relation、people、rich_text 最多只有 25 项。如果前像也取自这个响应，回滚会把关系截到 25 条；拿它做比较也会误判“R 已变”。
  - 修正：预检和前像对这些属性一律分页取全。
- **M3 锁定页面**：页面对象有 `is_locked`，快照中有 1 页已锁定；设计没有规定怎么处理，写回可能绕过用户在界面里设的锁。
  - 修正：预检发现 `is_locked` 为真时拒绝写回，记为冲突。
- **M4 可写性未核实的提及片段**：涉及 link_mention（231 处）、template_mention 和自定义 emoji 提及。官方 rich-text 页只列出 database、date、link_preview、page、template_mention、user 六种，没有 link_mention，也没说明哪些类型能在请求里提交。§7.3 的“片段原样复用”会把这些片段放进 PATCH，结果可能是 400（安全失败），也可能被 Notion 转成普通链接。后一种是静默降级：渲染结果相同，校验发现不了。前像补偿也有同样的问题。
  - 修正：核实之前，把这些片段归入 X 类，所在块只读；阶段 2 在测试页上逐类实测。
- **M5 插入锚点的歧义**：SPEC §5 中可折叠标题的子块渲染时不缩进。在标题行后插入新段落时，锚点是标题块，`after_block` 会把新块放到标题的整个子树之后，Notion 中的顺序与 Obsidian 不一致，回读失败，进而触发 S6。当前快照中带子块的可折叠标题为 0，但今后可能出现。插入点紧跟分栏最后一行时，“最近结束的块”可能是分栏内部最内层的块，新块会被插进分栏里。
  - 修正：锚点必须与插入行同父级；前一个块属于 X 区或可折叠标题的子树时，锚点改为该区域的最外层块，或者拒绝插入。
- **M6 编号列表的重编号**：SPEC v1.2 输出显式序号，而 Obsidian 的智能列表会自动重编号：插入一项后，后面各项的序号行都会变。如果规范化校验逐块独立渲染，正确的重编号会被判为不一致而拒绝。
  - 修正：以整个列表或整页为单位渲染比较。
- **M7 渲染器升级会让 base 失效**：SPEC 一天之内已从 v1 改到 v1.2，编号列表、callout 和 `.base` 格式的渲染结果都变了。双向启用后再改渲染器，有本地改动的笔记会全部变成 CONFLICT_BOTH；或者渲染差异混进 L 与 base 的差异里，被当作本地改动写回。
  - 修正：`bases` 和 `block_lines` 记录 `renderer_version`；只在没有未推送本地改动时升级渲染器，并对 base 和 L 同时做格式迁移。
- **M8 启用时的初始对账**：从 plan 切到 whitelist / full 时，凡是 sha ≠ base 的文件都会被当成待推送。当前实测：已发布的 5,857 个文件中 3 个不一致（2 个 `.base`、1 个 `.json`），1 个缺失，影响很小；但切换那一刻的状态无法预知。
  - 修正：切换时把已存在的本地差异冻结为“历史差异”，只列出、不推送，需要逐篇确认。
- **M9 免费版块上限对恢复的影响**：§11 没有写这一点。如果是多成员免费工作区，按前像重建、L4 重建、类型变化（先 APPEND 再 DELETE）都会被 403 挡住。官方页还说明这个上限只对 internal connections 和限定工作区的 OAuth 连接生效，不适用于 personal access tokens；§14 第 1 项应补充这一区分，同时评估这种令牌权限更大的风险。
  - 修正：§7.3 明确规定 APPEND 遇到 403 时整页中止，不再执行 DELETE（目前只在 R4 中一笔带过）。
- **M10 父页面进回收站后的子页面状态**：父页面被移入回收站后，单独 GET 子页面时 `in_trash` 可能仍为 false（文档没有说明）。按 §6.2，这会被判为“访问丢失”，结果文件夹一半被移走、一半留下。
  - 修正：删除判据改为“祖先链上任一对象 in_trash”，并在阶段 1 实测。
- **M11 全局熔断按净数量计**：“库内 md 数比已发布数少 2%”用的是净值，本地新建的笔记会抵消被删的数量。
  - 修正：改为统计“已发布 ID 中缺失的个数”。
- **M12 新建页面与 ADOPT 的竞态**：官方说明，两台设备在几分钟内新建同名笔记时，“Sync keeps the remote version without merging”。ADOPT 替换本地文件时如果碰上这种情况，本地版本只能靠 File Recovery 找回。
  - 修正：POST 之前先把原文件备份到库外；ADOPT 前后都检查 sha。

---

### L 低（事实与表述修正）

1. §0 第 3 条“延迟最多 5 分钟”应改为：“官方目标为 5 分钟内送达；可能重试到约 24 小时后，也可能丢失”（同 H8）。
2. §3.4：生效日期 2026-09-08 出自 request-limits 页（“takes effect on September 8, 2026”）；workspace-block-limits 页本身没有给日期，只写了 “The REST API now enforces…”。建议注明出处。
3. §3.5 / §11.1 L1：官方说法是，页面在回收站 30 天后被永久删除，此后再保留 30 天，但“inaccessible to all users, even workspace owners”；客服能恢复的是“a snapshot of your content in the past 30 days”。“之后 30 天内仅客服可协助”与此不完全等同，应改为“可联系客服，按官方说法可恢复过去 30 天内的工作区快照，不保证逐页恢复”。
4. §11.1 L1：“页面和块 … 侧栏 → 回收站 → 恢复”只对页面成立（见 H1）。
5. §3.6：“创建冲突文件”需要 Obsidian 1.9.7 及以上版本；冲突时“原文件保留远程版本，冲突副本是检测到冲突那台设备的改动”。§9 的建议需要按 S1 改写。
6. §4.4：热点轮询按 §5 分配的 1.5 次/秒计算，每轮约 5.3 分钟，不是 3 分钟（同 H7）。
7. §6.3 / §7.4：`last_edited_time` 只精确到分钟。它变了，能证明内容一定变了；它没变，不能证明内容没变。建议照此改写表述，并统一要求做内容比较。
8. §3.1 move page：写回 bot 必须对新父级有编辑权限。阶段 2 白名单之外的移动一定会 403，应在生成计划时就判定，不要等执行时才失败。

### 启用写回前建议补充的验收项

- 阶段 0：
  - 全库 JSON 级往返测试（S4）；
  - 对 S2 的“含子页面容器”做删除拒绝测试；
  - 写回客户端的非幂等重试测试，用模拟超时验证不会产生重复（S3）。
- 阶段 1：
  - 离线设备回放、从版本历史恢复旧版本、改名一篇被大量引用的笔记、只删 folder note，四个场景都应得到 0 个写回操作（S1、H3、H5）；
  - 模拟 Notion 规范化差异，确认页面进入 BLOCKED，不出现循环（S6）。
- 阶段 2 之前，在测试页实测：
  - 删块后用 `in_trash:false` 恢复（H1）；
  - 父页面进回收站后子页面的 `in_trash` 状态（M10）；
  - link_mention 等提及类型的可写性（M4）；
  - 集成自身的写入是否触发 webhook，以及 `authors` 字段的内容（H8）。
