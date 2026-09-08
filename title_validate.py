# -*- coding: utf-8 -*-
"""校验分类程序（c）：自己读源（经 d=title_cache 的查询函数）+ 比对缓存，返回 (state, source)。

- 0 = 不存在：源无此项。
- 1 = 存在且对齐：源存在且与缓存一致（命中且不用写），或缓存未命中但源存在。
- 2 = 存在但过期：源与缓存都在但值不一致（不一致算过期）。

返回值：
  (state, source) —— source 仅在「命中且2」或「未命中且1」时非 None（即需要 a 把它写回缓存的最新值）；
  其余情况 source 为 None（无新值可写）。

kind: 'id' | 'title'（决定读源方式：id 查单标题，title 查 id 列表）。
"""
from __future__ import annotations

import title_cache


def _read_source(kind: str, key: str):
    """d 读源：id → 标题(无则 None)；title → id 列表(无则 None)。用模块属性调用以便 patch。"""
    if kind == "id":
        v = title_cache.fetch_from_zcode(key)
        return None if v == "" else v
    ids = list(title_cache.find_ids_by_title(key))
    return ids if ids else None


def classify(cached, source) -> int:
    """纯比对分类（无 I/O）：0 不存在 / 1 对齐 / 2 过期。"""
    if source is None:
        return 0
    if cached is None:
        return 1
    if cached == source:
        return 1
    return 2


def check(kind: str, cached, key: str) -> tuple[int, object | None]:
    """c 校验：读源 + 比对。返回 (state, source)；source 仅在需写回(命中且2 / 未命中且1)时非 None。"""
    source = _read_source(kind, key)
    state = classify(cached, source)
    if state == 0:
        return 0, None
    if state == 2:
        return 2, source          # 命中但不一致 → 带出最新源值
    if cached is None:
        return 1, source          # 未命中但源存在 → 带出源值（学到并写）
    return 1, None                # 命中且对齐 → 不带新值
