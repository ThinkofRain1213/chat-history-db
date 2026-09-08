# 备份 · 迁移 · 恢复（chat-history-db）

本自托管聊天历史数据库的核心数据是 **LanceDB 目录 `chat.db/`**（内含 `messages.lance`，所有会话消息与 1024 维向量）和 **`title_cache.json`**（会话标题缓存）。本文件说明如何安全备份、恢复，以及如何应对 schema 变化（迁移）。

> 约定：备份统一放到 `~/.agent/backups/`，命名 `chat-history-db-<时间戳>`（例如 `chat-history-db-20260907-153237`）。

## 0. 备份时机的重要提醒

LanceDB 以目录形式存放数据。**MCP 进程运行时持有该数据库**，若在写入过程中直接拷贝目录，可能拷到不一致的中间状态。请遵循：

- 备份/恢复前，**尽量让服务处于非写入态**（最好先停掉 MCP 进程）。
- 恢复前**停止服务**，恢复后**重启**。

## 1. 要备份什么

| 文件/目录 | 说明 | 是否必备 |
|---|---|---|
| `chat.db/`（`messages.lance`） | 全部会话消息 + 向量 | ✅ 必备 |
| `title_cache.json` | session_id → 标题缓存 | ✅ 建议 |
| `error_codes.json` | 对外错误码文档 | ❌ 代码库文档，随代码走 |
| `models/`、`.venv/` | 模型与运行环境 | ❌ 可重装 |

## 2. 备份

用工具（推荐）：

```bash
python tools/backup.py backup        # 结果: 已备份到 ~/.agent/backups/chat-history-db-<时间戳>
python tools/backup.py list          # 列出已有备份（时间倒序）
```

> `tools/` 在项目版与安装版都有，且内容一致。**在哪个目录执行，就操作哪个目录的 `chat.db`**——日常治理生产库请到安装版目录（`C:\Users\Think\.agent\tools\chat-history`）执行，或用 `CHAT_HISTORY_DB` 显式指定。

或手工拷贝（等价）：

```bash
# Git Bash / PowerShell 均可；把 chat.db 整个目录和 title_cache.json 拷走
cp -r chat.db ~/.agent/backups/chat-history-db-$(date +%Y%m%d-%H%M%S)/
cp title_cache.json ~/.agent/backups/chat-history-db-$(date +%Y%m%d-%H%M%S)/
```

## 3. 恢复

```bash
python tools/backup.py restore <备份目录名或绝对路径>
# 例如: python tools/backup.py restore ~/.agent/backups/chat-history-db-20260907-153237
```

`restore` 会**先自动备份当前状态**（失败只警告、不阻断），再用指定备份覆盖回项目目录。恢复完成后：

1. **重启服务**。
2. 校验：
   ```bash
   python tools/backup.py verify          # 复用启动时的 schema 检查
   # 或访问 http://127.0.0.1:17891/health 看 db/schema 字段
   ```

## 4. 空间治理（vacuum / reindex）

### 为什么需要

LanceDB 每次写入都会生成一个**全量快照**版本清单（`.manifest`）。单条消息写入会让清单记录当时全部数据碎片，清单体积随碎片数增长、总量随写入次数**二次方累积**。实测生产库 4,500 行时，`chat.db/` 约 880 MiB 中 **97% 是过期版本清单**，真实数据不足 30 MiB。

同时，FTS 索引只在建表时创建一次，之后新增的行不会自动进入索引（实测 `num_indexed_rows=128` / `num_unindexed_rows=4443`），会拖垮 `recall` 的关键词分支。

### 一键治理

```bash
python tools/backup.py vacuum --yes      # 先自动备份，再回收旧版本+合并碎片+更新索引
python tools/backup.py vacuum --json     # 机器可读统计
```

`vacuum` 等价于 PostgreSQL 的 VACUUM，**不修改任何行数据**，只做三件事：

1. 合并小数据文件（compaction）
2. 删除除最新外的全部版本（prune，**不可回滚**）
3. 把新数据补进 FTS 索引（index）

执行前会检查：

- MCP 端口 `17891` 是否在监听 → 在跑则拒绝
- 待入库队列 `chat_pending.db` 是否还有 `pending/processing` → 有则拒绝
- 自动先备份当前状态（`--no-backup` 可关，但那样要求已有备份）

| 参数 | 作用 |
|---|---|
| `--yes` | 跳过交互确认（非交互环境必须加） |
| `--force` | 跳过 MCP/队列安全检查（危险） |
| `--no-backup` | 不自动备份（要求已有备份存在） |
| `--json` | 输出 JSON 统计 |

