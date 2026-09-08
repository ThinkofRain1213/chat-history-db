# 1.1 设计探查：`round` / `step` 改造

- **状态**：✅ 已实施并部署（2026-09-08）
- **日期**：2026-09-08
- **对应未决项**：[`OPEN_ITEMS.md`](../../OPEN_ITEMS.md) §1.1
- **结论**：可行，**不需要改 hook**。作用域就是 `session_id`：只读该会话最后一条，全局写入顺序与时间无关。唯一性只需保证「同会话写入不重叠」（按会话文件锁），不引入任何排序约束。

> **⚠ 2026-09-08 晚更正（见 [`DESIGN_1.7_DROP_TURN.md`](DESIGN_1.7_DROP_TURN.md)）**：本文写作时保留了 `turn` 作为老数据的排序键与展示回退（§7 第 3 条）。**`turn` 已于当日移除**：老数据按 `(turn, time, 物理序)` 重排进 round/step，`_session_tail`/`_recent_rows` 改按 `(round, step)` 排序，`round=0` 展示回退由 `#turn` 改为 `#0.step`。本文中"`order by turn desc`""`turn` 保留"等表述描述的是改造当时的状态，不再是当前实现。

---

## 1. 语义定义（按用户确认）

| 概念 | 定义 |
|---|---|
| `round` | 会话内**对话轮次**，从 1 开始。**每收到一条用户消息就 +1**；用户连发多条而 agent 未回复时，round 依次递增 |
| `step` | 轮内**步骤**，从 1 开始，**只属于 agent 侧消息**（mid / tool / final）；**用户消息无 step**（`step = 0`） |
| 归属 | agent 消息一律标记到**当前最后一个用户轮次**（"以用户最后 round 标记 agent 的回复"） |
| 唯一键 | `(session_id, round, step)` —— 会话内唯一 |
| 展示 | 用户消息 `#round`；agent 消息 `#round.step` |

典型一轮：`user(round=7, step=0)` → `mid(7,1)` → `tool(7,2)` → `tool(7,3)` → `final(7,4)`。

---

## 2. 作用域：按会话隔离

**本项的作用范围就是 `session_id`**：每次写入只读**该会话**的最后一行；其他会话的行、全局写入顺序、`time` 字段都不参与推导——它们只是碰巧装在同一个表里。

| 事实 | 说明 |
|---|---|
| 表里混装所有会话 | 但每条写入的作用域只有自己的 `session_id` |
| 推导只依赖该会话最后一行 | `where session_id = ?` + `order by turn desc limit 1` |
| 全局写入顺序 / `time` 无关 | 不参与 round/step 计算；`time` 仅用于展示，时间倒序也不影响结果 |
| `turn` 存在重复值（195 行） | **与本项无关**；仅当同一会话的两次写入**重叠**时才影响"最后一行"的判定（见 §6） |

---

## 3. 推导规则（单次查询）

取该会话最后一行（`order by turn desc limit 1` 下推）的 `(turn, kind, round, step)`：

```
if kind == "user":
    round = (tail.round if tail else 0) + 1
    step  = 0
else:
    round = tail.round if tail else 0
    step  = (tail.step if tail else 0) + 1
```

要点：
- agent 消息不判断上一条是不是 `final`，一律沿用 `tail.round`（用户最后轮次）——这正是"以用户最后 round 标记"。
- 斜杠命令轮（hook 跳过 user 行）的 agent 输出会归到上一个用户轮次，符合定义。
- 会话首行若就是 agent 消息（尚无任何 user），`round = 0`（见 §8 边界）。

### 在真实数据上的验证（当前会话 1,682 行）

| 指标 | 结果 |
|---|---|
| 推导轮数 | **151** |
| 重复 `(round, step)` | **0** |
| 含多个 user 的轮 | **0** |
| 无 user 的轮 | 0 |
| `round = 0` 的行 | 0 |
| **连续两条 user（agent 未回复）** | **40 处** → 每处都正确开了新轮 |
| user 行 `step` 全为 0 | ✅ |
| 每轮 agent 步数分布 | 0:41、1:35、3:7、4:6、11:4… |

样例：`user` → `mid/1` → `tool/2` → `tool/3` → `final/4`，结构正确。

---

## 4. 关键技术验证（已实测）

| 问题 | 结果 |
|---|---|
| `add_columns` 补列 | ✅ `{"round": "cast(0 as bigint)", "step": "cast(0 as bigint)"}` → **int64**，旧行取 0 |
| 重复补列 | ❌ `ValueError: Column round already exists` → **必须幂等守卫** |
| 补列后写新行（生产用 dict） | ✅ 成功 |
| 补列后写**旧结构**行 | ❌ `RuntimeError: missing=[step, round]` → 所有写入必须带新字段 |
| `LanceModel` 默认值 | ✅ `round: int = 0` 可用，schema 为 int64 |
| `search_recall` 取列 | 全列返回 → 新列自动可见，无需改动 |
| `_recent_rows` 取列 | 显式列清单 → **必须补 `round`/`step`** |
| schema 校验 | `Msg` 加列后，未迁移的库校验失败 → 启动顺序必须「补列 → 校验」 |

---

## 5. 改动清单

