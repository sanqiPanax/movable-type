# -*- coding: utf-8 -*-
"""newsdesk_db -- 事件追踪阶段A: 报道身份与持久化(SQLite)

设计依据: docs/EVENT-TRACKING-SPEC.md §4 最小数据契约 / §5 第一步 / §10 阶段A。

原则(规格原文落地):
- 报道身份优先来源自带ID; 当前 newsdesk RSS 未携带 guid -> 次选「规范链接」。
  规范链接只移除**已知跟踪参数**(utm_*/gclid/fbclid/...), 不删可能区分文章的查询参数。
- 同链接而内容变化 -> 保存内容版本(article_versions), **不另建无关报道**。
- 每次导入/出报写处理记录(runs): 输入指纹、版本、耗时、结果。
- 数据库是状态来源, HTML 是可重建产物; 出报管线对 DB 失败**免疫**(挂了报纸照出)。
- 本阶段只写不读: 跨期读取是阶段B的事, 现有 build_paper.py 仍读 latest.json。

用法:
  python -X utf8 newsdesk_db.py <snapshot.json>   # 导入一份采集快照并打印统计
代码内: import newsdesk_db; newsdesk_db.import_snapshot(data)
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "data", "newsdesk.db")
SNAP_KEEP = 30                      # 原始快照最多留 30 份(§3: 采集快照分开保存)

# 已知跟踪参数白名单外全保留(规格: 只移除已知跟踪参数)
TRACKING_PARAMS = {
    "gclid", "gbraid", "wbraid", "dclid", "fbclid", "igshid",
    "mc_eid", "mc_cid", "yclid", "msclkid", "_hsenc", "_hsmi",
    "vero_id", "wickedid", "oly_enc_id", "oly_anon_id",
    "ref_src", "cmpid", "spm", "scm", "share_medium", "share_source",
    "share_session_id", "share_tag", "share_plat", "si",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  source_id   TEXT PRIMARY KEY,      -- ithome / gnews_tech / x_sama ...
  name        TEXT,                  -- 显示名
  category    TEXT                   -- 分组: 科技/游戏/金融/政治/中文/社区/聚合/项目
);
CREATE TABLE IF NOT EXISTS articles (
  article_id        TEXT PRIMARY KEY,          -- a_<sha1(规范链接)16> 永久身份
  url_original      TEXT,
  url_canonical     TEXT UNIQUE,               -- 规范链接(身份依据)
  source_id         TEXT,
  title             TEXT,
  summary           TEXT,
  published_at      TEXT,                      -- ISO8601, 保留原始时区
  published_precision TEXT,                    -- minute|hour|day|unknown
  collected_at      TEXT,
  group_name        TEXT,
  first_seen_run    INTEGER,
  current_version   INTEGER DEFAULT 1,
  content_hash      TEXT,
  status            TEXT DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_at);
CREATE TABLE IF NOT EXISTS article_versions (   -- 证据基底: 节点证据引用「报道+版本」
  article_id  TEXT,
  version     INTEGER,
  title       TEXT,
  summary     TEXT,
  content_hash TEXT,
  observed_at TEXT,
  run_id      INTEGER,
  PRIMARY KEY (article_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT,
  finished_at TEXT,
  stage       TEXT,                             -- ingest | build | (阶段B+ 再扩)
  input_fingerprint TEXT,                       -- 输入材料指纹
  rule_version TEXT, prompt_version TEXT, model TEXT,
  result      TEXT,                             -- ok | error
  articles_new INTEGER, articles_updated INTEGER, articles_skipped INTEGER,
  duration_ms INTEGER, error TEXT
);
-- ================= 阶段B: 追踪(SPEC §4 Event/Node/Follow) =================
CREATE TABLE IF NOT EXISTS events (
  event_id        TEXT PRIMARY KEY,             -- e_<sha1(创始报道)12> 永久身份
  display_name    TEXT,                         -- 创始报道标题截断(显示用)
  origin_article_id TEXT,
  status          TEXT DEFAULT 'active',
  first_seen_at   TEXT,
  max_seq         INTEGER DEFAULT 0,            -- 节点更新序号水位(已读位置的参照)
  last_node_at    TEXT                          -- 最近节点收录时间(排序用)
);
CREATE TABLE IF NOT EXISTS nodes (
  node_id     TEXT PRIMARY KEY,                 -- n_<sha1(event+article)12>
  event_id    TEXT,
  article_id  TEXT,
  node_type   TEXT,                             -- origin(创始) | direct(直接进展)
  occurred_at TEXT,                             -- 发生/发表时间(原始时区)
  occurred_precision TEXT,
  seq         INTEGER,                          -- 事件内单调递增 = 已读位置口径(SPEC §6)
  match_score REAL,                             -- 规则匹配分(留痕供人工复核)
  first_seen_run INTEGER,
  created_at  TEXT,
  UNIQUE(event_id, article_id)                  -- 同事件同报道只挂一次 -> 重跑不重复追加
);
CREATE TABLE IF NOT EXISTS follows (
  follow_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_local  TEXT DEFAULT 'local',
  event_id    TEXT,
  status      TEXT DEFAULT 'active',            -- active | archived(关闭即归档)
  followed_at TEXT,
  archived_at TEXT,
  last_visible_seq INTEGER,                     -- 归档时最后可见版本
  last_read_seq INTEGER DEFAULT 0,
  UNIQUE(user_local, event_id)
);
"""


