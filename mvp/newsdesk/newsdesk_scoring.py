# -*- coding: utf-8 -*-
"""newsdesk_scoring -- 新闻重要性打分(2026-09-19 建)

回答的问题:「这条新闻刚出现、还没有热度时,值不值得写」。
核心思想:热度是滞后指标,重要性靠结构性信号提前估计。

六个维度(权重在 scoring_config.json,可改,总和按 100 归一):

  src  信源等级   源的权威度(techmeme/ars=75 ... reddit=35)
  imp  影响面     命中实体表(Steam/任天堂/英伟达...),权重 x impact_scale
  ex   独家性     标题聚类后的独立信源数:1 家=100,越多越低
  ev   证据强度   措辞分级:官宣 > 引语 > 默认 > 传闻
  irr  可逆性     一次性事件(收购/停服/漏洞)100,例行更新 35,传闻 20
  hk   后续钩子   有时间表(日期/发布会/财报)或争议点 -> 会有第二波

减分项:旧闻翻新(发布超 old_news_hours);弱信源单源独家。
产出字段(直接长在条目上,随 _latest.json 持久化):
  score   0-100 总分
  score_d 各维明细 {src,imp,ex,ev,irr,hk,pen}
  score_f 减分/提示标记列表
  score_e 命中的实体名(便于人肉核对)

打分时机:
  - newsdesk_server.do_fetch 落盘前 -> 分数持久化
  - newsdesk_server.load_disk 读旧数据时 -> 内存补打分,不动磁盘
每次 annotate() 整体重算,不做增量,幂等。
配置文件按 mtime 热加载,改 scoring_config.json 不用重启服务。
"""
import difflib
import json
import pathlib
import re
import time

HERE = pathlib.Path(__file__).resolve().parent
CFG_FILE = HERE / "scoring_config.json"

_CACHE = {"mtime": None, "cfg": None, "kw": None, "ent": None}

_DATE_RE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\bQ[1-4]\b|20\d{2}\s*年|\d{4}-\d{1,2}-\d{1,2}")

_SIM_THRESHOLD = 0.60   # 标题相似度聚类阈值(实测 0.55 会把不同报道误并,0.65 漏并)
_QUICK_GATE = 0.60      # quick_ratio 预筛门


# ------------------------------------------------------------------ 配置
def _compile_words(words):
    """把关键词表编译成一个正则。

    纯 ASCII 词用 \b 边界(防止 ban 误中 bank / fine 误中 fine-tuning),
    含 CJK 的词用普通子串。全部小写化,匹配时 haystack 也已小写。
    """
    parts = []
    for w in words or []:
        w = (w or "").strip().lower()
        if not w:
            continue
        if re.fullmatch(r"[a-z0-9 .+\-']+", w):
            parts.append(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])")
        else:
            parts.append(re.escape(w))
    if not parts:
        return None
    return re.compile("|".join("(?:%s)" % p for p in parts))


def _build_entities(cfg):
    """实体表 -> [(compiled_regex, 展示名, 权重)]"""
    ents = []
    for e in cfg.get("entities", []):
        w = e.get("w", 5)
        for name in e.get("names", []):
            n = (name or "").strip().lower()
            if not n:
                continue
            if re.fullmatch(r"[a-z0-9 .+\-']+", n):
                pat = re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])")
            else:
                pat = re.compile(re.escape(n))
            ents.append((pat, name, w))
    return ents


def _cfg():
    """读配置(带 mtime 热加载);失败时退空配置,打分退化为中性,不崩服务。"""
    try:
        mt = CFG_FILE.stat().st_mtime
    except OSError:
        mt = None
    if _CACHE["cfg"] is not None and _CACHE["mtime"] == mt:
        return _CACHE["cfg"]
    try:
        cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        _CACHE["mtime"] = mt
    except Exception:  # noqa: BLE001
        cfg = _CACHE["cfg"] or {}
        _CACHE["mtime"] = None
    _CACHE["cfg"] = cfg
    _CACHE["kw"] = {
        "evidence": [(r.get("score", 40), _compile_words(r.get("words")))
                     for r in cfg.get("evidence_rules", [])],
        "irr": [(r.get("score", 50), _compile_words(r.get("words")))
                for r in cfg.get("irreversibility_rules", [])],
        "hook_time": _compile_words(cfg.get("hook_time_words")),
        "hook_heat": _compile_words(cfg.get("hook_controversy_words")),
        "rumor_strong": _compile_words(cfg.get("rumor_markers")),
        "rumor_score": next((r.get("score", 22) for r in cfg.get("evidence_rules", [])
                             if "rumor" in (r.get("words") or [])), 22),
    }
    _CACHE["ent"] = _build_entities(cfg)
    return cfg


# ------------------------------------------------------------------ 工具
def _norm_title(t):
    """标题归一化:去 Google News 的「 - 媒体名」尾巴、去标点、压空白。"""
    t = (t or "").strip().lower()
    parts = t.split(" - ")
    if len(parts) >= 2 and len(parts[-1]) <= 40:
        t = " - ".join(parts[:-1])
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _clusters(titles):
    """标题相似度聚类,返回 {root: [idx...]}。

    O(n^2) 但有长度差预筛 + quick_ratio 预筛,n≈350 时单次 <2s,
    且只在抓取落盘/换数据时算一次,不在每次请求里算。
    """
    n = len(titles)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    sm = difflib.SequenceMatcher(None)
    for j in range(n):
        if len(titles[j]) < 4:
            continue
        sm.set_seq2(titles[j])
        lj = len(titles[j])
        for i in range(j):
            li = len(titles[i])
            if li < 4 or abs(li - lj) > 0.45 * max(li, lj):
                continue
            sm.set_seq1(titles[i])
            if sm.quick_ratio() < _QUICK_GATE:
                continue
            if sm.ratio() >= _SIM_THRESHOLD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return groups


