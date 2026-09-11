# 未决项报告（chat-history-db）

- **更新时间**：2026-09-09 00:25（北京时间）
- **基线状态**：生产库 `chat.db` 约 **27 MiB**（清理前 963 MiB），`messages` **8 列**（已去 `turn`，只留 round/step）；安装版与项目版**运行时代码完全对齐**；MCP 启动自动清理已上线（阈值 10 MiB）；工具面 5 个（`remember`/`recall`/`recent`/`list_sessions`/`session_admin`）；hooks 修复（1.3）、写入质量三修（1.5）、锁目录收口 + 快照新鲜度 + 多实例端口 + 首建竞态（1.6）、去 `turn`（1.7）、架构审查 A 组小修复（§6）、B 组重构（§7）、C3 limit 统一（§8）、队列可观测改道（§9）、测试精简（§10）已上线；全量测试 **182 项（181 通过 / 0 失败 / 1 跳过）**；GitHub 仓库已建立、`pyproject.toml` 就位（公开，见 §11；CI 挂起）
- **本文档是未决项的唯一权威清单**；`REPAIR_SPEC_20260908.md` §6 已改为指向本文

---

## 0. 已完成（作为基线，不再追踪）

| 项 | 结果 |
|---|---|
| 项目版代码 → 安装版迁移 | 完成；模块哈希逐一一致 |
| 库体积治理 | `tools/backup.py vacuum/reindex` 上线；MCP 启动自动清理上线；963 MiB → 23 MiB |
| 死代码 / 垃圾清理 | `title_fetcher.py`、0 字节 `=`、陈旧 `.pyc` 已送回收站 |
| `tools/` 部署到安装版、`README.md` 补回项目版 | 完成，两边哈希一致 |
| `tools/backup.py` 脚本模式 import 失败 | 已修复 + 回归测试 |
| 生产库 vacuum 首次执行 | 已由启动自动清理完成（无需停机） |
| `round`/`step` 改造（§1.1） | 已实施并部署；见 [`DESIGN_1.1_ROUND_STEP.md`](docs/design/DESIGN_1.1_ROUND_STEP.md) |
| 归档 / 删除会话（§1.2） | 已实施并部署；见 [`DESIGN_1.2_ARCHIVE.md`](docs/design/DESIGN_1.2_ARCHIVE.md) |
| 架构审查 A 组小修复（§6） | 8 项已实施并部署；测试 175 项 |
| 架构审查 B 组重构（§7） | B1/B2 + B3 计数路径已实施并部署；测试 180 项 |
| 架构审查 C3 limit 统一（§8） | 已实施并部署；测试 181 项 |
| 队列可观测改道（§9） | 删 `queue_alert` 残留 + `/health` 加队列字段；测试 183 项 |
| 测试精简（§10） | 删 1 个被覆盖用例 + 1 行冗余断言；HTTP 测试按类共用 server；182 项 / 21s |
| GitHub 仓库 + pyproject（§11） | 公开仓库 `ThinkofRain1213/chat-history-db` 建立（50 文件入库，模型/venv/运行时库/标题缓存排除）；`pyproject.toml` 元数据就位（不做 pip 打包）；CI 挂起 |

---

## 1. 功能待办（已决策，未实施）

### 1.1 `round` / `step` 改造 —— ✅ 已完成（2026-09-08）

已实施并部署，详见 [`DESIGN_1.1_ROUND_STEP.md`](docs/design/DESIGN_1.1_ROUND_STEP.md)。要点：用户消息开新轮且无 step（`step=0`），agent 消息归属"最后一个用户轮次"并占 `step≥1`，唯一键 `(session_id, round, step)`；老数据 `round=0` 展示回退 `#turn`；跨进程同会话写入用按会话文件锁串行。测试 140 passed（新增 12），生产库迁移实测 0.3s、行数不变。

### 1.2 归档 / 删除会话 —— ✅ 已完成（2026-09-08）

已实施并部署，详见 [`DESIGN_1.2_ARCHIVE.md`](docs/design/DESIGN_1.2_ARCHIVE.md)。工具面收敛为**单个 `session_admin(action=…)`**（`archive`/`restore`/`delete`；`list` 已于同日移除，列出统一走 `list_sessions(source=…)`）；**删除只针对已归档会话**（删活跃会话报 `E_NOTARCHIVED`，需先归档）；删除是两阶段闸门，第一次调用返回含询问工具参数的结构化文案，TTL 内二次调用才执行（`CHAT_HISTORY_DELETE_TTL` 默认 60 秒）；`recall`/`recent`/`list_sessions` 增加 `source` 参数。测试 161 passed（新增 21），生产库归档→恢复实测通过（净零变更）。

### 1.3 hooks 修复 —— ✅ 已完成（2026-09-08）

详见 [`DESIGN_1.3_HOOKS.md`](docs/design/DESIGN_1.3_HOOKS.md)。探查后根因**重新定性**：**(a) 原描述的根因不成立**——主会话 mid 查找在钩子触发时是有效的（实测 4/4 命中，含 `Agent` 调用），433 条 `mid 未取到` 里约 95% 是噪音（那些 model_io 行本来就没有前言文本，无 mid 可存）；真实缺口是①日志不分原因②4 MB 尾部窗口会漏③**子代理内部工具调用完全不触发钩子**（整条 tool/mid 记录都没有，客户端行为，钩子侧无法补）。

已实施：`_mid_from_rollout` 返回 `(text, reason)` + 尾部窗口未命中→全文件回退 + 日志按原因分级（`no_text` 不再记日志）；**(b)** `chat_worker` 默认路径改安装版、`_local_remember` 找不到 `mcp_server.py` 直接报错；**(c)** 彻底去掉 `[:300]` 截断。验证：一次性脚本 8/8 通过（测试队列 + patch Popen，零副作用）+ 项目全量 161 passed；生产实测工具行 784 字完整入库、日志噪音归零。

### 1.4 写入幂等 —— 用户明确「暂不做」

证据弱：孤儿恢复仅触发 2 次（1+3 行）、1.1 之后 0 组完全重复；代价是要么加列改 schema（方案 A），要么冒误杀真实重复的风险（方案 B）。若将来复现再评估。

### 1.5 写入质量三修（mid 去重 / 锁键归一 / NaN 防护）—— ✅ 已完成（2026-09-08）

详见 [`DESIGN_1.5_WRITE_QUALITY.md`](docs/design/DESIGN_1.5_WRITE_QUALITY.md)。由 1.4 的讨论衍生，排查中发现真正影响数据质量的是另外三个问题：

1. **mid 并行重复**（系统性）：一个模型响应发 N 个工具调用 → N 条相同 mid。已用**响应级原子认领**（`responseId` + `O_CREAT|O_EXCL`）修复，同响应只写一条；不碰 schema、不碰 MCP。
2. **`(round, step)` 碰撞**（1 例）：`_lock_path` 用库路径**原始字符串**做摘要，同一库不同拼写 → 4 把不同锁 → 跨进程互斥失效（实测异拼写并发 93 对重叠）。已改为 `normcase(abspath(...))`。**⚠ 2026-09-08 晚更正：这不是撞号的真实机制**，真因见 §1.6 第 6 条（锁外开表读到旧快照）；锁键/锁目录归一化本身仍是有效加固。
3. **NaN 向量写入失败**：`_embed` 归一化后未处理非有限值 → LanceDB 拒收整行。已清洗成 0 并计数，`/health` 新增 `nan_vectors` 字段暴露。

