# -*- coding: utf-8 -*-
"""阶段C 验收: 关联质量 + 转述合并(SPEC §5第五步 / §15-1)。
回归样例 = 2026-10-17 截图实锤的两条真实混入(COD / Innodata)。
用法: python -X utf8 _verify_c.py   (会插入测试报道,结束后自动清理)
"""
import sys

sys.path.insert(0, r"F:\DoMyProject\魔法日报\mvp")
import newsdesk_db as db  # noqa: E402

EID = "e_024bc4fcdb80fc17"          # 演示事件: GTA6 launch trailer
ORIGIN_TITLE = ("The Grand Theft Auto 6 launch trailer is here")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail else ""))


def insert_art(title, url):
    """模拟一条新入库报道。"""
    canon = db.canonical_url(url)
    aid = db.article_id_for(canon)
    ch = db.content_hash_of(title, "")
    conn = db.connect()
    conn.execute(
        "INSERT OR IGNORE INTO articles(article_id,url_original,url_canonical,source_id,"
        "title,summary,published_at,published_precision,collected_at,group_name,"
        "first_seen_run,current_version,content_hash) VALUES(?,?,?,?,?,'',?,?,?,?,'',1,?)",
        (aid, url, canon, "test_src", title, "2026-10-17T10:00:00+00:00", "minute",
         "2026-10-17T10:00:00+00:00", "测试", ch))
    conn.commit()
    conn.close()
    return aid


def cleanup(aids):
    conn = db.connect()
    for aid in aids:
        nids = [r["node_id"] for r in conn.execute(
            "SELECT node_id FROM nodes WHERE article_id=?", (aid,)).fetchall()]
        for nid in nids:
            conn.execute("DELETE FROM node_evidence WHERE node_id=?", (nid,))
            conn.execute("DELETE FROM nodes WHERE node_id=?", (nid,))
        conn.execute("DELETE FROM node_evidence WHERE article_id=?", (aid,))
        conn.execute("DELETE FROM decisions WHERE article_id=?", (aid,))
        conn.execute("DELETE FROM article_versions WHERE article_id=?", (aid,))
        conn.execute("DELETE FROM articles WHERE article_id=?", (aid,))
    conn.commit()
    conn.close()


def ev_count(nid):
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM node_evidence WHERE node_id=?",
                     (nid,)).fetchone()["c"]
    conn.close()
    return n


def node_state():
    conn = db.connect()
    rows = conn.execute(
        "SELECT node_id, seq FROM nodes WHERE event_id=? ORDER BY seq",
        (EID,)).fetchall()
    ms = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                      (EID,)).fetchone()["max_seq"]
    conn.close()
    return [(r["node_id"], r["seq"]) for r in rows], ms


print("=== 0. 基线(演示事件应已无混入) ===")
nodes0, ms0 = node_state()
check("0a 基线节点数=2(创始+真相关)", len(nodes0) == 2, nodes0)

