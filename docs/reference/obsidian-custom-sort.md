> **参考资料**｜custom-sort 插件核实记录（Notion 顺序 → Obsidian 文件列表）｜核实日期 2026-11-17，插件版本 3.2.0｜仓库副本，正文未改动（来源：开发沙箱 `notion-obsidian-dev/docs/custom-sort.md`）。导出器生成的 `Notion_Obsidian/sortspec.md` 与 `tools/plugin_bundle.py` 依据本文。返回[文档索引](../README.md)。

# custom-sort 插件核实记录（Notion 顺序 → Obsidian 文件列表）

- 核实日期：**2026-11-17**（本地时区 +07:00）
- 插件：Custom File Explorer sorting（id `custom-sort`，作者 SebastianMC，仓库 <https://github.com/SebastianMC/obsidian-custom-sort>）
- 适用版本：**3.2.0**（截至核实日期的最新稳定版）
- 本文件与 `SPEC.md` §9（排序呈现）配套，只记录**核实结论**和**生成规则建议**，不代表已经安装或启用。

## 0. 状态区分

| 项目 | 状态 | 说明 |
|---|---|---|
| 查询最新版本、manifest、发布资产 | 已执行、已验证 | GitHub API 与 raw 文件，见 §1 |
| 下载 3.2.0 发布资产 | 已执行、已验证 | `WORK/plugins/custom-sort/3.2.0/`；sha256 与 GitHub API 返回的 digest 一致；**未执行其中任何代码** |
| 摘录与核实语法（a–e） | 已执行 | 依据 README、advanced-README、manual、syntax-reference、wildcards、examples，以及 3.2.0 源码和发布版 main.js 的**文本检索**（未运行） |
| 示例 sortspec | 已验证（部分） | YAML 用 PyYAML 6.0.1 解析通过；逐行分类由**自写**的规则复现脚本推演（按源码规则重写，不是插件本身）。**插件实际解析未测试**（禁止运行插件代码和 Obsidian） |
| 安装到正式库、启用、界面确认 | 待处理 | 不在本子任务范围；需要用户在 Obsidian 界面操作（见 §6） |

## 1. 版本、兼容性与发布资产

| 项目 | 结果 | 来源 |
|---|---|---|
| 最新稳定版本 | `3.2.0`，发布于 2026-09-11T07:10:09Z，`prerelease=false` | <https://github.com/SebastianMC/obsidian-custom-sort/releases/tag/3.2.0>；API `releases/latest` |
| 上一版本 | 3.1.6（2025-07-24） | API `releases` |
| 发布资产 | **只有** `main.js`（80959 B）、`manifest.json`（340 B）；**没有** `styles.css`（下载地址返回 HTTP 404） | API `releases/tags/3.2.0` |
| manifest（发布资产） | `"id": "custom-sort"`, `"version": "3.2.0"`, `"minAppVersion": "1.7.2"`, `"isDesktopOnly": false` | 已下载的 `manifest.json` |
| versions.json | `"3.2.0": "1.7.2"` | <https://github.com/SebastianMC/obsidian-custom-sort/blob/6100b0c664988b08460c0b7312f77d98770fe3fa/versions.json> |
| 官方社区插件目录 | 已收录：`{"id": "custom-sort", "repo": "sebastianmc/obsidian-custom-sort"}` | <https://github.com/obsidianmd/obsidian-releases/blob/master/community-plugins.json> |
| 本机 Obsidian 版本 | **1.15.1**：从 `/data/software/Obsidian/opt/Obsidian/resources/{app,obsidian}.asar` 内部 `package.json` 读取（只读读取归档头部，**没有启动客户端**） | 本地文件 |
| 兼容性结论 | 1.15.1 ≥ minAppVersion 1.7.2，**满足声明的最低版本**。插件通过 monkey-patch 修改 File Explorer 内部实现，所以“1.15.1 上能否实际运行”**未核实**（不能启动 Obsidian）。截至核实日期，仓库近期 issue 中没有发现针对 1.15.x 的故障报告 | README L223-224；issues 列表 |

关于 tag 与源码的说明：tag `3.2.0` 指向 `9c768b5`，master HEAD 是 `6100b0c`（“Version bump before release”）。两者之间**只改了** manifest.json、package.json、tsconfig.json、versions.json 和 yarn.lock，`src/` 与 `docs/` 完全相同。本文所有永久链接都固定在 `6100b0c`，下文用 `B` 表示：

`B = https://github.com/SebastianMC/obsidian-custom-sort/blob/6100b0c664988b08460c0b7312f77d98770fe3fa`

下载文件（详见同目录下的 `SOURCES.txt`）：

| 文件 | 字节数 | sha256 |
|---|---|---|
| `/data120/Obsidian/notion-obsidian/plugins/custom-sort/3.2.0/main.js` | 80959 | `089315129555cde9e9085063b6a8553b4179e3a42a5a8dd1aa79cf79fb147b01` |
| `/data120/Obsidian/notion-obsidian/plugins/custom-sort/3.2.0/manifest.json` | 340 | `efeefc37bbe132ff03c2093f1b05db6817b9fb4599b36241a9f1f33c0338ebdb` |

`docs/manual.md` 开头自称“Document is partial, creation in progress”，`docs/syntax-reference.md` 中的 `## target-folder:` 一节也只写了“TBD”。因此，文档没写或写得含糊的地方，本文以 3.2.0 源码作补充核实，并逐条标明“源码”。发布版 `main.js` 已用文本检索确认包含相同逻辑：

