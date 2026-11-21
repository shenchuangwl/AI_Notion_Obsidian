# 运维指南（日常运行与维护）

> 面向维护者和工作区所有者的日常运维汇总，内容整理自[操作手册](../usage/operations-manual.md) §3、§5、§7 与[部署 / 回退 Runbook](../development/developer-guide.md#dev-runbook)，并对照了 `tools/refresh.py` 等代码。逐步操作与本人审批流程以操作手册为准。下文命令默认在 WORK `/data120/Obsidian/notion-obsidian` 中执行。返回[文档索引](../README.md)。

**目录**

- [1. 职责边界](#ops-roles)
- [2. 调度与锁](#ops-schedule)
- [3. 文件与目录：日志、报告、运行记录在哪里](#ops-files)
- [4. 每轮结果与退出码](#ops-results)
- [5. 日常检查](#ops-daily)
- [6. 暂停轮次与对象暂停：放行、放弃、转待决、解除](#ops-paused)
- [7. 手动刷新](#ops-manual)
- [8. 快照保留与磁盘空间](#ops-retention)
- [9. 备份](#ops-backup)
- [10. 急停与暂停自动同步](#ops-stop)
- [11. 编辑冻结、部署、回退与升级](#ops-freeze)
- [12. 写回相关的运维要点](#ops-writeback)

<a id="ops-roles"></a>
## 1. 职责边界

| 操作 | 是否改变 Notion | 谁可以执行 |
|---|---|---|
| `writeback.py status`、`show`、`whitelist-check`；`plan`（只写本机计划与预览） | 否 | 所有者，或经授权的执行者（含 AI 的只读协助） |
| `writeback.py audit-scope`、`reject`、`unreject`、`discard`、`restore-deleted`、`resolve`、`hold`、`reconcile`、`unblock`、`rebase`、`ack-backlog`、`ack-anomaly` | 否（可能重写本地文件或改变审批状态） | 所有者本人；出现短语或 y/N 提示时不得代答 |
| `writeback.py apply`、`apply --resume`、`rollback` | 是，可能部分生效 | 只能由所有者本人在自己的普通终端执行 |
| 定时刷新 | 否（改变库和 WORK 状态） | 由 systemd 定时器启动 |
| 手动刷新 | 否 | 维护窗口外、两个刷新服务都空闲时，所有者本人可启动；AI 只有在所有者提出、维护者已宣布窗口并指定它执行时才可以 |
| `refresh.py --approve-run`、`--discard-run`、`--release-hold`；`tools/rollback.py --apply` | 否（改变库） | 所有者本人 |
| 凭据、模式、白名单、`allow_*` 开关、急停文件 `state/WRITEBACK_STOP`、扩大共享 | 改变安全范围 | 所有者本人 |

完整说明见[操作手册开头的界限说明](../usage/operations-manual.md#ops-boundaries)和 [§7.1](../usage/operations-manual.md#sec-7-1)。

<a id="ops-schedule"></a>
## 2. 调度与锁

| 定时器 | 触发 | 执行 |
|---|---|---|
| `notion-obsidian-refresh-incremental.timer` | 每天 **00:30、06:30、12:30、18:30**，随机推迟最多 10 分钟 | `refresh.py --mode incremental`：只重读有改动的页面 |
| `notion-obsidian-refresh-full.timer` | 每周日 **05:30**，随机推迟最多 15 分钟 | `refresh.py --mode full`：重读所有页面 |

- 时间按本机时区（生产为 UTC+7）。两个 timer 都有 `Persistent=true`，停机错过的轮次在恢复调度后会补跑。
- 生产上一轮增量约 40 分钟，实际以本轮报告为准；service 超时 6 小时。
- **锁不排队。** 刷新与写回共用 `state/refresh.lock`：刷新遇锁记录 `skipped_lock_busy` 并退出（退出码 0，没有轮次编号和报告目录）；`plan`、`resolve`、`hold`、`rebase` 等遇锁立即退出。写回另有 `state/writeback.lock`。不要删除锁文件。
- 安排写回审批时，避开每次增量开始后约一小时，例如 08:00 或 14:00 前后，先确认两个服务都已结束。

```bash
systemctl --user list-timers --all 'notion-obsidian-*' --no-pager
systemctl --user show -p Id -p ActiveState -p SubState notion-obsidian-refresh@incremental.service notion-obsidian-refresh@full.service
```

增量服务运行时，对应 timer 的 NEXT 会暂时显示 `-`，可用 `systemctl --user show notion-obsidian-refresh-incremental.timer -p SubState -p NextElapseUSecRealtime` 确认 `SubState=running`。

<a id="ops-files"></a>
## 3. 文件与目录：日志、报告、运行记录在哪里

**WORK（库外，`/data120/Obsidian/notion-obsidian`）**

| 位置 | 内容 |
|---|---|
| `logs/refresh_history.jsonl` | 每轮一行：`run_id`、`mode`、`result`、`finished` / `at` 等；手动放行、放弃等操作也会写入 |
| `logs/refresh_<轮次>.log` | 该轮全部子工具的输出 |
| `reports/<轮次>/refresh_status.json` | 本轮状态汇总：结果、侧栏是否新鲜（`sidebar_fresh`）、成员数、源端变化、扫描计数、校验结果、暂停对象等 |
| `reports/<轮次>/run_record.md` | 新版轮次的可读运行记录 |
| `reports/<轮次>/verify/verify.json`、`verify_retry/`、`post_publish_verify/` | 发布前校验、重验（如有，以它为准）、发布后复验 |
| `reports/<轮次>/publish_plan.json`、`publish_journal.jsonl` | 发布计划与逐条发布日志（回退工具依据它撤销） |
| `reports/<轮次>/holds.json`、`wb_scan.json`、`expected_deviations.json`、`trash_archive.json` | 对象级暂停、库扫描完整结果、预期偏差、源端消失归档 |
| `reports/writeback/<计划编号>/` | 写回的 `plan.json`、`preview.md`、`result.md`、`result.json` |
| `state/` | `export.db`（发布状态）、`writeback.db` 与 `base/`（写回基线）、锁文件、`latest_snapshot.txt`、`last_published*.{txt,json}`、`auto_sync_hold.json`、`paused_runs.json`、`rate_gate.json`、`roots_<时间>.json`、急停文件 `WRITEBACK_STOP` 等 |
| `snapshots/snap_<时间>.db`（及 `.roots.json`）、`snapshots/wb/` | 只读快照；写回后的定向重抓快照 |
| `staging/<轮次>/` | 导出的暂存库 `vault/`、清单与导出报告 |
| `evidence/sidebar_<时间>.json` | 侧栏采集证据（`.raw.json` 为原始数据） |
| `blobs/<sha256>` | 按内容寻址的附件缓存 |
| `backup/vault-before/<轮次>/` | 发布时被替换的本地文件备份 |
| `backup/writeback-restore/<时间>/` | 采用 Notion 版本、拒绝并恢复、批量重建时被替换的本地文字 |
| `backup/deploy-*`、`backup/rollback-*` | 部署 / 回退时的工具与数据库备份 |
| `rollback/<轮次>/` | `tools/rollback.py --apply` 移出的文件（从不直接删除） |
| `approvals/` | 长期授权 `standing_daily_refresh.approval.md`、放行记录 `approve-run-<轮次>.approval.md`、写回确认 `writeback/` |
| `config/writeback.json` | 写回配置（键说明见[部署指南 §7.1](../deployment/deployment-guide.md#writeback-config)） |

**库内（`/data120/Obsidian/Godcreate/Notion_Obsidian/`）**

| 位置 | 内容 |
|---|---|
| `运行记录/` | 每轮摘要；整轮暂停时有 `<轮次>-已暂停.md`，发布了部分文件后失败时有 `<轮次>-未完成.md` |
| `conflicts/<轮次>/` | 双方都修改时保留的 Notion 版本副本 |
| `写回预览/` | 写回计划的可读预览 |
| `映射清单.md` | Notion ID 与库内路径的对照，可按 ID 前 8 位搜索 |
| `sortspec.md` | custom-sort 排序规则 |

查看某一轮的详细记录：

```bash
read -r -p '请输入要查看的刷新轮次编号：' RUN
if test -f "reports/${RUN:?请先输入轮次}/run_record.md"; then less "reports/$RUN/run_record.md"; else less "reports/$RUN/refresh_status.json"; fi
```

<a id="ops-results"></a>
## 4. 每轮结果与退出码

| `result` | 含义与下一步 |
|---|---|
| `published` | 正常发布。按笔记路径或映射清单抽查即可 |
| `published_with_holds` | 其余对象已发布，部分对象被暂停。查 `reports/<轮次>/holds.json`、运行记录和 `Notion_Obsidian/conflicts/<轮次>/`（如有） |
| `planned` | `--dry-run` 预演在发布前结束 |
| `skipped_lock_busy` | 另一轮或写回正在运行，本次跳过，不排队；只有时间，没有编号和报告目录 |
| `paused_for_review` | 整轮暂停待审阅，见第 6 节。发布中有人保存文件时可能已部分写入，切勿重复放行同一轮 |
| `failed` | 失败。看报告中的错误、日志，以及有无 `<轮次>-未完成.md`（有则说明已发布过部分文件） |
| `published_post_verify_failed` | 已发布，但发布后复验失败；不是暂停轮次，不能也无需放弃。复验中能定位的问题会转为对象暂停，无法定位的会让下一轮暂停 |

| 命令 | 退出码 |
|---|---|
| `refresh.py` | 0 已发布 / 预演 / 遇锁跳过（需结合 `result`）；1 失败或取消；2 整轮暂停；3 本人终端检查未通过 |
| `writeback.py` | 0 命令完成（仍需看每项结果）；1 失败或取消；2 受模式、计划或状态拒绝；3 本人终端检查未通过；64 参数错误 |

systemd 会把刷新的退出码 2 和 3 显示为 failed，请结合 `refresh_status.json` 与 `journalctl --user -u notion-obsidian-refresh@<模式>.service` 判断原因。结果未知的写回请求按[操作手册 §5.3](../usage/operations-manual.md#sec-5-3)核对，不能按退出码推断。

<a id="ops-daily"></a>
## 5. 日常检查

每天看一次：两个 timer 的 LAST / NEXT、最近三轮的编号 / 模式 / 结果，以及库内 `Notion_Obsidian/运行记录/` 是否出现新的 `-已暂停.md` 或 `-未完成.md`。每周另核对一次周日全量轮次。

```bash
systemctl --user list-timers --all 'notion-obsidian-refresh*' --no-pager
cd /data120/Obsidian/notion-obsidian && tail -n 3 logs/refresh_history.jsonl | jq -r '[(.run_id // "-"),.mode,.result,(.finished // .at // "-")] | @tsv'
```

查看最近一次有编号的实际刷新及其侧栏证据（跳过放弃等操作记录）：

```bash
jq -s '[.[] | select(.run_id != null and .action == null and has("sidebar_fresh"))] | last | {run_id,result,sidebar_fresh,sidebar}' logs/refresh_history.jsonl
```

`sidebar_fresh=false` 表示本轮侧栏采集失败、沿用了旧证据，新顶层页可能尚未被发现；连续 4 次失败（约一天）后运行记录会升级提醒。检查写回状态（只读）：

```bash
python3 -I -B tools/writeback.py status
```

<a id="ops-paused"></a>
## 6. 暂停轮次与对象暂停：放行、放弃、转待决、解除

**整轮暂停（`paused_for_review`）** 的常见原因：源端改动比例或消失数量超过阈值、本轮新冲突过多、同一轮新缺失的本地文件超过 `max_local_deletes`、渲染器版本变化、上一轮复验失败且无法定位。先打开库内 `Notion_Obsidian/运行记录/<轮次>-已暂停.md` 核对原因、对象和路径，再由所有者本人在自己的终端选择一条（每条都要求 y/N 确认）：

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/refresh.py --approve-run '普通暂停的刷新编号'   # 放行：发布暂停时评审过的同一计划
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/refresh.py --discard-run '普通暂停的刷新编号'   # 放弃：不写库
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/writeback.py hold --run '本地大批删除导致暂停的刷新编号'   # 把本轮本地删除转为待决删除
```

- 只能放行**最近一次**暂停的轮次；暂停后库内又有改动、计划不同时，放行会被拒绝，此时放弃并等待下一轮。
- 多轮暂停时，先运行 `writeback.py status`，再对最新的 `-已暂停.md` 对应轮次操作；较早的轮次已被取代。
- 暂停轮次的证据（快照、暂存库）在放行或放弃之前不会被清理，每个暂停轮次约占 0.8 GB。

**对象暂停（hold）**：单个对象及其子树暂时停止自动同步，其他对象照常发布。原因见 `reports/<轮次>/holds.json`。扫描类原因（如 Sync 冲突副本、重复 ID）在问题消失后的下一轮自动解除；需要人工解除的，由所有者在问题排除后执行：

```bash
cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/refresh.py --release-hold '已排除问题的对象ID'
```

各类冲突和删除的判断方法见[操作手册 §5](../usage/operations-manual.md#sec-5)与[故障排查](../troubleshooting/troubleshooting.md)。

<a id="ops-manual"></a>
## 7. 手动刷新

先确认两个服务都是 `ActiveState=inactive`，再运行：

```bash
python3 -I -B tools/refresh.py --mode incremental            # 立即拉取变化（会更新正式库）
python3 -I -B tools/refresh.py --mode full                   # 完整重读（会更新正式库）
python3 -I -B tools/refresh.py --mode incremental --dry-run  # 实时预演：不发布，但仍写侧栏证据、快照、暂存、日志和报告
```

离线演练请在独立完整的工作副本中进行（见[开发者指南 §5](../development/developer-guide.md#dev-rehearsal)）：生产工具即使改了 `--vault`，仍会使用自己 WORK 下的状态。

<a id="ops-retention"></a>
## 8. 快照保留与磁盘空间

刷新程序在每轮结束时自动清理（`refresh.py` 的 `prune()`，`--dry-run` 和缺少长期授权的轮次不清理）：

| 对象 | 保留规则 |
|---|---|
| 普通快照 `snapshots/snap_*.db` | 保留最新 4 份（`KEEP_SNAPSHOTS = 4`）。每份约 436 MiB，4 份合计约 1.7 GiB，正常每日 4 次增量时约为一天的轮次；手动加跑会缩短时间跨度 |
| 豁免的快照 | 最新完成的快照、上次发布的快照、基线快照、暂停轮次（及最多 3 个被取代的暂停轮次）的快照 |
| 暂存库 `staging/<轮次>/vault` | 保留最新 3 轮（`KEEP_STAGING = 3`），更早的删除 `vault/`、`block_lines.jsonl` 和 `page_json.jsonl`（上次发布轮次的 `page_json.jsonl` 保留） |
| 写回重抓快照 `snapshots/wb/` | 7 天后删除（下一次重抓所依据的快照除外） |
| `state/writeback.db` | 每个对象只保留最近 2 个基线版本的行映射；空闲页超过 25% 时最多每 7 天 VACUUM 一次 |
| `reports/`、`logs/`、`backup/`、`evidence/`、`blobs/` | **不会自动清理** |

**不要手动删除生产快照、暂存库、待决计划或备份**，故障核查时更要保留现场。磁盘检查：

```bash
df -h /data120/Obsidian
du -sh /data120/Obsidian/notion-obsidian/{snapshots,staging,state,blobs,backup,reports,logs}
```

生产环境 2026-11-21 的实测约为：快照 2.6 GB、暂存 1.0 GB、报告 333 MB、状态 253 MB、备份 216 MB、附件 53 MB。磁盘写满会导致快照、导出或发布失败，处理方法见[故障排查](../troubleshooting/troubleshooting.md#tr-disk)。需要清理 `reports/` 或 `backup/` 时，先确认没有待处理的暂停轮次、写回计划或事故核查，并由维护者记录清理范围。

<a id="ops-backup"></a>
## 9. 备份

工具自动产生的备份：

- 每次发布前被替换的本地文件：`backup/vault-before/<轮次>/`（`tools/rollback.py` 依据它撤销一次发布）。
- 写回中被替换的本地文字：`backup/writeback-restore/<时间>/`；同时保存冲突：`backup/writeback/<计划编号>/race/`。
- 部署 / 回退：`backup/deploy-<时间>/`、`backup/rollback-<时间>/`（含 `export.db` 一致备份与 SHA256）。

建议另做的备份（建议做法，频率由维护者决定）：

- `config/`、`approvals/`、`evidence/`：直接复制。
- `state/export.db`、`state/writeback.db`：在两轮之间、确认 `flock -n state/refresh.lock true` 成功后，用 SQLite 在线备份，不要直接复制正在使用的数据库文件。可沿用 Runbook D3 的命令：

  ```bash
  B=backup/manual-$(date +%Y%m%dT%H%M); mkdir "$B"
  python3 -I -B -c 'import sqlite3,sys; s=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); print(d.execute("PRAGMA integrity_check").fetchone()[0]); d.close(); s.close()' state/export.db "$B/export.db"
  sha256sum "$B/export.db" > "$B/SHA256SUMS"
  ```

- 库本身由 Obsidian Sync 的版本历史与各设备副本保护；工具从不删除库内文件。
- **不要**把 `~/.config/notion-migrate/.env`、`~/.config/notion-obsidian/writeback.env` 放进任何共享备份或仓库。

手工查看 SQLite 时一律使用 `mode=ro`；在 WAL 模式下，只读查询也可能留下空的 `-wal` / `-shm`，它们本身不能证明主库被写入（见[操作手册 §7.2](../usage/operations-manual.md#sec-7-2)）。

<a id="ops-stop"></a>
## 10. 急停与暂停自动同步

| 目的 | 操作（所有者本人） | 影响 |
|---|---|---|
| 停止后续写回请求 | `cd /data120/Obsidian/notion-obsidian && touch state/WRITEBACK_STOP` | 阻止写入、续办和回退；核对未知请求、解除页面阻断仍可进行；下行刷新照常 |
| 解除急停 | 确认原因已排除后 `rm -- state/WRITEBACK_STOP`，再用 `writeback.py status` 核对 | — |
| 暂停下行同步（事故时） | `systemctl --user stop notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer` | 已在运行的服务可能仍在工作 |
| 恢复下行同步 | `systemctl --user start notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer` | 错过的轮次可能立即补跑 |
| 撤销远端写权限 | 在 Notion 测试页 “••• → 连接” 移除写回连接，或在开发者门户撤销该集成 | 不会逆转已完成的写入；保留只读连接 |

平时保持两个 timer 开启。发现 timer 被停时，先查执行记录，保留当前状态，不要擅自启动。

<a id="ops-freeze"></a>
## 11. 编辑冻结、部署、回退与升级

- **编辑冻结。** 部署、工具版本回退或正式库本地还原前，维护者宣布编辑冻结：所有设备暂停编辑 Godcreate 笔记和对应的 Notion 页面，并退出 Obsidian。窗口内只有一位指定执行者操作 WORK，不另跑刷新、写回，不改配置，也不自行启动 timer。首轮验收通过、两个 timer 恢复后，维护者宣布冻结结束。
- **部署 / 升级。** 必须按 Runbook D1–D5 执行：`tools/` 与 `state/export.db` 一起部署、一起回退。摘要见[部署指南 §12](../deployment/deployment-guide.md#dep-12)，完整命令见[开发者指南 §6.2](../development/developer-guide.md#dev-runbook)。
- **回退。** 按 R1–R5 执行；回退不会撤销已写入 Notion 的内容，有写回计划时先用新版工具处理。
- **撤销一次本地发布。** 先预览：`python3 -I -B tools/rollback.py --run-id "$RUN" --vault /data120/Obsidian/Godcreate`，核对计划和跳过数；实际执行（加 `--apply`）没有本人确认提示，只能由所有者在无人并发修改的窗口执行，AI 不得运行。详见[操作手册 §7.2](../usage/operations-manual.md#sec-7-2)。
- **运行部署脚本的 systemd 单元（部署单元）被停止或杀死后**，逐个核对两个 timer 的状态：

  ```bash
  for t in notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer; do
    systemctl --user show "$t" -p Id -p UnitFileState -p ActiveState -p SubState -p NextElapseUSecRealtime
  done
  ```

<a id="ops-writeback"></a>
## 12. 写回相关的运维要点

- 生产配置为 `plan` 模式，白名单只有一个测试页；进入 `whitelist` 和批准写入都由所有者本人决定，验收后改回 `plan`。
- 计划默认 24 小时过期，不能跨模式使用；遇到锁、SHA 不符、过期或模式不符时重新生成计划。
- 写回前确认本机 Obsidian 桌面端已运行满 15 分钟且 Sync 完成，否则可执行项为 0（`SYNC_SETTLING`）。
- 写后检查 W1–W10 应为 `pass` 或有理由的 `n/a`；出现“部分完成”“结果未知的请求”“BLOCKED 页面”时按[操作手册 §5.3](../usage/operations-manual.md#sec-5-3)处理，不要重发。
- 后续扩大范围（全库只读试运行 ≥ 7 天、白名单写回 ≥ 7 天、逐步扩大）的验收要求见[操作手册 §7.3](../usage/operations-manual.md#sec-7-3)。
- 看到 `warn_block_limit` 时核对工作区套餐和成员数：免费工作区超额后有 3 天宽限期，期满后界面和 API 都不能新建内容。
