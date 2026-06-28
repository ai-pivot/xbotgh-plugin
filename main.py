#!/usr/bin/env python3
"""xbot gRPC channel 插件：GitHub Code Reviewer

当 GitHub App 被 @mention 时，自动对所在 PR 进行 Code Review 并评论。

架构:
  1. 插件进程接收 xbot 的 JSON-RPC（stdin/stdout）
  2. 收到 channel_config 后启动 Webhook HTTP 服务器
  3. GitHub webhook → 过滤 @mention + 白名单/黑名单 → send_inbound 到 xbot
  4. xbot LLM 处理消息 → 通过 channel_tools (get_pr_diff / post_review_comment) 完成 CR
"""

import sys
import os
import json
import time
import hmac
import hashlib
import logging
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler

from github_client import GitHubAppAuth, GitHubClient

# ---- 全局状态 ----

CONFIG: dict = {}
GITHUB_CLIENT: GitHubClient | None = None
BOT_USERNAME: str = "code-reviewer-bot"

# 已处理的评论 ID（防重复），持久化到文件，重启不丢失
_PROCESSED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_comments.json")
_PROCESSED_LOCK = threading.Lock()
PROCESSED_COMMENTS: set[int] = set()


def _load_processed():
    """从文件加载已处理的评论 ID。"""
    global PROCESSED_COMMENTS
    try:
        if os.path.exists(_PROCESSED_FILE):
            with open(_PROCESSED_FILE) as f:
                data = json.load(f)
                PROCESSED_COMMENTS = set(data.get("ids", []))
                log.info("从文件加载 %d 条已处理评论", len(PROCESSED_COMMENTS))
    except Exception as e:
        log.warning("加载已处理评论文件失败: %s", e)


def _save_processed():
    """保存已处理的评论 ID 到文件。"""
    try:
        # 只保留最近 10000 条，防止文件无限增长
        if len(PROCESSED_COMMENTS) > 10000:
            # set 无序，转 list 后截断
            ids = sorted(PROCESSED_COMMENTS)[-10000:]
            PROCESSED_COMMENTS.clear()
            PROCESSED_COMMENTS.update(ids)
        with open(_PROCESSED_FILE, "w") as f:
            json.dump({"ids": list(PROCESSED_COMMENTS)}, f)
    except Exception as e:
        log.warning("保存已处理评论文件失败: %s", e)


def _try_mark_processed(comment_id: int) -> bool:
    """原子地标记评论为已处理。

    Returns: True 表示首次标记成功（之前没处理过），False 表示已经处理过。
    线程安全，跨进程安全（通过文件持久化）。
    """
    with _PROCESSED_LOCK:
        if comment_id in PROCESSED_COMMENTS:
            return False
        PROCESSED_COMMENTS.add(comment_id)
        _save_processed()
        return True

# 日志输出到 stderr（stdout 用于 JSON-RPC 通信）
logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="[ghbot] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ghbot")

# ---- JSON-RPC 工具函数 ----


def write_stdout(obj: dict):
    """向 xbot 发送一条 JSON-RPC 消息。"""
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


_msg_counter = 0


def next_id() -> str:
    global _msg_counter
    _msg_counter += 1
    return f"ghbot-{_msg_counter}-{int(time.time() * 1000) % 1000000}"


def send_inbound(chat_id: str, content: str, sender_name: str, sender_id: str = ""):
    """向 xbot 推送一条 inbound 消息。"""
    write_stdout({
        "id": next_id(),
        "method": "send_inbound",
        "params": {
            "channel": "github",
            "chat_id": chat_id,
            "content": content,
            "sender_id": sender_id or chat_id,
            "sender_name": sender_name,
            "chat_type": "group",
        }
    })


# ---- 白名单 / 黑名单过滤 ----


def parse_list(value: str) -> list[str]:
    """解析逗号分隔的列表。"""
    if not value:
        return []
    return [item.strip().lower().strip("@") for item in value.split(",") if item.strip()]


def is_allowed(repo_full_name: str, username: str) -> tuple[bool, str]:
    """检查仓库和用户是否通过白名单/黑名单过滤。

    仓库白名单支持通配符: ai-pivot/* 表示该 org 下所有仓库。
    用户白名单支持 org 前缀: ai-pivot/* 表示该 org 下所有成员。

    Returns: (allowed, reason)
    """
    repo_lower = repo_full_name.lower()
    user_lower = username.lower()

    # 仓库黑名单（最高优先级）
    bl_repos = parse_list(CONFIG.get("blacklist_repos", ""))
    if repo_lower in bl_repos:
        return False, f"仓库 {repo_full_name} 在黑名单中"

    # 用户黑名单
    bl_users = parse_list(CONFIG.get("blacklist_users", ""))
    if user_lower in bl_users:
        return False, f"用户 @{username} 在黑名单中"

    # 仓库白名单（留空=全部允许），支持 org/* 通配符
    wl_repos = parse_list(CONFIG.get("whitelist_repos", ""))
    if wl_repos and not _repo_matches(repo_lower, wl_repos):
        return False, f"仓库 {repo_full_name} 不在白名单中"

    # 用户白名单（留空=全部允许），支持 org/* 通配符
    wl_users = parse_list(CONFIG.get("whitelist_users", ""))
    if wl_users and not _user_matches(user_lower, wl_users):
        return False, f"用户 @{username} 不在白名单中"

    return True, "ok"


