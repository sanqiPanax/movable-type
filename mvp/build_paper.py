# -*- coding: utf-8 -*-
"""魔法日报 MVP · movable-type
流程: data/latest.json -> 六维打分 -> 规则粗筛 -> LLM 主编(DeepSeek,失败自动回退规则排版)
      -> og:image 配图(失败退无图版面) -> 多版单文件 HTML (out/index.html)

版面结构(v2, 2026-10-17): 第一版头版=各版最重要新闻摘要; 之后每版一个板块
(科技[软件/硬件] / 游戏 / 金融 / 政治 / 生活), 翻版动画=行级飞散(0.8~1.1s)。

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
PROMPT_VER = "v2pages"          # 提示词版本, 进决策缓存签名, 改提示词自动失效

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROXIES = ["http://127.0.0.1:7897"]
MIN_W = 520
BAD_IMG = ("logo", "placeholder", "default", "avatar", "favicon", "1x1", "spacer")
RE_IMG = [
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'](?![^>]*og:description)[^>]+property=["\']og:image',
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
]
LAZY_ATTRS = ("data-original", "data-src", "data-lazy-src", "data-echo",
              "data-actualsrc", "data-lazyload")

# (key, 版名, 拉丁副名, 观察员署名) —— 渲染顺序即版次顺序(头版永远第一)
SECTIONS = [
    ("tech", "科技", "TECHNOLOGY", "本报科技观察员"),
    ("game", "游戏", "GAME DESK", "本报游戏观察员"),
    ("fin",  "金融", "FINANCE",    "本报财经观察员"),
    ("pol",  "政治", "POLITICS",   "本报时政观察员"),
    ("life", "生活", "LIVING",     "本报生活观察员"),
]
SEC_CAP = {"tech": 8, "game": 6, "fin": 6, "pol": 6, "life": 5}
NAME2KEY = {"科技": "tech", "游戏": "game", "金融": "fin", "政治": "pol", "生活": "life"}
GROUP2KEY = {"科技": "tech", "聚合": "tech", "项目": "tech", "社区": "tech",
             "中文": "tech", "游戏": "game", "金融": "fin", "政治": "pol"}
HARDWARE_RE = re.compile(
    r"chip|semiconductor|gpu|cpu|\bsoc\b|smartphone|\bphone\b|laptop|\bpc\b|"
    r"console|hardware|monitor|camera|chipset|ssd|memory|server|drone|robot|"
    r"芯片|显卡|处理器|手机|笔记本|电脑|主板|显示器|相机|固态|内存|服务器|机器人", re.I)
CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


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


def tech_sub(it):
    hay = (it.get("title") or "") + " " + (it.get("desc") or "")
    return "硬件" if HARDWARE_RE.search(hay) else "软件"


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
EDITOR_TPL = """本期为多版报纸。版面结构与容量:
- 头版:各版最重要新闻的摘要页。lead_id = 全期最重要的一条(通常就是某版的第一条)。
- 科技版 6~8 条:每块必须给 sub("软件"|"硬件")——芯片/手机/PC/相机/服务器等实体设备与半导体→"硬件";AI/应用/编程/互联网/安全/云服务→"软件"。
- 游戏版 4~5 条;金融版 4~5 条;政治版 4~5 条;生活版 3~4 条。总数 20~25。
- 每版内部按重要度降序;每版第一条 = 本版头条(会出现在头版摘要卡)。

选稿规则:
1. 归版按条目内容判断,g(来源分组)仅供参考:消费电子/芯片/PC→科技-硬件;AI/软件/互联网→科技-软件;游戏→游戏;股市/财报/经济/央行政策→金融;选举/法案/外交/政府→政治;社会/消费/健康/其余→生活。
2. 同题重复只上一条;英文条目照原文保留,不翻译;标题党/摘要只等于标题的不上每版头条。
3. dropped:从落选池挑最多 6 条读者会问「为什么没上」的,各写一句中文理由(只讲取舍,不重复新闻事实)。
4. editor_note 一句话讲本期取舍思路(<=50字,中文,不得陈述新闻事实)。

候选条目(字段:i 唯一id / t 标题 / d 摘要 / dl 摘要长度 / s 来源 / g 分组 / sc 评分):
__CANDS__

