# 设计：锁目录收口 + 快照新鲜度 + 多实例端口 + 首建竞态

- **日期**：2026-09-08
- **范围**：`db.py`（锁与连接）、`core.py`（写入读尾行的快照新鲜度、建表竞态）、`http_server.py`（多实例端口归属）、`chat_hook.py`（入队失败可见性）、测试与运维清理
- **前置**：[`DESIGN_1.5_WRITE_QUALITY.md`](DESIGN_1.5_WRITE_QUALITY.md) 修了「锁键里的库路径拼写」；本轮是同一类问题的复查续修，并在复验时找到 `(round, step)` 撞号的**真正元凶**（§2.6）。
- **未做**：1.4 写入幂等（用户明确暂不做）、清理历史重复行、ZCode 侧单实例化。

---

## 1. 问题：锁键归一了，锁**目录**还依赖环境变量

1.5 把锁文件名改成 `sha256(normcase(abspath(db))|session)`，但锁文件放哪由

```python
_LOCK_DIR = Path(tempfile.gettempdir()) / "chat-history-locks"
```

决定，而 `tempfile.gettempdir()` 按 `TMPDIR → TEMP → TMP` 取第一个可用值（CPython 3.14 `_candidate_tempdir_list` 顺序，见本机 `tempfile.py:156`）。

实测（本机）：

```
TMPDIR = %LOCALAPPDATA%\Temp   ← 第一个候选，赢
TEMP   = ~/.agent/temp          ← 被忽略
gettempdir() -> %LOCALAPPDATA%\Temp
```

含义：两个写入进程只要 `TMPDIR`（或它未设时的 `TEMP`/`TMP`）不同，**即使库路径拼写完全一致**也会落到不同锁目录 → 跨进程互斥静默失效。与 1.5 修的是同一个病根：拿「进程环境里的字符串」当跨进程键，而不是拿「目标文件本身」当键。

当前未暴露：MCP 与 hooks 都是 ZCode 子进程，继承同一个 `TMPDIR`。但只要从别的 shell / 别的 harness 跑一次写入脚本就会分叉。

## 2. 修法

### 2.1 锁目录紧挨库文件（项 1）

```python
def _db_canonical(path=None) -> str:      # realpath + abspath
    return os.path.realpath(os.path.abspath(path or _db_file()))

def _db_key(path=None) -> str:            # 比较键：canonical + normcase
    return os.path.normcase(_db_canonical(path))

def _lock_dir() -> Path:
    return Path(_db_canonical() + ".locks")
```

- 目录由库路径决定 → **能写这个库的进程，必然能写到同一个锁目录**，不再看 `TMPDIR/TEMP/TMP`。
- 生产库对应目录：`~/.agent/tools/chat-history/chat.db.locks`。
- 锁文件名仍用 `sha256(session_id)[:32].lock`（库身份已由目录承载）。

### 2.2 `realpath` 解析别名（项 2）

`normcase(abspath(...))` 只覆盖大小写 / 正反斜杠 / 末尾分隔符 / `.` 与 `..`。`os.path.realpath` 在 Windows 上走 `GetFinalPathName`，额外解析 junction / 符号链接 / 8.3 短名，因此同一库的这些写法都归一到同一把锁。实测 junction 生效（见 §4）。

**仍不覆盖**（诚实边界）：映射盘 vs UNC（`Z:\chat.db` 与 `\\server\share\chat.db` 是两个根，realpath 不跨盘统一）、`subst` 盘。当前没有任何来源这样写。

### 2.3 连接比较也归一（项 3）

`_ensure_db()` 原用 `_store.db_path != p` 原始字符串比较：同一进程内 env 换拼写会对同一个库重复建连接。改为比较 `_db_key()`，`Store` 增加 `db_key` 字段（`db_path` 仍存原始值，供日志/调试）。

### 2.4 入队失败不再静默（项 5）

`chat_hook._enqueue` 原来整个 `try/except Exception: pass`——sqlite 忙 / 磁盘满 / 队列损坏时消息直接丢且无迹可查。改为两段：

- 插入失败 → `_log("[FAIL] 入队失败 …")` 并 **return**（不再白拉 worker）；
- 行已入队但 `Popen` 失败 → `_log("[FAIL] worker 启动失败（行已入队，待下次事件拉起）…")`，行不丢。

