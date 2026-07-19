"""
言雾网关 MCP 服务端
===================
基于 FastMCP 通用网关架构，精简 + 网易云音乐集成。
"""

import os
import re
import json

# 自动加载 .env（本地开发用）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import time
import uuid
import random
import asyncio
import datetime
import requests
from functools import wraps

import uvicorn
from mcp.server.fastmcp import FastMCP

# ==========================================
# 1. 全局配置 & 客户端初始化
# ==========================================

mcp = FastMCP("YanwuGateway")
ORIGINAL_ENV = dict(os.environ)
API_SECRET = os.environ.get("API_SECRET", "").strip()

# ---------- 数据库客户端 (Supabase) ----------
supabase = None
try:
    from supabase import create_client
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"⚠️ Supabase 初始化失败: {e}")

# ---------- 向量记忆 (Pinecone) ----------
PINECONE_USER_ID = os.environ.get("PINECONE_USER_ID", "default").strip()
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()

try:
    from pinecone import Pinecone
except ImportError:
    Pinecone = None


class VectorMemoryClient:
    """Pinecone 向量记忆客户端。"""

    def __init__(self):
        self.pc = Pinecone(api_key=PINECONE_KEY) if PINECONE_KEY and Pinecone else None
        self.index_name = os.environ.get("PINECONE_INDEX_NAME", "notion-brain-v2")
        self.index = self.pc.Index(self.index_name) if self.pc else None

    def search(self, query, user_id=None, filters=None, limit=3):
        if not self.index:
            return []
        try:
            vec = _get_embedding(query)
            if not vec:
                return []
            r = self.index.query(vector=vec, top_k=limit, include_metadata=True)
            return [{"memory": m.metadata.get("text", ""), "id": m.id}
                    for m in r.matches if m.metadata]
        except Exception as e:
            print(f"❌ Pinecone 搜索失败: {e}")
            return []

    def add(self, messages, user_id=None):
        user_id = user_id or PINECONE_USER_ID
        if not self.index:
            return False
        try:
            if isinstance(messages, list):
                text = " | ".join([f"{m.get('role')}: {m.get('content')}"
                                   for m in messages if isinstance(m, dict)])
            else:
                text = str(messages)
            vec = _get_embedding(text)
            if not vec:
                return False
            self.index.upsert(vectors=[{"id": str(uuid.uuid4()), "values": vec,
                                        "metadata": {"text": text, "user_id": user_id}}])
            return True
        except Exception as e:
            print(f"❌ Pinecone 写入失败: {e}")
            return False

    def delete(self, memory_id):
        if self.index:
            try:
                self.index.delete(ids=[memory_id])
            except Exception:
                pass
        return True


vector_client = VectorMemoryClient()

# ---------- HTTP 会话 ----------
http_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=3)
http_session.mount('http://', adapter)
http_session.mount('https://', adapter)


# ==========================================
# 记忆分类宪法
# ==========================================
class MemoryType:
    STREAM = "流水"
    EPISODIC = "记事"
    IDEA = "灵感"
    EMOTION = "情感"
    FACT = "画像"


WEIGHT_MAP = {
    MemoryType.STREAM: 1, MemoryType.EPISODIC: 4, MemoryType.IDEA: 7,
    MemoryType.EMOTION: 9, MemoryType.FACT: 10,
}

TAROT_DECK = [
    "0. 愚者 (The Fool)", "I. 魔术师 (The Magician)", "II. 女祭司 (The High Priestess)",
    "III. 皇后 (The Empress)", "IV. 皇帝 (The Emperor)", "V. 教皇 (The Hierophant)",
    "VI. 恋人 (The Lovers)", "VII. 战车 (The Chariot)", "VIII. 力量 (Strength)",
    "IX. 隐士 (The Hermit)", "X. 命运之轮 (Wheel of Fortune)", "XI. 正义 (Justice)",
    "XII. 倒吊人 (The Hanged Man)", "XIII. 死神 (Death)", "XIV. 节制 (Temperance)",
    "XV. 魔鬼 (The Devil)", "XVI. 高塔 (The Tower)", "XVII. 星星 (The Star)",
    "XVIII. 月亮 (The Moon)", "XIX. 太阳 (The Sun)", "XX. 审判 (Judgement)", "XXI. 世界 (The World)"
]


