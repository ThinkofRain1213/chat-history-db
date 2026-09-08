# chat-history 修复流程规范报告（2026-09-08 验证版）

> 状态：**验证完成，未实施任何修复**。  
> 边界：生产库只读；所有写操作仅发生在临时副本（已清理）；安装版、生产库、源码均未改动。  
> 解释器：`C:/Users/Think/.agent/tools/chat-history/.venv/Scripts/python.exe`（lancedb 0.38.0）

---

## 1. 验证结论总表

| # | 问题 | 验证结论 | 严重度 |
|---|---|---|---|
| 1 | FTS 索引严重滞后 | **确认**：`num_indexed_rows=128` / `num_unindexed_rows=4443`（共 4,571 行），BM25 仅覆盖 2.8% | **P0 正确性** |
| 2 | 版本清单膨胀 | **确认**：副本 13,852 文件 / 903.96 MiB，其中清单占绝大多数 | **P0 空间** |
| 3 | optimize 效果 | **实测**：903.96 → 21.17 MiB（-97.7%），13,852 → 18 文件，行数 4,571 不变 | — |
| 4 | FTS 是否随 optimize 修复 | **确认修复**：128/4443 → **4571/0** | — |
| 5 | 优化后功能是否正常 | **通过**：health 全绿、recall 6.1s 返回正确结果、27 会话可读 | — |
| 6 | `round` 列名是否与 SQL 冲突 | **无冲突**：filter / order_by / add_columns 均正常 | — |
| 7 | `add_columns` 能否回填历史行 | **可以**，`transforms` 立即求值；二次执行抛 `ValueError: Column round already exists` | 需幂等保护 |
| 8 | `merge_insert` 能否幂等去重 | **可以**：同键重复插入后仍为 1 行 | — |
| 9 | 数据是否丢失 | **无丢失**：备份 4,442 行全部存在于生产库 | — |
| 10 | `mid 未取到` 根因 | **修正**：子代理工具调用的 model IO 落在子代理 rollout 文件，hook 只查主会话文件 | P2 数据质量 |
| 11 | worker 默认路径隐患 | **确认**：`chat_worker.py:19,29` 默认指向项目版空库，hook 默认指向安装版 | **P0 隐患** |

---

## 2. 逐项验证详情

### 2.1 FTS 索引滞后（最严重，此前未发现）

```
INDEX text_idx FTS ['text']
  num_indexed_rows   = 128
  num_unindexed_rows = 4443
  size_bytes         = 90,640
  created_at         = 2026-09-05 16:50:10 (北京时间)
  base_tokenizer     = icu
  num_segments       = 1
```

索引在表只有 128 行时创建，此后 4,400+ 次写入都未进入索引。**后果**：`recall` 的混合检索中 BM25 分支实际只覆盖 2.8% 数据，语义检索仍靠向量分支兜底，但关键词精确匹配基本失效。

### 2.2 / 2.3 体积与 optimize 实测（生产库副本）

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| 文件数 | 13,852 | **18** |
| 体积 | 903.96 MiB | **21.17 MiB** |
| 行数 | 4,571 | 4,571（不变） |
| FTS 已索引 | 128 | **4,571** |
| FTS 未索引 | 4,443 | **0** |
| 版本号 | 4,646 | 4,649 |

- 复制耗时 9.1s，`optimize` 耗时 17.6s
- 单条清单体积从第 1 个的 1,126 B 增长到最新的 407,042 B，累加呈二次方
- 增长速率：约 2 小时 +39 MiB / +303 版本，平均每次写入约 0.13 MiB

### 2.4 优化后功能验证（副本）

```
health = {'ok': True, 'db': 'ok', 'schema': True, 'fts': True, 'embeddings': True, 'reranker': True}
recall  = 6.1s, 3 条结果，最高分 2.99（内容正确）
list_sessions = 27
```

### 2.5 schema 迁移能力（内存库）

```python
t.add_columns(transforms={'round': '0', 'step': 'turn'})   # 成功，历史行回填 round=0/step=turn
t.search().where('round = 0')                              # 成功
order_by([ColumnOrdering(column_name='round', ...)])       # 成功
t.add_columns(transforms={'round': '0'})                   # ValueError: Column round already exists
```

结论：`round` 可直接作列名；补列必须做「先检查列是否存在」的幂等保护。

### 2.6 `mid 未取到` 根因（修正）

