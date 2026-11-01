"""feishu-new-topic — 飞书里「#new 」开头（或口述「开启一个新话题」）= 开一条新话题（新 session）。

原理
----
``pre_gateway_dispatch`` 钩子在 auth / 命令解析 / ``build_session_key()`` **之前** 触发，
拿得到 live ``event`` 与 ``gateway``，且 ``SessionSource`` 可变。所以：

1. 看到消息以 ``#new `` 开头（或语音短语）→ 调 Feishu ``messages/{id}/reply`` + ``reply_in_thread=true``
   在**用户这条消息**上开一条话题（返回体里带 ``thread_id``）；
2. 立刻撤回那条占位回复（话题保留，根消息是用户自己那条）；
3. 把 ``event.source.thread_id`` 改成该话题 id → 后面的 ``build_session_key()`` 自然算出
   **新会话键** → 这条消息落进全新 session，与正在跑的主会话互不干扰；
4. 去掉 ``#new `` 前缀再把文本交给 agent。

用 ``#new``（连写 token）而不是单个 ``#``：``# `` 是 Markdown 标题、终端 root 提示符、
Python/shell 注释的开头，粘贴正文时会撞车；而 ``#new`` 在 Markdown 里不是标题
（标题要求 ``# + 空格``），代码注释里也只会写成 ``# new …`` 带空格。
所以这个 token 几乎不会被粘贴内容命中 —— **没有转义、没有例外**。

副作用为零：不改核心、不改 config.yaml（启用由用户手动）。

已知边界
--------
* 话题内不能再开话题（飞书无嵌套话题）→ 在话题里发 ``#new`` 只会去掉前缀并附提示。
* 群聊里默认要 @ 机器人才收得到消息，本插件不改变这一点。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

logger = logging.getLogger(__name__)

# 打字触发：固定 token「#new」+ 空格（唯一写法，无例外）。
# 为什么是 #new 而不是单个 # 或 %：# 单独出现是 Markdown 标题、终端 root 提示符、Python/shell
# 注释的开头，粘贴正文就会撞车；而「#new」在 Markdown 里不是标题（标题要求「# + 空格」），
# 代码注释里也只会写成「# new …」带空格 —— 所以「#new」这个连写 token 几乎不会被粘贴内容命中。
# 全角（＃ｎｅｗ）、大小写（#NEW/#New）都认；「#new」后面必须跟空格或到此为止。
_MARKER = re.compile(r"^#new(?:[ \u3000]+(?=\S)|[ \u3000]*$)", re.I)
# 全角 → 半角（一一对应，translate 不改变长度，所以下标能直接映射回原文）
_MARKER_FW = str.maketrans({"＃": "#", "ｎ": "n", "Ｎ": "N", "ｅ": "e", "Ｅ": "E", "ｗ": "w", "Ｗ": "W", "　": " "})
_MARKER_HEAD = 16  # 只看开头这么多个字符，够容下 #new + 若干空格

# 语音触发：说这几个固定的短语（ASR 可能插空格/标点，也可能把「的」带上）
_VOICE_PHRASES = (
    "开启一个新的话题",
    "开启一个新的对话",
    "开启一个新话题",
    "开启一个新对话",
    "开始一个新话题",
    "开始一个新对话",
    "打开一个新话题",
    "新开一个话题",
    "新开一个对话",
)


def _phrase_pattern(phrase: str) -> str:
    """字符之间允许任意空白 —— 抗 ASR 断字（「开启 一个 新话题」）。"""
    return r"[\s\u3000]*".join(re.escape(ch) for ch in phrase)


_VOICE_TRIGGER = re.compile(
    "^(?:%s)[\\s\\u3000，。！？：；、,.!?:;—\\-]*"
    % "|".join(_phrase_pattern(p) for p in _VOICE_PHRASES),
    re.S,
)

# 建话题需要一条回复，内容用最小占位符，随后立即撤回
PLACEHOLDER_TEXT = "·"
_THREAD_ATTEMPTS = 3  # 飞书偶发 [2200] Internal Error，重试能过
# 只发了触发词、没有正文时，交给 agent 的占位文本
_EMPTY_BODY_TEXT = "（刚开了一条新话题，等你下一步指令）"
_STATE_KEY = "recent"
_STATE_MAX = 40
_STATE_TTL = 900.0  # 秒


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def register(ctx):
    async def _pre(event=None, gateway=None, **_kw):
        try:
            return await _handle(ctx, event, gateway)
        except Exception:  # 插件出错绝不能打断入站消息流
            logger.warning("[feishu-new-topic] hook failed", exc_info=True)
            return None

    ctx.register_hook("pre_gateway_dispatch", _pre)
    logger.info("[feishu-new-topic] pre_gateway_dispatch registered")


# --------------------------------------------------------------------------- #
# 主逻辑
# --------------------------------------------------------------------------- #
def _parse_trigger(text: str):
    """解析触发前缀，返回 ``(正文, 类型)``；不是触发就返回 None。

    类型：``marker``（``#new ``）、``voice``（「开启一个新话题」）。
    只有这两条规则，没有转义、没有例外。
    """
    body = _marker_body(text)
    if body is not None:
        return body, "marker"

    match = _VOICE_TRIGGER.match(text)
    if match:
        return text[match.end():], "voice"

    return None


def _marker_body(text: str):
    """``#new`` + 空格（或到此为止）→ 返回去掉 token 后的正文；否则 None。"""
    if not text:
        return None
    head = text[:_MARKER_HEAD].translate(_MARKER_FW)  # translate 一一对应，长度不变
    match = _MARKER.match(head)
    if not match:
        return None
    return text[match.end():]


