# -*- coding: utf-8 -*-
"""newsdesk_fetch -- 多渠道信息台采集核心(2026-09-18 建)

用途:一次调用抓齐十几个国外/国内信息源,输出统一结构的 JSON,
供 newsdesk_server.py 的网页看板与 agent 直接读取,
替代「agent 大范围联网搜索」这件慢且贵的事。

设计约束(均来自 2026-09-18 的本机实测,不是推测):
  1. 缓存优先:同一源在 cache_ttl 秒内不重复抓,避免被限流。
  2. 限流退避:Reddit 实测「首次 200、连发第 2 个请求即 429」= 按 IP 短窗口限流,
     故 Reddit 单独走长 TTL(redis 默认 900s),且串行、请求间加间隔。
  3. 代理分组:实测 HackerNews/Lobsters/Techmeme/ArsTechnica/TheVerge/Solidot/IT之家
     可直连;Reddit/GitHub Trending/Google News 需走 127.0.0.1:7897。
     每源先试其首选通路,失败再退到另一条。
  4. UA 必须像浏览器:python 默认 UA 抓 Reddit 直接 429,浏览器 UA 才 200。
  5. 时间权威 = 源自己的 pubDate/published,不靠模型猜;统一转 UTC。

用法(Windows 必须 cmd 包一层,否则管道编码崩):
    cmd /c "set PYTHONUTF8=1&& python newsdesk_fetch.py --hours 48 -o items.json"
    cmd /c "set PYTHONUTF8=1&& python newsdesk_fetch.py --only hackernews,arstechnica"
    cmd /c "set PYTHONUTF8=1&& python newsdesk_fetch.py --list-sources"
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PROXY_DIRECT = None
PROXY_CLASH = "http://127.0.0.1:7897"

HERE = pathlib.Path(__file__).resolve().parent
CACHE_DIR = HERE / "_cache"
STATE_FILE = HERE / "_state.json"

# ------------------------------------------------------------------ 源定义
# route: "direct"=直连实测可用 / "proxy"=实测必须走代理 / "auto"=先代理后直连
# kind:  rss / html(自行解析,如 GitHub Trending)
SOURCES = [
    # ---- 科技媒体(直连实测可用)----
    {"id": "hackernews", "name": "Hacker News", "kind": "rss",
     "url": "https://hnrss.org/frontpage", "route": "direct",
     "group": "社区", "ttl": 600},
    {"id": "lobsters", "name": "Lobsters", "kind": "rss",
     "url": "https://lobste.rs/rss", "route": "direct",
     "group": "社区", "ttl": 900},
    {"id": "techmeme", "name": "Techmeme", "kind": "rss",
     "url": "https://www.techmeme.com/feed.xml", "route": "direct",
     "group": "科技", "ttl": 600},
    {"id": "arstechnica", "name": "Ars Technica", "kind": "rss",
     "url": "https://feeds.arstechnica.com/arstechnica/index", "route": "direct",
     "group": "科技", "ttl": 900},
    {"id": "theverge", "name": "The Verge", "kind": "rss",
     "url": "https://www.theverge.com/rss/index.xml", "route": "direct",
     "group": "科技", "ttl": 900},
    {"id": "solidot", "name": "Solidot", "kind": "rss",
     "url": "https://www.solidot.org/index.rss", "route": "direct",
     "group": "中文", "ttl": 900},
    {"id": "ithome", "name": "IT之家", "kind": "rss",
     "url": "https://www.ithome.com/rss/", "route": "direct",
     "group": "中文", "ttl": 900},
    {"id": "sspai", "name": "少数派", "kind": "rss",
     "url": "https://sspai.com/feed", "route": "direct",
     "group": "中文", "ttl": 1800},
    {"id": "qbitai", "name": "量子位", "kind": "rss",
     "url": "https://www.qbitai.com/feed", "route": "direct",
     "group": "中文", "ttl": 1800},
    # ---- 需代理(实测 7897)----
    {"id": "gnews_ai", "name": "Google News · AI", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=artificial+intelligence"
             "&hl=en-US&gl=US&ceid=US:en"), "route": "proxy",
     "group": "聚合", "ttl": 900},
    {"id": "gnews_tech", "name": "Google News · 科技", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=technology+OR+semiconductor"
             "&hl=en-US&gl=US&ceid=US:en"), "route": "proxy",
     "group": "聚合", "ttl": 900},
    {"id": "github_trending", "name": "GitHub Trending", "kind": "trending",
     "url": "https://github.com/trending?since=daily", "route": "proxy",
     "group": "项目", "ttl": 3600},
    # ---- 限流源(降级:长 TTL + 串行 + 缓存)----
    {"id": "reddit_technology", "name": "Reddit · r/technology", "kind": "rss",
     "url": "https://www.reddit.com/r/technology/.rss", "route": "proxy",
     "group": "社区", "ttl": 1800, "throttle": True},
    {"id": "reddit_programming", "name": "Reddit · r/programming", "kind": "rss",
     "url": "https://www.reddit.com/r/programming/.rss", "route": "proxy",
     "group": "社区", "ttl": 1800, "throttle": True},
    {"id": "reddit_localllama", "name": "Reddit · r/LocalLLaMA", "kind": "rss",
     "url": "https://www.reddit.com/r/LocalLLaMA/.rss", "route": "proxy",
     "group": "社区", "ttl": 1800, "throttle": True},

    # ---- 游戏题材(2026-09-21 为小黑盒管线接入,逐个实测见
    #      F:\DoWrite\10_内容发布\小黑盒\_pipeline\_feeds_test.txt)----
    # 实测作废:r/gaming、r/Games、r/pcgaming(429)、机核(SSL断)、VGTIME/游民星空(404)
    {"id": "gnews_game", "name": "Google News · 游戏", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=video+games+OR+PlayStation"
             "+OR+Nintendo+OR+Xbox&hl=en-US&gl=US&ceid=US:en"), "route": "proxy",
     "group": "游戏", "ttl": 900},
    {"id": "gnews_game_cn", "name": "Google News · 游戏(中文)", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=%E6%B8%B8%E6%88%8F+OR+Steam"
             "+OR+%E4%B8%BB%E6%9C%BA&hl=zh-CN&gl=CN&ceid=CN:zh"), "route": "proxy",
     "group": "游戏", "ttl": 900},
    {"id": "pcgamer", "name": "PC Gamer", "kind": "rss",
     "url": "https://www.pcgamer.com/rss/", "route": "direct",
     "group": "游戏", "ttl": 1800},
    {"id": "eurogamer", "name": "Eurogamer", "kind": "rss",
     "url": "https://www.eurogamer.net/feed", "route": "direct",
     "group": "游戏", "ttl": 1800},
    {"id": "gamesindustry", "name": "GamesIndustry.biz", "kind": "rss",
     "url": "https://www.gamesindustry.biz/feed", "route": "direct",
     "group": "游戏", "ttl": 1800},
    {"id": "yystv", "name": "游研社", "kind": "rss",
     "url": "https://www.yystv.cn/rss/feed", "route": "direct",
     "group": "游戏", "ttl": 1800},
    {"id": "steam_news", "name": "Steam 官方新闻", "kind": "rss",
     "url": "https://store.steampowered.com/feeds/news.xml", "route": "direct",
     "group": "游戏", "ttl": 3600},
    # ---- 金融 / 政治(2026-10-17 为多版报纸接入;Google News 聚合,路由同上)----
    {"id": "gnews_fin", "name": "Google News · 金融", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=stocks+OR+economy"
             "+OR+central+bank+OR+earnings&hl=en-US&gl=US&ceid=US:en"),
     "route": "proxy", "group": "金融", "ttl": 900},
    {"id": "gnews_fin_cn", "name": "Google News · 金融(中文)", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=%E8%82%A1%E5%B8%82"
             "+OR+%E5%A4%AE%E8%A1%8C+OR+%E7%BB%8F%E6%B5%8E"
             "&hl=zh-CN&gl=CN&ceid=CN:zh"), "route": "proxy",
     "group": "金融", "ttl": 900},
    {"id": "gnews_pol", "name": "Google News · 政治", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=politics+OR+election"
             "+OR+government+OR+congress&hl=en-US&gl=US&ceid=US:en"),
     "route": "proxy", "group": "政治", "ttl": 900},
    {"id": "gnews_pol_cn", "name": "Google News · 政治(中文)", "kind": "rss",
     "url": ("https://news.google.com/rss/search?q=%E9%80%89%E4%B8%BE"
             "+OR+%E6%94%BF%E7%AD%96+OR+%E5%A4%94%E4%BA%A4"
             "&hl=zh-CN&gl=CN&ceid=CN:zh"), "route": "proxy",
     "group": "政治", "ttl": 900},
]

# ---- X / Twitter(2026-09-21 接入;走 twitter-cli 登录态 cookie,见 newsdesk_x.py)----
# 具体源由 x_sources.json 决定(关注流 + 指定账号),改配置不用动这个文件。
try:
    import newsdesk_x
    SOURCES.extend(newsdesk_x.sources())
except Exception as _e:  # noqa: BLE001
    # 兜底而不是崩:一个 X 模块的问题不该让整个信息台抓不到东西
    print("[x] newsdesk_x 加载失败: %s" % _e, file=sys.stderr)
    SOURCES.append({"id": "x_broken", "name": "X / Twitter(加载失败)", "kind": "disabled",
                    "url": "", "route": "proxy", "group": "社区", "ttl": 0,
                    "note": "newsdesk_x 加载失败: %s" % _e})


# ------------------------------------------------------------------ 网络
def _open(url, proxy, timeout, headers=None):
    ph = urllib.request.ProxyHandler({} if not proxy else {"http": proxy, "https": proxy})
    op = urllib.request.build_opener(ph)
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with op.open(req, timeout=timeout) as r:
        return r.status, r.read()


def fetch(url, route, timeout=20, tries=2, headers=None):
    """按 route 决定尝试顺序。返回 (body_text, via_label)。
    实测依据:直连源在代理下反而可能被 CDN 拒,故顺序不能一律先代理。
    """
    order = [("direct", PROXY_DIRECT), ("proxy", PROXY_CLASH)] if route == "direct" \
        else [("proxy", PROXY_CLASH), ("direct", PROXY_DIRECT)]
    last = None
    for _ in range(tries):
        for label, p in order:
            try:
                st, body = _open(url, p, timeout, headers)
                if len(body) < 100:
                    raise ValueError("short body %d" % len(body))
                return body.decode("utf-8", "replace"), label
            except Exception as e:  # noqa: BLE001
                last = "%s:%s" % (label, (type(e).__name__ + " " + str(e))[:70])
        time.sleep(1.5)
    raise RuntimeError(last or "unknown")


# ------------------------------------------------------------------ 解析
def strip_tags(s):
    """去掉 HTML 标签并还原实体。

    实测坑(2026-09-18):Google News 与 IT之家的 description 里 HTML 是
    双重转义的(&lt;p&gt; 而非 <p>)。若先 strip 再 unescape,拆完标签后
    又还原出可见的 <p ...> 裸标签,直接显示在摘要里。
    正确顺序:先 unescape,再清标签,循环两遍直到稳定。
    """
    if not s:
        return ""
    prev = None
    cur = s
    for _ in range(3):
        if cur == prev:
            break
        prev = cur
        cur = html.unescape(cur)
        cur = re.sub(r"<script[^>]*>.*?</script>", " ", cur, flags=re.S | re.I)
        cur = re.sub(r"<style[^>]*>.*?</style>", " ", cur, flags=re.S | re.I)
        cur = re.sub(r"<[^>]+>", " ", cur)
        cur = re.sub(r"\s+", " ", cur).strip()
    # 清掉残留的孤立实体与零宽字符
    cur = cur.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", cur).strip()


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", s)
    if not m:
        return None
    y, mo, d_, h, mi, se = (int(x) for x in m.groups()[:6])
    tz = m.group(7) or "Z"
    try:
        if tz == "Z":
            t = dt.datetime(y, mo, d_, h, mi, se, tzinfo=dt.timezone.utc)
        else:
            sign = 1 if tz[0] == "+" else -1
            tz2 = tz.replace(":", "")
            off = dt.timedelta(hours=int(tz2[1:3]), minutes=int(tz2[3:5])) * sign
            t = dt.datetime(y, mo, d_, h, mi, se, tzinfo=dt.timezone(off)).astimezone(dt.timezone.utc)
        return t
    except ValueError:
        return None


def _tag(block, *names):
    for n in names:
        m = re.search(r"<%s[^>]*>(.*?)</%s>" % (n, n), block, re.S | re.I)
        if m:
            v = re.sub(r"^\s*<!\[CDATA\[", "", m.group(1))
            return re.sub(r"\]\]>\s*$", "", v).strip()
    return ""


def clean_desc(s, limit=400):
    """摘要后处理:Google News 的 description 是纯链接堆(<a href=...>标题</a>),
    strip 完只剩 URL 残渣,显示出来毫无意义 —— 这种情况直接留空。"""
    s = strip_tags(s)
    if not s:
        return ""
    # 去掉裸露 URL(Google News 聚合常见)
    s2 = re.sub(r"https?://\S+", "", s).strip()
    s2 = re.sub(r"\s+", " ", s2).strip(" -·|")
    if len(s2) < 12:          # 剩下的是残渣,不如不显示
        return ""
    return s2[:limit]


def parse_rss(raw, src):
    items = re.findall(r"<item[\s>].*?</item>", raw, re.S) or \
            re.findall(r"<entry[\s>].*?</entry>", raw, re.S)
    out = []
    for it in items:
        title = strip_tags(_tag(it, "title"))
        link = strip_tags(_tag(it, "link", "id"))
        if not link.startswith("http"):
            m = re.search(r'<link[^>]*href="([^"]+)"', it)
            link = m.group(1) if m else link
        ts = parse_date(_tag(it, "pubDate", "published", "updated", "dc:date", "date"))
        desc = clean_desc(_tag(it, "description", "summary", "content:encoded"))
        if not title or not link:
            continue
        out.append({"title": title, "url": link,
                    "ts": ts.isoformat() if ts else None,
                    "desc": desc, "src": src["name"],
                    "src_id": src["id"], "group": src["group"]})
    return out


def parse_trending(raw, src):
    """GitHub Trending 是 HTML,没有时间戳。

    实测坑(2026-09-18):若统一用「抓取时刻」当 ts,21 条会以同一时刻
    霸占纯时间序看板的顶部,把真正有时效的新闻挤下去。
    故这里标记 ts=None,由 collect() 归到「趋势」区,不参与时间排序。
    """
    out = []
    for m in re.finditer(r'<article class="Box-row">(.*?)</article>', raw, re.S):
        block = m.group(1)
        repo = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"', block)
        if not repo:
            continue
        path = repo.group(1).strip()
        desc_m = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', block, re.S)
        star_m = re.search(r'([\d,]+)\s*</span>\s*</a>\s*</div>\s*<div[^>]*>\s*<a[^>]*>\s*([\d,]+)', block, re.S)
        stars = ""
        sm = re.search(r'aria-label="([\d,]+) users starred', block)
        if sm:
            stars = sm.group(1)
        out.append({
            "title": path.strip("/"),
            "url": "https://github.com" + path,
            "ts": None,
            "desc": (strip_tags(desc_m.group(1)) if desc_m else "")[:400] +
                    (" · ⭐%s" % stars if stars else ""),
            "src": src["name"], "src_id": src["id"], "group": src["group"]})
    return out


# ------------------------------------------------------------------ 缓存
def cache_path(sid):
    return CACHE_DIR / ("%s.json" % sid)


def read_cache(sid, ttl):
    p = cache_path(sid)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if ttl and age > ttl:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_cache_any(sid):
    """无视 TTL 读缓存 —— 抓失败时的兜底,宁可给旧数据也别给空白。"""
    p = cache_path(sid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def write_cache(sid, items):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(sid).write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ 单源
def grab_one(src, force=False, timeout=20):
    sid = src["id"]
    if src["kind"] == "disabled":
        return {"id": sid, "name": src["name"], "status": "disabled",
                "items": [], "note": src.get("note", "")}

    if not force:
        cached = read_cache(sid, src.get("ttl", 900))
        if cached is not None:
            return {"id": sid, "name": src["name"], "status": "cached",
                    "items": cached, "count": len(cached)}

    t0 = time.time()
    try:
        if src["kind"] == "x":
            # X 不走 HTTP:twitter-cli 子进程内部已处理代理与 cookie,
            # 单次调用约 4-8s,给足超时(实测 feed -n 30 约 8s)
            items, via = newsdesk_x.grab(src, timeout=max(timeout, 30))
        else:
            raw, via = fetch(src["url"], src["route"], timeout=timeout)
            if src["kind"] == "trending":
                items = parse_trending(raw, src)
            else:
                items = parse_rss(raw, src)
        if not items:
            raise ValueError("parsed 0 items")
        write_cache(sid, items)
        return {"id": sid, "name": src["name"], "status": "ok", "via": via,
                "items": items, "count": len(items),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        err = (type(e).__name__ + " " + str(e))[:120]
        # 抓失败 → 退到过期缓存,保证看板不空
        stale = read_cache_any(sid)
        if stale:
            return {"id": sid, "name": src["name"], "status": "stale",
                    "err": err, "items": stale, "count": len(stale),
                    "ms": int((time.time() - t0) * 1000)}
        return {"id": sid, "name": src["name"], "status": "fail", "err": err,
                "items": [], "count": 0, "ms": int((time.time() - t0) * 1000)}


def collect(sources, force=False, timeout=20):
    """限流源串行(且加间隔),其余并发 —— 实测 Reddit 并发必 429。"""
    throttled = [s for s in sources if s.get("throttle")]
    normal = [s for s in sources if not s.get("throttle")]

    results = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda s: grab_one(s, force, timeout), normal):
            results[r["id"]] = r

    for s in throttled:
        results[s["id"]] = grab_one(s, force, timeout)
        time.sleep(4)  # Reddit 按 IP 短窗口限流,间隔是必需的
    return [results[s["id"]] for s in sources]


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="多渠道信息台采集核心")
    ap.add_argument("--hours", type=float, default=72,
                    help="只看最近 N 小时(默认 72)")
    ap.add_argument("--days", type=float, default=None, help="等价 --hours 的另一种写法")
    ap.add_argument("-o", "--out", default=None, help="输出 JSON 路径")
    ap.add_argument("--only", default=None, help="只抓这些源 id,逗号分隔")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重抓")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--list-sources", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.list_sources:
        for s in SOURCES:
            print("%-18s %-22s %-7s ttl=%-5s %s" % (
                s["id"], s["name"], s["route"], s.get("ttl"), s["group"]))
        return 0

    srcs = SOURCES
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        srcs = [s for s in SOURCES if s["id"] in want]
        if not srcs:
            print("没有匹配的源: %s" % args.only, file=sys.stderr)
            return 2

    results = collect(srcs, force=args.force, timeout=args.timeout)

    hours = args.hours if args.days is None else args.days * 24
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

    all_items = []
    for r in results:
        for it in r["items"]:
            t = parse_date(it.get("ts"))
            # 无时间戳的条目(GitHub Trending)归入 trends,不进时间流
            if t is None:
                it["_ts"] = None
                it["_epoch"] = None
                all_items.append(it)
                continue
            if t < cutoff:
                continue
            it["_ts"] = t.isoformat()
            it["_epoch"] = t.timestamp()
            all_items.append(it)

    trends = [i for i in all_items if i.get("_epoch") is None]
    timed = [i for i in all_items if i.get("_epoch") is not None]
    timed.sort(key=lambda x: x["_epoch"], reverse=True)

    # 去重:同 URL 只留最新一条(时间流与趋势各自去重)
    def dedupe(seq):
        seen, out = set(), []
        for it in seq:
            key = it.get("url") or it.get("title")
            h = hashlib.md5(key.encode("utf-8")).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            out.append(it)
        return out

    deduped = dedupe(timed)
    trends = dedupe(trends)

    summary = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_hours": hours,
        "total": len(deduped),
        "trend_total": len(trends),
        "sources": [{"id": r["id"], "name": r["name"], "status": r["status"],
                     "count": r.get("count", 0), "via": r.get("via"),
                     "ms": r.get("ms"), "err": r.get("err"),
                     "note": r.get("note")} for r in results],
        "items": deduped,
        "trends": trends,
    }

    if args.out:
        outp = pathlib.Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        if not args.quiet:
            print("写入 %s (%d 条 + 趋势 %d)" % (outp, len(deduped), len(trends)))
    elif not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=1))

    if not args.quiet:
        ok = sum(1 for r in results if r["status"] in ("ok", "cached"))
        print("源状态: %d/%d 可用 | 条目 %d | 趋势 %d" % (
            ok, len(results), len(deduped), len(trends)), file=sys.stderr)
        for r in results:
            if r["status"] in ("fail", "disabled", "stale"):
                print("  [%s] %s %s" % (r["status"], r["name"], r.get("err") or r.get("note", "")),
                      file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
