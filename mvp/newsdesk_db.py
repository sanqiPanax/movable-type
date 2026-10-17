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