- `Wo="sortspec.md"`、`Go="sorting-spec"`
- 默认设置 `Et={additionalSortspecFile:"",indexNoteNameForFolderNotes:"",suspended:!0,…,automaticBookmarksIntegration:!1,…}`
- `checkForRiskyAttrSyntaxError` 中的 `startsWith` 循环

---

## 2. (a) sorting-spec 放在哪里

### 文档原文

> “Create a new note named `sortspec` in the folder for which you want to configure the sorting”
> — B/README.md#L46

> “for clarity: the underlying file name of the note `sortspec` is obviously `sortspec.md`”
> — B/README.md#L77

> “Download the **RAW CONTENT** of sortspec.md … and put it in any folder of your vault, can be the root folder. Ensure the exact file name is `sortspec.md`. That file contains a basic custom sorting specification under the `sorting-spec:` name in the YAML frontmatter.”
> — B/docs/advanced-README.md#L59-L60

> “it has to be placed inside the frontmatter YAML in one or more Obsidian notes of the vault
>   - the YAML key `sorting-spec:` has to be used
>   - because indentation matters, the recommended multiline YAML syntax is `sorting-spec: |`”
> — B/docs/syntax-reference.md#L31-L33

> “the following Obsidian notes are scanned for `sorting-spec:` YAML entries:
>   - `sortspec` notes (that is `sortspec.md` files actually) in all folders of the vault, including the root folder
>   - the so called `folder notes` - by convention these are notes named exactly the same as their parent folder, e.g. `Inbox/Inbox.md`
>     - this works regardless if any 'Folder note' plugin is installed or not in the vault
>   - the single designated note, as configured in plugin settings
> - all found `sections` in all above locations comprise the `sorting specification` for the vault”
> — B/docs/syntax-reference.md#L39-L44

> “A sorting specification for a folder has to reside in a single YAML entry in one of the listed locations.”
> — B/docs/advanced-README.md#L592

### 源码核实（B/src/main.ts#L71-L72、#L139-L170）

- 常量：`SORTSPEC_FILE_NAME = 'sortspec.md'`，`SORTINGSPEC_YAML_KEY = 'sorting-spec'`。
- 插件遍历整个库。只有满足下列任一条件的文件才会读取 frontmatter：
  1. `name === 'sortspec.md'`；
  2. `name === 'sortspec.md.md'`；
  3. `basename === 父文件夹名`（folder note）；
  4. 与设置项 `additionalSortspecFile` 的 basename、name、path 或 `path + '.md'` 相同；
  5. 与 `indexNoteNameForFolderNotes` 的 basename 或 name 相同。
- 值必须是**字符串**才会被解析（`typeof sortingSpecTxt === 'string'`，L154）。

### 结论

| 问题 | 答案 | 依据 |
|---|---|---|
| 必须叫 `sortspec.md` 吗？ | 不是唯一途径，但 `sortspec.md` 是固定被扫描的文件名，任何文件夹都可以，包括库根。这个名字**不能在设置里改**，是常量 | 文档 + 源码 |
| 任意笔记 frontmatter 里的 `sorting-spec` 会被读吗？ | **不会**。只读 sortspec.md、folder note、设置中指定的一条笔记，以及 index note | 文档 L39-44 + 源码 L142-152 |
| 设置“Path or name of additional note(s) containing sorting specification”（`additionalSortspecFile`） | 默认 **`''`（空）**。UI 说明：“A note name or note path to scan (YAML frontmatter) for sorting specification in addition to the `sortspec` notes and Folder Notes. The `.md` filename suffix is optional.” | B/src/settings.ts#L25-L37、#L88-L98；发布版 main.js 检索一致 |
| 设置“Name of index note (Folder Notes support)”（`indexNoteNameForFolderNotes`） | 默认 **`''`** | 同上 |
| 有没有“索引所有笔记”的选项？ | **没有**，3.2.0 设置项中不存在 | settings.ts 全文 |

**文档与源码冲突（已取舍）**：advanced-README 写的是“by default, it is `Inbox/Inbox.md`”（B/docs/advanced-README.md#L585-L587），但 3.2.0 源码和发布版 main.js 中 `additionalSortspecFile` 的默认值都是 `''`。**以源码和发布产物为准**，即默认没有指定笔记。

**对本项目的影响**：

- SPEC 中的 `Notion_Obsidian/sortspec.md` 会被自动扫描，无需改设置。
- 导出器生成的每个 folder note（`X/X.md`）**也会被扫描**。如果某个数据库行属性名恰好是 `sorting-spec`，它会被当成排序配置解析，可能报错，或与主配置的 `target-folder` 重复，导致整份配置失败。见 §8 的建议 S1。

---

## 3. (b) target-folder 的写法

### 文档原文

> “The line `target-folder: /` specifies to which folder apply the sorting rules which follow. The `/` indicates the root folder of the vault in File Explorer”
> — B/docs/advanced-README.md#L113-L115

> “If no additional modifiers follow the `target-folder:`, the remaining part of the line is treated as an exact folder path (leading and trailing spaces are ignored, infix spaces are treated literally as part of the folder path)
> Within the same vault duplicate definitions of same path in `target-folder:` are detected and error is raised in that case, indicating the duplicated path”
> — B/docs/manual.md#L351-L356

