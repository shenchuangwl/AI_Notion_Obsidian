> **参考资料**｜Obsidian Bases（`.base`）语法核实记录｜核实日期 2026-11-17，对象版本 Obsidian 1.15.1｜仓库副本，正文未改动（来源：开发沙箱 `notion-obsidian-dev/docs/bases.md`）。导出器据此为 Notion 数据库生成 `.base` 视图文件。返回[文档索引](../README.md)。

# Obsidian Bases（`.base`）语法核实：供导出器生成数据库视图

- 核实日期：2026-11-17
- 对象版本：Obsidian 1.15.1。版本取自本机 `/data/software/Obsidian/opt/Obsidian/resources/obsidian.asar` 内的 `package.json`（`"version": "1.15.1"`），asar 的 sha256 为 `42bf7f34782752f159d63c17ac6bac4ad01dd55709578f9879f76f8295abb2eb`。
- 本文只是语法核实记录，不代表导出器已经生成或发布了任何 `.base` 文件。
- 核实方法：
  - 【文档】：官方帮助原文。来源是 GitHub `obsidianmd/obsidian-help` 仓库 master 分支，提交 `cbe447c1a46d3f522bdc3b0fd81dfb9df3e7f5bf`（2026-11-16）。原文为英文，摘录时保留原文。
  - 【代码】：只读解包本机 1.15.1 的 `obsidian.asar`，解包到 `WORK/tmp/asar_extract/`，再对 `app.js` 和 `i18n.js` 做 grep 和静态阅读。解包用的是自写的 Python 头解析脚本，没有执行 asar 里的任何代码，也没有启动 Obsidian。
  - 【推断】：根据代码静态推断，没有在界面里实测。
  - 【未核实】：无法确认的内容。
- 本次没有打开或写入任何 Obsidian 库，也没有调用 Notion API 或 obsidian CLI。

## 来源 URL

| 页面 | 官方 URL（help.obsidian.md 会 301 跳到 obsidian.md/help） | GitHub 源文件 |
|---|---|---|
| Bases 简介 | https://help.obsidian.md/bases | `en/Bases/Introduction to Bases.md` |
| Bases 语法 | https://help.obsidian.md/bases/syntax | `en/Bases/Bases syntax.md` |
| 函数 | https://help.obsidian.md/bases/functions | `en/Bases/Functions.md` |
| 视图 | https://help.obsidian.md/bases/views | `en/Bases/Views.md` |
| 表格视图 | https://help.obsidian.md/bases/views/table | `en/Bases/Layouts/Table view.md` |
| 卡片视图 | https://help.obsidian.md/bases/views/cards | `en/Bases/Layouts/Cards view.md` |
| 创建与嵌入 | https://help.obsidian.md/bases/create-base | `en/Bases/Create a base.md` |
| 属性（frontmatter） | https://help.obsidian.md/properties | `en/Editing and formatting/Properties.md` |

GitHub 源文件的完整地址格式为 `https://github.com/obsidianmd/obsidian-help/blob/master/<路径>`。2026-11-17 用 curl 检查过：上表 8 个 help 地址都返回 301，跳到 `https://obsidian.md/help/<同路径>` 后返回 200。

---

## 0. 结论摘要

