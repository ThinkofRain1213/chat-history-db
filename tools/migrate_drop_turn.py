# -*- coding: utf-8 -*-
"""去掉 turn 列：把老数据的序号重排进 round/step，再删列（路线 A「语义重建」）。

背景与决策：docs/design/DESIGN_1.7_DROP_TURN.md。核心事实：
- 老数据（round=0/step=0）的唯一序号是 turn，占全库 86%；不重排就删列 = 这些行失去位置信息。
- 重排规则与线上 1.1 规则完全一致：用户消息开新轮（round+1, step=0），agent 消息归属
  最后一个用户轮次（step+1）。按 (turn, time, 物理顺序) 排序后逐行套用，因此**顺序保持不变**。
- 重排后 (session_id, round, step) 全库唯一（顺序赋值保证），round>=1。

顺序要求：**先停掉写入进程（MCP）再执行**。本脚本会 drop + 重建表，写进程持有旧 schema 会失败。
执行前请先 `tools/backup.py backup` 或整目录备份——`drop_columns` 不可逆，回滚只能靠备份。

用法：
    python tools/migrate_drop_turn.py --db <chat.db> --dry-run   # 只报告，不写
    python tools/migrate_drop_turn.py --db <chat.db> --yes       # 执行
"""
import argparse
import collections
import os
import sys
import time
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

import lancedb  # noqa: E402
from lancedb.index import FTS  # noqa: E402

import config  # noqa: E402
from db import Msg  # noqa: E402

_TABLES = (config.TABLE, config.ARCHIVE_TABLE)
_IMMUTABLE = ("session_id", "session_title", "kind", "time", "text")


def _read_all(tbl) -> list[dict]:
    """全列读出，保留物理顺序（index 即写入顺序，作为 turn 相同时的兜底次序）。"""
    rows = []
    for batch in tbl.search().to_batches(batch_size=2000):
        rows.extend(batch.to_pylist())
    for i, r in enumerate(rows):
        r["_idx"] = i
    return rows


def _renumber(rows: list[dict]) -> tuple[list[dict], dict]:
    """按会话重排 round/step。返回 (新行列表, 统计)。顺序与 (turn,time,idx) 一致。"""
    per = collections.defaultdict(list)
    for r in rows:
        per[r["session_id"]].append(r)
    out = []
    for sid, rs in per.items():
        rs.sort(key=lambda r: (int(r["turn"]), str(r["time"]), int(r["_idx"])))
        cur_round, cur_step = 0, 0
        for r in rs:
            if r["kind"] == "user":
                cur_round += 1
                cur_step = 0
            else:
                cur_step += 1
            r = dict(r)
            r["round"], r["step"] = cur_round, cur_step
            out.append(r)
    return out, {"sessions": len(per), "rows": len(out)}


