#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notify when a non-self-handled open Issue waits too long for a response.

Rules:
* No non-author response within 12 hours of Issue creation.
* After a non-author response, the author's latest comment waits 3 hours.

The scanner reads live GitCode data. Notification and comment observations are
persisted so unchanged Issues do not repeatedly fetch all comments or resend mail.
"""

import argparse
import configparser
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

from collector import V5_BASE, _load_token, _v5_headers, get_required, save_json
from stale_issue_notify import SMTP_CONFIG_PATH, load_mail_map, send_one_email


BASE_DIR = Path(__file__).resolve().parent
REPOS_CONFIG_PATH = BASE_DIR / "config" / "repos.yml"
RULES_CONFIG_PATH = BASE_DIR / "config" / "issue_response_notify.yml"
STATE_PATH = BASE_DIR / "data" / "issue_response_notified.json"

DEFAULT_INITIAL_HOURS = 12
DEFAULT_FOLLOWUP_HOURS = 3
STATE_VERSION = 2


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_time(value):
    if not value:
        return None
    value = str(value).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _hours_since(value, now):
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600


def _login(user):
    if isinstance(user, dict):
        return str(user.get("login") or "")
    return str(user or "")


def _is_bot_comment(comment, bot_users):
    user = comment.get("user") or {}
    login = _login(user)
    return user.get("type") == "Bot" or login in bot_users or login.endswith("[bot]")


def _comment_summary(comments, author, bot_users=None):
    """Return only the state needed by the two reminder rules."""
    bot_users = set(bot_users or [])
    valid = []
    for comment in comments:
        login = _login(comment.get("user"))
        created_at = comment.get("created_at") or ""
        if not login or not _parse_time(created_at) or _is_bot_comment(comment, bot_users):
            continue
        valid.append((
            _parse_time(created_at), str(comment.get("id") or created_at),
            login, created_at,
        ))
    valid.sort(key=lambda item: (item[0], item[1]))
    non_author = [item for item in valid if item[2] != author]
    latest = valid[-1] if valid else None
    latest_non_author = non_author[-1] if non_author else None
    return {
        "has_non_author_response": bool(non_author),
        "latest_comment_id": latest[1] if latest else "",
        "latest_comment_author": latest[2] if latest else "",
        "latest_comment_at": latest[3] if latest else "",
        "latest_non_author_comment_id": latest_non_author[1] if latest_non_author else "",
    }


def _is_self_handled(issue, linked_pr_authors):
    author = issue.get("author") or ""
    return bool(author and (
        author in (issue.get("assignees") or []) or author in linked_pr_authors
    ))


def _classify_waiting_event(issue, comments, now, initial_hours, followup_hours):
    """Return (kind, stable event token, wait hours), or None."""
    created_age = _hours_since(issue.get("created_at"), now)
    if created_age is None:
        return None
    if not comments.get("has_non_author_response"):
        if created_age >= initial_hours:
            return "initial", str(issue.get("created_at") or issue.get("iid")), created_age
        return None
    if comments.get("latest_comment_author") != issue.get("author"):
        return None
    comment_age = _hours_since(comments.get("latest_comment_at"), now)
    if comment_age is None or comment_age < followup_hours:
        return None
    # All consecutive author comments after the same responder belong to one wait round.
    token = comments.get("latest_non_author_comment_id")
    if not token:
        return None
    return "followup", str(token), comment_age


def load_rules_config():
    if not RULES_CONFIG_PATH.exists():
        return {}
    with open(RULES_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_notify_repos():
    rules = load_rules_config()
    configured = _dedupe(rules.get("repos") or [])
    if configured:
        return configured
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return [r["path"] for r in config.get("repos", [])
            if r.get("enabled", True) and r.get("notify", False)]


def load_state():
    if not STATE_PATH.exists():
        return {"version": STATE_VERSION, "issues": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "通知状态文件无法读取，为避免重复发信，本次任务已停止"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError("通知状态文件格式错误，本次任务已停止")
    if not isinstance(data.get("issues"), dict):
        raise RuntimeError("通知状态文件缺少 issues 字典，本次任务已停止")
    data["version"] = STATE_VERSION
    return data


def _issue_key(repo, iid):
    return f"{repo.replace('/', '__')}!{iid}"


def _paged_get(url, headers):
    results = []
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        data = get_required(
            f"{url}{separator}page={page}&per_page=100", headers=headers,
        )
        if not isinstance(data, list):
            raise RuntimeError(f"API returned a non-list payload: {url}")
        results.extend(data)
        if len(data) < 100:
            return results
        page += 1


def _normalize_issue(repo, item):
    assignees = [_login(v) for v in (item.get("assignees") or [])]
    state = str(item.get("state") or "").lower()
    return {
        "repo": repo,
        "iid": item.get("number"),
        "title": item.get("title") or "",
        "state": "closed" if state == "closed" else "opened",
        "author": _login(item.get("user")),
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "assignees": [v for v in assignees if v],
        "comment_count": int(item.get("comments") or 0),
        "web_url": item.get("html_url") or
                   f"https://gitcode.com/{repo}/issues/{item.get('number')}",
    }


def fetch_open_issues(repo, headers):
    owner, name = repo.split("/", 1)
    raw = _paged_get(f"{V5_BASE}/repos/{owner}/{name}/issues?state=open", headers)
    return [_normalize_issue(repo, item) for item in raw
            if str(item.get("state") or "").lower() != "closed"]


def fetch_issue(repo, iid, headers):
    owner, name = repo.split("/", 1)
    item = get_required(
        f"{V5_BASE}/repos/{owner}/{name}/issues/{quote(str(iid))}",
        headers=headers,
    )
    if not isinstance(item, dict):
        raise RuntimeError(f"Issue API returned a non-object payload: {repo}#{iid}")
    return _normalize_issue(repo, item)


def fetch_issue_comments(repo, iid, headers):
    owner, name = repo.split("/", 1)
    return _paged_get(
        f"{V5_BASE}/repos/{owner}/{name}/issues/{quote(str(iid))}/comments?order=asc",
        headers,
    )


def fetch_linked_pr_authors(repo, iid, headers):
    owner, name = repo.split("/", 1)
    prs = _paged_get(
        f"{V5_BASE}/repos/{owner}/{name}/issues/{quote(str(iid))}/pull_requests",
        headers,
    )
    return {_login(pr.get("user")) for pr in prs if _login(pr.get("user"))}


def _dedupe(values):
    result = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _required_recipient_users(issue, kind, escalation_users):
    assignees = _dedupe(issue.get("assignees") or [])
    if kind == "initial":
        return _dedupe(assignees + list(escalation_users))
    return assignees if assignees else _dedupe(escalation_users)


def _resolve_recipient_users(usernames, mail_map):
    """Return email -> GitCode users and users without an email mapping."""
    by_email = {}
    missing = []
    for username in _dedupe(usernames):
        email = str(mail_map.get(username) or "").strip()
        if not email:
            missing.append(username)
            continue
        by_email.setdefault(email, []).append(username)
    return by_email, missing


def build_html_email(issue, kind, waited_hours):
    if kind == "initial":
        heading = "Issue 首次响应超时"
        explanation = "该 Issue 创建后已超过响应时限，尚无非创建者在评论区响应。"
    else:
        heading = "Issue 创建者追问响应超时"
        explanation = "该 Issue 曾得到回复，但创建者的最新评论已超过响应时限，之后尚无人继续响应。"
    url = str(issue.get("web_url") or "")
    safe_url = html.escape(url, quote=True) if url.startswith(("http://", "https://")) else "#"
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
<h2 style="font-size:18px;color:#1a1d2e">{heading}</h2>
<p style="color:#555">{explanation} 当前等待约 <strong>{waited_hours:.1f} 小时</strong>，请及时处理。</p>
<table style="border-collapse:collapse;width:100%;font-size:13px" border="1" cellpadding="8">
<tr><th>仓库</th><td>{html.escape(str(issue.get('repo') or ''))}</td></tr>
<tr><th>Issue</th><td><a href="{safe_url}">#{html.escape(str(issue.get('iid') or ''))}</a></td></tr>
<tr><th>标题</th><td>{html.escape(str(issue.get('title') or ''))}</td></tr>
<tr><th>创建者</th><td>{html.escape(str(issue.get('author') or ''))}</td></tr>
<tr><th>责任人</th><td>{html.escape(', '.join(issue.get('assignees') or []) or '未分配')}</td></tr>
</table>
<p style="color:#999;font-size:11px">此邮件由 CANN Radar 自动发送。</p>
</div>"""


