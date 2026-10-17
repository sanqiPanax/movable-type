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
finally:
    cleanup(created)
    # 收尾复原: 删除测试期间新建的节点(真相关那条已remove,转述证据行随报道删除)
    conn = db.connect()
    rows = conn.execute(
        "SELECT node_id FROM nodes WHERE event_id=? ORDER BY seq", (EID,)).fetchall()
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
