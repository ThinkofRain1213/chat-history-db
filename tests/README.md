# 基础自动化测试

使用 Python 标准库 `unittest` 和 `unittest.mock`，无需新增测试框架依赖。请使用项目 `.venv`，其中已有 LanceDB、PyArrow、MCP 等运行依赖。

## 运行

在项目根目录的 PowerShell 7 中执行：

```powershell
& .\.venv\Scripts\python.exe -B -m unittest discover -s tests -t . -v
```

默认不加载真实 ONNX 模型。真实模型冒烟需要本地模型文件，显式运行：

```powershell
$previous = $env:CHAT_HISTORY_REAL_MODELS
try {
    $env:CHAT_HISTORY_REAL_MODELS = '1'
    & .\.venv\Scripts\python.exe -B -m unittest tests.test_storage.RealModelTests -v
} finally {
    $env:CHAT_HISTORY_REAL_MODELS = $previous
}
```

默认测试退出码为 **0**。

## 文件与范围

- `support.py`：隔离临时路径、恢复环境变量和模块状态、可重复的模型替代实现；阻止测试通过 LanceDB connect 访问当前用例目录以外的数据库。
- `test_logic.py`：固定时钟的日期/时间解析、kind 集合、会话解析、MCP 元数据提取、现有错误码。
- `test_storage.py`：真实临时 LanceDB 的建表和追加、schema 自检、启动调用顺序、round/step、recent 过滤/排序、会话统计、混合检索；包含一个显式启用的真实模型测试。
- `test_adapters.py`：临时 SQLite 会话拆分/标题缓存、随机端口上的真实 HTTP 请求、MCP 工具包装函数参数转发及错误返回。
- `test_errors.py`：错误码分类与 `_error_msg` 契约、`error_codes.json` 与 `ERROR_REASONS` 键集合一致性、日志不含 payload、读/写失败分类、模型加载失败分类。
- `test_archive.py`：归档/恢复/删除、归档幂等、`dry_run`、两阶段删除闸门、标题缓存清理。
- `test_backup.py`：`tools/backup.py` 的 backup / list / restore / vacuum / reindex / verify，以及 `_guard` 前置检查（MCP 在跑 / 队列积压时拒绝，`--force` 跳过）。
- `test_embedding.py`：bge-m3 输出 NaN/Inf 时的清洗与计数。
- `test_http_server.py`：端口占用探测、后台重试接管、绑定失败（全 mock）。
- `test_maintenance.py`：清理阈值、禁用开关、重入、安全参数、后台线程。
- `test_round_step.py`：按会话文件锁、锁目录/锁文件清理、锁内重开表（含旧快照反证）、round/step 推导。

默认数据库集成测试使用真实 LanceDB 和 FTS/RRF 查询，但替换嵌入计算和重排评分，验证的是查询逻辑，不是语义质量。真实模型测试覆盖 ONNX 嵌入及重排路径，但只有小规模冒烟数据，不能替代召回质量评测。

MCP 包装测试捕获工具注册函数后直接调用，不覆盖真实 stdio 协议、客户端握手或 ZCode hook/worker。HTTP 测试会真正启动本地服务器，但使用系统分配的临时端口，不使用 17891。

## 数据隔离

- 每个需要存储的用例使用 `~/.agent/temp/chat-history-tests-*` 下新建的唯一临时目录。
- `CHAT_HISTORY_DB`、标题缓存、ZCode SQLite 路径均重定向；`trace_split` 的硬编码常量也在测试内替换。
- 用例结束恢复环境变量和模块全局状态，关闭 SQLite/HTTP 资源后清理本用例目录。
- 默认模型加载函数被阻断，意外加载真实模型会失败；显式模型测试仅使用本地文件。
- 不调用运行中的 chat-history MCP，不读写项目正式 `chat.db`，不导入真实历史记录。
- 不要并发运行同一 Python 进程中的用例；测试通过 patch 修改模块级状态。不同进程使用独立目录。

## 当前结果（2026-09-08）

`python -m unittest discover -s tests` → **182 项：181 通过、0 失败、1 跳过**，约 21 秒。跳过项是需 `CHAT_HISTORY_REAL_MODELS=1` 才加载真实模型的 `RealModelTests`。

HTTP 相关用例（`HealthTests` / `HttpBodyLimitTests` / `test_adapters.HttpTests`）按类共用一个临时端口 server（`setUpClass`），不再逐用例起停。

历史说明：2026-09-07 曾把两个真实缺陷保留为普通失败测试（`resolve_session('current', None)` 扩大查询范围、乱序导入时 `first_time` 不准）。两项随后都已修复（`title_dispatcher.resolve_session` 对无上下文的 `current` 抛 `InvalidInput`；`list_sessions` 在循环内同时更新 `first_time`），对应用例现在自然通过，退出码恢复为 0。

## 后续维护

- 新增用例必须使用隔离路径（见 `support.py`）。
- 不要为了得到绿色结果删除断言，或把真实缺陷默认标为跳过；缺陷修好后让原用例自然转为通过。
- 修改公共错误码时同步 `error_codes.json` 与 `errors.ERROR_REASONS`——两者不一致会被 `test_errors.py` 直接判失败。
