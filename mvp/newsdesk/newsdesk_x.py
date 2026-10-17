# -*- coding: utf-8 -*-
"""newsdesk_x -- 信息台的 X / Twitter 源(走 twitter-cli 的登录态 cookie)

为什么这么做(2026-09-21 实测):
  * 免 key 的公开路线全断:RSSHub 404 / Nitter SSL 断 / syndication 429 /
    官方 API 401 —— 详见 README「两个还没解决的源」。
  * twitter-cli 的 cookie(存 `C:\\Users\\Lenovo\\.agent-reach\\config.yaml`)
    到 2026-09-21 仍然有效:`twitter status` = authenticated。
    `feed` / `user-posts` 可用;**`search` 上游 404,且 0.8.5 已是 PyPI 最新
    → 升级无解**,所以本模块只做「关注流 + 指定账号」,不做关键词搜索。
    要搜索只能走 OpenCLI(需在挂扩展的那个 Chrome 里登录 x.com)。
  * x.com 必须走代理:系统代理(WinINET)开着**没有用**,子进程不会继承它,
    必须以环境变量显式注入,否则直连报 getaddrinfo failed。

为什么用 `-o <file>` 而不是捕 stdout:
  ① 沙箱里 python 捕管道会抛 WinError 5(capture_output/PIPE 全禁);
  ② 落盘再读天然留证。实测 `--json -o xxx.json` 写出的就是纯 tweet 数组。

配置:同目录 `x_sources.json`,每次抓取重新读 → 改完保存即生效,不用重启。

自测:
    cmd /c "set PYTHONUTF8=1&& python newsdesk_x.py --test"
"""
import argparse
import html
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
CFG_FILE = HERE / "x_sources.json"
TMP_DIR = HERE / "_x_tmp"
LOG_DIR = HERE / "_x_log"

REACH_CFG = pathlib.Path(
    os.environ.get("AGENT_REACH_CONFIG", r"C:\Users\Lenovo\.agent-reach\config.yaml")
)
PROXY = os.environ.get("TW_PROXY", "http://127.0.0.1:7897")

DEFAULT_CFG = {
    "enabled": True,
    "ttl": 900,
    "feed": {"enabled": True, "name": "X · 关注流", "n": 30},
    "accounts": [],
}


# ---------------------------------------------------------------- 配置
def _load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))          # 深拷贝默认值
    if CFG_FILE.exists():
        try:
            user = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k.startswith("_"):
                    continue
                cfg[k] = v
        except Exception as e:  # noqa: BLE001
            print("[x] x_sources.json 解析失败,用默认配置: %s" % e, file=sys.stderr)
    return cfg


def _read_reach_cfg():
    """读 agent-reach 的 config.yaml(扁平 key: value)。"""
    if not REACH_CFG.exists():
        return {}
    out = {}
    for line in REACH_CFG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------- 可执行文件
def _resolve_exe():
    """定位真正的 twitter-cli。

    只接受 .exe —— Windows 版 shutil.which() 会把**当前目录**插到搜索路径最前,
    同名包装脚本会解析到自己身上(2026-09-21 因此刷出 600 个 python 进程)。
    优先复用 40_工具\\tools\\twitter_run.py 里那份已加固的实现。
    """
    tools = HERE.parent / "tools"
    if tools.is_dir() and str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        import twitter_run  # type: ignore
        exe = twitter_run.find_twitter_exe()
        if exe:
            return exe
    except Exception:  # noqa: BLE001
        pass

    cands = [
        pathlib.Path(os.environ.get("APPDATA", "")) / "Python" / "Python313" / "Scripts" / "twitter.exe",
        pathlib.Path.home() / "AppData" / "Roaming" / "Python" / "Python313" / "Scripts" / "twitter.exe",
    ]
    for c in cands:
        if c.is_file():
            return str(c)
    import shutil
    found = shutil.which("twitter.exe")
    return found if found and found.lower().endswith(".exe") else None


def _child_env():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[k] = PROXY
    rc = _read_reach_cfg()
    if rc.get("twitter_auth_token") and rc.get("twitter_ct0"):
        env["TWITTER_AUTH_TOKEN"] = rc["twitter_auth_token"]
        env["TWITTER_CT0"] = rc["twitter_ct0"]
    return env


# ---------------------------------------------------------------- 调用
def _slug(s):
    return re.sub(r"[^A-Za-z0-9_]+", "_", s or "")[:40] or "x"


def _call_twitter(extra, key, timeout):
    """跑一次 twitter-cli,返回 tweet 列表。输出走 -o 落盘。"""
    exe = _resolve_exe()
    if not exe:
        raise RuntimeError("twitter-cli 未找到(pip install --user twitter-cli)")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = TMP_DIR / ("%s.json" % _slug(key))
    log = LOG_DIR / ("%s.log" % _slug(key))
    if out.exists():
        out.unlink()

    cmd = [exe] + list(extra) + ["--json", "-o", str(out)]
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        subprocess.run(cmd, env=_child_env(), timeout=timeout,
                       stdout=lf, stderr=subprocess.STDOUT)

    if not out.exists() or out.stat().st_size < 2:
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError("twitter-cli 没有产出 JSON: %s" % " / ".join(tail)[:160])

    data = json.loads(out.read_text(encoding="utf-8"))
    if isinstance(data, dict):                      # 防御:可能是 {ok,data:[...]}
        data = data.get("data") or []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- 归一化
