# -*- coding: utf-8 -*-
"""track_server -- 事件追踪阶段B: 本地追踪服务(SPEC §3/§6)

设计约束(规格原文落地):
- 只监听 127.0.0.1; 修改操作校验请求来源(Origin)与本地会话凭据(启动时随机 token,
  只注入经本服务打开的页面)。
- 数据库是状态来源; 页面是产物。服务挂 -> 报纸静态打开仍可读,追踪区显示只读,不装能保存。
- 支线/历史回溯是阶段E, 归属LLM/转述合并是阶段C —— 本服务不实现,不预留假UI。

启动: python -X utf8 track_server.py     (或双击 看报纸.cmd, 自动拉起)
端口: 8801 (环境变量 MT_TRACK_PORT 可改)
"""
import datetime
import html
import io
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import newsdesk_db as db  # noqa: E402

PORT_CANDIDATES = [int(x) for x in
                   os.environ.get("MT_TRACK_PORTS", "7777,8090,9090,9527,8642").split(",")]
PORT = None                         # bind 成功后回填(动态端口: Windows 会预留端口段)
TOKEN = secrets.token_hex(16)
INDEX = os.path.join(HERE, "out", "index.html")
IMG_DIR = os.path.join(HERE, "out", "img")


def _allowed_origin():
    """按当前实际端口算白名单(bind 后 PORT 才有值,不能在 import 时算死)。"""
    if not PORT:
        return set()
    return {"http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}


def _probe_status(port):
    """该端口上是否已有本服务在跑。返回响应体(含 pid)或 None。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/status" % port, timeout=1.2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def esc(s):
    return html.escape(str(s or ""), quote=True)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- 页面
PAGE_CSS = """
:root{--paper:#f4eeda;--ink:#1b1712;--muted:#6d6353;--rule:#241e16;--hair:#cbbfa5;--seal:#9e2b25;--bg:#3a3226}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Songti SC","SimSun","Noto Serif CJK SC",Georgia,serif;-webkit-font-smoothing:antialiased}
.stage{display:flex;justify-content:center;padding:30px 16px 70px}
.sheet{width:960px;max-width:100%;background:var(--paper);padding:26px 34px 30px;position:relative;
  box-shadow:0 18px 54px rgba(0,0,0,.45)}
.sheet::after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.3;mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/><feColorMatrix type='saturate' values='0'/></filter><rect width='200' height='200' filter='url(%23n)' opacity='.4'/></svg>")}
.inner{position:relative;z-index:1}
h1{font-size:30px;letter-spacing:.24em;margin:0 0 4px}
.sub{font-size:11px;letter-spacing:.5em;color:var(--muted);margin-bottom:14px}
.rule{border-top:3px solid var(--rule);border-bottom:1px solid var(--rule);height:3px;margin:10px 0 16px}
.back{font-size:12px;letter-spacing:.14em;color:var(--seal);text-decoration:none}
.back:hover{text-decoration:underline}
.card{border:1px solid var(--hair);background:rgba(255,252,240,.5);padding:12px 15px;margin-bottom:12px}
.card h3{margin:0 0 8px;font-size:17px;line-height:1.5;font-weight:700}
.card h3 a{color:var(--ink);text-decoration:none}
.card h3 a:hover{color:var(--seal)}
.mrow{display:flex;justify-content:space-between;gap:12px;font-size:11.5px;color:var(--muted);
  letter-spacing:.06em;margin-bottom:6px}
.badge{display:inline-block;font-size:10px;letter-spacing:.12em;border:1px solid var(--seal);color:var(--seal);
  padding:1px 7px;margin-right:8px;vertical-align:2px}
.badge.arch{border-color:var(--muted);color:var(--muted)}
.badge.unread{background:var(--seal);color:var(--paper);border-color:var(--seal)}
.note{font-size:12.5px;color:var(--muted);line-height:1.7}
.tl{list-style:none;margin:0;padding:0}
.tl li{position:relative;padding:0 0 16px 22px;border-left:1px solid var(--rule);margin-left:6px}
.tl li:last-child{border-left-color:transparent}
.tl li::before{content:"";position:absolute;left:-5px;top:5px;width:9px;height:9px;background:var(--seal);
  border-radius:50%}
.tl li.origin::before{background:var(--paper);border:2px solid var(--seal)}
.tl .tdate{font-size:11px;color:var(--muted);letter-spacing:.08em}
.tl .ttitle{font-size:15.5px;line-height:1.55;font-weight:600;margin:2px 0}
.tl .ttitle a{color:var(--ink);text-decoration:none}
.tl .ttitle a:hover{color:var(--seal)}
.tl .tmeta{font-size:11px;color:var(--muted)}
.tl details.srcs{margin:4px 0 0;font-size:11.5px}
.tl details.srcs summary{color:var(--seal);cursor:pointer;letter-spacing:.08em}
.tl details.srcs .srcrow{padding:3px 0 2px 12px;border-left:1px dotted var(--hair);margin:3px 0}
.tl details.srcs .srcrow a{color:var(--ink);text-decoration:none}
.tl details.srcs .srcrow a:hover{color:var(--seal)}
.tl details.srcs .sm{color:var(--muted);font-size:10.5px}
.tl li.unread .ttitle a{color:var(--seal)}
.tl li.boundary-li{border-left-color:transparent;padding-bottom:10px}
.tl li.boundary-li::before{display:none}
.boundary{margin:2px 0 14px 22px;font-size:11px;letter-spacing:.2em;color:var(--seal);
  border-top:1px dashed var(--seal);padding-top:4px}
.btns{margin:16px 0 6px;display:flex;gap:12px;align-items:center}
.btns button{font:inherit;font-size:13px;font-family:inherit;background:var(--seal);color:var(--paper);
  border:0;padding:7px 16px;cursor:pointer;letter-spacing:.1em}
.btns button.ghost{background:none;color:var(--seal);border:1px solid var(--seal)}
.btns button:disabled{opacity:.5;cursor:default}
.btns .msg{font-size:12px;color:var(--muted)}
.sect-h{font-size:12px;letter-spacing:.3em;color:var(--seal);margin:22px 0 10px;
  border-bottom:3px double var(--rule);padding-bottom:6px}
.empty{font-size:13px;color:var(--muted);padding:8px 0}
"""


def page(title, inner):
    return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style>"
            "<meta name='track-token' content='%s'></head><body><div class='stage'>"
            "<div class='sheet'><div class='inner'>%s</div></div></div></body></html>"
            ) % (esc(title), PAGE_CSS, TOKEN, inner)


def follows_page():
    active = db.list_follows("active")
    archived = db.list_follows("archived")
    h = ["<a class='back' href='/'>← 返回日报</a>", "<h1>追踪列表</h1>",
         "<div class='sub'>FOLLOWED EVENTS</div><div class='rule'></div>"]
    h.append("<div class='sect-h'>进行中</div>")
    if not active:
        h.append("<div class='empty'>还没有追踪的事件。回日报点新闻旁的「追踪」。</div>")
    for f in active:
        last = (f.get("last_node_at") or f.get("followed_at") or "")[:10]
        h.append(
            "<div class='card'><div class='mrow'><span>关注于 %s</span><span>最近更新 %s</span></div>"
            "<h3><span class='badge'>%d 未读</span><a href='/storyline/%s'>%s</a></h3>"
            "<div class='mrow'><span>更新水位 seq=%d · 已读至 %d</span>"
            "<span><a href='/storyline/%s' style='color:var(--seal)'>进入脉络 →</a></span></div></div>"
            % (esc((f.get("followed_at") or "")[:10]), esc(last), f.get("unread") or 0,
               esc(f["event_id"]), esc(f.get("display_name")), f.get("max_seq") or 0,
               f.get("last_read_seq") or 0, esc(f["event_id"])))
    h.append("<div class='sect-h'>已归档</div>")
    if not archived:
        h.append("<div class='empty'>没有归档。</div>")
    for f in archived:
        h.append(
            "<div class='card'><div class='mrow'><span>归档于 %s</span><span>冻结于 seq=%s</span></div>"
            "<h3><span class='badge arch'>已归档</span>"
            "<a href='/storyline/%s'>%s</a></h3>"
            "<div class='note'>归档后不再更新;故事线保留可重读。首轮不支持恢复追踪。</div></div>"
            % (esc((f.get("archived_at") or "")[:10]), esc(f.get("last_visible_seq")),
               esc(f["event_id"]), esc(f.get("display_name"))))
    return page("追踪列表 · 活字新报", "".join(h))


def storyline_html(event_id):
    """渲染完整时间线页(boundary 插入 li 序列内)。"""
    conn = db.connect()
    try:
        r = conn.execute(
            "SELECT * FROM follows WHERE user_local='local' AND event_id=?",
            (event_id,)).fetchone()
        if r is None:
            return None, 404
        follow = dict(r)
    finally:
        conn.close()
    read_seq = follow.get("last_read_seq") or 0
    st = db.storyline(event_id, read_seq=read_seq)
    if "error" in st:
        return None, 404
    ev, nodes = st["event"], st["nodes"]
    is_arch = follow["status"] == "archived"
    if is_arch:
        cap = follow.get("last_visible_seq") or 0
        nodes = [n for n in nodes if (n.get("seq") or 0) <= cap]
    unread = sum(1 for n in nodes if n.get("is_unread"))

    head = ["<a class='back' href='/follows'>← 追踪列表</a>　<a class='back' href='/'>← 日报</a>",
            "<h1>%s</h1>" % esc(ev.get("display_name")),
            "<div class='sub'>EVENT STORYLINE · %s</div><div class='rule'></div>" % esc(event_id)]
    if is_arch:
        head.append(
            "<div class='note' style='margin-bottom:10px'><span class='badge arch'>已归档</span>"
            "归档于 %s,内容冻结于更新序号 %s,不再产生新进展。</div>"
            % (esc((follow.get("archived_at") or "")[:10]), esc(follow.get("last_visible_seq"))))
    else:
        head.append(
            "<div class='mrow'><span>关注于 %s</span><span>未读 %d · 水位 %d · 已读至 %d</span></div>"
            % (esc((follow.get("followed_at") or "")[:16]).replace("T", " ", 1),
               unread, ev.get("max_seq") or 0, read_seq))

    items = []
    boundary_drawn = False
    for n in nodes:
        if not is_arch and n.get("node_type") != "origin" and not boundary_drawn:
            items.append("<li class='boundary-li'><div class='boundary'>"
                         "以下为你开始追踪之后收录的进展</div></li>")
            boundary_drawn = True
        ts = n.get("occurred_at") or n.get("published_at") or ""
        ts_s = (ts or "")[:16].replace("T", " ")
        prec = n.get("occurred_precision") or "unknown"
        if not ts_s:
            tshow = "时间未知"
        else:
            tshow = esc(ts_s)
            if prec == "unknown":
                tshow += " <span style='color:var(--seal)'>(发生时间未知,按发布时间)</span>"
        cls = []
        if n.get("node_type") == "origin":
            cls.append("origin")
        if n.get("is_unread"):
            cls.append("unread")
        url = n.get("url_canonical") or n.get("url_original") or "#"
        btxt = ("未读" if n.get("is_unread")
                else ("创始报道" if n.get("node_type") == "origin" else "进展"))
        bcls = "unread" if n.get("is_unread") else ""
        # 来源证据链(C: 转述合并 -> 同一节点多来源, 可展开逐条溯源 SPEC §5第五步)
        srcs = n.get("sources") or []
        if len(srcs) > 1:
            rows_s = "".join(
                "<div class='srcrow'><a href='%s' target='_blank' rel='noopener'>%s</a>"
                " <span class='sm'>%s · %s</span></div>"
                % (esc(s.get("url_canonical") or s.get("url_original") or "#"),
                   esc((s.get("title") or "")[:80]), esc(s.get("source_id")),
                   esc((s.get("published_at") or "")[:16]).replace("T", " "))
                for s in srcs)
            srchtml = ("<details class='srcs'><summary>来源 %d 篇(转述已合并,"
                       "展开逐条溯源)</summary>%s</details>"
                       % (len(srcs), rows_s))
        else:
            srchtml = ""
        items.append(
            "<li class='%s'><div class='tdate'>%s <span class='badge %s'>%s</span></div>"
            "<div class='ttitle'><a href='%s' target='_blank' rel='noopener'>%s</a></div>"
            "<div class='tmeta'>来源 %s · 发表 %s · 收录于 %s</div>%s</li>"
            % (" ".join(cls), tshow, bcls, btxt, esc(url), esc(n.get("title")),
               esc(n.get("source_id")), esc((n.get("published_at") or "")[:16]).replace("T", " "),
               esc((n.get("created_at") or "")[:16]).replace("T", " "), srchtml))

    if is_arch:
        btns = ("<div class='btns'><span class='msg'>已归档事件为只读快照;"
                "首轮不提供恢复追踪。</span></div>")
    else:
        btns = ("<div class='btns'>"
                "<button id='btnRead'%s>标记本次已读</button>"
                "<button id='btnArch' class='ghost'>结束追踪并归档</button>"
                "<span class='msg' id='bmsg'></span></div>"
                % (" disabled" if unread == 0 else ""))
    js = ("<script>"
          "var TOK=document.querySelector('meta[name=track-token]').content;"
          "function post(u,b,cb){fetch(u,{method:'POST',headers:{'Content-Type':'application/json',"
          "'X-Track-Token':TOK},body:JSON.stringify(b||{})}).then(function(r){return r.json()})"
          ".then(cb).catch(function(e){cb({error:'服务不可达'})})}"
          "var br=document.getElementById('btnRead');"
          "if(br)br.addEventListener('click',function(){br.disabled=true;"
          "post('/api/read',{event_id:'%s'},function(j){var m=document.getElementById('bmsg');"
          "if(j.error){m.textContent='未保存:'+j.error;br.disabled=false}"
          "else{m.textContent='已读至 seq='+j.last_read_seq;setTimeout(function(){location.reload()},600)}})});"
          "var ba=document.getElementById('btnArch');"
          "if(ba)ba.addEventListener('click',function(){"
          "if(!confirm('结束追踪并归档?故事线会保留为只读快照,不再更新。'))return;"
          "post('/api/untrack',{event_id:'%s'},function(j){var m=document.getElementById('bmsg');"
          "if(j.error){m.textContent='未保存:'+j.error}"
          "else{m.textContent='已归档';setTimeout(function(){location.reload()},600)}})})"
          "</script>") % (esc(event_id), esc(event_id))

    inner = "".join(head) + btns + "<ul class='tl'>" + "".join(items) + "</ul>" + js
    return page("故事脉络 · 活字新报", inner), 200


# ---------------------------------------------------------------- API
def _json(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


class Server(ThreadingHTTPServer):
    """覆写两处稳健性:
    - handle_error: 客户端断开是常态,一行日志代替整版 traceback(日志噪音治理)
    - 日志走 try 的 log_message(见 Handler): 日志管道断了不许拖死请求线程
      (实测: 后台任务的日志管道被切断后,log_message 抛异常 -> 整个请求线程挂掉 ->
       服务看似活着却不回话, 连环坑)"""
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            sys.stderr.write("[track] client gone: %r\n" % exc)
        else:
            sys.stderr.write("[track] request error: %r\n" % (exc,))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            sys.stderr.write("[track] %s - %s\n" % (self.address_string(), fmt % args))
        except Exception:
            pass   # 日志失败绝不影响响应(见 Server 类注释)

    # ---- CORS 预检(file:// 静态打开时的探测; 修改仍要 token+Origin)
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Track-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_write(self):
        """SPEC §3: 修改操作校验请求来源 + 本地会话凭据。"""
        if self.headers.get("X-Track-Token") != TOKEN:
            return False, "会话凭据无效(请通过本服务打开日报)"
        origin = self.headers.get("Origin") or ""
        if origin and origin not in _allowed_origin():
            return False, "请求来源不被接受"
        return True, None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 65536)) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _file(self, path, ctype):
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            if not os.path.isfile(INDEX):
                body = ("<!DOCTYPE html><meta charset='utf-8'><body style='font-family:sans-serif;"
                        "padding:40px'>还没出过报。先双击「出新一期.cmd」生成本期。</body>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            with io.open(INDEX, encoding="utf-8") as f:
                html_txt = f.read()
            # 注入会话 token: 只有经服务打开的页面才有保存凭据
            html_txt = html_txt.replace("</head>",
                                        "<meta name='track-token' content='%s'></head>" % TOKEN, 1)
            if "track-token" not in html_txt:
                html_txt += "<meta name='track-token' content='%s'>" % TOKEN
            data = html_txt.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/follows":
            self._html(follows_page())
            return
        if path.startswith("/storyline/"):
            event_id = urllib.parse.unquote(path[len("/storyline/"):])
            body, code = storyline_html(event_id)
            if body is None:
                self._html("<h1>404</h1><p class='note'>没有这个事件或你没有追踪它。</p>", 404)
            else:
                self._html(body, code)
            return
        if path.startswith("/img/"):
            name = os.path.basename(path[len("/img/"):])
            if self._file(os.path.join(IMG_DIR, name), "image/jpeg"):
                return
            self.send_error(404)
            return
        # ---- API(读接口不需要 token)
        if path == "/api/status":
            _json(self, 200, {"ok": True, "phase": "B", "pid": os.getpid(),
                              "time": _now()})
            return
        if path == "/api/follows":
            q = urllib.parse.parse_qs(parsed.query)
            status = (q.get("status") or ["active"])[0]
            rows = db.list_follows(status if status in ("active", "archived") else "active")
            # 附带每条事件的报道集合, 供日报页把已追踪按钮置灰
            conn = db.connect()
            try:
                for r in rows:
                    r["article_ids"] = [x["article_id"] for x in conn.execute(
                        "SELECT article_id FROM nodes WHERE event_id=?",
                        (r["event_id"],)).fetchall()]
            finally:
                conn.close()
            _json(self, 200, rows)
            return
        if path.startswith("/api/storyline/"):
            event_id = urllib.parse.unquote(path[len("/api/storyline/"):])
            st = db.storyline(event_id)
            if "error" in st:
                _json(self, 404, st)
            else:
                _json(self, 200, st)
            return
        self.send_error(404)

    def _html(self, html_txt, code=200):
        data = html_txt.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        ok, why = self._auth_write()
        if not ok:
            _json(self, 403, {"error": why})
            return
        body = self._body()
        if path == "/api/track":
            aid = (body.get("article_id") or "").strip()
            if not aid:
                _json(self, 400, {"error": "缺少 article_id"})
                return
            _json(self, 200, db.track_article(aid))
            return
        if path == "/api/untrack":
            eid = (body.get("event_id") or "").strip()
            if not eid:
                _json(self, 400, {"error": "缺少 event_id"})
                return
            _json(self, 200, db.untrack_event(eid))
            return
        if path == "/api/read":
            eid = (body.get("event_id") or "").strip()
            if not eid:
                _json(self, 400, {"error": "缺少 event_id"})
                return
            _json(self, 200, db.mark_read(eid))
            return
        self.send_error(404)


def main():
    global PORT
    want_open = "--open" in sys.argv
    # 1) 已有健康实例 -> 幂等:只把浏览器带过去(SPEC §3 不起双份状态)
    for p in PORT_CANDIDATES:
        st = _probe_status(p)
        if st and st.get("ok"):
            PORT = p
            url = "http://127.0.0.1:%d/" % p
            print("[track] 服务已在运行: %s (pid=%s)" % (url, st.get("pid")), flush=True)
            if want_open:
                webbrowser.open(url)
            return 0
    # 2) 逐候选端口 bind。Windows 有保留端口段(Hyper-V 等),单端口写死必踩坑:
    #    10013=系统保留段/权限, 10048=真被占用 -> 都换下一个。
    # 3) 防双绑竞态: 先 bind 再起服务线程, **服务应答后再自探**(先探会把死连接排进
    #    backlog,起服务时对端已断,徒增 10053)。响应者 pid 不是自己 -> 让位换端口。
    srv = None
    for p in PORT_CANDIDATES:
        try:
            cand = Server(("127.0.0.1", p), Handler)
        except OSError as e:
            print("[track] 端口 %d 不可用(%s),换下一个" % (p, e), flush=True)
            continue
        PORT = p
        threading.Thread(target=cand.serve_forever, daemon=True).start()
        st = _probe_status(p)
        if st is not None and st.get("pid") not in (None, os.getpid()):
            print("[track] 端口 %d 由其他实例应答(pid=%s),让位换下一个"
                  % (p, st.get("pid")), flush=True)
            try:
                cand.shutdown()
                cand.server_close()
            except Exception:
                pass
            continue
        srv = cand
        break
    if srv is None:
        print("[track] 所有候选端口都失败,请设 MT_TRACK_PORTS=其他端口再试", flush=True)
        return 1
    url = "http://127.0.0.1:%d/" % PORT
    print("[track] 本地追踪服务: %s (Ctrl+C 停止)" % url, flush=True)
    print("[track] 数据库: %s" % db.DEFAULT_DB, flush=True)
    if want_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        # serve_forever 已在守护线程跑; 主线程只负责存活与 Ctrl+C
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("[track] 已停止", flush=True)
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