async def _handle(ctx, event, gateway):
    if event is None or gateway is None:
        return None
    source = getattr(event, "source", None)
    if source is None or _platform(source) != "feishu":
        return None

    text = getattr(event, "text", "") or ""

    parsed = _parse_trigger(text)
    if parsed is None:
        return None
    body = parsed[0]

    if not body.strip():
        body = _EMPTY_BODY_TEXT
    else:
        body = body.strip()

    # 未授权的人不该能触发建话题（钩子在 auth 之前跑，自己判一次）。
    # 探针不存在或本身出错时一律按「不是触发」处理（fail closed）：消息照常走内核的授权流程，
    # 既不会让未授权的人触发飞书写操作，也不会留下一条没人回复的孤儿话题。
    auth = getattr(gateway, "_is_user_authorized_for_source", None)
    if not callable(auth):
        logger.warning("[feishu-new-topic] gateway has no authorization probe; passing through")
        return None
    try:
        if not auth(source):
            return None
    except Exception:
        logger.warning("[feishu-new-topic] auth probe failed; passing through", exc_info=True)
        return None

    if getattr(source, "thread_id", None):
        # 已经在话题里：飞书没有嵌套话题，去掉前缀并在末尾说明
        return {"action": "rewrite", "text": f"{body}\n\n（#new 开新话题只在主聊天生效，本条按普通消息处理）"}

    message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
    if not message_id:
        return None

    cached = _recall(ctx, message_id)
    if cached:
        source.thread_id = cached
        return {"action": "rewrite", "text": body}

    adapter = _feishu_adapter(gateway, source)
    if adapter is None:
        logger.warning("[feishu-new-topic] no live feishu adapter; passing through")
        return {"action": "rewrite", "text": f"{body}\n\n（#new 开新话题失败：飞书适配器不在线，本条仍在主会话）"}

    try:
        thread_id = await _create_thread(adapter, message_id)
    except Exception as exc:
        logger.warning("[feishu-new-topic] thread creation failed: %s", exc)
        return {"action": "rewrite", "text": f"{body}\n\n（#new 开新话题失败：{exc}；本条仍在主会话）"}

    if not thread_id:
        return {"action": "rewrite", "text": f"{body}\n\n（#new 开新话题失败：飞书未返回话题 id；本条仍在主会话）"}

    source.thread_id = thread_id
    _remember(ctx, message_id, thread_id)
    logger.info("[feishu-new-topic] opened topic %s for message %s", thread_id, message_id)
    return {"action": "rewrite", "text": body}


