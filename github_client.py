#!/usr/bin/env python3
"""GitHub App 认证与 API 封装。

流程:
  1. 用 App ID + Private Key 生成 JWT（RS256，10 分钟有效期）
  2. 用 JWT 获取 Installation Access Token（1 小时有效，自动缓存刷新）
  3. 用 Installation Token 调用 GitHub API
"""

import time
import jwt
import requests

GITHUB_API = "https://api.github.com"


class GitHubAppAuth:
    """GitHub App 认证器：JWT 生成 + 多 Installation Token 管理。

    支持自动发现所有 installation，按仓库选择正确的 token。
    """

    def __init__(self, app_id: str, private_key_pem: str, installation_id: int = 0):
        self.app_id = str(app_id)
        self.private_key_pem = private_key_pem
        self.installation_id = installation_id  # 可指定单个，留 0 则自动发现全部
        # 多 installation token 缓存: {inst_id: (token, expires_at)}
        self._token_cache: dict[int, tuple[str, float]] = {}
        # 仓库 → installation_id 映射缓存
        self._repo_inst_map: dict[str, int] = {}
        self._all_installations: list[int] | None = None

    # ---- JWT ----

    def _generate_jwt(self) -> str:
        """生成 GitHub App JWT（RS256，10 分钟有效）。"""
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 10 * 60,
            "iss": self.app_id,
        }
        token = jwt.encode(payload, self.private_key_pem, algorithm="RS256")
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token

    # ---- Installation 发现 ----

    def list_all_installations(self) -> list[int]:
        """列出 App 的所有 installation ID。"""
        if self._all_installations is not None:
            return self._all_installations

        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.get(f"{GITHUB_API}/app/installations", headers=headers, timeout=10)
        ids = []
        if resp.status_code == 200:
            for inst in resp.json():
                ids.append(inst["id"])
        self._all_installations = ids
        return ids

    def find_installation_for_repo(self, repo_full_name: str) -> int:
        """查找仓库所属的 installation ID。"""
        # 先查缓存
        if repo_full_name in self._repo_inst_map:
            return self._repo_inst_map[repo_full_name]

        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        owner, _, repo = repo_full_name.partition("/")
        url = f"{GITHUB_API}/repos/{owner}/{repo}/installation"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            inst_id = resp.json()["id"]
            self._repo_inst_map[repo_full_name] = inst_id
            return inst_id

        raise RuntimeError(f"无法找到 {repo_full_name} 的 installation")

    # ---- Installation Token ----

    def get_installation_token(self, repo_full_name: str | None = None) -> str:
        """获取 Installation Access Token（带缓存，提前 5 分钟刷新）。

        如果指定了 repo_full_name，会自动选择正确的 installation。
        否则用默认的 installation_id（或第一个）。
        """
        # 确定使用哪个 installation
        if repo_full_name:
            try:
                inst_id = self.find_installation_for_repo(repo_full_name)
            except RuntimeError:
                inst_id = self.installation_id or (self.list_all_installations()[0] if self.list_all_installations() else 0)
        else:
            inst_id = self.installation_id or (self.list_all_installations()[0] if self.list_all_installations() else 0)

        if not inst_id:
            raise RuntimeError("无法确定 installation ID")

        # 检查缓存
        cached = self._token_cache.get(inst_id)
        if cached and time.time() < cached[1] - 300:
            return cached[0]

        # 获取新 token
        jwt_token = self._generate_jwt()
        url = f"{GITHUB_API}/app/installations/{inst_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.post(url, headers=headers, timeout=10)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"获取 installation token 失败: {resp.status_code} {resp.text}")

        data = resp.json()
        token = data["token"]
        expires_str = data.get("expires_at", "")
        if expires_str:
            from datetime import datetime
            dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            expires_at = dt.timestamp()
        else:
            expires_at = time.time() + 3300

        self._token_cache[inst_id] = (token, expires_at)
        return token


