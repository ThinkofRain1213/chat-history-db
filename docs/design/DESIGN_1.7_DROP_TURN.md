# 设计：去掉 `turn` 列（可行性与迁移方案报告）

- **日期**：2026-09-08
- **状态**：**已实施并部署（用户选路线 A，非本报告推荐的 B）**——实施记录见 §7
- **需求**：`turn` 不要了，序号只留 `round` / `step`
- **范围**：`db.py`（表模型/排序）、`core.py`（写入与展示）、`mcp_server.py`、测试、文档、生产库数据迁移
- **实测环境**：安装版 `.venv`，LanceDB **0.38.0**，生产库 `~/.agent/tools/chat-history/chat.db`（本报告期间 5735→5747 行，库在持续写入）

---

## 0. 结论（TL;DR）

**可行，且比预想便宜。** 推荐 **路线 B**：

1. 老数据用一条 SQL 把 `turn` 值搬进 `step`（`round` 保持 0）——实测 **0.05s / 4969 行**，逐行等价、尾行判定零不一致；
2. `drop_columns(["turn"])` ——实测 **0.01s**，行数不变、FTS 索引保留、体积不变。

代价主要在**代码与测试改动**（约 73 处引用、~25 处测试断言），不在数据。

**必须知道的三个硬事实：**

1. **`drop_columns` 不可逆**：drop 之后 `add_columns` 只能补回一列 0，原值丢失 → 唯一的回滚是**整库恢复备份**。
2. **顺序不能反**：drop 之后旧代码既读不了也写不了（读报 `No field named turn`，写报 `Field 'turn' not found`）→ 必须「停 MCP → 落新代码 → 再 drop」。
3. **`turn` 本来就已经不唯一**：`(session_id, turn)` 现存 **143 组重复 / 198 行**，所以它不是可靠主键；这也是"只留 round/step"的额外理由。

---

## 1. 现状盘点

### 1.1 表结构

`messages` 9 列：`session_id, session_title, time, turn, text, kind, vector, round, step`；`messages_archive` 同 9 列（当前 0 行）。索引只有 `text` 上的 FTS。

### 1.2 `turn` 的真实引用点（全库 grep 后逐处确认）

| 类别 | 位置 | 说明 |
|---|---|---|
| 表模型 | `db.py:33` `Msg.turn` | schema 必备列（`_validate_messages_schema` 会校验） |
| 写入 | `core.py:177,202-203,209` | `turn = tail.turn + 1`，随行写入 |
| 排序 | `db.py:239-255` `_session_max_turn` | **已无调用方**（只有测试引用）→ 死代码 |
| 排序 | `db.py:258-274` `_session_tail` | 取会话尾行的排序键（round/step 推导的唯一输入） |
| 排序 | `db.py:294-298` `_recent_rows` | 次排序键 `(time desc, turn desc)` |
| 展示 | `core.py:272-277` `_format_loc` | `round==0` 时回退显示 `#turn` |
| 输出 | `core.py:214,264,366,577` | remember/recent/recall 结构化 dict 与 HTTP 响应 |
| 接口 | `core.py:165,463` | MCP `remember(..., turn=None)` 参数 |
| 测试 | `tests/*` | 25 处断言/构造 |
| 文档 | `README.md`、`skills/chat-history/SKILL.md` | remember 签名、recent 排序说明 |

**不涉及 `turn` 的地方（重要）**：

- `archive.py` 完全不碰 `turn`（搬行/删除只按 `session_id`）；
- hooks 与 worker **不传 `turn`**：`chat_worker._http_remember` 的 body 只有 `content / session_id / kind`（实测源码），所以代码先上线不会破坏写入链路。

### 1.3 数据真相（生产库实测）

| 指标 | 值 |
|---|---|
| 总行数 | 5735～5747（写入中） |
| `round = 0` 的老数据 | **4969 行 / 27 个会话**（占 86.6%） |
| 同时含老数据与新数据的会话 | **只有 1 个**（本会话 `sess_fcd9e781…`：老 1764 + 新 771） |
| `round = 0 且 step > 0` 的行 | 17 行，全在本会话（新代码写的、尚无用户轮的 agent 消息） |
| `(session_id, turn)` 重复 | **143 组 / 198 行**（1.1 之前就存在） |
| `(session_id, round, step)` 重复 | 25 组 / 4928 行，其中 4928 行全是 `(0,0)` 老数据 |

结论：**去掉 `turn` 的核心难点不是"删列"，而是"86.6% 的行只有 turn 一个序号来源"**。删列前必须把这批序号安置到 `step`（或重算 round/step）。

