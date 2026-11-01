# feishu-new-topic

A Hermes Agent plugin for Feishu/Lark: start a message with `#new ` and it opens a **new topic** —
which in Hermes is a **new session** — while the session you were in keeps running, untouched.

```
you:  #new check this quote for me
      └─ opens topic omt_… → this message lands in a fresh session
         the session you were in keeps running, untouched
```

## Why not `/new`?

`/new` is a *reset*: it interrupts the turn that is running in the session, drops its queued
follow-ups and in-flight delegations, and clears the conversation. If a long task is running in the
main chat, `/new` kills it.

`#new` is a *second lane*. The message is routed to a different session (a Feishu topic), so the
work in progress keeps running exactly as it was — same model, same tool calls, same partial output.
Nothing is interrupted, nothing is cleared.

| You want | Use |
|---|---|
| Start something unrelated while the current task keeps running | `#new …` |
| Throw away the current conversation and everything running in it | `/new` |

## Rules

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

## Voice

Voice notes are matched on these phrasings (spacing inside the phrase, as ASR produces it, is
tolerated: `开启 一个 新话题`). Trailing punctuation is allowed; whatever follows is the body.

```
开启一个新话题 · 开启一个新的话题 · 开启一个新对话 · 开启一个新的对话
开始一个新话题 · 开始一个新对话 · 打开一个新话题 · 新开一个话题 · 新开一个对话
```

Phrase alone → topic opened, waiting for instruction.

## How it works

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

## Install

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

## Behaviour when things fail

| Situation | What happens |
|---|---|
| No live Feishu adapter | Message dispatches normally in the current session, with a note saying why |
| Reply API error / no `thread_id` | Same; the Feishu error code is included in the note. Up to 3 attempts with backoff, retrying only on `2200 Internal Error` and network errors |
| Placeholder delete fails | The topic still works; a `·` reply may stay in it (logged as a warning) |
| Duplicate delivery of the same message | The already-created `thread_id` is reused for 15 min (last 40 messages kept in the plugin state file) |
| Unauthorised sender | Not rewritten at all — the message follows Hermes's normal authorisation path |

## Limits

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

## Disclosure

- **Network:** two Feishu OpenAPI calls per trigger — `messages/{id}/reply` (create the topic) and
  `messages/{id}/delete` (remove the placeholder). Nothing else is called; no telemetry.
- **Credentials:** none handled. The plugin uses the Feishu client Hermes already authenticated.
- **State:** a dedupe map (15 min TTL, ≤40 entries) in the plugin context. Hermes persists that to
  `~/.hermes/plugin-data/<plugin>/state.json`; it holds only `{thread_id, timestamp}` pairs.
- **Hermes surfaces:** the `pre_gateway_dispatch` hook, plus read-only access to the gateway's live
  adapters (`_adapters_for_profile` / `adapters`) and that adapter's Feishu client. No rebinding of
  core objects.

## Test

```bash
~/.hermes/hermes-agent/venv/bin/python tests/test_plugin.py   # 16/16 passed
```

Covers: trigger and prefix stripping, full-width and case forms, plain text untouched, `#`-forms
untouched, `#news` boundary, bare `#new`, in-topic degradation, creation failure, duplicate reuse,
unauthorised sender, voice phrasings (including ASR spacing and phrase-inside-body), long body.

## 中文速览

飞书里发 `#new 正文`（或口述「开启一个新话题」）= 在当条消息上开一条新话题，也就是**新 session**。
关键差别：`/new` 是**重置** —— 会打断主会话里正在跑的任务并清空对话；`#new` 是**另开一条车道**，
主会话里正在跑的活照跑不误。只有这一条触发规则，没有转义；话题里不能嵌套话题；
建话题失败就按普通消息处理并在末尾附一句原因。
安装 `hermes plugins install kaigregliu/hermes-feishu-new-topic`，
再 `hermes plugins enable feishu-new-topic && hermes gateway restart`。

## License

MIT — see [LICENSE](LICENSE). Authors: kai & 雪碧.
Built for [Hermes Agent](https://hermes-agent.nousresearch.com/docs).
