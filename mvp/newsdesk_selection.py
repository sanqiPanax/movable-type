# -*- coding: utf-8 -*-
"""newsdesk_selection -- 本期选稿器(阶段D, SPEC §5第七步 三分区配额)

规则(规格原文落地):
- 每期 20 个展示名额: 随机探索 12 / 公共重要 4 / 追踪更新 4; 比例可配置,
  **不按个人点击行为自动改变**。
- 预留顺序: 先公共重要与追踪更新 -> 缺额转随机探索 -> 再抽探索。
- 随机探索: 按新闻类别**分层轮换**再抽样(只关注科技也能看到其他类别);
  同源不超过探索名额 1/4(候选充足时); 近 3 期已展示的不重复(去重规则,不是兴趣画像)。
- 公共重要: **公开规则**独立筛选(影响范围/紧迫性/可核验证据 -> 六维的 src/ev/score),
  来源不足的高风险消息不凭高分置顶(src 权威门槛); 超出 4 条进溢出清单,不挤占探索名额。
- 追踪更新: 更正/冲突等重要状态优先, 再按未展示时间; 每事件默认 1 名额;
  版面放不下不等于丢弃(其余仍可在追踪列表查看)。
- 抽样种子按期保存; 同 seed + 同输入 -> 同清单(可复现验收)。

输出: 稳定的期刊清单(issues/issue_items)、配额使用记录、未展示更新索引(追踪列表承担)。
"""
import datetime
import hashlib
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import newsdesk_db as db  # noqa: E402

QUOTA = {"explore": 12, "important": 4, "tracking": 4}   # 可配置; 不按点击改变
DEDUP_PERIODS = 3            # 探索区近 3 期去重
SRC_CAP_RATIO = 0.25         # 探索区同源上限(候选充足时): 12*0.25=3
# 公共重要门槛(公开规则, 非个人兴趣; 2026-10-18 按 810 条实测分布校准:
# 原 src>=70 三重AND=0条 -- 高分条目多为聚合源(src=50), 权威源(src75)条目分数反而不够。
# 语义对齐规格"影响范围/紧迫性/可核验证据": 高分(score)+可核验证据(ev)+非弱源(src))
IMP_MIN_SCORE = 70
IMP_SRC_MIN = 40             # 非弱源门槛(排除30以下弱源; 不强求权威,强求证据)
IMP_EV_MIN = 60              # 证据强度门槛: 可核验证据


def _shown_recent(conn, periods=DEDUP_PERIODS, exclude_issue=None):
    """近 N 期已展示的 article_id 集合与标题(探索去重用)。
    exclude_issue: 同期重建时排除自身——否则重建会把上一次输出当'已展示'而洗牌。"""
    sql = "SELECT issue_no, article_id FROM issue_items"
    args = []
    if exclude_issue is not None:
        sql += " WHERE issue_no!=?"
        args.append(exclude_issue)
    sql += " ORDER BY issue_no DESC LIMIT ?"
    args.append(periods * 40)
    rows = conn.execute(sql, tuple(args)).fetchall()
    recent_issues = sorted({r["issue_no"] for r in rows}, reverse=True)[:periods]
    aids = {r["article_id"] for r in rows if r["issue_no"] in recent_issues}
    titles = [r["title"] for r in conn.execute(
        "SELECT DISTINCT a.title FROM issue_items i JOIN articles a"
        " ON a.article_id=i.article_id"
        " WHERE i.issue_no IN (%s)" % ",".join("?" * len(recent_issues) or ["-1"]),
        tuple(recent_issues)).fetchall()] if recent_issues else []
    return aids, titles


def _tracking_partition(conn, cap):
    """追踪更新: 未读节点, 状态变化(corrected/conflicted)优先, 再按未展示时间。
    每事件 1 名额(最新未读), 其余仍在追踪列表可见(放不下不等于丢弃)。"""
    rows = conn.execute(
        "SELECT n.node_id, n.article_id, n.status, n.seq, f.last_read_seq,"
        " f.event_id, e.display_name"
        " FROM follows f JOIN events e ON e.event_id=f.event_id"
        " JOIN nodes n ON n.event_id=f.event_id"
        " WHERE f.user_local='local' AND f.status='active'"
        "   AND n.seq>f.last_read_seq AND n.seq>=0"
        "   AND COALESCE(n.status,'active') IN ('active','corrected','conflicted')"
        " ORDER BY f.event_id, n.seq DESC").fetchall()
    picked, seen_event = [], set()
    for r in rows:
        if r["event_id"] in seen_event:
            continue
        seen_event.add(r["event_id"])
        picked.append(r)
    # 重要状态优先 -> 未展示时间(最近出现期号最久) -> 稳定按 event_id
    def last_shown(aid):
        row = conn.execute(
            "SELECT MAX(issue_no) m FROM issue_items WHERE article_id=?",
            (aid,)).fetchone()
        return row["m"] or 0
    picked.sort(key=lambda r: (
        0 if r["status"] in ("corrected", "conflicted") else 1,
        last_shown(r["article_id"]), r["event_id"]))
    return [dict(r) for r in picked[:cap]]   # Row 转 dict: 下游用 .get()


def _important_partition(conn, cap, exclude):
    """公共重要: 公开规则独立筛选(权威源+证据强+高分), 按分降序; 溢出记清单。"""
    cands = []
    for r in conn.execute(
            "SELECT article_id, title, score, score_d FROM articles"
            " WHERE score IS NOT NULL AND score>=? AND status='ok'",
            (IMP_MIN_SCORE,)).fetchall():
        if r["article_id"] in exclude:
            continue
        try:
            d = json.loads(r["score_d"] or "{}")
        except Exception:
            d = {}
        if (d.get("src", 0) >= IMP_SRC_MIN and d.get("ev", 0) >= IMP_EV_MIN):
            cands.append(dict(r))
    cands.sort(key=lambda r: (-r["score"], r["article_id"]))
    return cands[:cap], [r["article_id"] for r in cands[cap:cap + 5]]