class GitHubClient:
    """GitHub API 客户端，使用 Installation Token 认证。"""

    def __init__(self, auth: GitHubAppAuth):
        self.auth = auth
        self._etags: dict[str, str] = {}  # url → ETag（用于条件请求，减少 rate limit 消耗）

    def _headers(self, repo_full_name: str | None = None, accept: str = "application/vnd.github+json") -> dict:
        token = self.auth.get_installation_token(repo_full_name)
        return {
            "Authorization": f"token {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ---- PR 信息 ----

    def get_pr_info(self, repo: str, pr_number: int) -> dict:
        """获取 PR 基本信息。"""
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        resp = requests.get(url, headers=self._headers(repo), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_pr_diff(self, repo: str, pr_number: int, max_chars: int = 80000) -> str:
        """获取 PR diff（截断到 max_chars 防止超长）。"""
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        headers = self._headers(repo, accept="application/vnd.github.v3.diff")
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        diff = resp.text
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n... [diff 已截断，原始长度 {len(diff)} 字符]"
        return diff

    def get_pr_files(self, repo: str, pr_number: int) -> list[dict]:
        """获取 PR 变更文件列表。"""
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
        resp = requests.get(url, headers=self._headers(repo), timeout=15)
        resp.raise_for_status()
        files = resp.json()
        # 只保留关键字段，减少 token 消耗
        result = []
        for f in files:
            result.append({
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "changes": f.get("changes"),
            })
        return result

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict]:
        """获取已有的 reviews（避免重复 review）。"""
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        resp = requests.get(url, headers=self._headers(repo), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_previous_reviews(self, repo: str, pr_number: int) -> dict:
        """获取 PR 上 bot 之前的 review 历史。

        Returns:
            {
                "total": int,
                "bot_reviews": [{state, body, submitted_at, comments}],
                "latest_bot_state": "COMMENT" | "REQUEST_CHANGES" | "APPROVE" | None,
                "latest_bot_body": str,
            }
        """
        reviews = self.get_pr_reviews(repo, pr_number)
        bot_reviews = []
        for r in reviews:
            user = r.get("user", {}).get("login", "")
            if "[bot]" not in user and "github-actions" not in user:
                # 过滤非 bot 的 review，但也保留其他人的 review 信息
                pass
            state = r.get("state", "")
            body = r.get("body", "")
            submitted_at = r.get("submitted_at", "")
            comments = []
            # 获取每个 review 的行级评论
            review_id = r.get("id")
            if review_id:
                try:
                    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
                    resp = requests.get(url, headers=self._headers(repo), timeout=10)
                    if resp.status_code == 200:
                        for c in resp.json():
                            comments.append({
                                "path": c.get("path"),
                                "line": c.get("line"),
                                "body": c.get("body", ""),
                            })
                except Exception:
                    pass
            bot_reviews.append({
                "user": user,
                "state": state,
                "body": body[:2000],
                "submitted_at": submitted_at,
                "comments": comments,
            })

        latest_bot = None
        for r in reversed(bot_reviews):
            if "[bot]" in r.get("user", ""):
                latest_bot = r
                break

        return {
            "total": len(bot_reviews),
            "reviews": bot_reviews,
            "latest_bot_review": latest_bot,
        }

    # ---- 发表评论 ----

    def post_review(self, repo: str, pr_number: int, body: str, event: str = "COMMENT") -> dict:
        """在 PR 上发表 review 评论。

        Args:
            event: APPROVE | REQUEST_CHANGES | COMMENT
        """
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        payload = {"body": body, "event": event}
        resp = requests.post(url, json=payload, headers=self._headers(repo), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def post_comment(self, repo: str, pr_number: int, body: str) -> dict:
        """在 PR 上发表普通 issue 评论。"""
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        payload = {"body": body}
        resp = requests.post(url, json=payload, headers=self._headers(repo), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def add_reaction(self, repo: str, comment_id: int, content: str = "eyes"):
        """给评论添加 emoji 反应，表示已收到。"""
        url = f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}/reactions"
        payload = {"content": content}
        try:
            requests.post(url, json=payload, headers=self._headers(repo), timeout=10)
        except Exception:
            pass  # 反应失败不影响主流程

    def get_pr_files_detail(self, repo: str, pr_number: int, max_files: int = 30) -> list[dict]:
        """获取 PR 变更文件详情（含 patch、行号范围），用于行级评论。

        返回的每个文件包含:
          - filename: 文件路径
          - status: added/modified/removed/renamed
          - additions/deletions: 增删行数
          - patch: diff 内容（可用于判断行号）
          - blob_sha: 文件 blob SHA（行级评论必需）
          - start_line: diff 起始行（新版行级评论 API 需要）
        """
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
        headers = self._headers(repo)
        params = {"per_page": 100}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        result = []
        for f in resp.json()[:max_files]:
            patch = f.get("patch", "")
            result.append({
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "sha": f.get("sha"),
                "blob_url": f.get("blob_url"),
                "raw_url": f.get("raw_url"),
                "patch": patch[:5000],  # 截断过长的 patch
                "patch_truncated": len(patch) > 5000,
            })
        return result

    def post_review_with_comments(
        self,
        repo: str,
        pr_number: int,
        body: str,
        comments: list[dict] | None = None,
        event: str = "COMMENT",
    ) -> dict:
        """发表带行级评论的 review。

        Args:
            body: Review 整体摘要
            comments: 行级评论列表，每个元素:
                {
                    "path": "src/main.py",        # 文件路径
                    "body": "建议修改",            # 评论内容
                    "line": 42,                   # 目标行号（文件中的行号）
                    "side": "RIGHT",              # RIGHT（修改后）或 LEFT（修改前）
                    "start_line": 40,             # 多行评论的起始行（可选）
                }
            event: COMMENT | APPROVE | REQUEST_CHANGES
        """
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        payload = {
            "body": body,
            "event": event,
        }
        if comments:
            payload["comments"] = []
            for c in comments:
                comment = {
                    "path": c["path"],
                    "body": c["body"],
                    "line": c.get("line", 1),
                    "side": c.get("side", "RIGHT"),
                }
                if c.get("start_line"):
                    comment["start_line"] = c["start_line"]
                    comment["start_side"] = c.get("side", "RIGHT")
                payload["comments"].append(comment)

        resp = requests.post(url, json=payload, headers=self._headers(repo), timeout=20)
        resp.raise_for_status()
        return resp.json()

    # ---- 轮询相关 API ----

    def get_app_info(self) -> dict:
        """获取当前认证的 GitHub App 信息（name, slug 等）。"""
        url = f"{GITHUB_API}/app"
        # App 信息需要用 JWT 而非 installation token
        jwt_token = self.auth._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def list_installation_repos(self) -> list[str]:
        """列出所有 installation 下的所有仓库（owner/repo 格式）。"""
        repos = []
        for inst_id in self.auth.list_all_installations():
            try:
                token = self.auth.get_installation_token()  # 会按 inst_id 缓存
                # 直接用指定 inst_id 获取 token
                jwt_token = self.auth._generate_jwt()
                url = f"{GITHUB_API}/app/installations/{inst_id}/access_tokens"
                headers_jwt = {
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                }
                resp = requests.post(url, headers=headers_jwt, timeout=10)
                if resp.status_code not in (200, 201):
                    continue
                token = resp.json()["token"]
                self.auth._token_cache[inst_id] = (token, time.time() + 3300)

                headers = {
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                }
                page = 1
                while True:
                    params = {"per_page": 100, "page": page}
                    resp = requests.get(f"{GITHUB_API}/installation/repositories",
                                        headers=headers, params=params, timeout=15)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    for repo in data.get("repositories", []):
                        repos.append(repo["full_name"])
                        # 缓存 repo → installation 映射
                        self.auth._repo_inst_map[repo["full_name"]] = inst_id
                    if len(data.get("repositories", [])) < 100:
                        break
                    page += 1
            except Exception:
                continue
        return repos

    def list_open_prs(self, repo: str) -> list[dict] | None:
        """列出仓库的 open PR（返回精简列表）。
        
        使用 ETag 条件请求：内容未变时返回 None（不消耗 rate limit）。
        """
        url = f"{GITHUB_API}/repos/{repo}/pulls"
        headers = self._headers(repo)
        # ETag 条件请求
        etag = self._etags.get(f"prs:{repo}")
        if etag:
            headers["If-None-Match"] = etag
        params = {"state": "open", "per_page": 50, "sort": "updated", "direction": "desc"}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        
        if resp.status_code == 304:
            return None  # 内容未变，不消耗 rate limit
        
        # 保存新的 ETag
        new_etag = resp.headers.get("ETag")
        if new_etag:
            self._etags[f"prs:{repo}"] = new_etag
        
        if resp.status_code != 200:
            return []
        
        result = []
        for pr in resp.json():
            result.append({
                "number": pr.get("number"),
                "title": pr.get("title"),
                "user": pr.get("user", {}).get("login"),
                "updated_at": pr.get("updated_at"),
            })
        return result

    def get_issue_comments(self, repo: str, pr_number: int) -> list[dict] | None:
        """获取 PR 的 issue comments（按时间正序）。
        
        使用 ETag 条件请求：内容未变时返回 None（不消耗 rate limit）。
        """
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        headers = self._headers(repo)
        etag_key = f"comments:{repo}:{pr_number}"
        etag = self._etags.get(etag_key)
        if etag:
            headers["If-None-Match"] = etag
        params = {"per_page": 100, "sort": "created", "direction": "asc"}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        
        if resp.status_code == 304:
            return None  # 内容未变
        
        new_etag = resp.headers.get("ETag")
        if new_etag:
            self._etags[etag_key] = new_etag
        
        if resp.status_code != 200:
            return []
        
        result = []
        for c in resp.json():
            result.append({
                "id": c.get("id"),
                "body": c.get("body", ""),
                "user": c.get("user", {}).get("login", ""),
                "user_type": c.get("user", {}).get("type", ""),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
            })
        return result