# ==========================================
# 2. 核心辅助函数
# ==========================================

def mcp_error_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            return f"❌ 工具执行出错: {e}"
    return wrapper


def _get_llm_client(provider: str = "main_chat"):
    from openai import OpenAI
    db_conf = {}
    if supabase:
        try:
            res = supabase.table("user_facts").select("value").eq("key", "llm_settings").execute()
            db_conf = json.loads(res.data[0]['value']) if res.data else {}
        except Exception:
            db_conf = {}
    api_key = db_conf.get("key") or os.environ.get("CHAT_API_KEY", "").strip()
    base_url = db_conf.get("url") or os.environ.get("CHAT_BASE_URL", "https://api.minimaxi.com/v1")
    model_name = db_conf.get("model") or os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat")
    client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None
    if client:
        client.custom_model_name = model_name
    return client


async def _ask_llm_async(client, prompt: str, system_prompt: str = "", temperature: float = 0.7) -> str:
    if not client:
        return ""
    model_name = getattr(client, 'custom_model_name', os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat"))
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    def _call():
        return client.chat.completions.create(model=model_name, messages=messages, temperature=temperature)

    try:
        resp = await asyncio.to_thread(_call)
        if not resp.choices:
            return ""
        raw_text = resp.choices[0].message.content.strip()
        return re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL | re.IGNORECASE).strip()
    except Exception as e:
        print(f"❌ LLM 调用失败: {e}")
        return ""


def _get_now_bj() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _save_memory_to_db(title: str, content: str, category: str = "流水", mood: str = "平静", tags: str = ""):
    if not supabase:
        return
    try:
        if category not in WEIGHT_MAP:
            mapping = {"日记": MemoryType.EPISODIC, "Note": MemoryType.IDEA,
                       "GPS": MemoryType.STREAM, "重要": MemoryType.EMOTION}
            category = mapping.get(category, MemoryType.STREAM)
        importance = WEIGHT_MAP.get(category, 1)

        if not tags:
            content_lower = content.lower()
            if any(w in content_lower for w in ["爱", "喜欢", "讨厌", "恨"]):
                tags = "情感,偏好"
            elif any(w in content_lower for w in ["吃", "喝", "买"]):
                tags = "消费,生活"
            elif any(w in content_lower for w in ["代码", "bug", "写"]):
                tags = "工作,Dev"
            else:
                tags = "System"

        data = {
            "title": title,
            "content": content,
            "category": category,
            "mood": mood,
            "tags": tags,
            "importance": importance,
            "created_at": _get_now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        }
        supabase.table("memories").insert(data).execute()
    except Exception as e:
        print(f"⚠️ 写入记忆失败: {e}")


def _get_embedding(text: str):
    try:
        api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
        embed_endpoint = os.environ.get("DOUBAO_EMBEDDING_EP", "").strip()
        if not api_key or not embed_endpoint:
            return []
        url = "https://api.siliconflow.cn/v1/embeddings"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": embed_endpoint, "input": text}
        response = http_session.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
        data = response.json()
        if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            return [float(x) for x in data["data"][0].get("embedding", [])]
        return []
    except Exception:
        return []


def _get_current_persona() -> str:
    base_persona = os.environ.get("AI_PERSONA", "你是一个通用智能助手。").strip()
    if supabase:
        try:
            res = supabase.table("user_facts").select("value").eq("key", "sys_ai_persona").execute()
            if res.data:
                base_persona = res.data[0]['value']
        except Exception:
            pass
    return f"{base_persona}\n\n（如果对话中自然联想到相关回忆，可以简短提及，但保持对话自然流畅。）"


