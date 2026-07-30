#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""admin_summary.py — 合并 MR + Issue 超期数据，按管理员发送汇总邮件。"""

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

CONTACT_INFO = "如有疑问请联系夏国正 x00806611"
MR_SUMMARY_FILE = DATA_DIR / "admin_mr_summary.json"
ISSUE_SUMMARY_FILE = DATA_DIR / "admin_issue_summary.json"


def load_mail_map():
    mapping = {}
    if not MAIL_MAP_PATH.exists(): return mapping
    for line in MAIL_MAP_PATH.read_text(encoding="utf-8").splitlines():
        p = line.strip().split("\t")
        if len(p) < 3: continue
        u, c2, c3 = p[0], p[1], p[2]
        if not u: continue
        e = c2 if c2 and c2 != "null" else c3
        mapping[u] = e if e and e != "null" else None
    return mapping


def load_repo_admin_map():
    m = {}
    if not REPOS_CONFIG_PATH.exists(): return m
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        c = yaml.safe_load(f) or {}
    for r in (c.get("repos") or []):
        p, a = r.get("path", ""), r.get("admin", "")
        if p and a: m[p] = a
    return m


def _author_display(author, mail_map):
    if author in mail_map:
        return author if mail_map[author] else f"{author} (无邮箱映射)"
    return f"{author} (外部)"


def load_smtp_config():
    if not SMTP_CONFIG_PATH.exists(): return None
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONFIG_PATH, encoding="utf-8")
    for k in ["server", "port", "username", "password"]:
        if not cfg.get("smtp", k, fallback="").strip(): return None
    return cfg


def send_one_email(cfg, to_email, subject, html_body, cc_email=None):
    srv, port = cfg.get("smtp", "server").strip(), int(cfg.get("smtp", "port").strip())
    uname, pwd = cfg.get("smtp", "username").strip(), cfg.get("smtp", "password").strip()
    sender = cfg.get("mail", "from", fallback=uname).strip()

    recipients = [e.strip() for e in to_email.split(",") if e.strip()]
    if cc_email: recipients += [e.strip() for e in cc_email.split(",") if e.strip()]

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("CANN Radar", "utf-8")), sender))
    msg["To"] = to_email
    if cc_email: msg["Cc"] = cc_email
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(srv, port, timeout=30) as smtp:
        smtp.login(uname, pwd)
        smtp.sendmail(sender, recipients, msg.as_string())