输出 JSON 结构(name 只能取:科技/游戏/金融/政治/生活;科技块 sub 必填;其余版不要 sub):
{{"lead_id":"iN","sections":[{{"name":"科技","blocks":[{{"item_id":"iN","weight":"feature","sub":"软件"}}]}}],"dropped":[{{"item_id":"iN","reason":"一句话理由"}}],"editor_note":"一句话"}}"""


def llm_edit(cands):
    key = deepseek_key()
    if not key:
        log("无 DeepSeek key,回退规则排版")
        return None
    payload = json.dumps(cands_payload(cands), ensure_ascii=False)
    for attempt in range(2):
        body = {
            "model": os.environ.get("MT_DEEPSEEK_MODEL", "deepseek-chat"),
            "temperature": 0.2,
            "max_tokens": 8192,   # deepseek-flash 是推理模型,输出长必须给足
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
        try:
            with opener().open(req, timeout=150) as r:
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
    """回退:按分组归版,评分顺排,第一条当 lead。"""
    dec = {"lead_id": "i0", "sections": [], "dropped": [],
           "editor_note": "规则排版(未接主编):按评分与分组顺排。"}
    used = {"i0"}
    for k, it in enumerate(cands[1:], 1):
        if len(used) >= 23:
            dec["dropped"].append({"item_id": "i%d" % k, "reason": "版面已满"})
            continue
        key = GROUP2KEY.get(it.get("group"), "life")
        name = next(n for n, kk in NAME2KEY.items() if kk == key)
        sec = next((s for s in dec["sections"] if s["name"] == name), None)
        if sec is None:
            sec = {"name": name, "blocks": []}
            dec["sections"].append(sec)
        block = {"item_id": "i%d" % k, "weight": "brief"}
        if key == "tech":
            block["sub"] = tech_sub(it)
        sec["blocks"].append(block)
        used.add("i%d" % k)
    return dec


def normalize_decision(dec, cands):
    """校验 id/容量;非法抛错回退。
    返回 {lead, cols{key:[(iid,weight,sub)]}, by_id, dropped, note}"""
    ids = {"i%d" % k for k in range(len(cands))}
    by_id = {"i%d" % k: (cands[k], it) for k, it in enumerate(cands)}
    lid = dec.get("lead_id")
    if lid not in ids:
        raise ValueError("lead_id 非法")
    used = set()          # lead 不占版面名额:头版头条通常同时是某版头条
    cols = {k: [] for k, _, _, _ in SECTIONS}
    for sec in dec.get("sections") or []:
        key = NAME2KEY.get(sec.get("name"))
        if key is None:
            continue
        for b in (sec.get("blocks") or [])[:SEC_CAP[key]]:
            iid = b.get("item_id")
            if iid in ids and iid not in used and len(cols[key]) < SEC_CAP[key]:
                sub = b.get("sub") if key == "tech" else None
                if key == "tech" and sub not in ("软件", "硬件"):
                    sub = tech_sub(by_id[iid][1])
                cols[key].append((iid, b.get("weight") or "brief", sub))
                used.add(iid)
    dropped = []
    for d in (dec.get("dropped") or [])[:8]:
        if d.get("item_id") in ids and d.get("item_id") not in used \
           and d.get("item_id") != lid and d.get("reason"):
            dropped.append(d)
    # 空版回填:优先同分组,其次按分;每版至少 2 条(池子不够就尽力)
    rest = sorted((k for k in by_id if k not in used and k != lid),
                  key=lambda x: -by_id[x][0].get("sc", 0))
    for key in cols:
        if cols[key]:
            continue
        name = next(n for n, kk in NAME2KEY.items() if kk == key)
        same = [x for x in rest
                if GROUP2KEY.get(by_id[x][1].get("group"), "life") == key]
        fill = (same + [x for x in rest if x not in same])[:3]
        for iid in fill:
            it = by_id[iid][1]
            sub = tech_sub(it) if key == "tech" else None
            cols[key].append((iid, "brief", sub))
            rest.remove(iid)
    return {"lead": by_id[lid], "lead_id": lid, "cols": cols, "by_id": by_id,
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
    """plan_items: [(slot_name, [item...])] 每槽给候选序列,优先非 Google News。
    同 URL 只抓一次,多槽共享结果。返回 {slot: (fname, credit_host)}"""
    os.makedirs(IMG_DIR, exist_ok=True)
    probe, seen_url, results = {}, {}, {}
    for slot, pool in plan_items:
        for it in pool[:3]:
            url = it.get("url") or ""
            if url and "news.google.com" not in url:
                if url in seen_url:            # 同图复用,不重复下载
                    results[slot] = seen_url[url]
                else:
                    probe[slot] = url
                    seen_url[url] = None       # 占位,完成后回填
                break

    def work(job):
        slot, url = job
        u, data = find_image(url)
        if not data:
            return None
        name = "p_%s.jpg" % re.sub(r"\W+", "", slot)
        open(os.path.join(IMG_DIR, name), "wb").write(data)
        return slot, url, (name, urllib.parse.urlparse(u).netloc.replace("www.", ""))

    if probe:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for r in ex.map(work, probe.items()):
                if r:
                    slot, url, res = r
                    results[slot] = res
                    seen_url[url] = res
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
.stage{display:flex;justify-content:center;padding:26px 16px 96px}
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
nav.tabs{display:flex;justify-content:center;gap:30px;font-size:12px;letter-spacing:.2em;
  border-bottom:1px solid var(--rule);padding:7px 0}
nav.tabs button{font:inherit;background:none;border:0;color:var(--ink);letter-spacing:.2em;cursor:pointer;
  padding:2px 4px;font-family:inherit}
nav.tabs button:hover{color:var(--seal)}
nav.tabs button.on{color:var(--seal);border-bottom:2px solid var(--seal);font-weight:700}
nav.tabs span{color:var(--muted);font-size:9.5px;letter-spacing:.14em;margin-left:5px}
/* ---------- 多版容器 ---------- */
.pages{position:relative}
.page{display:none}
.page.active{display:block}
.pagefolio{display:flex;justify-content:space-between;font-size:11px;letter-spacing:.26em;color:#4c432f;
  border-bottom:1px solid var(--rule);padding:7px 2px;margin-top:16px}
.pagefolio b{color:var(--seal);font-weight:700}
/* ---------- 翻版:行级飞散 ---------- */
.page.pg-out .blk{animation:pgOut .4s cubic-bezier(.5,0,.85,.4) both;
  animation-delay:calc(var(--i,0)*24ms)}
.page.pg-in .blk{animation:pgIn .46s cubic-bezier(.15,.75,.25,1) both;
  animation-delay:calc(var(--i,0)*30ms)}
@keyframes pgOut{to{transform:translate3d(-46px,14px,0) rotate(-1.6deg);opacity:0}}
@keyframes pgIn{from{transform:translate3d(66px,-18px,0) rotate(2.2deg);opacity:0}
  to{transform:none;opacity:1}}
/* ---------- 版面 ---------- */
.grid{display:grid;grid-template-columns:repeat(12,1fr)}
.lead{grid-column:span 8;padding:16px 30px 0 0;border-right:1px solid var(--hair)}
.side{grid-column:span 4;padding:16px 0 0 30px}
.kicker{font-size:11px;letter-spacing:.3em;color:var(--seal);margin-bottom:7px}
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
.fig-sec{height:300px;box-shadow:6px 8px 0 rgba(36,30,22,.15)}
.fig-small{height:128px}
.figcap{font-size:10.5px;color:var(--muted);margin:6px 0 14px;padding-bottom:10px;border-bottom:1px solid var(--hair)}
.figcap .figno{color:var(--seal);letter-spacing:.14em;margin-right:6px}
.body{columns:2;column-gap:28px;font-size:14.5px;line-height:1.8;text-align:justify}
.body p{margin:0 0 11px}
.body p.drop::first-letter{float:left;font-size:52px;line-height:.84;padding:6px 9px 0 0;font-weight:700}
.pullquote{font-family:"Kaiti SC","KaiTi","楷体",serif;font-style:italic;font-size:17px;line-height:1.65;
  color:#33291a;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
  padding:10px 4px;margin:14px 0;column-span:all;text-align:center}
.side h3{font-size:13px;letter-spacing:.3em;margin:0 0 12px;border-bottom:3px double var(--rule);padding-bottom:7px}
.side h3 span{color:var(--muted);font-size:9.5px;letter-spacing:.16em;margin-left:8px}
.brief{border-bottom:1px solid var(--hair);padding:9px 0;break-inside:avoid}
.brief h4{margin:0 0 4px;font-size:15px;line-height:1.45;font-weight:600}
.brief .meta{font-size:10.5px;color:var(--muted);letter-spacing:.06em}
.brief .meta a{color:var(--seal);text-decoration:none}
.tag{display:inline-block;font-size:9.5px;color:var(--paper);background:#4c432f;padding:1px 6px;
  letter-spacing:.1em;margin-right:6px;vertical-align:1px}
.tag.sub{background:var(--seal)}
.digestcard{border:1px solid var(--hair);background:rgba(255,252,240,.45);padding:10px 12px;margin-bottom:10px}
.digestcard .dsec{font-size:10px;letter-spacing:.3em;color:var(--seal);margin-bottom:5px}
.digestcard h4{margin:0 0 5px;font-size:14.5px;line-height:1.5;font-weight:600}
.digestcard .meta{font-size:10px;color:var(--muted)}
.digestcard .meta a{color:var(--seal);text-decoration:none}
.digestcard .same{color:var(--seal);font-size:10px;letter-spacing:.1em}
/* ---------- 内版 ---------- */
.sec-feature{padding-top:16px}
.sec-briefs{columns:2;column-gap:30px;margin-top:6px}
.sec-briefs .brief{break-inside:avoid}
/* ---------- 主编手记 ---------- */
.handnote{grid-column:span 12;margin-top:22px;border:1px solid var(--rule);background:rgba(255,252,240,.5);
  padding:12px 16px;display:grid;grid-template-columns:2fr 1fr;gap:18px}
.handnote h5{margin:0 0 6px;font-size:11.5px;letter-spacing:.3em;color:var(--seal)}
.handnote .note{font-family:"Kaiti SC","KaiTi","楷体",serif;font-size:14.5px;line-height:1.7}
.handnote .drops{font-size:11.5px;color:#4c432f;line-height:1.7}
.handnote .drops li{margin-bottom:3px}
.handnote .drops b{color:var(--seal);font-weight:600}
/* ---------- 阶段B: 追踪 ---------- */
.trk{display:none;font:inherit;font-size:10px;letter-spacing:.1em;color:var(--seal);background:none;
  border:1px solid var(--seal);border-radius:3px;padding:0 7px;cursor:pointer;margin-left:6px;vertical-align:1px}
body.tk-on .trk{display:inline-block}
.trk:hover{background:rgba(158,43,37,.08)}
.trk.done{background:var(--seal);color:var(--paper);cursor:default;opacity:.75}
.trackregion{grid-column:span 12;margin-top:20px;border:1px solid var(--rule);
  background:rgba(255,252,240,.5);padding:12px 16px}
.trackregion h5{margin:0 0 8px;font-size:11.5px;letter-spacing:.3em;color:var(--seal)}
.trackregion h5 a{color:var(--seal);text-decoration:none;letter-spacing:.1em;font-size:10px;margin-left:10px}
.trackregion h5 a:hover{text-decoration:underline}
.trackregion .tempty{font-size:12px;color:var(--muted);line-height:1.7}
.tcard{display:flex;justify-content:space-between;gap:14px;align-items:baseline;
  border-bottom:1px dotted var(--hair);padding:6px 0;font-size:13.5px}
.tcard:last-child{border-bottom:0}
.tcard a{color:var(--ink);text-decoration:none;font-weight:600}
.tcard a:hover{color:var(--seal)}
.tcard .tmeta{font-size:11px;color:var(--muted);white-space:nowrap;letter-spacing:.05em}
.tcard .tmeta b{color:var(--seal);font-size:13px}
footer{margin-top:18px;border-top:1px solid var(--rule);padding-top:8px;
  display:flex;justify-content:space-between;font-size:11px;color:var(--muted);letter-spacing:.1em}
/* ---------- 翻版控件 ---------- */
.pager{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:99;display:flex;gap:14px;
  align-items:center;background:rgba(22,18,12,.93);color:#f2e9d4;border-radius:12px;padding:10px 16px;
  font-family:system-ui,"Microsoft YaHei",sans-serif;font-size:13px;box-shadow:0 12px 34px rgba(0,0,0,.45)}
.pager button{font:inherit;background:#e0a94a;color:#1a1610;border:0;border-radius:7px;padding:6px 13px;
  cursor:pointer}
.pager button:disabled{opacity:.35;cursor:default}
.pager .pinfo{min-width:150px;text-align:center;letter-spacing:.12em}
.pager .pinfo b{color:#e0a94a}
.controls{position:fixed;right:20px;bottom:20px;z-index:99;display:flex;gap:15px;align-items:center;
  background:rgba(22,18,12,.93);color:#f2e9d4;border-radius:12px;padding:11px 15px;
  font-family:system-ui,"Microsoft YaHei",sans-serif;font-size:13px;box-shadow:0 12px 34px rgba(0,0,0,.45)}
.controls label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
.controls input{accent-color:#e0a94a;cursor:pointer}
.controls button{font:inherit;background:#e0a94a;color:#1a1610;border:0;border-radius:7px;padding:5px 11px;cursor:pointer}
.controls .sep{width:1px;height:22px;background:rgba(255,255,255,.2)}
/* ---------- 动效层 ---------- */
.alive-text .ink{clip-path:inset(0 100% -8% 0);animation:ink .8s cubic-bezier(.22,.9,.2,1) both;animation-delay:var(--d,0ms)}
@keyframes ink{to{clip-path:inset(0 -3% -8% 0)}}
.caret{display:inline-block;width:2px;height:.92em;background:var(--seal);vertical-align:-.08em;margin-left:2px;opacity:0}
.typing .caret{opacity:1;animation:blink .7s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
@media (prefers-reduced-motion: reduce){
  .alive-text .ink{animation:none;clip-path:none}
  .alive-img .figwrap img{animation:none}
  .alive-text .ticker .track{animation:none}
  .page.pg-out .blk,.page.pg-in .blk{animation:none}
}
@media (max-width:820px){
  .lead,.side{grid-column:span 12;border-right:0;padding:14px 0 0}
  .body,.sec-briefs{columns:1}
  .mast-center h1{font-size:44px}
  .couplet,.seal{display:none}
  .handnote{grid-template-columns:1fr}
  .controls{display:none}
}
"""