### 预期效果（生产库实测）

| 指标 | 前 | 后 |
|---|---:|---:|
| 文件数 | 13,852 | 18 |
| 体积 | 903.96 MiB | 21.17 MiB |
| 行数 | 4,571 | 4,571（不变） |
| FTS 已索引 | 128 | 4,571 |
| FTS 未索引 | 4,443 | 0 |

### reindex（兜底）

若 `vacuum` 后仍有未索引行（正常情况会自动重建并提示），可单独执行：

```bash
python tools/backup.py reindex
```

### 启动时自动清理（默认开启）

MCP 进程启动、HTTP 端口就绪后 2 秒，后台 daemon 线程会做一次阈值检查：

1. 扫描 `chat.db/messages.lance/_versions` 的总字节数（实测 4,700+ 文件约 4 ms）；
2. **≤ 10 MiB** → 直接返回，不做任何事；
3. **> 10 MiB** → 执行 `optimize(cleanup_older_than=0, delete_unverified=False)`，删旧版本、合并碎片、顺带把新数据补进 FTS 索引；
4. 全程不写 stdout（stdio 是 MCP 协议通道），不抢写锁，失败只记日志、不影响启动。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CHAT_HISTORY_GC_MB` | `10` | 垃圾体积阈值（MiB），超过才清理 |
| `CHAT_HISTORY_GC` | `1` | 置 `0` 可整体关闭自动清理 |
| `CHAT_HISTORY_GC_LOG` | `~/.agent/hooks/chat_maintenance.log` | 清理记录（ZCode 不保留 MCP 的 stderr，必须落文件） |

日志形如（每次 MCP 启动必留一条，`skip` 表示低于阈值未清理）：

```
2026-09-08 19:09:19 [chat-history] maintenance skip: 0.20 MiB <= 10 MiB
2026-09-08 19:03:06 [chat-history] maintenance cleaned 0.45 MiB -> 0.01 MiB, rows 4740 -> 4740, 0.3s
```

**与手动 `vacuum` 的分工**：启动清理是**在线安全版**——用 `delete_unverified=False`（`True` 在并发写入下实测 8/20 次失败），且不抢写锁，因此可以在服务运行中执行；`tools/backup.py vacuum` 是**离线深度清理**，会先自动备份、要求 MCP 停机、并可用 `delete_unverified=True` 清得更彻底，适合首次治理或异常时使用。

**已知边界**：清理只在 MCP 启动时触发。若 MCP 连续运行多天不重启，垃圾会在下次启动前继续累积；届时可手动跑一次 `vacuum`，或给 `maintenance.maybe_optimize()` 再加一个写入计数器触发点（该函数是唯一入口，加触发点只需一行）。

### 定期治理建议

- 日常无需手动：启动自动清理已按 10 MiB 阈值兜底
- 手动 `vacuum` 只在三种情况用：首次治理、MCP 长期不重启、异常排查
- 只读体检（不清理）：查看文件数与 `num_unindexed_rows`，异常再动手
- `vacuum` / `reindex` / `verify` 都会**同时覆盖 `messages` 与 `messages_archive` 两张表**（归档表不存在时自动跳过）

## 5. 迁移（schema 变化）

### 案例：role → kind（item 01，已发生）

旧库 messages 表用的是 `role` 字段；新 schema 用 `kind`（`user/tool/mid/final`）。启动时的 schema 自检（`_validate_messages_schema`）会检出两者不通（返回 `E_SCHEMA` 并阻止启动）。

迁移通用流程：

1. **先备份**：`python tools/backup.py backup`。
2. **读旧结构**：确认差异字段（本例 `role` vs `kind`）。
3. **改写数据**：导出行 → 把 `role` 映射到 `kind`（如 `user→user`、`assistant→final`）→ 重建表。
4. **验证**：`python tools/backup.py verify` + 跑测试套件 `python -m unittest discover -s tests`。

> 说明：若 `_validate_messages_schema` 校验失败（`E_SCHEMA`），启动会被拦截——这是**有意为之**，避免用旧结构读写导致静默错乱。迁移需按上述流程先备份再改写，改完让启动自检通过。

### 通用原则

- 任何 schema/数据结构改动前**先备份**（遵循本文件「备份时机」约定）。
- 迁移脚本只做「读旧 → 映射 → 写新」，不做任何破坏性覆盖；完成后用 `verify` + 测试套件双重确认。

## 6. 测试

工具本身的行为由 `tests/test_backup.py` 覆盖（文件拷贝、命名、恢复、verify 复用 schema 检查）。运行方式：

```bash
python -m unittest discover -s tests
```
