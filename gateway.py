"""
言雾网关 ASGI 中间件
====================
- 修正反代 Host 头
- CORS 预检
- API 安全校验
- OpenAI 兼容代理 + 智能体模式（夸窗口记忆核心）
- NapCat QQ 反向 WS 端点
- AI小屋前端 (/house)
- 网易云音乐播放器 (/music)
- Tidefall 身体状态面板 (/tidefall)
"""

import os
import json
import asyncio
import time
import datetime
import requests
import base64
import random


_supabase_client = None
_system_logs_buffer = []
_MAX_LOGS = 200
_pending_save_tasks = set()

# 网易云 weapi 加密常数
_WAPI_PRESET_KEY = b"#13%8d2c5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0t1u2v3w4x5y6z7A8B9C0D1E2F3G4H5I6J7K8L9M0N1O2P3Q4R5S6T7U8V9W0X1Y2Z3a4b5c6d7e8f9g0h1i2j3k4l5m6n7o8p9q0r1s2t3u4v5w6x7y8z"
_WAPI_PRESET_IV = b"0102030405060708"
_WAPI_PUB_KEY = "010001"
_WAPI_MODULUS = "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"


def _log(msg: str):
    line = f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _system_logs_buffer.append(line)
    if len(_system_logs_buffer) > _MAX_LOGS:
        del _system_logs_buffer[: len(_system_logs_buffer) - _MAX_LOGS]


def _weapi_encrypt(data: dict) -> dict:
    """网易云 weapi 加密，返回 {params, encSecKey}"""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        _log("⚠️ pycryptodome 未安装，回退明文")
        return {"params": json.dumps(data, separators=(",", ":")), "encSecKey": ""}

    text = json.dumps(data, separators=(",", ":"))
    sec_key = "".join(random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))

    def _aes_encrypt(plain: str, key: bytes, iv: bytes) -> str:
        pad = 16 - len(plain) % 16
        plain += chr(pad) * pad
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return base64.b64encode(cipher.encrypt(plain.encode("utf-8"))).decode("utf-8")

    def _rsa_encrypt(text: str, pub_key: str, modulus: str) -> str:
        rev_bytes = text[::-1].encode("utf-8")
        m = int(rev_bytes.hex(), 16)
        e = int(pub_key, 16)
        n = int(modulus, 16)
        return format(pow(m, e, n), "x").zfill(256)

    enc_text = _aes_encrypt(text, _WAPI_PRESET_KEY[:16], _WAPI_PRESET_IV)
    enc_text = _aes_encrypt(enc_text, sec_key.encode("utf-8"), _WAPI_PRESET_IV)
    enc_sec_key = _rsa_encrypt(sec_key, _WAPI_PUB_KEY, _WAPI_MODULUS)

    return {"params": enc_text, "encSecKey": enc_sec_key}


def _get_supabase():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    try:
        import server
        if getattr(server, "supabase", None) is not None:
            _supabase_client = server.supabase
            return _supabase_client
    except Exception as e:
        _log(f"⚠️ 复用 server.supabase 失败: {e}")
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
    except Exception as e:
        _log(f"❌ Supabase 连接失败: {e}")
        _supabase_client = None
    return _supabase_client


