# xbotgh-plugin — GitHub Code Reviewer for xbot

[![xbot](https://img.shields.io/badge/xbot-channel%20plugin-blue)](https://github.com/ai-pivot/xbot)
[![Python](https://img.shields.io/badge/python-3.10%2B-green)](https://www.python.org/)

> 当有人在 PR 中 **@你的 Bot** 时，自动拉取 diff 进行 AI Code Review，将评审结果发表为行级评论。

## ✨ 功能

- 🔔 **@mention 触发**：PR 评论中 @你的 Bot 即可触发 CR
- 📍 **行级评论**：评审精确到代码行，作者一眼定位问题
- 🔄 **重新审查**：修改后再次 @Bot，自动检查之前的问题是否已修复
- 🚦 **APPROVE / REQUEST_CHANGES / COMMENT**：根据代码质量选择最终结论
- 🛡️ **白名单/黑名单**：仓库级 + 用户级，支持 `org/*` 通配符
- 🔁 **Poll / Webhook 双模式**：默认轮询模式无需公网 IP，10 秒内响应
- 📝 **持久化防重复**：同一评论只触发一次，重启不丢失

## 🏗️ 架构

```
GitHub PR Comment (@bot)
       │
       ▼
  Poll 轮询 (每 10s) / Webhook
       │
       ├─ ✅ @mention 检测
       ├─ ✅ 白名单/黑名单过滤
       ├─ ✅ is it re-review?
       │
       ▼
  send_inbound → xbot LLM
       │
       ├─ get_pr_files_detail  → 获取带行号的 diff
       ├─ get_previous_reviews → 获取历史评审（re-review 时）
       ├─ LLM 分析代码...
       └─ post_line_review     → 发表行级评审
```

## 📋 前置条件

- **Python 3.10+** + `pip`
- **xbot** 运行中
- **GitHub App**（需自行创建，2 分钟）

### 安装依赖

```bash
pip install PyJWT cryptography requests
```

## 🚀 安装插件

```bash
# 1. 克隆到 xbot 插件目录
mkdir -p ~/.xbot/plugins/my.ghbot
cd ~/.xbot/plugins/my.ghbot
git clone https://github.com/ai-pivot/xbotgh-plugin.git .

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 重载插件
# 在 xbot TUI 中执行: /reload-plugins
# 或通过 CLI: xbot-cli reload-plugins
```

## 🔧 创建 GitHub App

1. 前往 https://github.com/settings/apps/new
2. 填写信息：

| 配置项 | 值 |
|--------|-----|
| App name | `My Code Reviewer`（任意） |
| Webhook URL | 留空（Poll 模式不需要） |
| Webhook secret | 留空（Poll 模式不需要） |
| **Repository permissions** | |
| └ Pull requests | **Read and write** |
| └ Issues | **Read and write** |
| └ Contents | **Read-only** |
| **Subscribe to events** | ☑ Issue comments |

3. 创建后：
   - 生成 **Private Key** → 下载 PEM 文件
   - 记录 **App ID**（页面顶部数字）
4. 安装 App 到你的仓库/组织 → 记录 **Installation ID**（URL 中的数字）

## ⚙️ 配置

在 xbot TUI 中：`/settings` → Channels → github → 编辑

或手动编辑 `~/.xbot/config.json`：

```json
{
  "channels": {
    "github": {
      "enabled": "true",
      "mode": "poll",
      "app_id": "你的 App ID",
      "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----",
      "installation_id": "0",
      "bot_username": "你的 Bot GitHub 用户名",
      "poll_interval": "10",
      "monitored_repos": "",
      "whitelist_repos": "",
      "blacklist_repos": "",
      "whitelist_users": "",
      "blacklist_users": ""
    }
  }
}
```

> **私钥替代方案**：如果不想把 PEM 明文放在 config 中，可以设置环境变量 `GITHUB_PRIVATE_KEY_FILE` 指向 PEM 文件路径，然后 `private_key` 字段留空。

### 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | toggle | `false` | 启用/禁用 |
| `mode` | select | `poll` | `poll`（轮询，无需公网IP）或 `webhook`（需公网IP） |
| `app_id` | text | — | GitHub App ID |
| `private_key` | text | — | App 私钥（PEM 格式） |
| `installation_id` | number | `0` | Installation ID（0 = 自动查找） |
| `bot_username` | text | `code-reviewer-bot` | Bot 的 GitHub 用户名（用于检测 @mention） |
| `poll_interval` | number | `10` | 轮询间隔（秒） |
| `monitored_repos` | text | — | 监控仓库列表（逗号分隔，留空=自动发现全部安装仓库） |
| `whitelist_repos` | text | — | 仓库白名单（逗号分隔，支持 `org/*` 通配符） |
| `blacklist_repos` | text | — | 仓库黑名单 |
| `whitelist_users` | text | — | 用户白名单 |
| `blacklist_users` | text | — | 用户黑名单 |
| `trigger_on_open` | toggle | `false` | PR 创建时是否自动触发 |
| `webhook_secret` | password | — | Webhook 签名密钥（仅 webhook 模式） |
| `webhook_port` | number | `9876` | Webhook 监听端口（仅 webhook 模式） |

## 📝 使用

在任意 PR 评论中输入：

```
@my-bot review this PR
```

> ⚠️ GitHub App 的完整 @mention 格式是 `@你的Bot名[bot]`（如 `@code-reviewer[bot]`），
> 在 GitHub UI 中输入时会自动补全为超链接。直接手打 `@bot名` 也能被检测到。

Bot 会：
1. 👀 添加眼睛反应表示已收到
2. 获取 PR diff 和文件详情
3. 进行 AI 代码审查
4. 在对应代码行发表行级评论

### 重新审查

修改代码后再次 @Bot，Bot 会自动识别为 re-review：
- 获取之前的评审记录
- 逐一检查之前的问题是否已修复
- 已修复的标 ✅，未修复的标 🔴

## 🛠️ Channel Tools

插件向 LLM 注入以下工具（仅 github channel 的会话中可见）：

| 工具 | 说明 |
|------|------|
| `get_pr_info` | 获取 PR 基本信息 |
| `get_pr_diff` | 获取 PR 完整 diff |
| `get_pr_files` | 获取变更文件列表 |
| `get_pr_files_detail` | 获取文件详细 diff（含行号） |
| `get_previous_reviews` | 获取 PR 已有的 review 历史 |
| `post_review_comment` | 发表整体 review 评论 |
| `post_line_review` | 发表行级 review 评论（推荐） |
| `post_pr_comment` | 发表普通 issue 评论 |

## 🔒 安全

- **`.gitignore` 已配置**：`private-key.pem`、`processed_comments.json` 等不会提交
- **私钥支持环境变量**：`GITHUB_PRIVATE_KEY_FILE` 避免明文写入 config
- **Webhook 签名验证**（webhook 模式）：支持 `X-Hub-Signature-256`

## 🐛 故障排查

| 症状 | 检查 |
|------|------|
| Bot 不响应 @mention | `enabled` 是否为 `true`；`bot_username` 是否正确 |
| `@bot[bot]` 无超链接 | 正常现象，手动输入即可；不影响功能 |
| API 调用失败 | `app_id`、`private_key`、`installation_id` 是否正确 |
| 重启后重复触发 | `processed_comments.json` 已持久化，不会重复 |
| @mention 不触发但有老评论 | 启动前的评论会静默标记，启动后的才触发 |
| rate limit 耗尽 | 检查 `monitored_repos` 是否过多；ETag 缓存已启用 |

## 📄 License

MIT
