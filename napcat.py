"""
言雾网关 NapCat QQ 机器人模块
=============================
负责：
- 接收本地 NapCat 通过反向 WebSocket 推送的 QQ 消息
- 转发给 LLM 处理后将回复发送回 QQ
- 自动重连与掉线通知（仅 QQ 通知）
"""

import os
import re
import json
import time
import uuid
import asyncio
import datetime

try:
    import websockets
except ImportError:
    websockets = None

try:
    import requests as _requests
except ImportError:
    _requests = None


NAPCAT_WS_URL = os.environ.get("NAPCAT_WS_URL", "").strip()
NAPCAT_HTTP_URL = os.environ.get("NAPCAT_HTTP_URL", "").strip()
NAPCAT_BOT_QQ = os.environ.get("NAPCAT_BOT_QQ", "").strip()
NAPCAT_TARGET_USER = os.environ.get("NAPCAT_TARGET_USER", "").strip()
NAPCAT_NOTIFY_QQ = os.environ.get("NAPCAT_NOTIFY_QQ", "").strip()
NAPCAT_ALLOWED_GROUPS = os.environ.get("NAPCAT_ALLOWED_GROUPS", "").strip()

NAPCAT_NOTIFY_QQ_LIST = [x.strip() for x in NAPCAT_NOTIFY_QQ.split(",") if x.strip()]
NAPCAT_ALLOWED_GROUPS_LIST = [x.strip() for x in NAPCAT_ALLOWED_GROUPS.split(",") if x.strip()]

RECONNECT_INITIAL_DELAY = int(os.environ.get("NAPCAT_RECONNECT_DELAY", 5))
RECONNECT_BACKOFF_FACTOR = float(os.environ.get("NAPCAT_BACKOFF_FACTOR", 1.5))
RECONNECT_MAX_DELAY = int(os.environ.get("NAPCAT_MAX_DELAY", 60))

OCR_ENABLED = os.environ.get("OCR_ENABLED", "false").strip().lower() in ("true", "1", "yes")
OCR_MAX_IMAGES = int(os.environ.get("OCR_MAX_IMAGES", "3"))
_ocr_client = None


def _get_ocr_client():
    global _ocr_client
    if _ocr_client is not None:
        return _ocr_client
    try:
        from openai import OpenAI
    except ImportError:
        return None
    vision_key = os.environ.get("VISION_API_KEY", "").strip()
    if not vision_key:
        return None
    vision_base = os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1").strip()
    vision_model = os.environ.get("VISION_MODEL_NAME", "gpt-4o-mini").strip()
    _ocr_client = OpenAI(api_key=vision_key, base_url=vision_base)
    _ocr_client.custom_model_name = vision_model
    return _ocr_client


_napcat_connected = False
_napcat_ws_send = None
_napcat_status_message = "未连接"
_napcat_last_connected_at = 0.0
_napcat_disconnect_count = 0
_napcat_logs = []
_napcat_ws_pending = {}


def _naplog(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _napcat_logs.append(line)
    if len(_napcat_logs) > 200:
        _napcat_logs.pop(0)
    print(line)


def _get_deps():
    try:
        import server
        return server
    except Exception:
        return None


def get_napcat_status() -> dict:
    return {
        "connected": _napcat_connected,
        "status_message": _napcat_status_message,
        "last_connected_at": _napcat_last_connected_at,
        "disconnect_count": _napcat_disconnect_count,
        "ws_url": NAPCAT_WS_URL or "未配置",
        "http_url": NAPCAT_HTTP_URL or "未配置",
        "bot_qq": NAPCAT_BOT_QQ,
        "target_user": NAPCAT_TARGET_USER,
        "notify_qq": NAPCAT_NOTIFY_QQ,
        "allowed_groups": NAPCAT_ALLOWED_GROUPS,
    }


def get_napcat_logs() -> list:
    return _napcat_logs[-100:]


async def _call_napcat_api(action: str, params: dict = None, timeout: float = 10.0) -> dict:
    if not _napcat_ws_send:
        return None
    echo = f"req_{uuid.uuid4().hex[:12]}"
    payload = {"action": action, "params": params or {}, "echo": echo}

    fut = asyncio.get_event_loop().create_future()
    _napcat_ws_pending[echo] = fut

    try:
        await _napcat_ws_send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=timeout)
    except Exception as e:
        _naplog(f"❌ WS API 调用失败 [{action}]: {e}")
        return None
    finally:
        _napcat_ws_pending.pop(echo, None)


