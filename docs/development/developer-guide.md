# 开发者指南

> 面向修改、测试和发布本仓库代码的开发者与维护者。本文的第 6 节由开发沙箱说明 `README-DEV.txt` 转写而来，部署与回退命令保持原样；其余各节依据 `notion-obsidian/tools/` 中的代码整理。返回[文档索引](../README.md)。

**目录**

- [1. 仓库结构](#dev-layout)
- [2. 开发环境](#dev-env)
- [3. 编码约定](#dev-rules)
- [4. 运行测试](#dev-tests)
- [5. 演练与离线复现](#dev-rehearsal)
- [6. 开发沙箱与部署 / 回退 Runbook（转写自 README-DEV.txt）](#dev-runbook)
- [7. 常见开发任务的注意事项](#dev-tasks)

<a id="dev-layout"></a>
## 1. 仓库结构

```text
AI_Notion_Obsidian/
├── README.md                     # 项目首页
├── requirements.txt              # 运行与测试依赖
├── docs/                         # 全部文档（本目录）
├── examples/                     # 配置、凭据、授权文件示例（占位符，无真实值）
├── notion-migration/
│   └── nmig/                     # 项目早期迁移工具包的内置副本（vendored，只导入、不修改）
│       ├── client.py             #   NotionClient：重试、限速、分页
│       ├── config.py             #   读取 NOTION_TARGET_TOKEN / NOTION_VERSION
│       └── rest_inventory.py     #   可续跑的 SQLite 快照爬虫（快照库表结构的来源）
└── notion-obsidian/
    ├── deploy/                   # systemd 用户单元（service 模板 + 增量 / 全量两个 timer）
    └── tools/                    # 全部工具，运行时 WORK = tools/ 的上级目录
        ├── refresh.py            # 入口：一轮 Notion → Obsidian 刷新（定时器调用）
        ├── writeback.py          # 入口：Obsidian → Notion 写回 CLI（预览 / 审批 / 回退）
        ├── capture_sidebar.py    # 通过 opencli 浏览器桥接只读抓取 Notion 侧栏顺序
        ├── snapshot.py           # 只读快照（基于 nmig.rest_inventory，附件按 sha256 落盘）
        ├── assets_fetch.py       # 快照后的附件补抓与重试
        ├── export_obsidian.py    # 快照 → 暂存库（Markdown、.base、索引、sortspec）
        ├── verify_vault.py       # 独立校验器 C01–C11（C11 只警告）
        ├── publish_vault.py      # 暂存库 → 正式库的两阶段发布（计划 + 应用，带日志与备份）
        ├── rollback.py           # 按发布日志撤销一次本地发布（无 --apply 时只预览）
        ├── rehearse.py           # 隔离副本上的发布 / 幂等 / 回退演练
        ├── trash_archive.py      # 源端消失对象的归档登记
        ├── plugin_bundle.py      # 生成 custom-sort 插件文件清单（供 publish_vault --extra；没有 --help，见下文）
        ├── sortspec.py           # custom-sort 3.2.0 排序规则生成
        ├── md2blocks.py          # 写回用的 Markdown → Notion 块反向解析
        ├── wb_scan.py            # 只读库扫描（refresh 与写回计划共用）
        ├── wb_plan.py            # 写回计划器
        ├── wb_apply.py           # 写回执行器（读回、W1–W10、采纳）
        ├── wb_refetch.py         # 写回后的定向重抓
        ├── wb_gate.py            # 本人终端检查与挑战短语
        ├── wb_state.py           # state/writeback.db 结构与迁移、state/base/ 存储
        ├── wb_config.py          # config/writeback.json 默认值、校验与有效模式
        ├── wb_locks.py           # state/<name>.lock 文件锁（refresh、writeback）
        ├── wb_common.py          # 路径常量、原子写入、只读打开快照等公共函数
        ├── wb_norm.py            # 导出与写回共用的规范化 JSON
        ├── readonly_client.py    # 只读 Notion 客户端（只允许 GET / search / query）
        ├── writeback_client.py   # 写回 Notion 客户端（Notion-Version 2026-03-11）
        ├── rate_gate.py          # 跨进程限速（state/rate_gate.json，合计 2.5 次/秒）
        ├── notion_tokens.py      # 读 / 写两个令牌的加载与权限检查
        └── tests/                # unittest 测试套件、fake_notion.py 与两个手动回归脚本
```

运行时目录（`config/`、`state/`、`snapshots/`、`staging/`、`reports/`、`logs/`、`backup/`、`blobs/`、`approvals/`，以及 `evidence/`、`plugins/`、`rehearsal/`、`rollback/`）都在 WORK 下，已被 `.gitignore` 排除，**不得提交**。其中 `evidence/`（侧栏证据，含真实页面标题与 ID）、`rehearsal/`（库的完整副本）、`rollback/`（隔离的笔记）尤其敏感，提交前请用 `git status` 确认工作树里没有误加的文件。各目录的用途见[运维指南 §3](../operations/operations-guide.md#ops-files)。

`plugin_bundle.py` 没有参数解析，`--help` 会被当作输出路径：它先创建 `WORK/plugins/custom-sort/generated/`，再因找不到插件文件而报错退出。实际用法为 `python3 -I -B tools/plugin_bundle.py <输出目录>/obsidian_extra.json`，要求 `WORK/plugins/custom-sort/3.2.0/` 下有 `main.js` 与 `manifest.json` 且 sha256 与代码中的 `EXPECT` 一致（文件缺失时以 `FileNotFoundError` 报错，哈希不符时以 `ABORT plugin file hash mismatch` 中止）；它会写 `WORK/plugins/custom-sort/generated/`、输出文件及对应的 `obsidian_extra.skipped.json`，库中已存在的插件文件不会列入清单。新部署直接从社区插件安装 custom-sort，通常不需要运行它。

<a id="dev-env"></a>
## 2. 开发环境

| 项目 | 要求 / 实测版本 |
|---|---|
| 操作系统 | Linux（实测 Ubuntu 24.04.5 LTS）；`fcntl` 文件锁与 systemd 用户单元使 Windows / macOS 不受支持 |
| Python | 3.12（实测 3.12.3） |
| SQLite（Python 内置） | ≥ 3.35（回退步骤 R3(b) 的 `DROP COLUMN` 需要；实测 3.45.1） |
| 依赖 | `requests>=2.31`、`PyYAML>=6.0`、`markdown-it-py>=3.0`；测试另需 `pexpect>=4.9`（见 [`requirements.txt`](../../requirements.txt)） |
| 可选 | `node`：有一项测试在缺少 node 时跳过；`jq`：手册里部分查询命令用到 |

安装依赖的两种方式：

```bash
# 方式 A（与生产一致）：Ubuntu 24.04 的系统包，版本即 requests 2.31.0 / PyYAML 6.0.1 / markdown-it-py 3.0.0 / pexpect 4.9
sudo apt install python3-requests python3-yaml python3-markdown-it python3-pexpect

# 方式 B：虚拟环境（Ubuntu 上需先安装 python3-venv 包）
python3 -m venv ~/.venvs/notion-obsidian
~/.venvs/notion-obsidian/bin/pip install -r requirements.txt
```

2026-11-21 实测：方式 B 安装到的是较新版本（markdown-it-py 4.3.0、requests 2.34.2、PyYAML 6.0.4），测试结果与方式 A 相同（1088 项，1 个错误、8 个跳过，见 4.1）。

注意：

- 所有工具都用 `python3 -I` 运行。`-I` 会忽略 `PYTHONPATH` 等 `PYTHON*` 环境变量和用户级 site-packages（`~/.local/lib/...`），所以 `pip install --user` 装的包**不会生效**。采用方式 B 时请用虚拟环境中的解释器运行命令，例如 `~/.venvs/notion-obsidian/bin/python3 -I -B ...`；第 4 节测试命令里的 `env -i PATH=/usr/bin:/bin` 会让 `python3` 解析为系统解释器（即使已激活虚拟环境），所以那里要用 `PY` 变量指定虚拟环境解释器的绝对路径。
- **nmig 的导入路径是写死的。** `snapshot.py:28`、`assets_fetch.py:21`、`readonly_client.py:36` 把 `/data120/Obsidian/notion-migration` 加进 `sys.path` 再导入 `nmig`；由于 `-I` 忽略 `PYTHONPATH`，测试套件和工具都要求该路径下存在 `nmig/`。在新开发机上可以用符号链接指向仓库里的副本（只影响导入，安全）：

  ```bash
  sudo install -d -o "$USER" -g "$USER" /data120/Obsidian/notion-migration
  ln -s "$PWD/notion-migration/nmig" /data120/Obsidian/notion-migration/nmig   # 在仓库根目录执行
  ```

  这台机器若同时是生产机，`/data120/Obsidian/notion-migration/nmig` 已经存在，不要覆盖。

  无 sudo 的替代（仅用于开发 / 测试，2026-11-21 实测可用）：采用方式 B 的虚拟环境时，`python3 -I` 仍会处理虚拟环境 site-packages 中的 `.pth` 文件，可以在仓库根目录执行

  ```bash
  echo "$PWD/notion-migration" > "$(~/.venvs/notion-obsidian/bin/python3 -c 'import site; print(site.getsitepackages()[0])')/nmig-repo.pth"
  ```

  之后用该虚拟环境的解释器运行测试与工具。注意：只要 `/data120/Obsidian/notion-migration/nmig` 存在（例如在生产机上），代码会因 `sys.path.insert(0, NMIG)` 优先导入那一份，而不是仓库副本；2026-11-21 核对时，仓库收录的 4 个模块与生产机上的同名文件内容相同（生产机的 `nmig/` 还有迁移工具包的其他模块）。
- WORK 由 `os.path.abspath(__file__)` 推出，即 `tools/` 的上级目录，**不解析符号链接**。在仓库内运行时 WORK 就是仓库里的 `notion-obsidian/`，运行时目录会出现在工作树中（已被忽略）。开发时尽量只运行只读命令和测试；需要跑完整流程时，用第 5 节的隔离副本。

<a id="dev-rules"></a>
## 3. 编码约定

1. **一律 `python3 -I -B`。** `-I` 忽略 `PYTHONPATH` 等 `PYTHON*` 环境变量和用户级 site-packages，也不把脚本目录加入 `sys.path`（`NOTION_TARGET_TOKEN` 等普通环境变量仍然生效），所以每个入口模块都显式把 `tools/` 插入 `sys.path`。`-B` 不写 `__pycache__`；`refresh.py` 调用子工具时固定使用 `[sys.executable, "-I", "-B"]`（`refresh.py` 中的 `PY`）。生产 systemd 单元目前只带 `-I`，运行时可能生成 `__pycache__`（已被忽略）。
2. **SQLite 只读打开。** 手工检查或新代码读取快照、`export.db`、`writeback.db` 时使用 `file:<path>?mode=ro` URI。已完成且没有待恢复 WAL 的快照用 `wb_common.open_snapshot_reader()`，它会在安全时采用 `mode=ro&immutable=1`，有非空 WAL 时保持 `mode=ro` 并要求 `-shm` 已存在，避免读操作创建旁路文件。**不要手工运行 `wb_state.ensure_export_db`**：它会迁移 `export.db`，旧版发布器之后每轮都会失败（见第 6 节）。
3. **绝不打印令牌。** 令牌只通过 `notion_tokens.py` / `nmig/config.py` 加载，日志和报告只能用 `mask()` 描述“已设置 / 前缀 / 长度”。测试必须传入临时令牌文件路径，不得读取 `~/.config`。仓库中不得出现任何真实令牌，示例只用 `<your-...-token>` 这类占位符。
4. **读写分离。** 自动管线（refresh、snapshot、assets_fetch）只通过 `readonly_client.ReadOnlyClient` 访问 Notion，它在发送前拦截 GET / search / query 以外的一切请求。会改变 Notion 的请求只能出现在 `wb_apply.py` 中，经由 `writeback_client.py`，并且只能在 `wb_gate.py` 的本人终端检查与挑战短语之后发出。`refresh.py` 从不导入写客户端，也不加载写回令牌。
5. **共享限速与锁。** 所有 Notion 进程共用 `state/rate_gate.json`（合计 2.5 次/秒，写回在刷新期间 1.0 次/秒）。刷新与写回共用 `state/refresh.lock`（非阻塞，遇锁立即退出，不排队）；写回另有 `state/writeback.lock`。锁文件不得删除。
6. **只能降低模式。** `wb_config` 只允许工具把有效模式向下调整（`record_downgrade`），提升模式只能由用户本人编辑 `config/writeback.json`。读不到 `writeback.db` 时有效模式最高为 `plan`（fail closed）。
7. **不修改 `nmig`。** `notion-migration/nmig` 是导入使用的内置副本，扩展行为请在 `tools/` 中子类化（如 `ReadOnlyClient`、`BlobInventory`）。
8. **用户可见输出中英双语**，退出码保持稳定：刷新 `0/1/2/3`，写回 `0/1/2/3/64`（含义见[运维指南 §4](../operations/operations-guide.md#ops-results)）。

<a id="dev-tests"></a>
## 4. 运行测试

### 4.1 单元测试全套

在仓库根目录执行。建议用空 `HOME`、私有 `TMPDIR` 和精简环境变量运行，保证测试读不到真实凭据，也不受 AI / CI 环境变量影响（本人终端检查会识别 `CLAUDECODE`、`AI_AGENT`、`CI`、`GITHUB_ACTIONS`、`CLAUDE_CODE_*`）：

```bash
# 两个测试夹具：部分测试会检查 WORK（即仓库内的 notion-obsidian/）下的 state/ 目录和默认配置文件。
# 这两个路径已被 .gitignore 排除，不会进入提交。
mkdir -p notion-obsidian/state notion-obsidian/config
cp examples/writeback.example.json notion-obsidian/config/writeback.json

PY=/usr/bin/python3            # 方式 B：PY=~/.venvs/notion-obsidian/bin/python3
T=$(mktemp -d) && mkdir -p "$T/home" "$T/tmp"
env -i PATH=/usr/bin:/bin HOME="$T/home" TMPDIR="$T/tmp" LANG=C.UTF-8 \
  "$PY" -I -B -m unittest discover -s notion-obsidian/tools/tests -p 'test_*.py'
```

- `env -i PATH=/usr/bin:/bin` 下的 `python3` 总是解析为 `/usr/bin/python3`，即使已激活虚拟环境也一样；依赖只装在虚拟环境中时，必须用 `PY` 指定虚拟环境解释器，否则各模块会以 `ModuleNotFoundError: requests` / `yaml` 失败。
- 规模与耗时（2026-11-21 本机实测，Python 3.12.3）：共 **1088** 项，约 **21 分钟**（1245 秒）。
- 在直接克隆、未准备夹具的仓库中，结果为 `FAILED (failures=1, errors=5, skipped=8)`：其中 5 项因缺少上面两个夹具而失败，补上夹具后单独复跑均通过；剩下 1 项 `test_v2_mode_guidance.ModeHelp.test_binding_contract_records_both_decisions` 读取未收入仓库的内部实现约定文档 `notion-obsidian/_resume_20261118/impl/IMPL_CONTRACT.md`，在仓库中必然报错，与代码功能无关。因此准备好夹具后，预期结果为 1 个错误、8 个跳过。
- 8 个跳过：`test_docs_consistency.py` 5 项（见 4.4），以及依赖开发沙箱 `vault/` 与 `state/export.db` 的 3 项（`test_wb_scan.py` 1 项、`test_wb_state.py` 2 项）。
- 运行过程中终端会出现 `ERROR: --expected-deviations unreadable`、`snap.db-wal is not empty` 等行，这是负向测试中子进程按预期打印的错误信息，不代表测试失败；以最后的 `Ran … / OK / FAILED` 汇总为准。
- 测试只使用本机 `127.0.0.1` 上的 `fake_notion.py` 和临时目录，不访问真实 Notion，也不读取 `~/.config`。
- 请以普通用户运行：以 root 运行时，三项依赖文件权限的测试会跳过（`test_refresh_snapshot_uri`、`test_wb_scan`、`test_wb_state` 各 1 项）。
- 其他可能的跳过：未安装 `pexpect` 时伪终端审批测试跳过，未安装 `node` 时 1 项跳过。

只跑一个模块：

```bash
python3 -I -B -m unittest discover -s notion-obsidian/tools/tests -p 'test_wb_plan.py' -v
```

### 4.2 端到端测试（fake_notion）

| 测试文件 | 覆盖内容 |
|---|---|
| `test_e2e_writeback.py` | 在 fake_notion 上完整走一遍：快照 → 真实的 refresh 轮次（`--use-snapshot`）→ 本地编辑 → `status` / `plan` / `show` → 通过伪终端输入挑战短语的 `apply` → W1–W10 与采纳 → 故障注入（409、提交后 503、连接中断、两步之间 kill -9 后续办）→ 回退演练 → 删除与改名策略 |
| `test_e2e_sandbox.py` | 两轮 Notion → Obsidian 的小型沙箱演练（复制的 SQLite 快照 + 假读连接） |

```bash
PY=/usr/bin/python3            # 方式 B：PY=~/.venvs/notion-obsidian/bin/python3
T=$(mktemp -d) && mkdir -p "$T/home" "$T/tmp"
env -i PATH=/usr/bin:/bin HOME="$T/home" TMPDIR="$T/tmp" LANG=C.UTF-8 \
  "$PY" -I -B -m unittest discover -s notion-obsidian/tools/tests -p 'test_e2e_*.py' -v
```

2026-11-21 实测：22 项，约 4 分钟（232 秒），全部通过；另在带有 `CLAUDECODE` / `AI_AGENT` / `CLAUDE_CODE_*` 变量的终端中直接运行（不加 `env -i`）同样通过，子进程环境已剔除这些变量。这两组测试需要 `pexpect`。子进程环境中会去掉上述 AI / CI 变量，只针对假服务和假令牌；本人终端检查本身没有改动，测试同样验证了它在这些变量存在、无 TTY 或管道输入时会拒绝。`fake_notion.FakeNotion` 的用法见该文件开头的说明。

### 4.3 手动回归脚本（不在 unittest 套件中）

| 脚本 | 作用 | 需要的本地数据 |
|---|---|---|
| `tests/regress_export_bytes.py` | 全量语料导出回归：沙箱快照重新导出后与生产清单逐文件比对 sha256，另检查 block_lines、命名覆盖等，约 5 分钟 | 开发沙箱 `snapshots/snap_20261117T1859.db` 与 `state/export.db`，生产 `evidence/sidebar_20261117T0417.json` 与 `staging/20261117T1859-T1/manifest.json`（路径写在脚本开头） |
| `tests/corpus_roundtrip.py` | `md2blocks` 在真实语料上的往返回归（行内、块、编辑、规范化编辑四类） | 一份已完成的快照（`--snapshot`），可选 `--export-db`；以 `mode=ro&immutable=1` 只读打开，报告中的示例已匿名化 |

这两个脚本依赖生产或开发沙箱中的真实数据，**单纯克隆仓库无法运行**。运行方式见各脚本开头的说明，例如：

```bash
python3 -I -B notion-obsidian/tools/tests/corpus_roundtrip.py --snapshot <WORK>/snapshots/snap_<TS>.db --out /tmp/roundtrip.json
```

### 4.4 文档一致性测试

`test_docs_consistency.py` 检查开发沙箱中的 `README-DEV.txt` 部署 / 回退 Runbook，以及 `docs/SPEC.md` §14、`docs/WRITEBACK_SPEC.md` 与代码是否一致。它按 `notion-obsidian/README-DEV.txt`、`notion-obsidian/docs/SPEC.md`、`notion-obsidian/docs/WRITEBACK_SPEC.md` 查找文件，缺失时跳过。本仓库把规格文档放在 [`docs/design/`](../design/)，并把 README-DEV.txt 转写成了本文，所以在仓库中这 5 项测试默认跳过。若要检查规格部分，可在临时副本中放置规格文档后单独运行（2026-11-21 实测：规格相关的 2 项通过，Runbook 相关的 3 项因缺少 README-DEV.txt 仍跳过）：

```bash
T=$(mktemp -d) && cp -r . "$T/repo" && mkdir -p "$T/repo/notion-obsidian/docs" \
  && cp docs/design/SPEC.md docs/design/WRITEBACK_SPEC.md "$T/repo/notion-obsidian/docs/" \
  && (cd "$T/repo" && python3 -I -B -m unittest discover -s notion-obsidian/tools/tests -p 'test_docs_consistency.py' -v)
```

<a id="dev-rehearsal"></a>
## 5. 演练与离线复现

- **隔离工作目录。** WORK 永远是脚本所在 `tools/` 的上级目录。`refresh.py` 没有 `--work` 参数，即使用 `--vault ... --allow-other-vault` 指向另一个库，状态、快照和报告仍写在脚本所属的 WORK 下。因此演练必须在一份**完整的独立副本**中进行：复制 `tools/`，准备副本自己的 `config/`、`state/` 和库副本，再用副本中的脚本运行。生产工具即便指定另一个库也可能写生产状态（见[操作手册 §7.2](../usage/operations-manual.md#sec-7-2)）。
- **离线重放快照。** `python3 -I -B tools/refresh.py --use-snapshot snapshots/snap_<TS>.db --vault <库副本> --allow-other-vault` 不抓侧栏、不访问 Notion，用一份已完成的快照走完导出 → 校验 → 发布。
- **写回演练。** `writeback.py` 支持 `--work`、`--vault`、`--base-url`（指向本地假服务）、`--read-token-file`、`--token-file`。在生产 WORK 以外，写回工具不会默认使用正式库，必须显式给出 `--vault`。
- **发布 / 回退演练。** `python3 -I -B tools/rehearse.py <run_id>` 把正式库只读复制到 `WORK/rehearsal/<run_id>/`，在副本上发布 `staging/<run_id>`、再发布一次检查幂等、然后回退并逐文件比对，结果写入 `reports/<run_id>/rollback_rehearsal.json`。它读取 `rehearse.py:19` 的正式库路径作为复制来源。

<a id="dev-runbook"></a>
## 6. 开发沙箱与部署 / 回退 Runbook（转写自 README-DEV.txt）

> 以下内容依据开发沙箱的 `README-DEV.txt` 转写为 Markdown，说明文字译为中文，命令保持原样。文中提到的 `G1.sha256`、`s5_deploy.sh`、`S5_DEPLOY_PLAN.md`、`W4-acceptance.md`、`IMPL_CONTRACT.md` 以及 L-xx / D-xx 编号属于项目内部的协作与审批记录，**未收入本仓库**；G1 是内部的发布审批关口，“督导”指签署发布物的项目监督人。生产环境的部署步骤以[部署指南](../deployment/deployment-guide.md)为准。

### 6.1 开发沙箱说明

- 开发沙箱（2026-11-17 建立，生产机上为 `/data120/Obsidian/notion-obsidian-dev`）用于实现写回功能，**不被 systemd 定时器使用**。
- `tools/` 是工作副本；WORK 解析到沙箱根目录，所以其中的 `state/`、`staging/`、`reports/` 与生产隔离。
- `blobs/`、`evidence/`、`plugins/` 是指向生产任务根目录的符号链接，只读使用。
- `snapshots/` 存放指向生产快照文件的符号链接（不得在沙箱中写生产快照）。
- `vault/` 是 `/data120/Obsidian/Godcreate` 的**副本**，供测试使用（发布时加 `--allow-other-vault`）。

### 6.2 部署与回退 Runbook

仅由负责人执行。部署说明同时写入用户手册（[操作手册 §7.2](../usage/operations-manual.md#sec-7-2)），并由 `tools/tests/test_docs_consistency.py` 检查。

- `P = /data120/Obsidian/notion-obsidian`，下面所有命令都在 `P` 下运行。
- **部署不只是“把 tools/ 拷回去”。** 新版发布器第一次实际发布时会迁移 `state/export.db`（`Publisher.open_rw` → `wb_state.ensure_export_db` 给 `published_files` 增加 `staged_sha256` 列，并创建 `conflict_copies` 与 `publish_intents` 两张表；L-02）。此后**旧版发布器在每一轮都会失败**（包括没有任何改动的轮次），错误发生在它第一条按位置写入的 INSERT：`table published_files has 7 columns but 6 values were supplied`。
- 因此 **`tools/` 与 `state/export.db` 必须一起部署、一起回退**。不要手工运行 `wb_state.ensure_export_db`。

#### 部署（DEPLOY）

**D1. 前提条件**：测试套件通过；待发布的 `tools/` 有经负责人签署的 sha256 清单，部署前用 `sha256sum -c <清单文件>` 校验。

> 原文：沙箱测试套件通过，`W4-acceptance.md` 为 PASS，督导已签署 G1，L-15 实时侧栏已验证；在沙箱根目录用 `sha256sum -c ../notion-obsidian-coordination/G1.sha256` 校验签署的发布物；本次发布使用该协作目录中的单进程脚本 `s5_deploy.sh` 和 `S5_DEPLOY_PLAN.md`。这些文件均未收入本仓库。

**D2. 停止两个定时器，确认没有正在运行的轮次：**

```bash
systemctl --user stop notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer
systemctl --user is-active notion-obsidian-refresh@incremental.service notion-obsidian-refresh@full.service
# 两者都不得输出 "active" 或 "activating"
flock -n state/refresh.lock true && echo lock-free
# 重复执行直到输出 lock-free；绝不删除锁文件
```

`state/export.db-journal` 不得存在（存在说明有被中断的轮次：停止操作并调查）。

**D3. 把 `tools/` 和 `state/export.db` 备份到一个新目录 B：**

```bash
B=backup/deploy-$(date +%Y%m%dT%H%M); mkdir "$B"
cp -a tools "$B/tools"; [ -d config ] && cp -a config "$B/config"; ls -la state > "$B/state-files.txt"
python3 -I -B -c 'import sqlite3,sys; s=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); print(d.execute("PRAGMA integrity_check").fetchone()[0], [r[1] for r in d.execute("PRAGMA table_info(published_files)")]); d.close(); s.close()' state/export.db "$B/export.db"
sha256sum "$B/export.db" > "$B/SHA256SUMS"
```

期望输出 `ok`，以及 6 个旧列 `rel_path, sha256, notion_id, kind, run_id, status`（没有 `staged_sha256`）。

**D4.** 安装经签署清单校验过的 `tools/` 与 `docs/` 条目（不含 `__pycache__`；原文为 G1.sha256 中签署的条目）。创建 `config/writeback.json`（除非负责人另有决定，`mode` 为 `"off"`；D-02）。

**D5. 手动运行一轮**：`python3 -I -B tools/refresh.py --mode incremental`。检查结果和校验（C01–C10 为 PASS；`published_files` 已有 `staged_sha256`）。然后重新启动定时器：

```bash
systemctl --user start notion-obsidian-refresh-incremental.timer notion-obsidian-refresh-full.timer
```

#### 回退（ROLLBACK）

回退到旧版工具**不会撤销已写入 Notion 的内容**。如果运行过 `writeback.py apply`，先在新版工具仍安装时处理这些计划：由用户本人在终端运行 `writeback.py rollback` / `resolve`。

**R1.** 按 D2 停止定时器并等待锁释放；另外执行：`flock -n state/writeback.lock true && echo lock-free`

**R2. 把当前状态备份到新目录 R，再放回旧工具：**

```bash
R=backup/rollback-$(date +%Y%m%dT%H%M); mkdir "$R"
# 执行 D3 中的 python3 备份命令，把 state/export.db 备份为 "$R/export.db"
mv tools "$R/tools.new" && cp -a "$B/tools" tools
```

**R3. export.db 只能二选一。** 绝不在已迁移的 `export.db` 上直接启动旧工具。

(a) 恢复部署前的副本。这是精确的部署前状态，适合 D5 之后还没有任何轮次发布的情况：

```bash
python3 -I -B -c 'import sqlite3,sys; s=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$B/export.db" state/export.db
```

此后，新工具在 D5 之后发布的文件在 `published_files` 中缺失或记录的是较旧的 sha。若 Notion 再次修改这类笔记，旧发布器会把它当作本地修改并写出冲突副本（部署前的行为）。

(b) 保留当前行，只删除新增列（需要 SQLite ≥ 3.35；生产 python3 为 3.45.1）：

```bash
python3 -I -B -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("ALTER TABLE published_files DROP COLUMN staged_sha256"); c.commit(); print([r[1] for r in c.execute("PRAGMA table_info(published_files)")])' state/export.db
```

期望得到 6 个旧列。旧工具会忽略新增的 `conflict_copies`、`publish_intents` 两张表，也会忽略状态值为 `renamed`、`trashed_via_writeback` 的行，因为它只读取 `status = 'active'` 的行。

**R4. 把只有新工具使用的状态文件移到 `"$R/state/"`（移动，不要删除）。** 旧工具会忽略它们。以后重新部署时不能从陈旧的基底或陈旧的暂停开始；把这些文件移走后，重新部署会像首次部署一样重新建立（D-20）：

```bash
mkdir "$R/state"; for f in writeback.db writeback.db-wal writeback.db-shm base auto_sync_hold.json paused_runs.json refresh_pause_next.json last_published.json last_published_snapshot.txt last_adopted_wb_snapshot.json runlog_standing_digest.json rate_gate.json sidebar_capture_failures.json WRITEBACK_STOP writeback.lock; do [ -e "state/$f" ] && mv "state/$f" "$R/state/"; done
```

保留旧工具要用的 `export.db`（R3）、`latest_snapshot.txt`、`refresh.lock` 和 `roots_*.json`。`snapshots/wb/` 与 `reports/writeback/` 可以保留。旧工具不读 `config/writeback.json`：若 `"$B/config"` 存在则恢复它，否则把它移到 `"$R/"`。

**R5.** 用旧工具手动运行一轮（`python3 -I tools/refresh.py --mode incremental`），确认退出码为 0，然后按 D5 重新启动两个定时器。

### 6.3 隔离测试复现

> 本节描述原开发沙箱内部的复现流程。所需夹具（`README-DEV.txt`、`IMPL_CONTRACT.md`、沙箱 `vault/`、迁移前 `export.db`、`fixtures/nmig` 适配器、G1 证据启动器）均未收入仓库，**在本仓库中不可复现**；在仓库中请使用第 4.1 节。

1. 从一份全新的工作副本开始，包含 `tools/`、`docs/`、`config/writeback.json`、`README-DEV.txt` 和 `_resume_20261118/impl/IMPL_CONTRACT.md`。
2. 提供沙箱夹具 `vault/`（包括 Markdown 与插件配置）和隔离的、迁移前的六列 `state/export.db`。冒烟测试和迁移测试需要这两个夹具。使用只读的输入副本和可写的私有 state 目录。
3. refresh 子进程使用挂载到其预期导入路径上的本地 `fixtures/nmig` 适配器。
4. 用空 `HOME`、私有 `TMPDIR`、假的远端客户端并禁用网络运行。在该工作根目录执行：

   ```bash
   python3 -I -B -m unittest discover -s tools/tests -p 'test_*.py' -v
   ```

5. 用 G1 证据启动器复现挂载、用户和配置，检查跳过数为零。

在本仓库中复现时，第 4.1 节的命令即可运行全部不依赖沙箱夹具的测试。

<a id="dev-tasks"></a>
## 7. 常见开发任务的注意事项

| 任务 | 需要同步修改的地方 |
|---|---|
| 新增 / 修改写回配置键 | `tools/wb_config.py` 的 `DEFAULTS` 与 `_validate()`；未知键会被拒绝，所以同时更新 [`examples/writeback.example.json`](../../examples/writeback.example.json) 和[部署指南 · 写回配置](../deployment/deployment-guide.md#writeback-config)；规格见 [WRITEBACK_SPEC §15](../design/WRITEBACK_SPEC.md) |
| 修改导出渲染 | 渲染器版本变化后，下一轮会以 `renderer_version_changed` 整轮暂停，需本人核对后放行；同时检查 `tests/test_export_block_lines.py` 与 `verify_vault.py` 的 C01–C10 |
| 新增状态文件 | 若只有新工具使用，把它加入第 6.2 节 R4 的 `mv` 清单（`test_docs_consistency.py` 在沙箱中检查这一点） |
| 修改 `export.db` 结构 | 只能由 `wb_state.ensure_export_db` 在发布时迁移；同步更新第 6.2 节的部署 / 回退说明 |
| 修改团队协作区或路径 | 这些值写死在多个模块中，见 [README · 移植性说明](../../README.md#portability) |
| 修改用户可见命令或提示 | 同步更新[操作手册](../usage/operations-manual.md)，避免文档与 `--help` 不一致 |