| # | 问题 | 结论 | 证据 |
|---|---|---|---|
| 1 | `filters` 的 and/or/not | 过滤器可以是一个字符串语句，也可以是对象。对象**只能有一个键**，取 `and`、`or`、`not` 之一，值必须是数组，可以递归嵌套。`not` 的含义是“列表中没有一项为真”。全局过滤器和视图过滤器之间按 AND 合并。 | 【文档】【代码】 |
| 2 | `file.inFolder("路径")` | 支持。参数是库根起算的完整相对路径，做前缀匹配：`file.path.startsWith(folder + "/")`，所以**包含子文件夹**，并且**区分大小写**。参数会先去掉首尾 `/`，再做 NFC 规范化；文件路径本身不做规范化。 | 【文档】【代码】 |
| 3 | 含空格的属性比较 `note["含空格的属性"] == "值"` | 支持。`note` 是对象，`[...]` 中的字符串会走 objectAccess。Obsidian 自己的补全遇到非简单标识符的名称时，也生成 `note["..."]`（经 `JSON.stringify` 转义）。双引号字符串字面量用 `JSON.parse` 解码。 | 【文档】【代码】 |
| 4 | `file.ext` | 支持，例如 `file.ext == "md"`。 | 【文档】【代码】 |
| 5 | 视图 `type`、`name` | 两者**都必填**，且必须是字符串。缺少 `name` 时报错 `Missing or invalid "name" in view N`，缺少 `type` 时报错 `"type" is required in view "X"`。1.15.1 内置的类型有 `table`、`cards`、`list`、`kanban`；`map` 需要 Maps 插件。 | 【代码】 |
| 6 | `order` | 必须是数组，否则报错。数组元素是**属性 ID**，不是表达式。没有 `note.`、`file.`、`formula.` 前缀的名称会自动视为 `note.<名称>`。不写 `order` 时默认只显示 `file.name` 一列。 | 【文档】【代码】 |
| 7 | `sort` | **官方文档没有写 YAML 键名，这一项仅由 1.15.1 代码确认。**写法是 `sort: [{property: <属性ID>, direction: ASC或DESC}]`。旧键 `column` 会被当作 `property` 接受。不合法的条目会被**静默丢弃**。多个条目按顺序作为多级排序键。 | 【代码】 |
| 8 | `limit` | 只接受大于 0 的数字，其他值会被忽略。 | 【文档】【代码】 |
| 9 | `groupBy`、`groupOrder` | `groupBy` 必须同时有 `property` 和 `direction` 两个键，否则报错。没有 `groupBy` 时 `groupOrder` 会被丢弃。 | 【文档】【代码】 |
| 10 | 卡片视图 | `type: cards`。视图级键有 `cardSize`（50–800）、`image`（属性 ID）、`imageFit`（`""` 表示 cover，或 `contain`）、`imageAspectRatio`（0.25–2.5）。 | 【文档】【代码】 |
| 11 | `properties.<name>.displayName` | 支持。**键要写成规范化的属性 ID，例如 `note.客户 状态`、`file.name`。**代码按原键存储、按规范化 ID 查找，因此官方示例中不带前缀的 `status:` 推断不会生效。 | 【代码】【推断】 |
| 12 | 引用 frontmatter | 在表达式中可以写 `note.xxx`、`note["xxx"]`，也可以直接写 `xxx`（默认指 note 属性）。查找时先精确匹配，再**不区分大小写**匹配。 | 【文档】【代码】 |
| 13 | 数值、日期、列表比较 | 数值按数值比较。符合 `YYYY-MM-DD` 或 `YYYY-MM-DDTHH:mm[:ss[.f]][Z/±hh:mm]` 的字符串会被识别为日期，可以与 `today()`、`now()`、`date("...")` 比较，也可以加减时长（如 `"30d"`）。列表可以用 `.contains()`、`.containsAny()`、`.containsAll()`、`.isEmpty()`。`==` 使用宽松相等。 | 【文档】【代码】 |
| 14 | 嵌入 `![[x.base]]` | 支持，默认显示第一个视图。 | 【文档】【代码】 |
| 15 | 嵌入指定视图 `![[x.base#视图名]]` | 支持。`#` 后的文字须与 `name` **完全相等**才能匹配；找不到时显示错误 `View "<名>" not found`，**不会退回第一个视图**。编辑器输入 `[[x.base#` 时会把视图名列为补全候选。 | 【文档】【代码】 |
| 16 | frontmatter 列表和 `"[[path\|alias]]"` 在 Bases 中的显示 | 列表中每个元素单独渲染。整个字符串以 `[[` 开头、以 `]]` 结尾时识别为链接，按最后一个 `\|` 拆出别名，显示别名并且可以点击。目标不存在时带 `is-unresolved` 样式。不含空格的 http(s) 字符串识别为 URL。 | 【文档】【代码】 |

---

## 1. 文件与顶层键

> 原文（Bases syntax）："When you create a base in Obsidian, it is saved as a `.base` file. … Bases must be valid YAML conforming to the schema defined below."
>
> 原文："By default a base includes every file in the vault. There is no `from` or `source` like in SQL or Dataview."

【代码】1.15.1 的顶层解析函数（`qc.parse`）识别以下键：`views`、`filters`、`display`（旧版的显示名，读取时迁移成 displayName）、`properties`、`formulas`、`summaries`、`newItemFolder`、`newItemTemplate`。不认识的顶层键会原样保留。

另外有两点：

- 空文件或只有空白的文件，会得到一个名为 `View` 的默认 table 视图，并且**没有过滤器**，因此会显示整个库的所有文件。导出器绝不能输出没有 filters 的 `.base`。
- Obsidian 保存 `.base` 时用 YAML 库重新序列化（`nullStr:""`、`lineWidth:0`），YAML 注释会丢失。因此生成的 `.base` 里不要放依赖注释的信息。

