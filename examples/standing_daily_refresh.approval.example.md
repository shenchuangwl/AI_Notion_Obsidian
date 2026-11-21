# 授权记录：每日自动增量发布（常设）

<!--
用法：复制为 <工作目录>/approvals/standing_daily_refresh.approval.md（生产为
/data120/Obsidian/notion-obsidian/approvals/standing_daily_refresh.approval.md），按实际情况改写下列各项。

代码如何使用本文件（tools/refresh.py STANDING_APPROVAL）：
- 非 --dry-run 的刷新轮次在读取快照、发布笔记之前检查该文件是否存在；缺失时本轮以
  “missing standing approval” 失败，不清理、不发布。
- 发布阶段把它作为 publish_vault.py --apply --approval 的审批记录，路径写入 reports/<轮次>/publish_journal.jsonl。
- 程序不解析正文内容；正文是给人看的授权依据，请如实记录授权人、时间和范围，不要写入任何令牌或密码。
-->

- 授权人：<库与 Notion 工作区的所有者姓名或账号>
- 授权时间：<YYYY-MM-DD HH:MM 时区>
- 授权方式：<书面答复 / 工单号 / 会议纪要等可追溯的记录>

## 授权范围

1. 目标库：`/data120/Obsidian/Godcreate`（只限这一个 Obsidian 库）。
2. 数据来源：Notion 工作区 `<工作区名称>` 中已共享给只读集成的页面，按以下团队协作区顺序：General / Quantitative Investing / x_to_notion / Godcreate Business / Godcreate AI工作空间。
3. 允许的写入：按暂存清单新增文件；只替换本工具此前发布且未被本地修改的文件；不删除库内任何文件；冲突时保留双方版本。
4. 允许在库的 `.obsidian/` 下写入 custom-sort 插件文件与 `community-plugins.json`（仅当文件不存在或与本工具发布值一致）。
5. 允许部署每日增量与每周全量的 systemd 用户定时器（单向 Notion → 库）。

## 不在授权范围内

- 连接、删除或重置任何 Obsidian Sync 远程库，输入或保存同步密码。
- 修改 Notion 内容（Obsidian → Notion 写回另有逐次审批流程，见操作手册第 4 节）。
- 写入其他库或其他目录。

## 变更记录

| 日期 | 变更 | 依据 |
|---|---|---|
| <YYYY-MM-DD> | 首次授权 | <记录位置> |