# ---------------------------------------------------------------- 规范链接/身份
def canonical_url(url):
    """规范链接: 去已知跟踪参数 + host 小写 + 去 fragment + path 空则补 /。
    不解码重编码 query(避免改动可能区分文章的参数值), 不动 path。"""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return None
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    netloc = p.netloc.lower()
    for port in (":80", ":443"):
        if netloc.endswith(port):
            netloc = netloc[: -len(port)]
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), netloc, p.path or "/", urllib.parse.urlencode(kept), ""))


def article_id_for(canonical):
    return "a_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def content_hash_of(title, summary):
    raw = ((title or "").strip() + "\n" + (summary or "").strip()).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _precision(ts):
    """发布时间精度: 原始字符串里能读出多少位。"""
    ts = (ts or "").strip()
    if len(ts) >= 16:
        return "minute"
    if len(ts) >= 13:
        return "hour"
    if len(ts) >= 10:
        return "day"
    return "unknown"


# ---------------------------------------------------------------- 存储
def connect(db_path=None):
    import sqlite3
    path = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_run(conn, stage, fingerprint, result, new=0, updated=0, skipped=0,
               started=None, error=None, prompt_version=None, model=None):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO runs(stage,input_fingerprint,prompt_version,model,result,"
        "articles_new,articles_updated,articles_skipped,started_at,finished_at,error)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (stage, fingerprint, prompt_version, model, result, new, updated, skipped,
         started or now, now, error))
    conn.commit()
    return cur.lastrowid