def _format_time_cn(iso_str: str) -> str:
    if not iso_str:
        return "未知时间"
    try:
        dt = datetime.datetime.fromisoformat(str(iso_str).replace('Z', '+00:00'))
        return (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
    except Exception:
        return "未知时间"


def _get_latest_gps_record():
    if not supabase:
        return None
    try:
        res = supabase.table("device_data").select("*").order("timestamp", desc=True).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def _gps_to_address(lat, lon):
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=zh-CN"
        resp = http_session.get(url, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("display_name", f"坐标点 ({lat},{lon})")
    except Exception:
        pass
    return f"坐标点: {lat}, {lon}"


async def get_latest_diary(limit: int = 15) -> str:
    if not supabase:
        return "（数据库未连接）"
    try:
        def _fetch_recent():
            return supabase.table("memories").select("*").order("created_at", desc=True).limit(limit).execute()
        def _fetch_house():
            return supabase.table("memory_house").select("*").order("created_at", desc=True).limit(15).execute()
        res_recent, res_house = await asyncio.gather(
            asyncio.to_thread(_fetch_recent),
            asyncio.to_thread(_fetch_house),
        )

        house_stream = ""
        if res_house and res_house.data:
            house_stream = "\n🏡 【近期小屋生活动态】:\n"
            for h in sorted(res_house.data, key=lambda x: x.get('created_at', '')):
                time_str = _format_time_cn(h.get('created_at'))
                locked = "🔒" if h.get('is_locked') else ""
                house_stream += f"{time_str} {locked}在【{h.get('room', '未知')}】{h.get('action_type', '活动')}: {str(h.get('content', ''))[:80]}...\n"

        memory_stream = "🧠 【当前大脑状态】:\n"
        if not res_recent or not res_recent.data:
            memory_stream += "📭 (一片空白)\n"
        else:
            for data in res_recent.data:
                time_str = _format_time_cn(data.get('created_at'))
                cat = data.get('category', '未知')
                title = data.get('title', '无题')
                mood_str = f" | Mood:{data.get('mood')}" if data.get('mood') else ""
                memory_stream += f"{time_str} [{cat}] 【{title}】: {data.get('content', '')}{mood_str}\n"
            memory_stream += house_stream

        return memory_stream
    except Exception as e:
        return f"（记忆读取失败: {e}）"


async def where_is_user() -> str:
    if not supabase:
        return "❌ 数据库未连接"
    try:
        data = await asyncio.to_thread(_get_latest_gps_record)
        if not data:
            return "📍 暂无位置记录。"

        time_str = _format_time_cn(data.get("timestamp"))
        weather_info = ""
        lat, lon = data.get("location_latitude") or data.get("lat"), data.get("location_longitude") or data.get("lon")

        if lat and lon:
            def _get_weather():
                try:
                    amap_key = os.environ.get("AMAP_API_KEY", "").strip()
                    if amap_key:
                        regeo_url = f"https://restapi.amap.com/v3/geocode/regeo?location={lon},{lat}&key={amap_key}"
                        regeo_res = requests.get(regeo_url, timeout=4).json()
                        if regeo_res.get("status") == "1":
                            adcode = regeo_res.get("regeocode", {}).get("addressComponent", {}).get("adcode")
                            if adcode:
                                weather_url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={adcode}&key={amap_key}"
                                weather_res = requests.get(weather_url, timeout=4).json()
                                if weather_res.get("status") == "1" and weather_res.get("lives"):
                                    live = weather_res["lives"][0]
                                    return f" ☁️ {live.get('weather')} {live.get('temperature')}℃"
                except Exception:
                    pass
                return ""
            weather_info = await asyncio.to_thread(_get_weather)

        current_status = f"🛰️ 实时状态：\n📍 {data.get('location_address', '未知')}{weather_info}\n📱 当前活跃应用: {data.get('foreground_app', '未知')}\n(更新于: {time_str})"

        def _get_apps():
            time_threshold = (datetime.datetime.utcnow() - datetime.timedelta(hours=12)).isoformat()
            res = supabase.table("device_data").select("timestamp, foreground_app").gt("timestamp", time_threshold).order("timestamp").execute()
            if not res.data:
                return "暂无轨迹"
            timeline, last_app = [], ""
            for r in res.data:
                app_name = (r.get("foreground_app") or "").strip()
                if not app_name:
                    continue
                ts = _format_time_cn(r.get("timestamp"))[-5:]
                if app_name != last_app:
                    timeline.append(f"[{ts}] {app_name}")
                    last_app = app_name
            if not timeline:
                return "无切换记录"
            if len(timeline) > 15:
                timeline = ["..."] + timeline[-15:]
            return " ➡️ ".join(timeline)
        app_timeline = await asyncio.to_thread(_get_apps)
        return f"{current_status}\n\n📱 今日手机轨迹: {app_timeline}"
    except Exception as e:
        return f"❌ 查询失败: {e}"


# ==========================================
# 3. MCP 工具定义
# ==========================================

@mcp.tool()
async def echo(text: str):
    """【回声测试】用于验证网关是否正常工作。"""
    return f"🔔 网关正常运行中，收到: {text}"


@mcp.tool()
@mcp_error_handler
async def save_memory(title: str, content: str, category: str = "事件"):
    """【保存记忆】将一条信息持久化到数据库，同时写入 Pinecone 向量库。"""
    await asyncio.to_thread(_save_memory_to_db, title, content, category)
    try:
        await asyncio.to_thread(vector_client.add, [{"role": "assistant", "content": f"{title}: {content}"}])
    except Exception:
        pass
    return f"✅ 记忆已保存: {title}"


@mcp.tool()
@mcp_error_handler
async def search_memory(query: str):
    """【搜索记忆】先查向量库，再查数据库关键词，合并结果。"""
    ans_parts = []
    try:
        vec_results = await asyncio.to_thread(vector_client.search, query)
        if vec_results:
            ans_parts.append("🧠 【语义相似记忆】:")
            for r in vec_results[:3]:
                mem = r.get("memory", str(r)) if isinstance(r, dict) else str(r)
                ans_parts.append(f"- {mem}")
    except Exception:
        pass
    if supabase:
        def _query():
            return supabase.table("memories").select("id, title, content, importance").or_(
                f"title.ilike.%{query}%,content.ilike.%{query}%"
            ).order("importance", desc=True).limit(5).execute()
        sb_res = await asyncio.to_thread(_query)
        if sb_res and sb_res.data:
            ans_parts.append("🔍 【关键词匹配记忆】:")
            for r in sb_res.data:
                ans_parts.append(f"- 【{r.get('title', '无题')}】: {r['content']}")
    if not ans_parts:
        return "🧠 暂未搜到相关记忆。"
    return "\n".join(ans_parts)


@mcp.tool()
@mcp_error_handler
async def manage_user_fact(key: str, value: str):
    """【管理用户画像】新增或更新一条用户事实。"""
    if not supabase:
        return "❌ 数据库未连接"
    def _upsert():
        return supabase.table("user_facts").upsert(
            {"key": key, "value": value, "confidence": 1.0}, on_conflict="key"
        ).execute()
    await asyncio.to_thread(_upsert)
    return f"✅ 画像已更新: {key} -> {value}"


@mcp.tool()
@mcp_error_handler
async def get_user_profile():
    """【获取用户画像】读取所有用户事实。"""
    if not supabase:
        return "❌ 数据库未连接"
    def _fetch():
        return supabase.table("user_facts").select("key, value").execute()
    response = await asyncio.to_thread(_fetch)
    if not response.data:
        return "👤 用户画像为空"
    return "📋 【用户画像】:\n" + "\n".join([f"- {i['key']}: {i['value']}" for i in response.data])


@mcp.tool()
@mcp_error_handler
async def organize_knowledge_base(target: str, action: str, query_or_data: str = ""):
    """【知识库管理】通用 CRUD 工具。target: profile/memory, action: list/search/read/update/delete"""
    if not supabase:
        return "❌ 数据库未连接"
    try:
        if target == "profile":
            if action == "list":
                res = await asyncio.to_thread(lambda: supabase.table("user_facts").select("*").execute())
                return json.dumps(res.data, ensure_ascii=False, indent=2)
            elif action == "update":
                data = json.loads(query_or_data)
                await asyncio.to_thread(lambda: supabase.table("user_facts").upsert(data).execute())
                return f"✅ 已更新: {data}"
            elif action == "delete":
                await asyncio.to_thread(lambda: supabase.table("user_facts").delete().eq("key", query_or_data).execute())
                return f"✅ 已删除: {query_or_data}"

        elif target == "memory":
            if action == "list":
                res = await asyncio.to_thread(lambda: supabase.table("memories").select("id, created_at, category, title, content").order("created_at", desc=True).limit(20).execute())
                return json.dumps(res.data, ensure_ascii=False, indent=2)
            elif action == "search":
                res = await asyncio.to_thread(lambda: supabase.table("memories").select("id, title, content").or_(f"title.ilike.%{query_or_data}%,content.ilike.%{query_or_data}%").limit(15).execute())
                return json.dumps(res.data, ensure_ascii=False, indent=2)
            elif action == "read":
                res = await asyncio.to_thread(lambda: supabase.table("memories").select("*").eq("id", query_or_data).execute())
                return json.dumps(res.data, ensure_ascii=False, indent=2) if res.data else "❌ 未找到"
            elif action == "update":
                data = json.loads(query_or_data)
                mid = data.pop("id", None)
                if not mid:
                    return "❌ 缺少 id"
                await asyncio.to_thread(lambda: supabase.table("memories").update(data).eq("id", mid).execute())
                return f"✅ 记忆 {mid} 已更新"
            elif action == "delete":
                await asyncio.to_thread(lambda: supabase.table("memories").delete().eq("id", query_or_data).execute())
                return f"✅ 记忆 {query_or_data} 已删除"
        return "❌ 未知指令"
    except Exception as e:
        return f"❌ 操作失败: {e}"


@mcp.tool()
async def web_search(query: str, max_results: int = 5):
    """【网页搜索】优先 Tavily，无配置时回退 DuckDuckGo。"""
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            def _tavily():
                return requests.post("https://api.tavily.com/search", json={
                    "api_key": tavily_key, "query": query,
                    "search_depth": "basic", "include_answer": False
                }, timeout=10).json()
            res = await asyncio.to_thread(_tavily)
            if res.get("results"):
                ans = f"🌐 '{query}' 的搜索结果 (Tavily):\n\n"
                for i, item in enumerate(res["results"][:3], 1):
                    preview = item.get('content', '')[:150]
                    ans += f"{i}. 【{item.get('title')}】\n   {preview}...\n   ({item.get('url')})\n\n"
                return ans.strip()
        except Exception as e:
            print(f"⚠️ Tavily 搜索失败，回退 DDG: {e}")
    try:
        from duckduckgo_search import DDGS
        def _ddg():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        results = await asyncio.to_thread(_ddg)
        if not results:
            return "🔍 未找到结果。"
        ans = f"🔍 '{query}' 的搜索结果 (DuckDuckGo):\n"
        for i, r in enumerate(results, 1):
            ans += f"{i}. {r.get('title', '')}\n   {r.get('body', '')[:100]}\n   {r.get('href', '')}\n"
        return ans
    except Exception as e:
        return f"❌ 搜索失败: {e}"


@mcp.tool()
@mcp_error_handler
async def manage_memory_house(action: str, room: str = "", activity: str = "", content: str = "", record_id: str = ""):
    """【记忆小屋管理】AI 虚拟生活系统。action: list/do/delete"""
    if not supabase:
        return "❌ 数据库未连接"
    if action == "list":
        res = await asyncio.to_thread(lambda: supabase.table("memory_house").select("*").order("created_at", desc=True).limit(20).execute())
        if not res.data:
            return "🏡 小屋还空荡荡的，言雾还没开始活动呢。"
        ans = "🏡 【AI 小屋动态】:\n"
        for h in res.data:
            ts = _format_time_cn(h.get('created_at'))
            locked = "🔒" if h.get('is_locked') else ""
            ans += f"- {ts} {locked}在【{h.get('room','未知')}】{h.get('action_type','活动')}: {str(h.get('content',''))[:60]}\n"
        return ans
    if action == "do":
        if not room or not activity:
            return "❌ 需要 room 和 activity 参数。"
        data = {
            "room": room,
            "action_type": activity,
            "content": content or "",
            "is_locked": False,
            "created_at": _get_now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        }
        await asyncio.to_thread(lambda: supabase.table("memory_house").insert(data).execute())
        return f"✅ 言雾在【{room}】开始{activity}了。"
    if action == "delete" and record_id:
        await asyncio.to_thread(lambda: supabase.table("memory_house").delete().eq("id", record_id).execute())
        return f"✅ 小屋动态 {record_id} 已删除。"
    return "❌ 未知操作。"


@mcp.tool()
@mcp_error_handler
async def tarot_reading(question: str):
    """【塔罗占卜】抽取三张牌，由 AI 解读。"""
    draw = random.sample(TAROT_DECK, 3)
    client = _get_llm_client("main_chat")
    if not client:
        return f"🔮 抽牌结果：{', '.join(draw)}。\n(⚠️ LLM 未配置，无法解读)"
    persona = await asyncio.to_thread(_get_current_persona)
    prompt = f"当前人设：{persona}\n场景：用户因 '{question}' 感到困惑，想通过塔罗牌找方向。\n抽牌：过去 {draw[0]} | 现在 {draw[1]} | 未来 {draw[2]}\n请给出 200 字内解读。"
    ai_reply = await _ask_llm_async(client, prompt, temperature=0.8)
    return f"🔮 【塔罗指引】\n🃏 牌阵: {draw[0]} | {draw[1]} | {draw[2]}\n\n💬 {ai_reply}"


# ==========================================
# 网易云音乐工具
# ==========================================

NETEASE_COOKIE = os.environ.get("NETEASE_COOKIE", "").strip()


def _netease_request(url, data=None):
    """向网易云 API 发请求。"""
    import urllib.request
    import urllib.parse
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://music.163.com/',
        'Cookie': NETEASE_COOKIE,
        'Content-Type': 'application/x-www-form-urlencoded' if data else 'application/json'
    }
    if data and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    elif data and isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"code": -1, "error": str(e)}


