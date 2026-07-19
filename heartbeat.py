"""
言雾网关 后台心跳模块
====================
只保留：每日日记生成 + 环境变量热同步
"""

import os
import re
import json
import time
import datetime
import asyncio
import threading


async def _sleep_to_next_minute():
    now = datetime.datetime.utcnow()
    sleep_sec = 60 - now.second + 1
    await asyncio.sleep(sleep_sec)


# ==========================================
# 深夜日记模式
# ==========================================

async def _perform_deep_dreaming():
    from server import (
        _get_llm_client, _ask_llm_async, _save_memory_to_db,
        _get_now_bj, supabase, MemoryType
    )

    AI_NAME = os.environ.get("AI_NAME", "言雾")
    USER_NAME = os.environ.get("USER_NAME", "用户")

    print("🌌 进入深度睡眠：正在整理昨日记忆...")
    try:
        now_bj = _get_now_bj()
        yesterday = (now_bj - datetime.timedelta(days=1)).date()
        iso_start = f"{yesterday.isoformat()} 00:00:00"
        iso_end = f"{now_bj.date().isoformat()} 00:00:00"

        def _fetch_yesterday():
            return supabase.table("memories").select(
                "title, created_at, category, content, mood"
            ).gt("created_at", iso_start).lt("created_at", iso_end).order("created_at").execute()

        mem_res = await asyncio.to_thread(_fetch_yesterday)
        if not mem_res.data:
            print("🌌 昨日无记忆数据，跳过日记生成。")
            return

        context = f"【昨日剧情 {yesterday}】:\n"
        for m in mem_res.data:
            content_preview = str(m.get('content', ''))[:500]
            ctx_time = str(m.get('created_at', ''))[11:16]
            context += f"[{ctx_time}] 【{m.get('title', '无题')}】 {content_preview} (Mood:{m.get('mood', '?')})\n"
        if len(context) > 80000:
            context = context[-80000:]

        client = _get_llm_client("main_chat")
        if not client:
            print("⚠️ 未配置 CHAT_API_KEY，日记生成跳过。")
            return

        prompt_summary = (
            f"{context}\n\n"
            f"请以【{AI_NAME}】的第一人称视角，将上述碎片整理成一篇具体日记。"
            f"⚠️严重警告：必须严格区分清楚【{AI_NAME}(我)】和【{USER_NAME}(对方)】各自说了什么、做了什么，"
            f"绝对不能张冠李戴搞混主语！直接输出纯文本，勿加前言后语及格式符号。"
        )
        summary = await _ask_llm_async(client, prompt_summary, temperature=0.7)

        if summary:
            await asyncio.to_thread(
                _save_memory_to_db,
                f"📅 昨日回溯: {yesterday}", summary,
                MemoryType.EMOTION, "平静", "Core_Cognition"
            )
            print(f"✅ 日记已生成: 📅 昨日回溯: {yesterday}")

        # 清理 2 天前的低重要度记录
        try:
            def _clean_old():
                del_time = (now_bj - datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
                supabase.table("memories").delete().lt("importance", 4).lt("created_at", del_time).execute()
            await asyncio.to_thread(_clean_old)
        except Exception as e:
            print(f"⚠️ 旧记忆清理失败: {e}")

        # 周度总结 (每周日)
        if now_bj.weekday() == 6:
            try:
                week_ago = (now_bj - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                week_res = await asyncio.to_thread(
                    lambda: supabase.table("memories").select("id, content").eq("tags", "Core_Cognition").gt("created_at", week_ago).execute()
                )
                if week_res.data and len(week_res.data) >= 3:
                    week_context = "\n".join([f"- {w['content']}" for w in week_res.data])
                    week_summary = await _ask_llm_async(
                        client,
                        f"【本周每日日记】:\n{week_context}\n\n请将这周的日记提炼成一篇深度的周度长期记忆总结。纯文本输出。",
                        temperature=0.7
                    )
                    if week_summary:
                        await asyncio.to_thread(
                            _save_memory_to_db, "📚 周度记忆沉淀", week_summary,
                            MemoryType.EMOTION, "温情", "Core_Cognition_Weekly"
                        )
                        print("✅ 周度记忆已沉淀。")
            except Exception as e:
                print(f"⚠️ 周度总结失败: {e}")

        # 月度总结 (每月最后一天)
        tomorrow = now_bj + datetime.timedelta(days=1)
        if tomorrow.day == 1:
            try:
                month_ago = (now_bj - datetime.timedelta(days=32)).strftime("%Y-%m-%d %H:%M:%S")
                month_res = await asyncio.to_thread(
                    lambda: supabase.table("memories").select("id, content").eq("tags", "Core_Cognition_Weekly").gt("created_at", month_ago).execute()
                )
                if month_res.data:
                    month_context = "\n".join([f"- {m['content']}" for m in month_res.data])
                    month_summary = await _ask_llm_async(
                        client,
                        f"【本月周度记忆】:\n{month_context}\n\n请以【{AI_NAME}】的第一人称视角，提炼本月的核心大事件与情感走向，生成一篇月度回忆录。纯文本输出。",
                        temperature=0.7
                    )
                    if month_summary:
                        await asyncio.to_thread(
                            _save_memory_to_db, "🌕 月度记忆沉淀", month_summary,
                            MemoryType.EMOTION, "感慨", "Core_Cognition_Monthly"
                        )
                        m_ids = [m['id'] for m in month_res.data]
                        await asyncio.to_thread(lambda: supabase.table("memories").delete().in_("id", m_ids).execute())
                        print(f"✅ 月度记忆已沉淀，清理 {len(m_ids)} 条历史周总结。")
            except Exception as e:
                print(f"⚠️ 月度总结失败: {e}")

        print("✨ 深度睡眠完成。")

    except Exception as e:
        print(f"❌ 深夜日记生成失败: {e}")


async def async_diary_worker():
    from server import supabase, _get_now_bj

    print("📔 每日日记生成已上线...")
    diary_time = os.environ.get("DIARY_TIME", "03:00")
    last_run_date = ""

    # 启动时补写昨日日记
    try:
        if supabase:
            now_bj = _get_now_bj()
            yesterday = (now_bj - datetime.timedelta(days=1)).date()
            target_title = f"📅 昨日回溯: {yesterday}"
            def _check_diary():
                return supabase.table("memories").select("id").eq("title", target_title).execute().data
            exists = await asyncio.to_thread(_check_diary)
            if not exists:
                print(f"📝 检测到昨日日记缺失，立即补写: {target_title}")
                await _perform_deep_dreaming()
                last_run_date = now_bj.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"❌ 启动补写日记失败: {e}")

    while True:
        try:
            now_bj = _get_now_bj()
            current_hm = now_bj.strftime("%H:%M")
            current_date = now_bj.strftime("%Y-%m-%d")

            if current_hm == diary_time and last_run_date != current_date:
                last_run_date = current_date
                print(f"📔 [{current_hm}] 到达日记生成时间，启动深度睡眠...")
                await _perform_deep_dreaming()
        except Exception as e:
            print(f"❌ 日记生成器报错: {e}")

        await _sleep_to_next_minute()


# ==========================================
# 环境变量热同步
# ==========================================

async def async_env_sync():
    from server import supabase, ORIGINAL_ENV

    print("⚙️ 环境变量热同步已上线...")
    default_sync_keys = [
        "CHAT_API_KEY", "CHAT_BASE_URL", "CHAT_MODEL_NAME",
        "AI_PERSONA", "PINECONE_USER_ID",
    ]
    extra_keys = [k.strip() for k in os.environ.get("SYNC_KEYS", "").split(",") if k.strip()]
    sync_keys = list(set(default_sync_keys + extra_keys))

    while True:
        try:
            if supabase:
                def _sync():
                    res = supabase.table("user_facts").select("value").eq("key", "sys_config").execute()
                    if res.data:
                        conf = json.loads(res.data[0]['value'])
                        for k in sync_keys:
                            val = str(conf.get(k, "")).strip()
                            if val:
                                os.environ[k] = val
                            else:
                                if k in ORIGINAL_ENV:
                                    os.environ[k] = ORIGINAL_ENV[k]
                                elif k in os.environ:
                                    del os.environ[k]
                await asyncio.to_thread(_sync)
        except Exception:
            pass
        await asyncio.sleep(10)


# ==========================================
# 启动入口
# ==========================================

def start_autonomous_life():
    def _run_diary(): asyncio.run(async_diary_worker())
    def _run_env_sync(): asyncio.run(async_env_sync())

    threading.Thread(target=_run_env_sync, daemon=True).start()
    threading.Thread(target=_run_diary, daemon=True).start()

    print("🐱 NapCat QQ 端点已就绪 (被动模式)。")
    print("🌾 后台心跳线程已启动（日记生成 + 环境同步）。")