---

## 2. 可行性（全部在副本上实测，生产库只读）

### 2.1 `drop_columns` 能力

```
LanceTable.drop_columns(columns: Iterable[str]) -> DropColumnsResult
```

| 实测项 | 结果 |
|---|---|
| 耗时 | **0.01s**（元数据操作，不重写数据） |
| 行数 | 5742 → 5742（不变） |
| schema | 去掉 `turn` 后 8 列，正确 |
| FTS 索引 | **保留且可检索**（`search(..., query_type="fts")` 命中 3 条） |
| 体积 | 27.0 → 27.1 MiB（基本不变） |
| 之后能否继续写 | ✅ 可以（`t.add` 成功；前提是进程已注册 `bgem3` 嵌入函数，生产 `db.py` 本来就注册） |
| 回滚 API | `add_columns({"turn": "cast(0 as bigint)"})` 可用，**但值全是 0** |

### 2.2 旧代码在新 schema 下的行为（决定迁移顺序）

| 动作 | 结果 |
|---|---|
| 读 `turn` | `ValueError: Invalid input, Schema error: No field named turn` |
| 写含 `turn` 的行 | `ValueError: Field 'turn' not found in target schema` |

→ **drop 必须在旧进程退出、新代码就位之后**。反序会立刻写入失败（hooks 会重试并最终进 ERROR）。

### 2.3 没有 `turn` 时的排序替代

`order_by(round desc, step desc)` 下推查询实测可用（纯老会话尾行取到 `step=2182`）。

### 2.4 表换名的坑（只影响路线 A）

`db.rename_table` 在 LanceDB OSS **不支持**：

```
NotImplementedError: LanceDBError: not supported: rename_table is not supported in LanceDB OSS
```

`db.clone_table` 在本次调用中亦报错（按 CWD 解析路径）。所以"建新表再换名"这条路只能退化为「drop 旧表 + create 新表」，旧表只能靠**文件级备份**保留。

---

## 3. 老数据的三条路线

### 路线 A：语义重建（贵，不推荐）

按 `(turn, time)` 重排每个会话，套用 1.1 的同一套规则重算 `round/step`（用户开轮、agent 进 step），再删 `turn`。

- 实测：重算 + 重建表 **0.23s**，FTS 重建 0.09s，drop+create 换名 0.20s。
- 优点：全库语义统一，`round=0` 这个特例彻底消失。
- 缺点：
  - 混合会话的 `round` 会从 `1..30` 变成 `1..183`（老数据占 1..153）——**历史 round 号整体位移**，过去聊天/文档里引用的 `#round` 会对不上；
  - 换名只能用 drop+create（见 2.4），回滚必须靠备份；
  - 要动全部 4969 行的 `round/step`，比 B 的改动面大得多。

### 路线 B：`step` 承载原 `turn`（**推荐**）

一条 SQL：

```python
tbl.update(values_sql={"step": "cast(turn as bigint)"}, where="round = 0")
```

- 实测：**0.05s / 4969 行**；老数据 `step` 与原 `turn` **逐行相等**（脚本比对 `True`）。
- **正确性关键证据**：迁移后按 `(round desc, step desc)` 取尾行，与迁移前按 `turn desc` 取尾行，**27 个会话 0 处不一致**。尾行是 round/step 推导的唯一输入，这一条等价就等于新写入行为不变。
- 唯一性不恶化：重复组从 25 组/4928 行（全 `(0,0)`）变为 **143 组/198 行**——正好等于 `turn` 本来就有的重复量；新数据（`round>0`）仍是 3 组/3 行（既有的 `(15,45)`/`(23,36)`/`(23,57)`）。
- 展示不变：`round==0 → #step`，显示的数字与今天的 `#turn` **完全相同**。
- 语义代价：`round=0` 从"老数据"扩为"尚无用户轮次的行"（含那 17 条新代码 agent 消息），需在文档里写清楚。

### 路线 C：不删列、只停用（零风险）

代码不再读/写 `turn`，schema 保留该列。

- 优点：零数据风险，随时可回退。
- 缺点：没达成"turn 不要了"；列仍占 ~46 KB；`_validate_messages_schema` 继续校验它。

### 对比

| | A 语义重建 | **B 搬进 step** | C 只停用 |
|---|---|---|---|
| 数据操作耗时 | ~0.5s | **~0.06s** | 0 |
| 老数据序号 | 重算 round/step | step=原 turn | 保留 turn |
| 历史 round 号 | **会位移** | 不变 | 不变 |
| `turn` 列 | 删除 | **删除** | 保留 |
| 回滚 | 备份 | **备份** | 改代码即可 |
| 推荐 | ✗ | ✅ | 备选 |

