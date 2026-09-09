---
name: chat-history
description: >
  用 chat-history MCP 工具组（mcp__chat-history__remember / recall / recent / list_sessions / session_admin）
  存取我们自己的对话历史。当用户提到过往对话（"之前/上次我们说过…"、"找一下…"、"还有印象吗"）、
  想看最近聊了什么、想列出有哪几个会话、想归档/恢复/删除某个会话，或明确要求存一段内容时触发。
  回答关于我们此前讨论的问题前，先当作 ground truth 查它，别靠脑补。
---

# chat-history 用法

我们自研的对话历史库，通过 MCP 暴露 5 个工具（`mcp__chat-history__*`）。底层是
LanceDB（bge-m3 向量 + BM25 + RRF + bge-reranker 跨编码器重排）。它是"我们自己的长期
记忆"，独立于当前临时上下文——**回答涉及"我们之前说过什么"时，先从这查，别靠脑补。**

数据分两张表：`messages`（活跃表，默认）与 `messages_archive`（归档表）。`recall` /
`recent` / `list_sessions` 用 `source` 参数选表，**一次只查一张，不做跨表混合检索**；
查到空但另一张表有该会话时，返回值里会带一句可操作提示。

## 先选对工具

| 需求 | 工具 | 需要 query | 返回 |
|---|---|---|---|
| 明确存一条内容进历史库 | `remember` | 否 | 成功/失败字符串 |
| "找一下 / 之前我们说过 X / 还有印象吗" | `recall` | **是** | 按相关度排序的若干行 + `score` |
| "最近聊了什么 / 刚才说到哪" | `recent` | 否 | 时间倒序若干行（无 `score`） |
| "有哪些会话 / 列出标题，拿 id" | `list_sessions` | 否 | 会话对象 JSON 数组 |
| 归档 / 恢复 / 永久删除某个会话 | `session_admin` | 否 | 结果字符串（删除是两阶段） |

口诀：**"找特定内容（有目标语义）"→ recall；"看最近的/枚举/按条件筛"→ recent；"列会话清单/取 id"→
list_sessions；"会话管理（归档/恢复/删除）"→ session_admin；"写入"→ remember。** 别拿文案硬塞给
不匹配的工具：`recent` 不接受 query，`recall` 不带 query 就报错；`session_admin` 不做列表——
传 `action='list'` 会报 `E_INVALID`，要列表请用 `list_sessions`。

### recall 的准入门槛（硬规则）

**没有目标语义就别动 `recall`。** 判据：你能用**一句自然语言**说清"要找的那段意思"，
且要的答案是"哪几条最相关"。以下都不是语义检索，各有对应工具，不要拿 `recall` 凑：

- **枚举 / 全量 / 按时间倒序 / 按 `kind`、`session`、`range` 筛** → 用 `recent`。
  例："我改了哪些文件""今天都聊了什么"——这类要的是完整清单，不是相关度排名。
- **列会话清单 / 取 id** → 用 `list_sessions`。
- **精确字符串或正则匹配**（某错误码 / 某路径出现在哪些行）→ **MCP 暂无对应工具**；`recall`
  的 BM25 分支虽能命中，但会被重排打乱、被 `top_k` 截断，且慢，不适合当"扫描"用。

## 各工具签名与要点

### remember — 写入（模型很少直接调）
钩子通常已自动入库，模型一般不需要主动调用；仅在用户**明确要求保存某段内容**、或钩子
未覆盖的场景才用。

签名：`remember(text, session_id=None, kind="final", time=None, session_title=None, round=None, step=None)`
- `text` 必填；`session_id` 缺省时用当前会话；`kind` 默认 `final`；`time/session_title`
  缺省则自动（当前北京时间、按会话查标题）。
- `round`/`step` 一般不用传：用户消息自动开新轮（`round+1`，`step=0`），agent 消息自动归属
  该会话最后一个用户轮次（`step+1`）。只在补录/纠偏时才显式传。
- 会话开头若先有 agent 消息（尚无用户轮次），这些行落在 `round=0`，显示为 `#0.step`。
- 返回 `"成功"` 或 `"失败 [错误码]"`。

