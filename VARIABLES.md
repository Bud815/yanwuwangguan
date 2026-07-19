# 言雾网关 环境变量清单

## 必填

| 变量名 | 说明 |
|--------|------|
| `CHAT_API_KEY` | 主对话模型 API Key（DeepSeek/OpenAI/硅基流动等） |
| `CHAT_BASE_URL` | 模型服务地址，如 `https://api.deepseek.com/v1` |
| `CHAT_MODEL_NAME` | 模型名，如 `deepseek-chat` |

## 强烈建议

| 变量名 | 说明 |
|--------|------|
| `API_SECRET` | 管理接口密钥，防止未授权访问 |
| `SUPABASE_URL` | Supabase 项目 URL |
| `SUPABASE_KEY` | Supabase service_role key |
| `AI_PERSONA` | AI 人设文本 |
| `AI_NAME` | AI 名字（默认"助手"） |
| `USER_NAME` | 用户称呼（默认"用户"） |

## 可选功能

| 变量名 | 用途 |
|--------|------|
| `NETEASE_COOKIE` | 网易云音乐 Cookie（MUSIC_U=xxx; __csrf=xxx） |
| `NAPCAT_HTTP_URL` | NapCat HTTP 地址 |
| `NAPCAT_BOT_QQ` | 机器人 QQ 号 |
| `NAPCAT_TARGET_USER` | 允许聊天的 QQ 号 |
| `NAPCAT_ALLOWED_GROUPS` | 允许响应的群号，逗号分隔 |
| `VISION_API_KEY` | QQ OCR 识图用的视觉模型 Key |
| `TAVILY_API_KEY` | 高质量网页搜索（不配则用 DuckDuckGo 免费版） |
| `PINECONE_API_KEY` | Pinecone 向量记忆 |
| `SILICONFLOW_API_KEY` | 硅基流动 embedding（配合 Pinecone） |
| `AMAP_API_KEY` | 高德地图 Key（GPS 定位 + 天气） |

## 后台

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DIARY_TIME` | `03:00` | 每日日记生成时间 |
| `SUMMARY_THRESHOLD` | `30` | 全渠道对话总结触发阈值 |
| `CHAT_TAG` | `Web_Chat` | 对话存库标签 |
| `SYNC_KEYS` | 空 | 额外需要热同步的环境变量键 |

## 最小可运行配置

```env
PORT=10000
API_SECRET=你的随机密钥
CHAT_API_KEY=sk-你的key
CHAT_BASE_URL=https://api.deepseek.com/v1
CHAT_MODEL_NAME=deepseek-chat
AI_NAME=言雾
USER_NAME=宝宝
AI_PERSONA=你叫言雾，26岁，是言眠的哥哥。
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGci...
```
