# 1.2 设计探查：归档 / 删除会话

- **状态**：✅ 已实施并部署（2026-09-08）
- **日期**：2026-09-08
- **对应未决项**：[`OPEN_ITEMS.md`](../../OPEN_ITEMS.md) §1.2
- **结论**：技术可行，机制已实测。不做跨表混合检索——**单表检索 + `source` 参数 + 已归档提示**；维护函数必须同时覆盖归档表。

---

## 1. 语义与工具面

| 操作 | 语义 |
|---|---|
| `session_admin(action='archive')` | 把该会话全部行从 `messages` 搬到 `messages_archive`（主表不再出现，可恢复）；支持 `dry_run=true` |
| `session_admin(action='restore')` | 从 `messages_archive` 搬回 `messages` |
| `session_admin(action='delete')` | 从**归档表**永久删除（**不可恢复**）。**只能删已归档会话**——删活跃会话报 `E_NOTARCHIVED`（提示先 `archive`）。**两阶段**：第一次调用只登记意向并返回含询问工具参数的固定文案，用户确认后在 TTL 内再次调用同一 action 才执行 |
| ~~`session_admin(action='list')`~~ | **已于 2026-09-08 移除**（工具面去重）：列出统一走 `list_sessions(source=…)`，`session_admin` 只保留 archive / restore / delete |
| **检索/列表** | `recall(source)` / `recent(source)` / `list_sessions(source)` 用 `source` 参数选表（`messages` 默认 / `archive`） |

删除闸门细节：状态是进程内 `session_id → 意向到期时间`（重启即清空，fail-safe）；TTL 由 `CHAT_HISTORY_DELETE_TTL` 配置（默认 60 秒）；过期后再次调用会被当作"新的第一次调用"，重新返回询问文案，不报错。

第一次 `delete` 返回的固定文案（含给询问工具的完整参数，AI 照抄、不要直接发给用户）：

```
【待确认 · 永久删除已归档会话】
会话：{标题或无标题}
ID：{session_id}
消息：{N} 行
影响：删除后不可恢复（该会话当前在归档表中）

请调用询问工具（ZCode 下为 AskUserQuestion），参数如下——不要把这段 JSON 直接发给用户：
{
  "questions": [{
    "question": "是否确认永久删除会话「{标题}」？删除后无法恢复。",
    "header": "删除确认",
    "multiSelect": false,
    "options": [
      {"label": "确认删除", "description": "{session_id} · {N} 行 · 删除后不可恢复"},
      {"label": "取消", "description": "保留该会话，不做任何改动"}
    ]
  }]
}

- 用户选择「确认删除」后，请再次调用 session_admin(action=delete, session=...)，本次意向 {ttl}后失效；
- 用户选择「取消」或未确认时，请不要再次调用。
```

- `source` 取值：**`messages`（默认）/ `archive`**，三处统一；非法值报 `E_INVALID`。
- `session` 参数复用 `title_dispatcher.resolve_session`，既可传 `sess_xxx` 也可传标题。
- **不做**跨表合并检索：一次调用只查一张表。

---

## 2. 现状事实

| 事实 | 证据 |
|---|---|
| 主表混装所有会话 | 生产 27 个会话 / 4,977 行 |
| 无用会话确实存在 | 1–8 行的小会话共 **11 个**（"你好"/"hi"/"排查一下"…） |
| 标题缓存是平铺 `id -> title` | 36 条，其中 **9 条空标题垃圾键**（`sess_x`/`sess_PROBE`/`sess_MIG_TEST`/`sess_HOOKTEST`/`sess_test`/`sess_lat`…） |
| `recall`/`recent`/`list_sessions` 只读 `messages` | 归档后自动不可见；改 `source` 才能查归档 |
| 标题反查会从 ZCode 重新学回 | 清缓存键 ≠ 隐藏会话；`recall(session=<标题>)` 仍能解析 |

---

## 3. 机制实测（scratch 库，全部通过）