def _collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _strip_tco(s):
    """t.co 短链在推文末尾很常见,标题里是噪声(推文本身的 URL 已经能点开)。"""
    s = re.sub(r"https://t\.co/\S+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -·|")


def _num(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return "%.1f万" % (n / 10000.0) if n >= 10000 else str(n)


def _norm(t, src):
    if not isinstance(t, dict):
        return None
    au = t.get("author") or {}
    screen = au.get("screenName") or ""
    tid = t.get("id")
    ts = t.get("createdAtISO") or ""
    # 没有时间戳的条目会被看板丢进「趋势」区,那是错的 —— 直接不要
    if not (tid and screen and ts):
        return None

    media = t.get("media") or []
    # 推文文本里带 HTML 实体(&amp; 之类),RSS 那边由 strip_tags 负责,这里要自己来
    clean = _strip_tco(_collapse(html.unescape(t.get("text") or "")))
    if not clean:
        if not media:
            return None
        label = {"photo": "[图片]", "video": "[视频]", "animated_gif": "[动图]"}.get(
            (media[0] or {}).get("type"), "[媒体]")
        clean = "%s @%s" % (label, screen)

    title = clean if len(clean) <= 140 else clean[:139] + "…"
    m = t.get("metrics") or {}
    bits = ["@" + screen]
    if m.get("likes") is not None:
        bits.append("♥%s" % _num(m.get("likes")))
    if m.get("retweets") is not None:
        bits.append("↻%s" % _num(m.get("retweets")))
    if t.get("isRetweet"):
        bits.append("转推")
    # desc 放**推文全文** + 元数据(2026-09-29 改): 旧版 desc 只有 @与赞转数,
    # 下游"摘要≥40字"过滤会把大部分推文当薄文丢掉,LLM 也只有标题那 140 字可用。
    desc = clean + " ｜ " + " · ".join(bits)

    return {
        "title": title,
        "url": "https://x.com/%s/status/%s" % (screen, tid),
        "ts": ts,
        "desc": desc[:400],
        "src": src["name"],
        "src_id": src["id"],
        "group": src["group"],
    }


# ---------------------------------------------------------------- 对外接口
def sources():
    """按 x_sources.json 生成源定义(与 newsdesk_fetch.SOURCES 同构)。"""
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return []
    ttl = int(cfg.get("ttl", 900))
    out = []

    feed = cfg.get("feed") or {}
    if feed.get("enabled", True):
        out.append({"id": "x_timeline", "name": feed.get("name") or "X · 关注流",
                    "kind": "x", "url": "", "route": "proxy", "group": "社区",
                    "ttl": ttl, "x_mode": "feed", "x_n": int(feed.get("n", 30))})

    for acc in cfg.get("accounts") or []:
        handle = (acc.get("handle") or "").lstrip("@").strip()
        if not handle or not acc.get("enabled", True):
            continue
        sid = "x_" + re.sub(r"[^a-z0-9_]", "", handle.lower())
        out.append({"id": sid, "name": acc.get("name") or ("X · @" + handle),
                    "kind": "x", "url": "", "route": "proxy", "group": "社区",
                    "ttl": ttl, "x_mode": "user-posts", "x_handle": handle,
                    "x_n": int(acc.get("n", 10))})
    return out


def grab(src, timeout=25):
    """抓一个 X 源。返回 (items, via);失败抛异常,由调用方退到过期缓存。"""
    mode = src.get("x_mode") or "feed"
    if mode == "feed":
        raw = _call_twitter(["feed", "-n", str(src.get("x_n", 30))],
                            "feed", timeout)
    else:
        handle = (src.get("x_handle") or "").lstrip("@")
        if not handle:
            raise RuntimeError("x_handle 为空")
        raw = _call_twitter(["user-posts", handle, "-n", str(src.get("x_n", 10))],
                            handle, timeout)

    # 付费推广过滤:Wario64 这类账号把联盟折扣都标 #ad,那些是广告不是新闻
    # (配置项 skip_contains,默认 ["#ad"];不想过滤就把它设成 [])
    cfg = _load_cfg()
    skip = [s.lower() for s in (cfg.get("skip_contains", ["#ad"]) or []) if s]
    items = []
    for t in raw:
        text = (t.get("text") or "").lower()
        if any(s in text for s in skip):
            continue
        it = _norm(t, src)
        if it:
            items.append(it)
    return items, "twitter-cli"


# ---------------------------------------------------------------- 自测
def main():
    ap = argparse.ArgumentParser(description="newsdesk X 源自测")
    ap.add_argument("--test", action="store_true", help="逐个源真跑一次并打印条数")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    srcs = sources()
    print("启用中的 X 源: %d" % len(srcs))
    for s in srcs:
        print("  %-16s %-22s %s n=%s ttl=%s" % (
            s["id"], s["name"], s.get("x_mode"), s.get("x_n"), s.get("ttl")))

    if not args.test:
        return 0
    print("-" * 70)
    for s in srcs:
        try:
            items, via = grab(s, timeout=args.timeout)
            print("[OK] %-16s %d 条 (via %s)" % (s["id"], len(items), via))
            for it in items[:2]:
                print("      %s  %s" % (it["ts"], it["title"][:70]))
        except Exception as e:  # noqa: BLE001
            print("[FAIL] %-16s %s: %s" % (s["id"], type(e).__name__, str(e)[:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