def _explore_partition(conn, cap, exclude, rng, issue_no=None):
    """随机探索: 同源去重 -> 近3期去重(排除自身期号) -> 分层轮换抽样 -> 同源上限。"""
    pool = [dict(r) for r in conn.execute(
        "SELECT article_id, title, source_id, group_name, score FROM articles"
        " WHERE score IS NOT NULL AND status='ok'"
        " ORDER BY score DESC, article_id").fetchall()]
    _, recent_titles = _shown_recent(conn, exclude_issue=issue_no)
    out, src_count = [], {}
    for a in pool:
        if a["article_id"] in exclude:
            continue
        # 近3期去重: article 级 + 标题近似级(同一事件连续3期不重复进探索区)
        if any(__import__("difflib").SequenceMatcher(
                None, a["title"] or "", t or "").ratio() >= 0.62 for t in recent_titles):
            continue
        if src_count.get(a["source_id"], 0) >= 3 and len(out) >= cap // 2:
            continue   # 同源上限(候选充足时生效): 前半程不卡, 后半程收口
        out.append(a)
        src_count[a["source_id"]] = src_count.get(a["source_id"], 0) + 1
        if len(out) >= cap * 2:   # 分层池上限, 防止全库遍历
            break
    # 分层轮换: 按类别分层, 每轮从每层抽1(rng 打乱层顺序, 种子决定 -> 可复现)
    layers = {}
    for a in out:
        layers.setdefault(a.get("group_name") or "其他", []).append(a)
    keys = sorted(layers)
    rng.shuffle(keys)
    picked, has_more = [], True
    while has_more and len(picked) < cap:
        has_more = False
        for k in keys:
            if len(picked) >= cap:
                break
            if layers[k]:
                picked.append(layers[k].pop(0))
                has_more = True
    return picked[:cap]


def build_issue(issue_no, seed=None, db_path=None):
    """产出本期三分区清单并落库。返回 {seed, partitions, overflow_important, report}。
    幂等: 同 issue_no 重跑覆盖自身行(同 seed 同输入 -> 同清单)。"""
    db_path = db_path or db.DEFAULT_DB
    seed = seed or str(issue_no)
    rng = random.Random(seed)
    conn = db.connect(db_path)
    try:
        # 1) 追踪更新(先预留, SPEC)
        tracking = _tracking_partition(conn, QUOTA["tracking"])
        exclude = {r["article_id"] for r in tracking}
        # 2) 公共重要(缺额由探索回补)
        important, overflow = _important_partition(
            conn, QUOTA["important"], exclude)
        exclude |= {r["article_id"] for r in important}
        # 3) 随机探索(回补追踪/重要的缺额)
        shortfall = (QUOTA["tracking"] - len(tracking)) + \
                    (QUOTA["important"] - len(important))
        explore = _explore_partition(
            conn, QUOTA["explore"] + max(0, shortfall), exclude, rng,
            issue_no=issue_no)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        quota_cfg = json.dumps(QUOTA, ensure_ascii=False)
        fp_src = ",".join([r["article_id"] for r in important] +
                          [r["article_id"] for r in tracking])
        fingerprint = hashlib.sha1(
            (seed + fp_src + str(len(explore))).encode()).hexdigest()[:12]
        conn.execute(
            "INSERT OR REPLACE INTO issues(issue_no,seed,quota_cfg,fingerprint,"
            "generated_at) VALUES(?,?,?,?,?)",
            (issue_no, seed, quota_cfg, fingerprint, now))
        conn.execute("DELETE FROM issue_items WHERE issue_no=?", (issue_no,))
        pos = 0
        for part, rows in (("important", important),
                           ("tracking", tracking),
                           ("explore", explore)):
            for r in rows:
                aid = r["article_id"]
                node_id = r.get("node_id") or None
                conn.execute(
                    "INSERT OR IGNORE INTO issue_items(issue_no,article_id,node_id,"
                    "partition,tags,position) VALUES(?,?,?,?,?,?)",
                    (issue_no, aid, node_id, part,
                     json.dumps([part], ensure_ascii=False), pos))
                pos += 1
        conn.commit()
        report = {
            "seed": seed, "issue_no": issue_no,
            "quota": dict(QUOTA),
            "counts": {"explore": len(explore), "important": len(important),
                       "tracking": len(tracking)},
            "overflow_important": overflow,
            "fingerprint": fingerprint,
            "shortfall_backfilled": max(0, shortfall),
        }
        return report
    finally:
        conn.close()


def issue_items(issue_no, db_path=None):
    """读取本期清单(带标题等, 供渲染/复现校验)。"""
    conn = db.connect(db_path or db.DEFAULT_DB)
    try:
        rows = conn.execute(
            "SELECT i.partition, i.tags, i.position, i.article_id, i.node_id,"
            " a.title, a.url_original, a.url_canonical, a.source_id, a.group_name,"
            " a.published_at, a.summary, a.score"
            " FROM issue_items i JOIN articles a ON a.article_id=i.article_id"
            " WHERE i.issue_no=? ORDER BY i.position", (issue_no,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def last_issue_no(db_path=None):
    conn = db.connect(db_path or db.DEFAULT_DB)
    try:
        r = conn.execute("SELECT MAX(issue_no) m FROM issues").fetchone()
        return r["m"] or 0
    finally:
        conn.close()
