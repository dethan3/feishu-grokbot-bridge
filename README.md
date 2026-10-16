# feishu-grokbot-bridge

把飞书（Lark）机器人收到的消息转发到 **Grok Bot webhook routine** 的轻量桥接器。纯 Python 标准库 + [`lark-cli`](https://github.com/larksuite)，无需公网回调地址。

## 它做什么

- 通过 `lark-cli event consume im.message.receive_v1 --as bot` 以长连接方式订阅消息事件（无需公网 IP / 回调 URL）
- 只转发白名单用户（`FEISHU_OWNER_OPEN_IDS`）的消息
- 群聊：只有 **@本机器人** 或 **@所有人** 的消息才会转发；单聊全部转发
- 按 `message_id` 去重（持久化，重启不重复）
- 以 `Authorization: Bearer $FEISHU_WEBHOOK_KEY` POST 如下 JSON：
  ```json
  {"message_id":"om_...","chat_id":"oc_...","chat_type":"p2p|group","sender_open_id":"ou_...",
   "message_type":"text","text":"...","create_time":"1700000000000"}
  ```
- 失败的请求写入磁盘队列，每 60 秒按顺序重试（有积压时新消息也先入队，保证顺序）
- 单实例锁；watchdog 负责进程死亡/事件总线断开时重启，并每 30 分钟主动重连
- `scripts/reply.sh` 以机器人身份回复消息，失败自动入队重试

## 架构

```
飞书开放平台 ──长连接──> lark-cli event bus ──NDJSON──> bridge/forwarder.py
                                                        │ 过滤(owner/群@) → 去重(seen_ids.txt)
                                                        ├─ POST ─> Grok Bot webhook
                                                        └─ 失败 ─> <work>/outbox-webhook/ ─(60s 重试)
Grok Bot ── scripts/reply.sh ──> lark-cli im +messages-reply ─(失败)─> <work>/outbox/ ─(60s 重试)
scripts/watchdog.sh: 每 60s 健康检查（进程存活 + `lark-cli event status`），每 30min 预防性重连
```

每个实例的状态都在 `BRIDGE_WORK_DIR`（默认 `run/default`）：`forwarder.log`、`seen_ids.txt`、队列目录、pid/lock 文件。

## 飞书权限配置

在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用，启用「机器人」能力，然后：

| 权限 | 用途 | 必需 |
|---|---|---|
| `im:message.p2p_msg:readonly` | 接收单聊消息 | 是 |
| `im:message.group_at_msg:readonly` | 接收群里 @机器人 的消息 | 群聊需要 |
| `im:message.group_msg` | 接收群内所有消息（敏感权限，本项目仍只转发 @bot/@all） | 可选 |
| `im:message:send_as_bot` | `reply.sh` 回复消息 | 回复需要 |

- 事件订阅：添加 `im.message.receive_v1`，订阅方式选 **长连接（WebSocket）**。
- 修改权限/事件后需 **创建新版本并发布**（管理员审核通过后生效）。
- **外部群**：机器人默认不能进外部群。需在应用设置中允许「对外共享 / 外部群可用」，并且群需允许添加外部机器人；否则加不进群或收不到事件。
- `@所有人` 能否触发事件取决于权限：仅有 `group_at_msg` 时，部分情况下 @所有人 不推送，需要 `im:message.group_msg`。

## 安装

```bash
# 1. 安装并登录 lark-cli（放在 ~/.local/bin 或 PATH 中）
lark-cli config init         # 填入 App ID / App Secret
lark-cli event status        # 确认可用
# 2. 获取代码
git clone https://github.com/dethan3/feishu-grokbot-bridge.git && cd feishu-grokbot-bridge
cp .env.example .env && chmod 600 .env && $EDITOR .env
```

需要 Python ≥ 3.8、bash、`flock`（util-linux）。

## 配置

| 变量 | 说明 |
|---|---|
| `FEISHU_OWNER_OPEN_IDS` | 允许的发送者 open_id，逗号分隔 |
| `FEISHU_BOT_OPEN_ID` | 本机器人 open_id，用于识别群内 @；留空则群聊只转发 @所有人 |
| `FEISHU_WEBHOOK_URL` | Grok Bot webhook routine 地址 |
| `FEISHU_WEBHOOK_KEY` | Bearer key |
| `BRIDGE_WORK_DIR` | 实例目录（相对仓库根目录或绝对路径） |
| `LARK_CLI` / `LARK_CLI_EXTRA_ARGS` | lark-cli 路径 / 额外全局参数（多机器人时选择不同配置） |
| `RETRY_EVERY` `SEEN_MAX` `WATCHDOG_CHECK_EVERY` `WATCHDOG_RECONNECT_EVERY` | 可选调优 |

webhook URL/KEY 每次发送时重新读取，轮换 key 无需重启。

**多个机器人**：每个机器人一个配置文件 + 独立 `BRIDGE_WORK_DIR`，例如 `bots/a.env`（`BRIDGE_WORK_DIR=run/a`）与 `bots/b.env`（`BRIDGE_WORK_DIR=run/b`），所有脚本第一个参数传配置文件路径。每个机器人需要各自的 lark-cli 应用凭证（通过 `LARK_CLI_EXTRA_ARGS` 或不同的 `HOME`/配置目录区分）。

## 运行

```bash
scripts/start.sh [bots/a.env]      # 启动（已在运行则跳过）
scripts/stop.sh  [bots/a.env]      # 停止
nohup scripts/watchdog.sh [bots/a.env] >/dev/null 2>&1 &   # 守护
tail -f run/default/forwarder.log
BRIDGE_ENV_FILE=.env scripts/reply.sh <chat_id> <message_id> "收到"
```

不传参数时使用仓库根目录的 `.env`。

## Watchdog

`scripts/watchdog.sh` 每 60 秒检查：forwarder 进程是否存活；`lark-cli event status` 是否显示 `Bus: running` 且 `Active consumers` ≥ 1。任一失败即重启；另外每 30 分钟预防性重启一次，规避长连接“静默断开”。日志写入同一个 `forwarder.log`（前缀 `WATCHDOG`）。

## 重启恢复

机器重启后进程不会自动恢复。推荐 crontab：

```cron
@reboot cd /path/to/feishu-grokbot-bridge && scripts/start.sh .env && nohup scripts/watchdog.sh .env >/dev/null 2>&1 &
```

或写一个 systemd user service 运行 `scripts/watchdog.sh`（watchdog 会在 60 秒内拉起 forwarder）。去重表和队列都在磁盘上，重启后自动继续重试。

## 故障排查

- **静默断开（进程在但收不到消息）**：长连接可能无报错地失效。看 `lark-cli event status`；watchdog 的 30 分钟重连就是为此。手动：`scripts/stop.sh && scripts/start.sh`。
- **单聊正常、群里收不到事件**：检查 `im:message.group_at_msg:readonly` 是否已开通 **且新版本已发布**；机器人是否在群里；是否真的 @ 了机器人（而非同名用户）；外部群见上文。日志中 `skip ... group message without @bot/@all` 说明事件到了但被过滤——检查 `FEISHU_BOT_OPEN_ID`。
- **日志里 `skip ... sender not owner`**：把发送者的 open_id（日志里可见）加入 `FEISHU_OWNER_OPEN_IDS`。注意 open_id 是 **按应用区分** 的，不同机器人看到的同一个人 open_id 不同。
- **`queued ...`**：webhook 不可达或返回非 2xx，检查 URL/KEY；队列在 `<work>/outbox-webhook/`。
- **`another forwarder is running`**：同一实例目录已有进程；多机器人请使用不同 `BRIDGE_WORK_DIR`。

## 测试

```bash
python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/pytest -q
```

---

## English

**feishu-grokbot-bridge** forwards Feishu/Lark bot messages to a Grok Bot webhook routine. It wraps `lark-cli event consume im.message.receive_v1 --as bot` (long connection, no public callback URL), forwards only messages from whitelisted open_ids (`FEISHU_OWNER_OPEN_IDS`), and in groups only when the message @-mentions this bot (`FEISHU_BOT_OPEN_ID`) or @all. It dedupes by `message_id`, POSTs a compact JSON payload with `Authorization: Bearer $FEISHU_WEBHOOK_KEY`, queues failures on disk and retries every 60 s in order, and holds a single-instance lock per work dir. `scripts/watchdog.sh` restarts it if the process dies or the event bus goes down, and reconnects every 30 min to avoid silent disconnects. Run several bots with one env file and work dir each.

Quick start: `cp .env.example .env`, fill it in, `scripts/start.sh`, `nohup scripts/watchdog.sh &`. Required scopes: `im:message.p2p_msg:readonly`, `im:message.group_at_msg:readonly` (groups), optionally `im:message.group_msg`; subscribe to `im.message.receive_v1` via long connection and publish a new app version. License: MIT.