# ------------------------------------------------------------------ 打分
def _score_one(it, cs, now_ts):
    cfg = _cfg()
    kw = _CACHE["kw"] or {}
    title = it.get("title") or ""
    desc = it.get("desc") or ""
    hay = (title + " " + desc).lower()

    # 1. 信源等级
    tier = (cfg.get("source_tiers") or {}).get(it.get("src_id") or "", {})
    src_s = tier.get("score", 50)

    # 2. 影响面(实体表,取最高权重;只匹配标题——摘要里顺嘴提到的
    #    OpenAI/苹果会把无关新闻的影响面抬高,2026-09-19 真实数据实测)
    hits = []
    wmax = 0
    thl = title.lower()
    for pat, name, w in (_CACHE["ent"] or []):
        if pat.search(thl):
            hits.append(name)
            if w > wmax:
                wmax = w
    imp_s = cfg.get("impact_none", 20) if wmax == 0 else \
        min(100, wmax * cfg.get("impact_scale", 14))

    # 3. 证据强度(规则按序,首个命中生效)
    #    传闻短路:标题/摘要里出现显式传闻词时,直接按传闻级,
    #    防止 "Rumor: X may unveil Y" 被后面的官宣词(unveil)抢先命中。
    ev_s = 40
    rumor_rx = kw.get("rumor_strong")
    hit_rumor = rumor_rx is not None and rumor_rx.search(hay)
    if hit_rumor:
        ev_s = kw.get("rumor_score", 22)
    else:
        for score, rx in kw.get("evidence", []):
            if rx is not None and rx.search(hay):
                ev_s = score
                break

    # 4. 可逆性
    irr_s = 50
    for score, rx in kw.get("irr", []):
        if rx is not None and rx.search(hay):
            irr_s = score
            break

    # 5. 后续钩子(只看标题,防摘要里的日期误触发)
    ttl = title.lower()
    has_time = bool(kw.get("hook_time") and kw["hook_time"].search(ttl)) \
        or bool(_DATE_RE.search(title))
    has_heat = bool(kw.get("hook_heat") and kw["hook_heat"].search(ttl))
    if has_time and has_heat:
        hk_s = 100
    elif has_time:
        hk_s = 85
    elif has_heat:
        hk_s = 65
    else:
        hk_s = 10

    # 6. 独家性(聚类后独立信源数)
    ex_map = cfg.get("exclusivity") or {"1": 100, "2": 70, "3": 50, "4": 35, "5+": 20}
    ex_s = ex_map.get(str(cs))
    if ex_s is None:
        ex_s = ex_map.get("5+", 20) if cs >= 5 else 50

    w = cfg.get("weights") or {"source": 25, "impact": 20, "exclusivity": 15,
                               "evidence": 15, "irreversibility": 10, "hooks": 15}
    total = (w.get("source", 25) * src_s + w.get("impact", 20) * imp_s +
             w.get("exclusivity", 15) * ex_s + w.get("evidence", 15) * ev_s +
             w.get("irreversibility", 10) * irr_s + w.get("hooks", 15) * hk_s) / 100.0

    # 减分项
    flags, pen = [], 0
    ep = it.get("_epoch")
    if ep:
        age_h = (now_ts - ep) / 3600.0
        if age_h > cfg.get("old_news_hours", 48):
            pen += cfg.get("old_news_penalty", -20)
            flags.append("发布已%.0f小时" % age_h)
    thin = cfg.get("thin_source") or {"tier_score_max": 30, "penalty": -15}
    if cs == 1 and src_s <= thin.get("tier_score_max", 30):
        pen += thin.get("penalty", -15)
        flags.append("弱信源单源")

    total = max(0, min(100, total + pen))
    return {
        "score": int(round(total)),
        "score_d": {"src": src_s, "imp": imp_s, "ex": ex_s, "ev": ev_s,
                    "irr": irr_s, "hk": hk_s, "pen": pen},
        "score_f": flags,
        "score_e": hits[:4],
    }


# ------------------------------------------------------------------ 入口
def annotate(data):
    """给 data['items'] 与 data['trends'] 的每条打分(原地修改,返回 data)。

    幂等:重复调用整体重算并覆盖,不会叠加减分。
    """
    cfg = _cfg()
    if not cfg:
        return data
    now = time.time()
    items = data.get("items") or []
    trends = data.get("trends") or []
    pools = items + trends
    if not pools:
        return data
    groups = _clusters([_norm_title(i.get("title")) for i in pools])
    for idxs in groups.values():
        cs = len({pools[k].get("src_id") for k in idxs})
        for k in idxs:
            pools[k]["_cs"] = cs
    for it in pools:
        cs = it.pop("_cs", 1)
        it.update(_score_one(it, cs, now))
    return data
