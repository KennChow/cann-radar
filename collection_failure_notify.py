#!/usr/bin/env python3
"""发送 CANN Radar 数据采集降级告警。"""

import argparse
import html
import json
import sys

from admin_summary import load_smtp_config, send_one_email
from collector import COLLECTION_FAILURES_PATH

DEFAULT_RECIPIENT = "caoqiancheng@huawei.com"


def load_failures():
    if not COLLECTION_FAILURES_PATH.exists():
        return []
    try:
        data = json.loads(COLLECTION_FAILURES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def build_html(failures):
    rows = []
    for item in failures:
        fallback = "已使用上一版完整数据" if item.get("fallback_used") else "无可用回退数据"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('repo') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('category') or '-'))}</td>"
            f"<td>{html.escape(fallback)}</td>"
            f"<td>{html.escape(str(item.get('error') or '-'))}</td>"
            "</tr>"
        )
    return f"""<div style="font-family:sans-serif;max-width:900px">
<h2>CANN Radar 数据采集异常</h2>
<p>本轮有 {len(failures)} 项采集失败。成功仓库继续使用新数据；失败仓库按下表降级。</p>
<table style="border-collapse:collapse;width:100%" border="1" cellpadding="8">
<thead><tr><th>仓库</th><th>数据类型</th><th>处理</th><th>错误</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", default=DEFAULT_RECIPIENT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    failures = load_failures()
    if not failures:
        print("✓ 本轮无采集失败，无需告警")
        return 0
    if args.dry_run:
        print(f"→ {args.to}: {len(failures)} 项采集失败 [dry-run]")
        return 0
    cfg = load_smtp_config()
    if not cfg:
        print("✗ SMTP 配置不可用，无法发送采集失败告警")
        return 1
    send_one_email(
        cfg, args.to, f"[CANN Radar] 数据采集异常（{len(failures)} 项）",
        build_html(failures),
    )
    print(f"✓ 已发送采集失败告警至 {args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