# --------------------------------------------------------------------------- #
# Feishu 调用
# --------------------------------------------------------------------------- #
async def _create_thread(adapter, message_id: str) -> str:
    """在 message_id 上开一条话题（reply_in_thread），撤回占位回复，返回 thread_id。"""
    client = getattr(adapter, "_client", None)
    if client is None:
        raise RuntimeError("飞书客户端未初始化")

    content = json.dumps({"text": PLACEHOLDER_TEXT}, ensure_ascii=False)
    request = _build_reply_request(adapter, content, message_id)

    response = None
    thread_id = None
    placeholder_id = None
    last_error = "飞书未返回话题 id"
    for attempt in range(_THREAD_ATTEMPTS):
        try:
            response = await _call(adapter, client.im.v1.message.reply, request)
        except Exception as exc:  # 网络抖动：重试
            last_error = f"{type(exc).__name__}: {exc}"
            logger.info("[feishu-new-topic] reply attempt %d failed: %s", attempt + 1, exc)
        else:
            data = getattr(response, "data", None)
            thread_id = getattr(data, "thread_id", None) if data is not None else None
            placeholder_id = getattr(data, "message_id", None) if data is not None else None
            if thread_id:
                break
            code = getattr(response, "code", "?")
            msg = getattr(response, "msg", "no thread_id in reply response")
            last_error = f"[{code}] {msg}"
            # 业务错误（权限/参数）重试没用，只有飞书侧 2200 这类才值得再试
            if code != 2200:
                break
            logger.info("[feishu-new-topic] reply attempt %d got [2200], retrying", attempt + 1)
        if attempt + 1 < _THREAD_ATTEMPTS:
            await asyncio.sleep(0.4 * (attempt + 1))

    if not thread_id:
        raise RuntimeError(last_error)

    if placeholder_id:
        await _recall_placeholder(adapter, client, placeholder_id)
    return thread_id


def _build_reply_request(adapter, content: str, message_id: str):
    """构造 ``messages/{id}/reply`` 请求。

    优先直接用 SDK 构造：adapter 的 ``ReplyMessageRequestBody`` 等名字是**懒绑定**的，
    没连过飞书时是 None（``_sdk_build`` 会退回 ``SimpleNamespace``，真发时会炸）。
    SDK 不可用才退到 adapter 的构造器。
    """
    try:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .content(content)
            .msg_type("text")
            .reply_in_thread(True)
            .uuid(str(uuid.uuid4()))
            .build()
        )
        return ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    except ImportError:
        build_body = getattr(adapter, "_build_reply_message_body", None)
        build_req = getattr(adapter, "_build_reply_message_request", None)
        if not (callable(build_body) and callable(build_req)):
            raise RuntimeError("lark_oapi 不可用，且适配器未提供构造器") from None
        body = build_body(
            content=content, msg_type="text", reply_in_thread=True, uuid_value=str(uuid.uuid4())
        )
        return build_req(message_id, body)


async def _recall_placeholder(adapter, client, message_id: str) -> None:
    """撤回占位回复：话题留在用户那条消息上，话题里不残留机器人噪声。"""
    try:
        from lark_oapi.api.im.v1 import DeleteMessageRequest

        request = DeleteMessageRequest.builder().message_id(message_id).build()
        await _call(adapter, client.im.v1.message.delete, request)
    except Exception:
        logger.warning("[feishu-new-topic] placeholder recall failed", exc_info=True)


async def _call(adapter, fn, request):
    run_blocking = getattr(adapter, "_run_blocking", None)
    if callable(run_blocking):
        return await run_blocking(fn, request)
    return await asyncio.to_thread(fn, request)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _platform(source) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def _feishu_adapter(gateway, source):
    adapters = {}
    lookup = getattr(gateway, "_adapters_for_profile", None)
    if callable(lookup):
        try:
            adapters = lookup(getattr(source, "profile", None)) or {}
        except Exception:
            logger.debug("[feishu-new-topic] adapter lookup by profile failed", exc_info=True)
    if not adapters:
        adapters = getattr(gateway, "adapters", None) or {}
    for platform, adapter in adapters.items():
        if str(getattr(platform, "value", platform)).lower() == "feishu":
            return adapter
    return None


def _state(ctx):
    return getattr(ctx, "state", None)


def _recall(ctx, message_id: str):
    """同一消息重复投递时复用已建的话题，不再开第二条。"""
    state = _state(ctx)
    if state is not None:
        try:
            entry = (state.get(_STATE_KEY, {}) or {}).get(message_id)
        except Exception:
            entry = None
        if isinstance(entry, dict):
            if entry.get("ts") and _now() - float(entry["ts"]) < _STATE_TTL:
                return entry.get("thread_id")
    return None


def _remember(ctx, message_id: str, thread_id: str) -> None:
    state = _state(ctx)
    if state is None:
        return
    try:
        data = dict(state.get(_STATE_KEY, {}) or {})
    except Exception:
        data = {}
    data[message_id] = {"thread_id": thread_id, "ts": _now()}
    if len(data) > _STATE_MAX:
        for key in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - _STATE_MAX]:
            data.pop(key, None)
    try:
        state.set(_STATE_KEY, data)
    except Exception:
        logger.debug("[feishu-new-topic] state write failed", exc_info=True)


def _now() -> float:
    import time

    return time.time()
