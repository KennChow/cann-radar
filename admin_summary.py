#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""admin_summary.py — 合并 MR + Issue 超期数据，按仓库发送管理员汇总邮件。

读取 data/admin_mr_summary.json 和 data/admin_issue_summary.json（由 stale_mr_notify.py / stale_issue_notify.py 生成），
按 repos.yml 中的 admin/cc 配置，每个仓库发送一封合并邮件。

用法：
    python admin_summary.py --dry-run
    python admin_summary.py
    python admin_summary.py --test someone@huawei.com
"""

import argparse
import configparser
import json
import smtplib
import sys
from collections import defaultdict
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPOS_CONFIG_PATH = Path("config/repos.yml")
MAIL_MAP_PATH = Path("config/gitcode_2_mail.txt")
SMTP_CONFIG_PATH = Path("config/smtp_config.ini")
STAFF_MAP_PATH = Path("config/gitcode_2_staff.txt")

CONTACT_INFO = "如有疑问请联系夏国正 x00806611"
MR_SUMMARY_FILE = DATA_DIR / "admin_mr_summary.json"
ISSUE_SUMMARY_FILE = DATA_DIR / "admin_issue_summary.json"


def load_staff_map():
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


def load_mail_map():
    mapping = {}
    if not MAIL_MAP_PATH.exists():
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


def load_repo_admin_map():
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
    if author in staff_map:
        name, eid = staff_map[author]
        return f"{author} ({name}/{eid})"
    if author in mail_map:
        if mail_map[author]:
            return author
        return f"{author} (无邮箱映射)"
    return f"{author} (外部)"


def load_smtp_config():
    if not SMTP_CONFIG_PATH.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONFIG_PATH, encoding="utf-8")
    required_keys = ["server", "port", "username", "password"]
    for key in required_keys:
        if not cfg.get("smtp", key, fallback="").strip():
            return None
    return cfg


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


def main():
    parser = argparse.ArgumentParser(description="合并 MR + Issue 超期数据，发送管理员汇总")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", metavar="EMAIL", help="测试模式：发送到指定邮箱")
    args = parser.parse_args()

    mail_map = load_mail_map()
    staff_map = load_staff_map()
    repo_admin_map = load_repo_admin_map()

    mr_data = {}
    issue_data = {}
    if MR_SUMMARY_FILE.exists():
        mr_data = json.loads(MR_SUMMARY_FILE.read_text(encoding="utf-8"))
    if ISSUE_SUMMARY_FILE.exists():
        issue_data = json.loads(ISSUE_SUMMARY_FILE.read_text(encoding="utf-8"))

    mr_items = mr_data.get("mr_items", [])
    issue_items = issue_data.get("issue_items", [])

    if not mr_items and not issue_items:
        print("✓ 无超期 MR / Issue，无需发送汇总")
        return 0

    # 按仓库分组
    repo_data = defaultdict(lambda: {"mr": [], "issue": []})
    for item in mr_items:
        repo_data[item["repo"]]["mr"].append(item)
    for item in issue_items:
        repo_data[item["repo"]]["issue"].append(item)

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg:
            print("✗ SMTP 配置不可用")
            return 1

    for repo in sorted(repo_data.keys()):
        items = repo_data[repo]
        admin_cc = repo_admin_map.get(repo, ("", ""))
        admin_addr, cc_addr = admin_cc
        if not admin_addr:
            continue

        total_mr = len(items["mr"])
        total_iss = len(items["issue"])

        # MR 表格
        mr_rows = ""
        for item in sorted(items["mr"], key=lambda x: -x["days_open"]):
            display = _author_display(item["author"], staff_map, mail_map)
            mr_rows += f"<tr><td>{display}</td><td>{item['title'][:50]}</td><td><a href='{item['web_url']}'>#{item['iid']}</a></td><td>{item['days_open']}天</td></tr>"

        # Issue 表格
        iss_rows = ""
        for item in sorted(items["issue"], key=lambda x: -x["days_open"]):
            display = _author_display(item.get("assignee_display", item.get("author", "")), staff_map, mail_map)
            iss_rows += f"<tr><td>{item['title'][:50]}</td><td><a href='{item['web_url']}'>#{item['iid']}</a></td><td>{item['days_open']}天</td><td>{display}</td></tr>"

        mr_section = ""
        if total_mr > 0:
            mr_section = f"""<h3>超期 MR（{total_mr} 个）</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;margin-bottom:20px">
<thead><tr style="background:#f0f2f5">
<th style="padding:8px 10px;text-align:left">提交人</th><th style="padding:8px 10px;text-align:left">标题</th>
<th style="padding:8px 10px;text-align:left">链接</th><th style="padding:8px 10px;text-align:center">时长</th>
</tr></thead><tbody>{mr_rows}</tbody></table>"""

        iss_section = ""
        if total_iss > 0:
            iss_section = f"""<h3>超期 Issue（{total_iss} 个）</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea">
<thead><tr style="background:#f0f2f5">
<th style="padding:8px 10px;text-align:left">标题</th><th style="padding:8px 10px;text-align:left">链接</th>
<th style="padding:8px 10px;text-align:center">时长</th><th style="padding:8px 10px;text-align:left">负责人</th>
</tr></thead><tbody>{iss_rows}</tbody></table>"""

        html = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:800px">
<h2>超期汇总报告 - {repo}</h2>
<p style="color:#666;font-size:13px">日期: {datetime.now().strftime('%Y-%m-%d')} | MR {total_mr} 个 + Issue {total_iss} 个</p>
{mr_section}{iss_section}
<p style="color:#999;font-size:11px;margin-top:16px">CANN Radar 自动生成 · {CONTACT_INFO}</p></div>"""

        if args.dry_run:
            cc_str = f" CC:{cc_addr}" if cc_addr else ""
            print(f"  → {repo} → {admin_addr}{cc_str}: MR {total_mr} + Issue {total_iss} [dry-run]")
        elif args.test:
            try:
                send_one_email(smtp_cfg, args.test, f"[CANN] 超期汇总 - {repo}（MR {total_mr} + Issue {total_iss}）", html, cc_email=cc_addr if cc_addr else None)
                print(f"  ✓ {repo} → {args.test} (CC: {cc_addr if cc_addr else '无'}): MR {total_mr} + Issue {total_iss}")
            except Exception as e:
                print(f"  ✗ {repo} → {args.test}: {e}")
        else:
            try:
                send_one_email(smtp_cfg, admin_addr, f"[CANN] 超期汇总 - {repo}（MR {total_mr} + Issue {total_iss}）", html, cc_email=cc_addr if cc_addr else None)
                print(f"  ✓ {repo} → {admin_addr}" + (f" (CC: {cc_addr})" if cc_addr else "") + f": MR {total_mr} + Issue {total_iss}")
            except Exception as e:
                print(f"  ✗ {repo} → {admin_addr}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