## 2. filters

> 原文："The `filters` section contains either a single filter statement as a string, or a recursively defined filter object. Filter objects may contain one of `and`, `or`, or `not`. These keys are a heterogeneous list of other filter objects or filter statements in strings."
>
> 原文："These two sections are functionally equivalent and when evaluating for a view they will be concatenated with an `AND`."
>
> 原文（Views）："**None of the following are true** is a `not` statement — results will not be shown if *any* of the conditions in the filter group are met."
>
> 原文（Functions）：
> ```
> file.inFolder(folder: string): boolean
> - Returns true if the file is in the specified folder or one of its sub-folders.
> ```
>
> 原文（Bases syntax）："For example, a filter `file.ext == "md"` will be true for all Markdown files and false otherwise."

【代码】过滤器解析（函数 `r1`）的规则如下：

- 字符串作为表达式解析。
- 非对象报错。
- 对象的键数不等于 1，或者键不是 and/or/not，报错 `"filters" may only have one of an "and", "or", or "not" keys.`。
- 值不是数组，报错 `"and" filter must be an array`（or、not 同理）。
- `not` 的求值是：任意一项为真就返回 false，即“全都不为真”。

【代码】`inFolder` 的实现：

```js
operate(t,e){let i=ye(e.data);return i==="/"?!0:t.file.path.startsWith(i+"/")}
// ye = 合并连续斜杠、去掉首尾斜杠、把  /  换成空格、normalize("NFC")
```

据此，导出器需要注意：

- 文件和文件夹名应当以 **NFC** 形式写盘，否则 `file.path` 与 NFC 化后的参数前缀可能对不上。
- 参数写库根起算的路径，不带开头和结尾的 `/`，例如 `file.inFolder("Godcreate Business/客户 名单")`。
- 匹配包含子文件夹，所以行页面的子页面也会落在范围内，需要靠 `notion_kind` 和 `notion_parent_id` 两个条件排除。

## 3. 属性引用：属性 ID 与表达式是两套写法

> 原文："**Note properties** are properties defined in the note's frontmatter. For example `note.price` or `note["price"]`. If no prefix is specified, the property is assumed to be a `note` property."
>
> 原文："Access object elements using square brackets and the element name or dot notation. For example, `property.subprop` or `property["subprop"]`."
>
> 原文："Note the use of nested quotes is necessary to include text literals in the YAML field. Text literals must be enclosed in single or double quotes."

### 3.1 属性 ID

属性 ID 用于 `order`、`sort[].property`、`groupBy.property`、`properties` 的键，以及卡片视图的 `image`。

【代码】属性 ID 是普通字符串，不经过表达式解析：

```js
function nl(o){return o.startsWith("note.")||o.startsWith("formula.")||o.startsWith("file.")?o:o==="file"?"file.file":`note.${o}`}
function Ct(o){let t=o.indexOf(".");…return e!=="note"&&e!=="formula"&&e!=="file"?{type:"note",name:o}:{type:e,name:o.slice(t+1)}}
```

由此得到以下规则：

- **含空格、中文或点号的名称直接写成 `note.<原名>` 即可**，例如 `note.客户 状态`、`note.v1.2`。不要加引号或方括号。
- 一律带上 `note.` 前缀，可以避免与 `file`、`formula` 等保留前缀产生歧义。例如名为 `file` 的属性，不加前缀会被解析成 `file.file`。
- Obsidian 自己写回文件时，`order`、`sort`、`groupBy` 会去掉 `note.` 前缀（函数 `u3`），`properties` 的键则保持原样。两种写法解析结果相同。

### 3.2 表达式

表达式用于 `filters` 和 `formulas`。

【代码】`note`、`file`、`formula`、`this` 是标识符（不区分大小写）。`note` 返回 frontmatter 对象，`x["..."]` 对对象做字符串下标访问：

```js
if(e instanceof Vi){ … return e.objectAccess(i.data)}       // 数组访问在对象上即按名取属性
function nx(o){ … Jg(t)?`${e}[${JSON.stringify(t)}]`:e==="note"?t:`${e}.${t}` }  // Obsidian 自己生成引用的方式
```

字符串字面量的解码方式：双引号形式直接 `JSON.parse`；单引号形式先把 `\'` 还原，再转成 JSON 解析。

