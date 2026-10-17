# -*- coding: utf-8 -*-
"""魔法日报 MVP · movable-type
流程: data/latest.json -> 六维打分 -> 规则粗筛 -> LLM 主编(DeepSeek,失败自动回退规则排版)
      -> og:image 配图(失败退无图版面) -> 单文件 HTML (out/index.html)

红线: AI 只选排不改写; 版面新闻文字一律原文切片; 编辑手记/弃稿理由是元信息,不陈述新闻事实。
用法: python -X utf8 build_paper.py [--no-llm] [--fetch]
"""
import datetime
import difflib
import hashlib
import html as H
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "latest.json")
DEC_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "out")
IMG_DIR = os.path.join(OUT_DIR, "img")
CRED_FILE = r"C:\Users\Lenovo\.dsh\.credentials.yaml"
NEWDESK_DIR = os.path.join(HERE, "newsdesk")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROXIES = ["http://127.0.0.1:7897"]
MAX_PROBE = 10          # 最多探测配图的选中条目数
WANT_IMG = 6
MIN_W = 520
BAD_IMG = ("logo", "placeholder", "default", "avatar", "favicon", "1x1", "spacer")
RE_IMG = [
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'](?![^>]*og:description)[^>]+property=["\']og:image',
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
]
LAZY_ATTRS = ("data-original", "data-src", "data-lazy-src", "data-echo",
              "data-actualsrc", "data-lazyload")

SECTIONS = [  # (key, 中文名, 拉丁副名, 观察员署名)
    ("top",   "要闻纵览", "IN BRIEF",        "本报时政观察员"),
    ("tech",  "科技 · 前沿", "TECH FRONTLINE", "本报科技观察员"),
    ("life",  "生活 · 观察", "LIVING & SOCIETY", "本报生活观察员"),
    ("game",  "游戏 · 战报", "GAME DESK",      "本报游戏观察员"),
]
GROUP_FALLBACK = {"科技": "tech", "聚合": "tech", "游戏": "game", "中文": "life", "社区": "life"}


# ---------------------------------------------------------------- 基础工具
def log(*a):
    print("[mvp]", *a, flush=True)


def opener(direct_first=True):
    handlers = [urllib.request.ProxyHandler({})] if direct_first else []
    for p in PROXIES:
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener(*handlers)


def http_get(url, op, timeout=14, limit=8_000_000, referer=None):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with op.open(req, timeout=timeout) as r:
        return r.read(limit), r.url


