# 部署指南：在一台全新的 Ubuntu 机器上部署

> 面向要在新机器上独立部署本工具的开发者或维护者。按顺序完成第 1–10 节即可得到一套能定时运行的 Notion → Obsidian 同步，写回保持关闭；启用写回预览与审批见第 11 节。命令均已对照 `notion-obsidian/tools/` 的代码与 `--help` 核对，未能在全新机器上实测的步骤会标注“未验证”。返回[文档索引](../README.md)。

**目录**

- [0. 部署前须知](#dep-0)
- [1. 先决条件](#dep-1)
- [2. 固定路径与写死的常量](#dep-2)
- [3. 获取代码并安装到固定路径](#dep-3)
- [4. 安装 Python 依赖](#dep-4)
- [5. 创建 Notion 集成并共享页面](#dep-5)
- [6. 配置令牌文件](#dep-6)
- [7. 写回配置、根清单与长期授权文件](#dep-7)
- [8. 准备 Obsidian 库与侧栏采集](#dep-8)
- [9. 首次快照、导出、校验与发布](#dep-9)
- [10. 安装 systemd 定时器并验证首轮](#dep-10)
- [11. （可选）启用写回预览](#dep-11)
- [12. 生产升级与回退 Runbook 摘要](#dep-12)
- [13. 部署检查清单](#dep-13)

<a id="dep-0"></a>
## 0. 部署前须知

- **本仓库当前只服务原 Godcreate 工作区。** `tools/snapshot.py:259` 会核对令牌所属的 Notion 工作区 ID，不一致就以 `ABORT: token workspace mismatch` 退出；五个团队协作区名称也写死在多个模块中（第 2 节）。在同一工作区重新部署（例如换一台机器）不需要改代码；接入**其他工作区**必须修改这些常量，这种用法未经测试。
- **路径是固定的。** 工具默认工作目录（下文称 WORK）为 `/data120/Obsidian/notion-obsidian`，库为 `/data120/Obsidian/Godcreate`，`nmig` 从 `/data120/Obsidian/notion-migration` 导入。推荐照原样创建这些路径（第 3 节）。
- **角色。** “所有者 / 本人”是 Notion 工作区与 Obsidian 库的主人，负责凭据、模式、白名单和全部写回审批；“维护者”负责安装、升级与排障。AI 助手只能运行只读命令，不得运行 `writeback.py apply`、`rollback`、`tools/rollback.py --apply` 等改变 Notion 或正式库的操作，也不得读取或转述令牌与挑战短语（详见[操作手册开头的界限说明](../usage/operations-manual.md#ops-boundaries)）。
- **两种场景。** 本文描述**全新部署**（空库、空状态）。若要把一套正在运行的部署**迁移**到新机器，建议在编辑冻结窗口内停表，把旧机器上的整个 WORK（`config/ state/ snapshots/ blobs/ evidence/ approvals/ backup/ reports/ logs/`）和库一起迁移，而不是在已有笔记的库上重新首发：新状态库不知道这些笔记是工具发布的，发布器会把同路径的已有文件判为覆盖风险并暂停对应对象。迁移流程未在本仓库中验证，请先在隔离副本演练。

<a id="dep-1"></a>
## 1. 先决条件

| 类别 | 要求 | 生产实测版本 / 说明 |
|---|---|---|
| 操作系统 | Linux，带 systemd 用户实例 | Ubuntu 24.04.5 LTS，systemd 255；其他发行版未验证 |
| 账户 | 一个普通用户（下文用 `$USER`），部署时需要 sudo（§3 建目录、§4 apt 安装、§10 开启 linger） | 生产为 `ubuntu`；令牌文件必须属于运行工具的用户 |
| Python | 3.12 | 3.12.3；SQLite 3.45.1（需 ≥ 3.35） |
| Python 依赖 | `requests>=2.31`、`PyYAML>=6.0`、`markdown-it-py>=3.0`（测试另需 `pexpect>=4.9`） | Ubuntu 24.04 系统包 |
| 命令行工具 | `git`、`jq`（手册里的查询命令用到）、`flock`（util-linux，Runbook 用到） | — |
| Obsidian | 桌面版，同一用户登录图形会话 | 1.15.1；写回计划要求本机桌面主进程已运行满 15 分钟 |
| Obsidian 插件 | custom-sort 3.2.0（按 Notion 顺序显示文件） | 见[custom-sort 核实记录](../reference/obsidian-custom-sort.md) |
| Obsidian Sync | 可选，多设备时使用 | 生产连接远程库 “Godcreate” |
| 侧栏采集 | Chrome + `opencli` 浏览器桥接，浏览器已登录 Notion 工作区 | `@jackwener/opencli` 1.8.6（源码构建），Node.js v24；安装见第 8 节，以其上游文档为准，本仓库未验证 |
| Notion | 工作区管理员权限（创建内部集成、共享页面） | API 版本 `2026-03-11` |
| 磁盘 | 建议 WORK 所在分区至少 10 GB 可用 | 生产 WORK 约 4.5 GB：快照 2.6 GB（单份约 436 MiB，保留 4 份 + 基线）、暂存 1.0 GB、状态 0.25 GB |
| 时区 | 定时器按本机时区触发 | 生产为 UTC+7 |

<a id="dep-2"></a>
## 2. 固定路径与写死的常量

下表列出代码中写死的环境相关常量。照原样部署时都不需要修改；只有改路径或换工作区时才需要逐一修改并重新测试（未验证）。

| 常量 | 位置（文件:行） | 当前值 | 作用 |
|---|---|---|---|
| `NMIG` | `notion-obsidian/tools/snapshot.py:28`、`tools/assets_fetch.py:21`、`tools/readonly_client.py:36` | `/data120/Obsidian/notion-migration` | 插入 `sys.path` 后导入 `nmig`；`python3 -I` 忽略 `PYTHONPATH`，无法用环境变量替代 |
| 生产 WORK | `tools/wb_common.py:143`、`tools/writeback.py:67`、`tools/wb_apply.py:168` | `/data120/Obsidian/notion-obsidian` | 只有在这个 WORK 下，写回才默认使用正式库；其他 WORK 必须显式 `--vault` |
| 正式库 | `tools/wb_common.py:21`（`refresh.py:60` 引用）、`tools/publish_vault.py:93`、`tools/rollback.py:52`、`tools/rehearse.py:19`、`tools/plugin_bundle.py:18` | `/data120/Obsidian/Godcreate` | 刷新默认库；发布器与回退工具拒绝其他库，除非加 `--allow-other-vault` |
| 工作区 ID 守卫 | `tools/snapshot.py:259` | 原 Godcreate 工作区 ID | 令牌属于其他工作区时快照中止 |
| 团队协作区列表 | `tools/capture_sidebar.py:41`、`tools/export_obsidian.py:59`、`tools/wb_scan.py:73`、`tools/verify_vault.py:87` | General / Quantitative Investing / x_to_notion / Godcreate Business / Godcreate AI工作空间 | 侧栏顺序、库顶层目录和 C01 校验 |
| 侧栏起始页 | `tools/capture_sidebar.py:43` | General 团队主页的 Notion URL | 采集时打开的页面 |
| 浏览器会话名 | `tools/capture_sidebar.py:42` | `nmig_ro_sidebar` | `opencli browser <会话>` 的默认会话 |
| 基线快照名 | `tools/refresh.py:65` | `snap_20261117T0145` | 清理时永不删除该名称的快照；新部署中不存在，无影响 |
| 库内说明文字 | `tools/export_obsidian.py:80`、`:88`、`:100` | 生产路径与操作手册路径 | 写进库内 `Notion_Obsidian/README.md` 的文字，只影响显示 |
| systemd 单元 | `notion-obsidian/deploy/notion-obsidian-refresh@.service` | `WorkingDirectory` 与 `ExecStart` 指向生产路径，解释器 `/usr/bin/python3 -I` | 定时刷新 |

此外，`tools/refresh.py:85` 的侧栏修复提示里写的会话名是 `wb_dev_sidebar2`，与 `capture_sidebar.py` 的默认值 `nmig_ro_sidebar` 不同，以后者为准。

<a id="dep-3"></a>
## 3. 获取代码并安装到固定路径

```bash
# 1) 克隆仓库到任意位置，作为源码与升级来源
git clone https://github.com/shenchuangwl/AI_Notion_Obsidian.git ~/src/AI_Notion_Obsidian
cd ~/src/AI_Notion_Obsidian && git log -1 --oneline

# 2) 创建固定目录（需要 sudo）
sudo install -d -o "$USER" -g "$USER" /data120/Obsidian
mkdir -p /data120/Obsidian/notion-obsidian/tools /data120/Obsidian/notion-migration

# 3) 安装工具（只复制模块，不带 tests/）、systemd 单元模板与 nmig
cp -p ~/src/AI_Notion_Obsidian/notion-obsidian/tools/*.py /data120/Obsidian/notion-obsidian/tools/
cp -a ~/src/AI_Notion_Obsidian/notion-obsidian/deploy /data120/Obsidian/notion-obsidian/
cp -a ~/src/AI_Notion_Obsidian/notion-migration/nmig /data120/Obsidian/notion-migration/

# 4) 检查
ls /data120/Obsidian/notion-obsidian/tools | wc -l      # 期望 28
ls /data120/Obsidian/notion-migration/nmig              # __init__.py client.py config.py rest_inventory.py
```

为什么复制而不用符号链接：

- WORK 由 `os.path.abspath(__file__)` 推出，不解析符号链接。若 `tools/` 是指向仓库的链接，在 `tools/` 目录里用相对路径运行脚本（如 `cd tools && python3 refresh.py`）时，WORK 会变成仓库里的路径，状态会写到错误的位置。
- 升级 Runbook（第 12 节）用 `cp -a tools "$B/tools"` 备份当前工具；对符号链接只会备份链接本身，起不到备份作用。
- WORK 必须是真实目录，库 `/data120/Obsidian/Godcreate` 也必须是真实目录：写回工具拒绝符号链接形式的库。

<a id="dep-4"></a>
## 4. 安装 Python 依赖

**方式 A（推荐，与生产一致）**：systemd 单元用 `/usr/bin/python3 -I` 启动，`-I` 会忽略用户级 site-packages，所以依赖要装在系统 Python 中。Ubuntu 24.04 的系统包版本满足 `requirements.txt`：

```bash
sudo apt update
sudo apt install python3 python3-requests python3-yaml python3-markdown-it python3-pexpect git jq util-linux
/usr/bin/python3 -I -c 'import requests, yaml, markdown_it, sqlite3; print("deps ok", sqlite3.sqlite_version)'
```

**方式 B（虚拟环境，Ubuntu 上需先 `sudo apt install python3-venv`）**：

```bash
python3 -m venv ~/.venvs/notion-obsidian
~/.venvs/notion-obsidian/bin/pip install -r ~/src/AI_Notion_Obsidian/requirements.txt
```

采用方式 B 时，后续所有命令里的 `python3` 都要换成 `~/.venvs/notion-obsidian/bin/python3`，并把第 10 节复制到 `~/.config/systemd/user/` 的 service 文件中 `ExecStart=` 的 `/usr/bin/python3` 改成同一个解释器。Ubuntu 24.04 的系统 Python 受 PEP 668 保护，直接 `pip install` 会被拒绝；`pip install --user` 装的包又会被 `-I` 忽略，所以不要用这两种方式。

工具在帮助、运行记录和暂停笔记中打印的命令一律写作 `python3 -I tools/…`（例如 `wb_common.cli_command` 生成的修复提示）。采用方式 B 时，先 `source ~/.venvs/notion-obsidian/bin/activate` 让 `python3` 指向虚拟环境，或把提示中的 `python3` 换成虚拟环境解释器再运行；否则会用到没有依赖的系统解释器。

<a id="dep-5"></a>
## 5. 创建 Notion 集成并共享页面

在 Notion 开发者门户的集成管理页（规格中记录的地址为 `https://app.notion.com/developers/connections`）创建内部集成。两个集成的权限要分开，规格依据见 [WRITEBACK_SPEC §2](../design/WRITEBACK_SPEC.md)。

| 项目 | 读连接（必需） | 写回连接（仅启用写回时） |
|---|---|---|
| 建议名称 | `Godcreate` | `Godcreate-Writeback` |
| 能力（Capabilities） | **只勾选** Read content；另勾选 Read user information without email addresses（用于成员数监控，不勾选时每轮都会出现成员数未知的额度警告）；不勾选 Update / Insert content 与评论 | Read content、Update content、Insert content；不勾选评论和用户信息 |
| 共享范围 | 五个团队协作区中要同步的每个顶层页面：页面右上角 “••• → 连接 → 添加” | **只**共享测试用的叶子页面（正文任何深度都没有子页、子数据库或内联数据库）。共享父页会把全部后代一起授权 |
| 令牌变量 | `NOTION_TARGET_TOKEN` | `NOTION_WRITEBACK_TOKEN` |

注意：

- 新增的顶层页面必须共享给读连接，刷新才会纳入；未共享的顶层页面会在运行记录里以 `new_roots_unshared` 提示。
- 读连接在 Notion 中确认只读后，由所有者把确认时间写入 `config/writeback.json` 的 `read_integration_readonly_confirmed_at`（第 11 节）。不要靠发送写请求来“测试”它是否只读。
- 不要用读连接运行 `notion-migrate` 的 `relink`、`repair`、`cleanup` 等写命令，也不要用其他脚本绕开写回审批。

<a id="dep-6"></a>
## 6. 配置令牌文件

令牌只放在用户目录的两个文件中，绝不进入仓库、命令行参数、日志或聊天。下面用 `install` 从示例创建文件，再用编辑器把占位符替换成真实值（不要用 `echo` 写令牌，以免留在 shell 历史里）：

```bash
cd ~/src/AI_Notion_Obsidian
install -d -m 700 ~/.config/notion-migrate ~/.config/notion-obsidian
install -m 600 examples/notion-migrate.env.example ~/.config/notion-migrate/.env
nano ~/.config/notion-migrate/.env                      # 填入读连接令牌

# 仅在启用写回时：
install -m 600 examples/writeback.env.example ~/.config/notion-obsidian/writeback.env
nano ~/.config/notion-obsidian/writeback.env            # 填入写回连接令牌

# 只检查权限，不显示内容
stat -c '%a %U %n' ~/.config/notion-migrate/.env ~/.config/notion-obsidian/writeback.env ~/.config/notion-obsidian
```

期望前两行为 `600 <你的用户名> …`，第三行为 `700 <你的用户名> …`。

| 文件 | 变量 | 读取方与规则 |
|---|---|---|
| `~/.config/notion-migrate/.env` | `NOTION_TARGET_TOKEN`（必需）、`NOTION_VERSION`（可选，默认 `2026-03-11`） | `tools/notion_tokens.py`（refresh、writeback 的读取）与 `notion-migration/nmig/config.py`（snapshot、assets_fetch）。同名环境变量优先于文件。`NOTION_MIGRATE_ENV_FILE` 只改变 nmig 读取的文件，`notion_tokens.py` 仍读默认路径，所以请使用默认位置。权限非 600 时 nmig 只给出警告。`writeback.py` 另有 `--read-token-file` 参数可指定其他读令牌文件（开发者指南列为演练用参数）；`refresh.py` 没有对应参数，定时运行始终使用默认路径 |
| `~/.config/notion-obsidian/writeback.env` | `NOTION_WRITEBACK_TOKEN` | 只有 `writeback.py apply`、`rollback`、`audit-scope` 加载。必须是属于当前用户的普通文件、权限恰好 600，所在目录属于当前用户、权限恰好 700，否则拒绝运行；不读取环境变量。`writeback.py --token-file <路径>` 可指定其他写回令牌文件（同样要求 600 / 700，开发者指南列为演练用参数） |

需要立即撤销写权限时：在 Notion 测试页 “••• → 连接” 移除写回连接，或在开发者门户撤销该集成，见[操作手册 §5.4](../usage/operations-manual.md#sec-5-4)。

<a id="dep-7"></a>
## 7. 写回配置、根清单与长期授权文件

以下命令都在 WORK 中执行：

```bash
cd /data120/Obsidian/notion-obsidian
install -d -m 700 config
install -m 640 ~/src/AI_Notion_Obsidian/examples/writeback.example.json config/writeback.json
python3 -I -B -m json.tool config/writeback.json >/dev/null && echo 'JSON 格式正确'
python3 -I -B tools/writeback.py status          # 期望：模式 mode：off（配置 off）
```

示例配置的 `mode` 为 `"off"`，与代码默认值和 Runbook D4 一致：写回子命令只打印说明，不生成计划、不访问 Notion；刷新按 off 模式处理本地删除（下一轮按 Notion 恢复）。配置文件缺失时同样按 `off` 处理。

<a id="writeback-config"></a>
### 7.1 `config/writeback.json` 各键说明

配置以 `tools/wb_config.py` 的 `DEFAULTS` 为底，文件中的键覆盖默认值。**未知键、重复键、类型错误、NaN/Infinity 都会报错**，标准 JSON 不允许注释，所以说明只能写在这里。完整示例见 [`examples/writeback.example.json`](../../examples/writeback.example.json)。

| 键 | 默认值 | 含义 |
|---|---|---|
| `mode` | `"off"` | `off` → `plan`（只生成预览）→ `whitelist`（只写白名单页面）→ `full`。只能由所有者手工提高，工具只会自动降低 |
| `workspace_members` | `1` | 工作区成员数（配置值）；与 API 读到的人数取较大者，未知或 ≥ 2 时发出免费额度警告 `warn_block_limit` |
| `notion_plan` | `"free"` | Notion 套餐；非 `free` 时不按免费额度预警 |
| `whitelist_page_ids` | `[]` | 允许写回的页面 ID，按精确 ID 匹配，不含后代 |
| `whitelist_scope` | `{}` | `{页面ID: "page" 或 "subtree"}`；当前只支持逐页授权，出现 `subtree` 时有效模式被压到 `plan` |
| `whitelist_database_ids` | `[]` | 允许更新 / 新建行的数据库 ID |
| `automation_free_database_ids` | `[]` | 确认没有 Notion 自动化的数据库；数据库行写回只允许这些库 |
| `test_page_id` | `null` | 测试页 ID，与白名单页面同样被视为可写范围 |
| `plan_ttl_hours` | `24` | 计划有效期（小时，上限 168），过期后须重新生成 |
| `max_ops_per_page` | `50` | 单页拟操作块数上限，超出触发写回熔断（本人核对后用 `ack-anomaly` 认可） |
| `max_modified_notes` | `20` | 一次计划中本地修改笔记数上限，超出同样触发熔断 |
| `max_local_deletes` | `5` | 一轮新出现的本地缺失文件上限；刷新中超出则整轮暂停，写回计划中超出则熔断 |
| `max_batch_deletes` | `5` | 一次批准的页面删除（D 项）上限 |
| `pending_delete_ttl_days` | `14` | 本地删除登记为“待决删除”后的保留天数，到期未决定则按 Notion 恢复 |
| `allow_block_trash` | `false` | 是否允许删除块（B 项）；初始禁用 |
| `allow_page_move` | `false` | 是否允许跨父级移动页面（MV 项，仅 `full` 模式）；初始禁用 |
| `sync_settle_minutes` | `15` | Obsidian 桌面端启动后需等待的分钟数，未满时计划提示 `SYNC_SETTLING`，可执行项为 0 |
| `sync_burst_files` | `20` | 120 秒内集中变化达到该文件数时，相关文件暂缓写回 |
| `rate_limit_total_rps` | `2.5` | 所有 Notion 进程合计的请求速率上限（不得超过 3.0） |
| `rate_limit_writeback_during_refresh_rps` | `1.0` | 刷新进行期间写回的速率上限（不得超过合计上限） |
| `conflict_pause_threshold` | `20` | 一轮新冲突达到该数量时整轮暂停 |
| `source_trash_pause_threshold` | `10` | 一轮 Notion 端消失对象达到该数量时整轮暂停 |
| `source_change_pause_ratio` | `0.10` | 一轮 Notion 端改动页面比例达到该值时整轮暂停（0–1） |
| `send_silent_notifications` | `true` | 写回请求尽量不触发 Notion 通知 |
| `max_pages_per_plan` | `50` | 规格中的单计划页面数上限；当前代码只校验取值，未见其他使用 |
| `read_integration_readonly_confirmed_at` | `null` | 所有者确认读连接只读的时间或说明；为 `null` 时有效模式最高为 `plan` |

### 7.2 根清单（`config/roots_sidebar.json`，可选）

当前 `refresh.py` **不读取** `config/roots_sidebar.json`。每轮刷新从侧栏证据 `evidence/sidebar_<时间>.json` 生成 `state/roots_<时间>.json`，再作为 `snapshot.py --roots` 的输入。生产上的 `config/roots_sidebar.json` 是首次全量快照前准备的初始根清单。在新部署中，只有在侧栏采集尚不可用、需要按第 9.2 节手工完成首轮时才需要它。格式是一个数组，每项包含 `id`、`title`、`teamspace`、`teamspace_order`（按第 2 节五个团队协作区的顺序，从 1 开始）和 `root_order`（团队协作区内的顺序，从 1 开始），见 [`examples/roots_sidebar.example.json`](../../examples/roots_sidebar.example.json)。根对象是页面还是数据库由 `snapshot.py` 首次运行时自动识别，写入 `<快照>.roots.json`。

要求比字段列表更严格：`teamspace` 必须与第 2 节的五个名称逐字一致，五个团队协作区各至少一个根，`teamspace_order` 与名称顺序一致。否则 `--use-snapshot` 生成的侧栏证据为 `order_match=false`（`refresh.py` 的 `sidebar_from_snapshot` 要求观察到的团队协作区顺序与五个名称完全相同），校验 C01（库顶层须有五个团队协作区文件夹）/ C04 可能失败，`teamspace_order` 与名称不符时校验器还会给出警告。

### 7.3 长期授权文件（必需）

非 `--dry-run` 的刷新在读取快照和发布笔记之前检查 `approvals/standing_daily_refresh.approval.md` 是否存在，缺失时本轮以 `missing standing approval` 失败。程序不解析正文，正文是人工授权记录，发布时作为 `publish_vault.py --approval` 写入发布日志。

```bash
cd /data120/Obsidian/notion-obsidian
install -d -m 700 approvals
cp ~/src/AI_Notion_Obsidian/examples/standing_daily_refresh.approval.example.md approvals/standing_daily_refresh.approval.md
nano approvals/standing_daily_refresh.approval.md      # 按实际授权人、时间、范围改写
```

<a id="dep-8"></a>
## 8. 准备 Obsidian 库与侧栏采集

**库。** 创建一个专用的空目录作为库，并在 Obsidian 桌面端用“打开本地文件夹作为仓库”打开它：

```bash
mkdir -p /data120/Obsidian/Godcreate          # 必须是真实目录，不能是符号链接
```

- 发布后的库顶层只应有五个团队协作区文件夹和 `Notion_Obsidian/`（校验 C01），请不要在库根目录放其他笔记。
- 在 Obsidian 的社区插件中安装并启用 custom-sort 3.2.0，它读取工具生成的 `sortspec` 按 Notion 顺序显示文件。`tools/plugin_bundle.py` 只在 `WORK/plugins/custom-sort/3.2.0/` 有经过哈希校验的插件文件时才可用，这些文件不在仓库中，新部署直接从社区插件安装即可。该工具没有 `--help`（会把参数当作输出路径并报错），用法见[开发者指南 §1](../development/developer-guide.md#dev-layout)。
- 多设备使用时，在首轮发布完成后再连接 Obsidian Sync 远程库。远程库里若已有旧机器的笔记，请改用第 0 节的迁移方式。

**侧栏采集。** 每轮刷新先运行 `capture_sidebar.py`，通过 `opencli browser nmig_ro_sidebar ...` 在后台窗口只读读取 Notion 网页侧栏，确定顶层页面及其顺序。要求：

1. 已安装 `opencli`，并且 systemd 用户实例的 `PATH` 能找到它。生产为源码构建的 1.8.6；如需与生产版本一致，可以安装 npm 上的同版本 `npm install -g @jackwener/opencli@1.8.6`（**未验证**：此命令未在本仓库验证；不指定版本会装到 2.x，与 `opencli browser <会话> eval` 用法的兼容性也未验证）。浏览器桥接与会话 `nmig_ro_sidebar` 的建立方式以 opencli 上游文档为准。检查：

   ```bash
   command -v opencli
   systemctl --user show-environment | grep '^PATH='     # 必须包含 opencli 所在目录
   ```

   若不包含，可在 `~/.config/environment.d/` 中添加 `PATH` 配置后重新登录，或把 `opencli` 安装到 `/usr/local/bin`（未在本仓库中验证）。
2. 本机 Chrome 已登录 Godcreate 工作区，opencli 的浏览器桥接可用，侧栏能看到五个团队协作区。
3. 手动试采一次：

   ```bash
   cd /data120/Obsidian/notion-obsidian && python3 -I -B tools/capture_sidebar.py
   ```

   成功时输出 `evidence/sidebar_<时间>.json` 的路径，退出码为 0，文件中 `order_match` 为 `true`。退出码 2 表示桥接不可用、团队协作区顺序不符或某个团队协作区读不到根页面。

采集失败时，刷新会退回使用最近一份 `order_match=true` 的旧证据，并在运行记录中警告；如果从未有过成功的证据，刷新会以 `no usable sidebar evidence` 失败。所以**定时刷新需要侧栏采集可用**。

<a id="dep-9"></a>
## 9. 首次快照、导出、校验与发布

一轮刷新依次执行：侧栏采集 → 快照（`snapshot.py`）→ 附件补抓（`assets_fetch.py`）→ 源端消失归档 → 只读库扫描 → 导出到 `staging/<轮次>/vault`（`export_obsidian.py`）→ 暂存库校验 C01–C10（`verify_vault.py`）→ 发布计划与暂停评估 → 发布（`publish_vault.py`）→ 发布后复验。

### 9.1 推荐方式：侧栏采集可用时

```bash
cd /data120/Obsidian/notion-obsidian

# 1) 预演：抓侧栏、全量快照、导出、校验、生成发布计划，但不写库
python3 -I -B tools/refresh.py --mode full --dry-run; echo "exit=$?"
```

预演结束时结果为 `planned`、退出码为 0。预演仍会在 WORK 写入侧栏证据、根清单、快照、暂存库、日志和报告，这是正常的。查看本轮编号和校验结果：

```bash
tail -n 1 logs/refresh_history.jsonl | python3 -I -B -c 'import json,sys; r=json.loads(sys.stdin.read()); print(r.get("run_id"), r.get("result"), r.get("verify"))'
```

`verify` 中 C01–C10 应全部为 `PASS`（C11 是敏感串警告，不阻断）。确认无误后正式运行一轮：

```bash
# 2) 正式首轮：发布到 /data120/Obsidian/Godcreate
python3 -I -B tools/refresh.py --mode full; echo "exit=$?"
```

首轮需要全量读取所有已共享页面，耗时取决于页面数量（生产上的增量轮约 40 分钟，service 超时为 6 小时）。退出码含义：0 = 已发布 / 预演 / 遇锁跳过；1 = 失败；2 = 整轮暂停待审阅；3 = 本人终端检查未通过。

### 9.2 备用方式：侧栏采集暂不可用时（仅用于首轮）

可以先按根清单手工做一份快照，再用 `--use-snapshot` 离线完成导出、校验和发布。这条路径不调用侧栏采集，但之后的定时轮次仍需要第 8 节的采集。

```bash
cd /data120/Obsidian/notion-obsidian
mkdir -p snapshots
cp ~/src/AI_Notion_Obsidian/examples/roots_sidebar.example.json config/roots_sidebar.json
nano config/roots_sidebar.json      # 换成真实的顶层页面 ID、标题与顺序

TS=$(date +%Y%m%dT%H%M)
python3 -I -B tools/snapshot.py --roots config/roots_sidebar.json --db "snapshots/snap_$TS.db" --workers 4 --rate 2.5
python3 -I -B tools/assets_fetch.py --db "snapshots/snap_$TS.db"

python3 -I -B tools/refresh.py --use-snapshot "snapshots/snap_$TS.db" --dry-run; echo "exit=$?"
python3 -I -B tools/refresh.py --use-snapshot "snapshots/snap_$TS.db"; echo "exit=$?"
```

说明：`snapshot.py` 中断后用同一命令重跑会续抓；`--use-snapshot` 要求快照已完成（`meta.finished_at` 存在），并从 `<快照>.roots.json` 生成本轮的侧栏证据；这种轮次不会更新 `state/latest_snapshot.txt`，所以下一次定时增量轮会做一次完整读取。该备用路径依据代码整理，未在全新机器上实测。

### 9.3 检查首轮结果

```bash
cd /data120/Obsidian/notion-obsidian
RUN=$(tail -n 1 logs/refresh_history.jsonl | python3 -I -B -c 'import json,sys; print(json.loads(sys.stdin.read()).get("run_id") or "")'); echo "$RUN"
python3 -I -B -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s.get("result"), s.get("verify"), (s.get("post_publish_verify") or {}))' "reports/$RUN/refresh_status.json"
less "reports/$RUN/run_record.md"
ls /data120/Obsidian/Godcreate               # 五个团队协作区文件夹 + Notion_Obsidian
```

期望结果为 `published`（或 `published_with_holds`，表示部分对象被暂停，见[运维指南](../operations/operations-guide.md#ops-results)）。发布前的校验在 `reports/$RUN/verify/verify.json`，发布后的复验在 `reports/$RUN/post_publish_verify/verify.json`。打开 Obsidian 确认笔记、附件和排序正常。

<a id="dep-10"></a>
## 10. 安装 systemd 定时器并验证首轮

```bash
mkdir -p ~/.config/systemd/user
cp /data120/Obsidian/notion-obsidian/deploy/notion-obsidian-refresh@.service \
   /data120/Obsidian/notion-obsidian/deploy/notion-obsidian-refresh-incremental.timer \
   /data120/Obsidian/notion-obsidian/deploy/notion-obsidian-refresh-full.timer \
   ~/.config/systemd/user/
# 采用虚拟环境（第 4 节方式 B）时，先把 ~/.config/systemd/user/notion-obsidian-refresh@.service 中
# ExecStart 的 /usr/bin/python3 改为虚拟环境的解释器
systemd-analyze --user verify ~/.config/systemd/user/notion-obsidian-refresh-incremental.timer ~/.config/systemd/user/notion-obsidian-refresh-full.timer
systemctl --user daemon-reload
systemctl --user enable --now notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer
sudo loginctl enable-linger "$USER"       # 用户未登录时定时器也运行
loginctl show-user "$USER" -p Linger      # 期望 Linger=yes
systemctl --user list-timers --all 'notion-obsidian-*' --no-pager
```

只启用两个 timer，不要单独启用 service 模板。调度为：

| 单元 | 触发时间 | 内容 |
|---|---|---|
| `notion-obsidian-refresh-incremental.timer` | 每天 00:30、06:30、12:30、18:30（`OnCalendar=*-*-* 00/6:30:00`），随机推迟最多 10 分钟 | `notion-obsidian-refresh@incremental.service` → `refresh.py --mode incremental` |
| `notion-obsidian-refresh-full.timer` | 每周日 05:30，随机推迟最多 15 分钟 | `notion-obsidian-refresh@full.service` → `refresh.py --mode full` |

两个 timer 都设置了 `Persistent=true`，停机错过的轮次会在恢复后补跑。service 为 `Type=oneshot`、`Nice=10`、`TimeoutStartSec=6h`。

**验证第一次定时运行**（或立即手动触发一次增量服务）：

```bash
systemctl --user start notion-obsidian-refresh@incremental.service      # 可选：立即跑一轮，命令会等待本轮结束
journalctl --user -u notion-obsidian-refresh@incremental.service -n 50 --no-pager
cd /data120/Obsidian/notion-obsidian && tail -n 3 logs/refresh_history.jsonl | jq -r '[(.run_id // "-"),.mode,.result,(.finished // .at // "-")] | @tsv'
```

服务管理器会把退出码 2（暂停）和 3（拒绝）显示为 failed，请结合 `refresh_status.json` 判断。`skipped_lock_busy` 表示当时另一轮正在运行，本次直接跳过，不是故障。

<a id="dep-11"></a>
## 11. （可选）启用写回预览

写回始终需要所有者本人在自己的终端审批，按以下顺序逐步开放，详细操作见[操作手册 §2](../usage/operations-manual.md#sec-2) 与 [§4](../usage/operations-manual.md#sec-4)：

1. 所有者在 Notion 确认读连接只读后，编辑 `config/writeback.json`：`mode` 改为 `"plan"`，`read_integration_readonly_confirmed_at` 填写确认时间（如 `"2026-11-17T18:30+07:00"`），`test_page_id` 和 `whitelist_page_ids` 填测试叶子页的 ID。编辑前先备份，编辑后运行 `python3 -I -B -m json.tool config/writeback.json >/dev/null && python3 -I -B tools/writeback.py status`。配置损坏会使后续每一轮刷新失败。
2. 检查范围：`python3 -I -B tools/writeback.py whitelist-check`（只读）。
3. 在编辑任何笔记之前建立基线：`python3 -I -B tools/writeback.py plan --scope whitelist`。首次计划会把已有差异列为 `INITIAL_BACKLOG`，可执行数为 0，属正常。
4. 创建写回令牌文件（第 6 节）后，由所有者运行 `python3 -I -B tools/writeback.py audit-scope`，结果须含 `"ok": true`、`"extra": []`、`"unreachable": []`。
5. 真正写入时，由所有者临时把 `mode` 改为 `"whitelist"`、重新生成计划，再在自己的终端运行 `apply` 并输入现场挑战短语；验收后改回 `"plan"`。AI 不得代为执行这一步。

写回计划还要求本机 Obsidian 桌面主进程已运行满 `sync_settle_minutes`（默认 15 分钟），否则可执行项为 0（`SYNC_SETTLING`）。只有命令行、没有桌面的服务器上，写回无法通过这项检查。

<a id="dep-12"></a>
## 12. 生产升级与回退 Runbook 摘要

升级不只是“替换 tools/”：新版发布器第一次发布时会迁移 `state/export.db`，此后旧版发布器每一轮都会失败，所以 **`tools/` 与 `state/export.db` 必须一起部署、一起回退**。完整命令见[开发者指南 §6.2](../development/developer-guide.md#dev-runbook)，要点如下（全部在 WORK 中执行，期间编辑冻结，只由一位指定执行者操作）：

| 步骤 | 内容 |
|---|---|
| D1 | 前提：测试套件通过，发布物已校验签署 |
| D2 | 停两个 timer；确认两个 service 都不是 `active` / `activating`；`flock -n state/refresh.lock true` 直到空闲；`state/export.db-journal` 不得存在 |
| D3 | 新建 `backup/deploy-<时间>/`，备份 `tools/`、`config/`、状态文件清单，并用 SQLite 在线备份 `export.db`，核对 `integrity_check` 为 `ok` 和 6 个旧列，记录 SHA256 |
| D4 | 安装新 `tools/`（不含 `__pycache__`），必要时创建 `config/writeback.json`（默认 `off`） |
| D5 | 手动运行一轮 `python3 -I -B tools/refresh.py --mode incremental`，确认 C01–C10 为 PASS、`published_files` 已有 `staged_sha256`，再启动两个 timer |
| R1 | 停 timer，等待 `refresh.lock` 和 `writeback.lock` 空闲 |
| R2 | 新建 `backup/rollback-<时间>/` 备份当前状态，把当前 `tools/` 移走，恢复 D3 备份的旧 `tools/` |
| R3 | `export.db` 二选一：(a) 恢复部署前副本；(b) 删除 `staged_sha256` 列（SQLite ≥ 3.35）。绝不在已迁移的库上直接启动旧工具 |
| R4 | 把只有新工具使用的状态文件（`writeback.db`、`base/`、`auto_sync_hold.json` 等）移入回退目录，不删除 |
| R5 | 用旧工具手动跑一轮，退出码为 0 后启动两个 timer |

从仓库升级时，先 `git pull` 并核对版本，在 D4 把新版工具装入新的空目录后再替换，避免上游已删除的模块残留（旧 `tools/` 已在 D3 备份）：

```bash
mkdir tools.new && cp -p ~/src/AI_Notion_Obsidian/notion-obsidian/tools/*.py tools.new/ \
  && mv tools "$B/tools.replaced" && mv tools.new tools
for f in ~/src/AI_Notion_Obsidian/notion-migration/nmig/*.py; do cmp "$f" "/data120/Obsidian/notion-migration/nmig/${f##*/}"; done   # 无输出表示 nmig 无需更新
```

仓库只收录了本工具用到的 nmig 4 个模块（原生产机上的 `nmig/` 还有迁移工具包的其他模块），所以只逐个比较仓库中的文件；有差异时用仓库中的版本替换对应文件。上述替换方式依据代码整理，未在生产升级中实测。回退**不会**撤销已写入 Notion 的内容；运行过写回的计划须先用新版工具处理（所有者本人运行 `writeback.py rollback` / `resolve`）。用户侧的冻结与恢复说明见[操作手册 §7.2](../usage/operations-manual.md#sec-7-2)。

<a id="dep-13"></a>
## 13. 部署检查清单

- [ ] `/data120/Obsidian/notion-obsidian/tools/` 有 28 个模块，`/data120/Obsidian/notion-migration/nmig/` 存在，二者都是真实目录
- [ ] `/usr/bin/python3 -I -c 'import requests, yaml, markdown_it'` 成功
- [ ] 读连接只读、已共享全部顶层页面；令牌文件 600、目录 700
- [ ] `config/writeback.json` 通过 JSON 检查，`writeback.py status` 显示预期模式
- [ ] `approvals/standing_daily_refresh.approval.md` 已按实际授权填写
- [ ] `capture_sidebar.py` 退出码 0，`order_match` 为 `true`；systemd 用户 `PATH` 能找到 `opencli`
- [ ] 首轮结果 `published`，C01–C10 全部 PASS，库中五个团队协作区文件夹和 `Notion_Obsidian/` 齐全
- [ ] 两个 timer 已启用，`Linger=yes`，`list-timers` 中 NEXT 有时间
- [ ] 第一次定时运行后 `logs/refresh_history.jsonl` 新增记录且结果正常
- [ ] 仓库与 WORK 中没有任何令牌文件或真实凭据