导出器规则：

- **简单 ASCII 标识符**（`^[A-Za-z_][A-Za-z0-9_]*$`，且不是 `this`、`note`、`file`、`formula`、`true`、`false`、`null`）可以写成 `note.notion_kind`。
- **其他名称一律写成 `note[<JSON字符串>]`**，例如 `note["客户 状态"]`、`note["Due Date"]`。
- 中文裸标识符能否被解析，取决于 Lezer 词法表，本次【未核实】，所以不要依赖它。
- 字符串值同样用 JSON 转义：`json.dumps(s, ensure_ascii=False)`。
- 整条表达式由 YAML 序列化器负责加引号。

### 3.3 名称不区分大小写

【代码】`objectAccess` 先找精确的键，找不到时再做不区分大小写的匹配（函数 `wr`）。“相关属性”集合按小写去重，列名使用库级属性注册表（`metadataTypeManager`）中记录的大小写。因此：

- 同一库里只有大小写不同的属性名，例如两个数据库分别有 `Status` 和 `status`，在 Bases 中会合并成一列，排序时也可能匹配不上。
- 生成器应在报告里列出这类冲突。
- 排序只依赖小写 ASCII 的 `notion_order`。

## 4. views

> 原文（Bases syntax）：
> ```yaml
> views:
>   - type: table
>     name: "My table"
>     limit: 10
>     groupBy:
>       property: note.age
>       direction: DESC
>     filters: …
>     order:
>       - file.name
>       - file.ext
>       - note.age
>       - formula.ppu
>       - formula.formatted_price
>     summaries:
>       formula.ppu: Average
> ```
> 原文："`type` selects from the built-in and plugin-added view types. `name` is the display name, and can be used to define the default view."
>
> 原文："As an example, a table view may use this to limit the number of rows or to select which column is used to sort rows and in which direction."（文档只说视图可以保存排序信息，**没有给出 `sort` 的 YAML 结构**）

【代码】视图解析函数（`Vre`，1.15.1）的处理如下，摘录已压缩：

```js
case"order": if(!Array.isArray(i.order)) throw …; a.order=i.order.map(nl); break;
case"sort":  if(Array.isArray(i.sort)){a.sort=[];for(let u of i.sort){u.column&&(u.property=u.column);
             let{property:p,direction:f}=u; p&&String.isString(p)&&(f==="ASC"||f==="DESC")&&a.sort.push({property:nl(p),direction:f})}} break;
case"limit": Number.isNumber(i.limit)&&i.limit>0&&(a.limit=i.limit); break;
case"groupBy": 需同时含 property、direction，direction ∈ {ASC, DESC}
case"groupOrder": 数组；无 groupBy 时置空
case"summaries": 对象，值为字符串
default: a.data[s]=i[s]        // 视图类型自有键：rowHeight、columnSize、cardSize、image …
```

视图写回 YAML 时的格式（`serialize`）为 `{type, name, filters?, groupBy?, groupOrder?, order?, sort: [{property, direction}], limit?, summaries?, …data}`。

各视图类型的自有键，【代码】取自 `getViewOptions`：

| type | 键 | 取值 |
|---|---|---|
| `table` | `rowHeight` | `""`（short）/ `medium` / `tall` / `extra` |
| `table` | `columnSize` | 列宽映射，由界面写入 |
| `cards` | `cardSize` | 滑块 50–800，步长 10 |
| `cards` | `image` | 属性 ID（`file.file` 或 note 属性） |
| `cards` | `imageFit` | `""`（cover）/ `contain` |
| `cards` | `imageAspectRatio` | 0.25–2.5 |
| `kanban` | `columnWidth` 200–500、`image`、`imageFit`、`imageAspectRatio` | 依赖 `groupBy` |
| `list` | 标记样式、缩进属性、分隔符 | 本次未用 |

> 原文（Cards view）："Cards support an optional cover image, which is property that's displayed as an image at the top of the card. The property can be any of the following: A link to a local attachment `"[[link/to/attachment.jpg]]"`; An external link (URL); A hex color code (`#000000`)"

注意：SPEC §4 中 `notion_cover` 的格式是 `attachment:<库内路径>`，**这种值不能直接作为卡片封面**。如果要做卡片封面，需要另加一个属性，值为 `"[[<附件库内路径>]]"` 或外链 URL。是否加这个属性，由 SPEC 决定，本文不擅自扩展。

> 原文（Views）："The first view in your list of views will load by default."