> “- `target-folder: My Folder` — this refers to the folder in the root of the vault and only to it
> - `target-folder: Archive/My Folder` — matches the `My Folder` sub-folder in the `Archive` folder (a sub-folder of the root)
> - `target-folder: .` — this refers to path of the folder where the sorting specification resides …
> - `target-folder: ./Some Subfolder` — this refers to path of a sub-folder of the folder where the sorting specification resides …”
> — B/docs/manual.md#L360-L369

> “if the path contains the `/...` or `/*` suffix its meaning is extended to: match the folder and all its immediate (child) subfolders - `/...` suffix; match the folder and all its subfolders at any level … - `/*` suffix”
> — B/docs/manual.md#L462-L465

> “the [target-folder:] is optional for the very first [section] … In that case it is assumed that the [target-folder:] is the parent folder of the note containing the `sorting-spec:` … subsequent lines with [target-folder:] are collected and treated as multi-target-folder specification … occurrence of [target-folder:] line after one or more `sorting-instruction(s)` indicates a beginning of a new [section]”
> — B/docs/syntax-reference.md#L55-L64

> “The specification can contain rules and orders for more than one folder”（Example 8：在同一个 `sorting-spec: |` 中依次写 `target-folder: /`、`target-folder: Projects`、`target-folder: Archive`）
> — B/docs/advanced-README.md#L298-L318

### 结论

| 问题 | 答案 | 依据 |
|---|---|---|
| 库根怎么写 | `target-folder: /` | 文档 L113-115 |
| 多级路径 | 写从库根算起的相对路径，用 `/` 分隔，例如 `target-folder: General/任务数据库`。**不要加前导 `/`，也不要加末尾 `/`**（文档示例都没有）。源码用 `TFolder.path` 精确查表，`'/General'` 匹配不到 `General`——这一点由源码推断 | manual L360-369；源码 B/src/main.ts#L626-L635 |
| 空格 | 中间的空格按字面保留；首尾空格会被忽略 | manual L351-353 |
| 中文、括号、全角符号 | **不需要转义，也不能加引号**。值是该行剩余部分 `trim()` 后的原样字符串（源码 `validateTargetFolderAttrValue`，B/src/custom-sort/sorting-spec-processor.ts#L1541-L1548），没有任何转义或引号机制，加了引号会被当成路径的一部分。YAML 层面，`|` 块标量里的内容按字面保留（PyYAML 已验证含 `：［］（）"'#{}&*!|>%` 的行原样保留） | 源码 + 本地 YAML 测试 |
| 会被特殊解释的路径 | 以 `name:`、`regexp:` 开头的路径；以 `/...`、`/.../`、`/*` 结尾的路径；等于 `.` 或以 `./` 开头的路径。经 SPEC §3 清洗后（`:`、`*` 改为全角，开头的 `.` 改为 `．`），导出文件夹名不会触发这些情况 | manual L334-L344、L460-L488；源码 L729-732、folder-matching-rules.ts L21-26 |
| 一个 sortspec 能写多个 target-folder 吗 | **能**。连续的多行 `target-folder:` 会合并成**同一个**节，共用后面的规则（Example 7）；`target-folder:` 出现在规则行之后，则开始新节（Example 8） | syntax-reference L58-64；advanced-README L280-318 |
| 同一路径写两次 | 报错 `DuplicateSortSpecForSameFolder`。**整份配置解析失败，插件全库挂起**（`suspended = true`） | manual L355-356；源码 L1050-1054、main.ts L173-188 |
| `target-folder:` 能否缩进 | 不能。缩进（相对于块的基准缩进）会报 `TargetFolderNestedSpec` | 源码 L1143-1157 |

**生成器必须遵守**：每个 `target-folder:` 之后**至少要有一行分组规则**（本方案总是带 `%`）。否则下一个 `target-folder:` 会与它合并成同一节，两个文件夹共用一套规则，顺序全乱。

---

## 4. (c) 按“精确名称”逐项排序

### 文档原文

> “The specification here lists items (files and folders) by name in the desired order. Notice, that only a subset of items was listed. Unlisted items go after the specified ones, if the specification doesn't say otherwise”
> — B/docs/advanced-README.md#L138-L141

> “then go all items not matching any of the above or below rules/names/patterns — the special symbol `%` has that meaning … and finally, in the very bottom, the `sortspec.md` file”（示例行：`Focus...` / `Inbox` / `%` / `Archive` / `sortspec`）
> — B/docs/advanced-README.md#L194-L212

> “- `/:files` --> `/:` e.g. `/:files Chapter \.d+ ...` is equivalent to `/: Chapter \.d+ ...`
> - `/:files.` --> `/:.` e.g. `/:files. ... \-D+.md` is equivalent to `/:. ... \-D+.md`
> - `/folders` --> `/` e.g. `/folders Archive...` is equivalent to `/ Archive...`
> - `/folders:files` --> `%` e.g. `/folders:files Chapter...` is equivalent to `% Chapter...`”
> — B/docs/syntax-reference.md#L145-L148

> 维护者回复（issue #250，2026-09-15）：“`/:files` - matches file names (file name extensions are ignored) … `/:files.` - matches file names with extensions … `/folders:files` - matches both file names and folder names (name extensions are ignored)”
> — <https://github.com/SebastianMC/obsidian-custom-sort/issues/250#issuecomment-5677586268>
> 同一 issue 中（2026-09-17）：“For folders there is no notion of folder name extension.”
> — <https://github.com/SebastianMC/obsidian-custom-sort/issues/250#issuecomment-5711495654>

