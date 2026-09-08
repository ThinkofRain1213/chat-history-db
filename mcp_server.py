# -*- coding: utf-8 -*-
"""MCP 服务入口（门面）：只做启动。

模块归属（不再从本模块 re-export 任何符号——历史上这里导出了 40+ 个名字，导致
「patch 门面不生效」「对外符号面由测试决定」这类陷阱；请直接 import 归属模块）：
- config.py       配置常量
- timeutil.py     时间工具
- db.py           数据层（表模型/连接/校验/查询/过滤）
- core.py         领域逻辑（写/查/列）——不依赖 mcp SDK
- mcp_tools.py    MCP 工具接线（唯一 import mcp.server 的地方）
- http_server.py  本地 HTTP 端点
- maintenance.py  启动期空间治理（按阈值回收旧版本清单）

启动流程里的每一步都经**归属模块**调用（db./http_server./mcp_tools./maintenance.），
这样测试与外部只需 patch 归属模块即可改变行为。
"""
from __future__ import annotations

import core
import db
import http_server
import maintenance
import mcp_tools
from config import ARCHIVE_TABLE
from errors import log_error


def main():
    try:
        db._migrate_messages_schema()  # 老库补 round/step 列（幂等），必须在校验之前
        db._migrate_messages_schema(table=ARCHIVE_TABLE)  # 归档表若已存在，同样补齐
        db._validate_messages_schema()
    except Exception as exc:
        log_error(exc, "startup.schema", core._error_code(exc))
        raise
    http_server._start_http_server()
    maintenance.start_background_maintenance()
    mcp_tools._build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
