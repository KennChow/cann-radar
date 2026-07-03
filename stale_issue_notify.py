#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stale_issue_notify.py — 超期 Issue 扫描与邮件通知。

扫描 data/issues/ 中所有 opened 状态的 Issue，筛选出：
  1. 非 Requirement 类型（见 _is_requirement()）
  2. 开启时间超过指定工作日数（默认 14 个工作日）
  3. 距离上次通知 ≥ 7 个工作日 或 尚未通知（通过 data/stale_issue_notified.json 去重）

升级机制：
  - 第 1 次通知：提醒所有当前 assignees
  - 第 2 次通知（距上次 ≥7 个工作日 issue 仍 open）：提醒 assignees 并抄送管理员
  - 此后永久跳过

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
ADMIN_EMAIL_PATH = Path("config/admin_email.txt")
NOTIFIED_PATH = DATA_DIR / "stale_issue_notified.json"

DEFAULT_STALE_DAYS = 14
RESEND_INTERVAL_DAYS = 7
MAX_NOTIFY_COUNT = 2

CONTACT_INFO = "如有疑问请联系夏国正 x00806611"


def _is_requirement(issue_type, title, labels):
    if issue_type == "需求":
        return True
    if title:
        if '[RFC]' in title or '[Feature-Request|需求反馈]' in title:
            return True
    if labels and 'requirement' in labels:
        return True
    return False


def _build_linked_pr_map(notify_paths):
    """从 MR 数据构建 issue → set of linked MR authors 的映射（用于自提判定）。"""
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
                linked[issue_num].add(mr_author)
    return linked


def _is_self_assigned(issue, linked_pr_map, mail_map):
    """判定 issue 是否自提（无需发送邮件提醒）。"""
    author = issue.get("author", "")
    assignees = issue.get("assignees") or []

    # 提单人是负责人之一
    if author and author in assignees:
        return True

    # 提单人关联了自己的 PR
    iid = str(issue.get("iid", ""))
    linked_authors = linked_pr_map.get(iid, set())
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


def load_admin_email():
    if not ADMIN_EMAIL_PATH.exists():
        return None
    text = ADMIN_EMAIL_PATH.read_text(encoding="utf-8").strip()
    if text:
        return text.splitlines()[0].strip()
    return None


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
    """返回 (should_notify, notify_stage, skip_reason)。"""
    if key not in notified:
        return True, 1, ''
    record = notified[key]
    count = record.get("count", 1)
    if count >= MAX_NOTIFY_COUNT:
        return False, 0, 'max'
    last_at = record.get("notified_at", "")
    if not last_at:
        return False, 0, 'max'
    try:
        last_date = datetime.strptime(last_at[:10], "%Y-%m-%d").date()
    except ValueError:
        return False, 0, 'max'
    working_days = _working_days_between(last_date, today)
    if working_days >= RESEND_INTERVAL_DAYS:
        return True, count + 1, ''
    return False, 0, 'waiting'