> “The sorting group expressed as `/:files` alone acts as a sorting group 'catch-all-files, which don't match any other sorting rule for the folder'”
> — B/docs/manual.md#L105

> “The evaluation (matching) is done in the order in which the sorting groups are defined in `sorting-spec: |` for the folder.”
> — B/docs/manual.md#L48-L49

### 源码核实

- 匹配用的名字：文件夹用 `entry.name`。文件默认用 `TFile.basename`（不含扩展名）；分组带 `/:files.`、`/:.`、`/folders:files.` 或 `/%.` 时，改用 `entry.name`（含扩展名）。见 B/src/custom-sort/custom-sort.ts#L403、#L415。
- 精确名称的比较是 `nameForMatching === group.exactText`（B/src/custom-sort/custom-sort.ts#L474）：**区分大小写**，也**不做 Unicode 规范化**（NFC 与 NFD 不相等）。
- 分组之间按定义顺序排列（`return itA.groupIdx - itB.groupIdx`，B/src/custom-sort/custom-sort.ts#L336）。所以逐项列出的顺序就是显示顺序。
- 没写 `%` 时，源码会在**末尾**隐式追加一个 catch-all 分组（outsiders，B/src/custom-sort/sorting-spec-processor.ts#L1456-L1461）。
- 没有前缀的普通行，按文件和文件夹两类匹配（第 971-973 行：“treat the line as exact name (of file or folder)”）。

### 结论

| 问题 | 写法 | 依据 |
|---|---|---|
| 文件夹 | `/folders <名称>`（短写 `/ <名称>`），只匹配文件夹 | syntax-reference L147；源码 |
| 笔记（.md） | 推荐 `/:files. <名称>.md`（带扩展名，只匹配文件）。也可以写 `/:files <名称>`（不带 .md，按 basename 匹配，但同名的 `.base` 也会命中） | syntax-reference L145-146；issue #250；源码 L415 |
| 笔记名带不带 `.md` | 不带前缀、`/:files`、`%` 写法**不带** .md（例如 Example 4 用 `sortspec` 指 sortspec.md）；`/:files.` 写法**必须带** .md | 同上 |
| `.base` 文件 | `/:files. <名称>.base` | issue #250 维护者示例 `/:files. ....base`；源码 |
| 同名的文件夹、`.md`、`.base`（SPEC 中数据库文件夹内的 `<name>.md` 与 `<name>.base`；有子页的页面文件夹 `<name>/<name>.md`） | **必须用类型前缀区分**。否则一行裸名 `<name>` 会同时命中三者，它们落入同一分组，组内顺序由默认比较器决定，与 Notion 顺序无关 | 源码 L415、L323-334 |
| 未列出的项放哪里 | 写在 `%` 这一行所在的位置。不写 `%` 时，它们放在最后（隐式 outsiders） | advanced-README L140、L194-212；源码 L1456-1461 |
| `...` | 通配符，不是“未列出项”的标记：`Focus...` 匹配前缀，`...ive` 匹配后缀，`A...s` 同时匹配头尾，单独一行 `...` 匹配全部。“the wildcard expression '...' can be used only once per line” | advanced-README L195、L256-276；wildcards.md L17-31 |
| 让 `_attachments` 排在最后 | 先列出全部子项，再写 `%`，**最后**写 `/folders _attachments`。如果省略 `%`，隐式 outsiders 会追加在 `_attachments` **之后**，`_attachments` 就不是最后一项 | Example 4 模式 + 源码 L1456-1461 |
| 显式分组之间不需要排序指令 | 每个精确名称分组正常只命中 1 项。同组多项时（例如 `%` 内），先用组级指令；没有组级指令就用文件夹级指令；都没有就用插件默认顺序（文档：“the default alphabetical sorting is assumed … treating the folders and notes equally”） | syntax-reference L62-63；源码 L323-334 |

---

## 5. (d) 名称中的特殊序列与注意事项

每一行先 `trim()`，再按以下顺序判断：

1. 是否以 `//` 开头（注释）；
2. 是否属性（`target-folder:`、`::::`、`<`、`>`、`\<`、`\>`、`order-asc:`、`order-desc:`、`sorting:`、`order-asc`、`order-desc`、`asc`、`desc`，第一个空格前的词，不区分大小写）；
3. **“危险属性前缀”检查**：整行小写后，只要以上述任一词**开头**就报错；
4. 分组前缀（`/!`、`/!!`、`/!!!`、`/+`、`/:files.`、`/:.`、`/:files`、`/:`、`/folders`、`/`、`%`、`/folders:files[.]`、`/%[.]`、`--%`、`/--hide:`），前缀可以连续剥离多个；
5. 剩余部分如果含 `...`，按通配符处理；以 `with-metadata:`、`bookmarked:`、`with-icon:` 开头的按特殊分组处理；其余按精确名称处理。

依据：B/src/custom-sort/sorting-spec-processor.ts#L956-L980、#L1097-L1139、#L1192-L1209、#L1211-L1350、#L1674-L1708、#L1749-L1805。