### 2.5 锁文件自清理（项 7）

锁文件每个会话一个、零字节，之前无任何清理逻辑（TEMP 下已积 1002 个）。除一次性回收旧目录外，新增 `_prune_locks()`：文件数 > 500 时删 7 天未动的。**仍被持有的锁文件删不掉**（Windows 拒绝删除已打开的文件），所以清理不会破坏互斥。

### 2.6 真正的撞号元凶：锁外开表读到旧快照（追加发现）

部署后复验生产数据时发现**本轮 round=23 又出现两组重复** `(23,36)`（两条 mid）、`(23,57)`（两条 tool）——内容不同、turn 相同，说明两次写入都读到同一个尾行。锁目录已经统一、锁实验 0 重叠，于是做探针（`~/.agent/temp/probe_lance_stale.py`）：

```
父进程 open_table 拿到表对象 A（count=1）
子进程提交一行 → count=2
父进程用同一个表对象 A 读  -> 1     ← 停在旧快照
父进程重新 db.open_table() 读 -> 2     ← 新鲜
```

结论：**LanceDB 表对象绑定打开那一刻的快照**。而 `core.remember` 的顺序是

```python
with _WRITE_LOCK:
    tbl = _open_or_none(db)        # ← 锁外打开，快照停在此刻
    ...
    with _session_lock(session_id):
        tail = _session_tail(tbl, session_id)   # ← 用旧快照读尾行
        tbl.add([row])
```

于是「A 开表 → 另一进程提交 → A 才拿到锁」时，A 读到的尾行不含别人的新行 → 算出相同的 turn/round/step。**文件锁只保证互斥，保证不了读到最新版本**——这才是 `(round, step)` 撞号的真正机制；1.5 修的是锁键/锁目录（真实隐患，实验可复现），但并不是这条症状的成因。

修法（`core.py`）：锁内重新打开表，用新鲜快照读尾行。

```python
with _session_lock(session_id):
    tbl = _open_or_none(db) or tbl
    tail = _session_tail(tbl, session_id)
```

`archive.py` 本来就是「锁内开表」，顺序正确，无需改。

回归测试 `test_remember_refreshes_table_inside_lock`：注入一个「锁外拿到的旧表对象」，再让另一个连接提交一行；修复后 turn 递增正确（3），并**反证**始终用旧快照时 turn 会与别人撞号（得到 2）。

### 2.7 多实例端口：先探测再绑 + 落盘日志 + 后台接管（项 6）

**先纠正一个错误认知**：此前以为「第二个 MCP 绑不上 17891，只在 stderr 写一行」。探针（`probe_http_and_table.py`）实测：

```
holder 已占用 127.0.0.1:17999
  [默认(allow_reuse_address=True)] **绑定成功**   ← Windows 的 SO_REUSEADDR 允许重复绑定
  [allow_reuse_address=False] 绑定失败: WinError 10048
```

即第二个进程**不报错、静默绑定成功**，两个实例同时监听、谁收连接由系统决定（实测 `Get-NetTCPConnection` 只显示最后一个 binder）。所以问题不是"报错没落盘"，而是"根本没报错"。

修法（`http_server.py`）：

- **先探测再绑**：启动时先 GET `http://127.0.0.1:17891/health`；有实例在服务就**不绑端口**（hooks 继续走它），避免双监听。
- **落盘日志**：新增 `~/.agent/hooks/chat_http.log`（`CHAT_HISTORY_HTTP_LOG` 可覆盖），记录「已监听 / 已有实例 / 绑定失败 / 已接管」。ZCode 不保留 MCP stderr，日志必须自己落文件。
- **后台重试接管**：被占时每 30s 探测一次，前一个实例退出后自动接管——否则本进程会**永久**失去 HTTP 通道。

单测 3 例（`tests/test_http_server.py`，全 mock，不碰真实端口）+ 真端口脚本 `verify_http_guard.py`（真实例占用下：探测 True → 不绑 → 返回 None → 日志正确）。

### 2.8 并发首建表竞态（顺手修）

两个进程**同时首次**写库时都会看到「表不存在」，一个建表成功、另一个 `create_table` 抛「表已存在」→ 那次写入失败（靠队列重试自愈，但白失败一次）。

