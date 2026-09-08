# 1.5 设计探查与实施：写入质量三修（mid 去重 / 锁键归一 / NaN 防护）

- **状态**：✅ 已实施并部署（2026-09-08）
- **日期**：2026-09-08
- **来源**：由 `OPEN_ITEMS.md` §1.4（写入幂等）的讨论衍生——排查中发现真正影响数据质量的是另外三个问题
- **结论**：三项均已修复并线上验证；**1.4 本身仍维持「暂不做」**（证据弱，见 §5）

---

## 1. 三个问题与根因

### 1.1 mid 并行重复（系统性，影响最大）

**现象**：一个模型响应里发 N 个并行工具调用 → 库里出现 N 条**完全相同**的 mid（实例：当前会话 `round=15` 的 step 1/3 与 47/49/51）。

**根因**：mid 的真实粒度是**一次模型响应**（一行 `model_io` 的 `response.text`），而钩子的粒度是**一次工具调用**。同一响应里的 N 个工具调用各自触发 PostToolUse，每次都从同一行取到同一段 text → 写 N 次。

**量化**：库里"内容重复"共 386 组 / 1119 行，但绝大多数是**真实重复内容**（同一文件被 Edit 64 次、用户真的打了 29 次"继续"）+ mid 并行重复；真正"同 `(session,kind,text,time,turn)` 的重写"只有 **19 组 / 23 行，且全部是 1.1 改造前的 `round=0` 老数据**。

### 1.2 `(round, step)` 碰撞（1 例）

**现象**：`round=15` 的 step 45 有两行（mid + tool），**turn 都是 1958**、时间差 1 秒；1.1 之后全库仅此 1 组。

**根因**：`db._lock_path` 用 `_db_file()` 的**原始字符串**做摘要。同一个库的不同拼写（大小写 / 正反斜杠 / 末尾分隔符）会算出**不同的锁文件**，跨进程互斥因此失效 → 两个写入进程并发"读会话尾行 → 推导 turn/step → 写" → 算出相同的 turn/step。

> **2026-09-08 晚更正（见 [`DESIGN_1.6_LOCK_DIR.md`](DESIGN_1.6_LOCK_DIR.md) §2.6）**：上面这条**不是撞号的真实机制**。锁键归一化本身是有效加固（下表实验成立），但复验时本轮又复现了 `(23,36)`/`(23,57)` 两组重复，探针定位到真因——**`core.remember` 在拿锁之前就打开了表对象，而 LanceDB 表对象绑定打开那一刻的快照**，于是"读到旧尾行"照样撞号（文件锁只保证互斥，保证不了读到最新版本）。已改为锁内重开表。本节的实验数据仍然有效，只是它证明的是"锁键会失效"，不是"这次撞号由它造成"。

**实测**（两进程各持锁 60 次、每次 2ms）：

| 场景 | 同一把锁 | 跨进程持锁区间重叠 |
|---|---|---|
| 两个进程都用 `C:\...` | 是 | **0** |
| 一个用 `C:\...`、一个用 `C:/...` | **否** | **93 对** |

### 1.3 NaN 向量写入失败

**现象**：worker 日志 `Arrow error: ... Vector column 'vector' has NaNs`，LanceDB 拒收整行 → 该条进重试，连续失败会标 ERROR（消息可能被丢）。

**根因**：`bgem3_embedding._embed` 归一化时 `se / np.where(n > 0, n, 1.0)` 对 NaN 不生效（NaN 的比较恒为假 → 除以 1 → NaN 原样保留）。

---

## 2. 方案与实现

### 2.1 mid 去重：响应级原子认领（不碰 schema）

- `_scan_rollout` 顺带返回命中行的 `response.responseId`（实测 134/134 唯一非空，UUID 形式）。
- `_mid_from_rollout` 返回 `(text, reason, key)`。
- 新增 `_claim_mid(session_id, key)`：`os.open(..., O_CREAT|O_EXCL)` 在 `~/.agent/hooks/mid_claims/` 下建认领文件。**成功才入队**；`FileExistsError` 表示同响应的兄弟调用已写过 → 跳过。
- 退化策略：拿不到 key（老格式行）或认领机制自身异常 → 返回 True（**宁可重复，也不丢 mid**）。
- 清理：认领文件数超过 800 时删除 24 小时前的，避免无限堆积。