def deepseek_key():
    try:
        txt = open(CRED_FILE, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", txt)
    return m.group(1) if m else None


# ---------------------------------------------------------------- 粗筛
def coarse(items, n=48):
    """score 降序 + 贪心去重(相似度>=0.62 视为同题)"""
    pool = sorted([i for i in items if i.get("score") is not None],
                  key=lambda x: -x["score"])
    picked, titles = [], []
    for it in pool:
        t = it.get("title") or ""
        if any(difflib.SequenceMatcher(None, t, o).ratio() >= 0.62 for o in titles[-400:]):
            continue
        picked.append(it)
        titles.append(t)
        if len(picked) >= n:
            break
    return picked


def cands_payload(cands):
    out = []
    for k, it in enumerate(cands):
        desc = (it.get("desc") or "").strip()
        out.append({
            "i": "i%d" % k,
            "t": (it.get("title") or "")[:140],
            "d": desc[:160],
            "dl": len(desc),
            "s": (it.get("src") or "")[:40],
            "g": it.get("group") or "",
            "sc": it.get("score"),
        })
    return out


# ---------------------------------------------------------------- AI 主编
EDITOR_SYS = """你是《活字新报》的主编。你只做两件事:选择与编排。绝不改写、翻译、补写任何标题或摘要,版面文字全部来自给定原文。
输出必须是一个 JSON 对象,不要输出任何其他文字。"""
EDITOR_TPL = """版面容量:头版 1 条(最有分量且信息实质最足);要闻纵览 4 条;科技 3 条;生活 3 条;游戏 3 条(各栏可±1,总数 10~14)。

选稿规则:
1. 头版优先给「摘要实质丰富(dl 大)、信源权威(sc 高)、影响面大」的条目;标题党/摘要只等于标题的条目不上头版。
2. 同题重复只上一条。
3. 英文条目照原文保留,不翻译。
4. 未上版条目不必都给理由;从落选池挑最多 5 条你认为读者会问「为什么没上」的,各写一句中文理由(只讲取舍,不重复新闻事实)。
5. editor_note 一句话讲本期取舍思路(<=50字,中文,不得陈述新闻事实)。

候选条目(字段:i 唯一id / t 标题 / d 摘要 / dl 摘要长度 / s 来源 / g 分类 / sc 评分):
__CANDS__

输出 JSON 结构(sections 的 name 只能取:要闻/科技/生活/游戏;头版单独给 lead_id):
{{"lead_id":"iN","sections":[{{"name":"要闻","blocks":[{{"item_id":"iN","weight":"feature"}}]}}],"dropped":[{{"item_id":"iN","reason":"一句话理由"}}],"editor_note":"一句话"}}"""


def llm_edit(cands):
    key = deepseek_key()
    if not key:
        log("无 DeepSeek key,回退规则排版")
        return None
    payload = json.dumps(cands_payload(cands), ensure_ascii=False)
    body = {
        "model": os.environ.get("MT_DEEPSEEK_MODEL", "deepseek-chat"),
        "temperature": 0.2,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": EDITOR_SYS},
            {"role": "user", "content": EDITOR_TPL.replace("__CANDS__", payload)},
        ],
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    for attempt, mt in enumerate((4096, 8192)):
        try:
            body["max_tokens"] = mt
            req = urllib.request.Request(
                "https://api.deepseek.com/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key})
            with opener().open(req, timeout=120) as r:
                resp = json.loads(r.read().decode("utf-8"))
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not content.strip():
                raise RuntimeError("content 为空 (finish=%s)" %
                                   (resp.get("choices") or [{}])[0].get("finish_reason"))
            m = re.search(r"\{.*\}", content, re.S)
            dec = json.loads(m.group(0))
            log("LLM 主编出稿 (model=%s, attempt=%d)" % (resp.get("model"), attempt))
            return dec
        except Exception as e:
            log("LLM 第%d次失败: %s" % (attempt + 1, e))
    return None


def rule_edit(cands):
    """回退:第一条当头版,其余按分栏回填。"""
    name2key = {"要闻": "top", "科技": "tech", "生活": "life", "游戏": "game"}
    dec = {"lead_id": "i0", "sections": [], "dropped": [], "editor_note": "规则排版(未接主编):按评分顺排。"}
    used = {"i0"}
    for k, it in enumerate(cands[1:], 1):
        if len(used) >= 13:
            dec["dropped"].append({"item_id": "i%d" % k, "reason": "版面已满"})
            continue
        key = GROUP_FALLBACK.get(it.get("group"), "top")
        name = next(n for n, kk in name2key.items() if kk == key)
        sec = next((s for s in dec["sections"] if s["name"] == name), None)
        if sec is None:
            sec = {"name": name, "blocks": []}
            dec["sections"].append(sec)
        sec["blocks"].append({"item_id": "i%d" % k, "weight": "brief"})
        used.add("i%d" % k)
    return dec


def normalize_decision(dec, cands):
    """校验 id/容量;非法则抛错回退。返回 {lead, cols{key:[(item,weight)]}, dropped, note}"""
    ids = {"i%d" % k for k in range(len(cands))}
    by_id = {"i%d" % k: (cands[k], it) for k, it in enumerate(cands)}
    lid = dec.get("lead_id")
    if lid not in ids:
        raise ValueError("lead_id 非法")
    used = {lid}
    cols = {k: [] for k, _, _, _ in SECTIONS}
    name2key = {"要闻": "top", "科技": "tech", "生活": "life", "游戏": "game"}
    for sec in dec.get("sections") or []:
        key = name2key.get(sec.get("name"))
        if key is None:
            continue
        for b in (sec.get("blocks") or [])[:5]:
            iid = b.get("item_id")
            if iid in ids and iid not in used and len(cols[key]) < 5:
                cols[key].append((iid, b.get("weight") or "brief"))
                used.add(iid)
    dropped = []
    for d in (dec.get("dropped") or [])[:8]:
        if d.get("item_id") in ids and d.get("item_id") not in used and d.get("reason"):
            dropped.append(d)
    # 空栏回填:按分数补
    rest = ["i%d" % k for k in range(len(cands)) if "i%d" % k not in used]
    for k in cols:
        while len(cols[k]) < 2 and rest:
            cols[k].append((rest.pop(0), "brief"))
    return {"lead": by_id[lid], "cols": cols, "by_id": by_id,
            "dropped": dropped, "note": (dec.get("editor_note") or "").strip()[:80]}


# ---------------------------------------------------------------- 配图
def _try_img(u, final, page_url, op):
    """下载单个候选图,过尺寸/体积门,缩到 1500 宽存 JPEG。返回 (url, bytes) 或 None。"""
    u = urllib.parse.urljoin(final, H.unescape(u.strip()))
    if any(b in u.lower() for b in BAD_IMG) or u.startswith("data:") or u.lower().endswith(".gif"):
        return None, None
    try:
        data, _ = http_get(u, op, timeout=15, limit=12_000_000, referer=page_url)
    except Exception:
        return None, None
    if len(data) < 12_000:
        return None, None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if im.width < MIN_W:
            return None, None
        im = im.convert("RGB")
        if im.width > 1500:
            im.thumbnail((1500, 1500))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        data = buf.getvalue()
    except Exception:
        return None, None
    return u, data


def find_image(page_url):
    """og:image -> twitter:image -> 正文首图(含懒加载属性)。中文源普遍懒加载,只读 src 会 0 命中。"""
    if "news.google.com" in page_url:
        return None, None
    op = opener(direct_first=True)
    try:
        raw, final = http_get(page_url, op, timeout=12, limit=600_000)
    except Exception:
        return None, None
    txt = raw.decode("utf-8", "ignore")

    for rx in RE_IMG:
        m = re.search(rx, txt, re.I | re.S)
        if m:
            u, data = _try_img(m.group(1), final, page_url, op)
            if u:
                return u, data

    # 兜底:正文里最大的 <img>(懒加载属性优先)
    cands = []
    for tag in re.findall(r"<img[^>]*>", txt, re.I)[:120]:
        u = None
        for a in LAZY_ATTRS:
            m = re.search(a + r'=["\']([^"\']+)["\']', tag, re.I)
            if m and not any(b in m.group(1).lower() for b in BAD_IMG):
                u = m.group(1)
                break
        if not u:
            m = re.search(r'srcset=["\']\s*([^\s,"\']+)', tag, re.I)
            if m:
                u = m.group(1)
        if not u:
            m = re.search(r'src=["\']([^"\']+)["\']', tag, re.I)
            if m:
                u = m.group(1)
        if not u:
            continue
        w = 0
        for wa in (r'width=["\']?(\d{3,5})', r'\bw=["\']?(\d{3,5})'):
            mw = re.search(wa, tag, re.I)
            if mw:
                w = int(mw.group(1))
                break
        cands.append((w, u))
    cands.sort(key=lambda x: -x[0])
    for _, u in cands[:6]:
        u2, data = _try_img(u, final, page_url, op)
        if u2:
            return u2, data
    return None, None


def fetch_images(plan_items):
    """plan_items: [(slot_name, [item...])] 每栏给候选序列,优先非 Google News 的。
    返回 {slot: (fname, credit_host)}"""
    os.makedirs(IMG_DIR, exist_ok=True)
    probe = []
    for slot, pool in plan_items:
        for it in pool[:3]:
            url = it.get("url") or ""
            if url and "news.google.com" not in url:
                probe.append((slot, it, url))
                break
    results = {}

    def work(job):
        slot, it, url = job
        u, data = find_image(url)
        if not data:
            return None
        name = "p_%s.jpg" % re.sub(r"\W+", "", slot)
        open(os.path.join(IMG_DIR, name), "wb").write(data)
        return slot, (name, urllib.parse.urlparse(u).netloc.replace("www.", ""))

    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(work, probe):
            if r:
                results[r[0]] = r[1]
    return results


# ---------------------------------------------------------------- 渲染
def esc(s):
    return H.escape(str(s or ""), quote=True)


def meta_line(it):
    ts = (it.get("ts") or "")[:16].replace("T", " ")
    src = esc((it.get("src") or "").split(" · ")[0])
    return "%s　|　%s" % (ts, src)


def domain(url):
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


CSS = """
:root{--paper:#f4eeda;--ink:#1b1712;--muted:#6d6353;--rule:#241e16;--hair:#cbbfa5;--seal:#9e2b25;--bg:#3a3226}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font-family:"Songti SC","SimSun","Noto Serif CJK SC",Georgia,serif;-webkit-font-smoothing:antialiased}
.stage{display:flex;justify-content:center;padding:26px 16px 90px}
.sheet{width:1500px;max-width:100%;background:var(--paper);padding:30px 40px 24px;position:relative;
  box-shadow:0 22px 64px rgba(0,0,0,.45)}
.sheet::after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.35;mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/><feColorMatrix type='saturate' values='0'/></filter><rect width='200' height='200' filter='url(%23n)' opacity='.4'/></svg>")}
.inner{position:relative;z-index:1}
/* ---------- 报头 ---------- */
.slogan{display:flex;justify-content:space-between;font-size:11px;letter-spacing:.34em;color:var(--muted);
  border-bottom:1px solid var(--rule);padding-bottom:5px;margin-bottom:10px}
.masthead{display:flex;align-items:center;justify-content:center;gap:26px;position:relative}
.couplet{writing-mode:vertical-rl;font-size:13px;letter-spacing:.42em;color:#4c432f;opacity:.85}
.mast-center{text-align:center}
.mast-center h1{font-size:74px;letter-spacing:.3em;text-indent:.3em;margin:2px 0 4px;font-weight:700}
.mast-center .sub{font-size:11.5px;letter-spacing:.5em;color:var(--muted);text-indent:.5em;margin-bottom:8px}
.seal{position:absolute;right:6px;top:6px;width:62px;height:62px;border:2.5px solid var(--seal);border-radius:8px;
  color:var(--seal);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;
  letter-spacing:.1em;transform:rotate(-7deg);opacity:.88;writing-mode:vertical-rl}
.rule-double{border-top:3px solid var(--rule);border-bottom:1px solid var(--rule);height:3px}
.folio{display:flex;justify-content:space-between;font-size:11.5px;color:#4c432f;padding:5px 2px;
  border-bottom:1px solid var(--hair);letter-spacing:.12em}
.ticker{overflow:hidden;white-space:nowrap;border-bottom:1px solid var(--rule);padding:6px 0;font-size:12.5px;color:#3a3226}
.ticker .track{display:inline-block;white-space:nowrap}
.alive-text .ticker .track{animation:roll 60s linear infinite}
@keyframes roll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.ticker .tk::after{content:"　◆　";color:var(--seal)}
nav.sections{display:flex;justify-content:center;gap:34px;font-size:12px;letter-spacing:.2em;
  border-bottom:1px solid var(--rule);padding:7px 0}
nav.sections a{color:var(--ink);text-decoration:none}
nav.sections a:hover{color:var(--seal)}
nav.sections span{color:var(--muted);font-size:9.5px;letter-spacing:.14em;margin-left:5px}
/* ---------- 版面 ---------- */
.grid{display:grid;grid-template-columns:repeat(12,1fr)}
.lead{grid-column:span 8;padding:16px 30px 0 0;border-right:1px solid var(--hair)}
.side{grid-column:span 4;padding:16px 0 0 30px}
.kicker{font-size:11px;letter-spacing:.3em;color:var(--seal);margin-bottom:7px}
.kicker .ent{color:#4c432f}
.headline{font-size:40px;line-height:1.24;margin:0 0 10px;font-weight:700}
.headline .tail{font-weight:400;color:#38301f}
.standfirst{font-family:"Kaiti SC","KaiTi","楷体",serif;font-size:16px;line-height:1.7;color:#3d3423;
  border-left:3px double var(--rule);padding-left:12px;margin:0 0 12px}
.byline{font-size:11.5px;color:var(--muted);letter-spacing:.1em;border-bottom:1px solid var(--hair);
  padding-bottom:10px;margin-bottom:14px}
.byline a{color:var(--seal)}
.figwrap{position:relative;overflow:hidden;background:#16130f;border:1px solid var(--rule)}
.figwrap img{display:block;width:100%;height:100%;object-fit:cover;filter:grayscale(1) contrast(1.06)}
.alive-img .figwrap img{animation:kb 26s ease-in-out infinite alternate}
@keyframes kb{from{transform:scale(1.03)}to{transform:scale(1.19) translate3d(-2.5%,-1.8%,0)}}
.fig-hero{height:330px;margin-right:-24px;box-shadow:6px 8px 0 rgba(36,30,22,.15)}
.fig-small{height:128px}
.figcap{font-size:10.5px;color:var(--muted);margin:6px 0 14px;padding-bottom:10px;border-bottom:1px solid var(--hair)}
.figcap .figno{color:var(--seal);letter-spacing:.14em;margin-right:6px}
.body{columns:2;column-gap:28px;font-size:14.5px;line-height:1.8;text-align:justify}
.body p{margin:0 0 11px}
.body p.drop::first-letter{float:left;font-size:52px;line-height:.84;padding:6px 9px 0 0;font-weight:700}
.pullquote{font-family:"Kaiti SC","KaiTi","楷体",serif;font-style:italic;font-size:17px;line-height:1.65;
  color:#33291a;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
  padding:10px 4px;margin:14px 0;column-span:all;text-align:center}
.side h3,.col .head{margin:0}
.side h3{font-size:13px;letter-spacing:.3em;margin-bottom:12px;border-bottom:3px double var(--rule);padding-bottom:7px}
.side h3 span,.col .head span{color:var(--muted);font-size:9.5px;letter-spacing:.16em;margin-left:8px}
.brief{border-bottom:1px solid var(--hair);padding:9px 0;break-inside:avoid}
.brief h4{margin:0 0 4px;font-size:15px;line-height:1.45;font-weight:600}
.brief .meta{font-size:10.5px;color:var(--muted);letter-spacing:.06em}
.brief .meta a{color:var(--seal);text-decoration:none}
.tag{display:inline-block;font-size:9.5px;color:var(--paper);background:#4c432f;padding:1px 6px;
  letter-spacing:.1em;margin-right:6px;vertical-align:1px}
.row3{grid-column:span 12;border-top:3px double var(--rule);margin-top:22px;display:grid;grid-template-columns:repeat(3,1fr)}
.col{padding:14px 22px 0;border-left:1px solid var(--hair)}
.col:first-child{border-left:0;padding-left:0}
.col:last-child{padding-right:0}
.col .head{font-size:12px;letter-spacing:.24em;margin-bottom:10px;color:#33291a}
/* ---------- 主编手记 ---------- */
.handnote{grid-column:span 12;margin-top:22px;border:1px solid var(--rule);background:rgba(255,252,240,.5);
  padding:12px 16px;display:grid;grid-template-columns:2fr 1fr;gap:18px}
.handnote h5{margin:0 0 6px;font-size:11.5px;letter-spacing:.3em;color:var(--seal)}
.handnote .note{font-family:"Kaiti SC","KaiTi","楷体",serif;font-size:14.5px;line-height:1.7}
.handnote .drops{font-size:11.5px;color:#4c432f;line-height:1.7}
.handnote .drops li{margin-bottom:3px}
.handnote .drops b{color:var(--seal);font-weight:600}
footer{grid-column:span 12;margin-top:18px;border-top:1px solid var(--rule);padding-top:8px;
  display:flex;justify-content:space-between;font-size:11px;color:var(--muted);letter-spacing:.1em}
/* ---------- 动效层 ---------- */
.alive-text .ink{clip-path:inset(0 100% -8% 0);animation:ink .8s cubic-bezier(.22,.9,.2,1) both;animation-delay:var(--d,0ms)}
@keyframes ink{to{clip-path:inset(0 -3% -8% 0)}}
.caret{display:inline-block;width:2px;height:.92em;background:var(--seal);vertical-align:-.08em;margin-left:2px;opacity:0}
.typing .caret{opacity:1;animation:blink .7s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.controls{position:fixed;right:20px;bottom:20px;z-index:99;display:flex;gap:15px;align-items:center;
  background:rgba(22,18,12,.93);color:#f2e9d4;border-radius:12px;padding:11px 15px;
  font-family:system-ui,"Microsoft YaHei",sans-serif;font-size:13px;box-shadow:0 12px 34px rgba(0,0,0,.45)}
.controls label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
.controls input{accent-color:#e0a94a;cursor:pointer}
.controls button{font:inherit;background:#e0a94a;color:#1a1610;border:0;border-radius:7px;padding:5px 11px;cursor:pointer}
.controls .sep{width:1px;height:22px;background:rgba(255,255,255,.2)}
@media (prefers-reduced-motion: reduce){
  .alive-text .ink{animation:none;clip-path:none}
  .alive-img .figwrap img{animation:none}
  .alive-text .ticker .track{animation:none}
}
@media (max-width:820px){
  .lead,.side{grid-column:span 12;border-right:0;padding:14px 0 0}
  .body{columns:1}
  .row3{grid-template-columns:1fr}
  .col{border-left:0;padding:14px 0 0;border-top:1px solid var(--hair)}
  .mast-center h1{font-size:44px}
  .couplet,.seal{display:none}
  .handnote{grid-template-columns:1fr}
}
"""


def render(ctx, images, gen_at, stats, period):
    lead_it = ctx["lead"][1]
    esc_ = esc

    def cap(slot, fallback_host):
        if slot in images:
            fname, host = images[slot]
            return fname, host
        return None, fallback_host

    fig_no = [0]

    def figblock(slot, item, cls, fb_host):
        fname, host = cap(slot, fb_host)
        if not fname:
            return "", ""
        fig_no[0] += 1
        fig = ('<figure class="figwrap %s alive-img ink" style="--d:60ms">'
               '<img src="img/%s" alt="" loading="lazy"></figure>' % (cls, fname))
        cap_html = ('<div class="figcap"><span class="figno">图 %02d</span>图源 %s · 本报灰度处理</div>'
                    % (fig_no[0], esc_(host)))
        return fig, cap_html

    # ---- 头版
    title = lead_it.get("title") or ""
    m = re.split(r"(?<=[:：])", title, maxsplit=1)
    h_html = esc_(m[0]) + (('<span class="tail">%s</span>' % esc_(m[1])) if len(m) > 1 else "")
    ents = lead_it.get("score_e") or []
    kicker_ent = ('　·　聚焦 %s' % esc_("、".join(ents[:2]))) if ents else ""
    desc = (lead_it.get("desc") or "").strip()
    if desc.startswith(title[:20]):
        desc = desc[len(title[:20]):].lstrip(" 　-—|")
    stand = ('<p class="standfirst ink" style="--d:120ms">%s</p>' % esc_(desc[:110])) if len(desc) > 24 else ""
    pull = ""
    sent = re.split(r"(?<=[。.!?])", desc)
    if sent and 12 < len(sent[0]) <= 70 and stand:
        pull = '<div class="pullquote ink" style="--d:300ms">%s</div>' % esc_(sent[0].strip())
    hero_fig, hero_cap = figblock("lead", lead_it, "fig-hero", domain(lead_it.get("url") or ""))
    body_p = ""
    if len(desc) > 40:
        body_p = ('<p class="drop ink" style="--d:160ms">%s</p>' % esc_(desc[:300]))
    lead_html = """
<section class="lead" id="lead">
  <div class="kicker">头版 · THE LEAD%s</div>
  <h2 class="headline" id="heroHead">%s</h2>
  %s
  <div class="byline">本报首席记者 · 综合%s 报道　%s　|　<a href="%s" target="_blank" rel="noopener">原文 @ %s</a></div>
  %s%s
  <div class="body">%s</div>
</section>""" % (kicker_ent, h_html, stand, esc_((lead_it.get("src") or "").split(" · ")[0]),
                 esc_((lead_it.get("ts") or "")[:16].replace("T", " ")),
                 esc_(lead_it.get("url")), esc_(domain(lead_it.get("url") or "")),
                 hero_fig, hero_cap, body_p + (pull if pull else ""))

    # ---- 要闻纵览
    side_items = ctx["cols"]["top"]
    briefs = []
    for k, (iid, _w) in enumerate(side_items):
        it = ctx["by_id"][iid][1]
        briefs.append('<article class="brief ink" style="--d:%dms"><h4>%s</h4>'
                      '<div class="meta"><span class="tag">%s</span>%s · <a href="%s" target="_blank" rel="noopener">原文</a></div></article>'
                      % (160 + k * 80, esc_(it.get("title")),
                         esc_((it.get("src") or "?").split(" · ")[0][:6]), esc_(meta_line(it)),
                         esc_(it.get("url"))))
    side_fig, side_cap = ("", "")
    if side_items:
        side_fig, side_cap = figblock("top", ctx["by_id"][side_items[0][0]][1], "fig-small",
                                      domain(ctx["by_id"][side_items[0][0]][1].get("url") or ""))
    side_html = """
<aside class="side" id="top">
  <h3>要闻纵览 <span>IN BRIEF</span></h3>
  %s%s
</aside>""" % (side_fig + side_cap, "".join(briefs))

    # ---- 三栏
    cols_html = []
    meta = {"top": SECTIONS[0], "tech": SECTIONS[1], "life": SECTIONS[2], "game": SECTIONS[3]}
    for key, cname, latin, byline in SECTIONS[1:]:
        rows = []
        sfig, scap = ("", "")
        for k, (iid, _w) in enumerate(ctx["cols"][key]):
            it = ctx["by_id"][iid][1]
            if k == 0 and not sfig:
                sfig, scap = figblock(key, it, "fig-small", domain(it.get("url") or ""))
            rows.append('<article class="brief ink" style="--d:%dms"><h4>%s</h4>'
                        '<div class="meta">%s · <a href="%s" target="_blank" rel="noopener">原文</a></div></article>'
                        % (200 + k * 80, esc_(it.get("title")), esc_(meta_line(it)), esc_(it.get("url"))))
        cols_html.append('<div class="col" id="%s"><div class="head">%s <span>%s · %s</span></div>%s%s%s</div>'
                         % (key, esc_(cname), esc_(latin), esc_(byline), sfig, scap, "".join(rows)))
    row3_html = '<section class="row3">%s</section>' % "".join(cols_html)

    # ---- 主编手记
    drops_html = "".join("<li><b>弃</b> %s — %s</li>" % (esc_(ctx["by_id"][d["item_id"]][1].get("title"))[:46],
                                                         esc_(d["reason"])) for d in ctx["dropped"][:4])
    hand_html = """
<section class="handnote">
  <div><h5>主编手记 · EDITOR'S NOTE</h5><div class="note">%s</div></div>
  <div><h5>取舍留痕 · WHY DROPPED</h5><ul class="drops">%s</ul></div>
</section>""" % (esc_(ctx["note"]) or "本期依六维评分与版面容量取舍。", drops_html or "<li>无</li>")

    # ---- 顶部
    ticker_items = [lead_it] + [ctx["by_id"][i][1] for i, _ in side_items]
    tk = "".join('<span class="tk">%s</span>' % esc_((i.get("title") or "")[:48]) for i in ticker_items[:6])
    today = datetime.date.today()
    wd = "一二三四五六日"[today.weekday()]
    all_items = [lead_it] + [ctx["by_id"][iid][1] for col in ctx["cols"].values() for iid, _ in col]
    read_min = max(1, round(sum(len(i.get("title") or "") + len(i.get("desc") or "") for i in all_items) / 300))
    n_items = len(all_items)

    top_html = """
<header>
  <div class="slogan"><span>独立 · 众源 · 一日一报</span><span>MOVABLE TYPE EDITION</span></div>
  <div class="masthead">
    <div class="couplet">汇百源新讯</div>
    <div class="mast-center">
      <h1>活字新报</h1>
      <div class="sub">THE MOVABLE TYPE DAILY</div>
    </div>
    <div class="couplet">成一纸活字</div>
    <div class="seal">活字</div>
  </div>
  <div class="rule-double"></div>
  <div class="folio">
    <span>第 %d 期</span>
    <span>%d 年 %d 月 %d 日 · 星期%s</span>
    <span>本期 %d 条 · 约 %d 分钟 · 定价:一盏茶</span>
  </div>
  <div class="ticker"><div class="track"><span class="tk">%s</span><span class="tk">%s</span></div></div>
  <nav class="sections">
    <a href="#lead">头版<span>FRONT PAGE</span></a>
    <a href="#top">要闻<span>BRIEF</span></a>
    <a href="#tech">科技<span>TECH</span></a>
    <a href="#life">生活<span>LIVING</span></a>
    <a href="#game">游戏<span>GAMES</span></a>
  </nav>
</header>""" % (period, today.year, today.month, today.day, wd, n_items, read_min, tk, tk)

    footer_html = """
<footer>
  <span>生成于 %s · movable-type MVP</span>
  <span>%s · 配图 og:image · 灰度统一</span>
</footer>""" % (gen_at, esc_(stats))

    doc = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>活字新报 · %s</title>
<style>%s</style>
</head>
<body>
<div class="stage">
  <div class="sheet alive-img alive-text" id="sheet"><div class="inner">
    %s
    <main class="grid">
      %s%s
      %s
      %s
      %s
    </main>
  </div></div>
</div>
<div class="controls">
  <label><input type="checkbox" id="tImg" checked> 图动</label>
  <label><input type="checkbox" id="tTxt" checked> 字动</label>
  <div class="sep"></div>
  <button id="replay">重播</button>
</div>
<script>
(function(){
  var sheet=document.getElementById('sheet');
  document.getElementById('tImg').addEventListener('change',function(e){sheet.classList.toggle('alive-img',e.target.checked)});
  document.getElementById('tTxt').addEventListener('change',function(e){sheet.classList.toggle('alive-text',e.target.checked);if(e.target.checked)typeHead()});
  var head=document.getElementById('heroHead');
  var full=head.textContent.trim(),typing=false;
  function typeHead(){
    if(typing)return;
    if(!sheet.classList.contains('alive-text')){head.textContent=full;return}
    typing=true;head.textContent='';head.classList.add('typing');
    var caret=document.createElement('span');caret.className='caret';head.appendChild(caret);
    var i=0,timer=setInterval(function(){
      if(i>=full.length){clearInterval(timer);typing=false;head.classList.remove('typing');head.textContent=full;return}
      caret.insertAdjacentText('beforebegin',full.charAt(i));i++;
    },24);
  }
  typeHead();
  document.getElementById('replay').addEventListener('click',function(){
    typing=false;
    sheet.classList.remove('alive-text');void sheet.offsetWidth;sheet.classList.add('alive-text');
    sheet.classList.remove('alive-img');void sheet.offsetWidth;sheet.classList.add('alive-img');
    typeHead();
  });
})();
</script>
</body>
</html>""" % (esc_("活字新报 " + str(today)), CSS, top_html, lead_html, side_html, row3_html, hand_html, footer_html)
    return doc


# ---------------------------------------------------------------- 主流程
def main():
    args = set(sys.argv[1:])
    if "--fetch" in args:
        os.system('python -X utf8 "%s" --hours 48 -o "%s"' %
                  (os.path.join(NEWDESK_DIR, "newsdesk_fetch.py"), DATA))
    sys.path.insert(0, NEWDESK_DIR)
    import newsdesk_scoring

    data = json.load(open(DATA, encoding="utf-8"))
    newsdesk_scoring.annotate(data)
    items = data["items"]
    log("数据 %s | 条目 %d" % (data.get("generated_at", "?")[:16], len(items)))

    cands = coarse(items)
    log("粗筛候选 %d" % len(cands))

    # 决策缓存:同一批候选不重复烧 token
    sig = hashlib.sha1(json.dumps(cands_payload(cands), ensure_ascii=False).encode()).hexdigest()[:12]
    cache = os.path.join(DEC_DIR, "decision_%s.json" % sig)
    dec = None
    if os.path.exists(cache):
        try:
            dec = json.load(open(cache, encoding="utf-8"))
            log("命中决策缓存 %s" % os.path.basename(cache))
        except Exception:
            dec = None
    if dec is None and "--no-llm" not in args:
        dec = llm_edit(cands)
        if dec:
            json.dump(dec, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if dec is None:
        dec = rule_edit(cands)
        log("使用规则排版")
    try:
        ctx = normalize_decision(dec, cands)
    except Exception as e:
        log("决策非法(%s),回退规则排版" % e)
        ctx = normalize_decision(rule_edit(cands), cands)

    # 配图:头版 + 各栏(每栏按分值顺位找非聚合链接)
    plan = [("lead", [ctx["lead"][1]])]
    for key in ("top", "tech", "life", "game"):
        pool = [ctx["by_id"][iid][1] for iid, _ in ctx["cols"][key]]
        if pool:
            plan.append((key, pool))
    images = fetch_images(plan)
    log("配图 %d/%d" % (len(images), len(plan)))

    gen_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    ok_src = sum(1 for s in data.get("sources", []) if s.get("status") in ("ok", "cached"))
    stats = "信息台 %d 源可用 · 全库 %d 条" % (ok_src, len(items))
    period = max(1, (datetime.date.today() - datetime.date(2026, 9, 21)).days + 1)

    doc = render(ctx, images, gen_at, stats, period)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "index.html")
    open(out, "w", encoding="utf-8").write(doc)
    n_img = sum(1 for c in ctx["cols"].values() for _ in c) + 1
    log("成版 -> %s (头版+%d条,配图%d)" % (out, n_img - 1, len(images)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
