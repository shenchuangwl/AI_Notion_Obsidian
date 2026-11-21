# Godcreate 的 Notion ↔ Obsidian 同步操作手册

> **用户侧操作手册（仓库副本）**：本文复制自生产机上的官方手册 `/data120/Obsidian/notion_obsidian+obsidian_notion 同步相通执行文档.md`，面向工作区所有者（文中的“你 / 本人”）和维护者，覆盖日常使用、写回审批、冲突处理、常见问题与维护。正文保持原样，仅为在 GitHub 上导航增加了本目录和 `sec-*` 锚点。
>
> **仓库编者注**
>
> - 文中命令按生产部署编写：工作目录 `/data120/Obsidian/notion-obsidian`，库 `/data120/Obsidian/Godcreate`。在其他机器部署请先阅读[部署指南](../deployment/deployment-guide.md)。
> - 文中出现的页面 ID、计划编号和轮次编号是 2026-11-21 首次部署时的真实记录，仅作示例。你的环境请换成自己的值。
> - 第 2 节查看配置键的 `grep` 示例里写的是 `database_whitelist_ids`，代码中的实际键名是 `whitelist_database_ids`（见 `tools/wb_config.py`），所以这一键不会被那条 `grep` 匹配到。各键的含义见[部署指南 · 写回配置](../deployment/deployment-guide.md#writeback-config)。
> - 正文沿用首次部署时的内部称呼：“督导”指负责部署验收与放行的项目监督人（在你的环境中对应维护者 / 负责人）；“D5.1 / scheduled 签收”指部署后对首个实际定时轮次的跟踪与验收检查（见[开发者指南 §6.2](../development/developer-guide.md#dev-runbook) 的 D5）；RID 即轮次编号 run_id；“部署单元”指运行部署脚本的 systemd 临时单元；`s6_user_apply` 是首次部署时本人写回所用的一次性脚本，未收入本仓库，排查时可从 `pgrep` 模式中省略；末尾变更记录中的 S6.3、AS-xx、CU-xx 与送审核验清单编号为内部审查记录，也未收入本仓库。
> - 汇总版的运维与排障说明见 [运维指南](../operations/operations-guide.md) 和 [故障排查](../troubleshooting/troubleshooting.md)。返回[文档索引](../README.md)。

**目录**

- [1 环境与前置条件](#sec-1)
- [2 初始化与配置](#sec-2)
- [3 Notion → Obsidian 自动同步](#sec-3)
- [4 Obsidian → Notion 写回预览与审批](#sec-4)
  - [4.1 预览：看本地改动会写到 Notion 哪里](#sec-4-1)
  - [4.2 批准并写入一处改动（只能你本人操作）](#sec-4-2)
  - [4.3 拒绝、作废或处理中断](#sec-4-3)
  - [4.4 写后核对：你看结果，维护者完成验收](#sec-4-4)
- [5 冲突处理](#sec-5)
  - [5.1 对照两份内容，保留需要的版本](#sec-5-1)
  - [5.2 在 Obsidian 删除一篇笔记会怎样](#sec-5-2)
  - [5.3 写回中断、返回 503 或结果未知](#sec-5-3)
  - [5.4 急停，以及本人回退已写入 Notion 的内容](#sec-5-4)
- [6 常见问题](#sec-6)
  - [6.1 页面找不到、可执行项为零或命令被拒绝](#sec-6-1)
  - [6.2 批量恢复本地笔记前](#sec-6-2)
  - [6.3 目录冲突、同时保存冲突和写回提示](#sec-6-3)
  - [6.4 已知限制](#sec-6-4)
- [7 维护与后续启用范围](#sec-7)
  - [7.1 每天看什么，出事去哪找证据](#sec-7-1)
  - [7.2 维护者参考：三类恢复与升级窗口](#sec-7-2)
  - [7.3 用户决定何时进入下一阶段](#sec-7-3)

| 你要做什么 | 去哪里 | 谁来做 |
|---|---|---|
| 确认 Notion 中的改动到了 Obsidian | 第 3 节，找上一轮的编号、结果和笔记 | 你或维护者随时可看 |
| 查看 Obsidian 中哪些改动能写回 | 第 2 节首次建基线，然后第 4.1 节查看差异 | 维护者；日常也可由你查看 |
| 批准测试页的一处文字修改 | 第 4.2 节，先审计范围，再切换模式、审批新计划 | 只能你本人，在自己的终端 |
| 暂停、部分完成、结果未知 | 记下轮次或计划编号，见第 5、6 节 | 维护者协助核对，由你裁决 |

**第一次使用**，在本机终端运行这一行检查是否装有新版工具。显示“已安装”后，督导会通知你“首轮验收通过、两个计时器已恢复”和“编辑冻结结束”。收到这两项通知后，再做第 2、4 节。单向刷新可先按第 3 节查看。

```bash
cd /data120/Obsidian/notion-obsidian && if test -f tools/writeback.py && test -f config/writeback.json; then echo '新版写回工具已安装'; else echo '尚未安装，请先查看第 3 节'; fi
```

<a id="ops-boundaries"></a>
**本人操作与 AI 协助的界限**

- 写回工具中，AI 可运行状态、生成预览、查看计划、检查白名单这四类命令（`status`、`plan`、`show`、`whitelist-check`），也可只读检查报告。首次测试中，维护者另获授权在已确认身份的测试笔记里改一处本地文字。生成计划会写本机记录。
- 凡是要求挑战短语或由本人回答 y/N 的操作，只由你在自己的终端完成，包括写入、续执行、写回回退、两种冲突裁决、拒绝/撤销/放弃、恢复删除、核对未知请求、解禁、范围审计、确认异常/积压、保留删除、用已发布版本重整本地，以及刷新轮次的放行、丢弃和对象解禁。AI 不代答、不获取或转述你的当次挑战短语。
- 第 6.3 节的“恢复同步基底”会用已发布的基底覆盖一篇正式笔记。该脚本和 `tools/rollback.py --apply` 都没有本人确认提示，只由你本人执行，AI 不得运行这两种恢复操作。还原前先核对对象、备份和哈希。维护窗口外，两项刷新服务均空闲时，你本人可启动一次手动刷新。AI 只有在你提出要求、维护者已宣布窗口并指定它执行时才能启动刷新。
- 日常由你掌管模式、白名单、高风险开关与急停文件 `state/WRITEBACK_STOP`。首次部署执行者按书面批准的方案安装初始 `plan` 配置；此后进入 `whitelist`、创建或移除急停文件、批准写入均由你本人决定并操作。
- Obsidian 的改动只有经过本人审批才能写入 Notion。请勿用 AI 的 Notion 连接器、自写 API 脚本或 notion-migrate 的 `relink`、`repair`、`cleanup` 绕开审批。你仍可在 Notion 界面直接编辑页面，下轮单向刷新会读取改动。

<a id="sec-1"></a>
## 1. 环境与前置条件

<a id="ops-terminal"></a>
**自己的终端。** 在这台 Linux 的本地或远程桌面从系统菜单打开“终端”（通常为 Ctrl+Alt+T），也可使用你单独登录本机的交互式 SSH 终端。审批时请离开 VS Code 扩展终端、与 AI 共享的终端会话及由代理控制的窗口。终端辅助检查能识别部分自动化标记、重定向及管道；它无法证明坐在终端前的是谁。下面的自检只检查标记名称，输出不含环境变量值。若提示更换终端，请新开一个由你控制的窗口。

```bash
if test -t 0 && test -t 1 && ! compgen -e | grep -Eq '^(CLAUDECODE$|AI_AGENT$|CI$|GITHUB_ACTIONS$|CLAUDE_CODE_)'; then echo '终端基本检查通过'; else echo '请更换本人独立终端'; fi
```

操作用户为 `ubuntu`，下列命令使用 Bash。**每次新开窗口**先运行：

```bash
cd /data120/Obsidian/notion-obsidian && pwd
```

应显示 `/data120/Obsidian/notion-obsidian`。生产工作目录含 `tools/`、`state/`、`reports/`、`logs/`；`/data120/Obsidian/Godcreate` 是在 Obsidian 中打开的库。开发和临时演练目录里的计划只适用于各自副本。一次只让一位操作者在生产目录执行操作。维护者在部署或版本退回时宣布编辑冻结开始。所有设备暂停编辑 Godcreate 笔记及对应的 Notion 页面，并退出 Obsidian。维护者完成手动首轮验收、恢复两只计时器并宣布编辑冻结结束后再打开 Obsidian，恢复编辑。发现计时器被停时，先查执行记录，保留其当前状态。

<a id="ops-desktop"></a>
**写回前打开桌面端。** 在这台 Linux 上启动 Obsidian，打开 Godcreate。确认应用内的 Sync 已同步完成，且本机桌面程序已连续运行满 15 分钟。用以下命令查看正在运行的进程，`etime` 是已运行时间，格式如 `15:00` 表示 15 分钟，`1-02:03:04` 表示 1 天 2 小时 3 分 4 秒。查看命令行未带 `--type=` 的桌面主进程，取这些主进程中最长的 `etime`。以实际计划的启动时间检查为准。

```bash
ps -C obsidian -o etime=,args=
```

没有输出时请打开桌面程序。刚开机或计划提示 `SYNC_SETTLING` 时，等候后**重新生成**计划；原计划的可执行项不会随时间改变。其他设备的修改须先经 Obsidian Sync 到达本机；离线设备重新上线可能带回旧版本（`STALE_REVERT`）和旧删除。120 秒内至少 20 个文件集中变化时，受影响文件可能暂缓写回。超过 5 个新缺失文件可使本轮暂停；按第 5 节核查是否为误删，并根据**该轮**的报告选择恢复。写回或发布期间，请让其他设备和 Notion 用户暂缓编辑同一页。

**目录和术语。** 五个团队目录依次为 General、Quantitative Investing、x_to_notion、Godcreate Business、Godcreate AI工作空间。`Notion_Obsidian/` 保存索引、映射、预览、运行记录和排序规则（sortspec）。现有排序插件负责显示顺序。

| 工具用语 | 在哪里看、是什么意思 |
|---|---|
| 库内 / 库外 | 库内指 Godcreate 文件夹。库外指生产工作目录下的报告、状态和日志，通常在终端查看。 |
| 读连接 / 写连接 | Notion 的 Godcreate 连接用于读取，Godcreate-Writeback 用于审批写回和审计共享范围。 |
| 轮次 / 计划 | `run_id` 标识一次 Notion → Obsidian 刷新；`wb-…` 标识一份 Obsidian → Notion 计划。 |
| 叶子页 / 笔记属性 | 叶子页的正文任何深度均无子页、子数据库或内联数据库。笔记开头两个 `---` 之间的属性叫 frontmatter，里面的 `notion_id` 标识源对象。 |
| 暂停 / 熔断 / 降级 | hold 表示该对象暂时停止自动同步；熔断表示改动量触发保护；降级表示工具把有效模式降回只生成预览。 |
| 基线 / 同步基底 / 请求日志 | 基线和同步基底都是最近一次发布或采纳后用于比较的版本。journal 逐条记录写请求，结果未知时按编号核对。C01–C10 是下行发布校验，W1–W10 是写回后的检查。 |
| 源端 / 采纳 | 源端指 Notion 一侧。采纳指写回成功后把 Notion 导出的版本写入本地笔记并设为新的基线。 |
| 目录笔记 / 文件夹笔记 | 与文件夹同名、代表含子页的 Notion 页的笔记。 |
| 侧栏根 / 根清单 / 采集桥接 | 侧栏根是 Notion 侧栏中的顶层页。根清单是这些顶层页的记录，采集桥接是维护者用于读取该记录的浏览器采集通道。 |
| 挑战短语 / 端点 / 规范化差异 | 挑战短语是程序现场生成、需本人原样输入的确认行。端点是连接实际能访问的页面、数据库或行范围。规范化差异是 Notion 返回结构经工具标准化后与本地表示的可解释差异。 |

<a id="ops-credentials"></a>
**凭据只查权限。** 在本机运行下列命令查看文件模式、属主和路径。常规安装期望头两行为 `600 ubuntu …`，第三行为 `700 ubuntu …`；写凭据及其父目录须由本人持有，文件须为普通文件。文件缺失或权限不符时记录哪一行出错，交给维护者。请勿打开、展示、发送或拷贝两个凭据文件的内容。

```bash
stat -c '%a %U %n' ~/.config/notion-migrate/.env ~/.config/notion-obsidian/writeback.env ~/.config/notion-obsidian
```

需立即撤销写权限时，在 Notion 测试页“••• → 连接”移除 Godcreate-Writeback 的页面访问；若要全局撤销，可在 Notion 开发者连接设置中撤销该集成的凭据或移除连接。随后由本人按第 5 节核对已发送的请求。请保留用于定时刷新的只读连接。

<a id="sec-2"></a>
## 2. 初始化与配置

<a id="ops-preflight"></a>
**先准备两个连接和侧栏。** 在 Notion 的 General 打开「Obsidian 写回测试页」，确认页面 ID 为 `41b12b2f-a1f9-80a6-98b3-fe4baf325424`，从右上角“••• → 连接”检查读连接 Godcreate 已添加。测试页同时共享给 Godcreate-Writeback；该写连接本次只应看到这个叶子页。在 Notion 的连接内容访问设置中核查，移除其他额外共享；从父页继承的权限需要回到父页处理。首次部署还需本机 Chrome 保持登录 Godcreate 工作区、侧栏中能看到该页。是否完成分别以第 3 节本轮侧栏证据、下文白名单检查和第 4.2 节范围审计为准。

**检查配置与基线（维护者先做；你可复核）。** 首轮验收后，尚未编辑测试页时，在生产工作目录依次运行，每次看完输出再继续：

```bash
python3 -I -B tools/writeback.py status
```

期望 `模式 mode：plan（配置 plan）`。配置缺失时默认 `off`；若有效模式与括号中的配置不同，记录后面的原因，交给维护者。用下列命令核对**页面范围**。

```bash
python3 -I -B tools/writeback.py whitelist-check
```

除标题外，应只有测试页这一条。查 ID、标题、`类型 page`、`后代页面 0`、`内联数据库 0`、`行 0`、`叶子 是`、`读连接可读 是`。任一项是“未知”“否”或有额外目标时，保留输出；重查读连接共享、侧栏和最近一次发布。需要查看配置键时运行：

```bash
python3 -I -B -m json.tool config/writeback.json | grep -E '"(mode|test_page_id|whitelist_page_ids|database_whitelist_ids|read_integration_readonly_confirmed_at)"' -A2
```

<a id="ops-baseline"></a>
**然后在编辑前建立基线**（已由维护者做过这一轮时，可核对其记录）：

```bash
python3 -I -B tools/writeback.py plan --scope whitelist
```

首次计划会把当时已有的全库差异列为 `INITIAL_BACKLOG` 拒绝项，可执行数为 0。请在拒绝清单中核对测试页的 ID 和路径，确认它没有积压。将基线计划编号交给维护者。其他页已有差异时保留其原文，不批量确认积压。测试页若已先被编辑，按第 4.1 节检查旧改动再处理。

| 用户需要核对的配置键 | 首次部署值 |
|---|---|
| `mode` | `"plan"`。第一次真实写入时只由你临时改成 `"whitelist"`。 |
| `test_page_id`、`whitelist_page_ids` | 前者是上面的测试页 ID，后者只含这一个 ID；数据库白名单为空。 |
| `read_integration_readonly_confirmed_at` | `"2026-11-17T18:30+07:00"`，沿用已经核对的只读记录；请勿发送写请求来重测。 |

其余安全阈值保持部署值。计划默认有效 24 小时，本地删除通常进入 14 天待决期（详见第 5 节）。本项目按单成员免费套餐配置；增加第二位成员前，先决定是否需要升级套餐。看到 `warn_block_limit` 表示工作区可能接近免费额度。超额后有 3 天宽限期，期满后界面和 API 都不能新建内容。按报告核对真实成员与套餐。出现已确认额度封锁时见第 6 节。

<a id="sec-3"></a>
## 3. Notion → Obsidian 自动同步

<a id="ops-round"></a>
**日常检查。** 本机为 UTC+7。增量每天 **00:30、06:30、12:30、18:30** 触发，最多随机推迟 10 分钟；全量每周日 **05:30** 触发，最多推迟 15 分钟。停机错过的轮次在恢复调度时可能补跑。查看两个计时器的 LAST、NEXT 和最近五条结果：

```bash
systemctl --user list-timers --all 'notion-obsidian-*' --no-pager
tail -n 5 logs/refresh_history.jsonl | python3 -I -B -c 'import json,sys; print("轮次    模式    结果    结束时间"); [print(r.get("run_id") or "-",r.get("mode") or "-",r.get("result") or "-",r.get("finished") or r.get("at") or "-") for r in map(json.loads,sys.stdin)]'
```

应有 incremental、full 两个 timer，NEXT 均有时间。增量服务运行时对应 timer 的 NEXT 会显示 `-`，可用 `systemctl --user show notion-obsidian-refresh-incremental.timer -p SubState -p NextElapseUSecRealtime` 核对 `SubState=running`。表内第一列即轮次编号，例 `20261121T0031-T1`。`skipped_lock_busy` 只有时间，没有编号和报告目录。若本轮已正常发布，按笔记路径查内容即可；还可在其他设备查看同一笔记确认 Obsidian Sync 已到达。只读试运行阶段若由你另行启动，第 7 节的每日写回预览要手动生成。

查看详细记录，先单独运行下一行，按提示输入上一表中的真实编号再回车：

```bash
read -r -p '请输入要查看的刷新轮次编号：' RUN
```

```bash
if test -f "reports/${RUN:?请先输入轮次}/run_record.md"; then less "reports/$RUN/run_record.md"; else less "reports/$RUN/refresh_status.json"; fi
```

`less` 里按空格翻页、按 q 退出。旧版轮次可查 `reports/$RUN/refresh_status.json` 和 `logs/refresh_$RUN.log`。新版先准备快照、附件、暂存库及发布计划，分别在发布前和发布后校验目录、对象、顺序、链接、元数据、附件和正文等。库外 `reports/$RUN/verify/verify.json` 是初验；有 `verify_retry/verify.json` 时以重验为准，发布后的结果在 `post_publish_verify/verify.json`。

| 报告结果 | 下一步 |
|---|---|
| `published` | 打开目标笔记或映射。重复且无新事件的轮次可省略库内摘要。 |
| `published_with_holds` | 其他对象已发布。查 `reports/$RUN/holds.json`、运行记录和库内 `Notion_Obsidian/conflicts/$RUN/`（如有）。 |
| `planned` / `skipped_lock_busy` | 预演在发布前结束；遇锁则直接跳过，不排队。等正在运行的任务结束。 |
| `paused_for_review` | 看库内 `Notion_Obsidian/运行记录/<轮次>-已暂停.md`，按第 5 节处理。若发布中有人保存文件，可能已经部分写入文件，切勿重复放行同一轮。 |
| `failed` / `published_post_verify_failed` | 看报告错误、日志及有无 `Notion_Obsidian/运行记录/<轮次>-未完成.md`；后者已发布过文件。将编号和失败项交给维护者。 |

<a id="ops-sidebar"></a>
**新增顶层页没有出现。** 先确认 Notion 页面已给 Godcreate 读连接共享。在上面选择的轮次看实际侧栏状态：

```bash
python3 -I -B -c 'import json,sys; s=json.load(open(sys.argv[1])); print("新鲜侧栏",s.get("sidebar_fresh"),"证据",s.get("sidebar"))' "reports/${RUN:?请先输入轮次}/refresh_status.json"
```

`False` 表示这轮没有取得新侧栏，可能用了旧证据，已有根仍可刷新；`None` 或缺字段不能证明取得了新证据。保持 Chrome 登录并展开能看到测试页的位置，交维护者核查采集桥接、侧栏证据和根清单。在 Obsidian 的 `Notion_Obsidian/映射清单.md` 搜索 ID 前八位 `41b12b2f` 核对实际路径。历史 2026-11-21 00:31 的生产快照根清单尚缺此页，应以新轮次的根清单和唯一笔记核实。

<a id="ops-manual-refresh"></a>
**手动刷新。** 维护窗口外，两项刷新服务均空闲时，你本人可启动一次手动刷新。AI 只有在你提出要求、维护者已宣布窗口并指定它执行时才能启动刷新。 先检查两项服务：

```bash
systemctl --user show -p Id -p ActiveState -p SubState notion-obsidian-refresh@incremental.service notion-obsidian-refresh@full.service
```

两者应为 `ActiveState=inactive`。正在运行时先等结束。需要现在拉取变化，可运行一次：

```bash
python3 -I -B tools/refresh.py --mode incremental
```

完整重读使用 `python3 -I -B tools/refresh.py --mode full`；两者均可能更新正式库。近期一轮增量约 40 分钟，实际以本轮报告为准。实时预演用 `python3 -I -B tools/refresh.py --mode incremental --dry-run`：不会发布到笔记库，仍可能在工作目录写侧栏证据、根清单、快照、暂存文件、日志和报告。某些旧版帮助对这些写入说明不全。离线演练由维护者在独立完整工作副本操作。单改目标库路径仍会使用脚本本身所属的工作区状态。

已共享范围内的页面正文、目录、数据库及行、模板、可映射的链接、可取得的附件均可在本地查看；数据库另有 `.base` 视图。外部目标保留原链接，附件下载失败或特殊块可显示提示。请勿手动修补生成的索引文件来改变同步范围。

| 上一条命令的退出码（可立即运行 `echo $?`） | 含义 |
|---|---|
| 刷新 0 / 1 / 2 / 3 | 0 为正常结束，可能是发布、预演或跳过锁；1 为失败或取消，2 为整轮暂停，3 为本人终端检查未通过。 |
| 写回 0 / 1 / 2 / 3 / 64 | 0 为命令完成；1 为失败或取消；2 为模式、计划或状态拒绝；3 为本人终端检查未通过；64 为命令行参数错误。核对实际结果行与报告。 |

<a id="sec-4"></a>
## 4. Obsidian → Notion 写回预览与审批

<a id="ops-preview"></a>
<a id="sec-4-1"></a>
### 4.1 预览：看本地改动会写到 Notion 哪里

**第一次测试由维护者编辑，你只在第 4.2 节批准。** 请先完成第 2 节编辑前的基线，并确认本机 Obsidian 已运行满 15 分钟、同步完成。以下命令在 Godcreate 的 General 内按完整 ID 查笔记，输出**库内相对路径**，支持带引号及带/不带连字符的 ID：

```bash
(cd /data120/Obsidian/Godcreate && grep -rilE --include='*.md' "^notion_id:[[:space:]]*['\"]?41b12b2f-?a1f9-?80a6-?98b3-?fe4baf325424['\"]?[[:space:]]*$" General)
```

应只显示一项，例如 `General/Obsidian 写回测试页.md`。以实际命中为准，在笔记顶部属性复核相同 ID。没有或有多个匹配时，保持原文，把命中数、路径及最后一次刷新编号交给维护者，按第 3 节核查映射清单与侧栏。

若预览明确列出此页 `INITIAL_BACKLOG`，先看它的既有差异。确实要把**这份**原有改动纳入写回时，才由你在自己的终端逐行操作，先输入刚才查到的相对路径：

```bash
read -r -p '库内相对路径（从 General/ 开始）：' REL
```

```bash
python3 -I -B tools/writeback.py ack-backlog --path "${REL:?请先输入实际相对路径}"
```

看到与该页相符的路径和 ID 后由你决定是否输入 y。确认后让维护者重新生成计划；无关陈年差异留在原处。若测试修改可以重新开始，也可先由维护者核对并恢复该次测试前的文字，在干净基线上重新做约定的编辑。

维护者在已发布测试页的**一个已有普通段落**替换几个字，例如在一处句末增加双方约定的测试标记；保留原文、标题、路径、层级和属性。仅项目首次实机验收由维护者把实际的「原文 → 新文」、笔记路径和计划编号记入执行记录，并将原文、新文、路径和计划编号直接发给你核对。日常在库外 `reports/writeback/<计划编号>/` 查看保存的计划和预览。只读查看顺序如下：

```bash
python3 -I -B tools/writeback.py plan --scope whitelist
```

从输出的 `计划 plan_id：wb-…` 复制编号。下列输入提示单独执行，回车后再运行查看命令；这些变量只在当前窗口有效：

```bash
read -r -p '请输入刚输出的计划编号：' PLAN
```

```bash
python3 -I -B tools/writeback.py show --plan "${PLAN:?请先输入本次计划编号}"
```

查看库外 `reports/writeback/<计划编号>/preview.md` 和完整 `plan.json`。库内 `Notion_Obsidian/写回预览/` 可供阅读。差异标题 `B ↔ L` 中 B 是上次发布的基线，L 是本地内容；正文片段以 `-` 起头的是原文，以 `+` 起头的是现文，上下文保持原样。忽略 `---`、`+++`、`@@` 这些文件名和位置标记。本次应只有约定的一对原文、新文，更改范围与维护者记录相符。在 `plan` 模式下，符合条件的正文修改也显示“可执行”，本例可见“可执行 1”。此时程序仍会拒绝 apply。只有本人切到 whitelist 并重新生成计划后，才可进入审批。每个被拒绝的条目在预览末尾“拒绝写回 / 只报告”中列出理由和下一步；未确认的目标、删除或冲突须先核清。

| 条目及范围 | 如何判断 |
|---|---|
| `M` 修改 | 现有普通段落、标题、列表和待办、引用、代码、公式、分隔线、列数不变的表格单元格和保留图标/颜色的 callout 文字，按实际差异及预览判断。本次只批准测试页的一个 M。 |
| `N` 新建 | 新建受白名单父对象、目录笔记、数据库、额度等条件限制。本次只授权一个叶子测试页，普通其他位置的新笔记留在本地；发生同路径碰撞时应看运行记录。 |
| `B`、`D` 删除 | B 是删块条目，只能专门选择，初始配置禁用；D 是页面移入回收站。`B-001` 与差异标题里的基线 B 含义不同。本次均不选。 |
| `T`、`MV`、`C` | T 是标题改名；初始配置下跨文件夹移动会拒绝 `UNSUPPORTED_RENAME`，需要调整层级请在 Notion 手工处理。C 是双方冲突，按第 5 节。 |
| 应到 Notion 编辑 | 分栏、同步块、图片和附件、嵌入与书签、带色或提及内容、指向 Notion 页的特殊链接、改块类型/缩进、数据库结构和模板。只改空行等无法可靠还原的差异会给出拒绝原因。 |

扫描限于五个团队目录下的纳管笔记。`Notion_Obsidian/`、`.obsidian/`、`*.base`、附件/模板目录及标记为数据库、模板或生成文件的笔记不作为普通正文写回。已有同一笔记的多个关联条目须共同选择；存在不能批准的部分时停在预览。这次不用 `--all-safe`、`--delete-all-in-plan`，也不扩大授权。英文 `--scope whitelist` 与 `--scope 白名单` 相同。

<a id="ops-confirm"></a>
<a id="sec-4-2"></a>
### 4.2 批准并写入一处改动（只能你本人操作）

**选时间和窗口。** 打开第 1 节所说的自己的终端，重新 `cd /data120/Obsidian/notion-obsidian`。避开第 3 节的定时刷新窗口；例如约 08:00 或 14:00 再检查两个服务已结束，给规划、审批与验收留时间。项目最近的增量约 40 分钟。刷新和写回争用同一把锁，均不排队：刷新会记录 `skipped_lock_busy`，规划等命令会拒绝。若在输入写回短语之后遇锁，计划可能已经是 `confirmed`；此次拒绝后等刷新完结，看 `status`，用新计划重新审批。

**1．保持预览模式，先查范围。** 在同一终端逐条运行：

```bash
python3 -I -B tools/writeback.py status
python3 -I -B tools/writeback.py whitelist-check
python3 -I -B tools/writeback.py audit-scope
```

前两项期望如第 2 节；审计使用写连接读取目标范围，不会写 Notion。结果必须含 `"ok": true`、`"extra": []`、`"unreachable": []`。如有额外或缺失对象，在 Notion 的“••• → 连接”纠正共享，重新审计；若提示解除过去的自动降级，仅在核准本次结果后由你确认。网络错误或无法确定可见范围时先保留输出并联系维护者，保持 `plan`。

<a id="ops-config-edit"></a>
**2．备份配置，只改一个词。** 确认当前配置和有效模式都是 `plan`、范围正确后运行：

```bash
CFG_BACKUP=$(mktemp config/writeback.json.before-whitelist.XXXXXXXX) && cp -p -- config/writeback.json "$CFG_BACKUP" && printf '请记下本次配置备份：%s\n' "$CFG_BACKUP"
```

备份成功才继续。记录显示的确切文件名，以便重新打开终端后找到本次备份。运行 `nano config/writeback.json`。只将 `"mode": "plan"` 中的 `plan` 改为小写 `whitelist`，保留英文引号和逗号。不要填写 `full` 或改变白名单、`allow_*` 等其他键。Ctrl+O 后按回车保存，Ctrl+X 退出。接着验证语法并检查实际生效模式：

```bash
python3 -I -B -m json.tool config/writeback.json >/dev/null && echo 'JSON 格式正确'
python3 -I -B tools/writeback.py status
```

两项都须通过，并出现 `模式 mode：whitelist（配置 whitelist）`。还可以运行 `diff -u -- "$CFG_BACKUP" config/writeback.json`，应只看到那一行模式变化。格式损坏可能使后续自动刷新失败。若格式错误、模式不符、发生额外变化或打算在**发送写请求前**放弃，本人当场恢复本次已核实的原配置并复查：

```bash
cp -p -- "${CFG_BACKUP:?请先填入本次保存的备份文件名}" config/writeback.json && python3 -I -B -m json.tool config/writeback.json >/dev/null && python3 -I -B tools/writeback.py status
```

预期回到 `模式 mode：plan（配置 plan）`。换过终端时先单独执行 `read -r -p '输入本次配置备份路径：' CFG_BACKUP`，填入刚才保存的文件名。把失败输出、已有计划编号和对应的库外预览路径交给维护者。计划默认 24 小时过期，需要立即作废用第 4.3 节。

**3．重新规划和逐项审阅。** 之前以 `plan` 模式生成的计划不能跨模式使用。确认桌面端已稳定后，生成一份**当前白名单模式**的新计划：

```bash
python3 -I -B tools/writeback.py plan --scope whitelist
```

记录四个值：`计划 plan_id：wb-…`、`sha（apply --sha 用前 12 位）：…`、`可执行 1`、末尾审批命令里的 `--items M-…`。计划的完整摘要绑定实际计划和选择项。现在单独输入新计划编号：

```bash
read -r -p '新白名单计划编号：' PLAN
```

```bash
read -r -p '末尾 --items 后唯一的 M 编号：' ITEM
```

```bash
read -r -p 'sha 前 12 位：' SHA
```

```bash
python3 -I -B tools/writeback.py show --plan "${PLAN:?请先输入新计划编号}" --item "${ITEM:?请先输入唯一的 M 编号}"
```

只应包含指定测试页、约定的一个普通段落及那一对原文/新文。如显示可执行数为 0 或出现 `SYNC_SETTLING`，保持 `whitelist`，按第 1 节和第 6.1 节等待同步稳定后重新生成计划。如出现其他目标、更多可执行项、不匹配的差异或拒绝，按步骤 2 恢复 `plan`，把预览交给维护者。

**4．从这份新计划复制仅有一个 M 项的完整审批命令，单独运行。** 先核对工具建议与刚才输入的 `PLAN`、`SHA`、`ITEM`，再运行：

```bash
python3 -I -B tools/writeback.py apply --plan "${PLAN:?请输入计划编号}" --sha "${SHA:?请输入 sha 前 12 位}" --items "${ITEM:?请输入 M 项编号}"
```

命令必须带上同一新计划的 `--plan`、`--sha` 和 `--items`。本文多用的 `-B` 只禁止写 Python 字节码缓存，不影响审批。

先逐项核对屏幕上的页面、操作和数量。程序在你自己的终端生成本次挑战码，提示“请原样输入下面这一行以确认”。一项 M 的整行包含「确认、计划后四位、修改1、新增0、删除0、改名0、当次挑战码」。在当前窗口手打，或从当前终端选中这行复制粘贴。Linux 终端复制/粘贴是 Ctrl+Shift+C / Ctrl+Shift+V。**空回车或输入别的内容可取消。** 挑战短语不发送给任何人。

确认后，程序先核对范围、页面和内容，写入后定向读回、导出、复验并将已确认内容采纳到本机。可能需几分钟。请保持终端和远程桌面连接，避免 Ctrl+C、关闭窗口或在两端编辑这篇笔记。预检和实际写入间仍存在约一秒的并发窗口。若不一致的选中页面超过 20%，会停止后续写入；遇到中断或异常按下一节查实际结果。

<a id="ops-decisions"></a>
<a id="sec-4-3"></a>
### 4.3 拒绝、作废或处理中断

只想保留本地修改时，到此停在预览即可。需要正式记录决定时，由你在自己的终端按情形**选择一条**。用第 4.2 节的输入方式填入当前 `PLAN` 和 `ITEM`，或先运行 `show` 查到对应编号；新窗口须重新输入。现场 y/N 只在路径、对象和影响与意图一致时确认。

**拒绝并保留本地文字**（针对这次的 M 项）：

```bash
python3 -I -B tools/writeback.py reject --plan "${PLAN:?请输入计划编号}" --items "${ITEM:?请输入 M 项编号}" --reason '暂留本地'
```

**拒绝且恢复本地已发布版本**是另一种选择。只对明确要丢弃本地编辑的 M 项使用下列命令；它会覆盖本地笔记，原文备份在 `backup/writeback-restore/<时间>/`。备份原文、确认路径后再决定：

```bash
python3 -I -B tools/writeback.py reject --plan "${PLAN:?请输入计划编号}" --items "${ITEM:?请输入 M 项编号}" --restore-notion
```

**撤销拒绝**：先单独运行 `read -r -p '输入预览中的库内相对路径：' REL`（如 `General/Obsidian 写回测试页.md`，含空格也可直接输入），再运行：

```bash
python3 -I -B tools/writeback.py unreject --path "${REL:?请输入实际相对路径}"
```

**作废整份草案**：本人核对后输入 y，计划状态变为 `superseded`。要继续时重新生成并审批新计划。

```bash
python3 -I -B tools/writeback.py discard --plan "${PLAN:?请输入计划编号}"
```

**撤销本地删除意图**：从对应笔记的 `notion_id` 或待决删除列表复制页面 ID，先单独运行 `read -r -p '输入待恢复的 Notion 页面 ID：' NID`，再运行下列命令。下一次刷新可从 Notion 恢复文件。

```bash
python3 -I -B tools/writeback.py restore-deleted --id "${NID:?请输入待恢复对象的 ID}"
```

**已有写请求或进程中断**：先看 `status`、库外结果及第 5 节的请求序号，保留仍有疑问的原文。只有状态仍是 `applying`、登记完改名、有效模式等于生成计划时的可写模式、急停已解除，才可由你本人评估 `python3 -I -B tools/writeback.py apply --resume "${PLAN:?请输入中断计划编号}"`。程序会重新核对并要求本人确认；在确认从未发送请求等特殊情形会先询问是否直接关闭。已关闭的 `aborted` 需重新规划；已发送且结果未知的请求不能直接重发。范围不明先留在急停，按第 5 节核对日志。本人回退也要求已解除急停、有效模式可写；若已恢复 `plan`，需要时先重新核准范围，临时恢复授权的 `whitelist`。已写入的异常不要仅以恢复配置或作废草案结案。

<a id="ops-verify"></a>
<a id="sec-4-4"></a>
### 4.4 写后核对：你看结果，维护者完成验收

终端成功结论为 `完成，可验收（applied）`，附近有 `W1–W10` 汇总和完整报告路径。打开 `reports/writeback/<新计划编号>/result.md`，先看“写后一致性检查（W1–W10）”，应为 `pass` 或有理由的 `n/a`，再看条目表“结果、原因、下一步”。本次没有删除或改名，相关检查应为不适用。若出现“部分完成，待人工处理”“已中止”、任一 `fail`、“结果未知的请求”或“BLOCKED 页面”，保留报告和请求编号，按第 5 节由本人核实。若出现“已拒绝（未发送写请求）”，转到第 6.1 节处理锁忙、SHA 不符、过期或模式不符。没有产生 result.md 时保留输出和状态；勿据此假定已经写入或可以直接重发。

给维护者**新计划编号、唯一 M 项、结论行与 result.md 路径**，不要发送凭据、挑战短语或无关笔记正文。维护者核对 Notion 实际段落、Godcreate 实际笔记、库外计划和请求记录，并重新运行 `python3 -I -B tools/writeback.py plan --scope whitelist`，确认同一处改动已清零再向你报结果。

本次单页实测记录（2026-11-21）：计划 `wb-20261121T182505-8354` 的 `M-001` 已由本人确认并完成写入，结果为 `applied`。Notion 实时段落为“这是一段写回验证文字”，中间“写回”保持加粗；Godcreate 对应笔记为 `这是一段**写回**验证文字`。W1、W2、W3、W6、W7、W8、W10 为 `pass`，W4、W5、W9 为有理由的 `n/a`；写回采纳的发布前后 C01–C10 全部 `PASS`。复核计划 `wb-20261121T183541-f78c` 的 N/M/B/D/T/MV/C 均为 0，可执行 0。验收完成后由本人把模式恢复为 `plan`。

首次实测经过：用户在写回过程中曾手动编辑对应的 Notion 测试页。17:07 新增段落，17:28 修改了原段落，程序随后三次判为冲突或在 apply 预检时中止，写请求为 0。之后由本人运行 `resolve --plan wb-20261121T173355-9067 --item C-001 --take notion`，重建基线并重新执行，最终计划 `wb-20261121T182505-8354` 成功 `applied`。写回期间不要手动编辑对应的 Notion 页面。

**维护者参考。** W1/W2 查目标读回和未选内容；W3 查每个写请求对应的对象与批准项；W4/W5 查删除及改名；W6 查导出、采纳与复验；W7/W8 查重新规划与映射/历史状态；W9/W10 查额度、批准清单和日志。W7 的代理结果须再用实际新计划验证。将来有 D 项时，下一轮成功刷新后再次规划，查删除核验为 `verified` 或 `needs_manual`。

本次核对完成、所有异常已处置后，由你恢复预览模式。在生产工作目录中单独备份当前白名单配置，保存终端显示的文件名。步骤 2 记录的原始 plan 备份继续保留。恢复模式前请确认写回进程已退出、计划状态为 `applied` 或 `superseded`，以及复核计划可执行数为 0。

```bash
CFG_BEFORE_PLAN=$(mktemp config/writeback.json.before-plan.XXXXXXXX) && cp -p -- config/writeback.json "$CFG_BEFORE_PLAN" && printf '本次白名单配置备份 %s\n' "$CFG_BEFORE_PLAN"
```

备份成功后，优先使用原始 plan 备份恢复。仅当原始备份不可用时，才运行 `nano config/writeback.json`，只将 `whitelist` 改为 `plan`。输入本小节开头显示的“本次白名单配置备份”文件名。恢复命令会核对白名单及其余全部键都相同后才复制。

```bash
read -r -p '步骤 2 记下的原始 plan 备份文件名 ' ORIGINAL_PLAN
python3 -I -B - "${ORIGINAL_PLAN:?}" <<'PY' && cp -p -- "$ORIGINAL_PLAN" config/writeback.json
import json, sys
from pathlib import Path

original = Path(sys.argv[1]).resolve(strict=True)
assert original.parent == Path('config').resolve() and original.name.startswith('writeback.json.before-whitelist.')
old = json.loads(original.read_text())
current = json.loads(Path('config/writeback.json').read_text())
assert old.pop('mode') == 'plan' and current.pop('mode') == 'whitelist' and old == current, '配置或范围不同，请人工核对'
PY
```

两种方式都须验证语法和生效模式。

```bash
python3 -I -B -m json.tool config/writeback.json >/dev/null && python3 -I -B tools/writeback.py status
```

应看到 `模式 mode：plan（配置 plan）`。若编辑后 JSON 或 status 检查失败，请立即恢复本步骤开始时保存的白名单配置。语法损坏会使后续每一轮下行刷新失败。换过终端时，先单独执行以下两行，输入本小节开头显示的“本次白名单配置备份”文件名。

```bash
cd /data120/Obsidian/notion-obsidian
read -r -p '本次切回预览之前的白名单备份文件名 ' CFG_BEFORE_PLAN
```

<!-- verify:config-recovery -->
```bash
python3 -I -B - "${CFG_BEFORE_PLAN:?请先输入本次备份文件名}" <<'CHECK' &&
import json, sys
from pathlib import Path

p = Path(sys.argv[1])
if (p.is_symlink() or p.resolve(strict=True).parent != Path('config').resolve()
        or not p.name.startswith('writeback.json.before-plan.')
        or json.loads(p.read_text())['mode'] != 'whitelist'):
    raise SystemExit('请选择本次已核实的白名单配置备份')
CHECK
cp -p -- "$CFG_BEFORE_PLAN" config/writeback.json &&
  python3 -I -B -m json.tool config/writeback.json >/dev/null &&
  python3 -I -B tools/writeback.py status
```

恢复后应显示 `模式 mode：whitelist（配置 whitelist）`。暂停审批任何写入。随即按本节的原始 plan 备份分支核对白名单和全部其他键，恢复步骤 2 已验证的 plan 配置，并再次检查 JSON 与 status。若检查仍失败，保留备份及错误输出，请维护者协助。日后是否扩大白名单由你另行决定。

<a id="sec-5"></a>
## 5. 冲突处理

先把仍需保留的文字另存到 Godcreate 以外。预览里 B 是上次同步到本机的原文，L 是本地内容，R 是 Notion 的当前内容。差异的 `-` 行是原文，`+` 行是修改后的文字。编号 `B-001` 则表示一个删除块请求。

本节命令按场景**选一条**。带「本人」的命令只在你从系统菜单单独打开的终端里运行。将命令中单引号内的中文占位词替换成报告里的实际值，保留引号。刷新编号来自刷新历史，如 `20261121T0031-T1`；写回计划编号以 `wb-` 开头，C、D、M 等项号取自同一份计划；对象 ID 是笔记顶部 `notion_id` 的值；请求序号取自结果报告的 journal 列，例如 `12`。新开终端也可直接使用这些命令。

<a id="sec-5-1"></a>
### 5.1 对照两份内容，保留需要的版本

| 现象 | 如何判断和处理 |
|---|---|
| 仅本地改动（LOCAL_ONLY） | 下行同步保留本地编辑。先用第 4 节的预览查看差异。目标相同的链接拼写改动、本地标签、可写正文字段分别处理，扫描到的文件数可以大于修改项数。 |
| 两边都改了（CONFLICT_BOTH），或 Notion 改名时本地仍有编辑 | 原笔记继续保留。若原笔记是文件夹笔记且存在 Sync 冲突副本，删除冲突副本前先备份两份版本；Notion 同时改名时按第 6.3 节的恢复窗口处理。源端副本通常在 `Notion_Obsidian/conflicts/<刷新编号>/<对应子目录>/<文件名> (Notion 版本 <刷新编号>).md`。同一对象和同一源端版本只生成一次。比对后可把两边需要的文字另存，自己合并，再重新预览。要保留合并稿在本地，可按第 4 节拒绝适用的可写条目。若产生 C 项，按下面两种选择处理。处理并备份后，可清理对应的工具生成副本，它没有原笔记的 `notion_id`。 |
| 出现带 `(Conflicted copy ` 的文件、重复 ID、路径和 ID 不一致 | 这是设备同步产生的副本或映射异常。先确认哪个文件是原件，另存两边内容后合并，保留原件的对象 ID。不要复制 ID 给个人笔记。查看下一轮报告确认对象暂停是否解除。 |
| 在同一目录改文件名 | 刷新先登记本地名字，Notion 标题保留。登记后重做计划，只有单独批准 T 项并选择改名才会更新远端标题。未登记的本地改名或移动会阻止写回和中断续办。可等下一轮自动刷新。维护窗口外，两项刷新服务均空闲时，你本人可启动一次手动刷新。AI 只有在你提出要求、维护者已宣布窗口并指定它执行时才能启动刷新。 具体检查见第 3 节。 |
| 拖到另一目录、改文件夹名、改了与目录同名的文件 | 当前配置拒绝这些移动（UNSUPPORTED_RENAME），对象及相应子树可能暂停接收更新。把文件移回原位并恢复原名，下一轮刷新重新判定。需要改变源端层级时本人在 Notion 中操作。若本地命名差异尚有效时 Notion 也改名，按报告检查两侧标题与 ID，先保全两版，再处理命名冲突。 |

**本人选择 C 项。** 采用 Notion 时会先将被替换的本地原文留在库外 `backup/writeback-restore/<时间>/`，然后把笔记恢复到上次发布版本，下一轮刷新才取得 Notion 当前内容。采用 Obsidian 时本人需要输入挑战短语，这一步生成新计划。之后还需核对新计划的变化并另行批准。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py resolve --plan '你的写回计划编号' --item '你的C项号' --take notion
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py resolve --plan '你的写回计划编号' --item '你的C项号' --take obsidian
```

<a id="sec-5-2"></a>
### 5.2 在 Obsidian 删除一篇笔记会怎样

1. 删除本地文件本身不会把 Notion 页面移入回收站。`off` 模式按源端还原；在预览或白名单模式，符合条件的缺失页面由刷新登记为「待决删除」，默认保留 14 天。用下面的状态命令查看对象、路径和截止时间，也可看该轮运行记录的待决删除清单。本机主动恢复了带原 ID 的文件，扫描会将其登记为已恢复。
2. 白名单外页面无法批准删除。初次测试页是侧栏根，属于 DELETE_UNSUPPORTED。只删除目录的同名笔记而保留子页（PARTIAL_DELETE）、删除数据库/模板/根对象、所删页面的 Notion 内容已变（CONFLICT_DEL_EDIT）等情况，按下一次成功刷新恢复。未决定的待决删除到期后，下一轮按 Notion 恢复。误删且不想等到期，本人可按 ID 撤销删除意图，再等待刷新。
3. Notion 已回收而本地仍有编辑，会提示 REMOTE_TRASHED_LOCAL_PRESENT。先确认源端回收为本人意图并另存本地文字。本地文件可作为留档，确认不再需要时本人可在 Obsidian 清理；若本应保留源页，先在 Notion 恢复。源端不再导出的对象有留档记录，不自动永久清除本地文件。
4. 默认同一轮新缺失超过 5 篇会暂停。离线设备重新上线可能重放旧删除。认清受影响文件后，普通暂停的「放行」按 Notion 恢复笔记；确认一批删除确实有意时，本人可选「登记待决」，再逐项决定。发布期间文件发生竞争应改看第 6.3 节。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py status
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py restore-deleted --id '误删笔记的对象ID'
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py hold --run '本地大批删除导致暂停的刷新编号'
```

**普通整轮暂停或单个对象暂停。** 在 Godcreate 打开 `Notion_Obsidian/运行记录/<刷新编号>-已暂停.md`，核对列出的原因、对象和路径。对象级暂停详见库外 `reports/<刷新编号>/holds.json`。仅在接受最近一次普通暂停的计划、库内也没有再次改变时，由本人选择放行；也可放弃本轮。单个对象的问题已排除、需要本人解除时按 ID 操作。三条命令各自要求本人确认。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/refresh.py --approve-run '普通暂停的刷新编号'
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/refresh.py --discard-run '普通暂停的刷新编号'
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/refresh.py --release-hold '已排除问题的对象ID'
```

<a id="sec-5-3"></a>
### 5.3 写回中断、返回 503 或结果未知

先读 `reports/writeback/<写回计划编号>/result.md` 的「结果」「结果未知的请求」「BLOCKED 页面」和各项的「下一步」。完成、部分完成、已中止、已拒绝分别记录。某个请求即使超时，也可能已在 Notion 生效。按页面 ID、块 ID、实际文字和时间复核，保留库外请求记录、源端与本地两版。能证明生效时，本人登记 `committed`；能证明未生效才登记 `not_committed`。仍无法判定就保留未知，把计划号、请求序号和报告位置交给维护者，不要重发。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py reconcile --journal '实际请求序号' --decision committed
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py reconcile --journal '实际请求序号' --decision not_committed
```

核清被阻断或只创建了一部分的页面（BLOCKED / PARTIAL_CREATE）、保存证据后，本人方可解除。仅原计划仍为 `applying`、急停已解除、有效模式允许写入且没有未登记改名时，才能在第 4 节的本人终端用新的挑战短语续办。已急停中止的 `aborted` 计划需重新规划。「部分完成」和结果未知应先查清事实。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py unblock --page '核清的页面ID'
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py apply --resume '仍为applying的计划编号'
```

<a id="sec-5-4"></a>
### 5.4 急停，以及本人回退已写入 Notion 的内容

- **停止后续写回请求。** 本人运行 `cd /data120/Obsidian/notion-obsidian && touch state/WRITEBACK_STOP`。已送出的请求按第 5.3 节核对。此文件阻止写入、续办和回退；逐条核对和解除页面阻断仍可由本人进行。正常的 Notion → Obsidian 定时同步继续。
- **单独暂停自动同步。** 平时保持两只计时器开启。仅当用户决定为实际事故暂停下行同步时，本人可运行 `systemctl --user stop notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer`。已经运行的服务可能仍在工作，用第 3 节查看。事故处理完且不在维护者的部署/退回窗口，才运行 `systemctl --user start notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer`。错过的轮次可能立即补跑。
- **撤销远端写权限。** 本人在 Notion 测试页的「••• → 连接」取消 Godcreate-Writeback 访问，或在开发者门户撤销该连接。必要时由本人将本机写连接凭据移至本人专用的受保护位置，保持只读连接可用。恢复时由本人把写凭据文件放回原路径，检查文件为 `600 ubuntu`、父目录为 `700 ubuntu`，先在 `plan` 模式运行 `audit-scope`，通过后再按第 4.2 节进入 `whitelist`。不要打开、打印或转发凭据。撤销权限不会逆转已完成写入。AI 不得代办上述操作或代输短语。

**仅回退指定写回项时，按以下顺序进行。**

1. 先急停并另存仍要保留的本地新文字，核对 Notion 当前内容、已送出项和 `result.md`。其间不编辑目标笔记。下一次刷新会按回退后的 Notion 内容更新本地，若有并发编辑，还可能出现冲突。
2. 本人用第 5.2 节的状态命令确认配置模式和有效模式。本次只允许 `whitelist`，产品亦支持另行授权的 `full`。若已降到 `plan`，先修正共享，由本人重新审计授权；若已结束试验切回 `plan`，仅在明确决定回退后按第 4 节恢复白名单，并再次核对状态。
3. 本人确认急停原因已排除，在自己的终端运行 `cd /data120/Obsidian/notion-obsidian && rm -- state/WRITEBACK_STOP`，再用状态命令确认急停已解除，随即单独运行下面命令。核对显示的逐项回退预览，只输入屏幕此次生成的短语，字段依次为“确认”、计划号后四位、回退项数和方括号内的项号、现场挑战码。真实短语留在本人终端。已发生后续变化的块可能跳过或转人工处理。
4. 查看回退结果和跳过项。按事件需要再次急停，或按第 4 节切回 `plan` 并核对状态。下一轮成功刷新后核对本地笔记和库外 `backup/vault-before/<刷新编号>/`。项目按 Notion 回收站 30 天窗口安排恢复；超出窗口应先联系 Notion 支持。归档重建会产生新 ID，无法承诺恢复原关联。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py rollback --plan '核对过的写回计划编号' --items '需回退的实际项号'
```

<a id="sec-6"></a>
## 6. 常见问题

<a id="sec-6-1"></a>
### 6.1 页面找不到、可执行项为零或命令被拒绝

| 提示或现象 | 你可以这样核对 |
|---|---|
| 找不到新顶层页或测试页 | 在 Godcreate 的 `Notion_Obsidian/映射清单.md` 搜索测试页 ID 前八位 `41b12b2f`。在 Notion 页面「••• → 连接」确认 Godcreate 只读连接可访问。按下面命令找最近有刷新编号的记录；`sidebar_fresh=false` 表示这一轮采用旧侧栏，新顶层页可能还没被发现。在本机 Chrome 登录 Godcreate 工作区并开着窗口，让维护者核对 opencli 浏览器桥接、该轮 `new_roots_unshared` 和 `logs/refresh_<刷新编号>.log`。以新轮次实际发布并且库内正好一个 ID 命中为准。 |
| `mode=off`、自动降到 `plan`、权限报错 | 检查运行目录、`status` 中的配置模式、有效模式和降级原因。凭据权限只用第 **1** 节的 `stat` 检查。本人核对两只连接的能力和内容访问权限，轮换凭据或变更共享后由本人执行第 4 节的授权范围审计。 |
| `SYNC_SETTLING`、本机没有 Obsidian、可执行 0 | 在这台 Linux 机器打开 Obsidian 桌面和 Godcreate，运行 `pgrep -x obsidian >/dev/null && echo 运行中 || echo 未运行`。等本机启动满 15 分钟，观察 Sync 同步完成再生成新计划。默认 120 秒内变动达到批量保护条件（至少 20 个文件）时，涉及的文件还会暂缓。旧计划不会随时间自动解锁。若均已满足，展开条目查看是否属于只读或冲突。 |
| `NO_BASE`、`NOT_EXPORTED`、`RENDERER_MISMATCH` | 查最近一次**成功发布**的刷新，等待该笔记生成匹配基线再规划。首次启用应在编辑前生成第 4 节所述基线计划。`INITIAL_BACKLOG` 是启用前已有的差异；本人先确认原文，再按下方命令逐篇认可，重新预览。 |
| 锁忙、SHA 不符、过期或模式不符 | 刷新和写回共用 `state/refresh.lock`。刷新遇锁记 `skipped_lock_busy`；plan、resolve、hold、rebase 遇锁立即退出。apply 已输短语才遇锁时，已经确认的计划不能再用。锁不会等待，保留现场并在当前轮结束后重新查看状态、生成计划和核对新 SHA。每天 00:30、06:30、12:30、18:30 的增量刷新可延后 10 分钟，单轮约 40 分钟。尽量避开开始后约一小时，具体据服务状态判断。 |
| 本人终端检查拒绝 | 管道、非交互运行及部分 Claude Code/CI 环境会被拒绝。终端检测只能辅助防误操作。本人从系统菜单另开普通终端，确认它没有交给 AI 控制。确认、短语、回退和恢复均由本人完成。 |
| 改动过多而暂停写回，或遇到 403 | 先核对所有异常修改。只有确认整批都属本人时才按下方命令认可，再生成**全库**计划。成员数未知时按多成员保守估算，可限制新建或追加，原块修改另行判断。范围外或连接无权访问要处理共享。命中免费套餐块限额的 403 会把 `creation_state` 留为 `blocked_confirmed`，该标记跨轮保留，联系维护者核实后处理，没有清除它的 CLI。 |
| 锁定、只读区域、属性有损、结构歧义 | 查看该项的原因、行号和建议。段落、标题、列表、待办勾选、引用、代码、公式、分隔线、部分标注块和列数保持不变的表格文字可进入预览；列布局、同步块、颜色/提及、附件上传、Notion 链接、数据库/模板结构应在 Notion 处理。数据库行只允许指定的无自动化库及支持的属性。如果只改了目标相同的链接拼写，见第 6.3 节。 |
| `ORACLE_MISMATCH`、`UTF8_BOM`、`LINE_ENDINGS` | 前者表示按计划重新导出与本地原文不一致。先保留原文字，只做简单的段落文字替换，避开缩进、表格、颜色与结构调整；有改名冲突先按第 6.3 节处理。后两者一般说明其他编辑器改变了编码或换行。先把原笔记复制到 Godcreate 外的带日期备份，用文本编辑器以 **UTF-8 无 BOM、LF** 另存要修复的笔记，核对文字再重新预览。可请维护者陪同，避免批量转换个人笔记。 |
| 手机或另一台电脑同步后笔记突然变旧，或从版本历史、备份还原过 | 离线设备重连可能重放旧内容或删除。等本机 Sync 完成，核对恢复的是哪些笔记，待变化平稳后重新预览。若出现 `STALE_REVERT`，先由本人按第 4 节切回 `plan`、另存所需本地差异。只有希望把全库已修改的可处理页面/行恢复为上次发布版本时才使用下方 rebase。刷新或写回期间不要两端同时编辑同一篇。 |
| 部分完成、本地采纳或重新规划失败 | 查 `result.md` 的每项“下一步”，区分已写到 Notion、已更新本地、被跳过和未知。保留编辑前后两版及计划 ID；按第 5.3 节逐条核对。对端设备也要打开真实笔记验证同步收敛，不能以本机刷新通过代替。 |

查看最近一次有编号的实际刷新及其侧栏证据。手动放弃轮次等操作也写入历史，下面的查询会略过这类记录。

```bash
cd /data120/Obsidian/notion-obsidian && jq -s '[.[] | select(.run_id != null and .action == null and has("sidebar_fresh"))] | last | {run_id,result,sidebar_fresh,sidebar}' logs/refresh_history.jsonl
```

**本人按具体提示选择。** 本地路径从预览中复制，使用 `General/…` 等库内相对路径；有空格时保留单引号。重新打开终端时重新取本次编号。只在需要的分支使用以下命令。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py ack-backlog --path 'General/替换成预览中的实际笔记名.md'
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py ack-anomaly --run '显示异常的写回计划编号'
```

认可异常后，生成全库计划，不加范围选项：

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py plan
```

<a id="sec-6-2"></a>
### 6.2 批量恢复本地笔记前

运行前须由本人按第 4.4 节将配置 mode 改回 plan，用 `status` 确认显示 `模式 mode：plan（配置 plan）`。配置仍是 whitelist 或 full 时，程序拒绝这项操作。

`rebase --from-notion` 可能替换全库所有有本地差异的已发布页面/行，包括这次事故以外的未写回修改。确认界面最多列出 50 条。先运行第 5.2 节的 status 核对总数，把仍需保留的文字另存库外。仅当全库范围符合你的意图时，本人在自己的终端执行下方命令并确认。被替换的原文可在库外 `backup/writeback-restore/<时间>/` 找到。完成后在 `plan` 模式重新生成计划，检查差异后再决定后续安排。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py rebase --from-notion
```

扫描计数可在 reports/本轮编号/wb_scan.json 查到。missing 包含工具文件和业务笔记；missing_tool_files 明列缺少的工具文件，tool_files_unregistered_rels 明列库内已存在但未登记的工具文件。失败提醒或写回预览可出现在后者，不能据此断定业务笔记丢失。refresh_status.json 的 scan_lists 只摘录前 50 条，完整列表以 wb_scan.json 为准。

<a id="sec-6-3"></a>
### 6.3 目录冲突、同时保存冲突和写回提示

**移动后找不到唯一笔记、链接重定向导出失败或暂存校验失败。** 记下运行记录中的 notion_id、保留路径和本轮编号。维护者打开该轮的 `refresh_status.json`，以及现存的 `verify_link_targets/verify.json` 或日志中列明的验证文件，核对首个失败的检查项。用文件列表核对真正携带该 notion_id 的笔记，将有独立修改的各个版本分别复制到库外。请维护者与你确认应保留的笔记及其路径，处理缺失或重复的同步副本，保留无 notion_id 的同名个人笔记。若报告显示目录笔记存在正文冲突，或安全校验仍然失败，后续轮次可能继续停下。普通对象暂停时，独立页面仍可发布。待修正后，由维护者核对下一轮实际的 C01–C10 及新链接。若报告明确为已暂停，由本人决定放弃该轮；失败轮没有可放行的审批。

**旧路径同时属于两篇页面。** 若报告同时列出两个页面 ID 和旧路径，按第 6.4 节“占用旧名的同级页面”的办法处理。此时先核对这两篇的身份，保留库外备份。下面的“恢复同步基底”只适用于已确认有正文编辑的目录笔记。

**目录页改名且有人编辑目录笔记。** Notion 将目录改名或移动时，如果你也改了本地目录笔记的正文，路径冲突可使整轮在发布前失败，其他页面的 Notion 改动也不会到达，直至恢复。由本人和维护者约定一个恢复窗口，所有设备暂停编辑 Godcreate 及对应 Notion 页面，并退出 Obsidian。打开 Godcreate 中的 `Notion_Obsidian/运行记录/<轮次>-未完成.md`，从“本次重定向涉及…”抄下保留目录笔记的路径和 notion_id。本人在自己的终端输入该库内相对路径，在库外备份整篇笔记及哈希。使用同一窗口完成本次操作，记下显示的备份目录。

```bash
cd /data120/Obsidian/notion-obsidian
read -r -p '目录笔记在 Godcreate 内的相对路径 ' REL
VAULT=/data120/Obsidian/Godcreate
NOTE=$(realpath -e -- "$VAULT/${REL:?请输入路径}") &&
  [[ $REL == */*.md && $NOTE == "$VAULT/$REL" && -f $NOTE ]] &&
  SAVE=$(mktemp -d "$PWD/backup/folder-note-XXXXXXXX") &&
  cp -p -- "$NOTE" "$SAVE/personal.md" &&
  cmp -s -- "$NOTE" "$SAVE/personal.md" &&
  (cd "$SAVE" && sha256sum personal.md > personal.sha256 && sha256sum -c personal.sha256) &&
  printf '已核对并备份到 %s\n' "$SAVE"
```

看到 `personal.md: OK` 及备份路径后，由你在 Notion 将这一个目录恢复到原名和原位置。

**A. 改回原名后下一轮 `published`。** 等待或由本人在维护窗口内启动一次手动增量刷新。维护者核对父笔记、子页和本人正文后宣布恢复窗口结束。

**B. 改回原名后再次暂停。** 打开最新的 `Notion_Obsidian/运行记录/<轮次>-已暂停.md`，本人按该轮报告放弃本轮，再执行下方“仅处理已备份的这一篇”恢复同步基底。等待下一轮 `published`，维护者核对父子路径，本人将库外 `personal.md` 中的独立正文合并回刚同步的笔记并重新预览。需要保留原先的 Notion 改名时，待 B 分支的 `published` 和正文合并完成后，再由本人在 Notion 重新改名并等待新的计划和刷新核对。恢复窗口内的手动刷新由本人启动，维护者先确认两项服务空闲且锁空闲。

```bash
cd /data120/Obsidian/notion-obsidian && read -r -p '再次暂停的刷新编号 ' RUN
python3 -I -B tools/refresh.py --discard-run "${RUN:?请先输入再次暂停的编号}"
```

**仅处理已备份的这一篇。** 若暂存目录已被清理，维护者可在计时器窗口外按批准的维护窗口运行一次 `python3 -I -B tools/refresh.py --mode incremental --dry-run`，从新生成的 `staging/<新轮次>/vault/` 读取父笔记链接行；也可按目录页 notion_id 从 `state/writeback.db` 的 `base_history` 读取最近基线。维护者陪同核对该页 ID、原路径和基底记录。本步会把库内这篇暂时恢复到同步基底，由本人明确同意并亲自在自己的终端执行。保持各端停止编辑、Obsidian 已退出；等运行中的刷新结束。下面的命令取得同一把刷新锁，并在交换文件时再次检查刚备份的字节。出现报错或“检测到并发保存”时保留现场和备份，由维护者重新核对。

```bash
cd /data120/Obsidian/notion-obsidian
```

即使已经等过一轮或重新打开了终端，也可重新输入第一步记录的三个值。每条命令单独运行，按回车提交所填内容。

```bash
read -r -p '第一步显示的库外备份目录 ' SAVE
```

```bash
read -r -p '未完成报告中目录笔记的库内相对路径 ' REL
```

```bash
read -r -p '未完成报告中该目录页的 notion_id ' NOTE_ID
```

```bash
(cd "${SAVE:?请先执行库外备份}" && sha256sum -c personal.sha256) &&
python3 -I -B - "${REL:?}" "${NOTE_ID:?}" "$SAVE" <<'PY'
import fcntl, hashlib, re, sqlite3, sys
from contextlib import closing
from pathlib import Path

work = Path.cwd()
sys.path.insert(0, str(work / 'tools'))
from publish_vault import guarded_replace
from wb_common import norm_notion_id, parse_frontmatter, split_note

rel, raw_id, saved = sys.argv[1:]
vault = Path('/data120/Obsidian/Godcreate')
note, saved = vault / rel, Path(saved).resolve(strict=True)
assert not Path(rel).is_absolute() and note.resolve(strict=True) == note and note.is_relative_to(vault)
assert saved.parent == (work / 'backup').resolve(strict=True)
with (work / 'state/refresh.lock').open('rb') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    nid = norm_notion_id(raw_id)
    uri = (work / 'state/writeback.db').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute('SELECT rel_path,sha256 FROM base_history WHERE notion_id=? ORDER BY seq DESC LIMIT 1', (nid,)).fetchone()
    assert row and row[0] == rel and re.fullmatch(r'[0-9a-f]{64}', row[1]), '同步基底的路径或 SHA 不符'
    base = (work / 'state/base' / row[1]).read_bytes()
    sha = lambda data: hashlib.sha256(data).hexdigest()
    assert sha(base) == row[1], '基底内容已改变'
    personal = (saved / 'personal.md').read_bytes()
    before = (saved / 'personal.sha256').read_text().split()[0]
    assert sha(personal) == before == sha(note.read_bytes()), '备份后笔记已改变'
    for blob in (base, personal):
        fm = split_note(blob.decode('utf-8'))[0]
        assert norm_notion_id(parse_frontmatter(fm)['notion_id']) == nid, '笔记 ID 不符'
    newer = guarded_replace(str(note), base, before, str(saved / 'race'))
    if newer is not None:
        raise SystemExit(f'检测到并发保存，请核对笔记和备份目录：{newer}')
    print('已恢复同步基底', rel, row[1])
PY
```

等下一轮 `published` 后，维护者核对父子路径并宣布结束恢复窗口。由本人打开刚同步的笔记，对照库外 `personal.md`，只合并独立修改的正文，保留最新 frontmatter 和生成的子页链接。随后重新预览，核对涉及的段落。

如果写回结果记录了连续两次保存，先把库内现有版本另存到库外，再从结果报告给出的 backup/writeback/计划编号/race/ 下的具体文件打开较新版本。对照两份内容，将需要的正文合并回笔记，重新生成计划。

以下处理适用于按本手册部署的工具。保留遇到问题时的刷新或计划编号、文件路径和报告。

| 场景 | 处理方法 |
|---|---|
| 发布中有人保存文件（apply_race） | 发布可能已写一部分。先将刚保存的笔记另存到库外，按该轮 `reports/<刷新编号>/publish_journal.jsonl` 核对实际写入、跳过和未处理路径，再查 `refresh_status.json`。发生同时保存冲突后，保留备份，放弃再次放行同一轮。留待下一轮重新扫描，维护者按对象确认暂停和保留版本。 |
| 远端改名时，新文件名已有个人笔记 | 保留两份原文。在文件列表核对旧文件和占位个人文件，再对照父笔记、目录索引、映射清单及排序规则。记下预期对象 ID、旧/新路径和真实落点；避免点击尚不存在的路径。校验全部通过仍须检查链接对应文件的 ID。工具将改名冲突的链接指向具有该 ID 的唯一保留笔记。删除或重复导致对象暂停时，暂时保留 Notion 路径，其他对象可继续发布。若目录与子页结构冲突，或报告称无法确认冲突笔记的唯一位置，请按本节前面的恢复步骤并保留报告。 |
| 只有链接拼写变化，或它与父笔记正文同时变化 | 工具按对象 ID 将纯链接改写识别为 LINK_REWRITE_NOOP，状态中跳过该差异。父笔记同时改了正文时，预览列出能独立规划的正文操作。若刷新后同时列出父正文 M 和子页改名 T，本人先按建议审批完整的正文项；该项验收成功后，在原范围重新 plan，核对新计划编号、SHA 和仍待改名的 T 项，再按新建议审批。若导出或写后核对仍依赖另一条路径，只给受阻预览。保留正文与现有链接，解决所列子页改名冲突、等待刷新，再重新规划。真正删除了系统生成的子页链接时，先还原那一行，或在 Notion 中调整层级。 |
| `mode=plan` 下拒绝写入 | 此时可以预览。只在本人此次明确批准的测试窗口按第 4 节先检查白名单、审计实际共享，再切换模式、生成新计划和核对内容。旧计划不能复用。 |
| 如何执行提示中的完整命令 | 第 2–4 节使用相对路径，先按第 1 节进入生产工作目录。带有 cd 前缀的命令可以从任意目录开始执行，例如 `cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py plan`。工具输出的 `-I` 与手册中的 `-I -B` 使用同一业务逻辑，`-B` 额外关闭字节码缓存写入。 |
| `AUTO_SYNC_HOLD` | 对象或祖先正在暂停，相关修改列在计划的拒绝清单。按原因核对该轮暂停报告和路径；应自动解除的异常先复位并等待刷新，确需本人解除的按第 5.2 节处理，再生成计划。 |

<a id="sec-6-4"></a>
### 6.4 已知限制

保留中的笔记在 Notion 改名，同时新建或改名的同级页面占用其旧名时，两篇笔记会争用同一路径。整轮暂停会影响其他页面。按报告给出的两个 notion_id 区分对象，将旧路径的笔记及各端冲突副本分别备份到库外。由你本人在 Notion 把占用旧名的同级页改为唯一名称，保留旧笔记，等待下一轮重新扫描。最先报 `published_post_verify_failed` 的轮次已完成发布、只是复验失败，不是暂停轮次，不能也无需丢弃。若多轮已暂停，先运行 `python3 -I -B tools/writeback.py status`，再输入最新的 `-已暂停.md` 对应轮次执行丢弃；较早轮次已经被后续轮次取代。旧轮有重名歧义，不能放行。若保留文件在发布前消失或被另一份文件替换，运行记录会提示拒绝，当次尚未发布。持续出现时按第 6.3 节备份并核对路径和 ID。

Notion 中的目录页改名或移动，同时本地有人编辑了该目录笔记正文时，整轮下行同步可能连续失败，其他页面的 Notion 改动也不会到达。原笔记是文件夹笔记时，删除 Sync 冲突副本前先分别备份原笔记和冲突副本，再按第 6.3 节核对未完成报告、备份个人正文并恢复。

工具生成的 `Notion_Obsidian/README.md` 在渲染器 1.2.1 中写着“每日 03:30（增量）与每周日 04:30（全量）自动从 Notion 更新；在库内改过的迁移笔记不会被覆盖，Notion 的新版本会放到 `Notion_Obsidian/conflicts/<运行编号>/`；Notion 中删除的页面不会自动从库内删除。”实际增量为每天 00:30、06:30、12:30、18:30 加最多 10 分钟；全量为周日 05:30 加最多 15 分钟。仅本地改动且 Notion 内容未变时保留原笔记，不生成冲突副本。README 更正和写回预览、运行记录的额外排序需等渲染器升级。Markdown 对评论、附件、分栏和交互样式的限制仍然存在。

<a id="sec-7"></a>
## 7. 维护与后续启用范围

<a id="sec-7-1"></a>
### 7.1 每天看什么，出事去哪找证据

每天查看两只计时器的 LAST/NEXT，最近三轮的编号、模式、结论，以及库内 `Notion_Obsidian/运行记录/` 是否有新的 `-已暂停.md` 或 `-未完成.md`。正常结论是 `published`；`published_with_holds` 表示部分对象还待处理。无编号的 `skipped_lock_busy` 没有报告目录。增量运行时 NEXT 会暂为 `-`，执行 `systemctl --user show notion-obsidian-refresh-incremental.timer -p SubState -p NextElapseUSecRealtime` 确认运行状态。每周另核对一次周日全量轮次和下次时间。使用 `less` 看报告时按空格翻页、按 q 退出。

```bash
systemctl --user list-timers --all 'notion-obsidian-refresh*'
cd /data120/Obsidian/notion-obsidian && tail -n 3 logs/refresh_history.jsonl | jq -r '[(.run_id // "-"),.mode,.result,(.finished // .at // "-")] | @tsv'
```

| 编号 | 查看位置 |
|---|---|
| 刷新编号 | 库外 `reports/<刷新编号>/refresh_status.json`、`publish_plan.json`、`publish_journal.jsonl` 和 `logs/refresh_<刷新编号>.log`。新版轮次还有 `run_record.md`。暂存校验有 `verify_retry/` 时看重试结果，另看 `post_publish_verify/`。被替换的本地文件备份在 `backup/vault-before/<刷新编号>/`。 |
| 写回计划编号 | 库外 `reports/writeback/<计划编号>/` 的 `plan.json`、`preview.md`、`result.md`、`result.json`。用户确认见 `approvals/writeback/`。选择以 Notion 为准、拒绝并恢复本地或批量重建时，被替换的文字见 `backup/writeback-restore/<时间>/`。维护者保留 `state/export.db`、`state/writeback.db`、`state/base/`、归档、附件和暂停证据。对外只提供必要的编号、路径、结论和脱敏片段。 |

若 D5.1 或 scheduled 签收报告“验收失败”，同时 `logs/refresh_history.jsonl` 显示 `skipped_lock_busy`、退出码为 0 且没有 RID，这属于锁忙误报，不代表刷新失败。先确认没有任何 `refresh.py`、`writeback.py` 或写回进程在运行，再恢复两个计时器并核对下一次触发，然后改用下一次实际运行的轮次签收。2026-11-21 18:35 的轮次就是这个处理例子。

```bash
pgrep -af 'refresh.py|writeback.py|s6_user_apply' || true
systemctl --user start notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer
systemctl --user list-timers --all 'notion-obsidian-refresh*'
```

| 命令组 | 是否改变 Notion | 谁操作 |
|---|---|---|
| writeback 的 status、show、whitelist-check；plan 生成本机计划及预览 | 否 | 用户或按本项目授权的执行者 |
| writeback 的 audit-scope、reject、unreject、discard、restore-deleted、两种 resolve、hold、reconcile、unblock、rebase、ack-backlog、ack-anomaly | 否。若选择恢复、重建、采用或登记，可能重写本地文件或变更审批状态 | 本人；遇到短语或确认提示不代办 |
| writeback 的 apply、续办和 rollback | 会向 Notion 发请求，可能部分生效 | 用户本人在自己的普通终端 |
| refresh 的普通下行刷新、暂停放行/放弃、解除对象暂停；本地发布还原工具 | 下行发布/还原可以改变 Godcreate 和库外状态 | 定时下行由计时器启动。维护窗口外，两项刷新服务均空闲时，你本人可启动一次手动刷新。AI 只有在你提出要求、维护者已宣布窗口并指定它执行时才能启动刷新。 暂停放行、放弃、对象解禁及正式库本地还原由本人执行。 |
| 凭据操作、模式/白名单/允许项、急停启停和扩大共享 | 改变安全范围或本机配置 | 用户本人。系统菜单打开的终端由本人控制 |

刷新退出码 0 可表示已发布、已生成预演或锁忙跳过，须结合 `result` 判读；1 表示失败，2 表示暂停审阅，3 表示本人终端检查拒绝。写回命令退出码 0 表示命令正常结束（需继续看每项结果），1 为失败，2 为受条件或策略拒绝，3 为本人终端检查拒绝，64 为用法错误。未知请求按第 5.3 节记录而非按退出码推断。

<a id="sec-7-2"></a>
### 7.2 维护者参考：三类恢复与升级窗口

**用户平时无需操作本节。** 维护者在部署、工具版本退回或正式库本地还原前宣布编辑冻结开始。所有设备暂停编辑 Godcreate 笔记及对应的 Notion 页面，并退出 Obsidian。首轮验收通过、两只计时器恢复后，维护者宣布编辑冻结结束，再恢复各设备编辑。窗口内只由一个指定执行者操作生产目录。计时器已停时先看执行记录；其他人等首轮验收并恢复调度的记录后再操作。窗口内不另跑刷新、写回、不改配置，也不自行启动计时器。锁仅协调参与协议的程序，不能覆盖人工替换工具和状态文件。计时器正常运行期间，日常查错也不需要改变 Sync 加密设置、插件或远程库名称。变更插件、删除或改名远程库另须用户同意。

**部署单元被停止时。** 维护者从该单元的 journal 核对最后一个安装检查点。SIGKILL、OOM 或超时强制结束时，程序来不及恢复调度。任何停止或杀死单元操作之后，都逐只核对以下状态。安装前失败应确认两只已启用且 active；调度恢复后继续保持编辑冻结，由督导决定是否重试部署，维护者在执行记录写明结论并宣布冻结结束。开始安装后的故障保留停表和备份，按第 7.2 节与部署方案 R1 的步骤交督导处理。

如果日志报告恢复计时器失败，维护者保存两只单元各自的 ActiveState 和下一次触发时间。旧工具回退时若提示本分钟已有快照，等到下一未占用的分钟再按原裁决重试。旧库损坏时，保留原始数据库、附属文件和哈希清单，依部署方案 R2、R3 核实恢复点。用户等候督导通知编辑恢复。

```bash
for t in notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer; do
  systemctl --user show "$t" -p Id -p UnitFileState -p ActiveState -p SubState -p NextElapseUSecRealtime
done
```

**撤销已写入 Notion 的内容**按第 5.4 节，由用户本人在当前新版工具上处理。保留进行过真实写回的计划、回读结果、日志及后续编辑。

**仅撤销一次 Notion → Obsidian 本地发布。** 用户决定是否还原，维护者先按同一刷新编号整理日志和备份。正式库还原由本人执行。窗口内暂停两只计时器，等当前服务退出，确认刷新锁可取得。先运行下列**预览**，核对 `plan`、`n_skipped`、数据库跳过数和影响记录，把已变化或缺备份的项目单独处理。若需实际执行，按另行批准的逐项恢复方案，由本人在无人并发修改的窗口执行本地还原，AI 不得加 `--apply`；该工具本身没有本人确认提示、会自行取得刷新锁，外层检查锁后必须释放再让它取得锁。按逐项结果核对 vault、状态数据库和跳过项，确认后恢复计时器、检查补跑及下一次实际调度。预览只显示计数。维护者按下方命令查询该轮发布日志。若已完成的动作含 ADOPT_WRITEBACK，恢复的是本地采纳前文件和相应基线，Notion 已写内容仍存在，需重做 `status` 和全库 `plan`，逐项查明差异。

```bash
cd /data120/Obsidian/notion-obsidian && read -r -p '输入要评估的刷新编号 ' RUN
python3 -I -B tools/rollback.py --run-id "${RUN:?请先输入刷新编号}" --vault /data120/Obsidian/Godcreate
jq -s '[.[] | select(.phase == "done" and .op == "ADOPT_WRITEBACK")] | length' "reports/$RUN/publish_journal.jsonl"
```

**退回工具版本。** 用户先决定所有已写计划要保留还是回退。仍使用新版工具，先处理有需要的回退、冲突、未知请求和被阻断页面。用状态、每份结果及日志逐项确认没有待处理的 `applying`、未解决的 `partially_applied`、unknown 或 BLOCKED。已留存的历史报告作为证据。换回旧工具后其不再提供写回命令，也不能用恢复旧数据库来撤销 Notion 内容。

1. 维护者在隔离副本先验证工具、字节兼容和真实规模快照；正式窗口在第 3 节核对最近一次发布、调度、磁盘空间。按此次批准的执行记录定位库外 `backup/deploy-<时间>-<随机后缀>/`，可只读列出 `cd /data120/Obsidian/notion-obsidian && ls -d -- backup/deploy-*`。记录原工具、配置、清单、哈希、库内基线和 SQLite 一致备份；校验数据库完整性和表结构。
2. 指定执行者暂停调度，等服务退出并取得锁。只有备份通过校验后才切换。首次新版增量发布会迁移 `export.db` 如新增 `published_files.staged_sha256`，按计划发布时迁移，不提前单独改库。按测试页及其预期范围核对首轮发布、C01–C10、侧栏新鲜度、计划和实际写入；越出预期时保留现场，交督导判读。
3. 需要回到旧版时，保存当时的新工具、配置和状态。恢复**配套的旧工具、配置与兼容的 export.db**。根据部署后有无已接受的发布，分别采用部署前备份或已经单独审核的兼容迁移，保留后来有效的发布记录与用户编辑。旧发布器不能直接写新版七列表。把仅新版创建的状态（如 writeback.db、base 目录等）按部署前清单移到本次恢复目录的 `state/` 留存，保护原先已存在的资料。
4. 按批准步骤交接锁，在计时器仍停时验证旧工具一次手动增量发布；首轮和库内抽查通过后恢复两只计时器，查下一次实际的定时/补跑记录与校验结论。失败时保留备份、原始报告与当前状态，联系督导。调度恢复前由本次执行者负责，不另起进程接管。
5. 平时由刷新程序保留最近 3 轮暂存库和最近 4 份普通自动快照。每份约 436 MiB，4 份合计约 1.7 GiB，正常每日 4 次增量时约为一天的轮次。手动加跑会缩短时间跨度。初始基线、暂停轮次、最新抓取及上次发布指针有独立豁免，实际占用可能更高。由刷新程序在部署后的成功轮次按规则清理，不手动删除生产快照。故障核查时保留相关文件，不手动清理待决计划和备份。升级若要做离线演练，维护者只在完整隔离的工作目录里使用副本的工具、状态和 vault；生产工具即便指定另一 vault 仍可能写生产状态。

**手工检查已完成的 SQLite 快照。** 使用 `mode=ro`。若要核对原始文件字节和时间，请先记录主文件及现存 -wal、-shm 的哈希或时间，再在一致副本上查询。在 WAL 模式下，普通只读查询也可能创建并留下空 -wal 和 -shm，或更新已有共享内存；这些文件单独不能证明主库被写入。运行工具读取已关闭的快照时，WAL 缺失或为零字节且没有待恢复事务，会使用 `mode=ro&immutable=1`。有非空 WAL 时应采用支持 WAL 的 `mode=ro` 一致读取或停止，保留现场。

生产 systemd 单元从 tools 目录以 python3 -I 启动；当前未启用 -B，运行期间可能生成 __pycache__。服务管理器会把退出 2（暂停）或 3（拒绝）显示为 failed。维护者应结合本轮 refresh_status.json 和服务日志核实原因，详见部署方案 D5。

<a id="sec-7-3"></a>
### 7.3 用户决定何时进入下一阶段

单页实机验证及其复核结束后，本人默认把模式改回 `plan` 并用 status 核实。后续各阶段独立授权，没有按时间自动放开写入的机制。

| 阶段 | 用户与维护者掌握的验收记录 |
|---|---|
| 全库只读试运行，至少 7 天 | 用户保持 `plan`，每天运行第 6.1 节的全库计划命令，打开新计划的预览。记下日期、计划号及未改笔记误报数（应为 0）。另与维护者在隔离演练验证离线设备重放、版本历史复原、只删目录同名笔记均有 0 个可执行项；改名大量引用的笔记时只有合格 T 项，无引用页 M 项，连续两轮无重复冲突。记录非交互确认被拒绝、Notion 规范化差异引发页面阻断且无回环的证据。 |
| 白名单写回，至少 7 天 | 只在新授权后添加测试页和最多 10 个叶子白名单页。每次由本人批准，维护者核对写后检查的通过或合理不适用项、回退演练和真实删除经下一轮刷新确认。仅在测试页另行授权演练父页回收后的子页状态、各类提及及访问、静默通知、长文本分段边界。启用允许删除块之前，必须另行完成“测试页删块后恢复，位置与子块一致”的验证。 |
| 逐步扩大 | 本人逐页授权，并另行决定进入 full。每次扩大后保留连续 3 次写后检查通过记录，无待处理的 BLOCKED。 |

**本人新增一个白名单页。** 此步骤只在后续阶段另行批准后使用。选没有子页面或子数据库的页面，在 Notion 页右上角「••• → 连接」添加 Godcreate-Writeback，保留原只读连接。取得页面 ID；在 `plan` 模式按第 4 节的备份和 JSON 检查步骤将其添加到 `whitelist_page_ids`，保留原成员。本人运行以下检查，核对该页显示为叶子、实际端点范围一致（审计 JSON 的 `ok` 为 true）。出现多余对象或缺失页面时先纠正共享，不启用写入；通过后才按第 4 节切换并生成新计划。每次记录获批 ID、检查时间、计划号、结果路径和复核人。离线设备回放和历史恢复由维护者陪同，演练不影响生产计时器。

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py whitelist-check
```

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py audit-scope
```

**成员和额度。** Godcreate 按免费套餐配置，已有内容超过多成员免费额度。增加第二位成员（访客另计）前，请先决定是否升级；达到工作区创建块上限后会进入 3 天宽限期，期满后 Notion 界面和 API 都会受新建限制。看到 `warn_block_limit` 即核对工作区套餐和用量。扩大白名单或认可异常都不会解除额度限制。

| 日期 | 手册变更 | 核验依据 |
|---|---|---|
| 2026-11-21 | 初稿汇总调度、单页人工审批和写后核对；本次审阅补入删除保留、冲突恢复、急停、跨设备排障与维护窗口。 | 项目规格、本机调度记录、首轮提交的工具与隔离演练。正式部署、本人实机审批、设备收敛和长期观察分别由实际记录确认。 |
| 2026-11-21 | 第二轮审查补充单页恢复同步基底、切回预览配置、全端编辑冻结与已知限制。 | 第二轮送审核验清单 `6bfd4856`，该轮未获部署批准。 |
| 2026-11-21 | 第三轮修订明确手动刷新权限、目录笔记冲突对其他页的影响、配置损坏恢复和换终端后的备份复查。 | 第三轮手册条目及隔离配置恢复测试。 |
| 2026-11-21 | S6.3 实机结果补入本手册草稿，并修订 AS-01 至 AS-06、CU-02、CU-04 至 CU-12。实测计划 `wb-20261121T182505-8354` 的 M-001 由用户本人确认并完成，W1、W2、W3、W6、W7、W8、W10 通过，W4、W5、W9 有理由地不适用；复核计划 `wb-20261121T183541-f78c` 为零改动。 | S6-3-182505-8354/live-notion-get.json、reports/writeback/wb-20261121T182505-8354/result.json、reports/writeback/wb-20261121T183541-f78c/plan.json。 |

配置、审批流程、排障和后续维护变更集中更新到本手册。每次变更注明日期、改动、核验依据；单次发布和人工审批留在各自的执行记录中。