## 5. 值类型与比较

> 原文："Comparison operators can be used to compare numbers, or Date objects. Equal and not equal can be used with any kind of value, not just numbers and dates."
>
> 原文："`file.mtime > now() - "1 week"` returns `true` if the file was modified within the last week."
>
> 原文（Functions）：`list.contains(value: any): boolean`、`list.containsAny(...values)`、`list.containsAll(...values)`、`list.isEmpty()`

【代码】frontmatter 的值按以下顺序转成 Bases 值（`Vi.fromFrontMatter`）：

1. 键名为 `tags` 时，值作为标签列表。
2. 字符串：
   - 以 `[[` 开头且以 `]]` 结尾，作为 **Link**。
   - 不含空格且是合法 URL，作为 **URL**。
   - 匹配 `^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}(:?\d{2})?)?$`，作为**带时间的日期**。
   - 匹配 `^\d{4}-\d{2}-\d{2}$`，作为**日期**（取本地 00:00）。
   - 以上都不符合，作为文本。
3. 其他值按 YAML 原生类型处理：数字、布尔、列表（列表元素递归应用同样规则）、对象、null。

比较规则：

- `==` 和 `!=` 使用 `looseEquals`。
- `>`、`<` 等比较时，如果左边是 Date、右边是字符串，会尝试把右边解析成日期。两边任一为空，结果为空，因此该行不满足条件。

可直接使用的示例：

```
note["金额"] >= 1000                     # YAML 数值
note["Due Date"] <= today() + "30d"      # 字符串 "2026-11-30" 被识别为日期
note.notion_last_edited_time > now() - "7d"
note["标签"].contains("重点")              # 列表
note["关联 项目"].contains(link("Godcreate Business/项目/项目A"))
!note["负责人"].isEmpty()
```

属性类型是**全库共享**的：

> 原文（Properties）："Once a property type is assigned to a property name, all properties with that name across your vault will use the same type."

不同数据库的同名属性如果类型不同（例如一个是数字、一个是文本），Obsidian 的属性面板会提示类型不一致。Bases 的取值仍然按 YAML 原始类型进行（【代码】`fromFrontMatter` 不依赖注册表），这一点【推断】。

## 6. 排序语义

【代码】排序实现（`applySort`）：

- 先按 `file.path` 排序，再用 `sort` 列表做**稳定排序**。因此在 `notion_order` 相同时，按路径排列。
- 只采用“出现在当前结果属性集合中”的排序属性。如果 `sort` 为空或全部无效，退回 `[{property: file.name, direction: ASC}]`。
- 比较规则：
  - 两边都为空，视为相等。
  - **只有一边为空时，空值始终排在最后，不受升降序影响**。
  - 布尔值按真假比较。
  - 两个数字按数值比较。
  - 两个日期按时间比较。
  - 两个时长按毫秒比较。
  - 其他情况按字符串比较。
- `limit` 在排序之后截取。

导出器约定：`notion_order` 必须写成 **YAML 整数**，不能写成字符串，以保证数值排序。表格视图按 `notion_order` 升序即可，与 SPEC §8 一致。

## 7. 嵌入

> 原文（Views）："You can embed base files in any other file using the `![[File.base]]` syntax. The first view in the list will be used. You can change the order by dragging views in the view menu. To specify the default view for an embed use `![[File.base#View]]`."
>
> 原文（Create a base）："Bases can also embedded directly into a note using a `base` code block and the bases syntax."（代码块的语言标记为 `base`）

【代码】1.15.1 的 Bases 插件注册信息：

- `embedRegistry.registerExtension("base", (ctx, file, subpath) => new Sf(…, subpath 去掉 "#" 后作为 viewName))`
- `registerCodeBlockPostProcessor("base", …)`
- 插件定义中 `this.id="bases"`，`this.defaultOn=!0`（默认开启）。

视图名的选取（`getViewConfig(name)`）：

- 没有给名称时取 `views[0]`。
- 给了名称时找 `name ===` 完全相等的视图。
- 找不到时显示 `msgErrorViewNotFound` 错误，文案为 `View "{{ view }}" not found`。

链接补全会把 `.base` 文件中各视图的 `name` 作为 `#` 后的候选项。

导出器注意事项：

