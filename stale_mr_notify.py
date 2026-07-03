#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stale_mr_notify.py — 超期 MR 扫描与邮件通知。

扫描 data/mrs/ 中所有 opened 状态的 MR，筛选出：
  1. 开启时间超过指定天数（默认 14 个工作日）
  2. 距离上次通知 ≥ 7 个工作日 或 尚未通知（通过 data/stale_mr_notified.json 去重）

升级机制：
  - 第 1 次通知：仅提醒开发者本人
  - 第 2 次通知（距上次 ≥7 个工作日 MR 仍 open）：提醒开发者并抄送管理员
  - 此后永久跳过

内/外判定基于 gitcode_2_mail.txt：
  - 有有效邮箱 → 发个人提醒邮件
  - 有记录但邮箱为 null → 管理员汇总报告（无邮箱）
  - 不在映射中 → 管理员汇总报告（外部开发者）

工作日计算使用 chinese_calendar，自动排除周末及中国法定节假日（含调休）。

用法：
    python stale_mr_notify.py --dry-run
    python stale_mr_notify.py
    python stale_mr_notify.py --stale-days 7
    python stale_mr_notify.py --report-to admin@huawei.com
    python stale_mr_notify.py --test someone@huawei.com
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
    print("  ⚠ chinese_calendar 未安装，仅排除周末（不排除法定节假日）")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MRS_DIR = DATA_DIR / "mrs"
REPOS_CONFIG_PATH = Path("config/repos.yml")
MAIL_MAP_PATH = Path("config/gitcode_2_mail.txt")
SMTP_CONFIG_PATH = Path("config/smtp_config.ini")
ADMIN_EMAIL_PATH = Path("config/admin_email.txt")
NOTIFIED_PATH = DATA_DIR / "stale_mr_notified.json"
STAFF_MAP_PATH = Path("config/gitcode_2_staff.txt")

DEFAULT_STALE_DAYS = 14
RESEND_INTERVAL_DAYS = 7
MAX_NOTIFY_COUNT = 2

CONTACT_INFO = "如有疑问请联系夏国正 x00806611"


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
    """返回 {username: email 或 None}。None 表示邮箱映射存在但两列均为 null。"""
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
    """author 在 mail_map 中且有有效邮箱。"""
    return mail_map.get(author) is not None


def _has_null_email(mail_map, author):
    """author 在 mail_map 中但两列邮箱均为 null。"""
    return author in mail_map and mail_map[author] is None


def load_admin_email():
    if not ADMIN_EMAIL_PATH.exists():
        return None
    text = ADMIN_EMAIL_PATH.read_text(encoding="utf-8").strip()
    if text:
        return text.splitlines()[0].strip()
    return None


def load_staff_map():
    """返回 {gitcode_id: (name, employee_id)}。"""
    staff = {}
    if not STAFF_MAP_PATH.exists():
        return staff
    for line in STAFF_MAP_PATH.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        uid, name, eid = parts[0], parts[1], parts[2]
        if uid:
            staff[uid] = (name, eid)
    return staff


def load_repo_admin_map():
    """返回 {repo_path: (admin_email, cc_email)}。"""
    admin_map = {}
    if not REPOS_CONFIG_PATH.exists():
        return admin_map
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    for repo in (config.get("repos") or []):
        path = repo.get("path", "")
        admin = repo.get("admin", "")
        cc = repo.get("cc", "")
        if path and admin:
            admin_map[path] = (admin, cc)
    return admin_map


def _author_display(author, staff_map, mail_map):
    """返回作者显示名：gitcode_id (姓名/工号) 或 gitcode_id (外部/无邮箱)。"""
    if author in staff_map:
        name, eid = staff_map[author]
        return f"{author} ({name}/{eid})"
    if author in mail_map:
        return f"{author} (有映射无邮箱)"
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


def _mr_key(repo_path, iid):
    safe = repo_path.replace("/", "__")
    return f"{safe}!{iid}"


