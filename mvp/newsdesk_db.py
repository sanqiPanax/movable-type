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
import difflib
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
  status            TEXT DEFAULT 'ok',
  score       INTEGER,                          -- 六维总分(annotate 算出,公共重要选稿用)
  score_d     TEXT                              -- 六维明细 JSON(src/imp/ex/ev/irr/hk)
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
  article_id  TEXT,                             -- 主报道(向后兼容;真实多来源看 node_evidence)
  node_type   TEXT,                             -- origin(创始) | direct(直接进展) | pending(待确认)
  occurred_at TEXT,                             -- 发生/发表时间(原始时区)
  occurred_precision TEXT,
  seq         INTEGER,                          -- 事件内单调递增 = 已读位置口径(SPEC §6);pending 不占 seq(-1)
  match_score REAL,                             -- 规则匹配分(留痕供人工复核)
  first_seen_run INTEGER,
  created_at  TEXT,
  status      TEXT DEFAULT 'active',            -- active|pending(待确认)|corrected(已被更正)|conflicted(说法不一致)
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
-- ============ 阶段C: 节点证据(SPEC §5第五步"同事实转述合并到同一节点,保留多个来源") ============
CREATE TABLE IF NOT EXISTS node_evidence (
  node_id     TEXT,
  article_id  TEXT,
  role        TEXT DEFAULT 'support',           -- primary(主报道) | support(转述/补充来源) | correction(更正来源)
  added_at    TEXT,
  added_run   INTEGER,
  PRIMARY KEY (node_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_ev_node ON node_evidence(node_id);
-- ============ 阶段C: 归属判定留痕(SPEC §5第四步: 任一自动归属可回查证据与决策版本) =======
CREATE TABLE IF NOT EXISTS decisions (
  article_id  TEXT,
  event_id    TEXT,
  verdict     TEXT,                             -- same_event|related|unrelated|insufficient|retell|corrected|removed
  reason      TEXT,
  prompt_ver  TEXT,
  model       TEXT,
  created_at  TEXT,
  PRIMARY KEY (article_id, event_id)
);
-- ============ 阶段D: 展示批次(SPEC §4 Issue / §5第七步 三分区配额) ============
CREATE TABLE IF NOT EXISTS issues (
  issue_no    INTEGER PRIMARY KEY,              -- 期号(与报纸期号一致)
  seed        TEXT,                             -- 抽样种子(同seed+同输入=同清单, SPEC验收)
  quota_cfg   TEXT,                             -- 各分区配额 JSON(可配置,不按点击行为改变)
  fingerprint TEXT,                             -- 输入材料版本指纹
  generated_at TEXT
);
CREATE TABLE IF NOT EXISTS issue_items (
  issue_no    INTEGER,
  article_id  TEXT,
  node_id     TEXT,                             -- 追踪更新条目关联节点(可空)
  partition   TEXT,                             -- explore|important|tracking
  tags        TEXT,                             -- 多标签 JSON(同一内容占多分区只展示一次)
  position    INTEGER,
  PRIMARY KEY (issue_no, article_id)
);
CREATE INDEX IF NOT EXISTS idx_issue_items_art ON issue_items(article_id);
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
    _migrate(conn)
    return conn


def _migrate(conn):
    """存量库轻量迁移: CREATE IF NOT EXISTS 不会给已存在的表加列。幂等。"""
    n_cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "status" not in n_cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN status TEXT DEFAULT 'active'")
    a_cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "score" not in a_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN score INTEGER")
        conn.execute("ALTER TABLE articles ADD COLUMN score_d TEXT")
        # 索引必须在列存在后建(放 SCHEMA 会在迁移前炸掉旧库连接)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(score)")
    conn.commit()


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
            sc = it.get("score")
            scd = json.dumps(it.get("score_d"), ensure_ascii=False) if it.get("score_d") else None
            row = conn.execute(
                "SELECT current_version, content_hash FROM articles WHERE article_id=?",
                (aid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO articles(article_id,url_original,url_canonical,source_id,"
                    "title,summary,published_at,published_precision,collected_at,group_name,"
                    "first_seen_run,current_version,content_hash,score,score_d)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, it.get("url"), canon, it.get("src_id"), it.get("title"),
                     it.get("desc"), it.get("ts"), _precision(it.get("ts")), now_iso,
                     it.get("group"), run_id, 1, ch, sc, scd))
                conn.execute(
                    "INSERT INTO article_versions(article_id,version,title,summary,"
                    "content_hash,observed_at,run_id) VALUES(?,?,?,?,?,?,?)",
                    (aid, 1, it.get("title"), it.get("desc"), ch, now_iso, run_id))
                new += 1
            elif row["content_hash"] == ch:
                skipped += 1
                # 存量补分: 阶段A入库时 items 尚无评分 -> 同内容重入时补写(幂等)
                if sc is not None:
                    conn.execute(
                        "UPDATE articles SET score=?, score_d=?"
                        " WHERE article_id=? AND (score IS NULL OR score!=?)",
                        (sc, scd, aid, sc))
            else:
                v = row["current_version"] + 1
                conn.execute(
                    "UPDATE articles SET title=?,summary=?,current_version=?,content_hash=?,"
                    "collected_at=?,score=?,score_d=? WHERE article_id=?",
                    (it.get("title"), it.get("desc"), v, ch, now_iso, sc, scd, aid))
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
            conn.execute(
                "INSERT OR IGNORE INTO node_evidence(node_id,article_id,role,added_at)"
                " VALUES(?,?,?,?)", ("n_" + event_id[2:], article_id, "primary", now))
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
    """故事线: 节点按 seq 升序 + 每节点带来源证据链(SPEC §6 详情/§5第五步多来源)。
    - pending(待确认)节点不进主线时间线, 单独返回 pending_nodes(人工复核用)
    - corrected(已被更正)/conflicted(说法不一致)保留在时间线并透传状态,不静默抹除
    - 旧节点无 evidence 行 -> 回退主报道为唯一来源
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
            " WHERE n.event_id=? AND COALESCE(n.status,'active')!='pending'"
            " ORDER BY n.seq", (event_id,)).fetchall()
        pend = conn.execute(
            "SELECT n.*, a.title, a.url_original, a.url_canonical, a.source_id,"
            " a.published_at"
            " FROM nodes n JOIN articles a ON a.article_id=n.article_id"
            " WHERE n.event_id=? AND n.status='pending'"
            " ORDER BY n.created_at", (event_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_unread"] = (read_seq is not None and d["seq"] > read_seq)
            evs = conn.execute(
                "SELECT e.role, e.article_id, a.title, a.url_original, a.url_canonical,"
                " a.source_id, a.published_at"
                " FROM node_evidence e JOIN articles a ON a.article_id=e.article_id"
                " WHERE e.node_id=? ORDER BY e.role DESC, a.published_at",
                (d["node_id"],)).fetchall()
            if evs:
                d["sources"] = [dict(x) for x in evs]
            else:  # 旧节点无证据行: 主报道即唯一来源
                d["sources"] = [{"role": "primary", "article_id": d["article_id"],
                                 "title": d["title"], "url_original": d["url_original"],
                                 "url_canonical": d["url_canonical"],
                                 "source_id": d["source_id"],
                                 "published_at": d["published_at"]}]
            out.append(d)
        pending_out = []
        for r in pend:
            d = dict(r)
            evs = conn.execute(
                "SELECT e.role, a.title, a.url_canonical, a.url_original, a.source_id"
                " FROM node_evidence e JOIN articles a ON a.article_id=e.article_id"
                " WHERE e.node_id=?", (d["node_id"],)).fetchall()
            d["sources"] = [dict(x) for x in evs] or [
                {"role": "primary", "title": d["title"],
                 "url_canonical": d["url_canonical"], "source_id": d["source_id"]}]
            dec = conn.execute(
                "SELECT reason, prompt_ver FROM decisions"
                " WHERE article_id=? AND event_id=?",
                (d["article_id"], event_id)).fetchone()
            d["judge_reason"] = dec["reason"] if dec else ""
            pending_out.append(d)
        return {"event": dict(ev), "nodes": out, "pending_nodes": pending_out}
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


# ---------------------------------------------------------- 进展扫描(C: 关联质量+转述合并)
# SPEC §15-1「先修关联质量」回归样例(2026-10-17 截图实锤):
#   - COD 新闻靠 "launch/trailer" 泛词混入 GTA6 故事线
#   - AI 公司营收报道靠 "here/the" 英文虚词混入
# 修法三件套: ①停用词不计分 ②DF 区分词过滤(全库标题出现率>=DF_MAX 的 token 不算区分性,
#   数据驱动而非手写硬扛) ③转述优先: 与节点既有来源同题 -> 补来源,不加节点不加未读。

_STOP_EN = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "been", "being", "have", "has", "had", "will", "would", "can", "could",
    "may", "might", "must", "shall", "should", "its", "his", "her", "their",
    "our", "your", "you", "they", "them", "not", "but", "about", "into",
    "over", "after", "before", "than", "then", "when", "where", "who", "what",
    "which", "how", "why", "here", "there", "more", "most", "some", "any",
    "each", "every", "both", "few", "many", "much", "other", "another",
    "such", "only", "same", "too", "very", "just", "also", "even", "still",
    "again", "once", "said", "says", "according", "between", "during",
    "under", "until", "while", "because", "since", "however", "these",
    "those", "been", "does", "did", "done", "get", "got", "like", "well",
    "back", "first", "last", "next", "new", "now", "way", "use", "out",
    "one", "two", "three", "all", "its",
}
_STOP_ZH = {
    "发布", "正式", "今天", "今日", "新款", "表示", "宣布", "市场", "用户",
    "产品", "公司", "游戏", "玩家", "进行", "相关", "报道", "最新", "上线",
    "更新", "推出", "亮相", "确认", "开发", "计划", "预计", "带来", "如何",
    "还是", "已经", "开始", "视频", "图片", "时间", "问题", "可能", "需要",
    "认为", "出现", "成为", "目前", "现在", "以及", "通过", "关于", "方面",
    "情况", "首次", "连续", "一个", "这个", "那个", "我们", "你们", "他们",
    "记者", "消息", "报告", "媒体", "显示", "透露", "提到", "指出", "谈到",
}
DF_MAX = 0.03          # 区分词门槛: 全库标题出现率 <3% 才算有区分性
RETELL_RATIO = 0.62    # 同题阈值(复用粗筛去重的经验值)
# 更正/冲突信号词(SPEC §5第五步: 更正关联旧节点保留记录;冲突保留双方证据不裁决)
# 规则版只认明确信号词,语义级矛盾判定属 LLM 层(阶段C后续)
_CORRECT_WORDS = re.compile(
    r"更正|撤回|撤稿|误传|致歉|retract|retracts|correction|erratum|"
    r"walks back|walked back|apolog", re.I)
_CONFLICT_WORDS = re.compile(
    r"不实|辟谣|否认|驳斥|误报|denies|denied|debunk|disputes|calls.{0,12}false|"
    r"not accurate|rejects claim", re.I)
_EDGE_MIN = 2           # 边缘候选下限: 2<=交集<3 送 LLM 判定(规则不再硬猜)


def _toks(text):
    """分词: 英文按词(len>=3)、中文按二元组;停用词直接丢弃。"""
    text = (text or "").lower()
    eng = {w for w in re.findall(r"[a-z][a-z0-9]{2,}", text) if w not in _STOP_EN}
    big = set()
    for seg in re.findall(r"[一-鿿]+", text):
        big.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return eng | {b for b in big if b not in _STOP_ZH}


def _df_rates(titles):
    """全库标题 token 出现率(区分性计算)。"""
    from collections import Counter
    cnt = Counter()
    n = 0
    for t in titles:
        n += 1
        cnt.update(_toks(t))
    if n == 0:
        return {}
    return {w: c / float(n) for w, c in cnt.items()}


def scan_progress(db_path=None, min_distinct=3, use_llm=True):
    """每日直接进展扫描(C 阶段: 关联质量 + 转述合并 + 更正/冲突 + LLM 归属)。
    对每个 active 事件, 在**全量合格材料**(不截断粗筛, SPEC §5 第二步):
    1) 同题判定优先(相似度>=RETELL_RATIO) -> 同节点补来源:
       - 普通转述: role=support, 不加节点不加未读(SPEC §5第五步)
       - 标题含更正信号词: role=correction, 节点标 corrected(旧记录可追溯,不静默抹除)
       - 标题含冲突信号词: role=conflict, 节点标 conflicted(保留双方证据,不裁决哪方为真)
    2) 关联判定: 区分性 token 交集>=min_distinct -> 直接挂 direct 节点。
       ⚠️ min_distinct=3 是 944 条实测校准的: 真相关共享 5 词,混入仅 2 词;
       DF 门槛在小库规模几乎不过滤(launch=0.32%),交集数量级才是当前有效区分器。
    3) 边缘候选(EDGE_MIN<=交集<min_distinct): 规则不硬猜, 批量送 LLM 四分类
       (same_event/related/unrelated/insufficient, SPEC §5第四步); insufficient ->
       pending 节点(不占 seq 不计未读,人工复核接受/驳回); LLM 失败放弃本轮不产生节点。
    幂等: (event,article) 与 (node,article) 双 UNIQUE,重跑不重复追加。
    返回 {checked, added, retold, llm_added, llm_pending, per_event}。"""
    db_path = db_path or DEFAULT_DB
    conn = connect(db_path)
    try:
        follows = conn.execute(
            "SELECT event_id FROM follows WHERE user_local='local' AND status='active'"
        ).fetchall()
        if not follows:
            return {"checked": 0, "added": 0, "retold": 0,
                    "llm_added": 0, "llm_pending": 0, "per_event": {}}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # 材料 = 全库报道(合格: 有标题有链接)
        all_arts = conn.execute(
            "SELECT article_id, title, summary, published_at, published_precision"
            " FROM articles WHERE title IS NOT NULL AND title!=''").fetchall()
        df = _df_rates([a["title"] for a in all_arts])
        distinct = {w for w, r in df.items() if r < DF_MAX}

        added = checked = retold = 0
        llm_added = llm_pending = 0
        edge_pairs = []
        per_event = {}
        for f in follows:
            eid = f["event_id"]
            # 画像 = 创始报道标题(固定画像,防节点并集滚雪球;措辞漂移判定属后续LLM阶段)
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
            prof = _toks(title) & distinct
            if not prof:
                per_event[eid] = {"added": 0, "retold": 0, "llm": 0, "pending": 0}
                continue
            ev_name = title[:80]
            # 事件已有证据(节点主报道 + 全部证据来源)的标题清单 -> 转述比对用
            have = set()
            ev_rows = conn.execute(
                "SELECT n.node_id, n.article_id, a.title FROM nodes n"
                " JOIN articles a ON a.article_id=n.article_id"
                " WHERE n.event_id=?", (eid,)).fetchall()
            src_titles = []
            node_of_article = {}
            for r in ev_rows:
                # 存量迁移: B阶段旧节点无 evidence 行 -> 补 primary(幂等, SPEC §4 证据必存)
                conn.execute(
                    "INSERT OR IGNORE INTO node_evidence(node_id,article_id,role,added_at)"
                    " VALUES(?,?,?,?)", (r["node_id"], r["article_id"], "primary", now))
                have.add(r["article_id"])
                src_titles.append((r["node_id"], r["title"]))
                node_of_article[r["article_id"]] = r["node_id"]
            ev2 = conn.execute(
                "SELECT e.node_id, e.article_id, a.title FROM node_evidence e"
                " JOIN articles a ON a.article_id=e.article_id"
                " JOIN nodes n ON n.node_id=e.node_id WHERE n.event_id=?",
                (eid,)).fetchall()
            for r in ev2:
                have.add(r["article_id"])
                src_titles.append((r["node_id"], r["title"]))
            # 人工纠错/历史否决的报道永久跳过: 否则下轮扫描按同规则重挂,
            # 人工判断会被机器推翻(SPEC §8 移出必须持久)
            for r in conn.execute(
                    "SELECT article_id FROM decisions WHERE event_id=?"
                    " AND verdict IN ('removed','unrelated')", (eid,)).fetchall():
                have.add(r["article_id"])

            seq = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                               (eid,)).fetchone()["max_seq"]
            n_add = n_retell = n_pending = 0
            ev_titles5 = [t for _n, t in src_titles][:5]
            for a in all_arts:
                checked += 1
                if a["article_id"] in have:
                    continue
                # 1) 同题判定: -> 挂到最相似节点的证据链(转述/更正/冲突)
                best_nid, best_r = None, 0.0
                at = _toks(a["title"])
                for nid, t in src_titles:
                    r = difflib.SequenceMatcher(None, a["title"] or "", t or "").ratio()
                    if r > best_r:
                        best_nid, best_r = nid, r
                if best_nid is not None and best_r >= RETELL_RATIO:
                    role = "support"
                    if _CORRECT_WORDS.search(a["title"] or ""):
                        role = "correction"
                        # 后发生的信号覆盖前状态(decisions 留痕完整可回查)
                        conn.execute(
                            "UPDATE nodes SET status='corrected' WHERE node_id=?",
                            (best_nid,))
                    elif _CONFLICT_WORDS.search(a["title"] or ""):
                        role = "conflict"
                        conn.execute(
                            "UPDATE nodes SET status='conflicted' WHERE node_id=?",
                            (best_nid,))
                    conn.execute(
                        "INSERT OR IGNORE INTO node_evidence(node_id,article_id,role,"
                        "added_at) VALUES(?,?,?,?)",
                        (best_nid, a["article_id"], role, now))
                    conn.execute(
                        "INSERT OR REPLACE INTO decisions(article_id,event_id,verdict,"
                        "reason,prompt_ver,model,created_at) VALUES(?,?,?,?,?,'rule',?)",
                        (a["article_id"], eid, role, "同题(%.2f)挂%s" % (best_r, role),
                         "c4-rule-v1", now))
                    have.add(a["article_id"])
                    src_titles.append((best_nid, a["title"]))
                    n_retell += 1
                    retold += 1
                    continue
                # 2) 关联判定: 区分性 token 交集
                ov = len(at & prof)
                if ov >= min_distinct:
                    seq += 1
                    nid = "n_%s_%s" % (eid[2:], a["article_id"][2:])
                    conn.execute(
                        "INSERT OR IGNORE INTO nodes(node_id,event_id,article_id,"
                        "node_type,occurred_at,occurred_precision,seq,match_score,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (nid, eid, a["article_id"], "direct", a["published_at"],
                         a["published_precision"] or "minute", seq, float(ov), now))
                    conn.execute(
                        "INSERT OR IGNORE INTO node_evidence(node_id,article_id,role,"
                        "added_at) VALUES(?,?,?,?)", (nid, a["article_id"], "primary", now))
                    conn.execute(
                        "INSERT OR REPLACE INTO decisions(article_id,event_id,verdict,"
                        "reason,prompt_ver,model,created_at) VALUES(?,?,?,?,?,'rule',?)",
                        (a["article_id"], eid, "same_event",
                         "区分词交集%d>=阈值%d" % (ov, min_distinct), "c4-rule-v1", now))
                    n_add += 1
                    have.add(a["article_id"])
                    src_titles.append((nid, a["title"]))
                    added += 1
                    continue
                # 3) 边缘候选: 规则不硬猜, 收集后批量送 LLM
                if ov >= _EDGE_MIN:
                    edge_pairs.append({
                        "article_id": a["article_id"], "event_id": eid,
                        "title": a["title"], "summary": a["summary"] or "",
                        "event_name": ev_name, "titles": ev_titles5})
            if n_add:
                conn.execute(
                    "UPDATE events SET max_seq=?, last_node_at=? WHERE event_id=?",
                    (seq, now, eid))
            per_event[eid] = {"added": n_add, "retold": n_retell,
                              "llm": 0, "pending": n_pending}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ---- LLM 归属判定(边缘候选批量;函数内 import 防循环依赖)
    if edge_pairs and use_llm:
        try:
            from newsdesk_editor import judge_candidates
            res = judge_candidates(edge_pairs)
        except Exception as e:
            res = {"verdicts": [], "failed": True}
            print("[scan] editor 判定不可用: %r" % e, file=sys.stderr)
        if not res.get("failed"):
            now2 = datetime.datetime.now(datetime.timezone.utc).isoformat()
            conn = connect(db_path)
            try:
                for v in res["verdicts"]:
                    eid = v["event_id"]
                    aid = v["article_id"]
                    st = per_event.get(eid) or {"added": 0, "retold": 0,
                                                "llm": 0, "pending": 0}
                    if v["verdict"] == "same_event":
                        a = next((x for x in all_arts if x["article_id"] == aid), None)
                        if a is None:
                            continue
                        seq = conn.execute(
                            "SELECT max_seq FROM events WHERE event_id=?",
                            (eid,)).fetchone()["max_seq"] + 1
                        nid = "n_%s_%s" % (eid[2:], aid[2:])
                        conn.execute(
                            "INSERT OR IGNORE INTO nodes(node_id,event_id,article_id,"
                            "node_type,occurred_at,occurred_precision,seq,match_score,"
                            "created_at,status) VALUES(?,?,?,?,?,?,?,?,?,'active')",
                            (nid, eid, aid, "direct", a["published_at"],
                             a["published_precision"] or "minute", seq, -1.0, now2))
                        conn.execute(
                            "INSERT OR IGNORE INTO node_evidence(node_id,article_id,role,"
                            "added_at) VALUES(?,?,?,?)", (nid, aid, "primary", now2))
                        conn.execute(
                            "UPDATE events SET max_seq=?, last_node_at=? WHERE event_id=?",
                            (seq, now2, eid))
                        st["added"] = st.get("added", 0) + 1
                        llm_added += 1
                    elif v["verdict"] == "insufficient":
                        nid = "n_%s_%s" % (eid[2:], aid[2:])
                        conn.execute(
                            "INSERT OR IGNORE INTO nodes(node_id,event_id,article_id,"
                            "node_type,occurred_at,occurred_precision,seq,match_score,"
                            "created_at,status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                            (nid, eid, aid, "pending", None, "unknown", -1, -1.0, now2))
                        conn.execute(
                            "INSERT OR IGNORE INTO node_evidence(node_id,article_id,role,"
                            "added_at) VALUES(?,?,?,?)", (nid, aid, "primary", now2))
                        st["pending"] = st.get("pending", 0) + 1
                        llm_pending += 1
                    # related/unrelated: decisions 已由 judge 写库, 不动节点
                    st["llm"] = st.get("llm", 0) + 1
                    per_event[eid] = st
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    return {"checked": checked, "added": added, "retold": retold,
            "llm_added": llm_added, "llm_pending": llm_pending,
            "per_event": per_event}


def accept_pending(event_id, node_id, db_path=None):
    """人工纠错: 接受待确认节点进正式故事线(SPEC §6 待确认由维护者裁决)。
    分配 seq(max_seq+1), 计入未读与水位。"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        n = conn.execute(
            "SELECT event_id, status FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if n is None or n["event_id"] != event_id:
            return {"error": "节点不存在或不属于该事件"}
        if n["status"] != "pending":
            return {"error": "该节点不是待确认状态(当前: %s)" % n["status"]}
        seq = conn.execute("SELECT max_seq FROM events WHERE event_id=?",
                           (event_id,)).fetchone()["max_seq"] + 1
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "UPDATE nodes SET status='active', node_type='direct', seq=?"
            " WHERE node_id=?", (seq, node_id))
        conn.execute("UPDATE events SET max_seq=?, last_node_at=? WHERE event_id=?",
                     (seq, now, event_id))
        conn.commit()
        return {"event_id": event_id, "node_id": node_id, "seq": seq}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_node(event_id, node_id, db_path=None):
    """最小人工纠错(SPEC §8 移出错误节点): 删除节点与其证据,seq 压缩重排,
    重算水位。写 verdict=removed 留痕——扫描据此**永久跳过**该报道,
    否则下轮扫描会按同一规则把报道重挂回来(实测: 人工纠错会被机器推翻)。"""
    conn = connect(db_path or DEFAULT_DB)
    try:
        n = conn.execute("SELECT event_id, article_id FROM nodes WHERE node_id=?",
                         (node_id,)).fetchone()
        if n is None or n["event_id"] != event_id:
            return {"error": "节点不存在或不属于该事件"}
        conn.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
        conn.execute("DELETE FROM node_evidence WHERE node_id=?", (node_id,))
        conn.execute(
            "INSERT OR REPLACE INTO decisions(article_id,event_id,verdict,reason,"
            "prompt_ver,model,created_at) VALUES(?,?,?,?,?,'human',?)",
            (n["article_id"], event_id, "removed", "人工移出",
             "c8-remove-v1",
             datetime.datetime.now(datetime.timezone.utc).isoformat()))
        rows = conn.execute(
            "SELECT node_id FROM nodes WHERE event_id=? ORDER BY seq",
            (event_id,)).fetchall()
        for i, r in enumerate(rows):
            conn.execute("UPDATE nodes SET seq=? WHERE node_id=?", (i, r["node_id"]))
        max_seq = len(rows) - 1 if rows else -1
        conn.execute("UPDATE events SET max_seq=? WHERE event_id=?", (max_seq, event_id))
        # 已读位置不超过新水位
        conn.execute(
            "UPDATE follows SET last_read_seq=MIN(last_read_seq,?)"
            " WHERE user_local='local' AND event_id=?", (max_seq, event_id))
        conn.commit()
        return {"event_id": event_id, "node_id": node_id, "max_seq": max_seq}
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