验证：锁实验修后 0 重叠、钩子脚本 13/13、项目测试 164 passed、生产 `/health` 带新字段且 `round` 17–19 无同轮重复 mid。

### 1.6 锁目录收口 / 入队失败可见 / 锁文件自清理 / 快照新鲜度 / 多实例端口 / 首建竞态 —— ✅ 已完成（2026-09-08）

详见 [`DESIGN_1.6_LOCK_DIR.md`](docs/design/DESIGN_1.6_LOCK_DIR.md)。由 1.5 的同类问题复查衍生，做 1/2/3/5/7 五项，复验时追加定位到 `(round, step)` 撞号的真因（第 6 条），用户追加要求后又做了多实例端口（第 7 条）与首建竞态（第 8 条）：

1. **锁目录不再依赖环境变量**：原 `_LOCK_DIR = tempfile.gettempdir()/"chat-history-locks"`，而 `gettempdir()` 按 `TMPDIR→TEMP→TMP` 取值——两个写入进程只要这些变量不同，库路径拼写再一致也各锁各的。改为 `Path(realpath(db) + ".locks")`（库旁），能写库的进程必然能写同一锁目录。
2. **`realpath` 解析别名**：junction / 符号链接 / 8.3 短名归一到同一把锁（实测 junction 通过；本卷不生成短名，脚本 SKIP）。映射盘 vs UNC 仍不统一，属已知边界。
3. **连接比较归一**：`_ensure_db` 改比 `_db_key()`，同进程内换拼写不再对同一库重复建连接。
4. **入队失败落日志**：`_enqueue` 由 `except: pass` 改为「插入失败 → 记 `[FAIL] 入队失败` 并 return」+「`Popen` 失败 → 记 `[FAIL] worker 启动失败（行已入队）`」，消息不再无声丢失。
5. **锁文件自清理 + 旧残留回收**：`_prune_locks()` 在文件数 > 500 时删 7 天未动的（仍被持有的删不掉，不影响互斥）；TEMP 下旧目录 1002 个零字节文件已送回收站。
6. **（复验时追加发现）`(round, step)` 撞号的真正元凶**：`core.remember` 在**拿锁之前**就 `_open_or_none(db)`，而 LanceDB 表对象绑定打开那一刻的快照——「A 开表 → 别人提交 → A 才拿到锁」时 A 用旧快照读尾行，照样算出相同的 turn/round/step。探针实证：同一表对象读到 1、重新 `open_table` 读到 2。已改为**锁内重开表**再读尾行；`archive.py` 本就是锁内开表，无需改。**文件锁只保证互斥，保证不了读到最新版本。**
7. **多实例端口归属（项 6）**：探针实测**纠正了原认知**——Windows 的 `SO_REUSEADDR` 让第二个进程「绑定成功」而不报错（`allow_reuse_address=False` 才报 WinError 10048），所以问题不是"错误没落盘"而是"根本没报错"。改为**先探测 `/health` 再绑**：有实例在服务就不绑（hooks 继续走它）；新增 `~/.agent/hooks/chat_http.log` 记录「已监听/已有实例/绑定失败/已接管」；被占时每 30s 重试，前一个实例退出后**自动接管**（否则本进程永久失去 HTTP 通道）。
8. **并发首建表竞态（顺手修）**：两个进程同时首写会一个建表成功、另一个 `create_table` 抛「表已存在」而白失败一次（靠队列重试自愈）。新增 `core._ensure_messages_table()`：建表失败就改用别人建好的表，并幂等补 FTS 索引；建表与追加合并为一条「空表创建 → 锁内读尾行 → add」路径。

验证：脚本 10/10（含「异 TMPDIR + 异拼写」跨进程 60×60 次持锁 **0 重叠**）、快照探针确认、HTTP 守卫真端口验证、回归测试含反证、项目测试 **172 passed**、部署哈希一致、`/health` 正常、`chat_http.log` 已记录、`chat_hook.log` FAIL 计数 0、修复后累计新增 **110 行 0 组重复**（冷启动并发建表 e2e 16 行 0 重复）。生产库现存 3 组重复 `(0,0)`（老数据，预期）/`(15,45)`/`(23,36)`/`(23,57)`（修复前产物）**未清理**。

### 1.7 去掉 `turn` 列（只留 round/step）—— ✅ 已完成（2026-09-08）

用户选**路线 A（语义重建）**并实施，详见 [`DESIGN_1.7_DROP_TURN.md`](docs/design/DESIGN_1.7_DROP_TURN.md) §7。按 `(turn, time, 物理序)` 把老数据重排进 round/step（规则与 1.1 一致），再重建表去掉 `turn`。

结果：生产库 **5835 行 / 0.43s** 重建完成，schema 8 列无 `turn`，FTS 保留，`(session_id, round, step)` **全库唯一**（历史 3 组重复随之消解），`round` 范围 1..249、`round=0` 行数为 **0**；`/health` 全绿；项目测试 **171 passed**。代价是 round 号全库位移（本会话 1..28 → 1..184）。备份/回滚点：`.agent/backups/chat-history-dropturn-20260908-222836/`（588 文件 + 哈希清单）。

代码侧：`db.Msg` 去掉 `turn`、`_session_tail`/`_recent_rows` 改按 round/step 排序、删 `_session_max_turn`；`core.remember` 去 `turn` 参数与返回字段、`_ref` 的 `round<=0` 回退改 `#0.step`；新增迁移工具 `tools/migrate_drop_turn.py`（`--dry-run` 默认）。

---

## 2. 可选优化（未决策）

### 2.1 写入微批（治本，大改）

把 `chat_worker` 的逐条 `add` 改成一次写 N 条，版本清单的 O(N²) 累积降到约 1/N²。需改写入路径与接口，属于结构性改动。

### 2.2 写入计数器次级触发（小）

目前只在 MCP **启动时**清理；若 MCP 连续运行多天不重启，垃圾会在下次启动前持续累积。`maintenance.maybe_optimize()` 是唯一入口，加触发点只需一行。

### 2.3 FTS 索引增量更新（小）

两次清理之间 FTS 索引滞后（`optimize` 会补，但只在清理时）。只影响检索性能，不影响正确性。

---

## 3. 待观察

| 编号 | 观察项 | 判据 |
|---|---|---|
| 3.1 | 启动自动清理的首次 `cleaned` 记录 | 阈值 10 MiB，约每 500 次写入触发；日志见 `~/.agent/hooks/chat_maintenance.log` |
| 3.2 | 库体积长期稳定性 | 是否稳定在几十 MiB；若持续增长说明清理频率不足 |
| 3.3 | `/health` 的 `nan_vectors` 是否长期为 0 | >0 说明嵌入仍有异常输入，需查是哪条文本 |
| 3.4 | `chat.db.locks` 文件数 | 每会话一个零字节文件；>500 时自动删 7 天前的（`_prune_locks`），若仍持续增长说明上限偏松 |
| 3.5 | 新写入是否长期 0 组 `(round, step)` 重复 | 修复后累计 110 行 0 重复；若再出现说明还有未覆盖的写入路径 |
| 3.6 | `chat_http.log` 是否出现「已接管」 | 出现说明确有双实例切换；若长期只有「已监听」则多实例场景未复现 |