| 序列 | 行为 | 对本项目的结论 |
|---|---|---|
| 行首 `asc`、`desc`、`order-asc`、`order-desc`、`<`、`>`（不区分大小写，**前缀匹配**） | 裸名 `Description…`、`desc…`、`ASCII…`、`Ascend…` 会报 `NoSpaceBetweenAttributeAndValue`，**整份配置失败，插件全库挂起**。第三方 issue #249（<https://github.com/SebastianMC/obsidian-custom-sort/issues/249>）独立报告过此问题：“any name beginning with those three or four characters is affected … The check runs before group parsing, so a `/folders ` or `/:files ` prefix does move the name off line-start and avoids it”。该 issue 于 2026-08-13 以 completed 关闭，**但 3.2.0 发布版 main.js 中这段检查仍然存在**（已检索确认） | **每个名称行都必须带类型前缀**（`/folders `、`/:files. `），不写裸名 |
| `...`（三个半角点） | 任意位置都是通配符：前缀、后缀或头尾匹配。**同一行出现两次会报 `SyntaxErrorInGroupSpec`，整份配置失败**。没有通用转义；源码只对 `./...` 有一个**未写入文档**的“四点转义”（L121-123、L1683-1698）。`....md` 这类写法也会被当作通配符 | 名称含 `...` 时**不要列出**（让它落入 `%`），并在报告中计数；或者在 SPEC 的 sanitize 中把 `...` 改成 `…`（建议 S3，需决策） |
| `/` | 行首 `/` 引导分组前缀、优先级（`/!`）、组合（`/+`）和隐藏（`/--hide:`）；`//` 引导注释。`target-folder` 值中的 `/` 是路径分隔符，末尾 `/...`、`/*` 是通配后缀 | SPEC 已把名称中的 `/` 换成 `／`，名称里不会出现 |
| `%` | 单独的 `%` 表示 outsiders 分组；`% ` 是“文件和文件夹”类型前缀；`--% ` 是隐藏项。**名称为 `%`、以 `% ` 开头或以 `--% ` 开头时，即使加类型前缀也会报 `TooManyGroupTypePrefixes`**（源码 L1272-1311，前缀可连续剥离，第二个类型前缀即报错）。名称中间或末尾的 `%`（例如 `50%`）没有问题 | 这类名称**不列出**（落入 `%`）并计数。`%` 不在 SPEC 的 sanitize 替换表中 |
| 前导或尾随空格 | 行首尾被 `trim()`，无法表达名称的首尾空格，也没有加引号的办法。行首缩进还决定层级：比块基准缩进更深的 `<`、`>` 行属于上一分组。YAML 层面，块标量缩进以第一行为准，**第一行（包括注释）不能比后续行缩进得更深**；缩进不能用 Tab（PyYAML 已验证这两种情况都会报错） | SPEC 的 sanitize 已去掉首尾空格并折叠连续空白，满足要求。生成器统一用 2 个空格缩进，不用 Tab |
| `\d`、`\R+`、`\[0-9]`、`\C`、`\l` 等反斜杠序列 | 内联正则或排序符号；每行最多一个排序符号，多于一个报错 | SPEC 已把 `\` 换成 `＼`，名称里不会出现 |
| 剥离前缀后以 `with-metadata:`、`bookmarked:`、`with-icon:` 开头 | 特殊分组，没有转义（issue #249 也提到） | 需要半角 `:`，SPEC 已换成 `：`，不会出现 |
| `{:%parent-folder-name%:}` | 源码中的宏（macros.ts），**文档未记载** | 需要 `:`，已被清洗；本方案不使用这个宏 |
| 大小写、Unicode 规范化 | 严格 `===` 比较 | sortspec 里的名字必须与写入磁盘的文件名**逐码点相同**（见建议 S4） |
| U+0085、U+2028、U+2029 | PyYAML（YAML 1.1）把 U+0085 当作换行，会破坏块标量（已验证报错）。Obsidian 使用的 YAML 解析器如何处理**未核实** | 建议 S5：sanitize 中把这三个字符替换为空格 |

---

## 6. (e) “启用即生效”需要的设置与界面操作

### 插件设置默认值（B/src/settings.ts#L25-L43，发布版 main.js 已检索一致）

```ts
additionalSortspecFile: '',
indexNoteNameForFolderNotes: '',
suspended: true,  // if false by default, it would be hard to handle the auto-parse after plugin install
statusBarEntryEnabled: true,
notificationsEnabled: true,
mobileNotificationsEnabled: false,
customSortContextSubmenu: true,
automaticBookmarksIntegration: false,
bookmarksContextMenus: false,
bookmarksGroupToConsumeAsOrderingReference: 'sortspec',
delayForInitialApplication: DEFAULT_DELAY   // = 1 * 1000 ms
// On API 1.2.x+ enable the bookmarks integration by default
DEFAULT_SETTING_FOR_1_2_0_UP = { automaticBookmarksIntegration: true, bookmarksContextMenus: true }
```

加载逻辑（B/src/main.ts#L698-L705）：

```ts
const data = await this.loadData() || {}
const isFreshInstall = Object.keys(data).length === 0
this.settings = Object.assign({}, DEFAULT_SETTINGS, data)
if (isFreshInstall) this.settings = Object.assign(this.settings, DEFAULT_SETTING_FOR_1_2_0_UP)
```

启动时的行为（B/src/main.ts#L616-L624、#L758-L790）：布局就绪后等待 `delayForInitialApplication` 毫秒。若 `suspended === false`，则自动执行“启用并解析”；否则什么都不做，等用户点击 ribbon 图标。解析失败时（语法错误、**没有找到任何 spec**、File Explorer 不可用），插件把 `suspended` 置为 `true` 并**写回 data.json**（main.ts L182-188、L320-328）。

修改 sortspec 后**不会自动重新解析**：

> “Remember to click the ribbon button twice each time after sorting specification change. This will suspend and re-enable the custom sorting, plus parse and apply the updated specification”
> — B/docs/advanced-README.md#L84-L85

源码中也只注册了 file-menu、files-menu、rename、delete 四类事件，没有监听 spec 文件的变化（B/src/main.ts#L379-L572）。也可以运行命令 `custom-sort:enable-custom-sorting`（名称为“Enable and apply the custom sorting, (re)parsing the sorting configuration first. Sort-on.”，B/src/main.ts#L600-L606），或者重启 Obsidian。

### 建议的 `.obsidian/plugins/custom-sort/data.json`（无敏感信息）

```json
{
  "additionalSortspecFile": "",
  "indexNoteNameForFolderNotes": "",
  "suspended": false,
  "statusBarEntryEnabled": true,
  "notificationsEnabled": true,
  "mobileNotificationsEnabled": false,
  "customSortContextSubmenu": true,
  "automaticBookmarksIntegration": false,
  "bookmarksContextMenus": false,
  "bookmarksGroupToConsumeAsOrderingReference": "sortspec",
  "delayForInitialApplication": 5000
}
```

- `suspended: false` 是启动后自动生效的**必要条件**（已核实）。
- data.json 非空，因此 `isFreshInstall` 为 false，书签集成保持 `false`，排序完全由 sortspec 决定，不会被书签顺序干扰（已核实）。
- `delayForInitialApplication: 5000`：设置界面的说明是“For large vaults, multi-plugin vaults or on mobile the value might need to be increased … The delay gives Obsidian additional time to sync notes from cloud storages, to populate notes metadata caches”（settings.ts L62-73），界面限制在 0–30 秒。**5000 ms 是否足够未核实**（库约 5.5k 页面，首次同步时 metadata cache 可能还没建好；如果超时没找到 spec，插件会自我挂起，用户需要点两下 ribbon）。
- 插件会在状态切换时自己改写 data.json，所以发布器只能“目标不存在才写”，**不能把 data.json 纳入 SPEC §11 的 sha 比对与替换**（建议 S6）。

### 首次启用社区插件需要用户在界面完成的操作

Obsidian 官方帮助（obsidianmd/obsidian-help @ `cbe447c`，<https://help.obsidian.md/plugin-security>）原文：

> “By default, Obsidian runs in Restricted Mode to prevent third-party code execution. … To turn off Restricted mode: 1. Open **Settings**. 2. In the side menu, select **Community plugins**. 3. Select **Turn on community plugins**.”
> “Installed plugins remain in your vault even if you turn on Restricted mode, but are ignored by Obsidian.”

> “Either select **Enable** directly after installing it, or enable it from the list of Community plugins under **Settings → Community plugins → Installed plugins**.”
> — <https://help.obsidian.md/community-plugins>

插件 README 中手动安装的步骤：

> “3. Copy the downloaded files `main.js` and `manifest.json` over to your vault `VaultFolder/.obsidian/plugins/custom-sort/`. … 4. Reload Obsidian 5. If prompted about Restricted (formerly 'Safe') Mode, you can disable restricted mode and enable the plugin. -Otherwise, go to `Settings` -> `Community plugins`, make sure restricted mode is off and enable the plugin from there.”
> — B/docs/advanced-README.md#L674-L682

据此，用户需要在 Obsidian 界面依次操作（待处理，需用户本人执行）：

1. 设置 → 第三方插件（Community plugins）→ **Turn on community plugins**，即关闭受限模式。
2. 在“已安装插件”中打开 **Custom File Explorer sorting** 的开关。如果 `community-plugins.json` 已包含 `custom-sort`，重载后它可能已经处于启用状态。
3. 如果 ribbon 图标显示未生效、出错或未应用，点击 ribbon 图标两次，或者运行命令“Sort-on”。正常时会提示“Parsing custom sorting specification SUCCEEDED!”，ribbon 图标变为 active（README L57-58、advanced-README L75-80）。
4. 前提：核心插件“文件列表”（Files / File Explorer）必须启用（advanced-README L619-624）。

**未核实**：1.15.1 首次打开含社区插件的库时是否弹出“信任作者”对话框，以及对话框的具体文字（官方帮助未记载，插件文档只写了“If prompted”）；受限模式开关保存在哪个文件。因此**不要**试图通过写文件绕过受限模式。

### `.obsidian/community-plugins.json`

官方帮助页（Community plugins、Plugin security、Configuration folder）**都没有写明**该文件的格式。按惯例，它是“已启用插件 id 的 JSON 数组”，例如 `["custom-sort"]`，但这一点**未核实**。发布器只能**合并**写入（保留已有 id，缺少 `custom-sort` 时追加），不能覆盖。

Sync 相关（官方帮助 Sync settings）：

> “To sync community plugins, manually enable **Active community plugin list** and **Installed community plugin list**.”
> “Community plugins typically do not support hot reloading and require a restart when new settings are applied.”

其他设备是否同步插件，以及 data.json 是否随“Installed community plugin list”一起同步，**未核实**。

---

## 7. 完整示例 sortspec（`Notion_Obsidian/sortspec.md`）

说明：库根的 5 个团队协作区和 `Notion_Obsidian` 用真实名称（SPEC §2）。`General` 及其数据库文件夹内的子项名称是**占位示例**，实际内容由生成器根据状态库生成。各名称刻意包含空格、中文、半角括号、全角符号，以及以 `Description` 和 `ASCII` 开头的危险名称，用来演示规则。

```yaml
---
sorting-spec: |
  // Notion_Obsidian/sortspec.md  (custom-sort 3.2.0)
  // 示例：根目录 + 一个团队协作区 + 一个数据库文件夹；子项名称为占位
  target-folder: /
  /folders General
  /folders Quantitative Investing
  /folders x_to_notion
  /folders Godcreate Business
  /folders Godcreate AI工作空间
  /folders Notion_Obsidian
  %

  target-folder: General
  /folders 项目总览 (2026)
  /:files. Description 草稿.md
  /folders 任务数据库
  /:files. 会议记录：周会［A］.md
  /:files. ASCII 速查（全角）.md
  %
  /folders _attachments

  target-folder: General/任务数据库
  /:files. 任务数据库.md
  /:files. 任务数据库.base
  /folders 任务 A（含子页面）
  /:files. 任务 B.md
  %