def render(ctx, images, gen_at, stats, period):
    lead_it = ctx["lead"][1]
    lead_id = ctx["lead_id"]

    esc_ = esc
    fig_no = [0]

    def figblock(slot, item, cls, fb_host, override_slot=None):
        use_slot = override_slot or slot
        if use_slot in images:
            fname, host = images[use_slot]
        elif slot in images:
            fname, host = images[slot]
        else:
            return "", ""
        fig_no[0] += 1
        fig = ('<figure class="figwrap %s alive-img ink" style="--d:60ms">'
               '<img src="img/%s" alt="" loading="lazy"></figure>' % (cls, fname))
        cap_html = ('<div class="figcap"><span class="figno">图 %02d</span>图源 %s · 本报灰度处理</div>'
                    % (fig_no[0], esc_(host)))
        return fig, cap_html

    def trk_btn(it):
        """阶段B: 追踪按钮。article_id 走与库内一致的规范链接身份;
        身份算不出就不放按钮(宁缺勿错)。默认 CSS 隐藏,页面确认服务可用才点亮。"""
        try:
            from newsdesk_db import article_id_for, canonical_url
            c = canonical_url(it.get("url"))
            if not c:
                return ""
            aid = article_id_for(c)
        except Exception:
            return ""
        return ('<button class="trk" data-aid="%s" data-title="%s">追踪</button>'
                % (aid, esc_((it.get("title") or "")[:60])))

    def brief_html(it, delay, show_sub=None):
        subtag = ('<span class="tag sub">%s</span>' % esc_(show_sub)) if show_sub else \
                 ('<span class="tag">%s</span>' % esc_((it.get("src") or "?").split(" · ")[0][:6]))
        return ('<article class="brief blk ink" style="--d:%dms"><h4>%s</h4>'
                '<div class="meta">%s%s · <a href="%s" target="_blank" rel="noopener">原文</a>%s</div></article>'
                % (delay, esc_(it.get("title")), subtag, esc_(meta_line(it)),
                   esc_(it.get("url")), trk_btn(it)))

    # ---- 非空版(渲染顺序=版次顺序)
    live_secs = [(k, n, l, b) for (k, n, l, b) in SECTIONS if ctx["cols"][k]]

    # ================= 第一版:头版 =================
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
    # 正文只接导语没讲完的部分,避免与导语开头重复(v1 用户反馈)
    body_rest = desc[110:] if len(desc) > 150 else ""
    if body_rest.startswith(("。", ",", "，")):
        body_rest = body_rest[1:]
    hero_fig, hero_cap = figblock("lead", lead_it, "fig-hero", domain(lead_it.get("url") or ""))
    body_p = ('<p class="drop ink" style="--d:160ms">%s</p>' % esc_(body_rest[:300])) if len(body_rest) > 30 else ""
    lead_html = """
<section class="lead blk">
  <div class="kicker">头版 · THE LEAD%s</div>
  <h2 class="headline" id="heroHead">%s</h2>
  %s
  <div class="byline">本报首席记者 · 综合%s 报道　%s　|　<a href="%s" target="_blank" rel="noopener">原文 @ %s</a>%s</div>
  %s%s
  <div class="body">%s</div>
</section>""" % (kicker_ent, h_html, stand, esc_((lead_it.get("src") or "").split(" · ")[0]),
                 esc_((lead_it.get("ts") or "")[:16].replace("T", " ")),
                 esc_(lead_it.get("url")), esc_(domain(lead_it.get("url") or "")),
                 trk_btn(lead_it),
                 hero_fig, hero_cap, body_p + (pull if pull else ""))

    # 头版侧栏 = 本版各版头条摘要卡
    digest = []
    for k, name, latin, _b in live_secs:
        iid, _w, _sub = ctx["cols"][k][0]
        it = ctx["by_id"][iid][1]
        same = '<span class="same">◆ 即上方头条</span>' if iid == lead_id else ""
        digest.append(
            '<div class="digestcard blk ink" style="--d:%dms">'
            '<div class="dsec">%s %s</div><h4>%s</h4>'
            '<div class="meta">%s · <a href="%s" target="_blank" rel="noopener">原文</a>%s　%s</div></div>'
            % (160 + len(digest) * 90, esc_(name), esc_(latin), esc_(it.get("title")),
               esc_(meta_line(it)), esc_(it.get("url")), trk_btn(it), same))
    side_html = """
<aside class="side blk">
  <h3>本版各版头条 <span>SECTION LEADS</span></h3>
  %s
</aside>""" % "".join(digest)

    # 主编手记(仅头版)
    drops_html = "".join("<li><b>弃</b> %s — %s</li>" % (esc_(ctx["by_id"][d["item_id"]][1].get("title"))[:46],
                                                         esc_(d["reason"])) for d in ctx["dropped"][:4])
    hand_html = """
<section class="handnote blk">
  <div><h5>主编手记 · EDITOR'S NOTE</h5><div class="note">%s</div></div>
  <div><h5>取舍留痕 · WHY DROPPED</h5><ul class="drops">%s</ul></div>
</section>""" % (esc_(ctx["note"]) or "本期依六维评分与版面容量取舍。", drops_html or "<li>无</li>")

    # 阶段B: 追踪进展区(只读渲染由 JS 拉 /api/follows; 服务不在则显示只读说明)
    track_html = """
<section class="trackregion blk" id="trackRegion">
  <h5>追踪进展 · TRACKED <a href="/follows" id="allFollows" style="display:none">全部故事线 →</a></h5>
  <div class="tempty" id="trackBody">正在检查本地追踪服务…</div>
</section>"""

    page0 = ('<section class="page active" data-label="第一版 · 头版">'
             '<div class="grid">%s%s%s%s</div></section>'
             % (lead_html, side_html, hand_html, track_html))

    # ================= 内版:每版一个板块 =================
    pages = [page0]
    for pi, (key, name, latin, byline) in enumerate(live_secs, start=1):
        items = ctx["cols"][key]
        iid0, _w0, _sub0 = items[0]
        fit = ctx["by_id"][iid0][1]
        # 头条图:如果本版头条就是头版大图那条,复用 p_lead
        ov = "lead" if iid0 == lead_id else key
        f_fig, f_cap = figblock(key, fit, "fig-sec", domain(fit.get("url") or ""), override_slot=ov)
        fdesc = (fit.get("desc") or "").strip()
        if fdesc.startswith((fit.get("title") or "")[:20]):
            fdesc = fdesc[len((fit.get("title") or "")[:20]):].lstrip(" 　-—|")
        f_stand = ('<p class="standfirst ink" style="--d:120ms">%s</p>' % esc_(fdesc[:130])) \
            if len(fdesc) > 24 else ""
        feature = """
<div class="sec-feature blk">
  <div class="kicker">%s · 本版头条 · %s</div>
  <h2 class="headline" style="font-size:32px">%s</h2>
  %s
  <div class="byline">%s · <a href="%s" target="_blank" rel="noopener">原文 @ %s</a>　%s%s</div>
  %s%s
</div>""" % (esc_(name), esc_(byline), esc_(fit.get("title")), f_stand,
                 esc_(byline), esc_(fit.get("url")), esc_(domain(fit.get("url") or "")),
                 esc_(meta_line(fit)), trk_btn(fit), f_fig, f_cap)
        briefs = []
        for k2, (iid, _w, sub) in enumerate(items[1:], 1):
            it = ctx["by_id"][iid][1]
            briefs.append(brief_html(it, 200 + k2 * 70, show_sub=(sub if key == "tech" else None)))
        page = ('<section class="page" data-label="第%s版 · %s">'
                '<div class="pagefolio"><span><b>第%s版</b></span><span>活字新报 · %s %s</span>'
                '<span>第 %d 期 · %s</span></div>'
                '%s<div class="sec-briefs">%s</div></section>'
                % (CN_NUM[pi], esc_(name), CN_NUM[pi], esc_(name), esc_(latin),
                   period, datetime.date.today().strftime("%Y-%m-%d"),
                   feature, "".join(briefs)))
        pages.append(page)

    # ================= 顶部/底部/控件 =================
    ticker_items = [lead_it] + [ctx["by_id"][ctx["cols"][k][0][0]][1] for k, _n, _l, _b in live_secs]
    tk = "".join('<span class="tk">%s</span>' % esc_((i.get("title") or "")[:48]) for i in ticker_items[:7])
    today = datetime.date.today()
    wd = "一二三四五六日"[today.weekday()]
    all_items = [ctx["by_id"][iid][1] for col in ctx["cols"].values() for iid, _w, _s in col]
    read_min = max(1, round(sum(len(i.get("title") or "") + len(i.get("desc") or "")
                                for i in all_items) / 300))
    n_items = len(all_items)
    npages = len(pages)

    tabs = ['<button data-go="0" class="on">头版<span>FRONT</span></button>']
    for i, (_k, name, latin, _b) in enumerate(live_secs, start=1):
        tabs.append('<button data-go="%d">%s<span>%s</span></button>' % (i, esc_(name), esc_(latin)))

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
    <span>第 %d 期 · 共 %d 版</span>
    <span>%d 年 %d 月 %d 日 · 星期%s</span>
    <span>本期 %d 条 · 约 %d 分钟 · 定价:一盏茶</span>
  </div>
  <div class="ticker"><div class="track"><span class="tk">%s</span><span class="tk">%s</span></div></div>
  <nav class="tabs">%s</nav>
