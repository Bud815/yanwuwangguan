"""
言雾网关 ASGI 中间件
====================
- 修正反代 Host 头
- CORS 预检
- API 安全校验
- OpenAI 兼容代理 + 智能体模式（夸窗口记忆核心）
- NapCat QQ 反向 WS 端点
"""

import os
import json
import asyncio
import time
import datetime
import requests


_supabase_client = None
_system_logs_buffer = []
_MAX_LOGS = 200
_pending_save_tasks = set()


def _log(msg: str):
    line = f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _system_logs_buffer.append(line)
    if len(_system_logs_buffer) > _MAX_LOGS:
        del _system_logs_buffer[: len(_system_logs_buffer) - _MAX_LOGS]


def _get_supabase():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    try:
        import server
        if getattr(server, "supabase", None) is not None:
            _supabase_client = server.supabase
            _log(f"✅ 复用 server.py 的 Supabase 客户端")
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


class HostFixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # NapCat 反向 WebSocket
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

        # 根路径
        if scope["path"] == "/":
            html = "<h1>🚪 言雾网关</h1><p>Endpoints: <code>/health</code> <code>/sse</code> <code>/v1/chat/completions</code></p>"
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": html.encode("utf-8")})
            return

        # 健康检查
        if scope["path"] == "/health":
            await _send_json_resp(send, 200, {"status": "ok", "service": "yanwu-gateway"})
            return

        # OpenAI 兼容代理
        if scope["path"].startswith("/v1/"):
            if scope["method"] == "OPTIONS":
                await _send_cors_preflight(send)
                return
            await self._handle_openai_proxy(scope, receive, send)
            return

        # API 安全拦截
        if (scope["path"].startswith("/api/") or scope["path"].startswith("/sse") or scope["path"].startswith("/messages")) and scope["method"] != "OPTIONS":
            if not await _check_api_secret(scope, send):
                return

        if scope["method"] == "OPTIONS":
            await _send_cors_preflight(send)
            return

        if scope["path"] == "/api/logs":
            await self._handle_logs(send)
            return

        headers = dict(scope.get("headers", []))
        headers[b"host"] = b"localhost:8000"
        scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

    async def _handle_openai_proxy(self, scope, receive, send):
        path = scope["path"]
        method = scope["method"]

        api_secret = os.environ.get("API_SECRET", "").strip()
        if api_secret:
            if not await _check_api_secret(scope, send):
                return

        if path == "/v1/models" and method == "GET":
            default_model = os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat")
            models = [{"id": default_model, "object": "model", "created": int(time.time()), "owned_by": "yanwu-gateway"}]
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

        # 阶段总结
        core_summaries = "无长期记忆"
        try:
            sr = await asyncio.to_thread(lambda: sb.table("memories").select("content").eq("tags", "Core_Cognition").order("created_at", desc=True).limit(3).execute())
            if sr and sr.data:
                core_summaries = "\n".join([f"- {s['content']}" for s in sr.data])
        except Exception:
            pass

        # 用户画像
        user_prof = "暂无"
        try:
            pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").execute())
            if pr and pr.data:
                user_prof = "\n".join([f"- {r['key']}: {str(r['value'])[:200]}" for r in pr.data[:30]])
        except Exception:
            pass

        # 向量记忆
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
        except Exception as e:
            _log(f"Pinecone 检索失败: {e}")

        # 最近对话历史
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

        _log(f"🧠 [智能体] 注入完成：画像{len(user_prof)}字 + 总结{len(core_summaries)}字 + 向量记忆{len(vector_context)}字 + 上文{len(history_msgs)}条")

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
                    _log(f"⚠️ 存库首次失败: {e}")
                    await asyncio.sleep(1.0)
                else:
                    _log(f"❌ 存库重试仍失败: {e}")
        if saved:
            _log(f"💾 已存库")

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