---

## 4. 建议推进顺序

1. ~~hooks 修复（1.3）~~ ✅ 已完成（2026-09-08）
2. ~~`round`/`step`（1.1）~~ ✅ 已完成
3. ~~归档 / 删除（1.2）~~ ✅ 已完成
4. ~~写入质量三修（1.5）~~ ✅ 已完成（2026-09-08）
5. ~~锁目录收口 / 入队失败可见 / 锁文件自清理（1.6）~~ ✅ 已完成（2026-09-08）
6. **写入幂等（1.4）** —— 用户明确「暂不做」。
7. ~~去掉 `turn` 列（1.7）~~ ✅ 已完成（2026-09-08，路线 A）
8. **可选优化（2.x）** —— 写入微批（治本，大改）/ 写入计数器次级触发（小）/ FTS 增量更新（小）；按需再议。

---

## 5. 运维注意

- **ZCode 不会每次都自动拉起被杀的 MCP 进程**：调用任意一个 MCP 工具（如 `list_sessions`）即可触发重连。
- **生产库治理在安装版目录执行**：`C:\Users\Think\.agent\tools\chat-history` 下运行 `python tools/backup.py ...`；在项目版目录执行会操作项目版空库（可用 `CHAT_HISTORY_DB` 显式指定）。
- **回滚点**（均含 SHA-256 清单）：`.agent/backups/chat-history-lockdir-20260908-214938/`（锁目录改动前）、`.agent/backups/chat-history-stale-table-20260908-215439/`（快照修复前）、`.agent/backups/chat-history-multiinstance-20260908-220042/`（多实例端口与竞态修复前）；更早的 `.agent/backups/chat-history-gc-20260908-185647/`。

---

## 6. 架构审查 A 组小修复 —— ✅ 已完成（2026-09-08）

完整架构审查（2026-09-08 晚）产出的 A 组「小修复」8 项，全部是局部缺陷、**不改变对外行为**：

| # | 修复 | 位置 | 要点 |
|---|---|---|---|
| A1 | 入队告警查错库 | `.agent/hooks/hook_common.py` | `queue_alert()` 原查 `pending.db` / 表 `pending`，真实队列是 `chat_pending.db` / 表 `chat_pending`。已对齐文件名与表名。**2026-09-08 23:52 回退**：该函数连同 engram 残留一并删除，队列可观测改为 `/health` 的 `queue_error`/`queue_pending`（见 §9）。 |
| A2 | 标题反查并发迭代 | `title_cache.py` | `ids_for_title` 原无锁迭代共享 dict，而 `_persist` 在锁内**原地**增删同一对象 → 并发可抛 `RuntimeError: dictionary changed size`。改为锁内快照后遍历。 |
| A3 | `restore` 缺运行时守卫 | `tools/backup.py` | 复用 `vacuum` 的 `_guard`：MCP 在跑或有队列积压时拒绝恢复（`--force` 跳过），避免 `rmtree` 掉正在被写的库。新增 3 项测试。 |
| A4 | 维护日志误报 | `maintenance.py` | 原来行数变化一律记 WARN；optimize 期间有并发写入是正常的。改为区分：变多 → 记「含 optimize 期间并发写入 N 行」，变少 → `WARN 行数减少`。 |
| A5 | 删除意向表无界 | `core.py` | `_delete_intents` 中被放弃的意向永不清理；超过 64 条时回收已过期项。 |
| A6 | 清理与误用防护 | `db.py` / `__pycache__` / `README.md` | 删 `db.py` 未使用的 `FTS` 导入；陈旧 `title_fetcher.cpython-314.pyc` 送回收站；README 增加「生产库不在本目录」警告。 |
| A7 | 测试文档漂移 | `tests/README.md` | 原文停留在 43/56 项、2 项失败、退出码 1；更新为当前基线、10 个测试文件、退出码 0。 |
| A8 | 双源常量无校验 | `tests/test_errors.py` | 新增断言：`error_codes.json` 键集合必须等于 `errors.ERROR_REASONS`，且每条含 `reason`/`fix`。 |

验证：项目测试 **175 项（174 通过 / 0 失败 / 1 跳过）**，新增 4 项；项目版与安装版运行时文件哈希一致；回滚点 `.agent/backups/chat-history-auditfix-20260908-230009/`（86 文件 + SHA-256 清单）。

未做（属其他分组）：D3 把 `queue_alert` 接进 SessionStart；B / C / E 组按原分类待议。

---

## 7. 架构审查 B 组重构 —— ✅ 已完成（2026-09-08）

用户选做 **B1、B2 与 B3 的「计数路径」**；**B3 的归档/恢复搬行逻辑与 B4 均不动**。

| 项 | 内容 | 结果 |
|---|---|---|
| **B1** | MCP 接线从领域层拆出 | 新增 `mcp_tools.py`（`_extract_session_id` + `_build_server` + 5 个工具闭包），是全项目**唯一** import `mcp.server` 的地方；`core.py` 不再依赖 MCP SDK（`http_server` / `db` 因此也不再被动依赖）。闭包统一按模块调用 `core.xxx`，patch 归属模块即生效。 |
| **B2** | 门面去 re-export | `mcp_server.py` 从 56 行缩到 43 行，只留 `main()`；删掉 40+ 个 re-export，`main()` 每步都按归属模块调用（`db.` / `http_server.` / `mcp_tools.` / `maintenance.`）。测试全面改为 import 归属模块（约 200 处 `server.X`）；`tools/backup.py verify` 改 import `db`。**破坏性**：`from mcp_server import X` 不再可用。**回归与修复**：`chat_worker.py` 的本地兜底路径调 `mcp_server.remember`，re-export 删除后失效 → 23:32 一条 tool 记录重试 3 次被标 ERROR（已改调 `core.remember`，并用「死端口 + 临时库」脚本验证兜底可用；该行已重放入库）。 |
| **B3** | 计数路径不再读整行 | `archive.count_rows` 改用 `count_rows(filter=...)` 下推计数；`session_info` 用同一计数 + `select(["session_title"])` 取标题。**归档/恢复/删除的搬行路径（`_session_rows`，必须带 vector）未改。** 收益：最大会话 2852 行 × 4 KB ≈ **11.7 MB** 向量不再被「只为计数」读进内存。 |
| **B4** | 不动 | `list_sessions` 全表聚合维持现状；建议设规模触发阈值（行数过 5 万或实测 >100ms 再评估）。 |