def _ne_get_uid():
    resp = _netease_request('https://music.163.com/api/nuser/account/get')
    try:
        return resp.get('profile', {}).get('userId') or resp.get('account', {}).get('id')
    except Exception:
        return None


def _ne_get_csrf():
    for part in NETEASE_COOKIE.split(';'):
        part = part.strip()
        if part.startswith('__csrf='):
            return part.split('=', 1)[1]
    return ''


@mcp.tool()
@mcp_error_handler
async def netease_search_song(query: str):
    """【网易云搜歌】搜索歌曲，返回前5首结果。"""
    import urllib.parse
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE，无法使用网易云功能。"
    url = 'https://music.163.com/api/search/get?s=' + urllib.parse.quote(query) + '&type=1&limit=5'
    resp = await asyncio.to_thread(_netease_request, url)
    songs = resp.get('result', {}).get('songs', [])
    if not songs:
        return f"🔍 没有搜到 '{query}' 相关歌曲。"
    lines = [f"🎵 '{query}' 的搜索结果:"]
    for i, s in enumerate(songs, 1):
        name = s.get('name', '')
        artist = ', '.join([a.get('name', '') for a in s.get('artists', [])])
        song_id = s.get('id')
        lines.append(f"{i}. {name} - {artist} (ID:{song_id})")
    return "\n".join(lines)