- `rollout/` 目录现有 4 个文件：1 个主会话 + 3 个子代理
- 抽查失败 cid：均**同时出现**在子代理 rollout 文件与主会话文件中
- 但主会话文件中该 cid 只作为**请求历史**出现，不含 `response.toolCalls` 的匹配项；hook 的匹配条件要求 `response.toolCalls` 含该 cid
- 即：**子代理发起的工具调用，其模型响应只落在子代理的 rollout 文件**，而 hook 只查主会话文件
- 今日日志统计：`mid 未取到` 64 次，`user 入队` 50 次，`final 入队` 21 次

### 2.7 worker 默认路径（隐患）

```
chat_hook.py:19  HOME    = ...\.agent\tools\chat-history        （安装版）
chat_hook.py:24  CHAT_DB = HOME/chat.db                          （安装版）
chat_worker.py:19 CHAT_DB = ...\Desktop\项目\chat-history-db\chat.db （项目版空库）
chat_worker.py:29 PROJ    = ...\Desktop\项目\chat-history-db        （项目版）
```

运行时靠 hook 通过 `env.setdefault` 注入正确路径；一旦环境变量缺失（手动调用、其他 harness、hook 改动），worker 会加载项目版代码并写入那个约 3 KiB 的空库，表现为"入库成功"但数据进了另一个库。

---

## 3. 修复流程规范

### 阶段 A：数据库治理（P0，建议先做）

#### A1 `tools/backup.py` 新增 `vacuum` 子命令

**落点**
- `tools/backup.py`：新增 `vacuum()` 函数（与 `backup`/`verify` 并列，约行 97 后）
- argparse：行 118 后加 `sub.add_parser("vacuum", ...)`
- 分发：行 133 `elif args.cmd == "verify":` 后插入分支
- `tests/test_backup.py::BackupTests`（行 12）新增用例
- `BACKUP.md` 补文档

**参数**
| 参数 | 说明 |
|---|---|
| `--yes` | 跳过交互确认 |
| `--force` | 跳过「MCP 正在运行」检查（危险，默认禁止） |
| `--json` | 输出机器可读统计 |

**执行规范（顺序不可变）**
1. 前置检查：17891 端口是否被监听、是否存在 `chat_worker` 进程 → 有则拒绝（除非 `--force`）
2. 前置检查：`~/.agent/backups/chat-history-db-*` 至少存在一个备份 → 无则拒绝
3. 记录 before：文件数、体积、行数、`num_indexed_rows` / `num_unindexed_rows`
4. 执行 `tbl.optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=True)`
5. 重新打开表，记录 after 并断言：
   - 行数不变
   - `num_unindexed_rows == 0`
   - 文件数与体积显著下降
6. 抽样验证：`search(..., query_type='fts')` 有结果、`search().select(...)` 正常

**失败回滚**：`optimize` 不改变行数据，仅清理旧版本；若中断，从最近备份恢复整个 `chat.db` 目录即可。

**验收阈值（基于实测）**
- 体积下降 > 95%（当前预期 903.96 → 约 21 MiB）
- 文件数下降 > 99%（13,852 → 约 18）
- 行数与执行前完全一致
- `num_unindexed_rows == 0`

#### A2 FTS 重建兜底

若 `optimize` 后仍有未索引行，新增 `reindex` 子命令：
```python
tbl.create_index("text", config=FTS(base_tokenizer="icu"), replace=True)
```

#### A3 定期调度规范

- 频率：按写入量而非纯时间。实测每次写入约 0.13 MiB 版本开销，建议「累计写入 500 条」或「每周一次」取先到者
- 形式：由 ZCode 的定时任务或 Windows 计划任务调用 `python tools/backup.py vacuum --yes`
- 每次执行前自动做一次备份（可加 `--backup` 开关）
- 只读体检（不清理）可每日执行：统计文件数/体积/未索引行数，异常告警

### 阶段 B：`turn` → `round` + `step`（历史不回填）

**语义**：`turn` 保留为会话内全局序号（向后兼容）；新增 `round`（对话轮次）与 `step`（轮内步骤）。