def _smtp_config_or_none():
    if not SMTP_CONFIG_PATH.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONFIG_PATH, encoding="utf-8")
    return cfg


def scan_events(repos, state, headers, now, initial_hours, followup_hours, bot_users,
                target_iid=None):
    events = []
    successful_repos = set()
    open_keys_by_repo = {}
    for repo in repos:
        if target_iid is None:
            issues = fetch_open_issues(repo, headers)
        else:
            target = fetch_issue(repo, target_iid, headers)
            issues = [] if target.get("state") == "closed" else [target]
        open_keys = set()
        for issue in issues:
            if issue.get("iid") is None or not issue.get("author"):
                continue
            key = _issue_key(repo, issue["iid"])
            open_keys.add(key)
            record = state["issues"].setdefault(key, {})
            record["repo"] = repo
            cached_summary = record.get("comments")
            comments_changed = (
                cached_summary is None
                or record.get("comment_count") != issue["comment_count"]
                or bool(issue.get("updated_at") and
                        record.get("issue_updated_at") != issue["updated_at"])
            )
            if comments_changed:
                comments = fetch_issue_comments(repo, issue["iid"], headers)
                cached_summary = _comment_summary(comments, issue["author"], bot_users)
                record["comments"] = cached_summary
                record["comment_count"] = issue["comment_count"]
                record["issue_updated_at"] = issue.get("updated_at") or ""
            event = _classify_waiting_event(
                issue, cached_summary, now, initial_hours, followup_hours,
            )
            if not event:
                continue
            linked_authors = fetch_linked_pr_authors(repo, issue["iid"], headers)
            if _is_self_handled(issue, linked_authors):
                continue
            kind, token, waited_hours = event
            event_key = f"{kind}:{token}"
            notifications = record.setdefault("notifications", {})
            notification = notifications.setdefault(
                event_key, {"delivered_users": {}},
            )
            if not isinstance(notification.get("delivered_users"), dict):
                notification["delivered_users"] = {}
            events.append({
                "issue": issue, "kind": kind, "token": token,
                "event_key": event_key, "waited_hours": waited_hours,
                "notification": notification,
            })
        open_keys_by_repo[repo] = open_keys
        successful_repos.add(repo)

    # A full repository scan can safely prune closed Issues. A targeted scan
    # cannot infer anything about other Issue keys in the same repository.
    if target_iid is None:
        for key in list(state["issues"]):
            repo = state["issues"][key].get("repo")
            if not repo:
                repo = key.rsplit("!", 1)[0].replace("__", "/", 1)
            if repo in successful_repos and key not in open_keys_by_repo[repo]:
                del state["issues"][key]
    return events