### 2.2 锁键归一（`db.py`）

```python
norm = os.path.normcase(os.path.abspath(_db_file()))   # 修前：直接用 _db_file()
digest = hashlib.sha256(f"{norm}|{session_id}".encode("utf-8")).hexdigest()[:32]
```

只归一**锁键**，不改 `lancedb.connect` 的路径语义。注意：部署瞬间新旧锁键并存，存在一次性过渡窗口（同一会话的两把锁都可能被用），窗口极短。

### 2.3 NaN 防护（`bgem3_embedding.py` + `core.py`）

- `_embed` 归一化后检测 `np.isfinite`，命中则 `np.nan_to_num(nan=0, posinf=0, neginf=0)` 清洗并累加 `_NAN_VECTORS`。
- `/health` 新增 **`nan_vectors`** 字段：MCP 的 stderr 不落盘（见 [[zcode-mcp-stderr-not-retained]]），用可查询的计数代替告警。
- 取舍：清洗后该条的语义检索退化，但**消息不丢**（FTS 关键词检索仍可用）。

---

## 3. 验证

| 项 | 结果 |
|---|---|
| 锁实验（修后） | 4 种拼写 → **同一把锁**；异拼写并发 **0 重叠** |
| 钩子验证脚本 `.agent/temp/verify_hooks_13.py` | **13/13**：3 元组契约 5 例、全文件回退、认领 4 例、端到端"一响应两调用 → 队列 `mid, tool, tool`"、长命令 1011 字不截断、确认走测试队列 |
| 项目全量测试 | **164 passed / 0 failed / 1 skipped**（新增 3：锁键归一 + 嵌入 NaN 清洗 2 例） |
| 生产 `/health` | 新字段 `nan_vectors: 0`，其余全绿 |
| 生产 mid 去重 | 当前会话 `round` 17–19 **无同轮重复 mid**；`mid_claims` 目录已产生认领文件 |
| 生产锁 | MCP 重启后端口 17891 正常监听 |

---

## 4. 实施记录

| 项 | 内容 |
|---|---|
| 改动文件 | `.agent/hooks/chat_hook.py`（`_scan_rollout`/`_mid_from_rollout` 三返回值；新增 `_claim_mid`/`_prune_claims`；main 按认领结果入队）<br>`db.py`（`_lock_path` 归一化）<br>`bgem3_embedding.py`（`_NAN_VECTORS`/`nan_vectors()` + `_embed` 清洗）<br>`core.py`（`/health` 增加 `nan_vectors`）<br>`tests/test_round_step.py`（+1）、`tests/test_embedding.py`（新增，2 例） |
| 部署 | `db.py` / `bgem3_embedding.py` / `core.py` 同步安装版（哈希一致）；`chat_hook.py` 单副本无需部署（钩子每次事件重读脚本） |
| 备份 | `.agent/backups/chat-history-mid-dedup-20260908-212407/`（project/installed/hooks 三份 + SHA-256） |
| 已知限制 | ① 子代理内部工具调用不触发钩子（1.3 已记）② `%TEMP%/chat-history-locks` 会随"不同库路径"累积（当前 930 个 0 字节文件），暂未清理 |
| 未做 | **1.4 写入幂等（重试行重）维持暂不做**：证据弱——孤儿恢复仅触发 2 次（1+3 行）、1.1 之后 0 组完全重复；代价是要么加列改 schema（方案 A），要么冒误杀真实重复的风险（方案 B） |

---

## 5. 待观察

- `nan_vectors` 是否长期为 0（>0 说明嵌入仍有异常输入，需查是哪条文本）。
- 锁目录文件数是否继续膨胀（若明显，可加"按 mtime 清理"）。
- 若将来 1.4 复现（出现 1.1 之后的同 `(session,kind,text,time,turn)` 重复），再评估方案 A。
