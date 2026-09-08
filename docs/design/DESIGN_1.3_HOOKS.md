# 1.3 设计探查：hooks 修复

- **状态**：✅ 已实施并部署（2026-09-08）
- **日期**：2026-09-08
- **对应未决项**：[`OPEN_ITEMS.md`](../../OPEN_ITEMS.md) §1.3
- **结论**：原报告三项里，**(b)(c) 成立；(a) 的根因不成立**，需重新定性——真实缺口与报告描述不同。

---

## 1. 结论速览

| 项 | 原描述 | 探查结论 |
|---|---|---|
| a | `_mid_from_rollout` 只匹配 `response.toolCalls` → 子代理工具调用取不到 mid | **根因不成立**。见 §3.1：主会话查找在钩子触发时有效（实测 4/4 命中，含 `Agent` 调用）；真正的问题是①日志 95% 是噪音（"本来就没有前言文本"被记成"未取到"）②子代理内部工具调用**根本不触发钩子**（整条记录都没有，不只是 mid） |
| b | `chat_worker.py` 默认 `CHAT_HISTORY_DB`/`CHAT_HISTORY_HOME` 指向项目版 | **成立**，真实隐患，已定位到行号 |
| c | `chat_hook.py` 工具描述 `[:300]` 截断 | **成立**，另有 `trace_split.py` 同款截断 |

---

## 2. 取证方法与原始数据

### 2.1 临时探针（已还原）

在 `chat_hook.py` 加临时 `_dbg()` 探针，记录每次 PostToolUse 的 payload 键、cid、rollout 查找结果；取证完成后已从备份还原（`sha256` 一致、探针零残留）。

### 2.2 钩子入参（现场）

PostToolUse payload 共 21 个键，关键项：

```
sessionId/session_id = sess_fcd9e781-…        （主会话 id）
toolCallId           = call_00_D29Va2nfUZbSi0kNc5ff3511   （完整 32 字符，未截断）
toolName/toolInput/toolResponse/toolResultPreview
transcriptPath/transcript_path、turnId、traceId、cwd、mode、permission_mode
```

→ **cid 是完整的**，不存在"传进来的 id 被截断导致匹配失败"。

### 2.3 钩子触发时的查找结果（4 次实测，全部命中）

| 工具 | cid | 文件大小 | 是否在尾部窗口 | 结果 |
|---|---|---|---|---|
| Bash | `call_00_D29Va2nf…` | 23.8 MB | True | HIT，text 24 字 |
| Bash | `call_00_ET_FGSUVheo…` | 24.2 MB | True | HIT，text 0 字 |
| Bash | `call_00_gQCcM0lTio…` | 24.5 MB | True | HIT，text 109 字 |
| **Agent** | `call_00_ygdBl3TqS8…` | 24.9 MB | True | HIT，text 136 字 |

→ 主会话的 mid 查找在钩子触发时**是有效的**，包括 `Agent` 调用本身。

### 2.4 今日量与失败率

| 指标 | 数值 |
|---|---|
| 今日工具事件（worker 日志 `kind=tool`） | 656 |
| 今日 mid 入库成功（`kind=mid`） | **223（34%）** |
| 今日 `mid 未取到` 日志 | **433** |
| 当前 rollout 窗口内：工具调用总数 / 位于「有文本行」的调用 | 80 / **30（38%）** |
| 当前 rollout 窗口内：含 toolCalls 的行 / 其中 text 为空 | 71 / **46（65%）** |

**34% ≈ 38%** → 今日 433 条失败里绝大多数**不是查找失败**，而是"该 model_io 行本来就没有前言文本，无 mid 可存"。

### 2.5 可核对子集的分类（425 条日志条目，按 cid 前缀匹配现有 rollout）

| 分类 | 条数 | 含义 |
|---|---:|---|
| A 前缀不在任何 rollout 文件 | 383 | 对应行已被 ZCode 裁剪（见 §2.6） |
| B 在文件里但该行 text 为空 | 41 | **正常**：无 mid 可存 |
| C 在文件里且 text 非空 | **1** | 真失败：目标行在文件**第 0 行**，落在 4 MB 尾部窗口之外 |