def revalidate_event(event, headers, now, initial_hours, followup_hours, bot_users):
    """Re-read one Issue immediately before mail delivery."""
    old_issue = event["issue"]
    repo, iid = old_issue["repo"], old_issue["iid"]
    issue = fetch_issue(repo, iid, headers)
    if issue.get("state") == "closed" or not issue.get("author"):
        return None
    comments = _comment_summary(
        fetch_issue_comments(repo, iid, headers), issue["author"], bot_users,
    )
    linked_authors = fetch_linked_pr_authors(repo, iid, headers)
    if _is_self_handled(issue, linked_authors):
        return None
    classified = _classify_waiting_event(
        issue, comments, now, initial_hours, followup_hours,
    )
    if not classified:
        return None
    kind, token, waited_hours = classified
    if f"{kind}:{token}" != event["event_key"]:
        return None
    refreshed = dict(event)
    refreshed.update({
        "issue": issue,
        "kind": kind,
        "token": token,
        "waited_hours": waited_hours,
    })
    return refreshed


def main():
    parser = argparse.ArgumentParser(description="Issue 首响与追问超时邮件提醒")
    parser.add_argument("--dry-run", action="store_true", help="扫描并展示，不发送邮件、不更新状态")
    parser.add_argument("--test", metavar="EMAIL", help="只发送一封测试样本，不更新状态")
    parser.add_argument("--repo", help="仅扫描指定仓库，例如 cann/ge")
    parser.add_argument("--issue", help="仅处理指定 Issue 编号（需要同时指定 --repo）")
    parser.add_argument("--initial-hours", type=float, help="首次无人响应阈值")
    parser.add_argument("--followup-hours", type=float, help="创建者追问无人响应阈值")
    args = parser.parse_args()
    if args.issue and not args.repo:
        parser.error("--issue 需要同时指定 --repo")

    rules = load_rules_config()
    initial_hours = (args.initial_hours if args.initial_hours is not None else
                     rules.get("initial_no_response_hours", DEFAULT_INITIAL_HOURS))
    followup_hours = (args.followup_hours if args.followup_hours is not None else
                      rules.get("author_followup_hours", DEFAULT_FOLLOWUP_HOURS))
    bot_users = set(rules.get("bot_users") or [])
    escalation_users = _dedupe(rules.get("escalation_users") or [])
    if not escalation_users:
        print("✗ 未配置 xia、hong、yuan 的 GitCode 用户名")
        return 1
    token = _load_token()
    if not token:
        print("✗ 缺少 config/gitcode_token.txt，无法读取实时 Issue 数据")
        return 1

    state = load_state()
    original_state = json.dumps(state, ensure_ascii=False, sort_keys=True)
    repos = load_notify_repos()
    if args.repo:
        if args.repo not in repos:
            print(f"✗ 仓库 {args.repo} 未在通知范围内")
            return 1
        repos = [args.repo]
    headers = _v5_headers(token)
    events = scan_events(
        repos, state, headers, _utc_now(),
        float(initial_hours), float(followup_hours), bot_users,
        target_iid=args.issue,
    )
    print(f"扫描完成：发现 {len(events)} 个待提醒事件")
    if args.issue and not events:
        print(f"→ {args.repo}#{args.issue} 当前未命中超时提醒规则")

    smtp_cfg = _smtp_config_or_none()
    mail_map = load_mail_map()
    test_sent = False
    failures = 0

    for event in events:
        if not args.dry_run:
            try:
                event = revalidate_event(
                    event, headers, _utc_now(), float(initial_hours),
                    float(followup_hours), bot_users,
                )
            except Exception as exc:
                failures += 1
                issue = event["issue"]
                print(f"✗ {issue['repo']}#{issue['iid']} 发信前复核失败: {exc}")
                continue
            if event is None:
                print("→ Issue 状态已变化，取消本次发送")
                continue
        issue = event["issue"]
        required_users = _required_recipient_users(
            issue, event["kind"], escalation_users,
        )
        notification = event["notification"]
        delivered_users = dict(notification.get("delivered_users") or {})
        # Compatibility with the unmerged v1 state format.
        legacy_emails = set(notification.get("delivered") or [])
        for username in required_users:
            email = str(mail_map.get(username) or "").strip()
            if email and email in legacy_emails and username not in delivered_users:
                delivered_users[username] = {"email": email, "migrated": True}
        pending_users = [user for user in required_users
                         if user not in delivered_users]
        recipients_by_email, missing = _resolve_recipient_users(
            pending_users, mail_map,
        )
        label = "首次响应超时" if event["kind"] == "initial" else "创建者追问超时"
        subject = f"[CANN Radar] {label}: {issue['repo']}#{issue['iid']}"
        body = build_html_email(issue, event["kind"], event["waited_hours"])

        if args.dry_run:
            _, dry_run_missing = _resolve_recipient_users(required_users, mail_map)
            print(f"→ {issue['repo']}#{issue['iid']} {label}: "
                  f"{len(required_users) - len(dry_run_missing)} 位收件人 [dry-run]")
            continue
        if args.test:
            if not test_sent:
                if smtp_cfg is None:
                    print("✗ SMTP 配置不存在")
                    return 1
                try:
                    send_one_email(smtp_cfg, args.test, f"[TEST] {subject}", body)
                except Exception as exc:
                    print(f"✗ 测试邮件发送失败: {exc}")
                    return 1
                else:
                    print(f"✓ 已发送测试样本到 {args.test}")
                    test_sent = True
            continue
        if smtp_cfg is None:
            print("✗ SMTP 配置不存在")
            return 1

        for email, usernames in recipients_by_email.items():
            try:
                send_one_email(smtp_cfg, email, subject, body)
                sent_at = _utc_now().isoformat(timespec="seconds")
                for username in usernames:
                    delivered_users[username] = {
                        "email": email,
                        "sent_at": sent_at,
                    }
            except Exception as exc:
                failures += 1
                print(f"✗ {issue['repo']}#{issue['iid']} 邮件发送失败: {exc}")
        if missing:
            failures += 1
            print(f"⚠ 以下收件人缺少邮箱映射，后续将继续尝试: {', '.join(missing)}")
        notification["delivered_users"] = delivered_users
        notification["required_users"] = required_users
        notification["satisfied"] = all(
            username in delivered_users for username in required_users
        )
        notification["last_checked_at"] = _utc_now().isoformat(timespec="seconds")
        if notification["satisfied"]:
            notification["satisfied_at"] = notification["last_checked_at"]

    if not args.dry_run and not args.test:
        state["last_run"] = _utc_now().isoformat(timespec="seconds")
        if json.dumps(state, ensure_ascii=False, sort_keys=True) != original_state:
            save_json(STATE_PATH, state)
            print(f"✓ 已更新状态文件: {STATE_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