**落点**
| 文件 | 改动 |
|---|---|
| `db.py` | `Msg`（行 19-26）加 `round: int = 0`、`step: int = 0`；新增 `_ensure_columns()`（幂等补列）；新增 `_session_max_round()` / `_round_max_step()` |
| `mcp_server.py` | `main()`（行 38-46）启动顺序改为「补列 → 校验 → HTTP → MCP」；re-export 新符号 |
| `core.py` | `remember()`（行 41-84）加 `round/step` 参数与推导；`search_recall`/`search_recent` 增加字段；`_format_recall`（144/149/151）、`_format_recent`（224/228/230）显示 `#round.step`，`round==0` 回退 `#turn`；`_handle_remember`（352-371）与 `_remember_tool`（300-308）接受新参数 |
| `tests/` | `test_storage.py::StorageTests`（轮次分配）、`SchemaTests`（补列幂等）、`test_logic.py`（格式）、`test_adapters.py::McpWrapperTests`（转发） |

**推导规则**
- `kind == "user"` → `round = max_round + 1`，`step = 1`
- 其他 kind → `round = max_round or 1`，`step = 该 round 内最大 step + 1`
- 显式传入优先
- 历史行 `round = 0`，展示层回退为 `#turn`

**补列迁移规范**
```python
existing = {f.name for f in tbl.schema}
add = {}
if 'round' not in existing: add['round'] = '0'
if 'step'  not in existing: add['step']  = 'turn'
if add: tbl.add_columns(transforms=add)   # 立即求值，幂等由 existing 检查保证
```

### 阶段 C：归档 / 删除会话（按已确认语义：搬表归档 + 永久删除）

**落点**
- 新模块 `archive.py`：`archive_session` / `restore_session` / `delete_session` / `list_archived`
- `db.py`：`ARCHIVE_TABLE = "messages_archive"`、`_ensure_archive`、`_open_archive_or_none`
- `core.py`：新增 4 个 MCP 工具
- `title_cache.py`：新增 `purge(session_id)`
- `tools/backup.py`：可选 `archive` 子命令
- `tests/test_storage.py` 新建 `ArchiveTests`

**语义与顺序**
1. 归档：读出该会话全部行 → 写入 `messages_archive`（同 schema）→ 确认写入成功 → 从主表 `delete` → 清理标题缓存
2. 恢复：反向搬回
3. 删除：主表 + 归档表 + 标题缓存全部清除
4. 归档表建 FTS 索引；删除后建议触发一次 `vacuum`

**并发与失败保护**：整个搬移在 `_WRITE_LOCK` 内串行；先写后删，写失败则不删。

### 阶段 D：写入幂等（待你选方案）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A（推荐） | hook 传 `chat_pending.id` 作为 `dedup_key`，表加列 + `merge_insert(on='dedup_key').when_not_matched_insert_all()` | 对重试/孤儿恢复完全幂等 | MCP 直写无 pid，需退化为内容哈希 |
| B | 统一 `sha256(session_id|kind|text)` 作为键 | 实现简单 | 可能误杀"用户重复发同一句话" |
| C | 暂不做 | 零风险 | 重试仍可能重复入库 |

已验证 `merge_insert` 去重行为正确。

### 阶段 E：hooks 修复（**不在项目版**，`C:\Users\Think\.agent\hooks\`）

| 项 | 落点 | 改法 |
|---|---|---|
| E1 worker 默认路径 | `chat_worker.py:19,29` | 默认改为安装版；或改为「env 缺失时显式报错」，不再静默落到空库 |
| E2 `mid` 回退 | `chat_hook.py:91-125` | miss 时在 rollout 目录内按近期 mtime + 体积上限回退扫描（限定最近 N 个文件）；或把 cid→文件映射缓存 |
| E3 tool 描述截断 | `chat_hook.py:177` | `[:300]` 改为可配置上限（如 2000） |

---

## 4. 生产执行规范（vacuum 维护窗口 SOP）

1. **备份**：`python tools/backup.py backup`，确认备份目录与清单生成
2. **停机**：停止安装版 MCP 进程，确认 17891 无监听、无 `chat_worker` 进程
3. **记录基线**：行数、文件数、体积、索引覆盖（`num_indexed_rows` / `num_unindexed_rows`）
4. **执行**：`python tools/backup.py vacuum --yes`
5. **验收**：
   - 行数与基线完全一致
   - 体积下降 > 95%，文件数下降 > 99%
   - `num_unindexed_rows == 0`
   - `health` 全绿
   - `recall` 抽样 3 条语义正确、`recent` 正常