@mcp.tool()
@mcp_error_handler
async def netease_list_playlists():
    """【网易云歌单列表】列出你的所有歌单。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    uid = await asyncio.to_thread(_ne_get_uid)
    if not uid:
        return "❌ 获取用户信息失败，Cookie 可能已过期。"
    url = f'https://music.163.com/api/user/playlist?uid={uid}&limit=50&offset=0'
    resp = await asyncio.to_thread(_netease_request, url)
    playlists = resp.get('playlist', [])
    if not playlists:
        return "📭 没有找到任何歌单。"
    lines = ["📋 你的歌单:"]
    for pl in playlists:
        own = '(自建)' if pl.get('creator', {}).get('userId') == uid else '(收藏)'
        lines.append(f"ID:{pl['id']} | {pl['name']} | {pl.get('trackCount', 0)}首 {own}")
    return "\n".join(lines)


@mcp.tool()
@mcp_error_handler
async def netease_get_playlist(playlist_id: int):
    """【网易云查看歌单】查看歌单里的所有歌曲。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    url = f'https://music.163.com/api/v6/playlist/detail?id={playlist_id}'
    resp = await asyncio.to_thread(_netease_request, url)
    playlist = resp.get('playlist', {})
    tracks = playlist.get('tracks', [])
    if not tracks:
        track_ids = playlist.get('trackIds', [])
        if track_ids:
            ids = [t['id'] for t in track_ids[:50]]
            detail = await asyncio.to_thread(_netease_request, f'https://music.163.com/api/song/detail?ids={json.dumps(ids)}')
            tracks = detail.get('songs', [])
    if not tracks:
        return f"📭 歌单 {playlist_id} 是空的。"
    lines = [f"📋 歌单: {playlist.get('name', '')} ({len(tracks)}首)"]
    for i, t in enumerate(tracks[:50], 1):
        artist = ', '.join([a.get('name', '') for a in t.get('ar', t.get('artists', []))])
        lines.append(f"{i}. {t.get('name', '')} - {artist} (ID:{t.get('id', '')})")
    return "\n".join(lines)


