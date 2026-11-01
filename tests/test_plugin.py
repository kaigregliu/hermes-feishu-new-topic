"""feishu-new-topic 自测（FakeCtx，无需 pytest）。

跑法：
    ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/feishu-new-topic/tests/test_plugin.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self):
        self.hooks = {}
        self.state = FakeState()

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_command(self, *a, **kw):
        raise AssertionError("not used")


class Platform:
    def __init__(self, value):
        self.value = value


class FakeSource:
    def __init__(self, thread_id=None, chat_type="dm"):
        self.platform = Platform("feishu")
        self.chat_id = "oc_test"
        self.chat_type = chat_type
        self.thread_id = thread_id
        self.profile = None
        self.message_id = "om_in"


class FakeEvent:
    def __init__(self, text, source=None, message_id="om_in"):
        self.text = text
        self.source = source or FakeSource()
        self.message_id = message_id


class FakeData:
    def __init__(self, thread_id="omt_new001", message_id="om_placeholder"):
        self.thread_id = thread_id
        self.message_id = message_id


class FakeResponse:
    def __init__(self, data=None, code=0, msg="ok"):
        self.data = data
        self.code = code
        self.msg = msg

    def success(self):
        return self.code == 0


class FakeMessageAPI:
    def __init__(self, owner):
        self.owner = owner

    def reply(self, request):
        self.owner.calls.append(("reply", request))
        if self.owner.fail:
            raise RuntimeError("boom")
        return FakeResponse(FakeData(thread_id=self.owner.thread_id,
                                     message_id=self.owner.placeholder_id))

    def delete(self, request):
        self.owner.calls.append(("delete", request))
        return FakeResponse(FakeData())


class FakeIM:
    def __init__(self, owner):
        self.v1 = type("V1", (), {})()
        self.v1.message = FakeMessageAPI(owner)


class FakeClient:
    def __init__(self, owner):
        self.im = FakeIM(owner)


class FakeAdapter:
    def __init__(self, thread_id="omt_new001", fail=False, placeholder_id="om_placeholder"):
        self.calls = []
        self.thread_id = thread_id
        self.fail = fail
        self.placeholder_id = placeholder_id
        self._client = FakeClient(self)

    # 用请求元组模拟 SDK 的构建器，避免测试依赖 lark_oapi
    def _build_reply_message_body(self, *, content, msg_type, reply_in_thread, uuid_value):
        return {"content": content, "msg_type": msg_type, "reply_in_thread": reply_in_thread}

    def _build_reply_message_request(self, message_id, body):
        return {"message_id": message_id, "body": body}

    async def _run_blocking(self, fn, request):
        return fn(request)


class FakeGateway:
    def __init__(self, adapter):
        self.adapters = {Platform("feishu"): adapter}
        self.authorized = True

    def _adapters_for_profile(self, profile):
        return self.adapters

    def _is_user_authorized_for_source(self, source):
        return self.authorized


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def build(adapter=None, gateway=None):
    import importlib

    mod = importlib.import_module("__init__")
    importlib.reload(mod)
    ctx = FakeCtx()
    mod.register(ctx)
    adapter = adapter or FakeAdapter()
    gateway = gateway or FakeGateway(adapter)
    hook = ctx.hooks["pre_gateway_dispatch"]
    return mod, ctx, adapter, gateway, hook


def run(hook, event, gateway):
    return asyncio.run(hook(event=event, gateway=gateway))


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def _req_parts(request):
    """兼容两种请求形态：SDK 对象（真实路径）或 FakeAdapter 的 dict（回退路径）。"""
    if isinstance(request, dict):
        return request.get("message_id"), request.get("body", {})
    body = getattr(request, "request_body", None)
    return getattr(request, "message_id", None), body


def _reply_in_thread(body):
    return body.get("reply_in_thread") if isinstance(body, dict) else getattr(body, "reply_in_thread", None)


def _content_text(body):
    raw = body.get("content") if isinstance(body, dict) else getattr(body, "content", "")
    try:
        return json.loads(raw or "{}").get("text", "")
    except (TypeError, ValueError):
        return ""


@case
def triggers_and_strips():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("#new 帮我核一下这张报价单")
    result = run(hook, event, gateway)
    assert result == {"action": "rewrite", "text": "帮我核一下这张报价单"}, result
    assert event.source.thread_id == "omt_new001", event.source.thread_id
    kinds = [c[0] for c in adapter.calls]
    assert kinds == ["reply", "delete"], kinds
    message_id, body = _req_parts(adapter.calls[0][1])
    assert message_id == "om_in", message_id
    assert _reply_in_thread(body) is True, body
    assert _content_text(body) == "·", body


@case
def fullwidth_trigger():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("＃ｎｅｗ　全角也行")
    result = run(hook, event, gateway)
    assert result == {"action": "rewrite", "text": "全角也行"}, result
    assert event.source.thread_id == "omt_new001"


@case
def plain_text_ignored():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("普通消息，不动")
    assert run(hook, event, gateway) is None
    assert adapter.calls == []
    assert event.source.thread_id is None


@case
def hash_alone_is_ordinary_text():
    """单个 # 不再触发：Markdown 标题、代码注释、shebang 都原样走，不需要任何转义。"""
    mod, ctx, adapter, gateway, hook = build()
    for text in ("# 这是字面标题", "## 二级标题", "# 注释", "#!/bin/bash", "# new 带空格的标题"):
        event = FakeEvent(text)
        assert run(hook, event, gateway) is None, text
    assert adapter.calls == []