async def send_qq_message(user_id: int, message: str, is_group: bool = False):
    action = "send_group_msg" if is_group else "send_private_msg"
    params = {"message": message}
    if is_group:
        params["group_id"] = user_id
    else:
        params["user_id"] = user_id
    return await _call_napcat_api(action, params)


async def _send_disconnect_notification():
    global _napcat_disconnect_count
    _napcat_disconnect_count += 1

    disconnect_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"⚠️ NapCat 掉线通知\n\n时间: {disconnect_time}\n断开次数: {_napcat_disconnect_count}\n请检查 NapCat 状态。"

    if NAPCAT_NOTIFY_QQ_LIST and _requests and NAPCAT_HTTP_URL:
        for qq in NAPCAT_NOTIFY_QQ_LIST:
            try:
                url = f"{NAPCAT_HTTP_URL}/send_private_msg"
                payload = {"user_id": int(qq), "message": message}
                _requests.post(url, json=payload, timeout=10)
                _naplog(f"✅ 掉线通知已发送到 QQ: {qq}")
            except Exception as e:
                _naplog(f"❌ 发送 QQ 掉线通知失败: {e}")


# ==========================================
# 消息处理
# ==========================================

def _extract_image_urls(text):
    urls = []
    for match in re.finditer(r'\[CQ:image([^\]]*)\]', text):
        params = match.group(1)
        url_match = re.search(r'url=([^\s,\]]+)', params)
        if url_match:
            urls.append(url_match.group(1))
        else:
            file_match = re.search(r'file=([^\s,\]]+)', params)
            if file_match and file_match.group(1).startswith('http'):
                urls.append(file_match.group(1))
    return urls[:OCR_MAX_IMAGES]


async def _ocr_image(image_url):
    if not OCR_ENABLED or not image_url:
        return ""
    try:
        client = await asyncio.to_thread(_get_ocr_client)
        if not client:
            return ""
        model_name = getattr(client, 'custom_model_name', 'gpt-4o-mini')

        def _call_vision():
            return client.chat.completions.create(
                model=model_name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请仔细识别这张图片中的所有文字内容并完整输出。如果是聊天记录/对话请完整还原。如果没有文字，简要描述图片内容。直接输出结果。"},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }],
                max_tokens=800,
                timeout=30,
            )
        resp = await asyncio.wait_for(asyncio.to_thread(_call_vision), timeout=35)
        if resp.choices:
            result = resp.choices[0].message.content.strip()
            if result:
                return result
    except Exception as e:
        _naplog(f"📷 OCR 失败: {e}")
    return ""


async def _ocr_process_message(raw_text):
    if not OCR_ENABLED:
        return ""
    image_urls = _extract_image_urls(raw_text)
    if not image_urls:
        return ""
    _naplog(f"📷 检测到 {len(image_urls)} 张图片，开始 OCR...")
    ocr_results = []
    for i, url in enumerate(image_urls):
        ocr_text = await _ocr_image(url)
        if ocr_text:
            prefix = f"[图片{i+1}内容]" if len(image_urls) > 1 else "[图片内容]"
            ocr_results.append(f"{prefix}: {ocr_text}")
    return "\n".join(ocr_results)