def _build_table(items, mail_map, by_field, display_fn, is_mr=True):
    grouped = defaultdict(list)
    for item in items:
        key = item.get(by_field, item.get("author", "?"))
        grouped[key].append(item)

    rows = ""
    new_count = waiting_count = 0
    for key in sorted(grouped, key=lambda k: -len(grouped[k])):
        sub = sorted(grouped[key], key=lambda x: -x["days_open"])
        cnt = len(sub)
        disp = display_fn(key, mail_map) if callable(display_fn) else key
        for idx, item in enumerate(sub):
            st = item.get("status", "new")
            if st == "new": new_count += 1; label = "新增"
            elif st == "daily": label = "持续提醒中"
            else: waiting_count += 1; label = "跟踪中"
            cols = f"<td rowspan='{cnt}'>{disp}</td><td rowspan='{cnt}' style='text-align:center'>{cnt}个</td>" if idx == 0 else ""
            rows += f"<tr>{cols}<td>{item['title'][:50]}</td><td><a href='{item['web_url']}'>#{item['iid']}</a></td><td>{item['days_open']}天</td><td>{label}</td></tr>"
    return rows, new_count, waiting_count


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test", metavar="EMAIL")
    args = p.parse_args()

    mail_map = load_mail_map()
    repo_admin_map = load_repo_admin_map()

    mr_data, issue_data = {}, {}
    if MR_SUMMARY_FILE.exists(): mr_data = json.loads(MR_SUMMARY_FILE.read_text(encoding="utf-8"))
    if ISSUE_SUMMARY_FILE.exists(): issue_data = json.loads(ISSUE_SUMMARY_FILE.read_text(encoding="utf-8"))
    mr_items = mr_data.get("mr_items", [])
    issue_items = issue_data.get("issue_items", [])

    if not mr_items and not issue_items:
        print("✓ 无超期 MR / Issue")
        return 0

    repo_data = defaultdict(lambda: {"mr": [], "issue": []})
    for item in mr_items: repo_data[item["repo"]]["mr"].append(item)
    for item in issue_items: repo_data[item["repo"]]["issue"].append(item)

    admin_repos = defaultdict(list)
    for repo in sorted(repo_data.keys()):
        ads = repo_admin_map.get(repo, "")
        for a in [x.strip() for x in ads.split(",") if x.strip()]:
            admin_repos[a].append(repo)

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg: return print("✗ SMTP 不可用"), 1

    for admin_addr in sorted(admin_repos.keys()):
        repos = admin_repos[admin_addr]
        all_sections = ""
        total_mr_all = total_iss_all = 0

        for repo in repos:
            items = repo_data[repo]
            total_mr = len(items["mr"])
            total_iss = len(items["issue"])
            total_mr_all += total_mr
            total_iss_all += total_iss
            if total_mr == 0 and total_iss == 0: continue

            repo_section = f"<h3 style='margin-top:24px;border-bottom:1px solid #e2e4ea;padding-bottom:6px'>{repo} — MR {total_mr} 个 + Issue {total_iss} 个</h3>"

            if total_mr > 0:
                mr_rows, mr_new, mr_waiting = _build_table(items["mr"], mail_map, "author", _author_display)
                repo_section += f"""<h4>超期 MR<span style="font-size:12px;font-weight:400;color:#666"> — 新增 {mr_new} 个，跟踪中 {mr_waiting} 个</span></h4>
<table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;margin-bottom:16px">
<thead><tr style="background:#f0f2f5">
<th style="padding:8px 10px;text-align:left">提交人</th><th style="padding:8px 10px;text-align:center">数量</th>
<th style="padding:8px 10px;text-align:left">标题</th><th style="padding:8px 10px;text-align:left">链接</th>
<th style="padding:8px 10px;text-align:center">工作天数</th><th style="padding:8px 10px;text-align:center">状态</th>
</tr></thead><tbody>{mr_rows}</tbody></table>"""

            if total_iss > 0:
                iss_rows, iss_new, iss_waiting = _build_table(
                    items["issue"], mail_map, "assignee_display",
                    lambda k, m: _author_display(k, m) if k != "(未分配)" else "(未分配负责人)")
                repo_section += f"""<h4>超期 Issue<span style="font-size:12px;font-weight:400;color:#666"> — 新增 {iss_new} 个，跟踪中 {iss_waiting} 个</span></h4>
<table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;margin-bottom:16px">
<thead><tr style="background:#f0f2f5">
<th style="padding:8px 10px;text-align:left">负责人</th><th style="padding:8px 10px;text-align:center">数量</th>
<th style="padding:8px 10px;text-align:left">标题</th><th style="padding:8px 10px;text-align:left">链接</th>
<th style="padding:8px 10px;text-align:center">工作天数</th><th style="padding:8px 10px;text-align:center">状态</th>
</tr></thead><tbody>{iss_rows}</tbody></table>"""

            all_sections += repo_section

        if not all_sections: continue

        repo_list = "、".join(repos)
        html = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:800px">
<h2>超期汇总报告</h2>
<p style="color:#666;font-size:13px">日期: {datetime.now().strftime('%Y-%m-%d')} | 仓库: {repo_list} | 共 MR {total_mr_all} + Issue {total_iss_all}</p>
{all_sections}
<p style="color:#999;font-size:11px;margin-top:16px">CANN Radar 自动生成 · {CONTACT_INFO}</p></div>"""

        if args.dry_run:
            print(f"  → {admin_addr}: {len(repos)} 仓 (MR {total_mr_all} + Issue {total_iss_all}) [dry-run]")
        elif args.test:
            try:
                send_one_email(smtp_cfg, args.test, f"[CANN] 超期汇总 - {repo_list}（MR {total_mr_all} + Issue {total_iss_all}）", html)
                print(f"  ✓ {args.test}: {len(repos)} 仓 → MR {total_mr_all} + Issue {total_iss_all}")
            except Exception as e:
                print(f"  ✗ {args.test}: {e}")
        else:
            try:
                send_one_email(smtp_cfg, admin_addr, f"[CANN] 超期汇总 - {repo_list}（MR {total_mr_all} + Issue {total_iss_all}）", html)
                print(f"  ✓ {admin_addr}: {len(repos)} 仓 → MR {total_mr_all} + Issue {total_iss_all}")
            except Exception as e:
                print(f"  ✗ {admin_addr}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