---

## 4. 迁移方案（推荐 B + drop）

### 4.1 代码改动清单（先做，可单独上线）

| 文件 | 改动 |
|---|---|
| `db.py` | `Msg` 去掉 `turn`；`_session_tail` 排序改 `(round desc, step desc)`、select 去掉 `turn`；`_recent_rows` 排序键与 select 去掉 `turn`；**删 `_session_max_turn`**（已无调用方） |
| `core.py` | `remember` 去掉 `turn` 参数/自增/返回字段；`_format_loc` 的 `round==0` 回退改 `#step`；`recent`/`recall` 结构化 dict 去 `turn`；HTTP `/remember` 响应去 `turn` |
| `mcp_server.py` | 去掉 `_session_max_turn` 导入 |
| 测试 | `test_storage` 19 处、`test_round_step` 5 处、`test_adapters` 4 处、`test_errors` 3 处（含 `test_auto_turn_reads_only_turn_column`、`test_max_turn_is_scoped_and_pushdown` 需重写为 round/step 版本） |
| 文档 | `README.md`、`skills/chat-history/SKILL.md`（`remember` 签名、`recent` 排序说明）、`OPEN_ITEMS.md` |

兼容性：hooks/worker 不传 `turn`（1.2 已证），代码先上线不破坏写入；`remember(turn=...)` 是唯一被移除的公开参数，无外部调用方。

### 4.2 数据迁移（停机窗口内，总耗时 < 1s）

0. **备份**：整个 `chat.db` 目录 + 代码目录 + `config.json`，记录 SHA-256、行数、schema、`/health` 基线；
1. **停 MCP**（成对进程）→ 确认 `127.0.0.1:17891` 无监听（用 `/health` 探测，别信 bind 报错）；
2. 生产库执行 `update(values_sql={"step": "cast(turn as bigint)"}, where="round = 0")`；
3. 对 `messages` 与 `messages_archive` 各执行 `drop_columns(["turn"])`；
4. 部署新代码 → 启动 MCP；
5. 按 4.3 验证。

### 4.3 验证清单

- schema = 8 列（无 `turn`）；行数与备份一致；FTS 可检索；`/health` 全绿（`schema/fts/nan_vectors` 正常）；
- `list_sessions` 的会话数、标题、`count`、`last_time` 与迁移前一致；
- `recent` / `recall` 输出行不再含 `turn`；老数据 `#step` 显示数字与迁移前 `#turn` 相同；
- 写一条探针 → `round/step` 推导正确、与既有行不撞号；
- 观察 24h：新写入 0 组 `(round, step)` 重复；`chat_worker.log` 无新增 FAIL/重试。

### 4.4 回滚

- 代码：从备份目录恢复（哈希比对）；
- 数据：**只能整库恢复备份**——`turn` 原值 drop 后无法重建（`add_columns` 只能补 0）；
- 因此**备份必须在 drop 之前完成并校验**，这是唯一的回滚点。

### 4.5 停机窗口与风险

- 实测数据操作 < 1s；窗口主要花在进程重启（数十秒）。
- 停机期间 hooks 照常入队到 `chat_pending.db`，worker 重试（`CLAIM_MAX_AGE=240s`）→ 不丢消息，但会看到一次重试记录。
- 最大风险是**顺序做错**：drop 后仍有旧进程在写 → 立即 `Field 'turn' not found`。务必先停进程、先落代码。

---

## 5. 建议

- **推荐路线 B**：改动小、行为等价（尾行 0 不一致）、展示不变，且真正达成"只留 round/step"。
- 若只想去掉这个概念、不介意列还在，**路线 C** 是零风险备选。
- 坦白收益：主要是**概念一致性**（少一个已失真的序号列），空间收益可忽略（~46 KB）。如果近期没有其它 schema 变更需求，这件事的优先级可以放低——它的价值在"不再误导"，不在性能。

---

## 6. 附录：本次实测脚本（均在 `.agent/temp`，只读生产库）

| 脚本 | 用途 |
|---|---|
| `probe_turn_inventory.py` | 盘点 turn/round/step 分布、重复、混合会话 |
| `probe_drop_turn.py` | 副本上 drop_columns / FTS / 体积 / 重加列 |
| `probe_drop_turn2.py` | 注册 bgem3 后的 add；旧代码读写新 schema 的报错验证 |
| `probe_turn_migration.py` | A 路线（重建+换名）与 B 路线（update+drop）实测 |
| `probe_turn_b_verify.py` | B 路线尾行等价性验证（0 不一致）、唯一性对比 |