def scan_stale_issues(stale_days, notify_paths=None, notified=None):
    today = datetime.now()
    matched_issues = []
    stats = {
        "total_opened": 0, "total_non_req": 0, "stale_matched": 0,
        "repos_scanned": 0, "skipped_waiting": 0, "skipped_max": 0,
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
                if skip_reason == 'max':
                    stats["skipped_max"] += 1
                else:
                    stats["skipped_waiting"] += 1
                continue

            created_at = issue.get("created_at", "")
            if not created_at:
                continue
            days_open = issue.get("working_days_open") or _working_days_since(created_at)
            if days_open <= stale_days:
                continue
            stats["stale_matched"] += 1

            if stage == 2:
                stats["stage2_count"] += 1
            else:
                stats["stage1_count"] += 1

            assignees = issue.get("assignees") or []
            matched_issues.append({
                "repo": repo_path,
                "iid": iid,
                "title": title,
                "created_at": created_at,
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
        labels_str = ", ".join(iss["labels"]) if iss["labels"] else "-"
        stage_note = " <span style='color:#e05f5f;font-size:11px'>(二次提醒)</span>" if iss.get("notify_stage") == 2 else ""
        rows += f"""<tr>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">{iss['repo']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">
    <a href="{iss['web_url']}" style="color:#2563eb;text-decoration:none">#{iss['iid']}</a>
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{iss['title']}{stage_note}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{iss['days_open']}天</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#666">{labels_str}</td>
</tr>"""
    return rows


def build_html_email(assignee, issues):
    stage2_count = sum(1 for i in issues if i.get("notify_stage") == 2)
    rows = _build_issue_table_rows(issues)
    escalation_note = ""
    if stage2_count:
        escalation_note = f"""
  <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:12px 16px;margin-bottom:16px">
    <strong style="color:#856404">⚠ 以下 {stage2_count} 个 Issue 已二次提醒，并抄送管理员跟进。</strong>
  </div>"""
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 Issue 提醒</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    Hi {assignee}，您有 <strong style="color:#e05f5f">{len(issues)}</strong> 个非 Requirement Issue 已开启超过 14 个工作日，请及时处理。
  </p>
  {escalation_note}
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Issue</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
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


def build_admin_report_html(stats, unassigned_issues, null_email_by_assignee, external_by_assignee, stale_days):
    sections = ""

    if unassigned_issues:
        rows = _build_issue_table_rows(unassigned_issues)
        sections += f"""
  <h3 style="font-size:15px;margin-top:24px;color:#e05f5f;border-bottom:1px solid #e2e4ea;padding-bottom:6px">
    未分配负责人（{len(unassigned_issues)} 个 Issue）
  </h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden;margin-bottom:16px">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Issue</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    for assignee in sorted(null_email_by_assignee.keys()):
        issues = null_email_by_assignee[assignee]
        rows = _build_issue_table_rows(issues)
        sections += f"""
  <h4 style="font-size:14px;margin:20px 0 8px;color:#e05f5f">{assignee}（有映射无邮箱，{len(issues)} 个 Issue）</h4>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden;margin-bottom:16px">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Issue</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    for assignee in sorted(external_by_assignee.keys()):
        issues = external_by_assignee[assignee]
        rows = _build_issue_table_rows(issues)
        sections += f"""
  <h4 style="font-size:14px;margin:20px 0 8px;color:#f5a623">{assignee}（外部，{len(issues)} 个 Issue）</h4>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden;margin-bottom:16px">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Issue</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    null_count = sum(len(v) for v in null_email_by_assignee.values())
    ext_count = sum(len(v) for v in external_by_assignee.values())

    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 Issue 管理员汇总报告</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    扫描条件：非Requirement、开启超过 <strong>{stale_days}</strong> 个工作日（距上次通知≥{RESEND_INTERVAL_DAYS}个工作日可重发，最多{MAX_NOTIFY_COUNT}次）
  </p>
  <table style="font-size:13px;border-collapse:collapse;margin-bottom:20px">
    <tr><td style="padding:4px 16px 4px 0;color:#666">扫描仓库</td><td><strong>{stats['repos_scanned']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">Opened Issue 总数</td><td><strong>{stats['total_opened']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">Requirement（排除）</td><td><strong>{stats['total_requirement']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">超期非Requirement</td><td><strong>{stats['stale_matched']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">首次通知</td><td><strong>{stats.get('stage1_count', 0)}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">二次升级通知</td><td><strong style="color:#e05f5f">{stats.get('stage2_count', 0)}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">未到重发间隔跳过</td><td><strong>{stats['skipped_waiting']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">已达上限永久跳过</td><td><strong>{stats['skipped_max']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">未分配负责人</td><td><strong style="color:#e05f5f">{len(unassigned_issues)}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">有映射无邮箱</td><td><strong style="color:#e05f5f">{null_count}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">外部 assignees</td><td><strong style="color:#f5a623">{ext_count}</strong></td></tr>
  </table>
  {sections}
  <p style="color:#999;font-size:11px;margin-top:20px">CANN Radar 自动生成 · {CONTACT_INFO}</p>
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

    recipients = [to_email]
    if cc_email:
        recipients.append(cc_email)

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


def main():
    parser = argparse.ArgumentParser(description="超期 Issue 扫描与邮件通知")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发送邮件")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help=f"超期工作日阈值（默认 {DEFAULT_STALE_DAYS}）")
    parser.add_argument("--report-to", help="管理员汇总报告发送到此邮箱（覆盖 config/admin_email.txt）")
    parser.add_argument("--test", metavar="EMAIL", help="测试模式：仅发送1封样本到指定邮箱，不发给实际作者")
    parser.add_argument("--init-smtp", action="store_true", help="生成 SMTP 配置模板到 config/smtp_config.ini")
    args = parser.parse_args()

    if args.init_smtp:
        init_smtp_config()
        return 0

    print(f"=== 超期 Issue 扫描 ===")
    print(f"  超期天数: >{args.stale_days} 个工作日")
    print(f"  升级间隔: 距上次通知≥{RESEND_INTERVAL_DAYS} 个工作日可重发（最多{MAX_NOTIFY_COUNT}次）")
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

    admin_email = args.report_to or load_admin_email()
    if admin_email:
        print(f"  管理员邮箱: {admin_email}")
    else:
        print(f"  管理员邮箱: 未配置（将不发送汇总报告）")

    matched_issues, stats = scan_stale_issues(
        args.stale_days, notify_paths, notified_data.get("notified", {}),
    )
    print(f"\n  扫描结果:")
    print(f"    仓库: {stats['repos_scanned']}")
    print(f"    Opened Issue: {stats['total_opened']}")
    print(f"    Requirement（排除）: {stats['total_requirement']}")
    print(f"    超期非Requirement: {stats['stale_matched']}")
    print(f"    首次通知: {stats['stage1_count']}")
    print(f"    二次升级: {stats['stage2_count']}")
    print(f"    未到重发间隔跳过: {stats['skipped_waiting']}")
    print(f"    已达上限永久跳过: {stats['skipped_max']}")

    if not matched_issues:
        print("\n  ✓ 无新增/待升级超期非Requirement Issue，无需通知")
        return 0

    # 自提过滤
    linked_pr_map = _build_linked_pr_map(notify_paths)
    print(f"  关联 PR 映射: {len(linked_pr_map)} 个 issue 有关联 MR")
    self_assigned_count = 0
    remaining_issues = []
    for iss in matched_issues:
        if _is_self_assigned(iss, linked_pr_map, mail_map):
            self_assigned_count += 1
        else:
            remaining_issues.append(iss)
    if self_assigned_count:
        print(f"  自提排除: {self_assigned_count} 个 Issue")
        for iss in matched_issues:
            if _is_self_assigned(iss, linked_pr_map, mail_map):
                print(f"    #{iss['iid']} author={iss['author']} (自提)")
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

    if not admin_email:
        if null_email_assignees or external_assignees or unassigned_issues:
            print(f"\n  ⚠ 管理员邮箱未配置，以下 Issue/用户无法收到通知：")
            for a in sorted(null_email_assignees.keys()):
                ids = ", ".join(f"#{i['iid']}" for i in null_email_assignees[a])
                print(f"    有映射无邮箱: {a} ({ids})")
            for a in sorted(external_assignees.keys()):
                ids = ", ".join(f"#{i['iid']}" for i in external_assignees[a])
                print(f"    外部 assignee: {a} ({ids})")
            for iss in unassigned_issues:
                print(f"    未分配负责人: #{iss['iid']} {iss['title'][:40]}")

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg:
            print("\n  ✗ SMTP 配置不可用，请使用 --dry-run 测试或先配置 SMTP")
            return 1

    notified_changed = False

    # 发送个人通知
    print(f"\n=== 发送个人通知 ===")
    sent = 0
    failed = 0
    test_sent = False

    for assignee, (email, issues) in sorted(has_email_assignees.items(), key=lambda x: -len(x[1][1])):
        has_stage2 = any(i.get("notify_stage") == 2 for i in issues)
        stage2_count = sum(1 for i in issues if i.get("notify_stage") == 2)
        subject = f"[CANN] 您有 {len(issues)} 个超期未关闭的 Issue（非Requirement）"
        if has_stage2:
            subject += " [二次提醒]"
        html = build_html_email(assignee, issues)

        cc = None
        if has_stage2 and admin_email and admin_email != email:
            cc = admin_email

        if args.dry_run:
            cc_str = f" 抄送:{cc}" if cc else ""
            stage_note = f" 其中{stage2_count}个二次提醒" if has_stage2 else ""
            print(f"  → {assignee} <{email}>{cc_str}: {len(issues)} 个 Issue{stage_note} [dry-run，未发送]")
        elif args.test:
            if not test_sent:
                try:
                    send_one_email(smtp_cfg, args.test, subject, html, cc_email=cc)
                    sent += 1
                    test_sent = True
                    stage_note = f" 含{stage2_count}个二次提醒" if has_stage2 else ""
                    print(f"  ✓ {assignee} <{email}> → {args.test}: {len(issues)} 个 Issue{stage_note} [测试样本，仅此1封]")
                except Exception as e:
                    failed += 1
                    ids = ", ".join(f"#{i['iid']}" for i in issues)
                    print(f"  ✗ {assignee} <{email}>: {e}  Issue: {ids}")
            else:
                print(f"  ⊘ {assignee} <{email}>: {len(issues)} 个 Issue [测试模式，跳过]")
        else:
            try:
                send_one_email(smtp_cfg, email, subject, html, cc_email=cc)
                sent += 1
                cc_str = f"，抄送管理员" if cc else ""
                stage_note = f" 含{stage2_count}个二次提醒" if has_stage2 else ""
                print(f"  ✓ {assignee} <{email}>: {len(issues)} 个 Issue{stage_note}{cc_str}")
            except Exception as e:
                failed += 1
                ids = ", ".join(f"#{i['iid']}" for i in issues)
                print(f"  ✗ {assignee} <{email}>: {e}  Issue: {ids}")

    if sent > 0 and not args.dry_run:
        notified_changed = True
    if not args.dry_run:
        print(f"\n  个人通知: 已发送 {sent}, 失败 {failed}")

    # 标记已通知的 issue
    if sent > 0 and not args.dry_run:
        _mark_issue_notified(notified_data, matched_issues)

    # 管理员汇总报告
    if args.dry_run:
        if admin_email and (null_email_assignees or external_assignees or unassigned_issues):
            print(f"\n=== 管理员汇总报告 ===")
            print(f"  → 将发送到 {admin_email} [dry-run，未发送]")
            print(f"    有映射无邮箱: {len(null_email_assignees)} 人")
            print(f"    外部 assignee: {len(external_assignees)} 人")
            print(f"    未分配负责人: {len(unassigned_issues)} 个")
        elif not admin_email and (null_email_assignees or external_assignees or unassigned_issues):
            print(f"\n=== 管理员汇总报告 ===")
            print(f"  → 管理员邮箱未配置，跳过发送 [dry-run]")
    elif admin_email and (null_email_assignees or external_assignees or unassigned_issues):
        print(f"\n=== 发送管理员汇总报告 → {admin_email} ===")
        admin_html = build_admin_report_html(
            stats, unassigned_issues, null_email_assignees, external_assignees, args.stale_days,
        )
        try:
            send_one_email(
                smtp_cfg, admin_email,
                f"[CANN] 超期 Issue 管理员报告（未分配 {len(unassigned_issues)}，无邮箱 {len(null_email_assignees)}，外部 {len(external_assignees)}）",
                admin_html,
            )
            print(f"  ✓ 管理员汇总报告已发送")
            notified_data["admin_report_last_sent"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            notified_data["admin_report_summary"] = {
                "unassigned": len(unassigned_issues),
                "null_email": len(null_email_assignees),
                "external": len(external_assignees),
            }
            notified_changed = True
        except Exception as e:
            print(f"  ✗ 管理员汇总报告发送失败: {e}")

    if notified_changed and not args.dry_run:
        save_notified(notified_data)
        print(f"\n  ✓ 已更新通知记录: {NOTIFIED_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
