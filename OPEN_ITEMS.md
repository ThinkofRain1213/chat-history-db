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

**C1（向量推理移出锁）、C2（缺 FTS 时降级）未做**；B4、B3 档 3 同样未做；E 组已完成建仓与 `pyproject.toml`（CI 挂起，见 §11）。原 D3「把 `queue_alert` 接进 SessionStart」**已改道**为 §9 的按需查询方案。

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