@mcp.tool()
@mcp_error_handler
async def netease_create_playlist(name: str, description: str = ""):
    """【网易云建歌单】在你的账号创建新歌单。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    csrf = _ne_get_csrf()
    url = f'https://music.163.com/api/playlist/create?csrf_token={csrf}'
    data = {'name': name, 'privacy': '0', 'type': 'NORMAL'}
    if description:
        data['description'] = description
    resp = await asyncio.to_thread(_netease_request, url, data=data)
    if resp.get('code') == 200:
        pl = resp.get('playlist', {})
        return f"✅ 已创建歌单「{name}」(ID:{pl.get('id')})"
    return f"❌ 创建失败: {resp.get('message', resp.get('error', '未知错误'))}"


@mcp.tool()
@mcp_error_handler
async def netease_add_to_playlist(playlist_id: int, song_ids: str):
    """【网易云加歌】把歌曲加进指定歌单。song_ids 用逗号分隔。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    csrf = _ne_get_csrf()
    ids = [int(s.strip()) for s in song_ids.split(',')]
    url = f'https://music.163.com/api/playlist/manipulate/tracks?csrf_token={csrf}'
    data = {'op': 'add', 'pid': str(playlist_id), 'trackIds': json.dumps(ids)}
    resp = await asyncio.to_thread(_netease_request, url, data=data)
    if resp.get('code') == 200:
        return f"✅ 已添加 {len(ids)} 首歌到歌单 {playlist_id}"
    if resp.get('code') == 502:
        return "⚠️ 歌曲已在歌单中"
    return f"❌ 添加失败: {resp.get('message', resp.get('error', '未知错误'))}"