### 2.6 ZCode 会裁剪 rollout（关键环境事实）

- 主会话 rollout `model-io-sess_fcd9e781-….jsonl` 只有 **66 行 / 覆盖 20:49–20:57（8 分钟）/ 24 MB**，今日更早的行全部不存在。
- 子代理 rollout 文件同样被裁剪：探查期间 `…44f49200….jsonl` 消失、新文件 `…c0c096d6….jsonl` 出现。
- 结论：rollout 是**滚动窗口**，不是全量日志；任何"回看很久以前"的假设都不成立。

### 2.7 子代理工具调用：完全不触发钩子

跑了一个子代理（内部 1 次 Read + 1 次 Bash），结果：

- 探针日志**没有任何子代理的 PostToolUse 事件**（只有主会话的 `Agent` 调用那一条）。
- DB 中 20:50 之后只有主会话的行，**没有** `sess_subagent_agent_c0c096d6-…` 的任何 kind。
- 钩子配置 `PostToolUse` matcher 是 `.*`（全匹配），所以不是 matcher 的问题。

→ 子代理内部工具调用**整条记录都不会入库**（既无 mid 也无 tool）。这是客户端行为，钩子侧无法补。

---

## 3. 逐项根因

### 3.1 (a) mid 缺失 —— 重新定性

真实问题拆成三块：

1. **日志噪音（主因）**：`_mid_from_rollout` 只返回 `""`，调用方无法区分"文件里没有这一行"和"这一行有、但没有前言文本"。后者是正常情况（今日约 65% 的工具调用如此），却被记成 `mid 未取到`，导致 433 条/天的噪音掩盖真实故障。
2. **4 MB 尾部窗口会漏**（次因，实测 1 例）：`max_tail_bytes=4_000_000`，而文件可长到 24 MB。钩子触发时目标行通常就是最后一行、必然在窗口内，但存在边界情况（如目标行已因裁剪/重写落到窗口之外）。
3. **子代理工具调用零记录**（原报告想说的，但机制不同）：不是"取不到 mid"，而是**没有钩子事件**。要补只能换数据源（见 §4 a3）。

### 3.2 (b) `chat_worker.py` 默认路径指向项目版

- `chat_worker.py:19` `CHAT_DB = os.environ.get("CHAT_HISTORY_DB", r"C:\Users\Think\Desktop\项目\chat-history-db\chat.db")`
- `chat_worker.py:29` `PROJ = os.environ.get("CHAT_HISTORY_HOME", r"C:\Users\Think\Desktop\项目\chat-history-db")`
- 正常链路被 `chat_hook.py:58-59` 的 `env.setdefault(...)` 掩盖（hook 注入安装版路径），所以线上没出事；**手动执行 `python chat_worker.py`** 就会 `sys.path` 指向项目版、`import mcp_server` 加载项目版代码、写项目版空库。

### 3.3 (c) 工具描述 300 字截断

- `chat_hook.py:177` `_enqueue(f"{tname}: {desc}"[:300], session_id, "tool")`
- `trace_split.py:75` 同款 `[:300]`（该模块当前**不在钩子链路**上，仅测试/备用引用）
- 影响：长 Bash 命令（如 `find`/`grep` 多参数）、长 Edit 路径被截断，事后排查看不到完整命令。

---

## 4. 修复方案（建议，待确认）

### a1. 日志去噪 + 原因区分（必做，改动小）

让 `_mid_from_rollout` 返回 `(text, reason)`，`reason ∈ {ok, no_text, not_in_window, file_missing, no_cid}`：

- `no_text`（行在、但文本为空）→ **不记 WARN**（可计数汇总，或降到 DEBUG 级）。
- `not_in_window` / `file_missing` → 记 WARN（真实异常才可见）。

### a2. 窗口 miss 时全量回退（建议做，改动小）

尾部 4 MB 快路径未命中时，再全文件扫一次。ZCode 已把 rollout 压在 ~24 MB，全扫可接受，且只在 miss 时触发。

### a3. 子代理工具调用入库（**建议不做**）

