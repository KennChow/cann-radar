"""
CANN GitCode 数据采集器
采集 gitcode.com/cann 组织下所有仓库的统计数据，以及每个仓库的 star 用户画像。

用法:
    python collector.py repos          # 采集所有仓库基本信息
    python collector.py stars          # 采集所有仓库的 star 用户列表
    python collector.py users          # 采集所有 star 用户的画像数据
    python collector.py all            # 顺序执行以上三步
    python collector.py report         # 生成分析报告（需先完成采集）
"""

import json
import time
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from pathlib import Path

# 修复 Windows GBK 控制台编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ─── 配置 ────────────────────────────────────────────────────────────────────

BASE_URL = "https://web-api.gitcode.com"
ORG = "cann"
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://gitcode.com/",
    "Origin": "https://gitcode.com",
}

# 请求间隔（秒），避免触发限流
REQUEST_DELAY = 0.3
# 用户信息请求间隔（较慢，避免频繁）
USER_REQUEST_DELAY = 0.2

# ─── HTTP 工具 ────────────────────────────────────────────────────────────────

def get(url, retries=3, delay=REQUEST_DELAY):
    """发送 GET 请求，返回解析后的 JSON 或 None。"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # 资源不存在，不重试
            print(f"  HTTP {e.code}: {url}")
            if e.code == 429:
                time.sleep(10)
            elif attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                return None
        except Exception as e:
            print(f"  Error ({attempt+1}/{retries}): {e} - {url}")
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return None
    return None


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── 步骤 1：采集仓库列表及详情 ───────────────────────────────────────────────

def collect_repos():
    """
    获取 cann 组织下所有仓库，并逐个请求详情（含 star/fork/issue 数）。
    结果保存到 data/repos.json。
    """
    print("\n=== 步骤 1：采集仓库列表及详情 ===")

    # 先获取全量列表（最多 100 条/页，总共不超过 200 条）
    all_repos = []
    page = 1
    while True:
        url = f"{BASE_URL}/api/v1/groups/{ORG}/projects?page={page}&per_page=50"
        data = get(url)
        if not data or not data.get("content"):
            break
        all_repos.extend(data["content"])
        total = data.get("total", 0)
        print(f"  已获取 {len(all_repos)}/{total} 个仓库 (第{page}页)")
        if len(all_repos) >= total:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    print(f"  共 {len(all_repos)} 个仓库，开始获取详情...")

    repos_detail = []
    for i, repo in enumerate(all_repos):
        path = repo["path_with_namespace"]  # e.g. "cann/manifest"
        encoded = urllib.parse.quote(path, safe="")
        url = f"{BASE_URL}/api/v1/projects/{encoded}"
        detail = get(url)
        if detail and "id" in detail:
            repos_detail.append({
                "id": detail["id"],
                "name": detail.get("name", ""),
                "path": detail.get("path_with_namespace", path),
                "description": detail.get("description", ""),
                "star_count": detail.get("star_count") or 0,
                "forks_count": detail.get("forks_count") or 0,
                "watch_count": detail.get("watch_count") or 0,
                "open_issues_count": detail.get("open_issues_count") or 0,
                "open_mr_count": detail.get("open_merge_requests_count") or 0,
                "release_count": detail.get("release_count") or 0,
                "created_at": detail.get("created_at", ""),
                "updated_at": detail.get("updated_at", ""),
                "last_activity_at": detail.get("last_activity_at", ""),
                "default_branch": detail.get("default_branch", ""),
                "language": detail.get("main_repository_language", [None])[0] if detail.get("main_repository_language") else None,
                "visibility": detail.get("visibility", ""),
            })
            print(f"  [{i+1}/{len(all_repos)}] {path}: star={repos_detail[-1]['star_count']} fork={repos_detail[-1]['forks_count']} issue={repos_detail[-1]['open_issues_count']}")
        else:
            print(f"  [{i+1}/{len(all_repos)}] {path}: 获取失败")
        time.sleep(REQUEST_DELAY)

    repos_detail.sort(key=lambda r: r["star_count"], reverse=True)
    save_json(DATA_DIR / "repos.json", repos_detail)
    print(f"\n  ✓ 已保存 {len(repos_detail)} 个仓库到 data/repos.json")
    return repos_detail


# ─── 步骤 2：采集 star 用户列表 ──────────────────────────────────────────────

def collect_stars():
    """
    为每个有 star 的仓库获取完整 star 用户列表。
    结果保存到 data/stars/{repo_path}.json，汇总到 data/all_star_users.json。
    """
    print("\n=== 步骤 2：采集 star 用户列表 ===")

    repos = load_json(DATA_DIR / "repos.json")
    if not repos:
        print("  请先运行 python collector.py repos")
        return

    stars_dir = DATA_DIR / "stars"
    stars_dir.mkdir(exist_ok=True)

    # user_name -> set of repo paths (该用户 star 了哪些仓库)
    user_stars_map = {}

    for repo in repos:
        if repo["star_count"] == 0:
            print(f"  跳过 {repo['path']}（star=0）")
            continue

        repo_id = repo["id"]
        repo_path = repo["path"]
        safe_name = repo_path.replace("/", "__")
        cache_file = stars_dir / f"{safe_name}.json"

        # 若已缓存则跳过
        cached = load_json(cache_file)
        if cached is not None:
            users = cached
            print(f"  {repo_path}: 使用缓存 ({len(users)} 用户)")
        else:
            users = []
            page = 1
            per_page = 100
            while True:
                url = f"{BASE_URL}/api/v2/projects/{repo_id}/star_users?page={page}&per_page={per_page}"
                data = get(url)
                if not data or not data.get("content"):
                    break
                users.extend(data["content"])
                total = data.get("total", 0)
                if len(users) >= total:
                    break
                page += 1
                time.sleep(REQUEST_DELAY)

            save_json(cache_file, users)
            print(f"  {repo_path}: ⭐{repo['star_count']} 实际获取 {len(users)} 用户")
            time.sleep(REQUEST_DELAY)

        for u in users:
            uname = u.get("user_name", "")
            if uname:
                if uname not in user_stars_map:
                    user_stars_map[uname] = {
                        "user_name": uname,
                        "nick_name": u.get("nick_name", ""),
                        "user_id": u.get("user_id"),
                        "avatar": u.get("avatar", ""),
                        "starred_repos": [],
                    }
                user_stars_map[uname]["starred_repos"].append(repo_path)

    # 保存汇总
    all_users = list(user_stars_map.values())
    save_json(DATA_DIR / "all_star_users.json", all_users)
    print(f"\n  ✓ 共 {len(all_users)} 位唯一 star 用户，已保存到 data/all_star_users.json")
    return all_users


# ─── 步骤 3：采集用户画像 ─────────────────────────────────────────────────────

def classify_user(profile):
    """
    判断用户类型。

    - developer：有创建的原创仓库（非纯 fork）或有明显贡献活动
    - casual：有一定关注/被关注，有少量活动
    - ghost：三无用户——无粉丝、无原创仓库、无任何贡献活动
    """
    fans = profile.get("fans_count", 0)
    follows = profile.get("follow_count", 0)
    original_repos = profile.get("original_repo_count", 0)
    total_contributions = profile.get("total_contributions", 0)
    star_repo_count = len(profile.get("starred_repos", []))

    if original_repos >= 2 or total_contributions >= 10 or fans >= 5:
        return "developer"
    elif original_repos >= 1 or total_contributions >= 1 or fans >= 1 or follows >= 3:
        return "casual"
    else:
        return "ghost"  # 三无用户


def collect_users():
    """
    为每位 star 用户获取画像（粉丝数、创建仓库数、贡献活动）。
    结果保存到 data/user_profiles.json。
    """
    print("\n=== 步骤 3：采集用户画像 ===")

    all_users = load_json(DATA_DIR / "all_star_users.json")
    if not all_users:
        print("  请先运行 python collector.py stars")
        return

    profiles_file = DATA_DIR / "user_profiles.json"
    # 加载已有进度
    existing = load_json(profiles_file) or []
    done_users = {p["user_name"] for p in existing}
    print(f"  已有 {len(done_users)} 位用户画像，待采集 {len(all_users) - len(done_users)} 位")

    profiles = list(existing)

    for i, user in enumerate(all_users):
        uname = user["user_name"]
        if uname in done_users:
            continue

        profile = {
            "user_name": uname,
            "nick_name": user.get("nick_name", ""),
            "user_id": user.get("user_id"),
            "starred_repos": user.get("starred_repos", []),
            "fans_count": 0,
            "follow_count": 0,
            "original_repo_count": 0,
            "total_repo_count": 0,
            "total_contributions": 0,
            "user_type": "ghost",
        }

        # 1. 关注/粉丝数
        url = f"{BASE_URL}/api/v1/follow/userBaseInfo?username={uname}"
        data = get(url)
        if data and "fans_count" in data:
            profile["fans_count"] = data.get("fans_count", 0)
            profile["follow_count"] = data.get("follow_count", 0)
        time.sleep(USER_REQUEST_DELAY)

        # 2. 创建的仓库（第一页，只看 total 和是否有非 fork 仓库）
        url = f"{BASE_URL}/api/v1/profile/{uname}/created_projects?page=1&per_page=20"
        data = get(url)
        if data and "content" in data:
            total_repos = data.get("total") or 0
            profile["total_repo_count"] = total_repos
            original_count = sum(
                1 for r in data.get("content", [])
                if not r.get("forked_from_project")
            )
            profile["original_repo_count"] = original_count
            if total_repos > 20 and original_count == 0:
                profile["original_repo_count"] = max(0, total_repos - 20)
        time.sleep(USER_REQUEST_DELAY)

        # 3. 贡献活动只在 fans=0 且 repos=0 时才需要（快速判断三无）
        if profile["fans_count"] == 0 and profile["original_repo_count"] == 0:
            url = f"{BASE_URL}/uc/api/v1/events/{uname}/contributions"
            data = get(url)
            if data and isinstance(data, dict) and "error_code" not in data:
                profile["total_contributions"] = sum(v for v in data.values() if isinstance(v, int))
            time.sleep(USER_REQUEST_DELAY)

        profile["user_type"] = classify_user(profile)
        profiles.append(profile)
        done_users.add(uname)

        if (i + 1) % 50 == 0 or i == len(all_users) - 1:
            save_json(profiles_file, profiles)
        if (i + 1) % 10 == 0 or i == len(all_users) - 1:
            print(f"  [{i+1}/{len(all_users)}] {uname}: fans={profile['fans_count']} repos={profile['original_repo_count']} contribs={profile['total_contributions']} -> {profile['user_type']}")

    save_json(profiles_file, profiles)
    print(f"\n  ✓ 已保存 {len(profiles)} 位用户画像到 data/user_profiles.json")
    return profiles


# ─── 步骤 4：生成报告 ─────────────────────────────────────────────────────────

def generate_report():
    """读取采集结果，输出分析报告。"""
    print("\n=== 分析报告 ===\n")

    repos = load_json(DATA_DIR / "repos.json") or []
    all_users = load_json(DATA_DIR / "all_star_users.json") or []
    profiles = load_json(DATA_DIR / "user_profiles.json") or []

    if not repos:
        print("缺少仓库数据，请先运行采集步骤。")
        return

    # ── 仓库统计 ──
    total_stars = sum(r["star_count"] for r in repos)
    total_forks = sum(r["forks_count"] for r in repos)
    total_issues = sum(r["open_issues_count"] for r in repos)
    total_mrs = sum(r["open_mr_count"] for r in repos)

    print(f"【组织概览】")
    print(f"  仓库总数：{len(repos)}")
    print(f"  总 Star 数：{total_stars}")
    print(f"  总 Fork 数：{total_forks}")
    print(f"  开放 Issue 数：{total_issues}")
    print(f"  开放 MR 数：{total_mrs}")

    print(f"\n【Star 数 Top 15 仓库】")
    print(f"  {'仓库':<45} {'Star':>6} {'Fork':>6} {'Issue':>6}")
    print(f"  {'-'*45} {'-'*6} {'-'*6} {'-'*6}")
    for r in repos[:15]:
        name = r["path"].split("/")[-1]
        print(f"  {name:<45} {r['star_count']:>6} {r['forks_count']:>6} {r['open_issues_count']:>6}")

    print(f"\n【Star 分布】")
    buckets = {"0": 0, "1-9": 0, "10-49": 0, "50-199": 0, "200+": 0}
    for r in repos:
        s = r["star_count"]
        if s == 0: buckets["0"] += 1
        elif s < 10: buckets["1-9"] += 1
        elif s < 50: buckets["10-49"] += 1
        elif s < 200: buckets["50-199"] += 1
        else: buckets["200+"] += 1
    for k, v in buckets.items():
        bar = "█" * v
        print(f"  {k:>6} stars: {bar} ({v})")

    # ── 用户统计 ──
    if profiles:
        profile_map = {p["user_name"]: p for p in profiles}
        type_counts = {"developer": 0, "casual": 0, "ghost": 0}
        for p in profiles:
            t = p.get("user_type", "ghost")
            type_counts[t] = type_counts.get(t, 0) + 1

        total_profiled = len(profiles)
        print(f"\n【唯一 Star 用户：{len(all_users)} 位，已画像：{total_profiled} 位】")
        print(f"\n【用户类型分布】")
        labels = {
            "developer": "开发者（有原创仓库/贡献/粉丝）",
            "casual":    "普通用户（有少量活动）",
            "ghost":     "三无用户（无粉丝/仓库/贡献）",
        }
        for t, label in labels.items():
            n = type_counts.get(t, 0)
            pct = n / total_profiled * 100 if total_profiled else 0
            bar = "█" * int(pct / 2)
            print(f"  {label:<30} {n:>5} ({pct:5.1f}%)  {bar}")

        # 每个仓库的用户类型分布
        print(f"\n【各仓库用户类型分布（Top 10 by star）】")
        print(f"  {'仓库':<40} {'总Star':>7} {'开发者':>7} {'普通':>7} {'三无':>7}")
        print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
        for repo in repos[:10]:
            if repo["star_count"] == 0:
                continue
            rpath = repo["path"]
            # 该仓库的 star 用户
            star_users_in_repo = [u for u in all_users if rpath in u.get("starred_repos", [])]
            dev = cas = ghost = 0
            for u in star_users_in_repo:
                p = profile_map.get(u["user_name"])
                if not p:
                    continue
                t = p.get("user_type", "ghost")
                if t == "developer": dev += 1
                elif t == "casual": cas += 1
                else: ghost += 1
            name = rpath.split("/")[-1]
            print(f"  {name:<40} {repo['star_count']:>7} {dev:>7} {cas:>7} {ghost:>7}")

        # 多仓库 star 用户（真正的社区参与者）
        multi_star = [u for u in all_users if len(u.get("starred_repos", [])) > 1]
        print(f"\n【Star 了多个仓库的用户：{len(multi_star)} 位】")
        if multi_star:
            multi_star.sort(key=lambda u: len(u.get("starred_repos", [])), reverse=True)
            for u in multi_star[:10]:
                utype = profile_map.get(u["user_name"], {}).get("user_type", "未知")
                repos_starred = ", ".join(r.split("/")[-1] for r in u["starred_repos"][:5])
                print(f"  {u['user_name']:<25} {len(u['starred_repos'])} 个仓库  [{utype}]  ({repos_starred}...)")

    # ── 时间趋势（各仓库最早 star 时间） ──
    print(f"\n【仓库创建时间分布（按年）】")
    year_count = {}
    for r in repos:
        year = r.get("created_at", "")[:4]
        if year:
            year_count[year] = year_count.get(year, 0) + 1
    for year in sorted(year_count):
        bar = "█" * year_count[year]
        print(f"  {year}: {bar} ({year_count[year]})")

    print("\n报告生成完毕。")


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "repos":
        collect_repos()
    elif cmd == "stars":
        collect_stars()
    elif cmd == "users":
        collect_users()
    elif cmd == "all":
        collect_repos()
        collect_stars()
        collect_users()
        generate_report()
    elif cmd == "report":
        generate_report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