修法：新增 `core._ensure_messages_table(db_handle)`——建表失败时改用 `_open_or_none()` 拿到别人建好的表；建完（或接管）后**幂等补 FTS 索引**。同时把建表与追加两条路径合并成一条：空表创建（`create_table(TABLE, schema=Msg)`，实测可行）后统一走「锁内读尾行 → 推导 → add」。

验证：单测 `test_ensure_messages_table_tolerates_concurrent_create`；端到端 `verify_concurrent_writes_16.py` 改为**冷启动**两进程并发建表 + 各写 8 条 → 16 行、turn 1..16 唯一、0 组重复。

---

## 3. 验证

| 检查 | 结果 |
|---|---|
| 锁目录紧挨库文件 / 不在 tempdir | PASS |
| 改 `TMPDIR/TEMP/TMP` 后锁路径不变 | PASS |
| junction 拼写归一 | PASS（`lock_probe_link\chat.db` → `lock_probe\chat.db.locks`） |
| 8.3 短名归一 | SKIP（本卷未生成短名，`GetShortPathNameW` 返回长名） |
| **跨进程互斥：两进程异 TMPDIR + 异拼写，60×60 次持锁** | **0 对重叠** |
| 入队失败落日志 / worker 启动失败落日志 / 行仍在队列 | PASS |
| LanceDB 快照探针（表对象陈旧 / 重新 open_table 新鲜） | 确认：1 vs 2 |
| 锁内重开表的回归测试（含反证） | PASS |
| **端到端并发**：两进程各写 8 条（完整 `core.remember`，假嵌入） | 16 行、turn 1..16 唯一、**0 组 `(round, step)` 重复**（冷启动同时建表） |
| HTTP 端口守卫（真实例占用） | 探测 True → 不绑 → 返回 None → 日志「已有实例」 |
| 项目全量测试 | **172 passed / 0 failed / 1 skipped**（较 1.5 基线 +8） |
| 部署 | 项目版与安装版 `db.py`（`6771110c…`）、`core.py`（`c516cbcb…`）、`http_server.py`（`fae51ad2…`）哈希一致；`chat_hook.py` 单副本即改即生效 |
| 生产 | `/health` 正常、`chat_http.log` 已记录启动、`chat.db.locks` 已创建、TEMP 旧目录已回收（1002 文件）、队列 0 积压、`chat_hook.log` FAIL 计数 0 |
| 生产复验 | 修复后累计新增 **110 行、0 组重复 `(round, step)`**（两轮部署分别 67 / 43 行） |

验证脚本：`~/.agent/temp/verify_lock_dir_16.py`（10 项，零副作用：子进程只碰锁文件、`CHAT_HISTORY_DB` 指向 `.agent/temp/lock_probe`）。

## 4. 部署与回滚

- **顺序**：停 MCP（2 个成对进程）→ 确认 17891 释放 → 拷贝 `db.py` / `core.py` → 校验哈希 → 调任意 MCP 工具拉起。这样不存在「旧代码用 TEMP 锁、新代码用库旁锁」的混合窗口。
- **回滚点**：`.agent/backups/chat-history-lockdir-20260908-214938/`（锁目录改动前的 `db.py` / `chat_hook.py`）；`.agent/backups/chat-history-stale-table-20260908-215439/`（快照修复前的 `core.py` / `db.py`）；`.agent/backups/chat-history-multiinstance-20260908-220042/`（多实例与竞态修复前的 `core.py` / `http_server.py`）。均含 `SHA256SUMS.txt`。

## 5. 遗留

- 1.4 写入幂等（用户明确暂不做）：worker HTTP 30s 超时后的重试、`CLAIM_MAX_AGE=240s` 孤儿恢复都可能造成重复行。
- 生产库现存 3 组 `(round, step)` 重复：`(0,0)` 是迁移前老数据（展示回退 `#turn`，属预期）、`(15,45)` 与 `(23,36)`/`(23,57)` 是本次修复前的产物，**未清理**（涉及删行，需用户决定）。
- 多实例下第二个 MCP 不再抢端口（探测 + 重试接管），但它仍会各自加载一份模型（内存翻倍）；彻底解决要 ZCode 侧单实例化，不在本项目范围内。
