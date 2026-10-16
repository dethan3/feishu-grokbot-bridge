# feishu-grokbot-bridge

把飞书（Lark）机器人收到的消息转发到 **Grok Bot webhook routine** 的轻量桥接器。纯 Python 标准库 + [`lark-cli`](https://github.com/larksuite/cli)（长连接，无需公网 IP / 回调地址）。

## 一键安装（Ubuntu / Debian / CentOS / Alibaba Cloud Linux，x86_64 / arm64）

```bash
curl -fsSL https://raw.githubusercontent.com/dethan3/feishu-grokbot-bridge/main/install.sh | sudo bash
```

安装脚本会：安装 `python3` 等依赖（apt / dnf / yum）→ 安装打过补丁的 `lark-cli`（1.0.98 + oapi-sdk-go v3.12.2，修复长连接“假死”；随仓库 `bin/` 提供，校验 SHA256；不可用时回退官方 npm `@larksuite/cli`）→ 交互式询问配置 → 写入 `/etc/feishu-grokbot-bridge.env`（chmod 600）→ 安装并启动 systemd 服务 `feishu-grokbot-bridge`（`Restart=always`、`RestartSec=3`、开机自启）。即使通过管道运行，提示也会从 `/dev/tty` 读取；密钥输入不回显。

需要提前准备：

| 项目 | 在哪里找 |
|---|---|
| App ID（`cli_...`） | [飞书开放平台](https://open.feishu.cn/app) → 你的应用 → **凭证与基础信息** |
| App Secret | 同上 **凭证与基础信息**（安装时隐藏输入；会用 `lark-cli config init --app-secret-stdin` 在线校验） |
| 主人 open_id（`ou_...`，可多个逗号分隔） | 应用内的用户 open_id；可在开放平台「API 调试台」调用获取用户信息，或看本项目日志中 `skip ... from ou_...` |
| 机器人 open_id（`ou_...`） | 安装时会用 App 凭证自动获取并作为默认值，直接回车即可 |
| Grok Bot webhook URL 与 key | Grok Bot 中对应 webhook routine 的侧边栏（key 隐藏输入） |

可选参数：`--dry-run`（只打印不改动）、`--no-start`；环境变量 `GH_PROXY=https://你的镜像/`（国内访问 GitHub 慢时给 github.com 下载加前缀）；也可预先设置 `FEISHU_APP_ID` `FEISHU_APP_SECRET` `FEISHU_OWNER_OPEN_IDS` `FEISHU_BOT_OPEN_ID` `FEISHU_WEBHOOK_URL` `FEISHU_WEBHOOK_KEY` 实现无人值守安装。重复运行即升级，回车保留旧值。

```bash
journalctl -u feishu-grokbot-bridge -f            # 实时日志
systemctl status|restart|stop feishu-grokbot-bridge
LARKSUITE_CLI_CONFIG_DIR=/var/lib/feishu-grokbot-bridge/lark-cli lark-cli event status
```

**注意：同一个飞书应用只能有一个消费者。** 新服务器跑起来后，务必停掉其它机器上的旧转发器，否则事件会被两边分摊。

## 它做什么

- `lark-cli event consume im.message.receive_v1 --as bot` 长连接接收消息
- **角色**：主人（`FEISHU_OWNER_OPEN_IDS`）的消息在单聊和群聊都转发，`role=owner`；其他人（guest）只有在**群聊中 @本机器人 或 @所有人** 时才转发，`role=guest`
- 跳过机器人自己发的消息；按 `message_id` 持久化去重；超过 15 分钟的旧消息（重连后的补投）跳过
- **轮询兜底**：每 90 秒用 API 拉取已知群/会话最近 5 分钟的消息，补发长连接漏掉的
- 以 `Authorization: Bearer $FEISHU_WEBHOOK_KEY` POST：
  ```json
  {"message_id":"om_...","chat_id":"oc_...","chat_type":"p2p|group","sender_open_id":"ou_...","role":"owner|guest",
   "message_type":"text","text":"...","create_time":"1700000000000"}
  ```
- 失败写入磁盘队列，每 60 秒按顺序重试；`scripts/reply.sh <chat_id> <message_id> <文本>` 以机器人身份回复，失败自动入队重试
- **轻量 watchdog**（内置）：直连网络下依赖 SDK 自动重连；只有当 bus.log 出现断线且之后 60 秒内没有任何总线活动时，才停止事件总线并退出，由 systemd 3 秒后拉起

## 配置（`/etc/feishu-grokbot-bridge.env`）

| 变量 | 说明 |
|---|---|
| `FEISHU_APP_ID` | 应用 App ID |
| `FEISHU_OWNER_OPEN_IDS` | 主人 open_id，逗号分隔 |
| `FEISHU_BOT_OPEN_ID` | 机器人 open_id（识别 @bot、跳过自身消息） |
| `FEISHU_WEBHOOK_URL` / `FEISHU_WEBHOOK_KEY` | Grok Bot webhook 地址 / Bearer key（每次发送时重新读取，改 key 无需重启） |
| `BRIDGE_WORK_DIR` | 状态目录，默认 `/var/lib/feishu-grokbot-bridge`（seen_ids、队列、chats.txt） |
| `LARKSUITE_CLI_CONFIG_DIR` / `LARK_CLI` | lark-cli 配置目录 / 可执行文件 |
| 可选 | `FEISHU_SEED_CHATS` `POLL_EVERY`(90) `POLL_WINDOW`(300) `STALE_SEC`(900) `RETRY_EVERY`(60) `SEEN_MAX`(5000) `WATCHDOG_QUIET_AFTER_DISCONNECT`(60) `WATCHDOG_SILENCE_MAX`(0=关闭) `BRIDGE_LOG_FILE` |

App Secret 不写入此文件，只保存在 lark-cli 配置目录（权限 700）。

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

## 无 systemd 的环境

安装脚本会自动改用 `scripts/start.sh`（循环重启）与 `scripts/stop.sh`，日志在 `$BRIDGE_WORK_DIR/forwarder.log`；需要自行加 `@reboot /opt/feishu-grokbot-bridge/scripts/start.sh` 到 crontab。

## 故障排查

- **群里收不到**：确认 `im:message.group_at_msg:readonly` 已开通且新版本已发布、机器人在群里、确实 @ 了机器人。日志 `skip ... guest without @bot/@all` 表示事件到了但被过滤。
- **`skip ... guest outside group`**：非主人单聊不转发；把对方 open_id 加进 `FEISHU_OWNER_OPEN_IDS`（open_id 按应用区分）。
- **`queued ...`**：webhook 不可达或非 2xx，检查 URL/KEY；队列在 `/var/lib/feishu-grokbot-bridge/outbox-webhook/`。
- **`watchdog restart`**：检测到断线后总线静默，已自动重启，属正常自愈。

## 自行编译 lark-cli 补丁版

`bin/lark-cli-linux-{amd64,arm64}.gz` 由 [larksuite/cli](https://github.com/larksuite/cli) v1.0.98 将 `github.com/larksuite/oapi-sdk-go/v3` 升级到 v3.12.2 后编译（`CGO_ENABLED=0 go build -trimpath -ldflags "-s -w"`），校验和见 `bin/SHA256SUMS`。

## 测试

```bash
python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/pytest -q
bash install.sh --dry-run
```

---

## English

One-line install on a Linux server (root): `curl -fsSL https://raw.githubusercontent.com/dethan3/feishu-grokbot-bridge/main/install.sh | sudo bash`. It installs python3 and a patched lark-cli (1.0.98 + oapi-sdk-go v3.12.2), prompts (via /dev/tty) for App ID, App Secret (hidden), owner open_id(s), bot open_id (auto-detected), webhook URL and key (hidden), writes `/etc/feishu-grokbot-bridge.env` (600), and runs the `feishu-grokbot-bridge` systemd service (`Restart=always`). Owners are forwarded everywhere; guests only in groups when they @bot or @all; own messages skipped; dedupe by message_id; messages older than 15 min skipped; 90 s polling backup; disk retry queue. Logs: `journalctl -u feishu-grokbot-bridge -f`. Only one consumer per Feishu app may run at a time. License: MIT.