def _working_days_between(start_date, end_date):
    """计算 start_date（含）到 end_date（含）之间的工作天数，排除周末和法定节假日。"""
    count = 0
    d = start_date
    while d <= end_date:
        if is_workday(d):
            count += 1
        d += timedelta(days=1)
    return count


def _working_days_since(date_str):
    """从 date_str 到今天的累计工作天数（含当日）。"""
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
        print(f"    请先运行: python stale_mr_notify.py --init-smtp")
        return None
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONFIG_PATH, encoding="utf-8")
    required_keys = ["server", "port", "username", "password"]
    for key in required_keys:
        if not cfg.get("smtp", key, fallback="").strip():
            print(f"  ✗ SMTP 配置缺少 [smtp] {key}")
            return None
    return cfg


def _check_mr_notify_status(key, notified, today):
    """返回 (should_notify, notify_stage, skip_reason)。
    notify_stage: 0=跳过, 1=首次通知, 2=二次升级通知
    skip_reason: ''=不跳过, 'waiting'=未到重发间隔, 'max'=已达上限
    """
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


def scan_stale_mrs(stale_days, notify_paths=None, notified=None):
    today = datetime.now()
    stale_by_author = defaultdict(list)
    stats = {
        "total_opened": 0, "stale_all": 0, "stale_matched": 0,
        "repos_scanned": 0, "skipped_waiting": 0, "skipped_max": 0,
        "stage1_count": 0, "stage2_count": 0,
    }

    if notified is None:
        notified = {}

    if not MRS_DIR.exists():
        print(f"  ✗ MR 数据目录不存在: {MRS_DIR}")
        return stale_by_author, stats

    for f in sorted(MRS_DIR.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if notify_paths is not None and repo_path not in notify_paths:
            continue
        mrs = json.loads(f.read_text(encoding="utf-8"))
        stats["repos_scanned"] += 1

        for mr in mrs:
            if mr.get("state") != "opened":
                continue
            if mr.get("draft"):
                continue
            stats["total_opened"] += 1

            iid = mr.get("iid")
            if iid is None:
                continue
            key = _mr_key(repo_path, iid)

            should_notify, stage, skip_reason = _check_mr_notify_status(key, notified, today.date())
            if not should_notify:
                if skip_reason == 'max':
                    stats["skipped_max"] += 1
                else:
                    stats["skipped_waiting"] += 1
                continue

            created_at = mr.get("created_at", "")
            if not created_at:
                continue
            days_open = mr.get("working_days_open") or _working_days_since(created_at)
            if days_open <= stale_days:
                continue
            stats["stale_all"] += 1
            stats["stale_matched"] += 1

            if stage == 2:
                stats["stage2_count"] += 1
            else:
                stats["stage1_count"] += 1

            author = mr.get("author", "")
            if not author:
                continue

            stale_by_author[author].append({
                "repo": repo_path,
                "iid": iid,
                "title": mr.get("title", ""),
                "created_at": created_at,
                "days_open": days_open,
                "web_url": mr.get("web_url", ""),
                "labels": mr.get("labels") or [],
                "notify_stage": stage,
            })

    return stale_by_author, stats


def _build_mr_table_rows(mrs):
    rows = ""
    for mr in sorted(mrs, key=lambda x: -x["days_open"]):
        labels_str = ", ".join(mr["labels"]) if mr["labels"] else "-"
        stage_note = " <span style='color:#e05f5f;font-size:11px'>(二次提醒)</span>" if mr.get("notify_stage") == 2 else ""
        rows += f"""<tr>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">{mr['repo']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">
    <a href="{mr['web_url']}" style="color:#2563eb;text-decoration:none">#{mr['iid']}</a>
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{mr['title']}{stage_note}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{mr['days_open']}天</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#666">{labels_str}</td>
</tr>"""
    return rows


def build_html_email(author, mrs, is_escalated=False):
    stage2_mrs = [m for m in mrs if m.get("notify_stage") == 2]
    rows = _build_mr_table_rows(mrs)
    escalation_note = ""
    if stage2_mrs:
        escalation_note = f"""
  <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:12px 16px;margin-bottom:16px">
    <strong style="color:#856404">⚠ 以下 {len(stage2_mrs)} 个 MR 已二次提醒，并抄送管理员跟进。</strong>
  </div>"""
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 MR 提醒</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    Hi {author}，您有 <strong style="color:#e05f5f">{len(mrs)}</strong> 个 MR 已开启超过 14 个工作日，请及时处理。
  </p>
  {escalation_note}
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">MR</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#999;font-size:11px;margin-top:16px">
    此邮件由 CANN Radar 自动发送，请检查 MR 状态后及时合并或关闭。
  </p>
  <p style="color:#999;font-size:11px">{CONTACT_INFO}</p>
</div>"""


def _build_author_mr_section(title, label_color, author_mrs_dict):
    if not author_mrs_dict:
        return ""
    total_mrs = sum(len(mrs) for mrs in author_mrs_dict.values())
    author_blocks = ""
    for author in sorted(author_mrs_dict.keys()):
        mrs = author_mrs_dict[author]
        rows = _build_mr_table_rows(mrs)
        author_blocks += f"""
  <h4 style="font-size:14px;margin:20px 0 8px;color:#1a1d2e">{author}（{len(mrs)} 个 MR）</h4>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden;margin-bottom:16px">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">MR</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    return f"""
  <h3 style="font-size:15px;margin-top:24px;color:{label_color};border-bottom:1px solid #e2e4ea;padding-bottom:6px">
    {title}（{len(author_mrs_dict)} 人，{total_mrs} 个 MR）
  </h3>{author_blocks}"""


def build_admin_report_html(stats, null_email_authors, external_authors, stale_days):
    null_section = _build_author_mr_section(
        "有映射但无邮箱", "#e05f5f", null_email_authors,
    )
    external_section = _build_author_mr_section(
        "外部开发者", "#f5a623", external_authors,
    )

    total_null = sum(len(mrs) for mrs in null_email_authors.values())
    total_external = sum(len(mrs) for mrs in external_authors.values())
    stage2_total = stats.get("stage2_count", 0)

    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 MR 管理员汇总报告</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    扫描条件：开启超过 <strong>{stale_days}</strong> 个工作日（距上次通知≥{RESEND_INTERVAL_DAYS}个工作日可重发，最多{MAX_NOTIFY_COUNT}次）
  </p>
  <table style="font-size:13px;border-collapse:collapse;margin-bottom:20px">
    <tr><td style="padding:4px 16px 4px 0;color:#666">扫描仓库</td><td><strong>{stats['repos_scanned']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">Opened MR 总数</td><td><strong>{stats['total_opened']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">超期 MR</td><td><strong>{stats['stale_matched']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">首次通知</td><td><strong>{stats.get('stage1_count', 0)}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">二次升级通知</td><td><strong style="color:#e05f5f">{stage2_total}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">未到重发间隔跳过</td><td><strong>{stats['skipped_waiting']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">已达上限永久跳过</td><td><strong>{stats['skipped_max']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">有映射无邮箱 MR 数</td><td><strong style="color:#e05f5f">{total_null}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">外部开发者 MR 数</td><td><strong style="color:#f5a623">{total_external}</strong></td></tr>
  </table>
  {null_section}
  {external_section}
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


def _send_personal_emails(has_email_authors, smtp_cfg, notified_data, admin_email, args):
    sent = 0
    failed = 0
    test_sent = False

    for author, (email, mrs) in sorted(has_email_authors.items(), key=lambda x: -len(x[1][1])):
        has_stage2 = any(m.get("notify_stage") == 2 for m in mrs)
        stage2_count = sum(1 for m in mrs if m.get("notify_stage") == 2)
        subject = f"[CANN] 您有 {len(mrs)} 个超期未关闭的 MR（需处理）"
        if has_stage2:
            subject += " [二次提醒]"
        html = build_html_email(author, mrs, is_escalated=has_stage2)

        cc = None
        if has_stage2 and admin_email and admin_email != email:
            cc = admin_email

        if args.dry_run:
            cc_str = f" 抄送:{cc}" if cc else ""
            stage_note = f" 其中{stage2_count}个二次提醒" if has_stage2 else ""
            print(f"  → {author} <{email}>{cc_str}: {len(mrs)} 个 MR{stage_note} [dry-run，未发送]")
        elif args.test:
            if not test_sent:
                try:
                    send_one_email(smtp_cfg, args.test, subject, html, cc_email=cc)
                    sent += 1
                    test_sent = True
                    stage_note = f" 含{stage2_count}个二次提醒" if has_stage2 else ""
                    print(f"  ✓ {author} <{email}> → {args.test}: {len(mrs)} 个 MR{stage_note} [测试样本，仅此1封]")
                    _mark_notified(notified_data, mrs)
                except Exception as e:
                    failed += 1
                    mr_ids = ", ".join(f"#{m['iid']}" for m in mrs)
                    print(f"  ✗ {author} <{email}>: {e}  MR: {mr_ids}")
            else:
                print(f"  ⊘ {author} <{email}>: {len(mrs)} 个 MR [测试模式，跳过]")
        else:
            try:
                send_one_email(smtp_cfg, email, subject, html, cc_email=cc)
                sent += 1
                _mark_notified(notified_data, mrs)
                cc_str = f"，抄送管理员" if cc else ""
                stage_note = f" 含{stage2_count}个二次提醒" if has_stage2 else ""
                print(f"  ✓ {author} <{email}>: {len(mrs)} 个 MR{stage_note}{cc_str}")
            except Exception as e:
                failed += 1
                mr_ids = ", ".join(f"#{m['iid']}" for m in mrs)
                print(f"  ✗ {author} <{email}>: {e}  MR: {mr_ids}")

    return sent, failed


def _mark_notified(notified_data, mrs):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for mr in mrs:
        key = _mr_key(mr["repo"], mr["iid"])
        if not key:
            continue
        existing = notified_data["notified"].get(key)
        new_count = (existing.get("count", 0) + 1) if existing else 1
        notified_data["notified"][key] = {"notified_at": now, "count": new_count}


def main():
    parser = argparse.ArgumentParser(description="超期 MR 扫描与邮件通知")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发送邮件")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help=f"超期工作日阈值（默认 {DEFAULT_STALE_DAYS}）")
    parser.add_argument("--report-to", help="管理员汇总报告发送到此邮箱（覆盖 config/admin_email.txt）")
    parser.add_argument("--test", metavar="EMAIL", help="测试模式：仅发送1封样本到指定邮箱，不发给实际作者")
    parser.add_argument("--init-smtp", action="store_true", help="生成 SMTP 配置模板到 config/smtp_config.ini")
    args = parser.parse_args()

    if args.init_smtp:
        init_smtp_config()
        return 0

    print(f"=== 超期 MR 扫描 ===")
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
    print(f"  已追踪 MR: {len(notified_data.get('notified', {}))} 个")

    admin_email = args.report_to or load_admin_email()
    if admin_email:
        print(f"  管理员邮箱: {admin_email}")
    else:
        print(f"  管理员邮箱: 未配置（将不发送汇总报告）")

    stale_by_author, stats = scan_stale_mrs(
        args.stale_days, notify_paths, notified_data.get("notified", {}),
    )
    print(f"\n  扫描结果:")
    print(f"    仓库: {stats['repos_scanned']}")
    print(f"    Opened MR: {stats['total_opened']}")
    print(f"    超期 MR: {stats['stale_matched']}")
    print(f"    首次通知: {stats['stage1_count']}")
    print(f"    二次升级: {stats['stage2_count']}")
    print(f"    未到重发间隔跳过: {stats['skipped_waiting']}")
    print(f"    已达上限永久跳过: {stats['skipped_max']}")
    print(f"    涉及作者: {len(stale_by_author)}")

    if not stale_by_author:
        print("\n  ✓ 无新增/待升级超期 MR，无需通知")
        return 0

    # 新分类：有邮箱 / null邮箱 / 不在映射中
    has_email_authors = {}
    null_email_authors = {}
    external_authors = {}

    for author, mrs in stale_by_author.items():
        if _has_valid_email(mail_map, author):
            has_email_authors[author] = (mail_map[author], mrs)
        elif _has_null_email(mail_map, author):
            null_email_authors[author] = mrs
        else:
            external_authors[author] = mrs

    print(f"\n  分类结果:")
    print(f"    有邮箱: {len(has_email_authors)} 人（{sum(len(v[1]) for v in has_email_authors.values())} 个 MR）")
    print(f"    有映射无邮箱: {len(null_email_authors)} 人（{sum(len(v) for v in null_email_authors.values())} 个 MR）")
    print(f"    外部开发者: {len(external_authors)} 人（{sum(len(v) for v in external_authors.values())} 个 MR）")

    if not admin_email:
        if null_email_authors or external_authors:
            print(f"\n  ⚠ 管理员邮箱未配置，以下用户无法收到通知：")
            for a in sorted(null_email_authors.keys()):
                ids = ", ".join(f"#{m['iid']}" for m in null_email_authors[a])
                print(f"    有映射无邮箱: {a} ({ids})")
            for a in sorted(external_authors.keys()):
                ids = ", ".join(f"#{m['iid']}" for m in external_authors[a])
                print(f"    外部开发者: {a} ({ids})")

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg:
            print("\n  ✗ SMTP 配置不可用，请使用 --dry-run 测试或先配置 SMTP")
            return 1

    notified_changed = False

    # 发送个人通知（有邮箱者）
    print(f"\n=== 发送个人通知 ===")
    if not has_email_authors:
        print("  （无有邮箱的开发者，跳过个人通知）")
    else:
        sent, failed = _send_personal_emails(
            has_email_authors, smtp_cfg, notified_data, admin_email, args,
        )
    if sent > 0 and not args.dry_run:
        notified_changed = True

    # 保存 MR 汇总数据供 admin_summary.py 读取
    if stale_by_author and not args.dry_run:
        summary_mrs = []
        for author, (email, mrs) in has_email_authors.items():
            for mr in mrs:
                summary_mrs.append({"author": author, "category": "有邮箱", **{k: mr[k] for k in ["repo","iid","title","days_open","web_url","notify_stage"]}})
        for author, mrs_list in null_email_authors.items():
            for mr in mrs_list:
                summary_mrs.append({"author": author, "category": "无邮箱", **{k: mr[k] for k in ["repo","iid","title","days_open","web_url","notify_stage"]}})
        for author, mrs_list in external_authors.items():
            for mr in mrs_list:
                summary_mrs.append({"author": author, "category": "外部", **{k: mr[k] for k in ["repo","iid","title","days_open","web_url","notify_stage"]}})
        with open(DATA_DIR / "admin_mr_summary.json", "w", encoding="utf-8") as f:
            json.dump({"mr_items": summary_mrs, "stats": stats, "stale_days": args.stale_days}, f, ensure_ascii=False)
        if not args.dry_run:
            print(f"\n  个人通知: 已发送 {sent}, 失败 {failed}")

    # 保存 tracking 文件
    if notified_changed and not args.dry_run:
        save_notified(notified_data)
        print(f"\n  ✓ 已更新通知记录: {NOTIFIED_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