6. **重启**：重启 ZCode 使 MCP 重新加载，确认 hook 写入链路恢复、pending 队列清空
7. **回滚**：任一项不达标 → 停止 MCP → 用备份目录整体恢复 `chat.db` → 重启 → 只读冒烟

---

## 5. 验收标准清单

- [ ] 项目版 `python -m unittest discover` 全绿，失败 0
- [ ] `vacuum` 在隔离库：行数不变、体积/文件数达标、`num_unindexed_rows == 0`
- [ ] 补列迁移对已有表连续执行两次不报错，旧表启动不失败
- [ ] 新写入 `round/step` 正确；历史行 `round=0` 显示回退 `#turn`
- [ ] 归档可往返（归档 → 恢复 → 数据一致）；删除后主表与归档表无残留
- [ ] 全程不改动安装版与生产库，除非进入单独维护窗口

---

## 6. 未决问题

> **清单已迁移到独立文档 [`OPEN_ITEMS.md`](../../OPEN_ITEMS.md)**，自 2026-09-08 19:18 起为唯一权威来源，本节不再维护。
>
> 本节原先的 4 个问题已全部收敛：范围取 P0 治理（已完成）、幂等暂不做、hooks 下轮、vacuum 由「MCP 启动自动清理」替代并已上线。两个收尾项（部署 `tools/`、补回 `README.md`）亦已完成。

---

## 7. 实施状态（2026-09-08）

> 本节记录当天早些时候的状态快照；文中「未部署到安装版」等表述此后已不成立——实际已于同日部署。最新状态见 §8 与 [`OPEN_ITEMS.md`](../../OPEN_ITEMS.md)。

### 已完成（仅项目版，未部署到安装版）

| 文件 | 改动 |
|---|---|
| `tools/backup.py` | 新增 `vacuum` 子命令（自动备份 + MCP/队列安全检查 + 前后统计 + 行数校验 + FTS 兜底重建）与 `reindex` 子命令 |
| `tests/test_backup.py` | 新增 `VacuumTests` 7 个用例：减少文件、保留行数、MCP 在跑拒绝、队列积压拒绝、`--force` 跳过、缺备份拒绝、非交互缺 `--yes` 拒绝、reindex 清空未索引 |
| `BACKUP.md` | 新增「4. 空间治理（vacuum / reindex）」章节 |

**全量测试**：117 passed / 0 failed / 1 skipped（可选真实 ONNX 模型测试）

### 真实环境验证

- `python tools/backup.py --help` 已列出 `vacuum` 与 `reindex`
- 真实环境下执行 `vacuum --yes` 被正确拒绝：`MCP 正在监听 127.0.0.1:17891`，未产生任何写入或备份

### 未执行（按用户决定）

- 生产库 vacuum：暂缓，等维护窗口
- 部署到安装版：未做；本批代码验收后再走 `MIGRATION_PLAN.md` 流程
- `round`/`step`、归档删除、写入幂等、hooks 修复：下轮

---

## 8. 实施状态：MCP 启动自动清理（2026-09-08，已部署）

### 决策过程与结论

需求是"库涨得太快，自动治理"。比较过三种触发点后选定 **MCP 进程内启动触发**：

| 方案 | 结论 |
|---|---|
| ZCode 会话开始钩子 | 否决。触发信号错配（垃圾随写入量涨、不随会话数涨）、同步阻塞、多会话并发互撞、钩子 stdout 契约易错 |
| 钩子只入队 + 后台串行执行 | 可行但多余。队列只解决"怎么执行"，不解决"该不该执行"；且需新增常驻消费者 |
| **MCP 进程内启动触发（采纳）** | 进程内即唯一写入者，无跨进程协调、无新组件、无需改 hooks |

阈值先定 25 MiB，后按用户要求调为 **10 MiB**。

### 改动清单

| 文件 | 改动 |
|---|---|
| `maintenance.py`（新增） | `garbage_bytes()` 扫描 `_versions` 字节和；`maybe_optimize()` 阈值判断 + 非重入 + 全量异常兜底；`start_background_maintenance()` 起 daemon 线程 |
| `mcp_server.py` | `main()` 中 `_start_http_server()` 之后插入 `maintenance.start_background_maintenance()` |
| `tests/test_maintenance.py`（新增） | 10 个用例：低于阈值跳过、高于阈值执行、异常不外抛、非重入、参数安全（`delete_unverified=False`）、环境变量关闭、表不存在静默、stdout 为空、daemon 线程、日志落文件 |
| `BACKUP.md` | 第 4 节新增「启动时自动清理（默认开启）」 |

