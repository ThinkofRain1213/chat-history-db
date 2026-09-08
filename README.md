# chat-history 对话历史库

结构化对话历史存储 + 强召回，以 MCP 工具形式提供给 Agent。

- **存储**：LanceDB（本地嵌入式，vector + 全文 + 元数据）
- **嵌入**：bge-m3（ONNX，本地推理）
- **重排**：bge-reranker-v2-m3（cross-encoder）
- **召回**：混合检索（BM25 精确词 + 向量语义）→ RRF 融合 → 跨编码器重排

## 功能（MCP 工具）

| 工具 | 说明 |
|---|---|
| `remember` | 存一条消息（会话/标题/时间/轮次自动，入库自动算向量） |
| `recall` | 语义召回（混合检索 + 重排，可按时间/会话/角色过滤） |
| `recent` | 最近消息，时间倒序 |
| `list_sessions` | 列出会话及条数（`source` 选活跃表 / 归档表） |
| `session_admin` | 会话管理：`archive` 归档 / `restore` 恢复 / `delete` 永久删除（两阶段确认） |

`recall` / `recent` / `list_sessions` 的 `source` 支持：`messages`（默认，活跃表）、`archive`（归档表）。

`recall` / `recent` 的 `session` 支持：空=不限、`current`=当前会话、`sess_xxx`=指定、其它=按标题匹配（匹配不到返回 `E_SESSIONNOTFOUND`，重名返回 `E_AMBIGUOUSTITLE`）。

## 安装

```bash
pip install -r requirements.txt
```

模型文件默认可放在 `./models/` 下，或用环境变量覆盖：
- `CHAT_HISTORY_MODEL_DIR`（默认 `models/bge-m3-onnx`）
- `CHAT_HISTORY_RERANK_DIR`（默认 `models/bge-reranker-v2-m3-onnx`）

## 运行（MCP stdio）

```bash
python mcp_server.py
```

作为 MCP server 通过 stdio 接入；同时在 **127.0.0.1:17891** 开一个本地 HTTP 端点供 hooks 复用本进程模型：
- `POST /remember`：入库（复用 MCP 进程内已加载的模型）
- `GET /health`：存活探测

端口随 MCP 进程退出而关闭。可用 `CHAT_HISTORY_PORT` 改端口。

## 会话标题

`title_dispatcher` / `title_cache` / `title_validate` 读取宿主（ZCode）的会话标题，用于按标题解析会话与回显。

## 运维

- **启动自动清理**：MCP 启动后按 `chat.db/messages.lance/_versions` 的垃圾体积阈值（默认 **10 MiB**）自动回收旧版本、合并碎片并更新 FTS 索引。日志见 `~/.agent/hooks/chat_maintenance.log`；`CHAT_HISTORY_GC_MB` 调阈值，`CHAT_HISTORY_GC=0` 关闭。
- **生产库不在本目录**：本仓库的 `chat.db/` 是空库骨架（只有空的 `messages.lance`），运行中的 MCP 用的是安装版目录 `C:\Users\Think\.agent\tools\chat-history\chat.db`。`tools/backup.py` 按当前工作目录解析库路径，**在项目版目录直接跑会操作这个空库**——治理/备份生产库请在安装版目录执行，或先设 `CHAT_HISTORY_DB`。
- **备份 / 恢复 / 手动治理**：`python tools/backup.py {backup,list,restore,verify,vacuum,reindex}`，细节见 [`docs/ops/BACKUP.md`](docs/ops/BACKUP.md)。`restore` 与 `vacuum` 一样会拒绝在 MCP 运行中/队列有积压时执行（`--force` 跳过）。
- **测试**：`python -m unittest discover -s tests`。

## 文档

| 位置 | 内容 |
|---|---|
| [`OPEN_ITEMS.md`](OPEN_ITEMS.md) | **唯一权威**的未决项 / 已完成清单——先看这个 |
| [`docs/design/`](docs/design) | 各项改造的设计与实施记录：`DESIGN_1.1_ROUND_STEP`、`1.2_ARCHIVE`、`1.3_HOOKS`、`1.5_WRITE_QUALITY`、`1.6_LOCK_DIR`、`1.7_DROP_TURN` |
| [`docs/migration/`](docs/migration) | 项目版 → 安装版的迁移计划与执行记录 |
| [`docs/ops/`](docs/ops) | 运维手册（`BACKUP.md`：备份 / 恢复 / 治理） |
| [`docs/archive/`](docs/archive) | 已归档的历史规格（`REPAIR_SPEC_20260908.md`，§6 已并入 `OPEN_ITEMS.md`） |

## 说明

- 模型为**外部依赖**（体积大、含许可），源码另行分发；运行时通过环境变量指定模型目录。
- 数据文件 `chat.db` 为 LanceDB 目录，首次写入自动创建。
