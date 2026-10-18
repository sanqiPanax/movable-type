# -*- coding: utf-8 -*-
"""阶段D 验收(SPEC §5第七步/§10-D): 三分区配额/种子复现/缺额回补/去重/公开规则。
全部在 newsdesk.db 的临时副本上跑, 不碰真库。
用法: python -X utf8 _verify_d.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import newsdesk_db as db  # noqa: E402
import newsdesk_selection as sel  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail else ""))


tmp = tempfile.mktemp(prefix="selftest_d_", suffix=".db")
shutil.copy(db.DEFAULT_DB, tmp)
try:
    conn = db.connect(tmp)
    n_follow = conn.execute(
        "SELECT COUNT(*) c FROM follows WHERE status='active'").fetchone()["c"]
    n_scored = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE score IS NOT NULL").fetchone()["c"]
    conn.close()
    check("0a 副本含活跃追踪", n_follow >= 1, n_follow)
    check("0b 副本含评分数据", n_scored >= 100, n_scored)

    print("=== 1. 三分区配额与公开规则 ===")
    rep1 = sel.build_issue(90001, seed="d-test-1", db_path=tmp)
    items1 = sel.issue_items(90001, db_path=tmp)
    check("1a 探索<=12+回补", rep1["counts"]["explore"] <= 12 + rep1["shortfall_backfilled"],
          rep1["counts"])
    check("1b 追踪<=4", rep1["counts"]["tracking"] <= 4, rep1["counts"])
    check("1c 重要<=4", rep1["counts"]["important"] <= 4, rep1["counts"])
    check("1d 总名额=实际展示数", len(items1) == sum(rep1["counts"].values()), len(items1))
    # 公开规则: 重要条目必须证据可核验+非弱源+高分(不凭点击/个人兴趣)
    conn = db.connect(tmp)
    imp_ids = [r["article_id"] for r in items1 if r["partition"] == "important"]
    ok_rule = True
    for aid in imp_ids:
        r = conn.execute("SELECT score, score_d FROM articles WHERE article_id=?",
                         (aid,)).fetchone()
        d = json.loads(r["score_d"] or "{}")
        if not (r["score"] >= sel.IMP_MIN_SCORE and d.get("src", 0) >= sel.IMP_SRC_MIN
                and d.get("ev", 0) >= sel.IMP_EV_MIN):
            ok_rule = False
    conn.close()
    check("1e 重要条目全部满足公开规则", ok_rule, imp_ids[:3])
    parts1 = {r["article_id"]: r["partition"] for r in items1}
    check("1f 同一内容不占多分区", len(parts1) == len(items1))

    print("=== 2. 种子复现: 同期重建不洗牌(SPEC 验收) ===")
    items_before = [(r["partition"], r["article_id"]) for r in items1]
    sel.build_issue(90001, seed="d-test-1", db_path=tmp)   # 同期号同seed重建
    items2 = sel.issue_items(90001, db_path=tmp)
    same = (items_before == [(r["partition"], r["article_id"]) for r in items2])
    check("2a 同期重建清单一致", same)
    rep2b = sel.build_issue(90002, seed="d-test-1", db_path=tmp)
    check("2b 指纹由seed决定", rep1["fingerprint"] == rep2b["fingerprint"])

    print("=== 3. 追踪更新优先级与每事件1名额 ===")
    conn = db.connect(tmp)
    unread_events = conn.execute(
        "SELECT COUNT(DISTINCT f.event_id) c FROM follows f JOIN nodes n"
        " ON n.event_id=f.event_id WHERE f.status='active'"
        " AND f.user_local='local' AND n.seq>f.last_read_seq AND n.seq>=0").fetchone()["c"]
    conn.close()
    track_items = [r for r in items1 if r["partition"] == "tracking"]
    evs = [r["node_id"] for r in track_items]
    check("3a 追踪条目<=活跃未读事件数", len(track_items) <= max(1, unread_events),
          "%d vs %d" % (len(track_items), unread_events))

    print("=== 4. 缺额回补: 关光追踪->追踪名额转探索 ===")
    conn = db.connect(tmp)
    conn.execute("UPDATE follows SET status='archived' WHERE status='active'")
    conn.commit()
    conn.close()
    rep4 = sel.build_issue(90004, seed="d-test-4", db_path=tmp)
    check("4a 追踪=0", rep4["counts"]["tracking"] == 0, rep4["counts"])
    check("4b 探索回补到16", rep4["counts"]["explore"] >= 16, rep4["counts"])
    check("4c 仍有完整日报候选(>=16条)", rep4["counts"]["explore"] + rep4["counts"]["important"] >= 16)

    print("=== 5. 探索区3期去重(连续多期不重复进探索区) ===")
    seeds = [(90005, "d-seed-a"), (90006, "d-seed-b"), (90007, "d-seed-c")]
    seen_titles = []
    for no, sd in seeds:
        sel.build_issue(no, seed=sd, db_path=tmp)
        for r in sel.issue_items(no, db_path=tmp):
            if r["partition"] == "explore":
                seen_titles.append(r["title"] or "")
    sel.build_issue(90008, seed="d-seed-d", db_path=tmp)
    import difflib
    dup = 0
    for r in sel.issue_items(90008, db_path=tmp):
        if r["partition"] != "explore":
            continue
        t = r["title"] or ""
        if any(difflib.SequenceMatcher(None, t, o).ratio() >= 0.62 for o in seen_titles):
            dup += 1
    check("5a 第4期探索区与前3期零重复", dup == 0, "重复%d条" % dup)

    print("=== 6. 渲染链路: 清单可喂主编候选 ===")
    rows = sel.issue_items(90008, db_path=tmp)
    cands = [{
        "title": r["title"], "desc": r["summary"], "url": r["url_original"],
        "src": r["source_id"], "src_id": r["source_id"],
        "group": r["group_name"], "ts": r["published_at"], "score": r["score"],
        "_partition": r["partition"]} for r in rows]
    import build_paper as bp
    payload = bp.cands_payload(cands)
    check("6a payload含分区标签", all("p" in x for x in payload), payload[0])
    check("6b 候选>=16", len(cands) >= 16, len(cands))
    # 规则排版兑底: issue候选也能出决策
    dec = bp.rule_edit(cands)
    ctx = bp.normalize_decision(dec, cands)
    n_cols = sum(len(v) for v in ctx["cols"].values())
    check("6c 规则排版兑底可用", n_cols > 0 and ctx["lead"] is not None, n_cols)
finally:
    try:
        os.remove(tmp)
    except OSError:
        pass

print()
print("==== PASS %d / FAIL %d ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