created = []
try:
    print("=== 1. 转述合并: 同题变体不加节点、只补来源、不加未读 ===")
    retell_id = insert_art(
        "The Grand Theft Auto 6 launch trailer is here, says publisher",
        "https://test.example.com/retell/gta6-trailer?utm_source=x")
    created.append(retell_id)
    r1 = db.scan_progress()   # 全活跃事件扫描
    nodes1, ms1 = node_state()
    check("1a 节点数不变", len(nodes1) == len(nodes0), "%d->%d" % (len(nodes0), len(nodes1)))
    check("1b max_seq 不变(转述不加未读)", ms1 == ms0, "%s->%s" % (ms0, ms1))
    origin_nid = nodes0[0][0]
    check("1c 创始节点证据+1(support)", ev_count(origin_nid) == 2, ev_count(origin_nid))
    check("1d 扫描报告 retold>=1", r1.get("retold", 0) >= 1, r1)

    print("=== 2. 幂等: 再扫一次零变化 ===")
    r2 = db.scan_progress()
    nodes2, ms2 = node_state()
    check("2a added=0 retold=0", r2["added"] == 0 and r2["retold"] == 0, r2)
    check("2b 节点/水位不变", len(nodes2) == len(nodes1) and ms2 == ms1)

    print("=== 3. 回归样例(§15-1 截图实锤): 泛词/虚词混入必须被拦 ===")
    cod_id = insert_art(
        "Call of Duty Modern Warfare 4 launch trailer reveal planned for June",
        "https://test.example.com/cod/mw4-trailer")
    created.append(cod_id)
    innodata_id = insert_art(
        "Here's What Revenue Trends Tell Investors About The Cloud Market",
        "https://test.example.com/innodata/nebiius-revenue")
    created.append(innodata_id)
    db.scan_progress()
    nodes3, ms3 = node_state()
    check("3a COD/Innodata 类混入被拦(节点数不变)", len(nodes3) == len(nodes1),
          "%d->%d" % (len(nodes1), len(nodes3)))

    print("=== 4. 真相关: 区分词交集>=3 应挂新节点 ===")
    real_id = insert_art(
        "Rockstar confirms Grand Theft Auto 6 trailer hit record views worldwide",
        "https://test.example.com/rockstar/gta6-views")
    created.append(real_id)
    db.scan_progress()
    nodes4, ms4 = node_state()
    check("4a 真相关挂上(节点+1)", len(nodes4) == len(nodes1) + 1,
          "%d->%d" % (len(nodes1), len(nodes4)))
    check("4b 水位+1(计未读)", ms4 == ms1 + 1, "%s->%s" % (ms1, ms4))

    print("=== 5. 人工纠错: remove_node 压缩 seq 并钳制已读 ===")
    new_nid = nodes4[-1][0]
    r5 = db.remove_node(EID, new_nid)
    nodes5, ms5 = node_state()
    check("5a 删后节点回到基线", len(nodes5) == len(nodes1), nodes5)
    check("5b seq 连续无空洞", [s for _, s in nodes5] == list(range(len(nodes5))),
          nodes5)
    check("5c max_seq 重算", ms5 == ms1, "%s->%s" % (ms1, ms5))

    print("=== 6. 旧节点无 evidence 行的回退(向后兼容) ===")
    conn = db.connect()
    orphan = conn.execute(
        "SELECT COUNT(*) c FROM nodes n WHERE n.event_id=? AND NOT EXISTS"
        " (SELECT 1 FROM node_evidence e WHERE e.node_id=n.node_id)",
        (EID,)).fetchone()["c"]
    conn.close()
    st = db.storyline(EID)
    all_have_sources = all(n.get("sources") for n in st["nodes"])
    check("6a storyline 每节点都有 sources(含回退)", orphan == 0 or all_have_sources,
          "orphan=%d" % orphan)

    print("=== 7. 更正检测: 同题+更正词 -> 节点标 corrected, 证据留 correction ===")
    corr_id = insert_art(
        "Publisher issues correction: The Grand Theft Auto 6 launch trailer is here",
        "https://test.example.com/corr/gta6-trailer-correction")
    created.append(corr_id)
    db.scan_progress(use_llm=False)
    conn = db.connect()
    origin_nid0 = nodes0[0][0]
    st7 = conn.execute("SELECT status FROM nodes WHERE node_id=?",
                       (origin_nid0,)).fetchone()["status"]
    ev7 = [r["role"] for r in conn.execute(
        "SELECT role FROM node_evidence WHERE node_id=? AND article_id=?",
        (origin_nid0, corr_id)).fetchall()]
    dec7 = conn.execute(
        "SELECT verdict FROM decisions WHERE article_id=? AND event_id=?",
        (corr_id, EID)).fetchone()
    conn.close()
    check("7a 节点标 corrected", st7 == "corrected", st7)
    check("7b 证据 role=correction", "correction" in ev7, ev7)
    check("7c decisions 留痕 verdict=correction",
          dec7 is not None and dec7["verdict"] == "correction",
          dec7 and dec7["verdict"])
    nodes7, ms7 = node_state()
    check("7d 更正不加节点不加未读", len(nodes7) == len(nodes0) and ms7 == ms0)

    print("=== 8. 冲突检测: 同题+否认词 -> 节点标 conflicted(保留双方证据不裁决) ===")
    conf_id = insert_art(
        "The Grand Theft Auto 6 launch trailer is here, outlet denies it is real",
        "https://test.example.com/conf/gta6-trailer-denies")
    created.append(conf_id)
    db.scan_progress(use_llm=False)
    conn = db.connect()
    st8 = conn.execute("SELECT status FROM nodes WHERE node_id=?",
                       (origin_nid0,)).fetchone()["status"]
    ev8 = [r["role"] for r in conn.execute(
        "SELECT role FROM node_evidence WHERE node_id=? AND article_id=?",
        (origin_nid0, conf_id)).fetchall()]
    conn.close()
    check("8a 节点标 conflicted", st8 == "conflicted", st8)
    check("8b 证据 role=conflict 且不裁决", "conflict" in ev8, ev8)

    print("=== 9. LLM 管线(假judge): 边缘候选 same_event/insufficient 落地 ===")
    e1 = insert_art("Auto trailer views break records worldwide",
                    "https://test.example.com/edge/auto-trailer-views")
    e2 = insert_art("Grand theft movie casting announced today",
                    "https://test.example.com/edge/theft-movie-casting")
    created.extend([e1, e2])
    import newsdesk_editor as ed
    real_judge = ed.judge_candidates

    def fake_judge(pairs, cap=20):
        # 按身份明确分配(不能按序号截断: 池里可能有 COD 等历史边缘候选)
        vs = []
        for p in pairs:
            if p["article_id"] == e1:
                v = "same_event"
            elif p["article_id"] == e2:
                v = "insufficient"
            else:
                v = "unrelated"     # COD 等历史边缘候选 -> 否决留痕
            vs.append({"article_id": p["article_id"], "event_id": p["event_id"],
                       "verdict": v, "reason": "假judge:管线测试"})
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        c2 = db.connect()
        for x in vs:
            c2.execute(
                "INSERT OR REPLACE INTO decisions(article_id,event_id,verdict,reason,"
                "prompt_ver,model,created_at) VALUES(?,?,?,?,?, 'fake', ?)",
                (x["article_id"], x["event_id"], x["verdict"], x["reason"],
                 ed.PROMPT_VER, now))
        c2.commit()
        c2.close()
        return {"verdicts": vs, "cached": [], "failed": False}

    ed.judge_candidates = fake_judge
    try:
        r9 = db.scan_progress(use_llm=True)
    finally:
        ed.judge_candidates = real_judge
    nodes9, ms9 = node_state()
    check("9a 边缘候选进了 LLM 判定", r9.get("llm_added", 0) >= 1
          and r9.get("llm_pending", 0) >= 1, r9)
    conn = db.connect()
    n_e1 = conn.execute("SELECT status, seq FROM nodes WHERE article_id=?", (e1,)).fetchone()
    n_e2 = conn.execute("SELECT status, seq FROM nodes WHERE article_id=?", (e2,)).fetchone()
    conn.close()
    check("9b same_event 挂正式节点", n_e1 is not None and n_e1["status"] == "active"
          and n_e1["seq"] >= 0, dict(n_e1) if n_e1 else None)
    check("9c insufficient 挂 pending 不占 seq", n_e2 is not None
          and n_e2["status"] == "pending" and n_e2["seq"] == -1,
          dict(n_e2) if n_e2 else None)
    check("9d pending 不进主线时间线", all(n["article_id"] != e2
          for n in db.storyline(EID)["nodes"]))
    check("9e pending 在待确认区", any(p["article_id"] == e2
          for p in db.storyline(EID)["pending_nodes"]))

    print("=== 10. 人工复核: 接受待确认进正式故事线 ===")
    conn = db.connect()
    nid_e2 = conn.execute("SELECT node_id FROM nodes WHERE article_id=?",
                          (e2,)).fetchone()["node_id"]
    ms_before = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                             (EID,)).fetchone()["max_seq"]
    conn.close()
    r10 = db.accept_pending(EID, nid_e2)
    nodes10, ms10 = node_state()
    check("10a 接受成功分配 seq", r10.get("seq") == ms_before + 1, r10)
    check("10b 状态转 active 计入水位", ms10 == ms_before + 1, "%s->%s" % (ms_before, ms10))
    check("10c 幂等拒绝重复接受", "error" in db.accept_pending(EID, nid_e2))

    print("=== 11. 真 LLM 冒烟(网络调用,失败不挡验收) ===")
    try:
        smoke = real_judge([{
            "article_id": "a_smoke_test_00000000", "event_id": EID,
            "title": "Auto trailer views break records", "summary": "",
            "event_name": ORIGIN_TITLE,
            "titles": [ORIGIN_TITLE]}], cap=1)
        if smoke.get("failed"):
            print("SKIP 11a 真 LLM 不可达(离线/无key),管线已由假judge覆盖")
        else:
            ok = all(v["verdict"] in ("same_event", "related", "unrelated",
                                      "insufficient") for v in smoke["verdicts"])
            check("11a 真 LLM verdict 合法", ok, smoke["verdicts"])
            conn = db.connect()
            conn.execute("DELETE FROM decisions WHERE article_id='a_smoke_test_00000000'")
            conn.commit()
            conn.close()
    except Exception as e:
        print("SKIP 11a 真 LLM 冒烟异常(不挡验收): %r" % e)
finally:
    cleanup(created)
    # 收尾复原: 删除测试期间新建的节点 + 状态复位
    conn = db.connect()
    conn.execute("UPDATE nodes SET status='active' WHERE event_id=?"
                 " AND status IN ('corrected','conflicted')", (EID,))
    rows = conn.execute(
        "SELECT node_id FROM nodes WHERE event_id=? AND COALESCE(status,'active')"
        "!='pending' ORDER BY seq", (EID,)).fetchall()
    for i, r in enumerate(rows):
        conn.execute("UPDATE nodes SET seq=? WHERE node_id=?", (i, r["node_id"]))
    conn.execute("UPDATE events SET max_seq=? WHERE event_id=?", (len(rows) - 1, EID))
    conn.commit()
    conn.close()

print()
print("==== PASS %d / FAIL %d ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
