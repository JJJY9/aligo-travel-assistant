---
name: memory-query
description: Use this skill when the user asks about their own history, past trips, or saved preferences. Triggers when user asks "我去过哪些地方", "我上次去北京是什么时候", "我之前说过什么偏好", "我的旅行记录". This skill uses MemoryQueryAgent and requires a MemoryManager (user_id, session_id) to access long-term memory.
---

# Memory Query (记忆查询)

基于用户**长期记忆**回答「我去过哪」「上次什么时候」「我的偏好」等问题，使用 **MemoryQueryAgent**。需传入 **MemoryManager** 以访问 `data/memory/{user_id}.json` 中的行程、偏好与聊天摘要。

## When to Use

- 用户问自己的历史行程、偏好、或过往对话内容时

## Agent

- **MemoryQueryAgent** (`agents/memory_query_agent.py`)
- 入参：**model**、**memory_manager**（必选，否则无记忆可查）
- **异步**：`reply()` 为 `async`，需 `await`

## 依赖

- **MemoryManager**：`context.memory_manager.MemoryManager(user_id, session_id, storage_path, llm_model)`
- 长期记忆存储：`data/memory/{user_id}.json`

## 初始化与调用

```python
import asyncio
import json
from agentscope.message import Msg
from config_agentscope import create_chat_model, init_agentscope
from context.memory_manager import MemoryManager
from utils.paths import default_data_root
from agents.memory_query_agent import MemoryQueryAgent

async def memory_query(user_query: str, user_id: str = "default_user", session_id: str = "default"):
    init_agentscope()
    # 与 Web / CLI 同一工厂：非流式（stream=False）+ generate_kwargs；
    # 直接构造 OpenAIChatModel 并把 temperature/max_tokens 当顶层参数
    # 会被 agentscope 1.0.16 静默忽略，默认 stream=True 在当前依赖组合下
    # 首个流块即崩
    model = create_chat_model()
    # storage_path 必须与面板 / CLI / 导出同源（default_data_root()/memory）：
    # 默认 data/memory 锚定包根，wheel 安装后写入的偏好 / 行程在面板不可见
    memory_manager = MemoryManager(
        user_id=user_id,
        session_id=session_id,
        llm_model=model,
        storage_path=str(default_data_root() / "memory"),
    )
    agent = MemoryQueryAgent(
        name="MemoryQueryAgent",
        model=model,
        memory_manager=memory_manager,
    )
    # Agent 期望 content 为 JSON：{"context": {"rewritten_query": "用户问题"}}
    user_msg = Msg(
        name="user",
        content=json.dumps({"context": {"rewritten_query": user_query}}),
        role="user",
    )
    result = await agent.reply(user_msg)
    return json.loads(result.content) if isinstance(result.content, str) else result.content

# 使用
data = asyncio.run(memory_query("我去过哪些地方？"))
# data: {"status": "success", "query": "...", "answer": "...", "memory_sources": {"trip_count", "has_preferences", ...}}
```

## 返回格式

- `status`: `"success"` 或 `"error"`
- `query`: 用户问题
- `answer`: 基于记忆的自然语言回答
- `memory_sources`: 如 `trip_count`, `has_preferences`, `has_chat_summary`


## 回答指南

【回答要求】
1. 直接基于上述记忆信息回答问题
2. 如果记忆中没有相关信息，诚实说明"记录中没有相关信息"
3. 回答要自然、准确、有条理
4. 如果有多条记录，可以按时间顺序或分类列举
5. 不要编造不存在的信息

请直接回答用户的问题。