验证：测试 **180 项（179 通过 / 0 失败 / 1 跳过）**，新增 3 项护栏——B1：子进程屏蔽 `mcp` 包后仍能导入 `core/db/http_server/archive/title_dispatcher`；B2：门面不得再有任何 re-export；B3：`_session_rows` 被替换成抛异常后计数/取标题仍正常。项目版与安装版哈希一致；新代码对生产库 `verify` 通过；kill 两进程 → 调工具唤醒 → `/health` 全绿、队列 0。回滚点 `.agent/backups/chat-history-refactor-b-20260908-231413/`（80 文件 + SHA-256）。

---

## 8. 架构审查 C 组：C3 `limit` 语义统一 —— ✅ 已完成（2026-09-08）

用户选 **C3 方案 (i)**：`limit<=0` 在 `recall` 与 `recent` 上语义统一为「要 0 条就是 0 条」。

- 改动：`core.search_recall` 的 limit 钳位下限从 1 改为 0，并在解析 session 之后、打开表之前短路返回空；`search_recent` 原本就是该语义，只补了文档说明。
- 行为变化：`recall(limit=0)` 由「返回 1 条」变为「返回空」，负数同样返回空。`top_k` 下限仍为 1（防止 LanceDB `limit(0)` 退化成全表读）。
- 测试：**181 项**（新增 `test_zero_or_negative_limit_returns_empty_for_both`；原 `test_recall_top_k_zero_does_not_read_full_table` 改用 `limit=1`，继续覆盖 top_k 钳位）。
- 回滚点：`.agent/backups/chat-history-c3-20260908-233158/`。

**C1（向量推理移出锁）已完成（2026-09-11，见 §14）；C2（缺 FTS 时降级）未做**；B4、B3 档 3 同样未做；E 组已完成建仓与 `pyproject.toml`（CI 挂起，见 §11）。原 D3「把 `queue_alert` 接进 SessionStart」**已改道**为 §9 的按需查询方案。

---

## 9. 队列可观测改为「按需查询」—— ✅ 已完成（2026-09-08）

**决策**：不做注入式告警（原 D3），改为把队列状态挂到 `/health`，同时删除 `queue_alert()` 这个 engram 残留。

- **来历**：`queue_alert()` 是 2026-08-15 为 engram 写的（宿主是 engram 的 SessionStart 钩子、数据源是 `pending.db`），08-16 接进 `session_inject.py`；2026-09-05 engram 下线时钩子与队列一并移除，它成了孤儿（A1 只把队列名改对，调用点仍缺）。
- **为什么不接注入**：失败本来就已可见——`chat_worker.log` 有 `[FAIL]`、队列行状态是 `ERROR`；缺的只是"有人主动看"。把告警推送进 agent 上下文容易让 agent 偏离用户当前任务，收益与代价不匹配。
- **做法**：`core._queue_status()` 只读 `~/.agent/hooks/chat_pending.db`（`CHAT_PENDING_DB` 可覆盖，与 `tools/backup.py` 同约定），`/health` 新增 `queue_error`（ERROR 条数）与 `queue_pending`（pending+processing 积压）。文件读不到返回 `null`（区别于 0），且不拉低 `ok`。
- **删除**：`hook_common.queue_alert()`（约 20 行），文件只留 `log()`/`_rotate()`；A1 的改动随之作废（见 §6 A1 行）。
- **测试**：**183 项**（新增 2 项：队列有 ERROR/积压时正确上报；队列文件缺失时返回 null 且不影响 ok）。`tests/support.py` 同时把 `CHAT_PENDING_DB` 指向临时目录，避免测试读到生产队列。
- 回滚点：`.agent/backups/chat-history-queuehealth-20260908-234858/`。

---

## 10. 测试精简 —— ✅ 已完成（2026-09-08）

逐用例计时后做的最小精简，**覆盖不减**：

| 项 | 处理 | 理由 |
|---|---|---|
| `StorageTests.test_recent_returns_empty_for_nonpositive_limit` | **删除** | 断言（`recent_messages(limit=0/-5)==""`）与 C3 的 `BoundTests.test_zero_or_negative_limit_returns_empty_for_both` 完全重复，后者还多覆盖 `search_recall/search_recent/recall` 三个入口 |
| `BoundTests.test_recent_limit_clamped` 第二行断言 | **删除** | 同上；该用例的独有价值是超大 limit 钳位，保留 |
| `HealthTests`（5）/ `HttpBodyLimitTests`（2）/ `test_adapters.HttpTests`（4） | **改按类共用 server**（`setUpClass`/`tearDownClass`） | 原来每个用例起停一个真实 `ThreadingHTTPServer`（约 0.6s/个），现每类一个；handler 无状态、env 逐用例 patch，语义不变 |

结果：**183 → 182 项，24~26s → 21.4s**。最慢的两个仍是子进程守卫（1.25s / 1.14s），是 B1 与 backup 脚本模式唯一护栏，保留。回滚点 `.agent/backups/chat-history-testtrim-20260908-235605/`。

---

## 11. GitHub 仓库 —— ✅ 已建立（2026-09-09）

**仓库**：<https://github.com/ThinkofRain1213/chat-history-db>（**公开**，默认分支 `main`）

| 项 | 说明 |
|---|---|
| 纳入范围 | 项目版源码 + 文档 + 测试；首次提交 `238cd7a` 时 **49 个文件 / 7319 行**，加 `pyproject.toml` 后 50 个（最大文件 31 KB） |
| 排除项（`.gitignore`） | `.venv/`、`models/`（约 4.4 GB）、`chat.db/`（运行时 LanceDB 库）、`title_cache.json`（会话标题缓存）、`__pycache__/`、`*.db` |
| git 身份 | 本机原先未配置，已设全局 `user.name=ThinkofRain1213`、`user.email=126307993+ThinkofRain1213@users.noreply.github.com`（GitHub noreply，不暴露真实邮箱） |
| 行尾 | 仓库级 `core.autocrlf=false`，按文件原样存储（项目内 CRLF/LF 混合），不改工作区文件 |

**踩过的坑**：`.gitignore` 首版把说明写在模式行尾（`models/   # 注释`），而 git **不支持行尾注释**——整行被当成模式，`models/` 与 `.venv/` 因而未被排除，`git add -A` 把约 5 GB 内容写进 `.git`（膨胀到 1019 MB）。已终止进程、删除该 `.git` 后重来，注释改为独占行。

### E2 进展

| 子项 | 状态 | 说明 |
|---|---|---|
| `pyproject.toml` | ✅ 已完成（2026-09-09） | 只声明项目元数据 + `requires-python = ">=3.14"` + 仓库 URL。**不做 pip 打包**：`config.py:7` 的 `_BASE = Path(__file__).resolve().parent` 把「模块所在目录」当项目根来定位 `models/`，打包进 site-packages 后语义失效，故不写 `build-system`。依赖唯一来源仍是 `requirements.txt`（不重复声明，避免双源漂移——项目刚在 A8 修过一处双源）。提交 `e5e5541`；加文件后测试 182 项仍全绿。 |
| CI | ⏸ 挂起（用户 2026-09-09 决定） | 可行性已评估：默认测试不加载真实模型，CI 无需 4.4 GB 模型，装依赖 + 跑测试约 1~3 分钟；但 `test_http_server.py` 端口探测、`test_round_step.py` 跨进程文件锁有平台相关成分，**Linux runner 能否全绿未实测**，重启该事项时先用 `windows-latest` 验证。 |