async def _process_napcat_message(data: dict, send):
    try:
        post_type = data.get("post_type")
        if post_type != "message":
            return

        message_type = data.get("message_type")
        raw_message = data.get("raw_message", "")
        sender = data.get("sender", {})
        sender_id = data.get("user_id")

        if message_type == "group":
            group_id = data.get("group_id")
            if NAPCAT_ALLOWED_GROUPS_LIST and str(group_id) not in NAPCAT_ALLOWED_GROUPS_LIST:
                return
            if f"[CQ:at,qq={NAPCAT_BOT_QQ}]" not in raw_message and NAPCAT_BOT_QQ:
                return
            clean_text = raw_message.replace(f"[CQ:at,qq={NAPCAT_BOT_QQ}]", "").strip()
        else:
            if NAPCAT_TARGET_USER and str(sender_id) != NAPCAT_TARGET_USER:
                return
            clean_text = raw_message.strip()

        ocr_text = await _ocr_process_message(raw_message)
        if ocr_text:
            clean_text = re.sub(r'\[CQ:image[^\]]*\]', '', clean_text).strip()
            clean_text = f"{clean_text}\n{ocr_text}".strip() if clean_text else ocr_text

        if not clean_text:
            return

        dep = _get_deps()
        if not dep:
            return

        client = dep._get_llm_client("main_chat")
        if not client:
            await send_qq_message(
                group_id if message_type == "group" else sender_id,
                "（AI 服务暂未配置，无法回复）",
                is_group=(message_type == "group")
            )
            return

        curr_persona = dep._get_current_persona()
        prompt = f"""
        收到一条 QQ 消息: {clean_text}
        发送者: {sender.get('nickname', '未知')}
        当前人设: {curr_persona}

        请用符合人设的口吻回复。纯文本，简洁自然。
        """
        reply = await dep._ask_llm_async(client, prompt, temperature=0.8)

        if reply:
            target_id = group_id if message_type == "group" else sender_id
            await send_qq_message(target_id, reply, is_group=(message_type == "group"))

            if hasattr(dep, "_save_memory_to_db"):
                await asyncio.to_thread(
                    dep._save_memory_to_db,
                    "🤖 QQ 互动",
                    f"{sender.get('nickname', '未知')}: {clean_text}\n回复: {reply}",
                    "流水", "温柔", "QQ_MSG"
                )

            asyncio.create_task(check_and_summarize_all())
    except Exception as e:
        _naplog(f"❌ 处理 QQ 消息失败: {e}")