- SPEC §5 中 child_database 的写法 `![[<数据库文件夹>/<name>.base]]` 可以直接使用，会显示第一个视图，也就是唯一的 table 视图。
- 如果要嵌入指定视图，写 `![[<路径>.base#全部记录]]`。视图名中不要含 `# | [ ] ^`，以免与链接语法冲突。
- 嵌入的 Base 在用户操作后会通过 `vault.modify` **写回 `.base` 文件**，例如改排序、调列宽。仅仅打开是否会写回，【未核实】。按 SPEC §11，这种写回会被识别为本地编辑冲突，正式库的文件保持不动，这是正确的行为。

## 8. frontmatter 中的列表和 wikilink 字符串在 Bases 中的显示

> 原文（Bases syntax）："Wikilinks in frontmatter properties are automatically recognized as Link objects. Links will render as a clickable link in the view."
>
> 原文（Properties）："Internal links in text properties must be surrounded with quotes." / "When using Internal links in list properties, surround them with quotes."
>
> 原文（File properties 表）："`file.links` | List | List of all internal links in the note, including frontmatter"

【代码】

- Link 的解析：`if(i.startsWith("[[")&&i.endsWith("]]")){let n=i.slice(2,-2),s=n.lastIndexOf("|"); … 别名 = n.substring(s+1)}`。
- Link 的渲染（`renderFileLink`）：
  - 有别名时显示别名，否则显示链接文本。
  - 链接用 `getFirstLinkpathDest(路径, "")` 解析，解析不到时加 `is-unresolved` 样式。
  - 点击时打开链接目标。
- 列表的渲染：外层是 `value-list-container`，每个元素放在单独的 `value-list-element` span 中，并按元素自身的类型渲染（链接、日期、文本等）。

显示效果：

| frontmatter 写法 | Bases 中的类型 | 表格单元格显示 |
|---|---|---|
| `关联: "[[Godcreate Business/项目/项目A\|项目 A]]"` | Link | 可点击的“项目 A” |
| `关联 项目: ["[[a/b\|B]]", "[[c/d\|D]]"]` | 由 Link 组成的列表 | 两个独立可点击的元素“B”和“D” |
| `标签: ["重点", "VIP"]` | 由文本组成的列表 | 两个独立元素 |
| `备注: "参见 [[a/b]]"` | 文本（不以 `[[` 开头） | 原样文本，**不可点击** |
| `notion_url: "https://app.notion.com/p/…"` | URL | 可点击的外链 |
| `Due Date: "2026-11-30"` | Date | 按本地日期格式显示（moment `L`） |
| `notion_created_time: "2026-01-02T03:04:05.000Z"` | 带时间的 Date | 按本地时区显示（`L LT`） |
| `完成: true` | 布尔 | 复选框 |
| 键不存在或值为 `null` | 空值 | 空白，排序时排在最后 |

导出器约定：

- Notion 的 relation 属性写成**列表**，每个元素是**整串**的 `"[[<库内路径不含.md>|<标题>]]"`。显示文字中的 `|`、`]` 换成全角，这与 SPEC §6 一致。
- 范围外的关联写成 Notion URL 字符串，Bases 中显示为可点击的外链。
- 路径一律从库根写起，这样不依赖“最短路径”解析。

## 9. 导出器生成规则（对应 SPEC §8）

1. 用 YAML 序列化器输出。Python 写法为 `yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=10**6)`，不要手工拼接。
2. 全局 `filters.and` 的三条语句按以下顺序排列：
   - `file.inFolder(<JSON 路径>)`
   - `note.notion_kind == "row"`
   - `note.notion_parent_id == <JSON 数据库ID>`
   - 可以另加 `file.ext == "md"`，但这一条是冗余的。
3. `properties`：
   - 键用规范化 ID：`file.name`，以及各属性的 `note.<原名>`。
   - `file.name` 的 displayName 取 Notion 标题属性名。
   - 其他属性的 displayName 取原属性名。
4. `views`：只生成一个 `type: table`，name 取固定值，例如 `全部记录`。
   - `order` 依次为 `file.name`、各属性的 `note.<原名>`（按 Notion 属性顺序）、`note.notion_order`。
   - `sort` 为 `[{property: note.notion_order, direction: ASC}]`。
   - 不写 `limit`。
