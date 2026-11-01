# feishu-new-topic

**[English](#english) · [中文](#中文)**

A Hermes Agent plugin for Feishu/Lark. `#new ` opens a new topic — a new Hermes session — while the
session you were in keeps running.

---

<a id="english"></a>

## English

```
you:  #new check this quote for me
      └─ opens topic omt_… → this message lands in a fresh session
         the session you were in keeps running, untouched
```

### Why not `/new`?

`/new` is a *reset*: it interrupts the turn running in the session, drops its queued follow-ups and
in-flight delegations, and clears the conversation. If a long task is running in the main chat,
`/new` kills it.

`#new` is a *second lane*. The message is routed to a different session (a Feishu topic), so the
work in progress keeps running exactly as it was — same model, same tool calls, same partial output.
Nothing is interrupted, nothing is cleared.

| You want | Use |
|---|---|
| Start something unrelated while the current task keeps running | `#new …` |
| Throw away the current conversation and everything running in it | `/new` |

### Rules

Two triggers. No escape character, no exceptions.

| You send | Result |
|---|---|
| `#new <body>` | Opens a topic on that message; `<body>` is the first turn of the new session |
| `#new` (alone) | Opens a topic; the body becomes a short placeholder telling the agent a topic was just opened |
| `#NEW …` / `＃ｎｅｗ …` | Same — case and full-width forms are folded |
| `#new …` **inside a topic** | No nested topics in Feishu: the prefix is stripped, a note is appended, the message stays in the current session |
| anything else | Untouched, including `# heading`, `# comment`, `# new title`, `## h2`, `#!/bin/sh`, `#news` |

The trigger must be the **first characters** of the message. Two consequences worth knowing: in a
group, if you @-mention someone else alongside the bot, Feishu's adapter prepends a
`[Mentioned: …]` hint and the trigger no longer starts the text, so it will not fire; and `#new`
followed by a slash command (`#new /reset`) opens the topic and then runs that command in the new
session, because the rewritten text goes through normal command parsing.

Why `#new` and not `#` or `%`: a bare `#` opens a Markdown heading, a root shell prompt and a
Python/shell comment; `%`, `$`, `>` collide with LaTeX, prompts and mail quotes. Pasting
documentation or code would then require an escape rule. `#new` is a single token that Markdown
never reads as a heading (`#` must be followed by a space) and that code only writes as `# new `.
It is effectively collision-free, so there is no escape syntax at all.

### Voice

Voice notes are matched on these phrasings (spacing inside the phrase, as ASR produces it, is
tolerated: `开启 一个 新话题`). Trailing punctuation is allowed; whatever follows is the body.

```
开启一个新话题 · 开启一个新的话题 · 开启一个新对话 · 开启一个新的对话
开始一个新话题 · 开始一个新对话 · 打开一个新话题 · 新开一个话题 · 新开一个对话
```

Phrase alone → topic opened, waiting for instruction.

### How it works

Hermes derives the session key from `platform + chat_id + thread_id`, so a new topic already means a
new session. What was missing is that Feishu has no "new conversation" button for a bot.

The plugin registers `pre_gateway_dispatch`, which Hermes runs at ingress **before** auth, command
parsing and `build_session_key()`, and whose `event.source` is mutable:

1. `POST /openapi/im/v1/messages/{message_id}/reply` with `reply_in_thread=true` on the user's own
   message — Feishu returns the `thread_id`.
2. That placeholder reply (`·`) is deleted. The topic survives; its root is the user's message, so
   nothing from the bot lingers in the new topic.
3. `event.source.thread_id` is set to the new id; the gateway re-reads `event.source` and
   `build_session_key()` then yields a fresh session key.
4. The trigger is stripped and the message is dispatched as a normal rewrite.

No core changes, no `config.yaml` changes.

### Install

```bash
hermes plugins install kaigregliu/hermes-feishu-new-topic
hermes plugins enable feishu-new-topic
hermes gateway restart
```

Manual: clone into any directory under `~/.hermes/plugins/` — the directory name is only a
convention, the manifest's `name` is the key — then
`hermes plugins enable feishu-new-topic && hermes gateway restart`.

Verify:

```bash
hermes plugins validate ~/.hermes/plugins/feishu-new-topic   # expect: Validation passed
hermes plugins doctor feishu-new-topic                       # expect: OK … 1 hook(s)
```

**Requirements:** Hermes Agent with the Feishu adapter configured and online. The manifest declares
`requires_hermes: ">=0.22"`; the plugin was built and verified against Hermes 0.22.5. No Python
dependencies — `lark_oapi` ships with Hermes and is imported lazily, only when a topic is actually
created.

### Behaviour when things fail

| Situation | What happens |
|---|---|
| No live Feishu adapter | Message dispatches normally in the current session, with a note saying why |
| Reply API error / no `thread_id` | Same; the Feishu error code is included in the note. Up to 3 attempts with backoff, retrying only on `2200 Internal Error` and network errors |
| Placeholder delete fails | The topic still works; a `·` reply may stay in it (logged as a warning) |
| Duplicate delivery of the same message | The already-created `thread_id` is reused for 15 min (last 40 messages kept in the plugin state file) |
| Unauthorised sender | Not rewritten at all — the message follows Hermes's normal authorisation path |

### Limits

- Feishu only. Not a generic multi-platform plugin; it is a no-op on every other platform.
- Groups: the bot must actually receive the message. That is Feishu's `require_mention` behaviour
  (default `true`, configurable in the adapter's `extra`), not something this plugin changes.
- One level only: a topic cannot contain a topic.
- No topic-title API is involved. Feishu derives the label shown in the message list from the first
  message in the topic; the plugin deletes its placeholder, so the agent's first reply is what
  shows. Feishu's API has no topic-title field to set.
- Depends on gateway/adapter internals (`_adapters_for_profile`, the adapter's `_client` and
  `_run_blocking`). These exist in Hermes 0.22 and are used by Hermes's own bundled plugins, but a
  future Feishu-adapter refactor can break this plugin — it degrades to "topic could not be opened",
  never to a dropped message.

### Disclosure

- **Network:** two Feishu OpenAPI calls per trigger — `messages/{id}/reply` (create the topic) and
  `messages/{id}/delete` (remove the placeholder). Nothing else is called; no telemetry.
- **Credentials:** none handled. The plugin uses the Feishu client Hermes already authenticated.
- **State:** a dedupe map (15 min TTL, ≤40 entries) in the plugin context. Hermes persists that to
  `~/.hermes/plugin-data/<plugin>/state.json`; it holds only `{thread_id, timestamp}` pairs.
- **Hermes surfaces:** the `pre_gateway_dispatch` hook, plus read-only access to the gateway's live
  adapters (`_adapters_for_profile` / `adapters`) and that adapter's Feishu client. No rebinding of
  core objects.

### Test

```bash
~/.hermes/hermes-agent/venv/bin/python tests/test_plugin.py   # 16/16 passed
```

Covers: trigger and prefix stripping, full-width and case forms, plain text untouched, `#`-forms
untouched, `#news` boundary, bare `#new`, in-topic degradation, creation failure, duplicate reuse,
unauthorised sender, voice phrasings (including ASR spacing and phrase-inside-body), long body.

### License

MIT — see [LICENSE](LICENSE). Authors: kai & 雪碧.
Built for [Hermes Agent](https://hermes-agent.nousresearch.com/docs).

---

<a id="中文"></a>

## 中文

```
你：  #new 帮我核一下这张报价单
      └─ 开一条话题 omt_… → 这句话进一个全新的 session
         你原来那个会话照跑不误
```

### 为什么不直接用 `/new`

`/new` 是**重置**：它会打断该会话里正在跑的 turn，丢掉排队的跟进和在飞的后台任务，然后清空对话。
主会话跑着长任务时，`/new` 会把它打死。

`#new` 是**另开一条车道**：这条消息被路由到另一个 session（飞书话题），正在跑的活原样继续 ——
同一个模型、同一批工具调用、已经产出的部分都还在。不打断，也不清空。

| 你想要 | 用哪个 |
|---|---|
| 当前任务继续跑，同时开一件不相干的事 | `#new …` |
| 丢掉当前对话和里面在跑的一切 | `/new` |

### 规则

两条触发，没有转义符，没有例外。

| 你发 | 结果 |
|---|---|
| `#new 正文` | 在这条消息上开一条话题，正文就是新会话的第一轮 |
| `#new`（单独一条） | 也开话题，正文变成一句「刚开了一条新话题，等你下一步指令」 |
| `#NEW …` / `＃ｎｅｗ …` | 一样 —— 大小写、全角都认 |
| 在**话题内**发 `#new …` | 飞书没有嵌套话题：去掉前缀、末尾附一句提示，消息留在当前会话 |
| 其它一切 | 原样不动，包括 `# 标题`、`# 注释`、`# new 标题`、`## 二级标题`、`#!/bin/sh`、`#news` |

触发词必须是消息的**头几个字符**。两个要知道的后果：群里同时 @ 了别人时，飞书适配器会在正文前插一句
`[Mentioned: …]`，`#new` 就不在开头了，不会触发；`#new` 后面跟斜杠命令（`#new /reset`）会先开话题、
再在新会话里执行那条命令 —— 因为改写后的正文走的是正常命令解析。

为什么用 `#new` 而不是单个 `#` 或 `%`：单个 `#` 是 Markdown 标题、终端 root 提示符、Python/shell
注释的开头；`%`、`$`、`>` 又分别是 LaTeX 注释、终端提示符、邮件引用 —— 粘贴文档或代码就会撞车，
撞了就得发明转义规则。`#new` 是个连写 token：Markdown 标题要求 `#` 后跟空格，代码注释只会写成
`# new …` 带空格，所以它几乎不会被粘贴内容命中，**不需要转义**。

### 语音

语音按下面这些说法命中（ASR 在字之间插空格也认：`开启 一个 新话题`）。短语后面可以跟标点，
剩下的就是正文。

```
开启一个新话题 · 开启一个新的话题 · 开启一个新对话 · 开启一个新的对话
开始一个新话题 · 开始一个新对话 · 打开一个新话题 · 新开一个话题 · 新开一个对话
```

只发短语不带正文 → 开话题，等你下一步指令。

### 原理

Hermes 的会话键来自 `platform + chat_id + thread_id`，所以「新话题 = 新 session」本来就是内核行为；
缺的是飞书这一层没有「给机器人新建一个空对话」的入口。

插件注册 `pre_gateway_dispatch`：它在 auth、命令解析、`build_session_key()` **之前**触发，
`event.source` 可变：

1. 对用户那条消息调 `POST /openapi/im/v1/messages/{message_id}/reply` 并带
   `reply_in_thread=true` —— 飞书返回 `thread_id`；
2. 立刻撤回那条一像素占位回复（`·`）。话题留下，根消息是用户自己那条，话题里不残留机器人噪声；
3. 把 `event.source.thread_id` 改成新话题 id；网关会重读 `event.source`，于是
   `build_session_key()` 算出新的会话键；
4. 去掉触发词，消息按正常 rewrite 分发。

不改内核、不改 `config.yaml`。

### 安装

```bash
hermes plugins install kaigregliu/hermes-feishu-new-topic
hermes plugins enable feishu-new-topic
hermes gateway restart
```

手动装：克隆到 `~/.hermes/plugins/` 下任意目录 —— 目录名只是约定，真正的键是 manifest 里的 `name`
—— 再 `hermes plugins enable feishu-new-topic && hermes gateway restart`。

确认：

```bash
hermes plugins validate ~/.hermes/plugins/feishu-new-topic   # 期望：Validation passed
hermes plugins doctor feishu-new-topic                       # 期望：OK … 1 hook(s)
```

**前提：** Hermes Agent 的飞书适配器已配好并在线。manifest 声明 `requires_hermes: ">=0.22"`，
插件在 Hermes 0.22.5 上开发并验证。不需要任何 Python 依赖 —— `lark_oapi` 随 Hermes 一起装，
且只在真正建话题时才懒加载。

### 失败时怎么办

| 情况 | 结果 |
|---|---|
| 飞书适配器不在线 | 消息照常在当前会话处理，末尾附一句原因 |
| 接口报错 / 没返回 `thread_id` | 同上，提示里带飞书错误码。最多 3 次退避重试，只在 `2200 Internal Error` 与网络错误时重试 |
| 撤回占位消息失败 | 话题照样可用，只是可能留一条 `·`（日志里是 warning） |
| 同一条消息重复投递 | 15 分钟内复用已建的话题（保留最近 40 条，存在插件状态文件里） |
| 未授权的发送者 | 完全不改写，消息走 Hermes 原本的授权流程 |

### 边界

- 只支持飞书，不是通用的多平台插件；在其它平台上它什么都不做。
- 群聊里机器人必须真收到这条消息 —— 那是飞书的 `require_mention` 行为（默认 `true`，可在适配器
  `extra` 里配），本插件不改变它。
- 只有一层：话题里不能再开话题。
- 不涉及任何话题标题 API。飞书消息列表里那个标题来自话题里的第一条消息；插件把占位消息撤掉了，
  所以显示的是 agent 的第一句回复。飞书的 API 没有可设置的话题标题字段。
- 依赖网关/适配器内部属性（`_adapters_for_profile`、适配器的 `_client` 与 `_run_blocking`）。
  这些在 Hermes 0.22 里存在，Hermes 自带的插件也在用，但将来飞书适配器重构可能让它失效 ——
  失效时降级成「开不了话题」，不会丢消息。

### 披露

- **网络：** 每次触发两次飞书 OpenAPI 调用 —— `messages/{id}/reply`（建话题）和
  `messages/{id}/delete`（撤掉占位）。没有其它调用，没有埋点。
- **凭据：** 不碰。用的是 Hermes 已经登录好的飞书客户端。
- **状态：** 插件上下文里存一份去重表（15 分钟 TTL，最多 40 条）。Hermes 会把它落到
  `~/.hermes/plugin-data/<plugin>/state.json`，内容只有 `{thread_id, 时间戳}` 这样的键值。
- **用到的 Hermes 面：** `pre_gateway_dispatch` 钩子，以及对网关在线适配器
  （`_adapters_for_profile` / `adapters`）和该适配器飞书客户端的**只读**访问。不改写内核对象的任何方法。

### 自测

```bash
~/.hermes/hermes-agent/venv/bin/python tests/test_plugin.py   # 16/16 passed
```

覆盖：触发与去前缀、全角与大小写、普通消息不动、单个 `#` 系列不动、`#news` 边界、只发 `#new`、
话题内降级、建话题失败降级、重复投递复用、未授权不触发、语音各种说法（含 ASR 断字与句中不触发）、
长正文照常开。

### 许可

MIT —— 见 [LICENSE](LICENSE)。作者：kai & 雪碧。
为 [Hermes Agent](https://hermes-agent.nousresearch.com/docs) 而写。