async def check_and_summarize_all():
    dep = _get_deps()
    if not dep:
        return
    try:
        threshold = int(os.environ.get("SUMMARY_THRESHOLD", "30"))
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")

        _MAX_MSG_CHARS = 500
        _MAX_PROMPT_CHARS = 80000

        def _check():
            if not getattr(dep, "supabase", None):
                return
            _ALL_CHAT_TAGS = [chat_tag, "QQ_MSG", "QQ_Chat", "QQ_Group", "TG_MSG", "Email_Process"]
            all_chats = dep.supabase.table("memories").select("id, title, content, tags").in_("tags", _ALL_CHAT_TAGS).order("created_at").execute()
            if all_chats and all_chats.data and len(all_chats.data) >= threshold:
                items_to_summarize = all_chats.data[-threshold:]
                all_ids_to_archive = [item['id'] for item in all_chats.data]

                _naplog(f"📦 全渠道累计对话满 {len(all_chats.data)} 条，触发统一总结...")

                chat_parts = []
                total_chars = 0
                for item in items_to_summarize:
                    truncated_content = item['content'][:_MAX_MSG_CHARS]
                    tag = item.get('tags', '')
                    channel_map = {
                        chat_tag: "网页", "QQ_MSG": "QQ", "QQ_Chat": "QQ",
                        "QQ_Group": "QQ群", "TG_MSG": "TG", "Email_Process": "邮件",
                    }
                    channel_label = channel_map.get(tag, tag)
                    part = f"[{channel_label}]{item['title']}: {truncated_content}"
                    if total_chars + len(part) > _MAX_PROMPT_CHARS:
                        break
                    chat_parts.append(part)
                    total_chars += len(part)

                chat_text = "\n".join(chat_parts)
                prompt = (
                    f"以下是我们最近在各个渠道（网页/QQ/TG/邮件）的{len(chat_parts)}条对话记录：\n{chat_text}\n\n"
                    f"请你以{ai_name}(我)的第一人称视角，提取核心要点，精炼地总结一下我们最近聊了什么、发生了什么。"
                    f"⚠️严重警告：1. 必须严格区分清楚'{ai_name}(我)'做了什么，以及'{user_name}'做了什么！"
                    f"2. 绝对禁止以'今天'开头！直接开门见山。"
                )
                client = dep._get_llm_client("main_chat")
                if client:
                    try:
                        model_name = getattr(client, 'custom_model_name', "abab6.5s-chat")
                        summary = client.chat.completions.create(
                            model=model_name,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.7
                        ).choices[0].message.content.strip()
                        if hasattr(dep, "_save_memory_to_db"):
                            dep._save_memory_to_db(
                                f"📚 全渠道阶段总结", summary, "记事", "温情", "Core_Cognition"
                            )
                        dep.supabase.table("memories").update(
                            {"tags": "Archived_Chat", "importance": 1}
                        ).in_("id", all_ids_to_archive).execute()
                        _naplog(f"✅ 全渠道对话总结完成，已归档 {len(all_ids_to_archive)} 条流水")
                    except Exception:
                        dep.supabase.table("memories").update(
                            {"tags": "Archived_Chat", "importance": 1}
                        ).in_("id", all_ids_to_archive).execute()
                else:
                    dep.supabase.table("memories").update(
                        {"tags": "Archived_Chat", "importance": 1}
                    ).in_("id", all_ids_to_archive).execute()
        await asyncio.to_thread(_check)
    except Exception as e:
        _naplog(f"❌ 全渠道统一总结失败: {e}")


# ==========================================
# 反向 WS 服务端处理
# ==========================================

async def handle_napcat_ws(scope, receive, send):
    global _napcat_connected, _napcat_ws_send, _napcat_last_connected_at, _napcat_status_message

    await send({"type": "websocket.accept"})
    _napcat_connected = True
    _napcat_ws_send = send
    _napcat_last_connected_at = time.time()
    _napcat_status_message = "已连接"
    _naplog("✅ NapCat 反向 WS 已连接")

    try:
        while True:
            try:
                msg = await receive()
            except Exception:
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] != "websocket.receive":
                continue
            raw_text = msg.get("text", "")
            if not raw_text:
                continue
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            echo_val = data.get("echo", "")
            if echo_val and echo_val in _napcat_ws_pending:
                future = _napcat_ws_pending[echo_val]
                if not future.done():
                    future.set_result(data)
                continue

            if data.get("post_type") == "meta_event" and data.get("meta_event_type") == "heartbeat":
                _napcat_last_connected_at = time.time()
                continue

            if data.get("post_type") == "meta_event" and data.get("meta_event_type") == "login":
                sub_type = data.get("sub_type", "")
                if "offline" in str(data).lower() or "kick" in str(data).lower():
                    _napcat_status_message = "🔴 QQ 已掉线"
                    _naplog("🚨 QQ 登录失效，需要重新扫码")
                elif sub_type == "login_success":
                    _napcat_status_message = "🟢 QQ 已登录"
                    _naplog("✅ QQ 已重新登录")
                continue

            if data.get("post_type") == "notice":
                if "offline" in str(data).lower():
                    _napcat_status_message = "🔴 QQ 已掉线"
                continue

            if data.get("post_type") != "message":
                continue
            try:
                await _process_napcat_message(data, send)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _napcat_ws_send = None
        _napcat_connected = False
        _napcat_status_message = "反向 WS 已断开"
        for eid, fut in _napcat_ws_pending.items():
            if not fut.done():
                fut.set_result(None)
        _napcat_ws_pending.clear()
        _naplog("❌ NapCat 反向 WS 连接已关闭")