5. `file.name` 对 `.md` 文件显示的是不带扩展名的文件名（【代码】`getShortName`），也就是清洗后的名称。如果要显示 Notion 原标题，可以在 order 中加 `note.notion_title`。
6. 风险：用户在 Bases 中点击 **New** 新建条目时，Obsidian 会根据过滤器中的 `==` 条件，把 `notion_kind: row`、`notion_parent_id: <ID>` 写进新笔记，并依据 `inFolder` 选定存放的文件夹（【代码】函数 `gf`）。这类笔记**没有 `notion_id`**。校验器和发布器应把它们识别为“Obsidian 本地新建行”，只记录，不覆盖、不删除。
7. 文件夹改名后，`inFolder` 的路径不会自动更新。代码中没有找到相关逻辑，属【推断】。`notion_parent_id` 这一条才是真正的归属键，`inFolder` 只是防护。改名以后视图可能变空，但不会混入其他数据库的行。

参考实现片段，已在本机用 PyYAML 6.0.1 往返验证：

```python
import json, re, yaml
SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED = {"this", "note", "file", "formula", "true", "false", "null"}

def prop_id(name):   # order / sort / groupBy / properties 的键：不是表达式
    return "note." + name

def note_ref(name):  # filters / formulas 表达式
    if SAFE_IDENT.match(name) and name not in RESERVED:
        return "note." + name
    return "note[" + json.dumps(name, ensure_ascii=False) + "]"

def lit(s):
    return json.dumps(s, ensure_ascii=False)

def build_base(db_folder, db_id, title_prop, props):
    cols = ["file.name"] + [prop_id(p) for p in props] + [prop_id("notion_order")]
    doc = {
        "filters": {"and": [
            f"file.inFolder({lit(db_folder)})",
            f"{note_ref('notion_kind')} == {lit('row')}",
            f"{note_ref('notion_parent_id')} == {lit(db_id)}",
        ]},
        "properties": {"file.name": {"displayName": title_prop},
                       **{prop_id(p): {"displayName": p} for p in props}},
        "views": [{"type": "table", "name": "全部记录", "order": cols,
                   "sort": [{"property": prop_id("notion_order"), "direction": "ASC"}]}],
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=1_000_000)
```

## 10. 完整示例 `.base`（对应 SPEC §8）

场景：数据库文件夹为 `Godcreate Business/客户 名单`（示例路径），数据库 ID 为 `2f1e0c3a-5b6d-4e7f-8a9b-0c1d2e3f4a5b`（**占位值**，实际使用时替换为真实数据库 ID）。只显示该文件夹内 `notion_kind == "row"` 且 `notion_parent_id` 等于该 ID 的笔记，按 `notion_order` 升序排列。

```yaml
filters:
  and:
    - 'file.inFolder("Godcreate Business/客户 名单")'
    - 'file.ext == "md"'
    - 'note.notion_kind == "row"'
    - 'note.notion_parent_id == "2f1e0c3a-5b6d-4e7f-8a9b-0c1d2e3f4a5b"'
properties:
  file.name:
    displayName: "客户名称"
  note.客户 状态:
    displayName: "客户 状态"
  note.Due Date:
    displayName: "截止日期"
  note.金额:
    displayName: "金额"
  note.标签:
    displayName: "标签"
  note.关联 项目:
    displayName: "关联 项目"
  note.notion_order:
    displayName: "Notion 顺序"
views:
  - type: table
    name: "全部记录"
    order:
      - file.name
      - note.客户 状态
      - note.Due Date
      - note.金额
      - note.标签
      - note.关联 项目
      - note.notion_order
    sort:
      - property: note.notion_order
        direction: ASC
```

对应的一条行笔记的 frontmatter（示意）：

```yaml
---
notion_id: "9a8b7c6d-0000-4000-8000-000000000001"
notion_kind: "row"
notion_parent_id: "2f1e0c3a-5b6d-4e7f-8a9b-0c1d2e3f4a5b"
notion_order: 3
客户 状态: "进行中"
Due Date: "2026-11-30"
金额: 12000
标签:
  - "重点"
关联 项目:
  - "[[Godcreate Business/项目/项目A|项目 A]]"
---
```

验证结果（2026-11-17）：

- `yaml.safe_load` 解析通过（PyYAML 6.0.1）。
- 按 1.15.1 解析规则写的静态校验器全部通过，校验内容包括：filters 只有一个键且值为数组；视图有 name 和 type；sort 条目是 `property` 加 `ASC/DESC`；`order` 是数组；表达式中的双引号字面量都能用 JSON 解码；括号配对。
- 生成器片段在路径含 `"`、`:`、`#`、空格时，YAML 往返结果一致。
- **没有在 Obsidian 界面实测渲染。**

### 扩展示例：视图级过滤、多级排序、卡片视图（可选，SPEC 未要求）