</header>""" % (period, npages, today.year, today.month, today.day, wd,
                 n_items, read_min, tk, tk, "".join(tabs))

    footer_html = """
<footer>
  <span>生成于 %s · movable-type MVP</span>
  <span>%s · 配图 og:image · 灰度统一</span>
</footer>""" % (gen_at, esc_(stats))

    pager_html = """
<div class="pager">
  <button id="pgPrev">&#9664; 上一版</button>
  <span class="pinfo" id="pgInfo">第 <b>1</b> / %d 版 · 头版</span>
  <button id="pgNext">下一版 &#9654;</button>
</div>""" % npages

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
    <main class="pages" id="pages">
      %s
    </main>
    %s
  </div></div>
</div>
%s
<div class="controls">
  <label><input type="checkbox" id="tImg" checked> 图动</label>
  <label><input type="checkbox" id="tTxt" checked> 字动</label>
  <div class="sep"></div>
  <button id="replay">重播</button>
</div>
<script>
(function(){
  var sheet=document.getElementById('sheet');
  var pages=[].slice.call(document.querySelectorAll('.page'));
  var tabs=[].slice.call(document.querySelectorAll('nav.tabs button'));
  var cur=0,busy=false;
  var labels=pages.map(function(p){return p.getAttribute('data-label')||('第'+(pages.indexOf(p)+1)+'版')});
  var reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function assignI(page){
    var blks=[].slice.call(page.querySelectorAll('.blk'));
    blks.forEach(function(el,i){el.style.setProperty('--i',i)});
    return blks;
  }
  function updPager(){
    document.getElementById('pgInfo').innerHTML='第 <b>'+(cur+1)+'</b> / '+pages.length+' 版 · '+labels[cur];
    document.getElementById('pgPrev').disabled=(cur===0);
    document.getElementById('pgNext').disabled=(cur===pages.length-1);
    tabs.forEach(function(b,i){b.classList.toggle('on',i===cur)});
  }
  function go(n){
    if(busy||n===cur||n<0||n>=pages.length)return;
    var out=pages[cur],inn=pages[n];
    if(reduced){
      out.classList.remove('active');inn.classList.add('active');
      cur=n;updPager();return;
    }
    busy=true;
    assignI(out);out.classList.add('pg-out');
    setTimeout(function(){
      out.classList.remove('active','pg-out');
      assignI(inn);inn.classList.add('active','pg-in');
      cur=n;updPager();
      if(cur===0)typeHead();
      setTimeout(function(){inn.classList.remove('pg-in');busy=false;},620);
    },430);
  }
  document.getElementById('pgPrev').addEventListener('click',function(){go(cur-1)});
  document.getElementById('pgNext').addEventListener('click',function(){go(cur+1)});
  tabs.forEach(function(b,i){b.addEventListener('click',function(){go(i)})});
  document.addEventListener('keydown',function(e){
    if(e.key==='ArrowRight'||e.key==='PageDown'){go(cur+1)}
    else if(e.key==='ArrowLeft'||e.key==='PageUp'){go(cur-1)}
  });

  document.getElementById('tImg').addEventListener('change',function(e){sheet.classList.toggle('alive-img',e.target.checked)});
  document.getElementById('tTxt').addEventListener('change',function(e){sheet.classList.toggle('alive-text',e.target.checked);if(e.target.checked)typeHead()});
  var head=document.getElementById('heroHead');
  var full=head?head.textContent.trim():'',typing=false;
  function typeHead(){
    if(!head||typing)return;
    if(!sheet.classList.contains('alive-text')){head.textContent=full;return}
    typing=true;head.textContent='';head.classList.add('typing');
    var caret=document.createElement('span');caret.className='caret';head.appendChild(caret);
    var i=0,timer=setInterval(function(){
      if(i>=full.length){clearInterval(timer);typing=false;head.classList.remove('typing');head.textContent=full;return}
      caret.insertAdjacentText('beforebegin',full.charAt(i));i++;
    },24);
  }
  typeHead();updPager();
  document.getElementById('replay').addEventListener('click',function(){
    typing=false;
    sheet.classList.remove('alive-text');void sheet.offsetWidth;sheet.classList.add('alive-text');
    sheet.classList.remove('alive-img');void sheet.offsetWidth;sheet.classList.add('alive-img');
    typeHead();
  });

  /* ---- 阶段B 追踪接线: 服务不在 -> 诚实只读,不装能保存(SPEC §3) ---- */
  var TCAND=[7777,8090,9090,9527,8642];   /* 与 track_server 的 MT_TRACK_PORTS 默认一致 */
  var TBASE=null;
  var TOKEL=document.querySelector('meta[name="track-token"]');
  var TOK=TOKEL?TOKEL.getAttribute('content'):null;
  var tbody=document.getElementById('trackBody');
  function escj(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
    return c==='&'?'&amp;':c==='<'?'&lt;':c==='>'?'&gt;':'&quot;';});}
  function ro(msg){ if(tbody) tbody.innerHTML='<b style="color:#9e2b25">只读模式</b> · '+msg;
    document.getElementById('allFollows').style.display='none'; }
  function loadFollows(){
    fetch(TBASE+'/api/follows?status=active').then(function(r){return r.json()}).then(function(fs){
      if(!fs.length){ tbody.innerHTML='还没有追踪的事件。点新闻旁的「追踪」关注一条故事线。';
        document.getElementById('allFollows').style.display=''; return; }
      document.getElementById('allFollows').style.display='';
      tbody.innerHTML=fs.map(function(f){
        var d=(f.last_node_at||f.followed_at||'').slice(0,10);
        return '<div class="tcard"><a href="'+TBASE+'/storyline/'+f.event_id+'">'+escj(f.display_name)+
          '</a><span class="tmeta">未读 <b>'+(f.unread||0)+'</b> · 更新 '+d+'</span></div>';
      }).join('');
      // 已追踪事件的报道按钮置灰
      var tracked={};
      fs.forEach(function(f){ (f.article_ids||[]).forEach(function(a){tracked[a]=1}) });
      [].forEach.call(document.querySelectorAll('.trk'),function(b){
        if(tracked[b.getAttribute('data-aid')]){b.classList.add('done');b.textContent='已追踪'}
      });
    }).catch(function(){ ro('本地追踪服务未启动。用 <b>看报纸.cmd</b> 打开即可追踪;直接开 HTML 文件只能看。') });
  }
  function probe(i){
    if(i>=TCAND.length){
      ro('本地追踪服务未启动。用 <b>看报纸.cmd</b> 打开即可追踪;直接开 HTML 文件只能看。'); return; }
    TBASE='http://127.0.0.1:'+TCAND[i];
    fetch(TBASE+'/api/status').then(function(r){return r.json()}).then(function(st){
      if(!st.ok) throw 0;
      if(!TOK){ ro('页面未经追踪服务打开,保存功能锁定。请访问 '+TBASE+'/ 使用完整功能。'); return; }
      document.body.classList.add('tk-on');
      tbody.innerHTML='正在加载…';
      loadFollows();
      // 追踪按钮(只绑一次:服务找到后不再重复探测)
      document.addEventListener('click',function(e){
        var b=e.target.closest?e.target.closest('.trk'):null;
        if(!b||b.classList.contains('done'))return;
        var aid=b.getAttribute('data-aid'), t=b.getAttribute('data-title')||'';
        if(!window.confirm('追踪这件事?\\n\\n「'+t+'」\\n\\n它的新进展将进入你的追踪区。'))return;
        b.disabled=true;
        fetch(TBASE+'/api/track',{method:'POST',headers:{'Content-Type':'application/json',
          'X-Track-Token':TOK},body:JSON.stringify({article_id:aid})})
        .then(function(r){return r.json()}).then(function(j){
          b.disabled=false;
          if(j.error){ alert('未保存:'+j.error); return; }
          b.classList.add('done'); b.textContent='已追踪';
          loadFollows();
        }).catch(function(){ b.disabled=false; alert('未保存:服务不可达,请重试'); });
      });
    }).catch(function(){ probe(i+1); });
  }
  probe(0);
})();
</script>
</body>
</html>""" % (esc_("活字新报 第%d期" % period), CSS, top_html, "".join(pages),
                 footer_html, pager_html)
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

    # 阶段A: 报道身份入库(幂等)。DB 挂了不许拖垮出报(规格 §1: 缓存新闻仍可生成报纸)
    try:
        import newsdesk_db
        r = newsdesk_db.import_snapshot(data)
        log("入库: 新报道 %d | 升版本 %d | 跳过 %d | run#%d"
            % (r["new"], r["updated"], r["skipped"], r["run_id"]))
    except Exception as e:
        log("入库失败(不影响出报): %r" % e)
        newsdesk_db = None

    # 阶段B: 活跃事件的直接进展扫描(SPEC §9: 每天随出报执行一次;失败不拖垮出报)
    if newsdesk_db is not None:
        try:
            sc = newsdesk_db.scan_progress()
            if sc["added"]:
                log("进展扫描: %d 活跃事件, 新增节点 %d %s"
                    % (len(sc["per_event"]), sc["added"],
                       {k: v for k, v in sc["per_event"].items() if v}))
            else:
                log("进展扫描: %d 活跃事件, 无新增" % len(sc["per_event"]))
        except Exception as e:
            log("进展扫描失败(不影响出报): %r" % e)

    cands = coarse(items)
    log("粗筛候选 %d" % len(cands))

    # 决策缓存:签名含提示词版本,改提示词自动失效
    sig = hashlib.sha1((PROMPT_VER + json.dumps(cands_payload(cands),
                                                ensure_ascii=False)).encode()).hexdigest()[:12]
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

    # 配图:头版大图 + 每个非空版(同 URL 复用)
    plan = [("lead", [ctx["lead"][1]])]
    for key, _n, _l, _b in SECTIONS:
        pool = [ctx["by_id"][iid][1] for iid, _w, _s in ctx["cols"][key]]
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
    n_pages = 1 + sum(1 for k, _n, _l, _b in SECTIONS if ctx["cols"][k])
    n_items = sum(len(c) for c in ctx["cols"].values())
    log("成版 -> %s (%d 版, %d 条, 配图 %d)" % (out, n_pages, n_items, len(images)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