### recall — 语义召回（**仅在有目标语义时用**，见上「准入门槛」）
签名：`recall(query, session=None, kind=None, range=None, limit=5, top_k=30, source="messages")`
- `query` 必填，自然语言，越能描述"要找的内容"越准。
- **`limit` = 最终返回条数上限（默认 5）**；想多要就调大，如 `limit=15`。
- **`top_k` = 候选池大小（默认 30）**：先召回最多 30 条交给重排器打分，再按 `limit` 截断。
  **调大 `top_k` 才变慢**（每条候选过 cross-encoder，真实长文本约 0.3s/条，`top_k=30`
  可达 8-10s）；调大 `limit` 不增加耗时。要"更多结果但别太慢"，优先加 `limit`，别加 `top_k`。
- `source`：`"messages"`（默认，活跃表）/ `"archive"`（归档表）。
- 返回每行带 `score`（相关度，可为负，越大越相关）。行格式取决于是否传 `session`：
  - 传了 session：`[#round[.step] | kind | time | score=s] text`
  - 不限会话：`[标题 | session_id | #round[.step] | kind | time | score=s] text`
    （标题为空时退化为 `[session_id | #round[.step] | kind | time | score=s] text`）
  - 查归档表时行尾多一个 `| archive` 标记。
- 示例：`mcp__chat-history__recall(query="我们之前讨论过的模型加载方式", limit=5)`

### recent — 最近消息（时间倒序，无语义）
签名：`recent(limit=None, session=None, kind=None, range=None, source="messages")`
- 不检索，纯按 `time`/`round`/`step` 倒序，用于"刚聊了什么"。
- **`limit` 默认 10**；传 `kind="all"` 时默认 **40**。
- `source` 语义同 recall。返回行无 `score`，行格式与 recall 相同：
  - 传了 session：`[#round[.step] | kind | time] text`
  - 不限会话：`[标题 | session_id | #round[.step] | kind | time] text`

### list_sessions — 会话清单（唯一的列表入口）
签名：`list_sessions(source="messages")`
- `source`：`"messages"`（默认，活跃会话）/ `"archive"`（已归档会话）。
- 返回 JSON 数组，每项含 `session_id / session_title / count / first_time / last_time`
  （`last_time` 倒序）。用于探索有哪些会话、拿 `session_id`、看每个会话标题与条数。

### session_admin — 会话管理（归档 / 恢复 / 删除）
签名：`session_admin(action, session=None, dry_run=False)`
- `action="archive"`：把该会话全部行从活跃表搬进归档表（可恢复）；`dry_run=True` 只统计不写。
- `action="restore"`：从归档表搬回活跃表。
- `action="delete"`：**永久删除，不可恢复**。只能删**已归档**会话——删活跃会话报
  `E_NOTARCHIVED`，要先 `archive`。**两阶段**：第一次调用只登记意向、返回一段含询问工具
  完整参数的文案（按它调用询问工具问用户）；用户确认后在 TTL（默认 60 秒）内**再调一次
  同一 action** 才真正执行。用户取消或超时未确认，就不要再调第二次。
- 除 `archive` 外都需要 `session`（`sess_xxx` 或标题）。

## 关键参数语义（最容易踩的坑）

### session 怎么传
- **不传 / `None`** → 不限（跨所有会话搜索）。
- **`"current"`** → 只看当前会话。
- **`"sess_xxx"`**（`sess_` 前缀）→ 指定会话 id。
- **其他任何文字** → 按**标题精确匹配**会话，可能抛：
  - `失败 [E_SESSIONNOTFOUND]`：无该标题的会话；
  - `失败 [E_AMBIGUOUSTITLE]`：标题重名，需改用会话 id。
- 遇到 `E_AMBIGUOUSTITLE` → 先 `list_sessions` 拿 id，再用 `session=<id>`。
- 一旦传了 `session`，输出行只留 `#round[.step]`，不重复标题/id。

### 标题 ↔ 会话 id 怎么对应查
标题匹配查的是 **ZCode 的 `session` 表**（`title_cache.find_ids_by_title`，`WHERE title=?`
精确匹配；`title_dispatcher` 编排、`title_validate` 比对缓存新鲜度），不是 chat.db 自己的
标题列。两种拿 id 的办法：
- **直接传标题**：`recall/recent` 的 `session` 参数传标题文字，工具自动反解成 id（0 个→
  `E_SESSIONNOTFOUND`，多个→`E_AMBIGUOUSTITLE`，1 个→命中）。前提是标题记得准。
