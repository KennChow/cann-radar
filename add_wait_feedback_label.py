#!/usr/bin/env python3
"""add_wait_feedback_label.py — 对有回复的 issue 自动打 wait-feedback 标签。

仅处理 repos.yml 中 wait_feedback: true 的仓库。
调 comments API 取最后一条评论，若作者 ≠ issue 作者则追加标签。
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ISSUES_DIR = DATA_DIR / "issues"
REPOS_CONFIG_PATH = Path("config/repos.yml")
TOKEN_PATH = Path("config/gitcode_token_rw.txt")
V5_BASE = "https://gitcode.com/api/v5"
LABEL_NAME = "wait-feedback"
REQUEST_DELAY = 0.3


def load_token():
    if not TOKEN_PATH.exists():
        return None
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def load_wait_feedback_repos():
    paths = set()
    if not REPOS_CONFIG_PATH.exists():
        return paths
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    for r in (config.get("repos") or []):
        if r.get("enabled", True) and r.get("wait_feedback"):
            paths.add(r["path"])
    return paths


def _is_requirement(issue_type, title, labels):
    if issue_type == "需求":
        return True
    title_lower = (title or "").lower()
    if any(kw in title_lower for kw in ["requirement", "feature", "[rfc]"]):
        return True
    if any(kw in (l.lower() for l in (labels or [])) for kw in ["requirement", "feature"]):
        return True
    return False


def api_get(url, token):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
               "Referer": "https://gitcode.com/", "token": token}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def api_patch_labels(owner, repo, number, labels_str, token):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
               "Referer": "https://gitcode.com/", "Private-Token": token,
               "Content-Type": "application/json"}
    body = json.dumps({"labels": labels_str}).encode("utf-8")
    req = urllib.request.Request(
        f"{V5_BASE}/repos/{owner}/{repo}/issues/{number}",
        data=body, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="对有回复的 issue 添加 wait-feedback 标签")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = load_token()
    if not token:
        print("✗ 缺少 gitcode_token_rw.txt（写权限 token）")
        return 1 if not args.dry_run else 0

    repos = load_wait_feedback_repos()
    if not repos:
        print("✗ 无仓库配置 wait_feedback: true")
        return 1
    print(f"目标仓库: {', '.join(sorted(repos))}")
    print(f"模式: {'dry-run' if args.dry_run else '正式'}")

    stats = {"scanned": 0, "replied": 0, "labeled": 0, "skipped_has": 0, "skipped_self": 0, "api_err": 0}

    for f in sorted(ISSUES_DIR.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if repo_path not in repos:
            continue
        owner, repo = repo_path.split("/", 1)

        for iss in json.loads(f.read_text(encoding="utf-8")):
            if iss.get("state") != "opened":
                continue
            itype = iss.get("issue_type") or ""
            title = iss.get("title") or ""
            labels = iss.get("labels") or []
            if _is_requirement(itype, title, labels):
                continue
            stats["scanned"] += 1

            if LABEL_NAME in labels:
                stats["skipped_has"] += 1
                continue

            iid = iss.get("iid")
            if iid is None:
                continue

            comments = api_get(f"{V5_BASE}/repos/{owner}/{repo}/issues/{iid}/comments", token) or []
            if not comments:
                continue

            last_author = (comments[-1].get("user") or {}).get("login", "")
            if last_author == iss.get("author", ""):
                stats["skipped_self"] += 1
                continue
            stats["replied"] += 1

            existing = ",".join(labels)
            new_labels = f"{existing},{LABEL_NAME}" if existing else LABEL_NAME

            if args.dry_run:
                print(f"  → {repo_path} #{iid}: {last_author} ≠ {iss['author']} → +{LABEL_NAME}")
                stats["labeled"] += 1
            else:
                if api_patch_labels(owner, repo, iid, new_labels, token):
                    stats["labeled"] += 1
                    print(f"  ✓ {repo_path} #{iid}: +{LABEL_NAME}")
                else:
                    stats["api_err"] += 1
                    print(f"  ✗ {repo_path} #{iid}: API错误")
            time.sleep(REQUEST_DELAY)

    print(f"\n=== 汇总 ===")
    print(f"  扫描: {stats['scanned']} | 有人回复: {stats['replied']} | 已打标签: {stats['labeled']}")
    print(f"  跳过(已有标签): {stats['skipped_has']} | 跳过(自回复): {stats['skipped_self']} | 错误: {stats['api_err']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