@mcp.tool()
@mcp_error_handler
async def netease_remove_from_playlist(playlist_id: int, song_ids: str):
    """【网易云删歌】从歌单里移除歌曲。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    csrf = _ne_get_csrf()
    ids = [int(s.strip()) for s in song_ids.split(',')]
    url = f'https://music.163.com/api/playlist/manipulate/tracks?csrf_token={csrf}'
    data = {'op': 'del', 'pid': str(playlist_id), 'trackIds': json.dumps(ids)}
    resp = await asyncio.to_thread(_netease_request, url, data=data)
    if resp.get('code') == 200:
        return f"✅ 已从歌单 {playlist_id} 移除 {len(ids)} 首歌"
    return f"❌ 移除失败: {resp.get('message', resp.get('error', '未知错误'))}"


@mcp.tool()
@mcp_error_handler
async def netease_play_history(limit: int = 30):
    """【网易云听歌记录】查看最近一周的播放记录。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    uid = await asyncio.to_thread(_ne_get_uid)
    if not uid:
        return "❌ 获取用户信息失败。"
    url = f'https://music.163.com/api/v1/play/record?uid={uid}&type=1&limit={limit}'
    resp = await asyncio.to_thread(_netease_request, url)
    records = resp.get('weekData') or []
    if not records:
        return "📭 这周还没有听歌记录。"
    lines = ["🎧 最近一周听歌记录:"]
    for i, r in enumerate(records[:limit], 1):
        song = r.get('song', {})
        name = song.get('name', '')
        artist = ', '.join([a.get('name', '') for a in song.get('ar', song.get('artists', []))])
        pc = r.get('playCount', '')
        lines.append(f"{i}. {name} - {artist} (播放{pc}次, ID:{song.get('id', '')})")
    return "\n".join(lines)