**待办**：安装版（`.agent/tools/chat-history`）仍靠手工同步，仓库只管理项目版。
**注意**：代码与文档中含本机绝对路径（`C:\Users\Think\...`），公开仓库下会暴露目录结构；如需隐藏可后续改为相对路径或占位符。

## 12. 写入雪崩事故的应急加固 —— ✅ 已完成（2026-09-10）

**事故**：2026-09-10 19:26–19:53，MCP `remember` 与 HTTP `/remember` 两条写入通道全部挂死（30s 超时且不落库），纯读取（`list_sessions`/`recent`）正常。

**根因**：LanceDB 把嵌入函数参数**冻结进表 schema 元数据**，开表时用 `create(**obj["model"])` 重建实例——建表那一刻的 `model_dir` 绝对路径跟着库走，运行时 `config.MODEL_DIR` 被完全忽略。本库冻结的是**已不存在**的项目副本路径（`models/` 被 `.gitignore` 忽略、按设计本就不入库），于是每次嵌入都在 `Tokenizer.from_file` 抛错。修法：`BGEM3Embedding.model_dir` 加 pydantic `field_validator(mode="before")`，路径不可用（含空值）即回落到 `config.MODEL_DIR`。

**放大器（本节加固的对象）**：① 失败点在 `tbl.add()` 内，而 `_session_lock` 与 `_WRITE_LOCK` 跨在整个 `tbl.add` 外 → 锁被握着不放；② LanceDB 指数退避 `max_retries=7`，实测 3.1/9.5/25.0/69.2/223.8s，累计约 **17 分钟**；③ `_session_lock` 是 `while True … sleep(0.02)`，**无超时** → 后来者永久自旋；④ `chat_hook._enqueue` 每事件无条件 `Popen` 一个 worker、**无单例**，还挂在 `PostToolUse` matcher `.*` 上 → 14 分钟堆到 **364 个**进程；⑤ 364 × ~0.6GB 提交量 → 提交量 124.4/126.9 GB 耗尽，连新进程都起不来（`OSError [WinError 8] 内存资源不足`），队列积压 642 条全是 `processing`。

| 加固项 | 实现 |
|---|---|
| 等锁超时 | `db._LOCK_TIMEOUT_SEC = 120`（`CHAT_HISTORY_LOCK_TIMEOUT` 可覆盖）；超时抛 `DatabaseError`，**绝不越过锁继续写**（越锁会读到旧尾行、算出相同 round/step） |
| worker 单例 + 排空 | `chat_worker` 启动抢 `chat_worker.lock` 字节锁，抢不到即退出（进程退出由系统释放锁，不留死锁文件）；`chat_hook._worker_running()` 先探测以少起进程；worker 由「处理一批就退出」改为「**排空为止**」（`MAX_BATCHES=500` 防死循环）——否则单例会让后入队的消息滞留到下一次事件 |
| 错误/告警落盘 | 新增 `logfile.py`（默认 `~/.agent/hooks/chat_errors.log`，`CHAT_HISTORY_ERROR_LOG` 可覆盖）：`mcp_server.main()` 与 worker 启动时 `setup()` 给 root logger 挂 WARNING handler（接住 LanceDB 的重试告警），`errors.log_error` 除 stderr 外同步追加同一文件。事故之所以半小时无人察觉，正是因为重试只 `logging.warning` 到 stderr、而 ZCode 丢弃 MCP 的 stderr |

**验证**：项目测试 **188 项**（187 通过 / 0 失败 / 1 跳过）。实测三件事：持锁期间入队成功且 worker 数保持 0；释放后自动拉起并排空；25 条积压一次排空 5.1s；故意触发错误后 `chat_errors.log` 出现与 stderr 同源、不含消息体的记录。

**踩坑记录**：Windows 下 `logging.FileHandler` 活着会让用例临时目录删不掉（`WinError 32`）——本轮仅有的两次测试失败都出在这里；`logfile` 因此配了对称的 `close()`，由 `tests/support.py` 在用例结束时统一调用，而不是让每个用例自己收拾。

**回滚点**：`.agent/backups/2026-09-10-chat-hook-archive/`（`before-hardening/` 是加固前的运行副本 `db.py`/`errors.py`/`mcp_server.py`；同目录另有本次事故的 `config.json.bak`、`bgem3_embedding.py.before`）。

**遗留**：`PostToolUse` 的 matcher 仍是 `.*`，即每次工具调用都会调一次钩子脚本（现在至多派生一个 worker）。要进一步降开销，可考虑只在队列非空时拉起，或收窄 matcher——未决策。

---

## 13. 模型外包：会话子进程不再各自加载模型 —— ✅ 已完成（2026-09-11）

**问题**：ZCode 给每个会话起一个独立 MCP 子进程。hooks 写入早就复用了 hub（`/remember`），但 **MCP 工具路径没有**：agent 只要调一次 `recall`/`remember`，那个会话进程就会把 bge-m3（2.2G）+ bge-reranker（2.2G）读进自己的内存（实测该进程 Commit 4548MB / WS 约 4.0G）。几条会话同时用工具就是几份模型。

**做法**：hub（抢到 17891 的那个进程）开放模型能力，其它会话进程把「算模型」外包出去。

| 侧 | 改动 |
|---|---|
| 客户端 | 新增 `model_hub.py`：`post(path, payload)` POST 到 hub，**任何失败返回 None**；`CHAT_HISTORY_MODEL_HUB=0` 可整体关闭 |
| 嵌入 | `bgem3_embedding.BGEM3Embedding._embed` 改为「先问 hub；拿不到、或返回条数不匹配，就落 `_embed_local`（原实现）」。hub 侧 `/embed` 调的正是 `_embed_local`，所以两边向量必然一致（同模型、同归一化与 NaN 清洗） |
| 重排 | `reranker.score` 同构：问 hub，失败落 `score_local`（原实现） |
| hub 侧 | `http_server.py` 的 `do_POST` 改为路由表（`/remember`、`/embed`、`/rerank`），新增 `_embed_route` / `_rerank_route`；`/rerank` 的 `model_dir` 不可用时回落本进程 `RERANK_DIR` |
| 超时 | 单次 30s（首次调用可能要等 hub 侧加载模型）；写路径仍握着会话锁，所以不能无限等——锁的等待上界另有 `db._LOCK_TIMEOUT_SEC` 兜底 |

**语义不变**：hub 不可用（没选上端口 / 已退出 / 正在换人）时自动回落本地 ONNX，与改造前完全一致；`_embed` 只在 hub 返回条数与请求一致时才采用其结果。

**验证（2026-09-11）**：

