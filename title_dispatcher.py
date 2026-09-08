# -*- coding: utf-8 -*-
"""分流程序（a）：纯编排，按 a/b.json/c/d 架构。

- b.json = title_cache（d 拥有：读写 + 查 ZCode）。
- c      = title_validate.check：校验（读源+比对），返回 (state, source)。
- d      = title_cache：get / ids_for_title / set_id_title / learn_title（+ fetch 读源）。

a 流程：读 b（d.get / d.ids_for_title）→ 拉起 c（kind, cached, key）
  ├─ 0 → 报错（id 柔化 ""；title 抛 SessionNotFound；current 抛 InvalidInput）
  ├─ 1 命中且对齐 → 直接用
  └─ 命中且2 / 未命中且1 → 用 c 带回的 source 调 d 写（set_id_title / learn_title）→ 返回
"""
from errors import InvalidInput
import title_validate
import title_cache


class SessionNotFound(ValueError):
    """session 值不是标准 id 且按标题匹配不到任何会话。"""


class AmbiguousTitle(ValueError):
    """session 值按标题匹配到多个会话（重名），需用会话 id 区分。"""


def get_title(session_id: str) -> str:
    """正查 id → 标题（a 纯编排：读 b → 拉起 c → 0/1 直接用 / 2 或 miss1 调 d 写）。"""
    cached = title_cache.get(session_id)                              # 读 b（未命中=None）
    try:
        state, source = title_validate.check("id", cached, session_id)  # 拉起 c（读源+比对）
    except Exception:
        return cached if cached is not None else ""                    # c 读源失败 → 回落缓存
    if state == 0:                                                     # 0 不存在 → 柔化空标题
        return ""
    if state == 1 and cached is not None:                              # 1 命中且对齐 → 直接用
        return cached
    # state==2 或 未命中且1（source 非 None）→ 调 d 写缓存并返回最新
    src = source if source is not None else ""
    title_cache.set_id_title(session_id, src)                          # d 写
    return src


def resolve_session(value: str | None, current_session_id: str | None = None) -> str | None:
    """反查 title → 会话 id（a 纯编排：读 b → 拉起 c → 0/1 直接用 / 2 或 miss1 调 d 写）。"""
    value = (value or "").strip()
    if not value:
        return None
    if value == "current":                                             # 0 非法：current 无上下文
        if current_session_id is None:
            raise InvalidInput("current 需要当前会话上下文")
        return current_session_id
    if value.startswith("sess_"):                                      # 已是 id → 直接返回
        return value

    cached = title_cache.ids_for_title(value)                          # 读 b 反向推导（未命中=None）
    try:
        state, source = title_validate.check("title", cached, value)   # 拉起 c
    except Exception:
        if cached is None:
            raise
        return _serve_reverse(value, cached)                           # c 读源失败 → 回落缓存
    if state == 0:                                                     # 0 不存在 → SessionNotFound
        return _serve_reverse(value, [])
    if state == 1 and cached is not None:                              # 1 命中且对齐 → 直接用
        return _serve_reverse(value, cached)
    # state==2 或 未命中且1 → 调 d 学进 (id -> title) 并返回
    learned = list(source) if source else []
    title_cache.learn_title(learned, value)                            # d 写
    return _serve_reverse(value, learned)


def _serve_reverse(value: str, ids: list[str]) -> str:
    """0 个 → SessionNotFound；多个 → AmbiguousTitle；1 个 → 返回。"""
    if len(ids) == 0:
        raise SessionNotFound(f"会话不存在: {value}")
    if len(ids) > 1:
        raise AmbiguousTitle(f"标题重名，请用会话id: {value}")
    return ids[0]
