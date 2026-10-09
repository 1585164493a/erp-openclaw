"""
对话 API 模块

提供流式对话接口、中断恢复接口和会话状态查询接口
"""

import json
import uuid
import re
import os
import tempfile
from datetime import datetime
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.types import Command
from pydantic import BaseModel

from agent.schema import (
    ChatRequest,
    ChatResponse,
    Message,
)
from api_view.agent_loader import agent_loader


# 创建路由
router = APIRouter()

# 调试日志文件路径（在项目根目录的 temp 文件夹下）
DEBUG_LOG_DIR = os.path.join(tempfile.gettempdir(), "deepagent_debug")
os.makedirs(DEBUG_LOG_DIR, exist_ok=True)


def get_debug_log_path(thread_id: str) -> str:
    """获取当前会话的调试日志文件路径"""
    safe_id = thread_id.replace("/", "_").replace("\\", "_")[:50]
    return os.path.join(DEBUG_LOG_DIR, f"stream_{safe_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


def write_debug_log(filepath: str, event_type: str, data: dict, raw_token: object = None):
    """写入调试日志"""
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {event_type}\n")
            f.write(f"  data: {json.dumps(data, ensure_ascii=False, default=str)[:2000]}\n")
            if raw_token is not None:
                token_info = {}
                if hasattr(raw_token, 'type'):
                    token_info['type'] = raw_token.type
                if hasattr(raw_token, 'name'):
                    token_info['name'] = raw_token.name
                if hasattr(raw_token, 'content'):
                    c = raw_token.content
                    if isinstance(c, str):
                        token_info['content_preview'] = c[:500]
                    elif isinstance(c, list):
                        token_info['content_len'] = len(c)
                        token_info['content_preview'] = str(c)[:500]
                    else:
                        token_info['content_preview'] = str(c)[:500]
                if hasattr(raw_token, 'tool_call_chunks') and raw_token.tool_call_chunks:
                    token_info['tool_call_chunks'] = str(raw_token.tool_call_chunks)[:500]
                if hasattr(raw_token, 'id'):
                    token_info['id'] = raw_token.id
                f.write(f"  token: {json.dumps(token_info, ensure_ascii=False, default=str)[:2000]}\n")
            f.write("\n")
    except Exception:
        pass  # 调试日志失败不影响主流程


def extract_subagent_type(args_str: str) -> str | None:
    """
    从 task 工具已累积的参数里取出 subagent_type。

    子 Agent 消息的 namespace 是 tools:<内部 id>，与 tool_call_id 无关，
    所以只能靠发起委派的 task 调用反推它到底是哪个子 Agent。
    """
    if not args_str:
        return None
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(args, dict):
        value = args.get("subagent_type")
        if isinstance(value, str) and value:
            return value
    return None


def is_likely_uuid(text: str) -> bool:
    """
    判断文本是否大概率是 UUID（而非有意义的回复内容）
    例如：3576bba4-42e5-a769-c2f7-ea8444829951
    """
    # UUID 格式：8-4-4-4-12
    uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    return bool(re.match(uuid_pattern, text.strip()))


def extract_content_from_token(token) -> str:
    """
    从 token 中提取文本内容

    处理 token.content 可能是字符串、列表或其他类型的情况
    过滤掉明显是 UUID 或无关标识符的内容
    """
    if not hasattr(token, 'content'):
        return ""

    content = token.content

    # 如果 content 是字符串，直接返回
    if isinstance(content, str):
        # 过滤纯 UUID 内容
        if is_likely_uuid(content):
            return ""
        return content

    # 如果 content 是列表，尝试提取其中的文本
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                # 结构化内容：只提取 text 类型，忽略 image_url 等
                item_type = item.get("type", "")
                if item_type == "text":
                    t = item.get("text", "")
                    if not is_likely_uuid(t):
                        text_parts.append(t)
                elif item_type == "image_url":
                    # 图片 URL 不提取为文本，由 serialize_tool_result 处理
                    pass
                elif "text" in item:
                    t = item["text"]
                    if not is_likely_uuid(str(t)):
                        text_parts.append(str(t))
                elif "content" in item:
                    text_parts.append(str(item["content"]))
            else:
                text_parts.append(str(item))
        return ''.join(text_parts)

    # 其他类型转换为字符串
    return str(content) if content is not None else ""


def serialize_tool_result(content) -> dict:
    """
    将工具执行结果序列化为前端可用的结构化数据

    处理 tool 消息的 content，可能是字符串、列表（包含文本和图片）
    提取文本和图片数据，返回结构化字典
    """
    result = {"text": "", "images": []}

    if isinstance(content, str):
        result["text"] = content
        # 提取 markdown 图片语法
        for match in re.finditer(r'!\[.*?\]\((.*?)\)', content):
            url = match.group(1)
            if url not in result["images"]:
                result["images"].append(url)

    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "text":
                    t = item.get("text", "")
                    text_parts.append(t)
                elif item_type == "image_url":
                    image_url = item.get("image_url", {})
                    url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                    if url:
                        result["images"].append(url)
                elif item_type == "image":
                    image_data = item.get("data", "") or item.get("image_data", "")
                    if image_data:
                        result["images"].append(image_data)
                elif "text" in item:
                    text_parts.append(item["text"])
                elif "content" in item:
                    text_parts.append(str(item["content"]))
                else:
                    text_parts.append(str(item))
            else:
                text_parts.append(str(item))
        result["text"] = ''.join(text_parts)
    else:
        result["text"] = str(content) if content is not None else ""

    # 从文本中提取所有图片 URL（包括 markdown 语法和直链）
    # 支持常见图片托管域名（即使 URL 不以图片扩展名结尾）
    if result["text"]:
        for match in re.finditer(r'!\[.*?\]\((.*?)\)', result["text"]):
            url = match.group(1)
            if url not in result["images"]:
                result["images"].append(url)
        # 也检测 data:image base64
        for match in re.finditer(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', result["text"]):
            url = match.group(0)
            if url not in result["images"]:
                result["images"].append(url)

    return result


def create_sse_message(data: dict) -> str:
    """创建 SSE 格式的消息"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================================
# Token 用量采集（用于前端展示「本轮用量 / 上下文占用」）
# ============================================================
class TurnUsageCollector(BaseCallbackHandler):
    """采集本轮所有模型调用（**含子 Agent**，回调会自动向下传播）的 token 用量。

    两个口径必须分清（这是设计的关键）：
    - `input/output_tokens` 累计：本轮**总花费**（会随步数一直涨）
    - `context_used`：**最后一次**调用的输入侧总量（input + 缓存读 + 缓存写）
      —— 这才是「当前上下文占用」，可以跟模型窗口比出百分比；
      累计值拿来算占用是错的（跑 10 步就"超窗口"了）。

    provider 的缓存字段在 LangChain 里统一落在 `usage_metadata.input_token_details`：
    `cache_read`（命中缓存的输入，便宜）/ `cache_creation`（写入缓存）。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001, D102
        for generation_list in getattr(response, "generations", []) or []:
            for gen in generation_list:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if usage:
                    self.calls.append(dict(usage))

    @staticmethod
    def _cache_split(usage: dict) -> tuple[int, int]:
        details = usage.get("input_token_details") or {}
        return int(details.get("cache_read", 0) or 0), int(details.get("cache_creation", 0) or 0)

    def build_event(self) -> dict:
        """组装前端要用的 usage 事件（模型窗口可用 MODEL_CONTEXT_WINDOW 覆盖）。

        缓存口径（踩过一次坑，写清楚）：
        DeepSeek / OpenAI 风格的 `prompt_tokens` **已包含**缓存命中部分
        （hit + miss = prompt），所以
          · context_used = 本轮所有调用里输入侧**最大**的那次（peak，见下方注释）
          · cache_hit_rate = 累计 cache_read / 累计 input_tokens
        Anthropic 风格是 `input_tokens` 不含缓存 —— 那种 provider 设
        `USAGE_CACHE_EXCLUSIVE=1` 走加法口径。
        """
        inp = sum(int(u.get("input_tokens", 0) or 0) for u in self.calls)
        out = sum(int(u.get("output_tokens", 0) or 0) for u in self.calls)
        cache_read = sum(self._cache_split(u)[0] for u in self.calls)
        cache_write = sum(self._cache_split(u)[1] for u in self.calls)

        exclusive = os.getenv("USAGE_CACHE_EXCLUSIVE", "0") == "1"

        # 上下文占用取【所有调用里输入侧最大的那一次】（peak），不是"最后一次调用"。
        # 原因（实测踩到）：多 Agent 图里最后完成的往往是一个上下文很小的子 Agent 调用，
        # 取末次会得出 "上下文 272 / 131.1k（0.2%）" 这种明显不对的数字；
        # peak 反映的是"这一轮我们最多把多长的上下文塞给过模型"，才是有意义的占用。
        def _prompt_of(u: dict) -> int:
            cr, cw = self._cache_split(u)
            return int(u.get("input_tokens", 0) or 0) + (cr + cw if exclusive else 0)

        context_used = max((_prompt_of(u) for u in self.calls), default=0)
        prompt_total = inp + (cache_read + cache_write if exclusive else 0)

        # 窗口大小：**不要凭印象填**。这里默认值来自实测
        #   GET https://api.deepseek.com/models  →  deepseek-flash: context_window=1048576
        # （评测集/probe_deepseek_usage.py 可复跑）。换模型/换 provider 必须改这里或设 MODEL_CONTEXT_WINDOW。
        # 血的教训：一开始我按"DeepSeek 128K"填了 131072，把占用比例放大了 8 倍。
        window = int(os.getenv("MODEL_CONTEXT_WINDOW", "1048576"))
        return {
            "type": "usage",
            "calls": len(self.calls),
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "context_used": context_used,
            "context_window": window,
            "context_ratio": round(context_used / window, 4) if window else 0.0,
            "cache_hit_rate": round(cache_read / prompt_total, 4) if prompt_total else 0.0,
        }


# ============================================================
# 中断恢复请求模型
# ============================================================

class ResumeRequest(BaseModel):
    """中断恢复请求体，resume 字段的格式取决于中断类型：
    - 数据补充中断: {"supplement": "用户自由文本输入"}
    - HITL 审批中断: {"decisions": [{"type": "approve"}]} 或 [{"type": "reject"}]
    """
    resume: dict
    user_id: str = "aff"


# ============================================================
# 流式对话核心逻辑
# ============================================================

async def stream_chat_response(
    message: str = None,
    thread_id: str = None,
    resume_data: dict = None,
    user_id: str = "aff",
) -> AsyncIterator[str]:
    """
    流式生成对话响应，支持 Human-in-the-Loop 中断与恢复。

    两种调用模式：
    1. 初始对话 — 传入 message（用户消息）
    2. 中断恢复 — 传入 resume_data（Command.resume 的值）

    当 Agent 触发中断时（request_order_info 数据补充 / order_create|update HITL 审批），
    流会发送 interrupt 事件后结束。前端收集用户决策后通过 /resume 端点恢复。

    同时累积完整的展示消息列表（包含子代理消息），在流结束后存入 MongoDB。
    """
    context = {"user_id": user_id, "username": user_id}
    config = agent_loader.create_config(thread_id, user_id=user_id)

    # Token 用量采集：挂成回调，LangChain 会把它传播到子 Agent 的模型调用上
    usage_collector = TurnUsageCollector()
    config.setdefault("callbacks", []).append(usage_collector)

    def usage_sse() -> str:
        """本轮用量事件（在 done / interrupt 之前发，前端好把数字挂到这条消息上）。"""
        return create_sse_message(usage_collector.build_event())

    # 获取该用户的 per-user agent graph（沙箱缓存 + 预计算组件）
    agent_graph = await agent_loader.get_agent_for_user(user_id)
    collected_content = ""
    # 工具调用栈，支持嵌套工具调用（主代理调 task → 子代理调 generate_chart）
    tool_call_stack = []
    # 子 Agent 命名空间 -> 可读名字（首次见到该 ns 时绑定）
    subagent_source_cache: dict = {}
    # 同一条 AIMessage 里可能并行发起多个工具调用，靠 tool_call_chunks 的
    # index 区分；纯 args 碎片不带 id，用这张表按 index 反查归属。
    pending_tool_ids: dict = {}

    # ---- 根据模式构建 input 和 display_messages ----
    if resume_data is not None:
        # 恢复模式：加载已有展示消息，用 Command(resume=...) 恢复
        existing = await agent_loader.get_display_messages(thread_id) or []
        display_messages = existing
        current_input = Command(resume=resume_data)
    else:
        # 初始模式：新建展示消息，用普通消息作为 input
        current_input = {"messages": [{"role": "user", "content": message}]}
        display_messages = [
            {
                "id": f"user-{uuid.uuid4()}",
                "role": "user",
                "content": message
            }
        ]

    # 创建调试日志文件
    debug_log = get_debug_log_path(thread_id)

    def _last_display_is_assistant():
        return (display_messages and
                display_messages[-1]["role"] == "assistant")

    try:
        write_debug_log(debug_log, "STREAM_START", {
            "message": message,
            "thread_id": thread_id,
            "is_resume": resume_data is not None,
        })

        # 流式调用 agent.astream()
        # stream_mode=["messages", "values"] — messages 流显示 + values 流检测中断
        async for chunk in agent_graph.astream(
            input=current_input,
            config=config,
            context=context,
            stream_mode=["messages", "values"],
            subgraphs=True, # 启用子代理流式输出
            version="v2",
        ):
            chunk_type = chunk.get("type")

            # ---- 值流（中断检测，必须在 messages 处理之前）----
            if chunk_type == "values" and chunk.get("interrupts"):
                interrupts = chunk["interrupts"]
                write_debug_log(debug_log, "INTERRUPT_DETECTED", {
                    "count": len(interrupts),
                })

                for interrupt in interrupts:
                    interrupt_value = interrupt.value

                    if "action_requests" in interrupt_value:
                        # ---- 第 2 层：HITL 审批中断（interrupt_on 配置）----
                        yield usage_sse()
                        yield create_sse_message({
                            "type": "interrupt",
                            "interrupt_type": "hitl_approval",
                            "action_requests": interrupt_value["action_requests"],
                            "review_configs": interrupt_value.get("review_configs", []),
                            "thread_id": thread_id,
                        })
                        write_debug_log(debug_log, "INTERRUPT_HITL", {
                            "actions": [a["name"] for a in interrupt_value["action_requests"]],
                        })

                    elif interrupt_value.get("type") == "order_info_request":
                        # ---- 第 1 层：数据补充中断（request_order_info 工具）----
                        yield usage_sse()
                        yield create_sse_message({
                            "type": "interrupt",
                            "interrupt_type": "order_info_supplement",
                            "missing_fields": interrupt_value["missing_fields"],
                            "collected_data": interrupt_value["collected_data"],
                            "thread_id": thread_id,
                        })
                        write_debug_log(debug_log, "INTERRUPT_SUPPLEMENT", {
                            "missing_fields": interrupt_value["missing_fields"],
                        })

                    else:
                        # 未知中断类型，透传原始值
                        yield usage_sse()
                        yield create_sse_message({
                            "type": "interrupt",
                            "interrupt_type": "unknown",
                            "interrupt_value": str(interrupt_value)[:2000],
                            "thread_id": thread_id,
                        })

                # 兜底：将所有 calling 状态的工具标记为 done
                for dm in display_messages:
                    if dm["role"] == "tool" and dm["tool_status"] == "calling":
                        dm["tool_status"] = "done"

                # 清理空的 assistant 消息
                cleaned = [
                    dm for dm in display_messages
                    if not (dm["role"] == "assistant" and not dm.get("content"))
                ]

                # 保存部分展示消息到 MongoDB（中断前的消息状态）
                # 注意：resume 模式下 display_messages 已含历史，保存时会覆盖旧记录
                await agent_loader.save_display_messages(thread_id, cleaned)
                write_debug_log(debug_log, "SAVE_DISPLAY_INTERRUPT", {
                    "thread_id": thread_id,
                    "message_count": len(cleaned),
                })

                # 发送 done 事件标记流结束（前端由此知道可以展示中断 UI）
                yield create_sse_message({
                    "type": "done",
                    "thread_id": thread_id,
                    "content": collected_content,
                    "interrupted": True,
                })
                return  # ← 结束本次流，等待前端 POST /resume

            # ---- 消息流（token / tool_call / tool_result）----
            if chunk_type != "messages":
                continue

            token, metadata = chunk["data"]
            namespace = chunk.get("ns", ())

            # 判断消息来源
            is_subagent = any(s.startswith("tools:") for s in namespace)
            if is_subagent:
                ns_key = tuple(namespace)
                if ns_key not in subagent_source_cache:
                    # 用仍在栈上的最内层 task 调用反推子 Agent 名
                    for entry in reversed(tool_call_stack):
                        if entry["name"] == "task":
                            bound = extract_subagent_type(entry.get("args", ""))
                            if bound:
                                subagent_source_cache[ns_key] = bound
                            break
                source = subagent_source_cache.get(ns_key, "subagent")
            else:
                source = "main"

            write_debug_log(debug_log, "RAW_CHUNK", {
                "ns": list(namespace),
                "source": source,
            }, raw_token=token)

            # 处理工具调用
            if hasattr(token, 'tool_call_chunks') and token.tool_call_chunks:
                for tool_chunk in token.tool_call_chunks:
                    tool_index = tool_chunk.get('index', 0)
                    # 工具开始调用
                    if tool_chunk.get('name'):
                        # 优先复用模型给出的真实 tool_call_id，避免自造 id 后无法回溯
                        tool_id = tool_chunk.get('id') or str(uuid.uuid4())
                        # 记住本批次该 index 对应的 id，供后续纯 args 碎片归属
                        pending_tool_ids[tool_index] = tool_id
                        new_tool = {
                            "id": tool_id,
                            "name": tool_chunk['name'],
                            "args": "",
                            "source": source
                        }
                        tool_call_stack.append(new_tool)

                        yield create_sse_message({
                            "type": "tool_start",
                            "tool_call_id": tool_id,
                            "tool_name": tool_chunk['name'],
                            "source": source
                        })

                        # 添加到展示消息
                        display_messages.append({
                            "id": tool_id,
                            "role": "tool",
                            "tool_name": tool_chunk['name'],
                            "args": "",
                            "text": "",
                            "images": [],
                            "source": source,
                            "tool_status": "calling"
                        })

                        write_debug_log(debug_log, "TOOL_START", {
                            "name": tool_chunk['name'],
                            "source": source,
                            "stack_depth": len(tool_call_stack)
                        })

                    # 工具参数
                    if tool_chunk.get('args'):
                        args_str = tool_chunk['args']
                        if tool_call_stack:
                            tool_call_stack[-1]["args"] += args_str

                        yield create_sse_message({
                            "type": "tool_args",
                            "tool_call_id": pending_tool_ids.get(tool_index),
                            "args": args_str,
                            "source": source
                        })

                        # 更新展示消息中对应工具的 args（从后向前找最后一个 calling 的 tool）
                        for dm in reversed(display_messages):
                            if dm["role"] == "tool" and dm["tool_status"] == "calling":
                                dm["args"] += args_str
                                break

            # 处理工具执行结果
            if hasattr(token, 'type') and token.type == "tool":
                tool_name = getattr(token, 'name', '未知工具')
                result_content = getattr(token, 'content', '')

                # 序列化工具结果为结构化数据
                serialized = serialize_tool_result(result_content)

                # 从栈中弹出对应工具（栈顶即为当前完成的工具）
                finished_tool = tool_call_stack.pop() if tool_call_stack else None
                tool_id = finished_tool["id"] if finished_tool else ""

                yield create_sse_message({
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_id,
                    "text": serialized["text"],
                    "images": serialized["images"],
                    "source": source
                })

                # 更新展示消息中对应工具的结果
                for dm in reversed(display_messages):
                    if dm["role"] == "tool" and dm["id"] == tool_id:
                        dm["text"] = serialized["text"]
                        dm["images"] = serialized["images"]
                        dm["tool_status"] = "done"
                        break

                write_debug_log(debug_log, "TOOL_RESULT", {
                    "tool_name": tool_name,
                    "tool_id": tool_id,
                    "text_preview": serialized["text"][:300],
                    "images_count": len(serialized["images"]),
                    "source": source,
                    "stack_remaining": len(tool_call_stack)
                })

                # 发送 tool_end 事件，标记工具调用结束
                yield create_sse_message({
                    "type": "tool_end",
                    "tool_name": tool_name,
                    "tool_call_id": tool_id,
                    "source": source
                })

                # 确保展示消息中的工具标记为 done（兜底）
                for dm in reversed(display_messages):
                    if dm["role"] == "tool" and dm["id"] == tool_id and dm["tool_status"] == "calling":
                        dm["tool_status"] = "done"
                        break

            # 处理 AI 文本内容
            # 注意：ToolMessage（type == "tool"）的内容是工具执行结果，已在上面
            # tool_result 事件中处理。这里必须跳过，否则工具结果会被当作 AI 文本重复输出。
            content_text = extract_content_from_token(token)
            has_tool_calls = hasattr(token, 'tool_call_chunks') and token.tool_call_chunks
            is_tool_result = hasattr(token, 'type') and token.type == "tool"
            # 节点输出里会带 SystemMessage（ContextInjectionMiddleware 注入的用户上下文），
            # 它同样走 messages 流。必须排除，否则内部提示词会被当作 AI 回答推给前端。
            is_system = getattr(token, 'type', None) == "system"

            if content_text and not has_tool_calls and not is_tool_result and not is_system:
                collected_content += content_text
                yield create_sse_message({
                    "type": "token",
                    "content": content_text,
                    "source": source
                })
                write_debug_log(debug_log, "TOKEN", {
                    "content": content_text[:200],
                    "source": source
                })

                # 更新展示消息：如果最后一条是 assistant 则追加，否则新建
                if _last_display_is_assistant():
                    display_messages[-1]["content"] += content_text
                    display_messages[-1]["source"] = source
                else:
                    display_messages.append({
                        "id": f"assistant-{uuid.uuid4()}",
                        "role": "assistant",
                        "content": content_text,
                        "source": source
                    })

        # ---- 流正常结束（无中断）----
        # 兜底：将所有 calling 状态的工具标记为 done
        for dm in display_messages:
            if dm["role"] == "tool" and dm["tool_status"] == "calling":
                dm["tool_status"] = "done"

        # 清理空的 assistant 消息
        display_messages = [
            dm for dm in display_messages
            if not (dm["role"] == "assistant" and not dm.get("content"))
        ]

        # 保存完整展示消息到 MongoDB（包含子代理消息）
        # 多轮对话：追加到已有消息，而非覆盖（每轮调用 save 时 display_messages 仅含当前轮）
        if resume_data is None:
            # 初始对话：追加到已有历史
            existing = await agent_loader.get_display_messages(thread_id) or []
            all_messages = existing + display_messages
        else:
            # resume 模式：display_messages 已包含历史（load 时获取），直接保存
            all_messages = display_messages
        await agent_loader.save_display_messages(thread_id, all_messages)
        write_debug_log(debug_log, "SAVE_DISPLAY", {
            "thread_id": thread_id,
            "total_count": len(all_messages)
        })

        # 流结束，发送 done 事件
        write_debug_log(debug_log, "STREAM_DONE", {
            "thread_id": thread_id,
            "total_content_len": len(collected_content)
        })

        yield usage_sse()
        yield create_sse_message({
            "type": "done",
            "thread_id": thread_id,
            "content": collected_content
        })

    except Exception as e:
        write_debug_log(debug_log, "STREAM_ERROR", {"error": str(e)})
        yield create_sse_message({
            "type": "error",
            "message": str(e)
        })


# ============================================================
# API 端点
# ============================================================

@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    流式对话接口

    接收用户消息，返回 SSE 流式响应
    支持实时显示 AI 生成的内容和工具调用信息
    检测到 Human-in-the-Loop 中断时会发送 interrupt 事件
    """
    thread_id = request.thread_id or str(uuid.uuid4())
    user_id = request.user_id

    return StreamingResponse(
        stream_chat_response(message=request.message, thread_id=thread_id, user_id=user_id),
        media_type="text/event-stream",  # text/json, text/html, image/jpg
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/chat/{thread_id}/resume")
async def chat_resume(thread_id: str, request: ResumeRequest):
    """
    中断恢复接口

    当中断发生后，前端收集用户决策/补充数据，通过此端点恢复 Agent 执行。
    request.resume 的格式取决于中断类型：
    - 数据补充: {"supplement": "用户输入的补充信息"}
    - HITL 审批: {"decisions": [{"type": "approve"}]} 或 [{"type": "reject"}]
    """
    return StreamingResponse(
        stream_chat_response(thread_id=thread_id, resume_data=request.resume, user_id=request.user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/chat/{thread_id}")
async def get_chat_state(thread_id: str):
    """获取会话状态"""
    try:
        messages = await agent_loader.get_current_messages(thread_id)

        message_list = []
        for i, msg in enumerate(messages):
            message = Message(
                id=f"msg-{i}",
                role=msg.get("role", "assistant"),
                content=msg.get("content", ""),
                created_at=datetime.now(),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            )
            message_list.append(message)

        return ChatResponse(
            thread_id=thread_id,
            messages=message_list
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat/{thread_id}/history")
async def get_chat_history(
    thread_id: str,
    limit: int = Query(50, ge=1, le=100)
):
    """获取会话历史状态列表"""
    try:
        states = await agent_loader.get_state_history(thread_id, limit)

        return {
            "thread_id": thread_id,
            "states": [
                {
                    "config": state.config,
                    "values": state.values,
                    "created_at": state.created_at,
                    "parent_config": state.parent_config
                }
                for state in states
            ]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
