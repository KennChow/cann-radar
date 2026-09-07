#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stale_issue_notify.py — 超期 Issue 扫描与邮件通知。

扫描 data/issues/ 中所有 opened 状态的 Issue，筛选出：
  1. 非 Requirement 类型（见 _is_requirement()）
  2. 开启时间达到指定工作日数（默认 10 个工作日）
  3. 首次通知后，每个工作日持续提醒（通过 data/stale_issue_notified.json 记录）

提醒机制：首次提醒当前 assignees；之后每个工作日持续提醒，直到 Issue 关闭。

去重按 issue 维度（{repo}!{iid}），issue 的当前 assignees 各自收个人通知。

内/外判定基于 gitcode_2_mail.txt。

用法：
    python stale_issue_notify.py --dry-run
    python stale_issue_notify.py
    python stale_issue_notify.py --stale-days 7
    python stale_issue_notify.py --report-to admin@huawei.com
    python stale_issue_notify.py --test someone@huawei.com
"""

import argparse
import configparser
import html
import json
import smtplib
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

import yaml

try:
    from chinese_calendar import is_workday
except ImportError:
    def is_workday(d):
        return d.weekday() < 5
    print("  ⚠ chinese_calendar 未安装，仅排除周末")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ISSUES_DIR = DATA_DIR / "issues"
REPOS_CONFIG_PATH = Path("config/repos.yml")
MAIL_MAP_PATH = Path("config/gitcode_2_mail.txt")
SMTP_CONFIG_PATH = Path("config/smtp_config.ini")
NOTIFIED_PATH = DATA_DIR / "stale_issue_notified.json"

DEFAULT_STALE_DAYS = 10
RESEND_INTERVAL_DAYS = 1

CONTACT_INFO = "如有疑问请联系夏国正 x00806611"


def _is_requirement(issue_type, title, labels):
    if issue_type == "需求":
        return True
    title_lower = (title or "").lower()
    if any(kw in title_lower for kw in ["requirement", "feature", "[rfc]"]):
        return True
    labels_lower = [l.lower() for l in (labels or [])]
    if any(kw in labels_lower for kw in ["requirement", "feature"]):
        return True
    return False


def _build_linked_pr_map(notify_paths):
    """从 MR 数据构建 (repo, issue) → MR authors 映射，避免跨仓串号。"""
    linked = defaultdict(set)
    mrs_dir = Path("data/mrs")
    if not mrs_dir.exists():
        return linked
    for f in sorted(mrs_dir.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if notify_paths and repo_path not in notify_paths:
            continue
        try:
            mrs = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        for mr in mrs:
            mr_author = mr.get("author", "")
            if not mr_author:
                continue
            for issue_num in mr.get("e2e_issues") or []:
                linked[(repo_path, str(issue_num))].add(mr_author)
    return linked


def _is_self_assigned(issue, repo_path, linked_pr_map):
    """判定 issue 是否自提（无需发送邮件提醒）。"""
    author = issue.get("author", "")
    assignees = issue.get("assignees") or []

    # 提单人是负责人之一
    if author and author in assignees:
        return True

    # 提单人关联了自己的 PR
    iid = str(issue.get("iid", ""))
    linked_authors = linked_pr_map.get((repo_path, iid), set())
    if author and author in linked_authors:
        return True

    return False


def load_notify_repo_paths():
    if not REPOS_CONFIG_PATH.exists():
        print(f"  ✗ 仓库配置不存在: {REPOS_CONFIG_PATH}")
        return set()
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    repos = config.get("repos", []) or []
    paths = set()
    for repo in repos:
        if repo.get("enabled", True) and repo.get("notify", False):
            paths.add(repo["path"])
    return paths


def load_mail_map():
    mapping = {}
    if not MAIL_MAP_PATH.exists():
        print(f"  ✗ 邮箱映射文件不存在: {MAIL_MAP_PATH}")
        return mapping
    for line in MAIL_MAP_PATH.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        username, col2, col3 = parts[0], parts[1], parts[2]
        if not username:
            continue
        email = col2 if col2 and col2 != "null" else col3
        if email and email != "null":
            mapping[username] = email
        else:
            mapping[username] = None
    return mapping


def _has_valid_email(mail_map, author):
    return mail_map.get(author) is not None


def load_repo_admin_map():
    admin_map = {}
    if not REPOS_CONFIG_PATH.exists():
        return admin_map
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    for repo in (config.get("repos") or []):
        path = repo.get("path", "")
        admin = repo.get("admin", "")
        if path and admin:
            admin_map[path] = admin
    return admin_map


def _author_display(author, mail_map):
    if author in mail_map:
        if mail_map[author]:
            return author
        return f"{author} (无邮箱映射)"
    return f"{author} (外部)"


def load_notified():
    if not NOTIFIED_PATH.exists():
        return {"last_run": None, "notified": {}}
    try:
        return json.loads(NOTIFIED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {"last_run": None, "notified": {}}


def save_notified(notified):
    notified["last_run"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFIED_PATH.write_text(json.dumps(notified, ensure_ascii=False, indent=2), encoding="utf-8")


def _issue_key(repo_path, iid):
    safe = repo_path.replace("/", "__")
    return f"{safe}!{iid}"


def _working_days_between(start_date, end_date):
    count = 0
    d = start_date
    while d <= end_date:
        if is_workday(d):
            count += 1
        d += timedelta(days=1)
    return count


def _working_days_since(date_str):
    try:
        start = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return _working_days_between(start, date.today())
    except (ValueError, TypeError):
        return 0


SMTP_CONFIG_TEMPLATE = """\
[smtp]
server = smtp.huawei.com
port = 465
username = your_name@huawei.com
password = your_auth_code