**`db.py`**
- `Msg` 增加 `round: int = 0`、`step: int = 0`（置于 `turn` 之后）。
- 新增 `_session_tail(tbl, session_id) -> dict | None`：一次下推取最后一行 `(turn, kind, round, step)`。
- 新增 `_migrate_messages_schema(db) -> bool`：按 `tbl.schema` 字段集合缺谁补谁，**幂等**；无表直接返回。
- `_recent_rows` 的 select 补 `round`、`step`。

**`core.py`**
- `remember(...)` 增加可选 `round`/`step`；未传则按 §3 推导；返回 dict 增加 `round`/`step`。
- `search_recall` / `search_recent` 返回 dict 增加 `round`/`step`。
- 展示：用户 `#round`、agent `#round.step`；`round == 0`（老数据）回退 `#turn`。
- `_handle_remember`（HTTP）与 `_remember_tool`（MCP）透传可选 `round`/`step`。

**`mcp_server.py`**
- `main()` 在 `_validate_messages_schema()` 前插入 `db._migrate_messages_schema()`。

**测试**
- 新增：user 开新轮 / agent 续轮 / 连发 user 各自开轮 / 斜杠命令归上一轮 / 老行 round=0 续接 / 显式覆盖 / 跨会话隔离 / 迁移幂等 / 展示回退 / `(round, step)` 唯一。
- 更新：`tests/test_adapters.py:109`、`tests/test_storage.py:149` 的展示断言。

---

## 6. 唯一性的实现要求：同会话写入不可重叠

`(session_id, round, step)` 唯一，只需要保证一件事：**同一会话的两次写入不能同时读到同一条"最后一行"**——否则两条会推出相同的 `(round, step)`。

- **不涉及顺序**：谁先谁后都可以，先到的占 `step n`、后到的占 `n+1`；全局写入顺序、时间字段都不参与。
- **现状**：HTTP 路径全部落在 MCP 进程内，已被 `_WRITE_LOCK` 串行化 → 已满足；只有 worker **本地兜底**时是独立进程，会与 MCP 进程重叠。
- **做法**：加一个**按会话**的文件锁（`msvcrt.locking`，进程退出由 OS 释放），只为原子性，不引入任何排序约束、也不改变跨会话并行。

> 附注：embedding 目前在锁内（约 1–2s/条）。若日后吞吐成为问题，可改为「锁外预计算向量 + 锁内只做推导与写入」（`remember` docstring 已记录该方向）。

---

## 7. 实施顺序

1. `db.py`：`Msg` 两列 + `_session_tail` + `_migrate_messages_schema` + `_recent_rows` 补列
2. 跨进程文件锁（§6 A）
3. `core.py`：推导 + 返回字段 + 展示
4. `mcp_server.py`：启动接入迁移
5. 测试（含迁移幂等与唯一性）
6. 备份安装版 → 部署 → 重启 MCP → 验证迁移日志与新行 round/step

---

## 8. 待确认边界

1. **会话首行就是 agent 消息**（无任何 user）：`round = 0`（表示"尚无用户轮次"）还是记 1？建议 0，展示回退 `#turn`。
2. **斜杠命令轮**：其 agent 输出归到上一个用户轮次（符合"以用户最后 round 标记"）——确认可接受。
3. **`turn` 保留**：仅用于老数据排序与展示回退；不作为 round/step 的推导依据（推导只看该会话最后一条）。
4. **展示格式**：用户 `#round`、agent `#round.step`——确认。

---

## 9. 实施记录（2026-09-08）

| 项 | 内容 |
|---|---|
| 改动文件 | `db.py`（`Msg` +2 列、`_session_tail`、`_session_lock`、`_migrate_messages_schema`、`_recent_rows` 补列）；`core.py`（`_derive_round_step`、`remember` 推导与返回、检索字段、展示回退、HTTP/MCP 参数）；`mcp_server.py`（启动先迁移再校验） |
| 新增测试 | `tests/test_round_step.py` 12 例：用户开轮无 step、agent 续轮、用户连发各自开轮、final 后仍归上一轮、会话隔离、`(round, step)` 唯一、显式覆盖、老数据回退、迁移幂等、锁互斥等 |
| 全量测试 | **140 passed / 0 failed / 1 skipped** |
| 生产副本迁移实测 | 4,950 行：补列 0.3s、二次调用 no-op、行数不变、`_validate_messages_schema()` 通过；新写入推导 `(turn, round, step) = (1,1,0) (2,1,1) (3,1,2)` |
| 生产部署 | `db.py` `2e8208395dc6`、`core.py` `a6eb74a0107c`、`mcp_server.py` `4db5d3a82f14`，安装版哈希一致；重启后 `messages` 含 `round`/`step`，`tools/backup.py verify` 报 schema 健康 |
| 线上写入实测 | 重启后新行已带 `step` 递增（该会话历史 `round=0`，故 `round=0, step=1/2/3`） |
| 备份 | `.agent/backups/chat-history-roundstep-20260908-193131/`（34 文件 + config + SHA-256 清单） |

**已知表现（符合设计）**：历史会话 `round=0`，其后的 agent 消息继续 `round=0`，直到该会话收到新的用户消息才开 `round=1`——这是"不回填历史 + agent 归属最后一个用户轮次"的直接结果。
