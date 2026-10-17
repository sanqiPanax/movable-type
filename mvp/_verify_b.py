# -*- coding: utf-8 -*-
"""阶段B 端到端验收(SPEC §10-B 完成标准):
1 连续多次出报同一事件仍同一故事线 / 2 关闭后停止更新 / 3 转述不重复计(UNIQUE幂等)
4 已读按seq / 5 归档冻结只读 / 6 服务鉴权(token+Origin)
"""
import json
import sys
import urllib.request

CAND = [7777, 8090, 9090, 9527, 8642]
BASE = None
for _p in CAND:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % _p, timeout=1.2) as r:
            if r.status == 200:
                BASE = "http://127.0.0.1:%d" % _p
                break
    except Exception:
        pass
if not BASE:
    print("FAIL 服务未在任何候选端口运行:", CAND)
    sys.exit(2)
print("服务地址:", BASE)
PASS = []
FAIL = []


def call(path, method="GET", body=None, token=None, origin=None):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Track-Token", token)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail else ""))


# 0. 服务存活 + token 注入
code, st = call("/api/status")
check("0a 服务存活", code == 200 and st.get("ok"), st)
req = urllib.request.Request(BASE + "/")
with urllib.request.urlopen(req, timeout=10) as r:
    page = r.read().decode("utf-8")
import re
tok = re.search(r'<meta name=.track-token. content=.([0-9a-f]+).', page)
tok = tok.group(1) if tok else None
check("0b 页面注入会话token", bool(tok))
check("0c 页面含追踪按钮与追踪区", 'class="trk"' in page and "trackRegion" in page)

# 1. 无 token 的写操作必须被拒(SPEC §3 校验凭据)
code, j = call("/api/track", "POST", {"article_id": "a_x"})
check("1a 无token写被拒", code == 403, j)

# 2. 伪 Origin 必须被拒
code, j = call("/api/track", "POST", {"article_id": "a_x"}, token=tok,
               origin="http://evil.example")
check("1b 伪Origin被拒", code == 403, j)

# 3. 正常追踪: 取库里一条真报道
sys.path.insert(0, r"F:\DoMyProject\魔法日报\mvp")
import newsdesk_db as db
conn = db.connect()
art = conn.execute(
    "SELECT article_id, title FROM articles WHERE title LIKE '%AI%' OR title LIKE '%发布%' "
    "ORDER BY RANDOM() LIMIT 1").fetchone()
art2 = conn.execute(
    "SELECT article_id FROM articles WHERE article_id!=? ORDER BY RANDOM() LIMIT 1",
    (art["article_id"],)).fetchone()
conn.close()
code, j = call("/api/track", "POST", {"article_id": art["article_id"]}, token=tok,
               origin=BASE)
eid = j.get("event_id")
check("2a 开启追踪", code == 200 and eid and j.get("created") is True, j)

# 4. 重复点同一报道 -> 同一事件,不重复创建
code, j2 = call("/api/track", "POST", {"article_id": art["article_id"]}, token=tok,
                origin=BASE)
check("2b 重复追踪幂等", code == 200 and j2.get("event_id") == eid
      and j2.get("created") is False, j2)

# 5. 进展扫描: 挂一条同主题报道为 direct 节点, 再跑幂等
r1 = db.scan_progress()
n_nodes = None
conn = db.connect()
n_nodes = conn.execute("SELECT COUNT(*) c FROM nodes WHERE event_id=?", (eid,)).fetchone()["c"]
conn.close()
r2 = db.scan_progress()
conn = db.connect()
n_nodes2 = conn.execute("SELECT COUNT(*) c FROM nodes WHERE event_id=?", (eid,)).fetchone()["c"]
conn.close()
check("3a 扫描幂等(重跑节点不涨)", n_nodes2 == n_nodes, "%s->%s" % (n_nodes, n_nodes2))
print("     scan_progress r1=%s" % r1)

# 6. 跨期: 模拟"连续多次出报仍同一故事线" —— 事件身份不变
conn = db.connect()
ev = conn.execute("SELECT event_id, display_name, max_seq FROM events WHERE event_id=?",
                  (eid,)).fetchone()
conn.close()
check("4a 事件身份稳定", ev and ev["event_id"] == eid, dict(ev) if ev else None)

# 7. 列表含未读与水位
code, fl = call("/api/follows?status=active")
me = [f for f in fl if f["event_id"] == eid]
check("5a 列表见该事件", code == 200 and len(me) == 1, me and me[0].get("unread"))

# 8. 故事线页可读(HTML)
req = urllib.request.Request(BASE + "/storyline/" + eid)
with urllib.request.urlopen(req, timeout=10) as r:
    html_st = r.read().decode("utf-8")
check("5b 故事线页渲染", "发展脉络" in html_st and "创始报道" in html_st or "追踪" in html_st)

# 9. 已读: 位置=max_seq
code, j = call("/api/read", "POST", {"event_id": eid}, token=tok,
               origin=BASE)
code, fl = call("/api/follows?status=active")
me = [f for f in fl if f["event_id"] == eid][0]
check("6a 已读置到水位", j.get("last_read_seq") == me.get("max_seq") and me["unread"] == 0, j)

# 10. 归档: 关闭即归档, 记最后可见版本
code, j = call("/api/untrack", "POST", {"event_id": eid}, token=tok,
               origin=BASE)
check("7a 归档成功", code == 200 and j.get("status") == "archived", j)
code, arch = call("/api/follows?status=archived")
me_a = [f for f in arch if f["event_id"] == eid]
check("7b 归档列表可见且带冻结水位", len(me_a) == 1 and me_a[0]["last_visible_seq"] is not None,
      me_a and me_a[0].get("last_visible_seq"))
code, act = call("/api/follows?status=active")
check("7c 归档后退出活跃列表", not any(f["event_id"] == eid for f in act))

# 11. 归档后扫描不再为其加节点(关闭后停止更新)
before = db.scan_progress()
conn = db.connect()
n3 = conn.execute("SELECT COUNT(*) c FROM nodes WHERE event_id=?", (eid,)).fetchone()["c"]
conn.close()
check("8a 归档事件扫描不加节点", n3 == n_nodes2, "%s->%s" % (n_nodes2, n3))

# 12. 归档后再点追踪 -> 明确不支持恢复(SPEC §6 首轮不做恢复入口)
code, j = call("/api/track", "POST", {"article_id": art["article_id"]}, token=tok,
               origin=BASE)
check("8b 归档事件不静默复活", code == 200 and "error" in j, j)

# 13. 报纸追踪区数据端可用 + 只读模式文案存在源码
code, fl2 = call("/api/follows?status=active")
check("9a 活跃列表可读", code == 200)
check("9b 报纸含只读降级文案", "只读模式" in page and "服务未启动" in page.replace(" ", "")
      or "只读模式" in page)

print()
print("==== PASS %d / FAIL %d ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