- **先 `list_sessions` 再取 id**（更稳）：`list_sessions` 返回 `session_id` + `session_title`
  对，按标题找到对应项，拿它的 `session_id`（形如 `sess_xxx`）传给 `session=<sess_xxx>`。
  重名时这也是唯一的绕法。
- **从查询结果行里抠 id**：`recall`/`recent` 不限会话时，每行前缀已带
  `| sess_xxx |`，直接抄即可。
顺序：**优先 `list_sessions` 拿准 id 再传**；确信标题唯一且拼写准确时，也可直接传标题省一步。

### kind 怎么传（默认只查 user 和 final）
- **不传 / `None`** → 只看 `{user, final}`（用户消息 + 最终回答）。
- **`"all"`** → 含 `{user, tool, mid, final}`（把中间推理 / 工具输出也搜进来）。
- **多值**：逗号分隔，如 `"user,final"`、`"tool,mid"`。
- 找"用户原话/我们结论"→ 默认即可；找"完整推理链/工具运行结果"→ 用 `kind="all"`。

### range 时间过滤
北京时区，纯时间默认今天：
`'YYYY'`、`'YYYY-MM-DD'`、`'YYYY-MM-DD HH:MM'`、`'HH:MM'`、`'HH:MM-HH:MM'`、
`'MM-DD'`、`'MM-DD HH:MM[:HH:MM]'`、`'09:00-10:00'`、`'08-15 09:00-10:00'`。
`until <= since` 视为跨天；格式错返回 `E_INVALID`。

## 报错怎么处理（错误码）
工具出错时 **wrapper 捕获异常并返回字符串 `"失败 [E_XXX]"`**——你看到的是这个字符串，不是异常。
它不会抛给你，所以要用**结果是否以 `失败 [` 开头**来判断成功/失败。常见错误码查 `error_codes.json`：

| 错误码 | 含义 | 处理 |
|---|---|---|
| `E_INVALID` | 参数不合法 | 检查必填是否缺失、`range` 时间格式、`session` 取值、`source` 是否只传 `messages`/`archive`、`session_admin` 的 action 是否合法 |
| `E_SESSIONNOTFOUND` | `session` 指定的会话不存在（非标准 id 且标题没匹配到） | 核对 session 传的是合法 id 或正确标题；改用 `list_sessions` 拿 id |
| `E_AMBIGUOUSTITLE` | 按标题匹配到多个会话（重名） | 改用会话 id（`sess_xxx`）精确指定 |
| `E_NOTARCHIVED` | 删除目标是活跃会话（还没归档） | 先 `session_admin(action='archive')` 归档，再删除 |
| `E_RUNTIMEERROR` | 数据库/索引运行时错误 | 检查 chat.db 是否存在、messages 表 schema、索引是否可读 |
| 其他 `E_XXX` | 未预期异常 | 看 MCP 服务器 stderr/日志定位；确定后用错误码+原因+解决补录进 `error_codes.json` |

处理口诀：**先看是不是 `失败 [`；是 → 按错误码对上表修参数；`E_AMBIGUOUSTITLE`/`E_SESSIONNOTFOUND`
→ 改用 `list_sessions` 拿准 id 那条路**。

## 使用建议
- **先确认意图再选工具**：用户问"找内容"要给具体 `query`；"看最近"别硬塞 query。
- **默认条数偏少**（recall 5 / recent 10）；用户要"更多/全部"就显式调 `limit`。
- **跨会话搜**别传 `session`；**只看当前会话**传 `session="current"`。
- **查不到不等于没有**：该会话可能已归档——换 `source="archive"` 再查一次（工具也会返回提示）。
- **`score` 是相关度不是置信度**，负数不代表无关，只是相对排序靠后；正数越大越相关。
- 结果行是压缩单行（`[...]` 前缀 + 正文全文不截断），适合直接读出来转发给用户。

## 深入源码（排查异常行为时读）
MCP 工具接线（参数默认值、`source`、两阶段删除闸门）在 `core.py`；数据层（表/schema/过滤/
查询）在 `db.py`；归档/恢复/删除在 `archive.py`；会话标题解析在 `title_dispatcher.py` /
`title_cache.py` / `title_validate.py`；重排与候选截断在 `reranker.py`；嵌入在
`bgem3_embedding.py`；`mcp_server.py` 只是门面入口（模块聚合 + 启动）。
安装版：`C:\Users\Think\.agent\tools\chat-history\`；工程版（改动需手动同步）：
`C:\Users\Think\Desktop\项目\chat-history-db\`。