- 项目测试 **204 项**（188 → 204，新增 `tests/test_model_hub.py` 16 项）。`support.py` 统一设 `CHAT_HISTORY_MODEL_HUB=0`——否则用例会连上本机真实运行的 hub 拿回真向量、绕过 `_load` 的 patch，NaN 清洗与「加载失败报 ModelError」两类断言会失真；`test_embedding.py` 不走 `IsolatedCase`，单独 patch 了 `model_hub.post`。
- 隔离实测（A = 新代码起的 hub，中性端口 17895；B = 直调 `core.recall`）：B 返回 7842 字真实结果，而 **B 未导入 onnxruntime、嵌入/重排模型缓存均为 0**；A 侧 onnxruntime 已加载、Commit 4523MB（两份模型都在 hub，只一份）。
- MCP 层实测（A/B 都是新代码 `mcp_server.py`）：`recall` 经 MCP 工具返回 7842 字、`isError=False`。首次 24s = hub 冷启动加载两份模型，之后复用。
- 过程中观察到一次瞬时 `E_DATABASE`（四进程并发启动、且真实生产进程同时在写时的一次读失败）：隔离层与 MCP 层各自复跑均未复现；本次改动没有触碰任何 DB 访问路径。
- 两副本同步并逐字节校验（改前哈希一致，证明回滚点对两副本都有效）：`model_hub.py`、`bgem3_embedding.py`、`reranker.py`、`http_server.py`。回滚点 `.agent/backups/chat-history-modelhub-20260911/`。

**仍未做**：会话进程依然要 import lancedb/pyarrow（约 592MB Commit / 138MB WS 的底，其中 490MB 是 numpy/OpenBLAS 的记账预留）；`recall` 的向量检索本身仍在会话进程内执行。C1（向量推理移出锁）已在 §14 单独完成。

**生效范围**：只对**新起**的 MCP 进程生效——已在运行的会话子进程仍是旧代码，等 ZCode 重启或会话重建后才走外包。

---

## 14. C1：向量推理移出锁 —— ✅ 已完成（2026-09-11）

**改前**：`core.remember` 里两把锁（进程级 `_WRITE_LOCK` + 跨进程按会话文件锁 `_session_lock`）跨在整个 `tbl.add([row])` 外面，而 `row` 不带 `vector`，于是 **LanceDB 在 `add` 内部自己调嵌入函数**算向量 —— 一次同步的 bge-m3 推理（1~2s，冷启动更久）全程在锁里。更糟的是失败路径：LanceDB 对嵌入调用套的是 `compute_source_embeddings_with_retry`（`lancedb/embeddings/base.py:135-146`，`retry_with_exponential_backoff`，`max_retries=7`），实测退避 3.1/9.5/25/69/223.8 秒、**累计约 17 分钟**，这 17 分钟同样在锁里 —— 这是 2026-09-10 写入雪崩的放大器（§12）。

**改法**：向量在**锁外**先算好，锁内只做「重开表 → 读尾行 → 推导 round/step → 带向量写入」。

```python
    db_handle = _ensure_db()
    vector = _emb.compute_source_embeddings([text])[0]   # ① 锁外算
    with _WRITE_LOCK:
        tbl = _ensure_messages_table(db_handle)
        with _session_lock(session_id):
            tbl = _open_or_none(db_handle) or tbl
            tail = _session_tail(tbl, session_id)
            ...推导 round/step（未改）...
            row = dict(..., text=text, vector=vector)    # ② 带上显式向量
            with error_boundary(DatabaseError, "append message"):
                tbl.add([row])                            # add 不再触发嵌入
```

四个关键点：

1. **`compute_source_embeddings`（不是 `_with_retry` 版本）**：不带那 7 次指数退避，失败立刻上抛；而且是在锁外抛，锁从未被取过。重试交给上层队列（hook worker 自己的重试策略）。
2. **不直接调 `_emb._embed`**：`compute_source_embeddings` 会先过 `sanitize_input`，与 LanceDB 自己调用时的入参处理逐字一致。
3. **`add` 为什么会跳过嵌入**：`lancedb/table.py:849-851` 只在「该列缺失，或该列全为 null」时才自己算 —— 给了非空 `vector` 就走我们的。
4. **锁内那段「重开表 → 读尾行」一行未动**：那是 §1.6 修掉的旧快照撞号真因，C1 只搬推理。全项目 `tbl.add([row])` 只此一处（`archive.py` 搬的是已带 vector 的旧行，不触发推理，无需改）。

**代价**：向量先算后写；若后续写入失败（并发 step 冲突、DB 报错），这一次推理白算——1~2 秒 CPU。

**验证（2026-09-11）**：

- 项目测试 **207 项**（204 → 207）。新增 3 项：
  - `test_embedding_runs_outside_the_session_lock`：把 `core._session_lock` 换成记账实现，让嵌入实现在被调用那一刻断言「此刻未持有会话锁」——**反证**，谁把推理挪回锁里这条就失败；
  - `test_written_row_carries_the_precomputed_vector`：写后读回，断言库里的向量就是锁外算好的那一份（防止有人去掉 `row["vector"]` 让推理静默回到锁内）；
  - `test_embedding_failure_does_not_hold_the_lock`：嵌入抛错时秒级上抛，且之后同一会话仍能正常写入。
- **并发实测**（同一会话、两个进程同时写；假 hub 每次 `/embed` 睡 3 秒以放大差异）：
  - 改前（运行版旧 `core.py`）：第二次写入 **6.16s**、总墙钟 7.46s（两次推理被锁串起来）；
  - 改后：两次都在 **3.1s** 完成、总墙钟 4.29s（推理并行）；round/step 分别为 (1,0)、(2,0)，库里的向量与 hub 返回的一致。
- **失败场景实测**（同一会话，一个嵌入坏掉、一个正常）：坏的 **0.09s** 抛 `ModelError: embedding inference`，正常的 3.10s 完成、未被阻塞（改前它会在锁内退避约 17 分钟）。
- 两副本同步并校验（`core.py` 改前哈希一致）；回滚点 `.agent/backups/chat-history-c1-20260911/`（`project/core.py`、`runtime/core.py`、`test_round_step.py`）。

---

## 15. range 时间语法重做 + 空结果文案 + 5 条工具描述 —— ✅ 已完成（2026-09-11 21:36）

**用户指令**：先只改文本定稿（5 条 description + range 规格），最后统一合并落地。

**新语法**（破坏性，替换旧的连字符时段 / 裸时刻单值）：

- **单值**（不含 `/`，只接受日期级整段）4 种：`'YYYY'` 整年 / `'YYYY-MM'` 整月 / `'YYYY-MM-DD'` 整天 / `'MM-DD'` 整天（缺年按今年）。
- **区间**（必须含 `/`）恒左闭右开：`'起点/终点'`，或 `'起点/'`（终点留空 = 到此刻）。
- **补全只补更粗的粒度**：缺年按今年、缺年月日按今天，**不从另一端借**；**两端缺法必须一致**（都写全 / 都缺年 / 都缺年月日），一端有一端没有一律 `E_INVALID`。
- **段取值**：左端取段首、右端取段尾（所以 `'2026/2026'` 是整年，`'2026-08-15/2026-08-18'` 含 8/18 全天）。
- **判据**：止 < 起（负宽）非法；止 == 起（零宽）合法但必然 0 行。