def _repo_matches(repo_lower: str, patterns: list[str]) -> bool:
    """检查仓库是否匹配白名单模式列表。支持 org/* 通配符。"""
    for pat in patterns:
        if pat.endswith("/*"):
            org = pat[:-2]
            if repo_lower.startswith(org + "/"):
                return True
        elif pat == repo_lower:
            return True
    return False


def _user_matches(user_lower: str, patterns: list[str]) -> bool:
    """检查用户是否匹配白名单模式列表。支持 org/* 通配符。"""
    for pat in patterns:
        if pat == user_lower:
            return True
        # org/* 格式暂不支持精确匹配用户 org 归属（GitHub API 需额外调用）
        # 这里只做用户名精确匹配
    return True  # 如果有 org/* 格式，暂时放行（精确匹配已在上面处理）


def is_mentioned(text: str) -> bool:
    """检测评论中是否 @mention 了 bot。

    GitHub App 的 mention 格式有三种：
      @xbotgh[bot]   ← 完整格式（GitHub UI 渲染为超链接）
      @xbotgh         ← 简写格式（GitHub 不渲染链接，但 API 原文中会出现）
      @apps/xbotgh    ← App 路径格式
    """
    if not text:
        return False
    username = BOT_USERNAME.lower().lstrip("@")
    # 一次性匹配所有格式
    pattern = rf"@(?:{re.escape(username)}\[bot\]|apps/{re.escape(username)}|{re.escape(username)})\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """验证 GitHub Webhook 签名 (X-Hub-Signature-256)。"""
    if not secret:
        return True  # 未配置 secret 则跳过验证
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---- Channel Tools 声明 ----


def declare_tools():
    """向 xbot 声明 channel-scoped 工具。"""
    write_stdout({
        "type": "channel_tools",
        "tools": [
            {
                "name": "get_pr_info",
                "description": "获取 PR 基本信息（标题、描述、作者、分支等）",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                ],
            },
            {
                "name": "get_pr_diff",
                "description": "获取 PR 的完整 diff 内容",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                ],
            },
            {
                "name": "get_pr_files",
                "description": "获取 PR 变更文件列表（文件名、状态、增删行数）",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                ],
            },
            {
                "name": "get_pr_files_detail",
                "description": "获取 PR 变更文件的详细 diff（含 patch 内容和行号），用于行级评论。返回每个文件的 patch diff，从中可获取行号",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                ],
            },
            {
                "name": "get_previous_reviews",
                "description": "获取 PR 上已有的 review 历史（含 bot 之前的评审内容和行级评论），用于重新审查时检查之前的问题是否已修复",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                ],
            },
            {
                "name": "post_review_comment",
                "description": "在 PR 上发表 Code Review 评论（整体 review 形式，不针对特定行）",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                    {"name": "body", "type": "string", "description": "Review 内容（Markdown 格式）", "required": True},
                    {"name": "event", "type": "string", "description": "Review 类型：COMMENT（默认）、APPROVE、REQUEST_CHANGES", "required": False},
                ],
            },
            {
                "name": "post_line_review",
                "description": "发表带行级评论的 Code Review。可以在指定文件的指定行上添加评论，同时附上整体总结。强烈推荐使用此工具进行精细化的代码审查",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                    {"name": "body", "type": "string", "description": "Review 整体总结（Markdown 格式）", "required": True},
                    {"name": "event", "type": "string", "description": "Review 类型：COMMENT（默认）、APPROVE、REQUEST_CHANGES", "required": False},
                    {"name": "comments", "type": "array", "description": "行级评论列表，每个元素包含 path(文件路径)、body(评论)、line(行号)、side(RIGHT或LEFT)、start_line(多行起始,可选)", "required": False, "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "文件路径，必须与 diff 中的 filename 一致"},
                            "body": {"type": "string", "description": "评论内容（Markdown）"},
                            "line": {"type": "integer", "description": "目标行号（diff 中 + 或 - 后的数字）"},
                            "side": {"type": "string", "description": "RIGHT（修改后，默认）或 LEFT（修改前）"},
                            "start_line": {"type": "integer", "description": "多行评论的起始行号（可选）"},
                        },
                    }},
                ],
            },
            {
                "name": "post_pr_comment",
                "description": "在 PR 上发表普通评论（issue comment）",
                "parameters": [
                    {"name": "repo", "type": "string", "description": "仓库全名 owner/repo", "required": True},
                    {"name": "pr_number", "type": "integer", "description": "PR 编号", "required": True},
                    {"name": "body", "type": "string", "description": "评论内容（Markdown 格式）", "required": True},
                ],
            },
        ],
    })


# ---- Tool 执行 ----


def execute_tool(name: str, input_str: str) -> tuple[str, bool]:
    """执行 channel tool 调用。

    Returns: (content, is_error)
    """
    global GITHUB_CLIENT
    if not GITHUB_CLIENT:
        return "GitHub 客户端未初始化", True

    try:
        params = json.loads(input_str)
    except json.JSONDecodeError as e:
        return f"参数 JSON 解析失败: {e}", True

    repo = params.get("repo", "")
    pr_number = params.get("pr_number", 0)
    if not repo or not pr_number:
        return "缺少必需参数 repo 或 pr_number", True

    try:
        if name == "get_pr_info":
            info = GITHUB_CLIENT.get_pr_info(repo, pr_number)
            summary = {
                "number": info.get("number"),
                "title": info.get("title"),
                "body": (info.get("body") or "")[:3000],
                "state": info.get("state"),
                "user": info.get("user", {}).get("login"),
                "head": info.get("head", {}).get("ref"),
                "base": info.get("base", {}).get("ref"),
                "additions": info.get("additions"),
                "deletions": info.get("deletions"),
                "changed_files": info.get("changed_files"),
                "commits": info.get("commits"),
                "draft": info.get("draft"),
            }
            return json.dumps(summary, ensure_ascii=False, indent=2), False

        elif name == "get_pr_diff":
            diff = GITHUB_CLIENT.get_pr_diff(repo, pr_number)
            return diff, False

        elif name == "get_pr_files":
            files = GITHUB_CLIENT.get_pr_files(repo, pr_number)
            return json.dumps(files, ensure_ascii=False, indent=2), False

        elif name == "get_pr_files_detail":
            files = GITHUB_CLIENT.get_pr_files_detail(repo, pr_number)
            return json.dumps(files, ensure_ascii=False, indent=2), False

        elif name == "get_previous_reviews":
            prev = GITHUB_CLIENT.get_previous_reviews(repo, pr_number)
            return json.dumps(prev, ensure_ascii=False, indent=2), False

        elif name == "post_review_comment":
            body = params.get("body", "")
            event = params.get("event", "COMMENT")
            if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
                event = "COMMENT"
            result = GITHUB_CLIENT.post_review(repo, pr_number, body, event)
            return f"✅ Review 已发表 (event={event})\n{result.get('html_url', '')}", False

        elif name == "post_line_review":
            body = params.get("body", "")
            event = params.get("event", "COMMENT")
            if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
                event = "COMMENT"
            comments = params.get("comments", [])
            if not isinstance(comments, list):
                return "comments 参数必须是数组", True
            # 校验每个 comment 的必需字段
            for i, c in enumerate(comments):
                if not c.get("path"):
                    return f"comments[{i}] 缺少 path 字段", True
                if not c.get("body"):
                    return f"comments[{i}] 缺少 body 字段", True
                if not c.get("line"):
                    return f"comments[{i}] 缺少 line 字段", True
            result = GITHUB_CLIENT.post_review_with_comments(
                repo, pr_number, body, comments if comments else None, event
            )
            comment_count = len(comments)
            return (
                f"✅ Review 已发表 (event={event}, {comment_count} 条行级评论)\n"
                f"{result.get('html_url', '')}"
            ), False

        elif name == "post_pr_comment":
            body = params.get("body", "")
            result = GITHUB_CLIENT.post_comment(repo, pr_number, body)
            return f"✅ 评论已发表\n{result.get('html_url', '')}", False

        else:
            return f"未知工具: {name}", True

    except Exception as e:
        log.exception("工具执行失败: %s", name)
        return f"GitHub API 调用失败: {e}", True


# ---- 共享 CR 触发逻辑（webhook 和 poll 共用） ----


def has_bot_reviewed_before(repo_full: str, pr_number: int) -> bool:
    """检查 bot 是否已经在这个 PR 上发表过 review。"""
    if not GITHUB_CLIENT:
        return False
    try:
        prev = GITHUB_CLIENT.get_previous_reviews(repo_full, pr_number)
        bot_reviews = [r for r in prev["reviews"] if "[bot]" in r.get("user", "")]
        return len(bot_reviews) > 0
    except Exception:
        return False


def trigger_cr(repo_full: str, pr_number: int, comment_body: str, sender: str, comment_id: int):
    """触发一次 Code Review 请求，发送给 xbot LLM。

    被 webhook handler 和 poll loop 共同调用。
    自动检测：bot 之前 review 过 → re-review 模式。
    通过 _try_mark_processed 保证一条评论只触发一次（跨进程、跨重启）。
    """
    # 原子标记：如果已处理过则直接返回（防并发、防重启后重复）
    if not _try_mark_processed(comment_id):
        log.info("评论 %d 已处理过，跳过", comment_id)
        return

    log.info("收到 CR 请求: repo=%s pr=#%d by @%s", repo_full, pr_number, sender)

    # 添加 eyes 反应表示已收到
    if GITHUB_CLIENT:
        threading.Thread(
            target=GITHUB_CLIENT.add_reaction,
            args=(repo_full, comment_id, "eyes"),
            daemon=True,
        ).start()

    chat_id = f"{repo_full}#pr-{pr_number}"

    # 获取 PR 基础信息
    pr_summary = ""
    try:
        if GITHUB_CLIENT:
            info = GITHUB_CLIENT.get_pr_info(repo_full, pr_number)
            pr_summary = (
                f"**标题:** {info.get('title', 'N/A')}\n"
                f"**作者:** @{info.get('user', {}).get('login', 'N/A')}\n"
                f"**分支:** {info.get('head', {}).get('ref', '?')} → {info.get('base', {}).get('ref', '?')}\n"
                f"**变更:** +{info.get('additions', 0)} -{info.get('deletions', 0)}, "
                f"{info.get('changed_files', 0)} 文件\n"
            )
    except Exception as e:
        log.warning("获取 PR 信息失败: %s", e)

    # 自动检测：bot 之前 review 过 → re-review 模式
    is_rereview = has_bot_reviewed_before(repo_full, pr_number)

    if is_rereview:
        content = _build_rereview_prompt(repo_full, pr_number, pr_summary, sender, comment_body)
    else:
        content = _build_first_review_prompt(repo_full, pr_number, pr_summary, sender, comment_body)

    send_inbound(chat_id, content, f"@{sender}")



def _build_first_review_prompt(repo_full, pr_number, pr_summary, sender, comment_body):
    """构建首次 CR 的 prompt。"""
    return (
        f"## 🔍 Code Review 请求\n\n"
        f"**仓库:** `{repo_full}`\n"
        f"**PR:** #{pr_number}\n"
        f"{pr_summary}\n"
        f"**触发者:** @{sender}\n"
        f"**评论内容:** {comment_body}\n\n"
        f"---\n\n"
        f"请对这个 PR 进行 Code Review。\n\n"
        f"**步骤:**\n"
        f"1. 使用 `get_pr_files_detail` 获取每个文件的详细 diff（含行号信息）\n"
        f"2. 仔细审查代码：Bug、安全风险、性能问题、代码风格\n"
        f"3. 使用 `post_line_review` 发表行级评审（推荐），将评论精确到具体代码行\n\n"
        f"**工具说明:**\n"
        f"- `post_line_review`：**推荐使用**。可以在指定文件的指定行上添加评论，"
        f"让作者直接看到哪行有问题。comments 数组中每个元素的 line 字段对应 "
        f"diff 中 `+` 或 `-` 后的行号，path 对应文件路径\n"
        f"- `post_review_comment`：发表整体总结，不针对特定行\n\n"
        f"**评审标准（重要！严格遵守）:**\n"
        f"- 用中文撰写评审\n"
        f"- **只评论有实际问题的代码行**，不评论无问题的代码\n"
        f"- 禁止发表以下类型的评论：🟢 良好实践、命名清晰、代码风格良好、注释完整等正面评价\n"
        f"- 行级评论按严重程度标注：🔴 严重（Bug/安全） / 🟡 建议（需修改）\n"
        f"- 如果某行代码没有问题，不要在该行添加任何评论\n"
        f"- 整体总结只需一句话概括，重点是行级评论\n"
        f"- 给出具体的修改建议和代码示例\n\n"
        f"**最终结论（重要！必须明确选择）:**\n"
        f"- 有 🔴 或 🟡 问题 → 使用 `REQUEST_CHANGES`\n"
        f"- 没有任何问题 → 使用 `APPROVE`（不要使用 COMMENT 凑数）\n"
        f"- 不要总是用 COMMENT"
    )


def _build_rereview_prompt(repo_full, pr_number, pr_summary, sender, comment_body):
    """构建重新 CR 的 prompt。获取之前的 review 历史，引导 LLM 检查修复情况。"""
    # 获取之前的 review 历史
    prev_reviews_info = ""
    try:
        if GITHUB_CLIENT:
            prev = GITHUB_CLIENT.get_previous_reviews(repo_full, pr_number)
            bot_reviews = [r for r in prev["reviews"] if "[bot]" in r.get("user", "")]
            if bot_reviews:
                latest = bot_reviews[-1]
                prev_reviews_info = (
                    f"\n**之前的 Review 状态:** {latest.get('state', '未知')}\n"
                    f"**之前的 Review 内容:**\n{latest.get('body', '无')[:2000]}\n"
                )
                # 列出之前提的行级评论
                if latest.get("comments"):
                    prev_reviews_info += "**之前提的行级问题:**\n"
                    for c in latest["comments"]:
                        prev_reviews_info += f"  - `{c['path']}:{c.get('line', '?')}` - {c['body'][:200]}\n"
    except Exception as e:
        log.warning("获取之前 review 历史失败: %s", e)

    return (
        f"## 🔄 重新 Code Review 请求\n\n"
        f"**仓库:** `{repo_full}`\n"
        f"**PR:** #{pr_number}\n"
        f"{pr_summary}\n"
        f"**触发者:** @{sender}\n"
        f"**评论内容:** {comment_body}\n\n"
        f"---\n\n"
        f"用户已修改代码，要求重新审查。\n"
        f"{prev_reviews_info}\n"
        f"---\n\n"
        f"请重新审查这个 PR，**重点关注之前提出的问题是否已修复**。\n\n"
        f"**步骤:**\n"
        f"1. 使用 `get_previous_reviews` 获取完整的之前 review 历史\n"
        f"2. 使用 `get_pr_files_detail` 获取最新 diff（含行号信息）\n"
        f"3. 逐一检查之前提出的问题是否已修复\n"
        f"4. 检查是否有新引入的问题\n"
        f"5. 使用 `post_line_review` 发表评审\n\n"
        f"**评审要点（重要！严格遵守）:**\n"
        f"- 用中文撰写评审\n"
        f"- **只评论有实际问题的代码行**，不评论无问题的代码\n"
        f"- 禁止发表以下类型的评论：🟢 良好实践、命名清晰、已修复的正面确认等\n"
        f"- 对已修复的问题不需要评论（除非修复引入了新问题）\n"
        f"- 对未修复的问题重新提出，标注 🔴 未修复\n"
        f"- 对新发现的问题按严重程度标注：🔴 严重（Bug/安全） / 🟡 建议（需修改）\n"
        f"- 整体总结只需一句话\n\n"
        f"**最终结论（重要！根据修复情况选择）:**\n"
        f"- 所有问题已修复且无新问题 → 使用 `APPROVE`（不要用 COMMENT 凑数）\n"
        f"- 仍有未修复的问题 → 使用 `REQUEST_CHANGES`\n"
        f"- 大部分已修复，仅剩小的 🟡 建议 → 使用 `COMMENT`"
    )

    send_inbound(chat_id, content, f"@{sender}")


def should_process_comment(
    comment_body: str, sender: str, sender_type: str,
    repo_full: str, comment_id: int,
) -> tuple[bool, str]:
    """统一的评论过滤逻辑。Returns (should_process, reason)。"""
    # 忽略 bot 自己的评论（Bot 的用户名是 slug[bot] 格式）
    if sender_type == "Bot" or sender.lower().startswith(f"{BOT_USERNAME.lower()}[") or sender.lower() == BOT_USERNAME.lower():
        return False, "bot 自身评论"
    # 跳过已处理的
    if comment_id in PROCESSED_COMMENTS:
        return False, "已处理过"
    # 检测 @mention
    if not is_mentioned(comment_body):
        return False, "未 @mention"
    # 白名单/黑名单
    allowed, reason = is_allowed(repo_full, sender)
    if not allowed:
        return False, reason
    return True, "ok"


# ---- 轮询模式 ----


def get_monitored_repos() -> list[str]:
    """获取要监控的仓库列表。优先用配置，否则自动发现 + 白名单补充。"""
    configured = parse_list(CONFIG.get("monitored_repos", ""))
    if configured:
        return list(dict.fromkeys(configured))  # 去重保序

    repos = set()

    # 自动发现 App 安装的所有仓库
    if GITHUB_CLIENT:
        try:
            discovered = GITHUB_CLIENT.list_installation_repos()
            repos.update(discovered)
            log.info("自动发现 %d 个仓库: %s", len(discovered), discovered)
        except Exception as e:
            log.error("自动发现仓库失败: %s", e)

    # 白名单中的精确仓库也加入监控（即使 App 未安装在该仓库也能尝试轮询）
    wl_repos = parse_list(CONFIG.get("whitelist_repos", ""))
    for r in wl_repos:
        if not r.endswith("/*"):
            repos.add(r)

    return list(repos)


def poll_loop(interval: int):
    """轮询循环：每 interval 秒检查所有监控仓库的 open PR 上的新 @mention 评论。"""
    global _startup_time
    _startup_time = time.time()
    log.info("轮询模式启动，间隔 %ds，启动时间戳 %.0f", interval, _startup_time)

    while True:
        try:
            _scan_comments()
            time.sleep(interval)
        except Exception:
            log.exception("轮询异常，继续重试")


# 轮询线程池（并发扫描多个仓库）
_poll_executor: ThreadPoolExecutor | None = None
# 缓存每个仓库的 open PR 编号（ETag 304 时使用）
_repo_prs_cache: dict[str, list[int]] = {}
# 启动时间戳：启动前的评论静默标记，启动后的正常触发（防重启洪水）
_startup_time: float = 0.0


def _scan_one_repo(repo: str) -> list[tuple]:
    """扫描单个仓库的所有 open PR 评论。返回需触发的 CR 请求列表。

    Returns: [(repo, pr_number, comment_body, sender, comment_id), ...]
    """
    triggers = []
    if not GITHUB_CLIENT:
        return triggers

    try:
        prs = GITHUB_CLIENT.list_open_prs(repo)
        if prs is None:
            # ETag 304：PR 列表未变，用缓存的 PR 编号继续查评论
            cached_pr_nums = _repo_prs_cache.get(repo, [])
            if not cached_pr_nums:
                return triggers
            pr_numbers = cached_pr_nums
        else:
            if not prs:
                _repo_prs_cache[repo] = []
                return triggers
            # 更新缓存
            pr_numbers = [pr["number"] if isinstance(pr, dict) else pr for pr in prs]
            _repo_prs_cache[repo] = pr_numbers

        # 并发获取每个 PR 的评论
        def fetch_comments(pr_number):
            try:
                return GITHUB_CLIENT.get_issue_comments(repo, pr_number)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=min(8, len(pr_numbers))) as ex:
            futures = {ex.submit(fetch_comments, pn): pn for pn in pr_numbers}
            for fut in as_completed(futures):
                pr_number = futures[fut]
                comments = fut.result()
                if comments is None:
                    continue  # 304

                for c in comments:
                    comment_id = c["id"]

                    should, reason = should_process_comment(
                        c["body"], c["user"], c["user_type"], repo, comment_id
                    )
                    if not should:
                        continue

                    # 防重启洪水：启动前的评论静默标记，不触发
                    if _startup_time > 0 and c.get("created_at"):
                        try:
                            from datetime import datetime, timezone
                            created_dt = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                            if created_dt.timestamp() < _startup_time:
                                # 启动前的老评论，静默标记为已处理
                                _try_mark_processed(comment_id)
                                continue
                        except (ValueError, TypeError):
                            pass  # 时间解析失败，按正常流程处理

                    triggers.append((repo, pr_number, c["body"], c["user"], comment_id))
    except Exception as e:
        log.warning("扫描仓库失败: %s: %s", repo, e)

    return triggers


def _scan_comments():
    """并发扫描所有监控仓库的 open PR 评论。"""
    global _poll_executor

    repos = get_monitored_repos()
    if not repos:
        return

    # 并发扫描所有仓库（ETag 缓存使大部分仓库秒回 304）
    max_workers = min(20, len(repos))
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(_scan_one_repo, repo): repo for repo in repos}
        for fut in as_completed(future_map):
            repo = future_map[fut]
            try:
                triggers = fut.result()
                results.extend(triggers)
            except Exception as e:
                log.warning("仓库扫描异常: %s: %s", repo, e)

    # 处理所有触发的 CR 请求
    for repo, pr_number, comment_body, sender, comment_id in results:
        trigger_cr(repo, pr_number, comment_body, sender, comment_id)


# ---- Webhook 处理 ----


class WebhookHandler(BaseHTTPRequestHandler):
    """GitHub Webhook HTTP 请求处理器。"""

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)

            # 验证签名
            signature = self.headers.get("X-Hub-Signature-256", "")
            secret = CONFIG.get("webhook_secret", "")
            if not verify_signature(raw_body, signature, secret):
                log.warning("Webhook 签名验证失败")
                self._respond(401, "Invalid signature")
                return

            event_type = self.headers.get("X-GitHub-Event", "")
            body_text = raw_body.decode("utf-8") if raw_body else "{}"

            if event_type == "issue_comment":
                self._handle_issue_comment(body_text)
            elif event_type == "pull_request":
                self._handle_pull_request(body_text)
            else:
                log.debug("忽略事件类型: %s", event_type)

            self._respond(200, "ok")

        except Exception as e:
            log.exception("Webhook 处理异常")
            self._respond(500, str(e))

    def _respond(self, code: int, message: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def _handle_issue_comment(self, body_text: str):
        """处理 issue_comment 事件。"""
        event = json.loads(body_text)
        action = event.get("action")
        if action != "created":
            return

        comment = event.get("comment", {})
        issue = event.get("issue", {})
        repo_data = event.get("repository", {})

        # 必须是 PR 评论
        if "pull_request" not in issue:
            log.debug("评论不是在 PR 上，跳过")
            return

        comment_body = comment.get("body", "")
        sender = comment.get("user", {}).get("login", "unknown")
        sender_type = comment.get("user", {}).get("type", "")
        repo_full = repo_data.get("full_name", "")
        pr_number = issue.get("number", 0)
        comment_id = comment.get("id", 0)

        should, reason = should_process_comment(
            comment_body, sender, sender_type, repo_full, comment_id
        )
        if not should:
            if reason not in ("未 @mention", "bot 自身评论"):
                log.info("评论被过滤: %s (repo=%s)", reason, repo_full)
            return

        trigger_cr(repo_full, pr_number, comment_body, sender, comment_id)

    def _handle_pull_request(self, body_text: str):
        """处理 pull_request 事件（可选触发）。"""
        if CONFIG.get("trigger_on_open", "false") != "true":
            return

        event = json.loads(body_text)
        action = event.get("action")
        if action not in ("opened", "reopened"):
            return

        pr_data = event.get("pull_request", {})
        repo_data = event.get("repository", {})
        sender = event.get("sender", {}).get("login", "unknown")
        repo_full = repo_data.get("full_name", "")
        pr_number = pr_data.get("number", 0)

        # 白名单/黑名单过滤
        allowed, reason = is_allowed(repo_full, sender)
        if not allowed:
            log.info("PR 事件被过滤: %s", reason)
            return

        log.info("PR 打开事件触发 CR: repo=%s pr=#%d", repo_full, pr_number)

        chat_id = f"{repo_full}#pr-{pr_number}"
        content = (
            f"## 🔍 新 PR 自动 Code Review\n\n"
            f"**仓库:** `{repo_full}`\n"
            f"**PR:** #{pr_number} - {pr_data.get('title', '')}\n"
            f"**作者:** @{sender}\n\n"
            f"请执行 Code Review。使用 `get_pr_diff` 获取 diff，"
            f"审查后用 `post_review_comment` 发表评审。"
        )
        send_inbound(chat_id, content, f"@{sender}")

    def log_message(self, *args):
        pass  # 抑制默认日志


# ---- 主消息循环 ----


def main():
    global CONFIG, GITHUB_CLIENT, BOT_USERNAME

    log.info("插件进程启动，等待 xbot 消息...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.error("无法解析消息: %s", line[:200])
            continue

        method = msg.get("method", "")
        msg_type = msg.get("type", "")
        msg_id = msg.get("id", "")

        # ---- 阶段 1: 激活 — 声明 channel provider ----
        if method == "activate":
            log.info("收到 activate 请求")
            write_stdout({
                "result": "ok",
                "channel_provider": {
                    "name": "github",
                    "config_schema": [
                        {"key": "enabled", "label": "启用", "type": "toggle", "default_value": "false"},
                        {"key": "mode", "label": "运行模式", "type": "select", "options": ["poll", "webhook"], "default_value": "poll"},
                        {"key": "app_id", "label": "GitHub App ID", "type": "text", "default_value": ""},
                        {"key": "private_key", "label": "Private Key (PEM)", "type": "text", "default_value": ""},
                        {"key": "installation_id", "label": "Installation ID", "type": "number", "default_value": "0"},
                        {"key": "bot_username", "label": "Bot 用户名", "type": "text", "default_value": "code-reviewer-bot"},
                        {"key": "poll_interval", "label": "轮询间隔（秒）", "type": "number", "default_value": "10"},
                        {"key": "monitored_repos", "label": "监控仓库（逗号分隔，留空=自动发现全部）", "type": "text", "default_value": ""},
                        {"key": "webhook_secret", "label": "Webhook Secret（仅 webhook 模式）", "type": "password", "default_value": ""},
                        {"key": "webhook_port", "label": "Webhook 端口（仅 webhook 模式）", "type": "number", "default_value": "9876"},
                        {"key": "whitelist_repos", "label": "仓库白名单（逗号分隔）", "type": "text", "default_value": ""},
                        {"key": "blacklist_repos", "label": "仓库黑名单（逗号分隔）", "type": "text", "default_value": ""},
                        {"key": "whitelist_users", "label": "用户白名单（逗号分隔）", "type": "text", "default_value": ""},
                        {"key": "blacklist_users", "label": "用户黑名单（逗号分隔）", "type": "text", "default_value": ""},
                        {"key": "trigger_on_open", "label": "PR 打开时自动触发", "type": "toggle", "default_value": "false"},
                    ],
                },
            })
            continue

        # ---- 阶段 2: channel_config — 初始化 + 启动 webhook ----
        if msg_type == "channel_config":
            log.info("收到 channel_config")
            raw_config = msg.get("metadata", {}).get("config", "{}")
            CONFIG = json.loads(raw_config) if isinstance(raw_config, str) else (raw_config or {})

            # 如果未启用，跳过初始化
            if CONFIG.get("enabled", "false") != "true":
                log.info("插件未启用（enabled != true），跳过初始化")
                continue

            # 初始化 GitHub 客户端
            app_id = CONFIG.get("app_id", "")
            private_key = CONFIG.get("private_key", "")

            # 支持从文件读取私钥
            if not private_key:
                key_file = os.environ.get("GITHUB_PRIVATE_KEY_FILE", "")
                if key_file and os.path.isfile(key_file):
                    with open(key_file) as f:
                        private_key = f.read()
                    log.info("从文件读取私钥: %s", key_file)

            if not app_id or not private_key:
                log.error("缺少 app_id 或 private_key，无法初始化 GitHub 客户端")
                continue

            installation_id = int(CONFIG.get("installation_id", 0) or 0)
            auth = GitHubAppAuth(app_id, private_key, installation_id)
            GITHUB_CLIENT = GitHubClient(auth)

            BOT_USERNAME = CONFIG.get("bot_username", "code-reviewer-bot")
            log.info("Bot 用户名: %s", BOT_USERNAME)

            # 尝试自动获取 Bot 真实用户名（GitHub App slug）
            try:
                app_info = GITHUB_CLIENT.get_app_info()
                slug = app_info.get("slug", "")
                if slug:
                    BOT_USERNAME = slug
                    log.info("从 API 获取 Bot 用户名: %s", BOT_USERNAME)
            except Exception as e:
                log.debug("获取 App 信息失败（使用配置的用户名）: %s", e)

            # 声明 channel tools
            declare_tools()

            # 加载已处理评论持久化数据（防重启重复触发）
            _load_processed()

            # 根据模式启动
            mode = CONFIG.get("mode", "poll")
            if mode == "webhook":
                port = int(CONFIG.get("webhook_port", "9876"))
                server = HTTPServer(("0.0.0.0", port), WebhookHandler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                log.info("Webhook 模式启动，监听端口 %d", port)
            else:
                interval = int(CONFIG.get("poll_interval", "10"))
                threading.Thread(target=poll_loop, args=(interval,), daemon=True).start()
                log.info("Poll 模式启动，间隔 %ds", interval)
            continue

        # ---- 阶段 3: execute_tool RPC — LLM 调用工具 ----
        if method == "execute_tool":
            tool_name = msg.get("params", {}).get("name", "")
            tool_input = msg.get("params", {}).get("input", "{}")
            log.info("执行工具调用: %s", tool_name)
            content, is_error = execute_tool(tool_name, tool_input)
            write_stdout({
                "id": msg_id,
                "result": {"content": content, "is_error": is_error},
            })
            continue

        # ---- 其他 xbot → plugin 事件 ----

        # Agent 回复消息（最终文本）
        if msg_type == "text":
            log.debug("Agent 回复: %s", msg.get("content", "")[:100])
            continue

        # 流式内容
        if msg_type == "stream_content":
            continue

        # 会话状态变化
        if msg_type == "session":
            action = msg.get("session", {}).get("action", "")
            chat_id = msg.get("chat_id", "")
            log.debug("会话状态: %s (chat=%s)", action, chat_id)
            continue

        # 进度事件
        if msg_type == "progress_structured":
            phase = msg.get("progress", {}).get("phase", "")
            log.debug("进度: %s", phase)
            continue

        # RPC 请求（如 channel_send，用于主动推送消息到 channel）
        if method == "channel_send":
            # xbot 要发送消息到 GitHub，我们把它作为 PR 评论发出
            chat_id = msg.get("params", {}).get("chat_id", "")
            content_text = msg.get("params", {}).get("content", "")
            log.info("channel_send → chat_id=%s", chat_id)
            # 解析 chat_id 格式: owner/repo#pr-N
            if GITHUB_CLIENT and "#pr-" in chat_id:
                repo_part, _, pr_part = chat_id.partition("#pr-")
                try:
                    pr_num = int(pr_part)
                    GITHUB_CLIENT.post_comment(repo_part, pr_num, content_text)
                    write_stdout({"id": msg_id, "result": "ok"})
                except Exception as e:
                    log.error("发送评论失败: %s", e)
                    write_stdout({"id": msg_id, "error": str(e)})
            else:
                write_stdout({"id": msg_id, "result": "ok"})
            continue

        log.debug("未处理的消息: method=%s type=%s", method, msg_type)


if __name__ == "__main__":
    main()