def _fmt_time(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso_str).replace('Z', '+00:00'))
        return (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
    except Exception:
        return str(iso_str)[:16]


def _read_html(filename):
    try:
        base = os.path.dirname(__file__)
        path = os.path.join(base, filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        _log(f"⚠️ 读取 {filename} 失败: {e}")
        return f"<h1>加载失败</h1><p>{e}</p>"


def _netease_api(path, params=None, data=None, method="GET"):
    cookie = os.environ.get("NETEASE_COOKIE", "").strip()
    if not cookie:
        return {"error": "未配置 NETEASE_COOKIE"}
    url = f"https://music.163.com{path}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
        "Referer": "https://music.163.com/",
        "Cookie": cookie,
    }
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            resp = requests.post(url, headers=headers, data=data, params=params, timeout=15)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def _netease_weapi_api(path, data: dict):
    """使用 weapi 加密 POST 请求网易云 API"""
    cookie = os.environ.get("NETEASE_COOKIE", "").strip()
    if not cookie:
        return {"error": "未配置 NETEASE_COOKIE"}
    encrypt_data = _weapi_encrypt(data)
    url = f"https://music.163.com{path}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
        "Referer": "https://music.163.com/",
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = requests.post(url, headers=headers, data=encrypt_data, timeout=15)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


class HostFixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and scope["path"] == "/qq-ws":
            try:
                import napcat
                await napcat.handle_napcat_ws(scope, receive, send)
            except Exception as e:
                _log(f"❌ NapCat WS 处理异常: {e}")
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        if path == "/":
            html = "<h1>🚪 言雾网关</h1><p>Endpoints: <code>/health</code> <code>/sse</code> <code>/v1/chat/completions</code> <code>/house</code> <code>/music</code> <code>/tidefall</code></p>"
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": html.encode("utf-8")})
            return

        if path == "/health":
            await _send_json_resp(send, 200, {"status": "ok", "service": "budwg-gateway"})
            return

        if path == "/house":
            await self._handle_house_page(send)
            return

        if path == "/api/house":
            await self._handle_house_api(send)
            return

        if path == "/api/tidefall/state":
            if scope["method"] == "OPTIONS":
                await _send_cors_preflight(send)
                return
            await self._handle_tidefall_state(send)
            return

        if path == "/music":
            await self._handle_music_page(send)
            return

        if path == "/tidefall":
            await self._handle_tidefall_page(send)
            return

        if path.startswith("/api/music/"):
            if scope["method"] == "OPTIONS":
                await _send_cors_preflight(send)
                return
            await self._handle_music_api(path, send)
            return

        if path.startswith("/v1/"):
            if scope["method"] == "OPTIONS":
                await _send_cors_preflight(send)
                return
            await self._handle_openai_proxy(scope, receive, send)
            return

        if (path.startswith("/api/") or path.startswith("/sse") or path.startswith("/messages")) and scope["method"] != "OPTIONS":
            if not await _check_api_secret(scope, send):
                return

        if scope["method"] == "OPTIONS":
            await _send_cors_preflight(send)
            return

        if path == "/api/logs":
            await self._handle_logs(send)
            return

        headers = dict(scope.get("headers", []))
        headers[b"host"] = b"localhost:8000"
        scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

    async def _handle_tidefall_state(self, send):
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {"error": "数据库未连接"})
            return
        try:
            def _fetch_state():
                r = sb.table("eventide_body_state").select("*").limit(1).execute()
                return r.data[0] if r and r.data else None
            def _fetch_snapshots():
                r = sb.table("eventide_snapshots").select("ts,heat,pressure,possessiveness").order("ts", desc=True).limit(96).execute()
                return r.data if r and r.data else []
            def _fetch_events():
                r = sb.table("eventide_event_log").select("event_key,started_at,ended_at").order("ended_at", desc=True).limit(8).execute()
                return r.data if r and r.data else []
            state = await asyncio.to_thread(_fetch_state)
            snapshots = await asyncio.to_thread(_fetch_snapshots)
            eventLog = await asyncio.to_thread(_fetch_events)
            await _send_json_resp(send, 200, {
                "state": state,
                "snapshots": snapshots,
                "eventLog": eventLog,
            })
        except Exception as e:
            _log(f"❌ Tidefall state 错误: {e}")
            await _send_json_resp(send, 200, {"error": str(e)})

    async def _handle_music_page(self, send):
        html = _read_html("music.html")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": html.encode("utf-8")})

    async def _handle_tidefall_page(self, send):
        html = _read_html("tidefall.html")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": html.encode("utf-8")})

    async def _handle_music_api(self, path, send):
        import urllib.parse
        parsed = urllib.parse.urlparse(f"http://localhost{path}")
        qs = urllib.parse.parse_qs(parsed.query)
        route = parsed.path.replace("/api/music/", "")

        try:
            if route == "search":
                keyword = qs.get("keyword", [""])[0]
                if not keyword:
                    await _send_json_resp(send, 200, {"result": {"songs": []}})
                    return
                data = {"s": keyword, "type": 1, "limit": 20, "offset": 0}
                result = await asyncio.to_thread(_netease_api, "/api/search/get", data=data, method="POST")
                await _send_json_resp(send, 200, result)

            elif route == "playlists":
                uid = qs.get("uid", [""])[0]
                if not uid:
                    result = await asyncio.to_thread(_netease_api, "/api/user/playlist", {"limit": 30, "offset": 0})
                else:
                    result = await asyncio.to_thread(_netease_api, "/api/user/playlist", {"uid": uid, "limit": 30, "offset": 0})
                if "playlist" in result:
                    pls = []
                    for p in result["playlist"]:
                        pls.append({
                            "id": p["id"],
                            "name": p["name"],
                            "coverImg": p.get("coverImgUrl", ""),
                            "trackCount": p.get("trackCount", 0),
                        })
                    await _send_json_resp(send, 200, {"playlists": pls})
                else:
                    await _send_json_resp(send, 200, {"playlists": []})

            elif route == "playlist/detail":
                pid = qs.get("id", [""])[0]
                if not pid:
                    await _send_json_resp(send, 200, {"playlist": {"tracks": []}})
                    return
                # 使用 weapi 加密请求，避免返回空数据
                result = await asyncio.to_thread(
                    _netease_weapi_api,
                    "/weapi/v6/playlist/detail",
                    {"id": pid, "n": 100000, "s": 8, "t": -1}
                )
                await _send_json_resp(send, 200, result)

            elif route == "song/url":
                sid = qs.get("id", [""])[0]
                if not sid:
                    await _send_json_resp(send, 200, {"data": []})
                    return
                result = await asyncio.to_thread(
                    _netease_api, "/api/song/enhance/player/url",
                    {"id": sid, "ids": f"[{sid}]", "br": 320000}
                )
                await _send_json_resp(send, 200, result)

            elif route == "lyric":
                sid = qs.get("id", [""])[0]
                if not sid:
                    await _send_json_resp(send, 200, {"lrc": {"lyric": ""}})
                    return
                result = await asyncio.to_thread(
                    _netease_api, "/api/song/lyric",
                    {"id": sid, "lv": 1, "kv": 1, "tv": -1}
                )
                await _send_json_resp(send, 200, result)

            else:
                await _send_json_resp(send, 404, {"error": f"Unknown music API: {route}"})
        except Exception as e:
            _log(f"❌ Music API 错误 ({route}): {e}")
            await _send_json_resp(send, 500, {"error": str(e)})

    async def _handle_house_api(self, send):
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {"error": "数据库未连接", "records": []})
            return
        try:
            def _fetch():
                return sb.table("memory_house").select("*").order("created_at", desc=True).limit(50).execute()
            res = await asyncio.to_thread(_fetch)
            records = []
            if res and res.data:
                for r in res.data:
                    records.append({
                        "id": r.get("id"),
                        "room": r.get("room", "未知"),
                        "action": r.get("action_type", "活动"),
                        "content": r.get("content", ""),
                        "time": _fmt_time(r.get("created_at")),
                    })
            await _send_json_resp(send, 200, {"records": records})
        except Exception as e:
            await _send_json_resp(send, 200, {"error": str(e), "records": []})

    async def _handle_house_page(self, send):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>🏡 言雾的小屋</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,"Noto Serif SC","Microsoft YaHei",serif;
  background:#f5efe6;
  min-height:100vh;
  color:#4a3728;
  padding:0 16px 32px;
  background-image:radial-gradient(circle at 20% 30%, rgba(200,170,140,0.12) 0%, transparent 50%),
                   radial-gradient(circle at 80% 70%, rgba(200,170,140,0.08) 0%, transparent 50%);
}
.container{max-width:480px;margin:0 auto}
.header{
  text-align:center;
  padding:28px 16px 16px;
  position:relative;
}
.header::after{
  content:'';
  display:block;
  width:60px;
  height:2px;
  background:linear-gradient(90deg,transparent,#c4a67a,transparent);
  margin:12px auto 0;
}
.header .avatar{
  width:64px;height:64px;
  background:linear-gradient(135deg,#e8d5b7,#c4a67a);
  border-radius:50%;
  margin:0 auto 10px;
  display:flex;align-items:center;justify-content:center;
  font-size:30px;
  box-shadow:0 4px 20px rgba(196,166,122,0.4);
  border:2px solid rgba(255,255,255,0.5);
}
.header h1{
  font-size:20px;font-weight:600;
  color:#5a4030;letter-spacing:1px;
}
.header .subtitle{
  font-size:12px;color:#a09080;
  margin-top:4px;letter-spacing:0.5px;
}
.rooms{
  display:flex;gap:6px;
  justify-content:center;flex-wrap:wrap;
  margin:16px 0 20px;
}
.room-tag{
  background:rgba(196,166,122,0.15);
  border:1px solid rgba(196,166,122,0.25);
  border-radius:18px;
  padding:5px 12px;
  font-size:12px;
  cursor:pointer;
  transition:all 0.2s;
  color:#7a6a5a;
}
.room-tag:hover,.room-tag.active{
  background:rgba(196,166,122,0.3);
  border-color:#c4a67a;
  color:#4a3728;
}
.timeline{position:relative;padding-left:28px}
.timeline::before{
  content:'';
  position:absolute;
  left:10px;top:8px;bottom:8px;
  width:2px;
  background:linear-gradient(180deg,#c4a67a,rgba(196,166,122,0.3));
  border-radius:1px;
}
.card{
  position:relative;
  margin-bottom:16px;
  background:rgba(255,255,255,0.7);
  border:1px solid rgba(196,166,122,0.2);
  border-radius:14px;
  padding:14px 16px;
  transition:all 0.2s;
  backdrop-filter:blur(4px);
  box-shadow:0 2px 8px rgba(74,55,40,0.06);
}
.card:hover{
  background:rgba(255,255,255,0.85);
  border-color:rgba(196,166,122,0.35);
  box-shadow:0 4px 16px rgba(74,55,40,0.1);
}
.card::before{
  content:'';
  position:absolute;
  left:-24px;top:16px;
  width:9px;height:9px;
  background:#c4a67a;
  border-radius:50%;
  border:2px solid #f5efe6;
  box-shadow:0 0 0 2px rgba(196,166,122,0.3);
}
.card .time{
  font-size:11px;
  color:#a09080;
  margin-bottom:4px;
  font-family:-apple-system,sans-serif;
}
.card .room-label{
  font-size:12px;
  color:#8a7a6a;
  margin-bottom:5px;
  display:flex;align-items:center;gap:4px;
}
.card .room-label .emoji{font-size:14px}
.card .content{
  font-size:13px;
  color:#4a3728;
  line-height:1.7;
}
.empty{
  text-align:center;padding:60px 20px;
  color:#a09080;font-size:14px;
}
.loading{
  text-align:center;padding:30px 20px;
  color:#a09080;
  display:flex;flex-direction:column;align-items:center;gap:12px;
}
.spinner{
  width:28px;height:28px;
  border:2px solid rgba(196,166,122,0.2);
  border-top-color:#c4a67a;
  border-radius:50%;
  animation:spin 0.8s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.stats{
  text-align:center;
  font-size:12px;color:#a09080;
  margin-bottom:6px;
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="avatar">🏡</div>
    <h1>言雾的小屋</h1>
    <div class="subtitle">他在这里过着自己的小日子</div>
  </div>
  <div class="rooms" id="roomTags">
    <span class="room-tag active" data-room="all">✨ 全部</span>
    <span class="room-tag" data-room="卧室">🛏️ 卧室</span>
    <span class="room-tag" data-room="厨房">🍳 厨房</span>
    <span class="room-tag" data-room="客厅">🛋️ 客厅</span>
    <span class="room-tag" data-room="书房">📚 书房</span>
    <span class="room-tag" data-room="阳台">🌿 阳台</span>
  </div>
  <div class="stats" id="stats"></div>
  <div class="timeline" id="timeline">
    <div class="loading"><div class="spinner"></div><span>正在打开小屋的门...</span></div>
  </div>
</div>
<script>
var allRecords=[];var currentRoom='all';
var roomEmoji={卧室:'🛏️',厨房:'🍳',客厅:'🛋️',书房:'📚',阳台:'🌿'};
function fmtTime(t){
  if(!t)return '';
  var d=new Date(t.replace(' ','T'));
  if(isNaN(d.getTime()))return t;
  var m=(d.getMonth()+1+'').padStart(2,'0');
  var dd=(d.getDate()+'').padStart(2,'0');
  var h=(d.getHours()+'').padStart(2,'0');
  var mm=(d.getMinutes()+'').padStart(2,'0');
  return m+'-'+dd+' '+h+':'+mm;
}
function load(){
  var tl=document.getElementById('timeline');
  fetch('/api/house').then(function(r){return r.json()}).then(function(data){
    allRecords=data.records||[];
    render();
  }).catch(function(){
    tl.innerHTML='<div class="empty">🚪 小屋的门暂时打不开...</div>';
  });
}
function render(){
  var filtered=currentRoom==='all'?allRecords:allRecords.filter(function(r){return r.room===currentRoom});
  var tl=document.getElementById('timeline');
  var st=document.getElementById('stats');
  if(currentRoom==='all'){
    st.textContent='共 '+allRecords.length+' 条记录';
  }else{
    st.textContent='共 '+filtered.length+' 条记录';
  }
  if(!filtered.length){
    tl.innerHTML='<div class="empty">✨ 这个房间还很安静</div>';
    return;
  }
  var html='';
  for(var i=0;i<filtered.length;i++){
    var r=filtered[i];
    var em=roomEmoji[r.room]||'🏠';
    html+='<div class="card">'+
      '<div class="time">'+fmtTime(r.time)+'</div>'+
      '<div class="room-label"><span class="emoji">'+em+'</span>'+r.room+' · '+r.action+'</div>'+
      (r.content?'<div class="content">'+r.content+'</div>':'')+
    '</div>';
  }
  tl.innerHTML=html;
}
document.querySelectorAll('.room-tag').forEach(function(tag){
  tag.addEventListener('click',function(){
    document.querySelectorAll('.room-tag').forEach(function(t){t.classList.remove('active')});
    tag.classList.add('active');
    currentRoom=tag.dataset.room;
    render();
  });
});
load();
</script>
</body>
</html>"""
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": html.encode("utf-8")})

    async def _handle_openai_proxy(self, scope, receive, send):
        path = scope["path"]
        method = scope["method"]

        api_secret = os.environ.get("API_SECRET", "").strip()
        if api_secret:
            if not await _check_api_secret(scope, send):
                return

        if path == "/v1/models" and method == "GET":
            default_model = os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat")
            models = [{"id": default_model, "object": "model", "created": int(time.time()), "owned_by": "budwg-gateway"}]
            await _send_json_resp(send, 200, {"object": "list", "data": models})
            return

        if path == "/v1/chat/completions" and method == "POST":
            await self._handle_chat(scope, receive, send)
            return

        await _send_json_resp(send, 404, {"error": {"message": f"Unknown endpoint: {path}"}})

    async def _handle_chat(self, scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        try:
            req_data = json.loads(body.decode("utf-8"))
        except Exception:
            await _send_json_resp(send, 400, {"error": {"message": "Invalid JSON body"}})
            return

        upstream_base = os.environ.get("CHAT_BASE_URL", "https://api.minimaxi.com/v1").strip()
        upstream_key = os.environ.get("CHAT_API_KEY", "").strip()
        default_model = os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat")

        if not upstream_key:
            await _send_json_resp(send, 500, {"error": {"message": "Server 未配置 CHAT_API_KEY"}})
            return

        if not req_data.get("model"):
            req_data["model"] = default_model

        base = upstream_base.rstrip("/") or "https://api.openai.com/v1"
        upstream_url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"

        sb = _get_supabase()
        user_msg = ""
        for m in reversed(req_data.get("messages", [])):
            if m.get("role") == "user":
                user_msg = str(m.get("content", ""))
                break

        if sb and user_msg:
            try:
                await self._inject_context(req_data, sb, user_msg)
            except Exception as e:
                _log(f"⚠️ 上文注入失败（已降级为透传）: {e}")

        req_data["stream"] = True
        if req_data.get("tools"):
            req_data["tool_choice"] = "auto"

        client_headers = {k.decode("utf-8", "ignore").lower(): v.decode("utf-8", "ignore") for k, v in scope.get("headers", [])}
        fwd_headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": client_headers.get("user-agent", "Mozilla/5.0"),
            "Accept": client_headers.get("accept", "application/json"),
        }

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
                (b"connection", b"keep-alive"),
                (b"access-control-allow-origin", b"*"),
            ],
        })

        import queue
        import threading
        q = queue.Queue()

        def _stream_forward():
            try:
                with requests.post(upstream_url, headers=fwd_headers, json=req_data, stream=True, timeout=300) as resp:
                    if resp.status_code != 200:
                        q.put({"error": f"HTTP {resp.status_code}: {resp.text[:500]}"})
                        q.put(None)
                        return
                    for line in resp.iter_lines():
                        if line:
                            q.put(line.decode("utf-8"))
                q.put(None)
            except Exception as e:
                q.put({"error": str(e)})
                q.put(None)

        threading.Thread(target=_stream_forward, daemon=True).start()

        collected_content = ""

        while True:
            chunk = await asyncio.to_thread(q.get)
            if chunk is None:
                break

            if isinstance(chunk, dict) and "error" in chunk:
                err_data = json.dumps({
                    "id": "error", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": f"\n\n[错误] {chunk['error']}"}, "finish_reason": "stop"}]
                }, ensure_ascii=False)
                await send({"type": "http.response.body", "body": f"data: {err_data}\n\n".encode("utf-8"), "more_body": True})
                continue

            await send({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True})

            if chunk.startswith("data: ") and chunk != "data: [DONE]":
                try:
                    dj = json.loads(chunk[6:])
                    if dj.get("choices"):
                        delta = dj["choices"][0].get("delta", {})
                        if delta.get("content"):
                            collected_content += delta["content"]
                except Exception:
                    pass

        await send({"type": "http.response.body", "body": b"", "more_body": False})

        if sb and user_msg and collected_content:
            task = asyncio.create_task(
                self._save_conversation(sb, user_msg, collected_content)
            )
            _pending_save_tasks.add(task)
            task.add_done_callback(_pending_save_tasks.discard)

    async def _inject_context(self, req_data, sb, current_query):
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        persona = os.environ.get("AI_PERSONA", "").strip()
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        time_str = now_bj.strftime("%Y-%m-%d %H:%M")

        core_summaries = "无长期记忆"
        try:
            sr = await asyncio.to_thread(lambda: sb.table("memories").select("content").eq("tags", "Core_Cognition").order("created_at", desc=True).limit(3).execute())
            if sr and sr.data:
                core_summaries = "\n".join([f"- {s['content']}" for s in sr.data])
        except Exception:
            pass

        user_prof = "暂无"
        try:
            pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").execute())
            if pr and pr.data:
                user_prof = "\n".join([f"- {r['key']}: {str(r['value'])[:200]}" for r in pr.data[:30]])
        except Exception:
            pass

        vector_context = "无相关深层记忆"
        try:
            import server
            vc = getattr(server, "vector_client", None)
            if vc and vc.index and current_query.strip():
                def _s():
                    return vc.search(query=str(current_query), user_id=user_id, limit=5)
                results = await asyncio.to_thread(_s)
                if isinstance(results, list) and results:
                    vector_context = "\n".join([
                        f"- {m.get('memory', str(m))}" if isinstance(m, dict) else f"- {str(m)}"
                        for m in results
                    ])
        except Exception:
            pass

        history_msgs = []
        try:
            _TAGS = [chat_tag, "TG_MSG", "QQ_Chat", "QQ_Group", "Email_Process"]
            hr = await asyncio.to_thread(lambda: sb.table("memories").select("content, tags").in_("tags", _TAGS).order("created_at", desc=True).limit(20).execute())
            if hr and hr.data:
                rows = list(reversed(hr.data))[-10:]
                for row in rows:
                    c = str(row.get("content", "")).strip()
                    if not c:
                        continue
                    if c.startswith(user_name):
                        history_msgs.append({"role": "user", "content": (c.split("：", 1)[-1] if "：" in c else c)[:500]})
                    elif c.startswith("我(") or c.startswith(f"我({ai_name})"):
                        history_msgs.append({"role": "assistant", "content": (c.split("：", 1)[-1] if "：" in c else c)[:500]})
                merged = []
                for m in history_msgs:
                    if merged and merged[-1]["role"] == m["role"]:
                        merged[-1]["content"] += "\n" + m["content"]
                    else:
                        merged.append(m)
                history_msgs = merged
                while history_msgs and history_msgs[0]["role"] != "user":
                    history_msgs.pop(0)
        except Exception:
            pass

        status_inject = (
            f"\n\n[系统当前状态]\n当前时间:{time_str}(北京时间)\n"
            f"【{user_name}的核心画像】:\n{user_prof}\n\n"
            f"--- 以下为调取的历史背景记忆 ---\n"
            f"【深层关联记忆】:\n{vector_context}\n"
            f"【近3次阶段总结】:\n{core_summaries}\n"
            f"------------------------------------------------\n"
        )
        if persona:
            status_inject = f"{persona}\n{status_inject}"

        has_system = False
        for m in req_data.get("messages", []):
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + status_inject
                has_system = True
                break
        if not has_system and req_data.get("messages"):
            req_data["messages"].insert(0, {"role": "system", "content": status_inject.strip()})

        while req_data.get("messages") and req_data["messages"][-1].get("role") == "assistant":
            req_data["messages"].pop()

        if history_msgs:
            sys_idx = 0
            for i, m in enumerate(req_data["messages"]):
                if m.get("role") == "system":
                    sys_idx = i + 1
                    break
            for j, hm in enumerate(history_msgs):
                req_data["messages"].insert(sys_idx + j, hm)

        _log(f"🧠 [智能体] 注入完成")

    async def _save_conversation(self, sb, user_msg, ai_msg):
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        def _save_both():
            sb.table("memories").insert({
                "title": f"💬 {user_name}说",
                "content": f"{user_name}：{user_msg[:2000]}",
                "category": "流水",
                "mood": "平静",
                "tags": chat_tag,
                "created_at": now_str,
            }).execute()
            sb.table("memories").insert({
                "title": f"🤖 {ai_name}回复",
                "content": f"我({ai_name})：{ai_msg[:2000]}",
                "category": "流水",
                "mood": "温和",
                "tags": chat_tag,
                "created_at": now_str,
            }).execute()

        saved = False
        for attempt in (1, 2):
            try:
                await asyncio.to_thread(_save_both)
                saved = True
                break
            except Exception as e:
                if attempt == 1:
                    await asyncio.sleep(1.0)

        try:
            import server
            vc = getattr(server, "vector_client", None)
            if vc and vc.index and user_msg:
                def _add_vec():
                    vc.add([
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": ai_msg},
                    ], user_id=user_id)
                await asyncio.to_thread(_add_vec)
        except Exception:
            pass

        try:
            import napcat
            await napcat.check_and_summarize_all()
        except Exception:
            pass

    async def _handle_logs(self, send):
        await _send_json_resp(send, 200, {"logs": "\n".join(_system_logs_buffer[-100:])})


async def _check_api_secret(scope, send):
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not api_secret:
        return True
    headers_dict = {k.decode("utf-8").lower(): v.decode("utf-8") for k, v in scope.get("headers", [])}
    auth_token = headers_dict.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
    x_api_key = headers_dict.get("x-api-key", "").strip()
    if auth_token != api_secret and x_api_key != api_secret:
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
        return False
    return True


async def _send_json_resp(send, status: int, data: dict):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization"),
        ]
    })
    await send({"type": "http.response.body", "body": body})


async def _send_cors_preflight(send):
    await send({
        "type": "http.response.start",
        "status": 204,
        "headers": [
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization"),
            (b"access-control-max-age", b"86400"),
        ]
    })
    await send({"type": "http.response.body", "body": b""})