| 验证 | 结果 |
|---|---|
| 建归档表 | `db.create_table("messages_archive", schema=Msg)` ✅ |
| 搬行 | 读主表行（**含 vector，无需重新嵌入**）→ `arch.add(rows)` → `main.delete(where)` ✅ |
| 删除不存在的会话 | `main.delete(...)` 静默无副作用 ✅ |
| 恢复 | 归档行搬回主表、归档表清空 ✅ |
| **删除后 FTS 是否仍返回已删行** | **不会**：命中数立即 6 → 3，optimize/reindex 后仍 3 ✅ |
| dict 写入字段完整性 | `add()` 传 dict 必须含全部列（缺 `round`/`step` 会 `RuntimeError`）；从表读出的行自带全部列 ✅ |
| 空间回收 | `delete` 是逻辑删除，磁盘靠 `optimize` 回收（启动自动清理 / `vacuum` 兜底） |

---

## 4. 原子性与并发

搬行是「读主表 → 写归档表 → 删主表」三步，非原子。设计：

1. **顺序**：先写归档表 → **校验归档行数 == 待搬行数** → 再删主表。中途失败只留下"归档多一份"，主表未动，可重跑。
2. **幂等**：搬之前先 `archive.delete("session_id = X")` 清该会话旧归档行，重跑不堆重复。
3. **并发**：持 `db._WRITE_LOCK` + `db._session_lock(session_id)`，与 1.1 写入路径互斥；归档期间该会话不会被插入新行。
4. **删除**：只删主表一步，但仍持同样的锁。

---

## 5. `source` 参数与已归档提示

- `recall` / `recent` / `list_sessions` 增加 `source="messages"|"archive"`（默认 `messages`）。
- `recall(source='archive')` 依赖归档表的 FTS 索引 → **建表时创建，且每次归档/恢复后重建**（索引不随 `add` 更新）。
- **已归档提示**：仅当「传了 `session` 且目标表结果为空」时，额外查一次另一张表的计数：
  - 另一张表有行 → 返回提示，含行数与正确的 `source` 取值，例如：
    `该会话已归档（12 行），请用 source='archive' 检索，或 restore_session 恢复。`
  - 两张表都没有 → 保持空结果（不误导）。
- 未传 `session` 的全局查询不触发额外查询，零开销。
- 展示层：归档行带 `source` 标记，避免 AI 分不清来源。

---

## 6. 维护必须包络归档表

| 位置 | 改动 |
|---|---|
| `db._migrate_messages_schema(db, table=TABLE)` | 支持表名；启动时对 `messages` 与 `messages_archive` 各跑一次（后者不存在则跳过） |
| `maintenance.garbage_bytes()` | 统计**两张表**的 `_versions` 字节和（否则归档表的空间无人回收） |
| `maintenance._run()` / `reindex()` | 对两表都执行 optimize / 重建 FTS |
| `tools/backup.py` `vacuum` / `reindex` / `verify` | 覆盖两表（索引统计、行数校验） |
| `list_sessions(source)` | 复用 `_summary_rows(tbl)`，传入目标表 |

---

## 7. 改动清单

- **`config.py`**：`ARCHIVE_TABLE = "messages_archive"`。
- **新增 `archive.py`**：`_archive_table(db)`、`archive_session(session_id, dry_run=False)`、`restore_session(session_id)`、`delete_session(session_id, confirm=False)`、`count_rows(session_id, table)`（供提示用）。
- **`db.py`**：`_open_table_or_none(db, name)`、`_migrate_messages_schema` 支持表名、`_summary_rows` 支持表名。
- **`core.py`**：`search_recall`/`search_recent`/`list_sessions` 加 `source`；空结果时的已归档提示；3 个新 MCP 工具。
- **`title_cache.py`**：`purge(session_id)`（归档/删除后清该会话键）、`drop_empty()`（清空标题垃圾键）。
- **`maintenance.py` / `tools/backup.py`**：按 §6 包络归档表。
- **测试**：`tests/test_archive.py`（归档/恢复/删除/幂等/dry_run/confirm 缺失被拒/vector 保留/归档后主表不可见/提示文案/source 参数/维护包络）。

---

## 8. 已定决策