@mcp.tool()
@mcp_error_handler
async def netease_like_song(song_id: int, like: bool = True):
    """【网易云收藏】收藏或取消收藏歌曲。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    csrf = _ne_get_csrf()
    action = 'true' if like else 'false'
    url = f'https://music.163.com/api/radio/like?alg=itembased&trackId={song_id}&like={action}&time=25&csrf_token={csrf}'
    resp = await asyncio.to_thread(_netease_request, url)
    if resp.get('code') == 200:
        act = "收藏" if like else "取消收藏"
        return f"✅ 已{act}歌曲 {song_id}"
    return f"❌ 失败: {resp.get('message', resp.get('error', '未知错误'))}"


@mcp.tool()
@mcp_error_handler
async def netease_daily_recommend():
    """【网易云每日推荐】获取今天的30首个性化推荐。"""
    if not NETEASE_COOKIE:
        return "❌ 未配置 NETEASE_COOKIE。"
    csrf = _ne_get_csrf()
    url = f'https://music.163.com/api/v3/discovery/recommend/songs?csrf_token={csrf}'
    resp = await asyncio.to_thread(_netease_request, url, data='{}')
    songs = resp.get('data', {}).get('dailySongs', [])
    if not songs:
        return "❌ 无法获取每日推荐，Cookie 可能已过期。"
    lines = ["✨ 今日推荐:"]
    for i, s in enumerate(songs[:30], 1):
        name = s.get('name', '')
        artist = ', '.join([a.get('name', '') for a in s.get('ar', s.get('artists', []))])
        reason = s.get('reason', '')
        line = f"{i}. {name} - {artist} (ID:{s.get('id', '')})"
        if reason:
            line += f" [{reason}]"
        lines.append(line)
    return "\n".join(lines)


# ==========================================
# 4. 启动入口
# ==========================================

from gateway import HostFixMiddleware
from heartbeat import start_autonomous_life


def _print_config_report():
    def _ok(key):
        return bool(os.environ.get(key, "").strip())

    items = [
        ("主对话 (CHAT)",     _ok("CHAT_API_KEY"),     os.environ.get("CHAT_MODEL_NAME", "未设置")),
        ("数据库 (Supabase)", _ok("SUPABASE_URL") and _ok("SUPABASE_KEY"), "已连接" if supabase else "未连接"),
        ("向量记忆 (Pinecone)", _ok("PINECONE_API_KEY"), "已启用" if vector_client.index else "未配置"),
        ("向量嵌入 (SiliconFlow)", _ok("SILICONFLOW_API_KEY"), "已配置" if _ok("SILICONFLOW_API_KEY") else "未配置"),
        ("QQ 机器人 (NapCat)", _ok("NAPCAT_WS_URL") or _ok("NAPCAT_HTTP_URL"), "已配置" if (_ok("NAPCAT_WS_URL") or _ok("NAPCAT_HTTP_URL")) else "未配置"),
        ("网易云音乐",        _ok("NETEASE_COOKIE"),    "已配置" if _ok("NETEASE_COOKIE") else "未配置"),
        ("网页搜索",          _ok("TAVILY_API_KEY"),   "Tavily" if _ok("TAVILY_API_KEY") else "DDG 免费兜底"),
        ("地图/GPS (高德)",    _ok("AMAP_API_KEY"),     "已配置" if _ok("AMAP_API_KEY") else "未配置"),
        ("接口安全密钥",      _ok("API_SECRET"),        "已配置" if _ok("API_SECRET") else "⚠️ 未配置(危险)"),
    ]
    enabled = sum(1 for _, ok, _ in items if ok)
    total = len(items)
    line = "═" * 44
    print(f"\n╔{line}╗")
    print(f"║{'🔍 言雾网关 配置体检':^36}║")
    print(f"╠{line}╣")
    for name, ok, detail in items:
        mark = "✅" if ok else "❌"
        text = f" {mark} {name:<16} → {detail}"
        print(f"║{text:<44}║")
    print(f"╠{line}╣")
    print(f"║{'已启用 ' + str(enabled) + '/' + str(total) + ' 项功能':^36}║")
    print(f"╚{line}╝\n")


if __name__ == "__main__":
    _print_config_report()
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    print(f"🚀 言雾网关运行在端口 {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