@case
def marker_needs_word_boundary():
    """#new 后面必须跟空格或到此为止；#newx / #news 不算。"""
    mod, ctx, adapter, gateway, hook = build()
    for text in ("#newx 标题", "#news 今天", "#newtopic"):
        event = FakeEvent(text)
        assert run(hook, event, gateway) is None, text
    assert adapter.calls == []


@case
def marker_alone_opens_topic():
    """只发 #new 也开话题（正文为空 → 用占位文本）。"""
    mod, ctx, adapter, gateway, hook = build()
    for text in ("#new", "#new  ", "#NEW"):
        event = FakeEvent(text)
        result = run(hook, event, gateway)
        assert event.source.thread_id == "omt_new001", text
        assert "新话题" in result["text"], (text, result)


@case
def inside_topic_degrades():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("#new 话题里再来一条", source=FakeSource(thread_id="omt_old"))
    result = run(hook, event, gateway)
    assert result["action"] == "rewrite"
    assert result["text"].startswith("话题里再来一条")
    assert "只在主聊天生效" in result["text"]
    assert adapter.calls == []
    assert event.source.thread_id == "omt_old"


@case
def failure_degrades():
    mod, ctx, adapter, gateway, hook = build(adapter=FakeAdapter(fail=True))
    event = FakeEvent("#new 会失败的")
    result = run(hook, event, gateway)
    assert result["action"] == "rewrite"
    assert "开新话题失败" in result["text"]
    assert event.source.thread_id is None


@case
def duplicate_message_reuses_topic():
    mod, ctx, adapter, gateway, hook = build()
    first = FakeEvent("#new 同一条", message_id="om_dup")
    run(hook, first, gateway)
    calls_after_first = len(adapter.calls)
    second = FakeEvent("#new 同一条", message_id="om_dup")
    result = run(hook, second, gateway)
    assert result == {"action": "rewrite", "text": "同一条"}
    assert second.source.thread_id == "omt_new001"
    assert len(adapter.calls) == calls_after_first, "重复投递不该再建话题"


@case
def unauthorized_user_ignored():
    mod, ctx, adapter, gateway, hook = build()
    gateway.authorized = False
    event = FakeEvent("#new 陌生人")
    assert run(hook, event, gateway) is None
    assert adapter.calls == []


class _GatewayWithoutAuthProbe:
    """网关没有授权探针（旧版/非标准网关）—— 插件必须 fail closed，不能先写飞书。"""

    def __init__(self, adapter):
        self.adapters = {Platform("feishu"): adapter}

    def _adapters_for_profile(self, profile):
        return self.adapters


@case
def missing_auth_probe_fails_closed():
    mod, ctx, adapter, gateway, hook = build()
    gateway = _GatewayWithoutAuthProbe(adapter)
    event = FakeEvent("#new 没有授权探针")
    assert run(hook, event, gateway) is None
    assert adapter.calls == [], adapter.calls
    assert event.source.thread_id is None


@case
def voice_phrase_with_content():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("开启一个新话题 帮我核一下这张报价单")
    result = run(hook, event, gateway)
    assert result == {"action": "rewrite", "text": "帮我核一下这张报价单"}, result
    assert event.source.thread_id == "omt_new001"


@case
def voice_phrase_asr_tolerant():
    mod, ctx, adapter, gateway, hook = build()
    for text in (
        "开启 一个 新话题，帮我核一下",
        "开启一个新的话题：帮我核一下",
        "开始一个新话题。帮我核一下",
        "新开一个话题帮我核一下",
    ):
        event = FakeEvent(text)
        result = run(hook, event, gateway)
        assert result == {"action": "rewrite", "text": "帮我核一下"}, (text, result)


@case
def voice_phrase_alone_opens_topic():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("开启一个新话题。")
    result = run(hook, event, gateway)
    assert event.source.thread_id == "omt_new001"
    assert "新话题" in result["text"], result


@case
def voice_phrase_inside_body_not_trigger():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("刚才说要开启一个新话题，你怎么看")
    assert run(hook, event, gateway) is None
    assert adapter.calls == []


@case
def markdown_doc_paste_untouched():
    mod, ctx, adapter, gateway, hook = build()
    event = FakeEvent("# 报价单核对\n\n- 单价对不上\n- 数量少 2 件\n- 运费没算")
    assert run(hook, event, gateway) is None
    assert adapter.calls == []
    assert event.source.thread_id is None


@case
def long_body_opens_topic():
    """没有长度限制：#new 开头的长文（含换行/序号）一样开话题，不需要任何例外规则。"""
    mod, ctx, adapter, gateway, hook = build()
    body = "帮我核这份长清单\n1. 第一条\n2. 第二条\n3. 第三条\n4. 第四条"
    event = FakeEvent("#new " + body)
    result = run(hook, event, gateway)
    assert event.source.thread_id == "omt_new001"
    assert result["text"] == body, result


def main():
    failures = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
