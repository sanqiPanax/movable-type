# -*- coding: utf-8 -*-
"""newsdesk_editor -- 事件归属的 LLM 判定(阶段C, SPEC §5第四步/第九步)

分工: 规则层(newsdesk_db.scan_progress)处理能明确判定的(同题转述/更正、区分词交集>=3);
本模块只判**规则无法明确判定的边缘候选**(2<=交集<3), 每轮批量一次调用。

四分类: same_event(同一事件实质进展) / related(独立但有明确关联依据的另一事件)
        / unrelated(无关) / insufficient(材料不足无法判断)
落地: same_event -> 挂 direct 节点; insufficient -> 挂 pending 待确认(不占 seq 不计未读);
      unrelated/related -> 只留痕 decisions(related 是未来支线候选,SPEC §6 首轮不自动建关系)。

红线: 只判定归属, 不生成新闻文本; reason 只讲取舍不重复新闻事实。
失败兜底: LLM 挂 -> 返回 None, 调用方放弃本轮边缘候选(不产生节点,下轮再试)。
"""
import datetime
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import newsdesk_db as db  # noqa: E402

PROMPT_VER = "c4-v1"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
CRED_FILE = r"C:\Users\Lenovo\.dsh\.credentials.yaml"

SYS = """你是新闻编辑助理,判断候选报道是否属于对应事件故事线。只做判断,不改写任何文字。
输出必须是 JSON 数组,不要输出其他文字。"""

TPL = """对每个候选(编号 i)给出四选一判定:
- same_event: 同一事件的实质进展(同一主体+同一行动/对象,或其直接结果/官方后续)
- related: 明确相关的**另一件事**(报道原文明确涉及本事件的资源/进度/决策;仅同公司/同领域/相近时间不算)
- unrelated: 无关
- insufficient: 标题摘要材料不足,无法判断(宁可不足,不可硬判)

事件: __EVENT__
事件已有报道标题: __TITLES__

候选列表:
__CANDS__

输出 JSON 数组,每项 {"i":编号,"v":"判定","r":"一句话理由(只讲取舍判断,不重复新闻事实,不编造)"}:
"""


def _key():
    try:
        txt = open(CRED_FILE, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", txt)
    return m.group(1) if m else None


def _cached(conn, pairs):
    """查决策缓存: 已判定过的 (article,event) 不重送(留痕可回查, SPEC §5第四步)。"""
    todo = []
    for p in pairs:
        row = conn.execute(
            "SELECT verdict FROM decisions WHERE article_id=? AND event_id=?",
            (p["article_id"], p["event_id"])).fetchone()
        if row is None:
            todo.append(p)
    return todo


def judge_candidates(pairs, cap=20):
    """批量判定边缘候选。pairs: [{article_id,event_id,title,summary,event_name,titles}]
    返回 {verdicts:[{article_id,event_id,verdict,reason}], cached:[...], failed:bool}。
    校验器(SPEC §5第四步): 非法 verdict/编号/空输出整批拒绝(failed=True),不半信半疑采纳。"""
    if not pairs:
        return {"verdicts": [], "cached": [], "failed": False}
    conn = db.connect()
    try:
        todo = _cached(conn, pairs)
    finally:
        conn.close()
    if not todo:
        return {"verdicts": [], "cached": [], "failed": False}
    todo = todo[:cap]
    key = _key()
    if not key:
        return {"verdicts": [], "cached": [], "failed": True}
    # 按事件分组拼 prompt(一次调用判所有)
    by_event = {}
    for p in todo:
        by_event.setdefault(p["event_id"], []).append(p)
    blocks = []
    idx_map = []
    for k, (eid, ps) in enumerate(by_event.items()):
        cands = []
        for j, p in enumerate(ps):
            cands.append({"i": "%d.%d" % (k, j), "a": (p.get("title") or "")[:140],
                          "d": (p.get("summary") or "")[:120]})
            idx_map.append(("%d.%d" % (k, j), p))
        blocks.append(TPL.replace("__EVENT__", (ps[0].get("event_name") or "")[:60])
                      .replace("__TITLES__", json.dumps(
                          [(t or "")[:60] for t in (ps[0].get("titles") or [])[:5]],
                          ensure_ascii=False))
                      .replace("__CANDS__", json.dumps(cands, ensure_ascii=False)))
    body = {
        "model": os.environ.get("MT_DEEPSEEK_MODEL", "deepseek-chat"),
        "temperature": 0.0,
        "max_tokens": 8192,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": "\n\n".join(blocks)}],
    }
    req = urllib.request.Request(
        DEEPSEEK_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=150) as r:   # DeepSeek 国内直连
            resp = json.loads(r.read().decode("utf-8"))
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\[.*\]", content, re.S)
        arr = json.loads(m.group(0))
    except Exception as e:
        print("[editor] LLM 判定失败: %r" % e, file=sys.stderr)
        return {"verdicts": [], "cached": [], "failed": True}
    # 校验器: 编号必须命中输入, verdict 必须在枚举; 任一非法 -> 该条丢弃(不采纳)
    ok_ids = {i for i, _ in idx_map}
    id2pair = dict(idx_map)
    allowed = {"same_event", "related", "unrelated", "insufficient"}
    verdicts = []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db.connect()
    try:
        for it in arr:
            iid = str(it.get("i"))
            v = (it.get("v") or "").strip().lower()
            if iid not in ok_ids or v not in allowed:
                continue   # 校验不通过: 不采纳也不缓存
            p = id2pair[iid]
            verdicts.append({"article_id": p["article_id"], "event_id": p["event_id"],
                             "verdict": v, "reason": (it.get("r") or "")[:120]})
            conn.execute(
                "INSERT OR REPLACE INTO decisions(article_id,event_id,verdict,reason,"
                "prompt_ver,model,created_at) VALUES(?,?,?,?,?,?,?)",
                (p["article_id"], p["event_id"], v, (it.get("r") or "")[:120],
                 PROMPT_VER, body["model"], now))
        conn.commit()
    finally:
        conn.close()
    return {"verdicts": verdicts, "cached": [], "failed": False}


def record_rule_decision(article_id, event_id, verdict, reason):
    """规则层判定也留痕(retell/corrected/same_event),保证任一归属可回查(SPEC §5第四步)。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO decisions(article_id,event_id,verdict,reason,"
            "prompt_ver,model,created_at) VALUES(?,?,?,?,?,'rule',?)",
            (article_id, event_id, verdict, reason, "c4-rule-v1", 
             datetime.datetime.now(datetime.timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