关键参数：`CHAT_HISTORY_GC_MB`（默认 10）、`CHAT_HISTORY_GC=0` 关闭、`CHAT_HISTORY_GC_LOG`（默认 `~/.agent/hooks/chat_maintenance.log`）。

### 安全约束（均有实测支撑）

- **`delete_unverified=False`**：`True` 在并发写入下实测 8/20 次 `LanceError(IO): Not found`；`False` 为 0/20，且清理效果完全相同（副本实测 944.97 → 21.64 MiB / 18 文件）。
- **不抢 `db._WRITE_LOCK`**：抢锁会让一次 `/remember` 卡住整个 optimize 时长（大库 17.4s），而 worker 的 HTTP 超时是 30s。
- **不写 stdout**：stdio 是 MCP 协议通道；测试中断言 stdout 为空。
- **启动后延迟 2 秒再动磁盘**，避免与 stdio 握手竞争。

### 生产验证（安装版）

| 指标 | 清理前 | 清理后 |
|---|---:|---:|
| `_versions` | 931.19 MiB / 4,793 文件 | 0.41 MiB / 6 文件 |
| 整个 `chat.db/` | 962.94 MiB / 14,290 文件 | 21.13 MiB / 24 文件 |
| 行数 | 4,673（副本基线） | 4,724（含清理期间新写入，无丢失） |
| FTS 索引 | 4,718 已索引 / 6 未索引 | 同上（未索引来自清理后的新写入） |

- `list_sessions`、`recent`、`recall` 均正常返回（`recall` 带 score 命中）。
- 小库强制清理实测：0.45 → 0.01 MiB，行数 4,740 → 4,740，0.3s。
- 日志实测落盘：`2026-09-08 19:03:06 [chat-history] maintenance cleaned 0.45 MiB -> 0.01 MiB, rows 4740 -> 4740, 0.3s`。

### 部署与回滚

- 备份：`.agent/backups/chat-history-gc-20260908-185647/`（19 个代码/配置文件 + `config.json` + `MANIFEST.json` 含 SHA-256）
- 部署：`maintenance.py`、`mcp_server.py` 复制到 `.agent/tools/chat-history`，哈希校验一致
- 回滚：用备份目录的 `mcp_server.py` 覆盖回安装版即可恢复旧行为（`maintenance.py` 留着不被引用，无副作用）

### 过程中发现并修掉的四个问题

1. **ZCode 不保留 MCP 的 stderr** —— 首轮清理成功但日志无处可查，因此 `_log()` 增加文件落盘（`~/.agent/hooks/chat_maintenance.log`）。
2. **测试污染生产日志** —— 首版测试直接写到真实日志文件；已在 `MaintenanceTests.setUp` 中把 `_DEFAULT_LOG` 指向临时目录，并清理了已写入的测试行，重跑后日志 mtime 未变。
3. **"没清理"不可区分** —— 阈值未触发时原本不写日志，导致"低于阈值"与"维护没跑"无法分辨。已补 skip 日志，实测重启后 3 秒落盘：`2026-09-08 19:09:19 [chat-history] maintenance skip: 0.20 MiB <= 10 MiB`，即每次启动都可审计。
4. **`tools/backup.py` 脚本模式 import 失败** —— 直接运行脚本时 `sys.path[0]` 是 `tools/` 而非项目根，`verify`/`vacuum`/`reindex` 一律 `ModuleNotFoundError: mcp_server`（`backup`/`list`/`restore` 不 import mcp_server，长期掩盖了它）。已补 `sys.path` 注入并加回归测试 `test_cli_script_mode_reaches_server_module`。

### 已知边界

- **MCP 不重启则不清理**：阈值只决定"下次启动清不清"。以 10 MiB 阈值估算，正常每天一次重启足够；若发现 MCP 连续运行多天，再给 `maybe_optimize()` 加写入计数器触发点（唯一入口，一行接入）。
- **清理期间的新写入会留下碎片**，终态不追求"最少文件数"；两次清理之间 FTS 索引有滞后，只影响性能不影响正确性。
- 离线 `tools/backup.py vacuum` 保留原样，作为深度清理与应急手段。

### 测试基线

**127 passed / 0 failed / 1 skipped**（此前 117 + 本轮新增 10）。