---
```

### 预期显示顺序

- **库根**：General → Quantitative Investing → x_to_notion → Godcreate Business → Godcreate AI工作空间 → Notion_Obsidian → 库中其他已有内容（`%`）。
- **General**：`项目总览 (2026)/`（有子页的页面）→ `Description 草稿`（无子页的页面）→ `任务数据库/`（数据库）→ `会议记录：周会［A］` → `ASCII 速查（全角）` → 其他未列出项（`%`）→ `_attachments/`。
- **General/任务数据库**：`任务数据库.md`（数据库说明笔记，同时是 folder note）→ `任务数据库.base` → `任务 A（含子页面）/`（有子页的行）→ `任务 B`（行）→ 其他（`%`）。
  - `.md` 排在 `.base` 之前是本方案的提议，SPEC §3 和 §8 没有规定二者的相对位置，**需要调用方确认**。

### 逐条对应的文档规则

| # | 示例中的写法 | 对应规则 |
|---|---|---|
| R1 | 文件名为 `sortspec.md`，放在 `Notion_Obsidian/`，配置在 frontmatter 的 `sorting-spec: \|` 中 | syntax-reference L31-33、L39-40；advanced-README L59-60 |
| R2 | 第一行以 `target-folder:` 开头，不依赖“默认为 spec 所在文件夹” | syntax-reference L55-57；advanced-README L223-224 |
| R3 | `target-folder: /` 表示库根 | advanced-README L113-115 |
| R4 | `target-folder: General/任务数据库`：从库根算起的路径，中间空格和中文原样，不加引号，无前导或尾随 `/` | manual L351-353、L360-363；源码 L1541-1548 |
| R5 | 一个 sortspec 里有多个节；每个 `target-folder:` 后面都有规则行，因此不会合并 | syntax-reference L58-64；advanced-README Example 8 |
| R6 | 每个文件夹路径只出现一次 | manual L355-356 |
| R7 | `/folders <名称>` 只匹配文件夹 | syntax-reference L147；issue #250 |
| R8 | `/:files. <名称>.md` 和 `/:files. <名称>.base` 按带扩展名的文件名精确匹配 | syntax-reference L146；issue #250 维护者回复；源码 custom-sort.ts L415、L474 |
| R9 | 分组按书写顺序显示 | manual L48-49；advanced-README L138-141；源码 custom-sort.ts L336 |
| R10 | `%` 放在未列出项应出现的位置 | advanced-README L194-212 |
| R11 | `_attachments` 写在 `%` **之后**，所以排在最后 | R10 + 源码 L1456-1461（不写 `%` 时隐式 outsiders 会追加在末尾） |
| R12 | `Description 草稿`、`ASCII 速查…` 带了类型前缀，避开 `asc`、`desc` 前缀误判 | 源码 L1192-1209；issue #249 |
| R13 | 注释行 `//` 与其他行缩进相同（2 个空格） | syntax-reference L74-87 |
| R14 | 空行只用于提高可读性，不表示新节 | syntax-reference L48-49 |
| R15 | 示例中没有 `...`，也没有以 `%` 开头的名称 | advanced-README L275；源码 L1308-1311 |

