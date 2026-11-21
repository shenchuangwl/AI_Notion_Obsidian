# 故障排查

> 按“现象 → 原因 → 处理”整理的常见问题，来源为[操作手册](../usage/operations-manual.md) §5、§6、§6.4、§7 与部署 Runbook，并对照了 `tools/refresh.py` 的失败说明表（`FAILURE_HELP`）。每一行都给出操作手册中的详细章节。凡是需要挑战短语或 y/N 确认的处理，只能由所有者本人在自己的终端执行，AI 不得代答。返回[文档索引](../README.md)。

**排查前先收集**：轮次编号（如 `20261121T0031-T1`）或写回计划编号（`wb-…`）、`reports/<轮次>/refresh_status.json` 或 `reports/writeback/<计划编号>/result.md`、`logs/refresh_<轮次>.log`、库内 `Notion_Obsidian/运行记录/` 中对应的笔记。对外只提供编号、路径、结论和脱敏片段，**不要发送令牌、挑战短语或无关笔记正文**。

**目录**

- [1. 定时刷新与调度](#tr-refresh)
- [2. 侧栏采集与顶层页面](#tr-sidebar)
- [3. 校验失败与暂停](#tr-verify)
- [4. 冲突、删除与改名](#tr-conflict)
- [5. 写回预览与审批](#tr-writeback)
- [6. 凭据、配置与环境](#tr-env)
- [7. 数据库、快照与磁盘](#tr-disk)

<a id="tr-refresh"></a>
## 1. 定时刷新与调度

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| 历史中出现 `skipped_lock_busy`，没有轮次编号和报告目录 | 另一轮刷新或写回命令持有 `state/refresh.lock`；锁不排队，遇锁直接跳过 | 不是故障。等正在运行的任务结束；下一轮会照常运行。不要删除锁文件 | [手册 §3](../usage/operations-manual.md#sec-3)、[§6.1](../usage/operations-manual.md#sec-6-1) |
| `plan`、`resolve`、`hold`、`rebase` 立即退出并提示锁忙；`apply` 输入短语后才遇锁 | 同上，刷新正在进行 | 等刷新结束后用 `status` 查看；输入短语后遇锁的计划可能已是 `confirmed`，不能再用，需重新生成计划、核对新 SHA 后审批 | [手册 §4.2](../usage/operations-manual.md#sec-4-2)、[§6.1](../usage/operations-manual.md#sec-6-1) |
| 外部验收或监控脚本报告刷新“失败”，但 `refresh_history.jsonl` 显示 `skipped_lock_busy`、退出码 0、没有轮次编号 | 锁忙跳过被误判为失败（另一轮刷新或写回正在运行） | 先确认没有刷新或写回在运行：`pgrep -af 'refresh.py\|writeback.py' \|\| true`；若两个 timer 因此被停，再 `systemctl --user start notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer` 并核对下一次触发，以下一次实际运行的轮次为准 | [手册 §7.1](../usage/operations-manual.md#sec-7-1) |
| `list-timers` 中 timer 不见了或 NEXT 为空 | timer 被停止 / 未启用，或运行部署脚本的 systemd 单元被杀死后来不及恢复调度 | 先查执行记录并保留现场；在维护窗口外、确认无部署 / 回退进行时，用 `systemctl --user start notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer` 恢复。增量服务运行中 NEXT 暂为 `-` 属正常 | [手册 §5.4](../usage/operations-manual.md#sec-5-4)、[§7.2](../usage/operations-manual.md#sec-7-2) |
| 用户注销后定时器不运行 | 未开启 linger | `sudo loginctl enable-linger "$USER"`，`loginctl show-user "$USER" -p Linger` 应为 `yes` | [部署指南 §10](../deployment/deployment-guide.md#dep-10) |
| systemd 显示服务 failed | 刷新退出码 2（整轮暂停）或 3（本人终端检查拒绝）也会被显示为 failed | 结合 `reports/<轮次>/refresh_status.json` 的 `result` 与 `journalctl --user -u notion-obsidian-refresh@incremental.service` 判断 | [手册 §7.2](../usage/operations-manual.md#sec-7-2) |
| 轮次失败：`missing standing approval` | `approvals/standing_daily_refresh.approval.md` 不存在 | 从备份恢复该文件（或按[部署指南 §7.3](../deployment/deployment-guide.md#dep-7) 重新填写授权），等待下一轮 | [部署指南 §7.3](../deployment/deployment-guide.md#dep-7) |
| 轮次失败：`snapshot failed` | 网络或 Notion 接口暂时不可用；或令牌所属工作区不符（日志中有 `ABORT: token workspace mismatch`）；或读令牌缺失 / 无效 | 网络问题等下一轮自动重试；工作区不符见第 6 节；令牌问题按[部署指南 §6](../deployment/deployment-guide.md#dep-6)只检查权限，由所有者更换 | [手册 §6.1](../usage/operations-manual.md#sec-6-1) |
| 轮次失败：`export failed` / `publish plan failed` / `staging verify produced no verify.json` | 导出、计划生成或校验程序出错，本轮没有写库 | 查看 `logs/refresh_<轮次>.log`，下一轮自动重试；连续失败交维护者 | [运维指南 §4](../operations/operations-guide.md#ops-results) |
| 轮次失败：`publish failed`，库内出现 `<轮次>-未完成.md` | 写入库时失败，部分笔记可能已写入（写入前均有备份） | 查看 `reports/<轮次>/publish_journal.jsonl` 与日志，下一轮会重新比较并继续；需要撤销时把发布记录交维护者，用 `tools/rollback.py` 先预览 | [手册 §6.3](../usage/operations-manual.md#sec-6-3)、[§7.2](../usage/operations-manual.md#sec-7-2) |
| 改了 `config/writeback.json` 后每一轮刷新都失败 | JSON 语法损坏、出现未知键或取值非法（刷新和写回都会读取并校验该文件） | 用备份恢复，再运行 `python3 -I -B -m json.tool config/writeback.json >/dev/null && python3 -I -B tools/writeback.py status` | [手册 §4.2](../usage/operations-manual.md#sec-4-2)、[§4.4](../usage/operations-manual.md#sec-4-4) |

<a id="tr-sidebar"></a>
## 2. 侧栏采集与顶层页面

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| `sidebar_fresh=false`，运行记录提示侧栏抓取失败、改用旧证据 | `capture_sidebar.py` 退出码非 0：opencli 桥接不可用、浏览器未登录 Notion、团队协作区顺序不符、某个团队协作区读不到根页面（`no root rendered`）或展开开关未识别 | 已有顶层页照常同步，只是新顶层页暂时不会被发现。保持 Chrome 登录并能看到五个团队协作区，在 WORK 中手动运行 `python3 -I -B tools/capture_sidebar.py` 查看问题；成功时输出证据路径、退出码 0、`order_match: true`。连续 4 次失败后运行记录会升级提醒 | [手册 §3](../usage/operations-manual.md#sec-3)、[§6.1](../usage/operations-manual.md#sec-6-1) |
| 侧栏采集结果 `roots` 为 0 或缺少整个团队协作区 | 该团队协作区在侧栏中折叠且展开失败（采集会中止并只恢复已展开的开关），或 Notion 界面结构变化 | 查看 `evidence/sidebar_<时间>.json` 的 `problems` 与 `aborted`，以及 `.raw.json`；手动在 Notion 中展开该团队协作区后重试。界面结构变化需要维护者调整 `capture_sidebar.py` | [开发者指南 §1](../development/developer-guide.md#dev-layout) |
| 轮次失败：`no usable sidebar evidence` | 本轮采集失败，且 `evidence/` 中从未有过 `order_match=true` 的证据（常见于新部署） | 先修好侧栏采集；首轮也可按[部署指南 §9.2](../deployment/deployment-guide.md#dep-9)用根清单手工快照后 `--use-snapshot` 发布，但定时轮次仍需要采集 | [部署指南 §8](../deployment/deployment-guide.md#dep-8) |
| 手动运行正常、定时运行时采集总是失败 | systemd 用户实例的 `PATH` 中找不到 `opencli`，或定时运行时浏览器桥接不可用 | `systemctl --user show-environment \| grep '^PATH='` 检查；把 `opencli` 所在目录加入用户实例环境，或安装到 `/usr/local/bin`（未验证） | [部署指南 §8](../deployment/deployment-guide.md#dep-8) |
| Notion 新建的顶层页没有出现在库中；运行记录有 `new_roots_unshared` | 该页未共享给读连接，或本轮没有取得新侧栏 | 在页面 “••• → 连接” 添加读连接；在 `Notion_Obsidian/映射清单.md` 按 ID 前 8 位搜索，以新轮次实际发布且库内恰好一个 ID 命中为准 | [手册 §3](../usage/operations-manual.md#sec-3)、[§6.1](../usage/operations-manual.md#sec-6-1) |

<a id="tr-verify"></a>
## 3. 校验失败与暂停

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| 轮次失败：`blocking checks failed: ['C0x', …]` | 暂存库未通过 C01–C10 中的某项（C11 敏感串只警告）。各项：C01 库顶层结构，C02 身份集合，C03 层级，C04 顺序，C05 排序配置，C06 内部链接，C07 附件，C08 frontmatter，C09 文件名，C10 正文完整性 | 本轮没有写库。查看 `reports/<轮次>/verify/verify.md` 与 `verify.json` 的 `status_by_check`、首个失败项；下一轮自动重试，连续两次失败交维护者。库根目录放了其他非隐藏文件会导致 C01 问题 | [手册 §6.3](../usage/operations-manual.md#sec-6-3)、[规格 SPEC](../design/SPEC.md) |
| 结果 `published_post_verify_failed`，库内有 `<轮次>-未完成.md` | 已发布，但发布后复验失败 | 不是暂停轮次，不能也无需放弃。能定位的对象已转为暂停，无法定位的项目会让下一轮整轮暂停等待放行；详情见 `reports/<轮次>/post_publish_verify/verify.md` | [手册 §6.4](../usage/operations-manual.md#sec-6-4) |
| 结果 `paused_for_review`，库内有 `<轮次>-已暂停.md` | 源端改动比例 ≥ `source_change_pause_ratio`、源端消失 ≥ `source_trash_pause_threshold`、新冲突 ≥ `conflict_pause_threshold`、本地新缺失 > `max_local_deletes`、渲染器版本变化、上一轮复验无法定位等 | 阅读暂停笔记后，由本人选择 `--approve-run`（只能放行最近一次暂停）、`--discard-run`，或对大批本地删除用 `writeback.py hold --run`。发布中有人保存过文件时切勿重复放行同一轮 | [手册 §5.2](../usage/operations-manual.md#sec-5-2)、[运维指南 §6](../operations/operations-guide.md#ops-paused) |
| 结果 `published_with_holds`；写回计划里出现 `AUTO_SYNC_HOLD` | 部分对象（或其祖先）被暂停自动同步，原因见 `reports/<轮次>/holds.json` | 扫描类原因在问题消失后的下一轮自动解除；需要人工解除的，问题排除后由本人运行 `refresh.py --release-hold <对象ID>`，再重新生成计划 | [手册 §5.2](../usage/operations-manual.md#sec-5-2)、[§6.3](../usage/operations-manual.md#sec-6-3) |
| 多轮连续暂停，不知道该处理哪一轮 | 较早的暂停轮次已被后续轮次取代 | 先运行 `writeback.py status`，只对最新的 `-已暂停.md` 对应轮次放弃或放行；旧轮有重名歧义时不能放行 | [手册 §6.4](../usage/operations-manual.md#sec-6-4) |

<a id="tr-conflict"></a>
## 4. 冲突、删除与改名

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| `CONFLICT_BOTH`：库内出现 `Notion_Obsidian/conflicts/<轮次>/…/<文件名> (Notion 版本 <轮次>).md` | 同一笔记在 Obsidian 和 Notion 两边都改了；原笔记保留，Notion 版本另存为副本（同一对象同一源端版本只生成一次） | 先把两边需要的文字另存到库外，自行合并后重新预览；产生 C 项时由本人选择 `resolve --take notion`（备份后恢复到上次发布版本，下一轮取 Notion 内容）或 `--take obsidian`（只生成新计划，仍需审批） | [手册 §5.1](../usage/operations-manual.md#sec-5-1) |
| 出现带 `(Conflicted copy …)` 的文件、重复 ID、路径与 ID 不一致 | Obsidian Sync 在设备间产生的冲突副本或映射异常；对应对象会被暂停 | 确认哪个是原件，另存两边内容后合并，保留原件的 `notion_id`，不要把 ID 复制给个人笔记；删除副本前先备份。下一轮报告确认暂停是否解除 | [手册 §5.1](../usage/operations-manual.md#sec-5-1)、[§6.4](../usage/operations-manual.md#sec-6-4) |
| 预览只显示链接拼写变化（`LINK_REWRITE_NOOP`），或父笔记正文与子页改名同时出现 | 工具按对象 ID 识别纯链接改写，状态中跳过该差异；父正文 M 项与子页改名 T 项需分步审批 | 先审批完整的正文项，验收后在原范围重新 `plan`，核对新计划编号、SHA 和剩余 T 项再审批 | [手册 §6.3](../usage/operations-manual.md#sec-6-3) |
| `UNSUPPORTED_RENAME`：拖到另一目录、改文件夹名、改与目录同名的文件 | 当前配置不支持跨文件夹移动，相应对象及子树可能暂停接收更新 | 把文件移回原位并恢复原名，等下一轮重新判定；需要改层级时在 Notion 中操作 | [手册 §5.1](../usage/operations-manual.md#sec-5-1) |
| 在 Obsidian 删除了笔记，Notion 页面没有被删除；或出现“待决删除” | 删除本地文件不会把 Notion 页面移入回收站：`off` 模式下一轮按 Notion 恢复；`plan` / `whitelist` 模式登记为待决删除（默认 14 天） | 用 `writeback.py status` 查看；误删时由本人 `writeback.py restore-deleted --id <对象ID>`，下一轮从 Notion 恢复 | [手册 §5.2](../usage/operations-manual.md#sec-5-2) |
| 一轮内本地大量文件消失导致暂停 | 离线设备重新上线可能重放旧删除；新缺失超过 `max_local_deletes`（默认 5） | 认清受影响文件；普通放行会按 Notion 恢复笔记；确认删除有意时由本人 `writeback.py hold --run <轮次>` 登记待决，再逐项决定 | [手册 §5.2](../usage/operations-manual.md#sec-5-2) |
| `REMOTE_TRASHED_LOCAL_PRESENT` | Notion 已把页面移入回收站，而本地仍有编辑 | 确认源端回收是本人意图并另存本地文字；本应保留时先在 Notion 恢复 | [手册 §5.2](../usage/operations-manual.md#sec-5-2) |
| 目录页在 Notion 改名或移动，同时本地有人编辑了目录笔记，整轮连续失败 | 路径冲突使整轮在发布前失败，其他页面的改动也无法到达 | 约定恢复窗口，全端停止编辑并退出 Obsidian；按手册备份目录笔记（含哈希），在 Notion 恢复原名，再按 A / B 分支处理 | [手册 §6.3](../usage/operations-manual.md#sec-6-3)、[§6.4](../usage/operations-manual.md#sec-6-4) |
| 远端改名时新文件名已被个人笔记占用；旧路径同时属于两篇页面 | 两篇笔记争用同一路径，整轮可能暂停 | 按报告中的两个 `notion_id` 区分对象，分别备份；由本人在 Notion 把占用旧名的同级页改为唯一名称 | [手册 §6.3](../usage/operations-manual.md#sec-6-3)、[§6.4](../usage/operations-manual.md#sec-6-4) |
| 发布过程中有人保存文件（`apply_race`） | 发布可能已部分写入 | 先另存刚保存的笔记，按 `reports/<轮次>/publish_journal.jsonl` 核对实际写入，放弃再次放行同一轮，等下一轮重新扫描 | [手册 §6.3](../usage/operations-manual.md#sec-6-3) |

<a id="tr-writeback"></a>
## 5. 写回预览与审批

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| 计划可执行数为 0，提示 `SYNC_SETTLING`；或本机没有运行 Obsidian | 本机 Obsidian 桌面主进程运行不足 `sync_settle_minutes`（默认 15 分钟），或 120 秒内至少 20 个文件集中变化 | 打开 Obsidian 桌面端和库，`pgrep -x obsidian >/dev/null && echo 运行中 \|\| echo 未运行`；用 `ps -C obsidian -o etime=,args=` 看运行时长，满 15 分钟且 Sync 完成后**重新生成**计划（旧计划不会自动解锁） | [手册 §1](../usage/operations-manual.md#sec-1)、[§6.1](../usage/operations-manual.md#sec-6-1) |
| 首次计划把大量差异列为 `INITIAL_BACKLOG`，可执行数为 0 | 启用写回前已有的本地差异，属于正常的基线行为 | 不批量认可积压；只有确实要写回某一篇的原有改动时，由本人 `writeback.py ack-backlog --path '<库内相对路径>'`，再重新预览 | [手册 §2](../usage/operations-manual.md#sec-2)、[§4.1](../usage/operations-manual.md#sec-4-1) |
| `NO_BASE`、`NOT_EXPORTED`、`RENDERER_MISMATCH` | 该笔记还没有与最近一次成功发布匹配的基线 | 等最近一次成功发布生成基线后再规划 | [手册 §6.1](../usage/operations-manual.md#sec-6-1) |
| `ORACLE_MISMATCH` | 按计划重新导出的结果与本地原文不一致（常见于缩进、表格、颜色或结构调整） | 保留原文字，只做简单的段落文字替换；有改名冲突先按手册 §6.3 处理 | [手册 §6.1](../usage/operations-manual.md#sec-6-1) |
| `UTF8_BOM`、`LINE_ENDINGS` | 其他编辑器改变了编码或换行 | 先把原笔记复制到库外备份，用文本编辑器以 UTF-8 无 BOM、LF 另存后重新预览；避免批量转换个人笔记 | [手册 §6.1](../usage/operations-manual.md#sec-6-1) |
| `STALE_REVERT`，或其他设备同步后笔记突然变旧 | 离线设备重连重放了旧内容或旧删除 | 等 Sync 完成、变化平稳后重新预览；必要时本人切回 `plan`、另存所需差异，只有确实要把全库可处理页面恢复为上次发布版本时才用 `rebase --from-notion` | [手册 §6.1](../usage/operations-manual.md#sec-6-1)、[§6.2](../usage/operations-manual.md#sec-6-2) |
| 改动过多而熔断（超过 `max_modified_notes` / `max_local_deletes` / `max_ops_per_page`） | 写回安全阈值触发 | 先核对所有异常修改；确认整批都属本人后才 `writeback.py ack-anomaly --run <计划编号>`，再生成**全库**计划 | [手册 §6.1](../usage/operations-manual.md#sec-6-1) |
| `mode=off`、有效模式被自动降到 `plan`、权限报错 | 配置缺失（默认 off）、`read_integration_readonly_confirmed_at` 为空、`whitelist_scope` 含 `subtree`、`audit-scope` 发现多余或不可达对象、或读不到 `writeback.db` | 看 `writeback.py status` 中括号内的配置模式和降级原因；修正共享后由本人运行 `audit-scope`，核对通过后按提示解除 | [手册 §6.1](../usage/operations-manual.md#sec-6-1)、[部署指南 §7.1](../deployment/deployment-guide.md#writeback-config) |
| 本人终端检查拒绝（退出码 3） | 管道、非交互运行或带 `CLAUDECODE`、`AI_AGENT`、`CI`、`GITHUB_ACTIONS`、`CLAUDE_CODE_*` 等标记的环境 | 从系统菜单另开普通终端（不是 VS Code 扩展终端或与 AI 共享的会话），由本人完成确认 | [手册 §1](../usage/operations-manual.md#sec-1)、[§6.1](../usage/operations-manual.md#sec-6-1) |
| 锁忙、SHA 不符、计划过期或模式不符，结果为“已拒绝（未发送写请求）” | 计划与当前状态不再一致 | 等刷新结束，重新生成计划并核对新 SHA；`plan` 模式下生成的计划不能用于 `whitelist` | [手册 §4.4](../usage/operations-manual.md#sec-4-4)、[§6.1](../usage/operations-manual.md#sec-6-1) |
| 写回“部分完成”、返回 503、结果未知的请求、BLOCKED 页面 | 请求超时也可能已在 Notion 生效 | 按页面 ID、块 ID、实际文字和时间复核；能证明生效才 `reconcile --decision committed`，能证明未生效才 `not_committed`，无法判定就保留未知，**不要重发** | [手册 §5.3](../usage/operations-manual.md#sec-5-3) |
| 命中免费套餐块额度的 403，`creation_state` 为 `blocked_confirmed` | 工作区达到创建块上限 | 该标记跨轮保留，没有清除它的 CLI；核对套餐与成员后联系维护者处理 | [手册 §6.1](../usage/operations-manual.md#sec-6-1)、[§7.3](../usage/operations-manual.md#sec-7-3) |
| 写回期间曾手动编辑同一 Notion 页面，计划多次判为冲突或 apply 预检中止 | 两端同时修改 | 写回期间不要在 Notion 编辑对应页面；按冲突处理后在干净基线上重做 | [手册 §4.4](../usage/operations-manual.md#sec-4-4) |

<a id="tr-env"></a>
## 6. 凭据、配置与环境

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| `凭据文件权限为 0o…，必须为 0o600` / `凭据目录权限为 …，必须为 0o700` / `不允许符号链接` | 写回令牌文件或目录的权限、属主不符合要求（只针对 `writeback.env`） | 只检查权限：`stat -c '%a %U %n' ~/.config/notion-obsidian/writeback.env ~/.config/notion-obsidian`；由所有者执行 `chmod 600` / `chmod 700`。不要打开、打印或发送文件内容 | [手册 §1](../usage/operations-manual.md#sec-1)、[部署指南 §6](../deployment/deployment-guide.md#dep-6) |
| `NOTION_TARGET_TOKEN 未在 … 中设置` | 读令牌文件缺失、变量名写错或值为空 | 按 [`examples/notion-migrate.env.example`](../../examples/notion-migrate.env.example) 核对变量名，由所有者填写 | [部署指南 §6](../deployment/deployment-guide.md#dep-6) |
| 日志中 `ABORT: token workspace mismatch (<id>)` | 读令牌属于其他 Notion 工作区，`tools/snapshot.py:259` 拒绝运行 | 换成原工作区的读连接令牌；本仓库目前只支持原工作区 | [部署指南 §0](../deployment/deployment-guide.md#dep-0) |
| `ModuleNotFoundError: No module named 'nmig'` | `/data120/Obsidian/notion-migration/nmig` 不存在（导入路径写死，`python3 -I` 忽略 `PYTHONPATH`） | 按[部署指南 §3](../deployment/deployment-guide.md#dep-3) 安装 nmig | [开发者指南 §2](../development/developer-guide.md#dev-env) |
| `ModuleNotFoundError: No module named 'requests'`（或 `yaml`、`markdown_it`），尤其只在定时运行时出现 | 依赖装在用户级 site-packages 或虚拟环境中，而 systemd 用 `/usr/bin/python3 -I` | 用 apt 安装系统包，或把 service 的 `ExecStart` 改为虚拟环境解释器 | [部署指南 §4](../deployment/deployment-guide.md#dep-4) |
| 写回提示 `需要 --vault <库路径>` 或 `库路径不存在或是符号链接` | WORK 不是 `/data120/Obsidian/notion-obsidian`（例如在仓库里直接运行），或库是符号链接 | 在生产 WORK 中运行；库必须是真实目录 | [部署指南 §2](../deployment/deployment-guide.md#dep-2)、[§3](../deployment/deployment-guide.md#dep-3) |
| 发布器 / 回退工具 `ABORT: vault must be /data120/Obsidian/Godcreate` | 指定了其他库 | 演练副本需加 `--allow-other-vault`，并在独立完整的 WORK 中进行 | [开发者指南 §5](../development/developer-guide.md#dev-rehearsal) |

<a id="tr-disk"></a>
## 7. 数据库、快照与磁盘

| 现象 | 原因 | 处理 | 详见 |
|---|---|---|---|
| 需要手工查看 `export.db`、`writeback.db` 或快照 | — | 一律用只读 URI，例如 `python3 -I -B -c 'import sqlite3; c=sqlite3.connect("file:state/export.db?mode=ro", uri=True); print(c.execute("PRAGMA table_info(published_files)").fetchall())'`。不要在生产库上执行写语句，不要手工运行 `wb_state.ensure_export_db`（会迁移 `export.db`，旧工具随后每轮失败） | [手册 §7.2](../usage/operations-manual.md#sec-7-2)、[开发者指南 §3](../development/developer-guide.md#dev-rules) |
| 只读查询后出现空的 `-wal` / `-shm` 文件 | WAL 模式下普通只读查询也可能创建这些文件或更新共享内存 | 单独存在不能证明主库被写入。要核对原始字节和时间时，先记录主文件及现存 `-wal`、`-shm` 的哈希或时间，再在一致副本上查询 | [手册 §7.2](../usage/operations-manual.md#sec-7-2) |
| 校验或导出报告快照 `-wal is not empty; use a finished, checkpointed snapshot` | 快照仍有未检查点的 WAL（写入方未关闭或被中断） | 使用已完成、已检查点的快照；正在写入的快照不要拿来 `--use-snapshot`；有非空 WAL 时保留现场 | [手册 §7.2](../usage/operations-manual.md#sec-7-2) |
| `state/export.db-journal` 存在 | 有被中断的轮次 | 部署 / 回退前遇到时停止操作并调查（Runbook D2），不要删除 | [开发者指南 §6.2](../development/developer-guide.md#dev-runbook) |
| 回退后旧工具每轮失败：`table published_files has 7 columns but 6 values were supplied` | 新版发布器已迁移 `export.db`，旧工具不能直接使用 | 按 Runbook R3 二选一：恢复部署前副本，或删除 `staged_sha256` 列 | [开发者指南 §6.2](../development/developer-guide.md#dev-runbook) |
| 磁盘写满：快照、导出、发布失败，日志中有 `No space left on device` | 快照（每份约 436 MiB）、暂存库和长期不清理的 `reports/`、`backup/` 占满分区；每个未处理的暂停轮次约占 0.8 GB | `df -h /data120/Obsidian` 与 `du -sh` 定位；先处理积压的暂停轮次（放行或放弃后其证据可被清理）。不要手动删除生产快照、暂存库、待决计划或备份；清理 `reports/`、`backup/` 前确认没有待处理事项并记录范围，必要时扩容 | [运维指南 §8](../operations/operations-guide.md#ops-retention)、[手册 §7.2](../usage/operations-manual.md#sec-7-2) |
| 本人想单独撤销一次 Notion → Obsidian 本地发布 | — | 先预览 `python3 -I -B tools/rollback.py --run-id <轮次> --vault /data120/Obsidian/Godcreate`；实际执行没有本人确认提示，只能由所有者在停表、无人并发修改的窗口加 `--apply`，AI 不得运行 | [手册 §7.2](../usage/operations-manual.md#sec-7-2) |