def _validate(old: list[dict], new: list[dict]) -> dict:
    """重排的正确性检查；任何一条不成立都抛异常（宁可不迁移）。"""
    if len(old) != len(new):
        raise RuntimeError(f"行数不一致：{len(old)} -> {len(new)}")

    old_by_idx = {r["_idx"]: r for r in old}
    new_by_idx = {r["_idx"]: r for r in new}
    if set(old_by_idx) != set(new_by_idx):
        raise RuntimeError("行集合发生变化")

    # 1) 除 round/step/turn 外，所有字段原样
    for i, o in old_by_idx.items():
        n = new_by_idx[i]
        for f in _IMMUTABLE:
            if o.get(f) != n.get(f):
                raise RuntimeError(f"字段 {f} 在行 {i} 上被改动")
        if o.get("vector") != n.get("vector"):
            raise RuntimeError(f"行 {i} 的 vector 被改动")

    # 2) round>=1 且 (session, round, step) 唯一
    bad_round = [r["_idx"] for r in new if int(r["round"]) < 1]
    if bad_round:
        raise RuntimeError(f"{len(bad_round)} 行 round<1（首行不是 user？）示例 {bad_round[:5]}")
    c = collections.Counter((r["session_id"], int(r["round"]), int(r["step"])) for r in new)
    dups = {k: v for k, v in c.items() if v > 1}
    if dups:
        raise RuntimeError(f"(session, round, step) 仍有 {len(dups)} 组重复：{list(dups)[:3]}")

    # 3) 顺序保持：每会话按 (round,step) 排序 == 按 (turn,time,idx) 排序
    def key_old(r):
        return (int(r["turn"]), str(r["time"]), int(r["_idx"]))

    def key_new(r):
        return (int(r["round"]), int(r["step"]))

    per_old = collections.defaultdict(list)
    per_new = collections.defaultdict(list)
    for r in old:
        per_old[r["session_id"]].append(r)
    for r in new:
        per_new[r["session_id"]].append(r)
    for sid in per_old:
        a = [r["_idx"] for r in sorted(per_old[sid], key=key_old)]
        b = [r["_idx"] for r in sorted(per_new[sid], key=key_new)]
        if a != b:
            raise RuntimeError(f"会话 {sid} 重排后顺序变了")

    return {"rows": len(new), "sessions": len(per_new),
            "max_round": max(int(r["round"]) for r in new),
            "dup_turn_groups": len({k: v for k, v in collections.Counter(
                (r["session_id"], int(r["turn"])) for r in old).items() if v > 1})}


def _apply(db, table: str, new_rows: list[dict]) -> None:
    """drop 旧表 → 用新 schema 重建 → 建 FTS。不可逆，调用前必须有备份。"""
    rows = [{k: v for k, v in r.items() if k not in ("turn", "_idx")} for r in new_rows]
    db.drop_table(table)
    tbl = db.create_table(table, data=rows, schema=Msg)
    tbl.create_index("text", config=FTS(base_tokenizer="icu"))


def _migrate_table(db, table: str, dry_run: bool) -> dict:
    try:
        tbl = db.open_table(table)
    except Exception:
        return {"table": table, "skipped": "表不存在"}
    names = [f.name for f in tbl.schema]
    if "turn" not in names:
        return {"table": table, "skipped": "已无 turn 列"}
    old = _read_all(tbl)
    if not old:
        if dry_run:
            return {"table": table, "rows": 0, "note": "空表，将直接删列"}
        tbl.drop_columns(["turn"])
        return {"table": table, "rows": 0, "action": "drop_columns"}
    new, stats = _renumber(old)
    report = _validate(old, new)
    report["table"] = table
    report["action"] = "dry-run" if dry_run else "rebuild"
    if dry_run:
        return report
    t0 = time.time()
    _apply(db, table, new)
    report["seconds"] = round(time.time() - t0, 2)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="去 turn：重排 round/step 并删除 turn 列")
    ap.add_argument("--db", default=os.environ.get("CHAT_HISTORY_DB") or str(_PROJECT / "chat.db"),
                    help="库目录路径（默认 CHAT_HISTORY_DB 或项目版 chat.db）")
    ap.add_argument("--dry-run", action="store_true", help="只校验与报告，不写入")
    ap.add_argument("--yes", action="store_true", help="确认执行（不加则等同 dry-run）")
    args = ap.parse_args()
    dry_run = not args.yes or args.dry_run

    db = lancedb.connect(args.db)
    print(f"库：{os.path.abspath(args.db)}")
    print(f"模式：{'dry-run（不写入）' if dry_run else '执行'}")
    for table in _TABLES:
        r = _migrate_table(db, table, dry_run)
        print("  " + ", ".join(f"{k}={v}" for k, v in r.items()))
    if dry_run:
        print("\n未写入。确认无误后加 --yes 执行（执行前请先备份）。")
    else:
        print("\n完成。请用新代码启动服务并跑验证清单（DESIGN_1.7 §4.3）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