### 示例的验证情况

- **已验证**：用 PyYAML 6.0.1 解析，`sorting-spec` 为字符串。用自写脚本按源码规则逐行分类（脚本在临时目录，未保存到 WORK）：0 处错误；3 个节；17 行精确名称（类型分别为 `/folders` 和 `/:files.`）；3 行 `%`。
- **未验证**：真实插件的解析和 File Explorer 中的实际显示（需要在 Obsidian 中启用后进行界面抽检）。

---

## 8. 对 SPEC 与生成器的建议

以下均为计划，需要调用方决策，尚未执行。

- **S1（SPEC §4）**：把 `sorting-spec` 加入行属性冲突前缀名单（改为 `prop_sorting-spec`）。原因是 folder note 会被插件扫描。
- **S2（生成器）**：每个文件夹（团队协作区、有子对象的页面、数据库）生成一节。节内顺序为：
  - folder note `/:files. <name>.md`（页面文件夹和数据库文件夹）
  - 数据库文件夹再加 `/:files. <name>.base`
  - 子对象按 `notion_order`：有子对象的写 `/folders <name>`，没有的写 `/:files. <name>.md`
  - `%`
  - 团队协作区一级再加 `/folders _attachments`

  每节必须至少有 1 行规则。名称只能取状态库 `objects_map.name`（与磁盘文件名同源）。
