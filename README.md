# feishu-new-topic

> Start a Feishu message with `#new ` and it lands in a **brand-new thread** — which is a **brand-new Hermes session**.
> The session you were in keeps running, untouched, no context bleed. Two rules, no escapes, no exceptions.

飞书里 **`#new ` 开头** 发消息，或**口述「开启一个新话题」** = 立刻开一条新话题（= 新 session），
内容直接落进新会话。正在跑的主会话完全不动，上下文也不串。

**只有两条规则，没有转义、没有例外。**

## 怎么用

| 你发 | 结果 |
|---|---|
| `#new 帮我核一下这张报价单` | 立刻开话题 `omt_…`，这句落进新 session，机器人在话题里回 |
| `#new`（单独一条） | 也开话题，机器人回一句"等你下一步指令"，你在话题里接着说 |
| 说 `开启一个新话题 帮我核一下这张报价单` | 同上（语音专用，见下） |
| `开启一个新话题`（单独一句） | 也开话题，机器人回一句"等你下一步指令"，你在话题里接着说 |
| `＃ｎｅｗ　全角` / `#NEW` | 同上（全角、大小写都认） |
| 在**话题内**发 `#new …` | 不建新话题（飞书没有嵌套话题），只去掉前缀并附一句提示 |

其它一切都按普通消息走：`# 标题`、`# 注释`、`# new 标题`、`## 二级标题`、`#!/bin/bash`
都**不受影响**，粘贴 Markdown 或代码进来不需要做任何转义。

## 为什么是 `#new` 不是单个符号

单个 `# ` 是 Markdown 一级标题、终端 root 提示符、Python/shell 注释的开头，而 `%` `$` `>` `*` `+` `/`
也各自是 LaTeX 注释、终端提示符、邮件引用、列表、diff、路径的开头 —— 都会跟粘贴进来的正文撞车，
撞了就只能发明转义（比如 `##`），等于让人记例外。

`#new` 是个**连写 token**：Markdown 标题要求 `#` 后必须跟空格，所以 `#new` 在文档里不是标题；
代码注释只会写成 `# new …` 带空格；终端提示符也是 `# `。因此这个 token 几乎不会被粘贴内容命中 ——
**不需要转义，也没有例外**。

## 语音路线

语音触发认这些说法（ASR 在字之间插空格也认：`开启 一个 新话题`）：

开启一个新话题 / 开启一个新的话题 / 开启一个新对话 / 开始一个新话题 /
打开一个新话题 / 新开一个话题 / 新开一个对话

短语后面可以跟标点（。，：），剩下的就是正文；只发短语不带正文时，机器人回一句等你指令。

## 为什么这么设计

* 会话键 = `platform + chat_id + thread_id`，所以「新话题 = 新 session」是内核本来就有的行为，
  缺的只是飞书这一层没有「新建空对话」的入口。
* `pre_gateway_dispatch` 钩子在 auth / 命令解析 / `build_session_key()` **之前** 跑，
  且 `SessionSource` 可变 → 插件可以「先建话题 → 改 `event.source.thread_id` → 放行」，
  这条消息就落进新 session。**不改核心、不改 config.yaml。**
* 建话题的办法：调 `messages/{id}/reply` + `reply_in_thread=true` 回一条一像素占位符，
  从响应里取 `thread_id`，随后**撤回**那条占位符 —— 话题留下，根消息是用户自己那条，
  话题里不残留机器人噪声（实测：撤回后话题仍在，只剩根消息）。
* 飞书偶发 `[2200] Internal Error`（回复刚发出的消息时更容易撞上）→ 3 次退避重试。
* 建话题失败（网络/权限/适配器不在线）→ 降级：去掉 `% ` 并在正文末尾附一句失败原因，
  消息照常在原会话处理，不会静默吞消息。
* 同一条消息重复投递 → 复用已建话题（`ctx.state` 记 15 分钟），不会开两条。

## 安装

```bash
hermes plugins install kaigregliu/hermes-feishu-new-topic
hermes plugins enable feishu-new-topic
hermes gateway restart
```

手动装：仓库放到 `~/.hermes/plugins/feishu-new-topic/`（目录名要等于 `plugin.yaml` 的 `name`），
再 `hermes plugins enable feishu-new-topic && hermes gateway restart`。

前提：Hermes Agent 的飞书适配器已配好并在线；插件本身不引入任何 Python 依赖
（`lark-oapi` 由 Hermes 自带，且只在真正建话题时懒加载）。

确认加载：

```bash
hermes plugins doctor feishu-new-topic     # 期望：OK … 1 hook(s)
grep feishu-new-topic ~/.hermes/logs/agent.log | tail
```

停用：

```bash
hermes plugins disable feishu-new-topic && hermes gateway restart
```

## 自测

```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/feishu-new-topic/tests/test_plugin.py
```

16 条用例：`#new` 触发/去前缀、全角、普通消息不触发、单个 `#` 系列原样放行、`#news` 边界、只发 `#new`、
话题内降级、建话题失败降级、重复投递复用话题、未授权不触发、语音各种说法、
长正文照常开、语音只发短语。

## 已知边界

* 群里仍然要 @ 机器人才收得到消息（本插件不改变这一点）。
* 话题标题仍走内核约定：**我在这条话题里的第一条回复的开头**，所以新话题的第一轮回复要以
  `📌 …` 开头。
* 会话被 `/new` 之后主聊天进入新 session，`#new` 行为不受影响。