**随之作废**：`'09:00'`（裸时刻做单值）、`'2026-08-15 09:00'`（日期+时刻不带 `/`）、`'09:00-17:00'` / `'23:00-01:00'`（连字符时段）；跨零点/跨年不再隐式滚动；`'2026-08-01/17:00'` 这类「一端完整一端缺省」也由合法转非法（要写 `'2026-08-01 00:00/2026-08-01 17:00'`）。

**空结果文案**（新增）：有 range 无命中（含零宽）→ `该时段没有消息`；无 range 无命中 → `没有消息`；另表有该会话时与 `_other_table_hint` 用 `。` **拼接**（不再二选一）。空串返回彻底取消，`limit<=0` 路径也吃这条。

**改动**：

| 文件 | 改动 |
|---|---|
| `timeutil.py` | `_parse_time_range` 重写；新增 `_hhmm` / `_day_span` / `_month_span` / `_bound`（形态 + 缺什么）/ `_value`；删掉旧 `_tm`（连字符时段 + 跨天滚动 + 未来取空） |
| `core.py` | 两处完全重复的 `E_INVALID` 文案抽成 `_RANGE_HELP`；新增 `_empty_note`/`_empty_result`，`recall`、`recent_messages` 两处调用点替换 |
| `mcp_tools.py` | 5 处 `description` 全部重写（remember / recall / recent / list_sessions / session_admin） |
| `tests/test_logic.py` | `test_calendar_ranges` 26 条（原 7 条里 4 条旧写法已作废）；`test_invalid_ranges` 29 条（语法 17 + 缺法不一致 7 + 负宽 5），整个循环套上固定时钟——负宽依赖「此刻」 |
| `tests/test_storage.py` | 5 处空串断言改文案；1 处 range 字面改 `'2026-09-07 09:00/2026-09-07 10:00'`（旧字面在新语法里非法）；新增 3 项（有 range 空、零宽、另表提示拼接） |
| `skills/chat-history/SKILL.md` | 两份（`.agent\skills\chat-history\` 与仓库快照）range 段重写，改后逐字节一致 |

**验证（2026-09-11 21:36）**：

- 独立脚本按权威表核对解析器：**合法 26 条 / 非法 29 条，零不符**。
- 项目全量测试 **210 项**（207 → 210，新增 3 项），**OK (skipped=1)**，28.5s。
- 两副本同步：`timeutil.py` / `core.py` / `mcp_tools.py` 逐字节哈希一致；未改动的 `archive/db/errors/config/http_server/mcp_server` 复核仍一致。
- 回滚点 `.agent/backups/chat-history-range-merge-20260911-2130/`（project 5 个文件 + runtime 3 个模块）。

**注意**：range 行为与工具描述**要重启 MCP 进程才生效**（工具表在连接时发给模型）。依赖「空串判断无结果」的下游要改——仓库内 grep 过只有测试。

---

## 变更记录

- **2026-09-08 19:18** 初版，承接 `REPAIR_SPEC_20260908.md` §6（该节原 4 个问题已全部收敛）。
- **2026-09-08 20:50** 工具面去重：移除 `session_admin(action='list')`，列出统一走 `list_sessions(source=…)`（详见 `DESIGN_1.2_ARCHIVE.md` §11）；同步修 `chat-history` skill 文档漂移（补 `session_admin`、`source`、`round`/`step`、`E_NOTARCHIVED`、源码路径）。
- **2026-09-08 21:08** 1.3 hooks 修复完成：根因重新定性（(a) 原根因不成立）、日志去噪 + 窗口回退、worker 默认路径与防写错库守卫、去掉 300 字截断；详见 `DESIGN_1.3_HOOKS.md`。
- **2026-09-08 21:35** 1.5 写入质量三修完成：mid 响应级认领去重、锁键归一（修 `(round,step)` 碰撞根因）、NaN 向量清洗 + `/health` 计数；详见 `DESIGN_1.5_WRITE_QUALITY.md`。1.4 维持暂不做。
- **2026-09-08 21:56** 1.6 锁目录收口完成：锁目录改为库旁 `<db>.locks`（不再依赖 TMPDIR）、`realpath` 归一 junction/短名、`_ensure_db` 比归一键、`_enqueue` 失败落日志、锁文件超 500 自动清理 + TEMP 旧残留（1002 文件）回收；详见 `DESIGN_1.6_LOCK_DIR.md`。测试 167 passed。
- **2026-09-08 22:00** 复验发现本轮又出现 `(23,36)`/`(23,57)` 重复 → 探针定位真因：`core.remember` 锁外开表读到**旧快照**（LanceDB 表对象绑定打开时的版本），文件锁保证互斥但保证不了新鲜度。已改为锁内重开表 + 回归测试（含反证）；同步更正 1.5 的根因结论。测试 168 passed，生产修复后 32 行 0 重复。
- **2026-09-08 22:05** 1.6 追加两项：**项 6 多实例端口**（探针纠正认知：Windows `SO_REUSEADDR` 让第二个进程静默绑定成功 → 改为先探测再绑 + `chat_http.log` + 30s 重试接管）、**并发首建竞态**（`_ensure_messages_table` 容忍「表已存在」并幂等补 FTS，建表/追加合并为一条路径）。测试 **172 passed**；冷启动两进程并发建表 e2e 16 行 0 重复；生产累计 110 行 0 重复。
- **2026-09-08 22:35** 新增 **1.7 去掉 `turn` 列**的可行性与迁移方案报告（`DESIGN_1.7_DROP_TURN.md`）：副本实测路线 B 数据操作 0.05s+0.01s、尾行 27 会话 0 不一致、FTS 保留；`drop_columns` 不可逆、顺序必须先落代码再 drop、`turn` 已不唯一（143 组重复）。**未实施，待用户选路线。**
- **2026-09-08 22:30** **1.7 实施完成（用户选路线 A 语义重建）**：按 `(turn,time,物理序)` 重排老数据 round/step → 重建表删 `turn`。生产 5835 行 / 0.43s；schema 8 列；`(session,round,step)` 全库唯一；`round` 1..249、`round=0` 行数 0；FTS 保留；`/health` 全绿；测试 **171 passed**。代码：`db.Msg` 去 turn、`_session_tail`/`_recent_rows` 改 round/step 排序、删 `_session_max_turn`、`_ref` 回退改 `#0.step`、新增 `tools/migrate_drop_turn.py`。回滚点 `.agent/backups/chat-history-dropturn-20260908-222836/`。详见 `DESIGN_1.7_DROP_TURN.md` §7。
- **2026-09-08 23:10** 架构审查 A 组小修复 8 项完成（A1 告警查错库 / A2 标题反查并发 / A3 restore 守卫 / A4 维护日志误报 / A5 删除意向表回收 / A6 清理与误用防护 / A7 测试文档 / A8 双源常量校验）；项目测试 **175 项**；回滚点 `.agent/backups/chat-history-auditfix-20260908-230009/`；详见 §6。
- **2026-09-08 23:25** 架构审查 B 组重构完成（用户选 B1/B2 + B3 计数路径）：新增 `mcp_tools.py` 解耦 MCP SDK、`mcp_server.py` 只留 `main()` 并删除 40+ re-export、`archive.count_rows/session_info` 改过滤下推计数；测试改为 import 归属模块（约 200 处）；项目测试 **180 项**；回滚点 `.agent/backups/chat-history-refactor-b-20260908-231413/`；详见 §7。
- **2026-09-08 23:33** 修 B2 回归：`chat_worker.py` 本地兜底路径原调 `mcp_server.remember`（re-export 已删）→ 一条 tool 记录重试 3 次标 ERROR；改调 `core.remember`，用「死端口 + 临时库」脚本验证兜底可用，该行已重放入库；队列回到 0/0/0。
- **2026-09-08 23:35** 架构审查 C3 完成（用户选方案 i）：`recall`/`recent` 的 `limit<=0` 统一为返回空；`recall(limit=0)` 由 1 条变 0 条；项目测试 **181 项**；回滚点 `.agent/backups/chat-history-c3-20260908-233158/`；详见 §8。C1/C2 与 D/E 组未做。
- **2026-09-08 23:52** 队列可观测改道：删除 engram 残留 `hook_common.queue_alert()`（A1 随之作废），改为 `/health` 新增 `queue_error`/`queue_pending` 按需查询（不做注入）；测试 **183 项**；回滚点 `.agent/backups/chat-history-queuehealth-20260908-234858/`；详见 §9。
- **2026-09-08 23:57** 测试精简：删 1 个被 C3 用例完全覆盖的用例 + 1 行冗余断言；三处 HTTP 测试改按类共用 server（183→182 项，24~26s→21.4s）；覆盖不减；回滚点 `.agent/backups/chat-history-testtrim-20260908-235605/`；详见 §10。
- **2026-09-09 00:15** 建立公开 GitHub 仓库 `ThinkofRain1213/chat-history-db`：项目版 49 个文件 / 7319 行入库，`.gitignore` 排除模型、venv、运行时库与标题缓存；本机 git 身份设为 GitHub noreply 邮箱；详见 §11。
- **2026-09-09 00:25** E2 的 `pyproject.toml` 完成（用户指令「e2先做吧，ci挂起」）：只声明元数据 + `requires-python = ">=3.14"`，**不做 pip 打包**（`config.py:7` 以模块所在目录为项目根，打包后路径语义失效）；依赖仍由 `requirements.txt` 唯一管理；提交 `e5e5541`，加文件后测试 182 项全绿；CI 挂起并记录可行性评估；详见 §11。
- **2026-09-09 12:56** 同步 `skills/chat-history/SKILL.md` 到最新版：仓库镜像落后唯一源（`.agent\skills\chat-history\SKILL.md`）约 2.8 KB，缺 `session_admin`/`source`/`round`-`step` 说明；提交 `5996d53`。
- **2026-09-09 13:05** recall/recent 行格式调整（用户定稿「按你的做」）：unscoped 行由「标题#轮次 | session_id | kind | time」改为「标题 | session_id | #轮次 | kind | time」（标题与 id 各占一格、轮次前移到 id 之后）；scoped 行不变。分隔符**统一 `|`**——用户原本提议 `#轮次 kind` 贴空格，我建议否掉（依赖「kind 永不含空格」的隐式约定，且保持 `|` 可让 scoped 形状完全不动、改动面更小），用户采纳。空标题退化为 `[session_id | #轮次 | kind | time | score]`；归档标记仍挂行尾 `| archive`。改动：`core.py` 的 `_format_recall`/`_format_recent`、skill 文档、`tests/test_storage.py` 新增 2 条格式断言（含空标题退化）。测试 **184 项**（183 通过 / 0 失败 / 1 跳过，26.5s）；已部署安装版并重启 MCP（**两对进程全杀**，避免另一对旧实例 30s 后接管端口），实测 scoped/unscoped/recall 三形状正确、`/health` 全绿；回滚点 `.agent/backups/chat-history-format-20260909-1302/`。
- **2026-09-10 20:20** 写入雪崩事故的应急加固完成（用户指令「两边同步做加固」，项目版与安装版同步；事故与根因详见 §12）：新增 `logfile.py` + `mcp_server.main()` 挂 logging 文件 handler（接住 LanceDB 重试告警）+ `errors.log_error` 同步落盘；`db._session_lock` 加等锁超时（120s，`CHAT_HISTORY_LOCK_TIMEOUT` 可覆盖）；`chat_worker` 抢单例字节锁并改为排空循环（`MAX_BATCHES=500`）、`chat_hook._worker_running()` 先探测。同事故的根因修复是 `bgem3_embedding.py` 的 `_resolve_model_dir`（元数据里冻结的 `model_dir` 失效即回落 `config.MODEL_DIR`）。项目测试 **188 项**（184 → 188，新增锁超时 1 项 + 落盘 3 项）；回滚点 `.agent/backups/2026-09-10-chat-hook-archive/before-hardening/`。
- **2026-09-11 15:20** 模型外包完成（用户选「只做模型复用」）：hub 新增 `/embed` 与 `/rerank` 两个路由，新增客户端 `model_hub.py`，`BGEM3Embedding._embed` 与 `reranker.score` 改为「先问 hub、失败落本地」（原实现搬去 `_embed_local`/`score_local`）；hub 不可用时行为与改造前完全一致。项目测试 **204 项**（188 → 204，新增 `tests/test_model_hub.py` 16 项）；隔离与 MCP 两层实测：会话侧 recall 正常返回但**未导入 onnxruntime、模型缓存为 0**，hub 侧两份模型 Commit 4523MB；两副本同步并校验；回滚点 `.agent/backups/chat-history-modelhub-20260911/`；详见 §13。
- **2026-09-11 15:45** C1（向量推理移出锁）完成（用户指令「做」）：`core.remember` 改为锁外先算向量（`_emb.compute_source_embeddings`，不带 LanceDB 的 7 次指数退避）再带 `vector` 写入，`add` 不再触发嵌入；锁内只剩「重开表 → 读尾行 → 推导 round/step → 写入」。项目测试 **207 项**（204 → 207，新增「推理不在锁内」反证、显式向量、失败不占锁 3 项）。并发实测：同一会话两进程同时写，改前第二次 6.16s（串行）、改后两次均 3.1s（并行）；失败场景：坏嵌入 0.09s 抛错、正常写不被阻塞（改前会在锁内退避约 17 分钟）。回滚点 `.agent/backups/chat-history-c1-20260911/`；详见 §14。
- **2026-09-11 21:36** range 时间语法重做 + 空结果文案 + 5 条工具描述落地（用户指令「你把修改落地吧」）：单值 4 种 / 区间必须带 `/` 且左闭右开 / **两端缺法必须一致**（「向另一端借日」废除）/ 零宽合法；空结果由空串改为「没有消息」「该时段没有消息」（另表提示改为拼接）；`timeutil.py`、`core.py`、`mcp_tools.py`、两个测试文件、两份 SKILL.md 全部更新；项目测试 **210 项** OK；两副本同步哈希一致；回滚点 `.agent/backups/chat-history-range-merge-20260911-2130/`；详见 §15。