[mail]
from = your_name@huawei.com

[issue_response]
# xgz、hyc、wrq 的邮箱，逗号分隔；也可通过 GitHub Secret
# ISSUE_RESPONSE_ESCALATION_TO 提供。
escalation_to =
"""


def init_smtp_config():
    if SMTP_CONFIG_PATH.exists():
        print(f"  SMTP 配置已存在: {SMTP_CONFIG_PATH}")
        return
    SMTP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SMTP_CONFIG_PATH.write_text(SMTP_CONFIG_TEMPLATE, encoding="utf-8")
    print(f"  ✓ 已生成 SMTP 配置模板: {SMTP_CONFIG_PATH}")
    print(f"    请编辑该文件，填入实际的邮箱地址和授权码。")


def load_smtp_config():
    if not SMTP_CONFIG_PATH.exists():
        print(f"  ✗ SMTP 配置不存在: {SMTP_CONFIG_PATH}")
        print(f"    请先运行: python stale_issue_notify.py --init-smtp")
        return None
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONFIG_PATH, encoding="utf-8")
    required_keys = ["server", "port", "username", "password"]
    for key in required_keys:
        if not cfg.get("smtp", key, fallback="").strip():
            print(f"  ✗ SMTP 配置缺少 [smtp] {key}")
            return None
    return cfg


def _check_issue_notify_status(key, notified, today):
    """首次后每个工作日持续提醒"""
    if key not in notified:
        return True, 1, ''
    record = notified[key]
    last_at = record.get("notified_at", "")
    if not last_at:
        return True, 1, ''
    try:
        last_date = datetime.strptime(last_at[:10], "%Y-%m-%d").date()
    except ValueError:
        return True, 1, ''
    if last_date >= today:
        return False, 0, 'waiting'
    count = record.get("count", 1)
    working_days = _working_days_between(last_date, today)
    if working_days >= RESEND_INTERVAL_DAYS:
        return True, count + 1, ''
    return False, 0, 'waiting'


def scan_stale_issues(stale_days, notify_paths=None, notified=None):
    today = datetime.now()
    matched_issues = []
    stats = {
        "total_opened": 0, "total_non_req": 0, "stale_matched": 0,
        "repos_scanned": 0, "skipped_waiting": 0,
        "stage1_count": 0, "stage2_count": 0, "total_requirement": 0,
    }

    if notified is None:
        notified = {}

    if not ISSUES_DIR.exists():
        print(f"  ✗ Issue 数据目录不存在: {ISSUES_DIR}")
        return matched_issues, stats

    for f in sorted(ISSUES_DIR.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if notify_paths is not None and repo_path not in notify_paths:
            continue
        issues = json.loads(f.read_text(encoding="utf-8"))
        stats["repos_scanned"] += 1

        for issue in issues:
            if issue.get("state") != "opened":
                continue
            stats["total_opened"] += 1

            iid = issue.get("iid")
            if iid is None:
                continue

            title = issue.get("title") or ""
            labels = issue.get("labels") or []
            issue_type = issue.get("issue_type") or ""

            if _is_requirement(issue_type, title, labels):
                stats["total_requirement"] += 1
                continue
            stats["total_non_req"] += 1

            key = _issue_key(repo_path, iid)
            should_notify, stage, skip_reason = _check_issue_notify_status(key, notified, today.date())
            if not should_notify:
                stats["skipped_waiting"] += 1
                continue

            created_at = issue.get("created_at", "")
            if not created_at:
                continue
            days_open = _working_days_since(created_at)
            if days_open < stale_days:
                continue
            stats["stale_matched"] += 1

            if stage > 1:
                stats["stage2_count"] += 1
            else:
                stats["stage1_count"] += 1

            assignees = issue.get("assignees") or []
            matched_issues.append({
                "repo": repo_path,
                "iid": iid,
                "title": title,
                "created_at": created_at,
                "author": issue.get("author", ""),
                "days_open": days_open,
                "web_url": issue.get("web_url", ""),
                "labels": labels,
                "assignees": assignees,
                "notify_stage": stage,
            })

    return matched_issues, stats


def _build_issue_table_rows(issues):
    rows = ""
    for iss in sorted(issues, key=lambda x: -x["days_open"]):
        labels_str = html.escape(", ".join(iss["labels"]) if iss["labels"] else "-")
        stage_note = " <span style='color:#e05f5f;font-size:11px'>（持续提醒）</span>" if iss.get("notify_stage", 1) > 1 else ""
        rows += f"""<tr>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">{html.escape(str(iss['repo']))}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">
    <a href="{_safe_web_url(iss.get('web_url'))}" style="color:#2563eb;text-decoration:none">#{html.escape(str(iss['iid']))}</a>
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{html.escape(str(iss['title']))}{stage_note}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{iss['days_open']}天</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#666">{labels_str}</td>
</tr>"""
    return rows


def _safe_web_url(value):
    value = str(value or "")
    return html.escape(value, quote=True) if value.startswith(("https://", "http://")) else "#"


def build_html_email(assignee, issues, stale_days=DEFAULT_STALE_DAYS):
    stage2_count = sum(1 for i in issues if i.get("notify_stage", 1) > 1)
    rows = _build_issue_table_rows(issues)
    escalation_note = ""
    if stage2_count:
        escalation_note = f"""
  <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:12px 16px;margin-bottom:16px">
    <strong style="color:#856404">以下 {stage2_count} 个 Issue 正在持续提醒中。</strong>
  </div>"""
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 Issue 提醒</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    Hi {html.escape(str(assignee))}，您有 <strong style="color:#e05f5f">{len(issues)}</strong> 个非 Requirement Issue 已开启达到 {stale_days} 个工作日，请及时处理。
  </p>
  {escalation_note}
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Issue</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启工作天数</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#999;font-size:11px;margin-top:16px">
    此邮件由 CANN Radar 自动发送，请检查 Issue 状态后及时处理。
  </p>
  <p style="color:#999;font-size:11px">{CONTACT_INFO}</p>
</div>"""