- **S3（SPEC §3 sanitize 或生成器）**：含 `...` 的名称会被当作通配符，出现两次还会导致整份配置失败。二选一：sanitize 时把 `...` 改为 `…`；或者生成器跳过这类名称（落入 `%`）并计数。同样，名称为 `%`、以 `% ` 开头或以 `--% ` 开头的也要跳过并计数。
- **S4（SPEC §3）**：名称在 sanitize 时统一做 NFC 规范化，写入文件系统和写入 sortspec 用同一个字符串。插件按 `===` 严格比较。
- **S5（SPEC §3）**：把 U+0085、U+2028、U+2029 替换为空格，避免破坏 YAML。
- **S6（SPEC §11）**：`.obsidian/plugins/custom-sort/data.json` 只在目标不存在时写入，不做 sha 比对和替换（插件会改写它）。`community-plugins.json` 只做合并追加。`main.js` 和 `manifest.json` 写入前校验 `SOURCES.txt` 中的 sha256。
- **S7（运维）**：每次增量导出更新 sortspec 后，都要在 Obsidian 中点击 ribbon 两次，或执行 Sort-on 命令，否则新顺序不会生效。
- **S8（发布前自检）**：在生成器中加入 lint，复现第 5 节的规则：
  - 每行都有类型前缀；
  - 没有 `...`；
  - 没有重复的 `target-folder`；
  - 每节至少一行规则；
  - YAML 能解析为字符串。

  不通过时不发布 sortspec。只要有一行错误，就会导致插件**全库挂起**。

不采用的替代方案：用 `with-metadata:` 加 `< a-z by-metadata: notion_order` 按 frontmatter 排序（advanced-README L460-538）。原因有三：

- 文档注明 metadata 变化后不会自动刷新；
- folder note 自身的 `notion_order` 是它在父级中的序号，不是它在本文件夹中的位置，放到文件夹首位需要用未写入文档的宏；
- `.base` 和 `_attachments` 没有 frontmatter。

此方案**未实测**。

---

## 9. 未核实事项汇总

1. 3.2.0 在 Obsidian 1.15.1 上的实际运行情况。只确认了版本号满足 minAppVersion；没有启动客户端测试。
2. 示例 sortspec 被真实插件解析后的结果，以及在 File Explorer 中的实际显示顺序。只做了 YAML 解析和按源码规则的脚本推演。
3. 1.15.1 首次启用社区插件时，是否出现“信任作者”对话框及其文字；受限模式状态保存在哪里。
4. `.obsidian/community-plugins.json` 的格式（官方帮助未记载，按惯例是 id 数组）。
5. `delayForInitialApplication` 设为多少才能在约 5.5k 页面的库和 Sync 首次同步时稳定生效。
6. 数千行、上千节的大型 sortspec 在解析和排序时的性能（文档没有说明）。
7. Obsidian 的 frontmatter YAML 解析器如何处理 U+0085、U+2028、U+2029，以及是否做 Unicode 规范化。
8. Obsidian Sync 是否会把 `plugins/custom-sort/data.json` 同步到其他设备，以及插件自我挂起时写回的 `suspended: true` 是否会传播到其他设备。
9. 关闭 Bases 核心插件时，`.base` 文件是否仍然显示在 File Explorer 中（这会影响该分组是否可见，但不影响解析）。
10. issue #249 状态为“completed”，但 3.2.0 中的检查代码没有变化。后续版本是否修复未知。本方案用“全部加类型前缀”规避，不依赖修复。

## 10. 来源清单（均于 2026-11-17 获取）

- 仓库、发布：
  - <https://github.com/SebastianMC/obsidian-custom-sort>
  - <https://github.com/SebastianMC/obsidian-custom-sort/releases/tag/3.2.0>
  - <https://api.github.com/repos/SebastianMC/obsidian-custom-sort/releases>
- 文档（固定在 `6100b0c`）：
  - `B/README.md`
  - `B/docs/advanced-README.md`
  - `B/docs/manual.md`
  - `B/docs/syntax-reference.md`
  - `B/docs/wildcards.md`
  - `B/docs/examples/{quickstart,basic,1,5}/sortspec.md`
- 源码（固定在 `6100b0c`，只作文本阅读）：
  - `B/src/main.ts`
  - `B/src/settings.ts`
  - `B/src/custom-sort/sorting-spec-processor.ts`
  - `B/src/custom-sort/custom-sort.ts`
  - `B/src/custom-sort/macros.ts`
  - `B/src/custom-sort/folder-matching-rules.ts`
- Issues：
  - <https://github.com/SebastianMC/obsidian-custom-sort/issues/249>
  - <https://github.com/SebastianMC/obsidian-custom-sort/issues/250>
- Obsidian 官方帮助（obsidianmd/obsidian-help @ `cbe447c1a46d3f522bdc3b0fd81dfb9df3e7f5bf`）：
  - `en/Extending Obsidian/Community plugins.md`
  - `en/Extending Obsidian/Plugin security.md`
  - `en/Files and folders/Configuration folder.md`
  - `en/Obsidian Sync/Sync settings and selective syncing.md`
- 官方插件目录：<https://github.com/obsidianmd/obsidian-releases/blob/master/community-plugins.json>