1. **不做跨表混合检索**：单表检索，`source` 选择表；不提供 `all`。
2. **`list_archived` 不单独开工具**：由 `list_sessions(source='archive')` 承担。
3. **已归档提示**：传了 `session` 且结果为空时给出可操作提示。
4. **维护包络归档表**：迁移、GC、reindex、vacuum、verify 全覆盖。
5. **删除用两阶段闸门**（取代显式 `confirm=true`）：第一次调用登记意向并返回固定询问文案，TTL 内再次调用才执行；状态在进程内存、重启即清空；TTL 由 `CHAT_HISTORY_DELETE_TTL` 配置（默认 60 秒）。
6. **归档表建 FTS 索引**，并在归档/恢复后重建。
7. **工具面收敛为单个 `session_admin(action=…)`**，取代原先的 `archive_session`/`restore_session`/`delete_session` 三个工具。
8. **删除只针对已归档会话**：活跃会话报 `E_NOTARCHIVED`（新增错误码，见 `error_codes.json`），必须"先归档、再删除"。
9. **第一次删除返回结构化询问参数**（`question`/`header`/`multiSelect`/`options`），选项固定为「确认删除 / 取消」，并明确要求不要直接把 JSON 发给用户。

---

## 9. 实施顺序

1. `config.py` + `db.py`（表名参数化）+ `archive.py`
2. `tests/test_archive.py`，隔离库跑通
3. `core.py`（`source`、提示、3 个工具）+ `title_cache`（purge/drop_empty）
4. `maintenance.py` / `tools/backup.py` 包络归档表 + 补测试
5. 备份 → 部署 → 重启验证（先归档一个 1 行小会话，确认可恢复，再恢复回来）
6. 文档更新

---

## 10. 实施记录（2026-09-08）

| 项 | 内容 |
|---|---|
| 改动文件 | `config.py`（`ARCHIVE_TABLE`）；`db.py`（`_open_table_or_none`、`_migrate_messages_schema(table=)`）；**新增 `archive.py`**；`core.py`（`source` 参数、已归档提示、来源标记、**单个 `session_admin(action=…)` 工具 + 两阶段删除闸门**）；`title_cache.py`（`purge`/`drop_empty`）；`mcp_server.py`（启动迁移覆盖两表）；`maintenance.py`（GC 覆盖两表）；`tools/backup.py`（`vacuum`/`reindex`/`verify` 覆盖两表） |
| 新增测试 | `tests/test_archive.py` 14 例 + `tests/test_adapters.py` 4 例（两阶段删除、意向过期、archive/restore/list、非法 action） |
| 全量测试 | **160 passed / 0 failed / 1 skipped**（连续 3 次全绿；此前出现过 1 次 HTTP 测试的瞬时超时 flake，未复现） |
| 生产副本实测 | 归档：主表 1→0、归档表 0→1；归档表 FTS 索引建立；`recent(source='archive')` 命中并带 `\| archive` 标记；主表查询返回提示；`recall(source='archive')` 带 score 命中；恢复归位 |
| 生产实测（净零变更） | 用 1 行小会话 `sess_be38bbbd…` 走安装版代码：归档 → `list_sessions(source='archive')` 列出 → `recent` 命中 → 主表查询提示 → 恢复 → 主表 1 行、归档表 0 行 |
| 备份 | `.agent/backups/chat-history-archive-20260908-195000/`（34 文件 + config + SHA-256 清单） |
| 已知 | 新增的 3 个 MCP 工具要 **ZCode 重启或新开会话**才会出现在客户端的工具列表里（MCP 进程已重连，但客户端工具清单未刷新） |

---

## 11. 后续调整（2026-09-08 晚）

**移除 `session_admin(action='list')`，列出统一走 `list_sessions(source=…)`。**

原因：本轮实施时 `list` 被同时留在 `session_admin` 和独立的 `list_sessions` 工具里，形成两个列表入口且语义不等价——`session_admin` 的 `list` 硬编码 `source='archive'`（只能列归档），`list_sessions` 两表都能列。列出是只读操作，不属于"会话管理"，故收敛为单一入口。

- `session_admin` 动作收敛为 `archive` / `restore` / `delete` 三个；非法 action 返回 `E_INVALID`。
- 工具面保持 5 个：`remember` / `recall` / `recent` / `list_sessions` / `session_admin`。
- 测试：`test_session_admin_archive_restore` 改用 `list_sessions(source='archive')` 断言；新增 `test_session_admin_list_moved_to_list_sessions`。全量 **161 passed / 0 failed / 1 skipped**。
- 备份：`.agent/backups/chat-history-list-consolidate-20260908-204928/`。