def send_one_email(cfg, to_email, subject, html_body, cc_email=None):
    server = cfg.get("smtp", "server").strip()
    port = int(cfg.get("smtp", "port").strip())
    username = cfg.get("smtp", "username").strip()
    password = cfg.get("smtp", "password").strip()
    sender = cfg.get("mail", "from", fallback=username).strip()

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("CANN Radar", "utf-8")), sender))
    msg["To"] = to_email
    if cc_email:
        msg["Cc"] = cc_email
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [e.strip() for e in to_email.split(",") if e.strip()]
    if cc_email:
        recipients += [e.strip() for e in cc_email.split(",") if e.strip()]

    with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.sendmail(sender, recipients, msg.as_string())


def _mark_issue_notified(notified_data, issues):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for iss in issues:
        key = _issue_key(iss["repo"], iss["iid"])
        if not key:
            continue
        existing = notified_data["notified"].get(key)
        new_count = (existing.get("count", 0) + 1) if existing else 1
        notified_data["notified"][key] = {
            "notified_at": now, "count": new_count,
            "assignees": iss.get("assignees") or [],
        }


def _save_admin_issue_summary(notify_paths, notified_data, linked_pr_map, mail_map, stale_days, stats):
    """保存管理员汇总 JSON（含所有 open 超期 Issue，带跟踪状态和自提过滤）。"""
    notified = notified_data.get("notified", {})
    summary = []
    today = date.today()

    for f in sorted(ISSUES_DIR.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if notify_paths and repo_path not in notify_paths:
            continue
        issues = json.loads(f.read_text(encoding="utf-8"))
        for iss in issues:
            if iss.get("state") != "opened":
                continue
            iid = iss.get("iid")
            if iid is None:
                continue
            title = iss.get("title") or ""
            labels = iss.get("labels") or []
            itype = iss.get("issue_type") or ""
            if _is_requirement(itype, title, labels):
                continue
            days_open = _working_days_since(iss.get("created_at", ""))
            if days_open < stale_days:
                continue
            # self-assigned check
            if _is_self_assigned(iss, repo_path, linked_pr_map):
                continue

            key = _issue_key(repo_path, iid)
            assignees = iss.get("assignees") or []
            assignee_display = ", ".join(assignees) if assignees else "(未分配)"

            if key in notified:
                record = notified[key]
                cnt = record.get("count", 1)
                status = "daily" if cnt >= 2 else "waiting"
            else:
                status = "new"

            cat = "未分配" if not assignees else "有邮箱" if any(_has_valid_email(mail_map, a) for a in assignees) else "外部"

            summary.append({
                "repo": repo_path, "iid": iid, "title": title,
                "days_open": days_open, "web_url": iss.get("web_url", ""),
                "assignee_display": assignee_display, "category": cat, "status": status,
            })

    counts = {"new": 0, "daily": 0}
    for s in summary:
        counts["new"] += 1 if s["status"] == "new" else 0
        counts["daily"] += 1 if s["status"] == "daily" else 0

    with open(DATA_DIR / "admin_issue_summary.json", "w", encoding="utf-8") as f:
        json.dump({"issue_items": summary, "stale_days": stale_days}, f, ensure_ascii=False)
    print(f"\n  管理员汇总: {len(summary)} 个 Issue（新发现 {counts['new']}，需介入 {counts['daily']}）")


def _fully_delivered_issues(issues, required_recipients, delivered_recipients):
    """只返回所有可通知负责人均已成功收到邮件的 Issue。"""
    result = []
    for issue in issues:
        key = _issue_key(issue["repo"], issue["iid"])
        required = required_recipients.get(key, set())
        if required and required <= delivered_recipients.get(key, set()):
            result.append(issue)
    return result


def main():
    parser = argparse.ArgumentParser(description="超期 Issue 扫描与邮件通知")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发送邮件")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help=f"超期工作日阈值（默认 {DEFAULT_STALE_DAYS}）")
    parser.add_argument("--report-to", help="管理员汇总报告发送到此邮箱（测试用）")
    parser.add_argument("--test", metavar="EMAIL", help="测试模式：仅发送1封样本到指定邮箱，不发给实际作者")
    parser.add_argument("--init-smtp", action="store_true", help="生成 SMTP 配置模板到 config/smtp_config.ini")
    args = parser.parse_args()

    if args.init_smtp:
        init_smtp_config()
        return 0

    print(f"=== 超期 Issue 扫描 ===")
    print(f"  超期天数: ≥{args.stale_days} 个工作日")
    print(f"  超期阈值: ≥{args.stale_days} 个工作日，首次后每个工作日持续提醒")
    if args.test:
        print(f"  模式: 测试（仅1封样本发送到 {args.test}）")
    elif args.dry_run:
        print(f"  模式: dry-run（不发送）")
    else:
        print(f"  模式: 正式发送")

    notify_paths = load_notify_repo_paths()
    if not notify_paths:
        print("  ✗ 无仓库配置 notify: true，请在 config/repos.yml 中设置")
        return 1
    print(f"  通知仓库: {', '.join(sorted(notify_paths))}")

    mail_map = load_mail_map()
    print(f"  邮箱映射: {len(mail_map)} 条")

    notified_data = load_notified()
    print(f"  已追踪 Issue: {len(notified_data.get('notified', {}))} 个")

    repo_admin_map = load_repo_admin_map()
    for repo in sorted(notify_paths):
        if repo not in repo_admin_map or not repo_admin_map[repo]:
            print(f"  ⚠ {repo} 已启用 notify 但未配置 admin，将不发送汇总报告")

    matched_issues, stats = scan_stale_issues(
        args.stale_days, notify_paths, notified_data.get("notified", {}),
    )
    print(f"\n  扫描结果:")
    print(f"    仓库: {stats['repos_scanned']}")
    print(f"    Opened Issue: {stats['total_opened']}")
    print(f"    Requirement（排除）: {stats['total_requirement']}")
    print(f"    超期非Requirement: {stats['stale_matched']}")
    print(f"    首次通知: {stats['stage1_count']}")
    print(f"    持续提醒: {stats['stage2_count']}")
    print(f"    未到重发间隔跳过: {stats['skipped_waiting']}")

    # 构建关联 PR 映射（需在保存前调用）
    linked_pr_map = _build_linked_pr_map(notify_paths)

    # 保存管理员汇总数据（含所有 open 超期 Issue，带状态）
    _save_admin_issue_summary(notify_paths, notified_data, linked_pr_map, mail_map, args.stale_days, stats)

    if not matched_issues:
        print("\n  ✓ 无首次/持续提醒待发送超期非Requirement Issue，无需通知")
        return 0

    # 自提过滤
    linked_pr_map = _build_linked_pr_map(notify_paths)
    print(f"  关联 PR 映射: {len(linked_pr_map)} 个 issue 有关联 MR")
    self_assigned_count = 0
    remaining_issues = []
    for iss in matched_issues:
        if _is_self_assigned(iss, iss["repo"], linked_pr_map):
            self_assigned_count += 1
        else:
            remaining_issues.append(iss)
    if self_assigned_count:
        print(f"  自提排除: {self_assigned_count} 个 Issue")
        for iss in matched_issues:
            if _is_self_assigned(iss, iss["repo"], linked_pr_map):
                print(f"    #{html.escape(str(iss['iid']))} author={iss.get('author', '')} (自提)")
    matched_issues = remaining_issues

    if not matched_issues:
        print("\n  ✓ 均为自提 Issue，无需通知")
        return 0

    # 按 assignee 聚合 (每人一份邮件)
    assignee_issues = defaultdict(list)
    unassigned_issues = []
    for iss in matched_issues:
        assignees = iss.get("assignees") or []
        if not assignees:
            unassigned_issues.append(iss)
            continue
        for a in assignees:
            assignee_issues[a].append(iss)

    # 分类 assignee
    has_email_assignees = {}
    null_email_assignees = defaultdict(list)
    external_assignees = defaultdict(list)

    for assignee, issues in assignee_issues.items():
        if _has_valid_email(mail_map, assignee):
            has_email_assignees[assignee] = (mail_map[assignee], issues)
        elif assignee in mail_map:
            null_email_assignees[assignee] = issues
        else:
            external_assignees[assignee] = issues

    print(f"\n  分类结果:")
    print(f"    有邮箱 assignee: {len(has_email_assignees)} 人")
    print(f"    有映射无邮箱: {len(null_email_assignees)} 人")
    print(f"    外部 assignee: {len(external_assignees)} 人")
    print(f"    未分配负责人: {len(unassigned_issues)} 个 Issue")

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg:
            print("\n  ✗ SMTP 配置不可用，请使用 --dry-run 测试或先配置 SMTP")
            return 1

    required_recipients = defaultdict(set)
    for assignee, (_, issues) in has_email_assignees.items():
        for issue in issues:
            required_recipients[_issue_key(issue["repo"], issue["iid"])].add(assignee)
    delivered_recipients = defaultdict(set)

    notified_changed = False

    # 发送个人通知
    print(f"\n=== 发送个人通知 ===")
    sent = 0
    failed = 0
    test_sent = False

    for assignee, (email, issues) in sorted(has_email_assignees.items(), key=lambda x: -len(x[1][1])):
        has_stage2 = any(i.get("notify_stage", 1) > 1 for i in issues)
        stage2_count = sum(1 for i in issues if i.get("notify_stage", 1) > 1)
        subject = f"[CANN] 您有 {len(issues)} 个超期未关闭的 Issue（非Requirement）"
        if has_stage2:
            subject += " [持续提醒]"
        email_html = build_html_email(assignee, issues, args.stale_days)

        cc = None

        if args.dry_run:
            cc_str = f" 抄送:{cc}" if cc else ""
            stage_note = f" 其中{stage2_count}个持续提醒" if has_stage2 else ""
            print(f"  → {assignee} <{email}>{cc_str}: {len(issues)} 个 Issue{stage_note} [dry-run，未发送]")
        elif args.test:
            if not test_sent:
                try:
                    send_one_email(smtp_cfg, args.test, subject, email_html, cc_email=cc)
                    sent += 1
                    test_sent = True
                    stage_note = f" 含{stage2_count}个持续提醒" if has_stage2 else ""
                    print(f"  ✓ {assignee} <{email}> → {args.test}: {len(issues)} 个 Issue{stage_note} [测试样本，仅此1封]")
                except Exception as e:
                    failed += 1
                    ids = ", ".join(f"#{i['iid']}" for i in issues)
                    print(f"  ✗ {assignee} <{email}>: {e}  Issue: {ids}")
            else:
                print(f"  ⊘ {assignee} <{email}>: {len(issues)} 个 Issue [测试模式，跳过]")
        else:
            try:
                send_one_email(smtp_cfg, email, subject, email_html, cc_email=cc)
                sent += 1
                for issue in issues:
                    delivered_recipients[_issue_key(issue["repo"], issue["iid"])].add(assignee)
                cc_str = f"，抄送管理员" if cc else ""
                stage_note = f" 含{stage2_count}个持续提醒" if has_stage2 else ""
                print(f"  ✓ {assignee} <{email}>: {len(issues)} 个 Issue{stage_note}{cc_str}")
            except Exception as e:
                failed += 1
                ids = ", ".join(f"#{i['iid']}" for i in issues)
                print(f"  ✗ {assignee} <{email}>: {e}  Issue: {ids}")

    fully_delivered = _fully_delivered_issues(
        matched_issues, required_recipients, delivered_recipients,
    )
    if fully_delivered and not args.dry_run and not args.test:
        _mark_issue_notified(notified_data, fully_delivered)
        notified_changed = True
    if not args.dry_run:
        print(f"\n  个人通知: 已发送 {sent}, 失败 {failed}")

    if notified_changed and not args.dry_run:
        save_notified(notified_data)
        print(f"\n  ✓ 已更新通知记录: {NOTIFIED_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