```yaml
filters:
  and:
    - 'file.inFolder("Godcreate Business/客户 名单")'
    - 'note.notion_kind == "row"'
    - 'note.notion_parent_id == "2f1e0c3a-5b6d-4e7f-8a9b-0c1d2e3f4a5b"'
properties:
  note.客户 状态:
    displayName: "客户 状态"
views:
  - type: table
    name: "进行中（金额≥1000，30 天内到期）"
    filters:
      and:
        - 'note["客户 状态"] == "进行中"'
        - 'note["金额"] >= 1000'
        - 'note["Due Date"] <= today() + "30d"'
        - 'note["标签"].contains("重点")'
        - 'note["关联 项目"].contains(link("Godcreate Business/项目/项目A"))'
        - not:
            - 'note["负责人"].isEmpty()'
    order: [file.name, note.客户 状态, note.金额, note.Due Date]
    sort:
      - property: note.Due Date
        direction: ASC
      - property: note.notion_order
        direction: ASC
    limit: 50
  - type: cards
    name: "卡片"
    order: [file.name, note.客户 状态]
    sort:
      - property: note.notion_order
        direction: ASC
    image: note.封面
    imageFit: contain
    cardSize: 200
    imageAspectRatio: 0.75
```

这个扩展示例同样通过了 YAML 解析和静态校验。其中 `note.封面` 需要是 `"[[附件路径]]"` 或 URL 才能显示为图片，见第 4 节。

## 11. 与官方文档的出入

| 项目 | 官方文档 | 1.15.1 代码 | 采用 |
|---|---|---|---|
| `sort` 的 YAML 结构 | 没有给出 | `sort: [{property, direction: ASC/DESC}]`，兼容旧键 `column` | 以代码为准 |
| `properties` 的键 | 示例写 `status:`（无前缀） | 按原键存储、按 `note.status` 查找，推断无前缀的写法不生效 | 一律写 `note.<名称>` |
| `file.inFolder` 的参数 | “folder name” | 库根起算的完整路径前缀，区分大小写，包含子文件夹 | 写完整路径 |
| `file.name` | “File name” | `.md` 文件返回不带扩展名的名称（`file.basename` 同理，另有 `file.fullname`） | 不影响 |
| 嵌入时视图名不存在 | 未说明 | 显示 `View "X" not found`，不回退到第一个视图 | 视图名保持稳定 |

## 12. 未核实事项

1. **界面渲染没有实测。**本任务禁止启动或操作 Obsidian 客户端，也禁止打开库，所以以上结论都来自文档和静态代码阅读。建议在发布后，对 1–2 个数据库人工抽检：列名显示、行数、按 `notion_order` 的排序、嵌入效果。
2. **不带前缀的 `properties.status.displayName` 是否生效**，属静态推断（第 11 节）。导出器采用带前缀写法，结果不受这一点影响。
3. **中文或其他非 ASCII 的裸标识符**（例如直接写 `客户状态 == "x"`）能否被 Lezer 词法接受，没有核实。导出器统一使用 `note["..."]` 来规避。
4. **仅打开 `.base` 或嵌入视图，是否会触发写回文件**，没有核实。代码只确认用户修改视图配置后会保存。
5. **目标库 `.obsidian/core-plugins.json` 中 Bases 是否开启**，没有核实（禁止读库）。代码确认插件默认开启（`defaultOn=!0`），但如果库配置里显式关闭了它，`.base` 将无法渲染。应在发布阶段只读确认。
6. **Obsidian 改名文件夹时，是否会同步改写 `.base` 中 `inFolder` 的路径**：代码中没有找到相关逻辑，推断不会，未实测。
7. **大量 `.base` 并存时的性能**：每个 Base 都要扫描全库，本库约 5.5k 页，没有测量。
8. 同时存在 NFD 和 NFC 文件名时，`inFolder` 的表现只是由代码推断（参数做 NFC 规范化，路径不做），没有实测。导出器统一写 NFC 即可规避。

## 附：本次核实的工件位置

- 解包目录：`/data120/Obsidian/notion-obsidian/tmp/asar_extract/`。这是来自 `obsidian.asar` 的只读副本，属于不可信数据，不要执行其中任何文件；本文结论核对完后可以删除。
- 官方帮助源文、静态校验器和生成器测试脚本都放在会话 scratchpad 中，属于临时文件，不在 WORK 内长期保存。可复用的规则和代码已经写在本文第 9、10 节。