def _archive_snapshot(data, db_path):
    """原始采集快照另存(§3: 采集快照/决策日志/展示快照分开保存), 只留最近 SNAP_KEEP 份。"""
    snap_dir = os.path.join(os.path.dirname(db_path), "snap")
    os.makedirs(snap_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = os.path.join(snap_dir, "snap_%s.json" % ts)
    open(fp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    snaps = sorted(f for f in os.listdir(snap_dir) if f.endswith(".json"))
    for old in snaps[:-SNAP_KEEP]:
        try:
            os.remove(os.path.join(snap_dir, old))
        except OSError:
            pass
    return fp


# ---------------------------------------------------------------- 导入
def import_snapshot(data, db_path=None):
    """幂等导入一份 newsdesk 快照。返回 {new, updated, skipped, run_id}。
    - 新报道: INSERT articles(v1) + article_versions(v1)
    - 同规范链接 + 内容变化: 升版本, 旧版本保留 -> 「同链接更新不丢旧版本」
    - 同规范链接 + 同内容: 跳过 -> 「相同输入重跑不重复建报道」
    """
    db_path = db_path or DEFAULT_DB
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t0 = datetime.datetime.now()
    items = data.get("items") or []
    fp = hashlib.sha1(json.dumps(
        [[i.get("url"), i.get("title")] for i in items],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    conn = connect(db_path)
    try:
        for s in data.get("sources") or []:
            if s.get("id"):
                conn.execute("INSERT OR REPLACE INTO sources(source_id,name) VALUES(?,?)",
                             (s["id"], s.get("name")))
        run_id = record_run(conn, "ingest", fp, "ok", started=started)  # 占位, 结束回填计数
        new = updated = skipped = 0
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for it in items:
            canon = canonical_url(it.get("url"))
            if not canon:
                skipped += 1
                continue
            aid = article_id_for(canon)
            ch = content_hash_of(it.get("title"), it.get("desc"))
            row = conn.execute(
                "SELECT current_version, content_hash FROM articles WHERE article_id=?",
                (aid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO articles(article_id,url_original,url_canonical,source_id,"
                    "title,summary,published_at,published_precision,collected_at,group_name,"
                    "first_seen_run,current_version,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, it.get("url"), canon, it.get("src_id"), it.get("title"),
                     it.get("desc"), it.get("ts"), _precision(it.get("ts")), now_iso,
                     it.get("group"), run_id, 1, ch))
                conn.execute(
                    "INSERT INTO article_versions(article_id,version,title,summary,"
                    "content_hash,observed_at,run_id) VALUES(?,?,?,?,?,?,?)",
                    (aid, 1, it.get("title"), it.get("desc"), ch, now_iso, run_id))
                new += 1
            elif row["content_hash"] == ch:
                skipped += 1
            else:
                v = row["current_version"] + 1
                conn.execute(
                    "UPDATE articles SET title=?,summary=?,current_version=?,content_hash=?,"
                    "collected_at=? WHERE article_id=?",
                    (it.get("title"), it.get("desc"), v, ch, now_iso, aid))
                conn.execute(
                    "INSERT INTO article_versions(article_id,version,title,summary,"
                    "content_hash,observed_at,run_id) VALUES(?,?,?,?,?,?,?)",
                    (aid, v, it.get("title"), it.get("desc"), ch, now_iso, run_id))
                updated += 1
        dur = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        conn.execute(
            "UPDATE runs SET articles_new=?,articles_updated=?,articles_skipped=?,"
            "duration_ms=? WHERE run_id=?", (new, updated, skipped, dur, run_id))
        conn.commit()
        try:
            _archive_snapshot(data, db_path)
        except OSError:
            pass
        return {"new": new, "updated": updated, "skipped": skipped,
                "run_id": run_id, "duration_ms": dur}
    except Exception as e:
        conn.rollback()
        record_run(conn, "ingest", fp, "error", started=started, error=repr(e)[:400])
        raise
    finally:
        conn.close()


def stats(db_path=None):
    conn = connect(db_path or DEFAULT_DB)
    try:
        out = {}
        for name in ("sources", "articles", "article_versions", "runs"):
            out[name] = conn.execute("SELECT COUNT(*) c FROM %s" % name).fetchone()["c"]
        out["versioned"] = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE current_version>1").fetchone()["c"]
        return out
    finally:
        conn.close()


# ================================================================ 阶段B: 追踪
# 落地 SPEC §6 用户操作 + §5 第五步直接进展判定(阶段B只做"规则可判"的部分,
# 转述合并/冲突/更正是阶段C,LLM 归属判定也是C——B 不碰模型)。

def track_article(article_id, db_path=None):
    """开启追踪: 报道 -> 事件 -> origin 节点 -> active follow。幂等。
    首轮: 一篇报道对应一个事件(event_id 派生自创始报道身份, 永久稳定)。
    返回 {event_id, display_name, created} 或 {"error": ...}"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        art = conn.execute(
            "SELECT title, url_canonical FROM articles WHERE article_id=?",
            (article_id,)).fetchone()
        if art is None:
            return {"error": "报道不存在(不在已入库材料中)"}
        # 该报道已是某事件的 origin/节点 -> 关联既有事件而不是新建
        node = conn.execute(
            "SELECT event_id FROM nodes WHERE article_id=? LIMIT 1",
            (article_id,)).fetchone()
        created = False
        if node is not None:
            event_id = node["event_id"]
        else:
            event_id = "e_" + hashlib.sha1(article_id.encode("utf-8")).hexdigest()[:16]
            created = True
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            name = (art["title"] or "").strip()[:60]
            conn.execute(
                "INSERT INTO events(event_id,display_name,origin_article_id,"
                "first_seen_at,max_seq) VALUES(?,?,?,?,0)",
                (event_id, name, article_id, now))
            conn.execute(
                "INSERT OR IGNORE INTO nodes(node_id,event_id,article_id,node_type,"
                "occurred_at,occurred_precision,seq,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("n_" + event_id[2:], event_id, article_id, "origin",
                 None, "unknown", 0, now))
            conn.execute("UPDATE events SET max_seq=0 WHERE event_id=?", (event_id,))
        # follow 幂等: 已有 active 不重复建
        fol = conn.execute(
            "SELECT status FROM follows WHERE user_local='local' AND event_id=?",
            (event_id,)).fetchone()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if fol is None:
            conn.execute(
                "INSERT INTO follows(user_local,event_id,status,followed_at,last_read_seq)"
                " VALUES('local',?,?,?,?)", (event_id, "active", now, 0))
        elif fol["status"] == "archived":
            # 首轮不做恢复追踪(SPEC §6): 归档即终态,重复点击给明确提示
            return {"error": "该事件已归档(首轮不支持恢复追踪)", "event_id": event_id}
        conn.commit()
        return {"event_id": event_id,
                "display_name": conn.execute(
                    "SELECT display_name FROM events WHERE event_id=?",
                    (event_id,)).fetchone()["display_name"],
                "created": created}
    finally:
        conn.close()


def untrack_event(event_id, db_path=None):
    """关闭追踪 = 归档(SPEC §6): 记关闭时间与最后可见版本,故事线保留。"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        ev = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                          (event_id,)).fetchone()
        if ev is None:
            return {"error": "事件不存在"}
        fol = conn.execute(
            "SELECT status FROM follows WHERE user_local='local' AND event_id=?",
            (event_id,)).fetchone()
        if fol is None:
            return {"error": "没有该追踪"}
        if fol["status"] == "archived":
            return {"event_id": event_id, "status": "archived", "changed": False}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "UPDATE follows SET status='archived', archived_at=?, last_visible_seq=? "
            "WHERE user_local='local' AND event_id=?", (now, ev["max_seq"], event_id))
        conn.commit()
        return {"event_id": event_id, "status": "archived", "changed": True}
    finally:
        conn.close()


def list_follows(status="active", db_path=None):
    """追踪列表: 事件标题 + 未读更新数 + 最近更新 + 关注时间。"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        rows = conn.execute(
            "SELECT f.event_id, f.status, f.followed_at, f.archived_at, f.last_read_seq,"
            " f.last_visible_seq, e.display_name, e.max_seq, e.last_node_at,"
            " (SELECT COUNT(*) FROM nodes n WHERE n.event_id=f.event_id"
            "   AND n.seq>f.last_read_seq) AS unread"
            " FROM follows f JOIN events e ON e.event_id=f.event_id"
            " WHERE f.user_local='local' AND f.status=?"
            " ORDER BY COALESCE(e.last_node_at, f.followed_at) DESC",
            (status,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def storyline(event_id, db_path=None, read_seq=None):
    """故事线: 节点按 seq 升序 + 每节点带原文证据(SPEC §6 详情)。
    read_seq>0 时标 is_unread; 关注分界 = origin 节点之后。"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        ev = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if ev is None:
            return {"error": "事件不存在"}
        rows = conn.execute(
            "SELECT n.*, a.title, a.url_original, a.url_canonical, a.source_id,"
            " a.published_at, a.group_name"
            " FROM nodes n JOIN articles a ON a.article_id=n.article_id"
            " WHERE n.event_id=? ORDER BY n.seq", (event_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_unread"] = (read_seq is not None and d["seq"] > read_seq)
            out.append(d)
        return {"event": dict(ev), "nodes": out}
    finally:
        conn.close()


def mark_read(event_id, db_path=None):
    """标记已读: 位置 = 当前 max_seq(SPEC §6: 用更新序号不用时间)。"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        ev = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                          (event_id,)).fetchone()
        if ev is None:
            return {"error": "事件不存在"}
        conn.execute(
            "UPDATE follows SET last_read_seq=? WHERE user_local='local' AND event_id=?",
            (ev["max_seq"], event_id))
        conn.commit()
        return {"event_id": event_id, "last_read_seq": ev["max_seq"]}
    finally:
        conn.close()


# ---------------------------------------------------------- 进展扫描(B: 规则)
_RE_RETELL = re.compile(r"转述|报道|获悉|表示|宣布|消息人士|sources?:|according to|"
                        r"report(ed|s)?|says?|confirmed?", re.I)


def _toks(text):
    """粗分词: 英文按词,中文按二元组。够 B 阶段做候选重合度。"""
    text = (text or "").lower()
    eng = set(re.findall(r"[a-z][a-z0-9]{2,}", text))
    cjk = re.findall(r"[一-鿿]+", text)
    big = set()
    for seg in cjk:
        big.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return eng | big


def scan_progress(db_path=None, top_k=3, min_overlap=2):
    """每日直接进展扫描(SPEC §5 第五步的 B 阶段规则版):
    对每个 active 事件, 在**全量合格材料**(不截断粗筛, SPEC §5 第二步)里找
    与事件既有报道 token 重合>=min_overlap 的新报道 -> 挂 direct 节点。
    - 同一 (event, article) UNIQUE -> 天然幂等, 重跑不重复追加
    - B 阶段不做归属 LLM/转述合并(那是 C), 先把"新节点进来+跨期保存"跑通
    返回 {checked, added, per_event}。"""
    db_path = db_path or DEFAULT_DB
    conn = connect(db_path)
    try:
        follows = conn.execute(
            "SELECT event_id FROM follows WHERE user_local='local' AND status='active'"
        ).fetchall()
        if not follows:
            return {"checked": 0, "added": 0, "per_event": {}}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # 事件画像 = 创始报道标题(固定画像)。⚠️ 不能用"所有节点并集":节点越多画像越大,
        # 每轮扫描都会比上一轮多抓(实测 3->25 节点滚雪球),同输入重跑不再同输出。
        # 判定新进展的措辞漂移是阶段C(LLM)的事,B 阶段钉死画像换幂等。
        profiles = {}
        for f in follows:
            eid = f["event_id"]
            row = conn.execute(
                "SELECT a.title FROM events e JOIN articles a"
                " ON a.article_id=e.origin_article_id WHERE e.event_id=?",
                (eid,)).fetchone()
            if row is None or not row["title"]:
                ev = conn.execute("SELECT display_name FROM events WHERE event_id=?",
                                  (eid,)).fetchone()
                title = ev["display_name"] if ev else ""
            else:
                title = row["title"]
            profiles[eid] = _toks(title)
        # 材料 = 全库报道(合格: 有标题有链接)
        all_arts = conn.execute(
            "SELECT article_id, title, published_at, published_precision FROM articles"
            " WHERE title IS NOT NULL AND title!=''").fetchall()
        added = checked = 0
        per_event = {}
        for eid, prof in profiles.items():
            if not prof:
                continue
            have = {r["article_id"] for r in conn.execute(
                "SELECT article_id FROM nodes WHERE event_id=?", (eid,)).fetchall()}
            seq = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                               (eid,)).fetchone()["max_seq"]
            n_add = 0
            for a in all_arts:
                checked += 1
                if a["article_id"] in have:
                    continue
                ov = len(_toks(a["title"]) & prof)
                if ov < min_overlap:
                    continue
                seq += 1
                conn.execute(
                    "INSERT OR IGNORE INTO nodes(node_id,event_id,article_id,node_type,"
                    "occurred_at,occurred_precision,seq,match_score,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    ("n_%s_%s" % (eid[2:], a["article_id"][2:]), eid, a["article_id"],
                     "direct", a["published_at"],
                     a["published_precision"] or "minute", seq, float(ov), now))
                n_add += 1
                have.add(a["article_id"])
                added += 1
            if n_add:
                conn.execute(
                    "UPDATE events SET max_seq=?, last_node_at=? WHERE event_id=?",
                    (seq, now, eid))
            per_event[eid] = n_add
        conn.commit()
        return {"checked": checked, "added": added, "per_event": per_event}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    # python -X utf8 newsdesk_db.py <snapshot.json> [db_path]
    if len(sys.argv) < 2:
        print("usage: newsdesk_db.py <snapshot.json> [db_path]")
        sys.exit(2)
    snap = json.load(open(sys.argv[1], encoding="utf-8"))
    dbp = sys.argv[2] if len(sys.argv) > 2 else None
    r = import_snapshot(snap, dbp)
    print("导入: 新报道 %(new)d | 新版本 %(updated)d | 跳过 %(skipped)d | %(duration_ms)dms | run#%(run_id)d" % r)
    print("库内:", json.dumps(stats(dbp), ensure_ascii=False))