---

## 7. 实施记录（2026-09-08 22:28，路线 A）

**用户选择路线 A**（语义重建），未采纳报告推荐的 B。实际执行与验证如下。

### 7.1 代码改动

| 文件 | 改动 |
|---|---|
| `db.py` | `Msg` 去掉 `turn`；`_session_tail` 排序改 `(round desc, step desc)`、只读 `kind/round/step`；`_recent_rows` 排序改 `(time desc, round desc, step desc)`、select 去掉 `turn`；**删除 `_session_max_turn`**（已无调用方） |
| `core.py` | `remember` 去掉 `turn` 参数/自增/返回字段；`_ref` 的 `round<=0` 回退由 `#turn` 改为 `#0.step`；`recent`/`recall` 结构化结果去 `turn`；HTTP `/remember` 响应去 `turn` |
| `mcp_server.py` | 导入列表：去掉 `_session_max_turn`、补上 `_session_tail` |
| `tools/migrate_drop_turn.py` | **新增**迁移工具：按会话 `(turn, time, 物理序)` 重排 round/step → 校验（行数/字段/顺序/唯一性）→ 重建表 + FTS；`--dry-run` 默认，`--yes` 执行 |
| 测试 | `test_storage` / `test_round_step` / `test_adapters` / `test_errors` 共 ~20 处断言改写；`_OldMsg` 保留 `turn`（它是 1.1 之前的历史 schema 夹具） |
| 文档 | 本文件、`OPEN_ITEMS.md`、`skills/chat-history/SKILL.md`（remember 签名、recent 排序、`#0.step` 说明） |

### 7.2 重排规则（与线上 1.1 完全一致）

按会话把行按 `(turn, time, 物理写入序)` 排序后逐行套用：用户消息开新轮（`round+1, step=0`），
agent 消息 `step+1`。因为 27 个会话的**首行都是 user**，重排后全库 `round>=1`——`round=0` 特例
在存量数据里彻底消失（`#0.step` 只剩新写入的"会话首条是 agent"场景）。

### 7.3 切库执行（`~/.agent/temp/cutover_drop_turn.py`，单进程跑完）

1. 备份 → `.agent/backups/chat-history-dropturn-20260908-222836/`（588 文件 + `MANIFEST.json` 哈希清单，含代码/库/标题缓存）；
2. 等队列排空（`pending=0 processing=0`）→ 停 MCP（2 个进程）→ `/health` 探测确认 17891 无服务；
3. 迁移生产库：`rows=5835, sessions=27, max_round=249`，**重建 0.43s**；归档表空表直接 `drop_columns`；
4. 部署新代码 → 哈希比对**全部一致**；
5. 调 MCP 工具拉起新进程 → 验证。

### 7.4 验证结果

| 项 | 结果 |
|---|---|
| schema | 8 列（`session_id/session_title/time/text/kind/vector/round/step`），无 `turn` |
| 行数 | 迁移前后一致（5835 → 5838 含新写入） |
| FTS | 索引保留、可检索 |
| 唯一性 | `(session_id, round, step)` **全库唯一**（顺序赋值保证，历史 3 组重复随之消解） |
| round 范围 | `min=1`、`max=249`，`round=0` 行数 **0** |
| `/health` | `{"ok": true, "schema": true, "fts": true, "nan_vectors": 0, ...}` |
| 展示 | 本会话 `#184 | user`、`#184.69 | tool`（迁移后新写入 step 从 65 续到 69，无撞号） |
| 老会话追加 | 影子验证：纯老会话尾行 `(249,5)` → 新 user `(250,0)` → final `(250,1)` |
| 测试 | 项目全量 **171 passed / 0 failed / 1 skipped** |

### 7.5 与路线 B 的差异（事后记录）

用户选 A 后，`round` 号发生**全库位移**：本会话原 `round 1..28` 变为 `1..184`（老数据占 1..153），
历史聊天/文档里引用的 `#round` 数字需要按"迁移后"理解。收益是语义彻底统一、`(round,step)` 全库唯一。

### 7.6 回滚

备份目录 `.agent/backups/chat-history-dropturn-20260908-222836/`：`code/` 是旧代码，`chat.db/` 是迁移前整库。
回滚 = 停 MCP → 用备份覆盖 `chat.db` 与代码 → 拉起。`turn` 原值只在备份里，无法由现有列重建。