钩子侧拿不到事件，只能改数据源（`trace_split` 从 ZCode 会话库 `message`/`part` 表补录）。工作量大、且与现有钩子链路是两套机制。**建议记为已知限制**，除非你要求补。

### b. worker 默认路径改为安装版 + 防写错库（必做）

- 默认值改为 `C:\Users\Think\.agent\tools\chat-history`（与 `chat_hook.py` 的 `HOME` 默认一致）。
- 在 `_local_remember` 里校验：`PROJ` 下必须存在 `mcp_server.py`，否则**抛错并记 ERROR 日志**，绝不静默 import 到另一个版本。

### c. 截断上限提高（必做）

`chat_hook.py:177` 与 `trace_split.py:75` 的 `[:300]` → `[:2000]`（DB 字段无长度限制；2000 能覆盖绝大多数命令）。若你希望完全不截断也可以，但超长内容会让行很脏。

---

## 5. 待确认

1. **(a)** 是否只要「去噪 + 窗口回退」，不追求子代理工具调用入库？（建议：是）
2. **(c)** 截断上限设多少？（建议 2000）
3. **(b)** "找不到 `mcp_server.py` 就报错"是否可接受？（建议：可接受，宁可失败也不要写错库）

确认后按 1.1/1.2 同样流程：改代码 → 补测试 → 全量跑 → 备份 → 部署安装版 → 重启验证。

**用户决定（2026-09-08）**：(a) 去噪 + 窗口回退，子代理记为已知限制；(c) **完全不截断**；(b) 找不到 `mcp_server.py` **直接报错**。

---

## 6. 实施记录（2026-09-08）

| 项 | 内容 |
|---|---|
| 改动文件 | `.agent/hooks/chat_hook.py`（新增 `_scan_rollout`；`_mid_from_rollout` 改返回 `(text, reason)`；尾部窗口未命中→全文件回退；日志按 reason 分级，`no_text`/`no_cid` 不再记日志；去除 `[:300]`）<br>`.agent/hooks/chat_worker.py`（新增 `HOME`，默认值与 `chat_hook.HOME` 对齐为安装版；`_local_remember` 增加 `mcp_server.py` 存在性校验，缺失即抛错）<br>`trace_split.py`（去除 `[:300]`，同步部署安装版） |
| reason 契约 | `ok` / `no_text`（行在、无前言，正常）/ `not_found`（窗口+全文件都没匹配到，行已被 ZCode 裁剪）/ `file_missing` / `no_cid` / `error`；只有后三类（除 no_cid）记 WARN |
| 验证（一次性脚本 `.agent/temp/verify_hooks_13.py`，用测试队列 + patch Popen，零副作用） | 8/8 通过：reason 契约 5 例、尾部窗口未命中→全文件回退、端到端长命令 1011 字未截断、确认走测试队列 |
| worker 守卫验证 | `CHAT_HISTORY_HOME` 指向无 `mcp_server.py` 的临时目录 → 抛 `RuntimeError`，拒绝本地兜底 |
| 默认值核对 | 清空 env 后 `chat_worker.PROJ == chat_hook.HOME == C:\Users\Think\.agent\tools\chat-history`，`CHAT_DB` 一致 |
| 全量测试 | **161 passed / 0 failed / 1 skipped** |
| 生产验证 | 改动后 21:07 写入的 tool 行长度 784/738/771 字（改前有正好 300 字的截断行）；`chat_hook.log` 在 21:05:46（新代码落地前 6 秒）后再无 `mid 未取到`，也无 `mid 取回异常` |
| 备份 | `.agent/backups/chat-history-hooks-13-20260908-210700/`（`chat_hook.py.orig`、`chat_worker.py.orig`（反推重建，diff 核对仅本次 3 处）、`trace_split.py.orig`） |
| 已知限制 | 子代理内部工具调用**不触发钩子**（客户端行为，matcher 已是 `.*`），整条 tool/mid 记录都没有；钩子侧无法补，需换数据源（`trace_split`）才能补录 |
| 环境事实 | ZCode 会把 rollout 裁成滚动窗口（主会话实测仅保留 66 行/约 24 MB/最近 8 分钟，子代理文件也会被删）；payload 含 `transcriptPath` 等 21 个字段 |
