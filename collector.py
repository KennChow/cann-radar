"""
指定 GitCode 仓库数据采集器
采集 cann/ge、cann/hixl 与 Ascend/torchair 的统计数据，以及每个仓库的 star 用户画像。

用法:
    python collector.py repos          # 采集所有仓库基本信息
    python collector.py stars          # 采集所有仓库的 star 用户列表
    python collector.py users          # 采集所有 star 用户的画像数据
    python collector.py activities     # 采集各仓库 MR/Issue 作者（区分贡献者/提问者）
    python collector.py forks          # 采集各仓库 Fork 明细（用于 D0 用户识别）
    python collector.py issues         # 采集各仓库所有 Issue（含关闭时间）
    python collector.py mrs            # 采集各仓库 MR 详情（含时间戳）
    python collector.py weekly         # 生成周粒度活跃度数据
    python collector.py reclassify     # 补充贡献数据并重新分类
    python collector.py overview       # 生成概览聚合数据
    python collector.py dlevels        # 生成 D0/D1/D2 汇总数据
    python collector.py all            # 顺序执行以上所有步骤
    python collector.py report         # 生成分析报告（需先完成采集）
"""

import json
import time
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 修复 Windows GBK 控制台编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ─── 配置 ────────────────────────────────────────────────────────────────────

BASE_URL = "https://web-api.gitcode.com"
CONFIG_PATH = Path("config/repos.json")
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def load_repo_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"缺少配置文件: {CONFIG_PATH}")
    config = load_json(CONFIG_PATH) or {"repos": []}
    repos = config.get("repos", [])
    if not repos:
        raise ValueError("config/repos.json 中未配置任何仓库")
    return repos


def active_repo_configs():
    return [repo for repo in load_repo_config() if repo.get("enabled", True)]


def active_repo_paths():
    return [repo["path"] for repo in active_repo_configs()]

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

def get(url, retries=3, delay=REQUEST_DELAY, timeout=8):
    """发送 GET 请求，返回解析后的 JSON 或 None。"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    获取指定仓库列表，并逐个请求详情（含 star/fork/issue 数）。
    结果保存到 data/repos.json。
    """
    print("\n=== 步骤 1：采集仓库列表及详情 ===")
    repo_configs = active_repo_configs()
    target_paths = [repo["path"] for repo in repo_configs]
    print(f"  目标仓库：{', '.join(target_paths)}")

    repos_detail = []
    for i, path in enumerate(target_paths, start=1):
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
            print(f"  [{i}/{len(target_paths)}] {path}: star={repos_detail[-1]['star_count']} fork={repos_detail[-1]['forks_count']} issue={repos_detail[-1]['open_issues_count']}")
        else:
            print(f"  [{i}/{len(target_paths)}] {path}: 获取失败")
        time.sleep(REQUEST_DELAY)

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

def classify_user(profile, mr_authors=None, issue_authors=None):
    """
    判断用户类型。

    开发者（有 GitCode 贡献活动）进一步细分：
    - contributor：在 CANN 仓库提交过 MR/PR（贡献者）
    - questioner：在 CANN 仓库提过 Issue，但无 MR（提问者）
    - developer：有 GitCode 贡献，但无 CANN 特定 MR/Issue（开发者）

    非开发者（无贡献活动）：
    - star_enthusiast：Star 了多个 CANN 仓库（Star 爱好者）
    - die_hard_fan：只 Star 了某一个 CANN 仓库（铁粉）
    """
    total_contributions = profile.get("total_contributions", 0)
    starred_count = len(profile.get("starred_repos", []))
    uname = profile.get("user_name", "")

    if total_contributions >= 1:
        if mr_authors and uname in mr_authors:
            return "contributor"     # 贡献者
        elif issue_authors and uname in issue_authors:
            return "questioner"      # 提问者
        else:
            return "developer"       # 开发者（无 CANN 特定活动）
    elif starred_count >= 2:
        return "star_enthusiast"     # Star 爱好者
    else:
        return "die_hard_fan"        # 铁粉


def _fetch_one_user(user):
    """采集单个用户的画像数据（供线程池调用）。"""
    uname = user["user_name"]
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
    data = get(f"{BASE_URL}/api/v1/follow/userBaseInfo?username={uname}", timeout=8)
    if data and "fans_count" in data:
        profile["fans_count"] = data.get("fans_count", 0)
        profile["follow_count"] = data.get("follow_count", 0)

    # 2. 创建的仓库
    data = get(f"{BASE_URL}/api/v1/profile/{uname}/created_projects?page=1&per_page=20", timeout=8)
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

    # 3. 贡献活动
    data = get(f"{BASE_URL}/uc/api/v1/events/{uname}/contributions", timeout=8)
    if data and isinstance(data, dict) and "error_code" not in data:
        profile["total_contributions"] = sum(v for v in data.values() if isinstance(v, int))

    profile["user_type"] = classify_user(profile)
    return profile


# 并发线程数
USER_FETCH_WORKERS = 5


def collect_users():
    """
    为每位 star 用户获取画像（粉丝数、创建仓库数、贡献活动）。
    使用线程池并发采集，结果保存到 data/user_profiles.json。
    """
    print("\n=== 步骤 3：采集用户画像 ===")

    all_users = load_json(DATA_DIR / "all_star_users.json")
    if not all_users:
        print("  请先运行 python collector.py stars")
        return

    profiles_file = DATA_DIR / "user_profiles.json"
    active_user_names = {u["user_name"] for u in all_users}
    existing = [p for p in (load_json(profiles_file) or []) if p.get("user_name") in active_user_names]
    done_users = {p["user_name"] for p in existing}
    pending = [u for u in all_users if u["user_name"] not in done_users]
    print(f"  已有 {len(done_users)} 位用户画像，待采集 {len(pending)} 位（并发 {USER_FETCH_WORKERS} 线程）")

    if not pending:
        print("  无需采集")
        return existing

    profiles = list(existing)
    completed = 0
    failed = 0
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=USER_FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_user, u): u for u in pending}
        for future in as_completed(futures):
            try:
                profile = future.result()
            except Exception as e:
                uname = futures[future]["user_name"]
                failed += 1
                print(f"  ✗ {uname}: {e}")
                continue
            profiles.append(profile)
            completed += 1
            if completed % 50 == 0 or completed == len(pending):
                save_json(profiles_file, profiles)
            if completed % 10 == 0 or completed == len(pending):
                elapsed = time.time() - t_start
                speed = completed / elapsed if elapsed > 0 else 0
                remaining = (len(pending) - completed) / speed if speed > 0 else 0
                print(f"  [{completed}/{len(pending)}] {profile['user_name']}: "
                      f"fans={profile['fans_count']} repos={profile['original_repo_count']} "
                      f"contribs={profile['total_contributions']} -> {profile['user_type']}  "
                      f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s left, {speed:.1f} users/s, {failed} failed)")

    save_json(profiles_file, profiles)
    print(f"\n  ✓ 已保存 {len(profiles)} 位用户画像到 data/user_profiles.json")
    return profiles


# ─── 步骤 3.5：采集各仓库 MR / Issue 作者 ────────────────────────────────────

def _fetch_repo_activities(repo):
    """采集单个仓库的 MR/Issue 作者（供线程池调用）。"""
    repo_id   = repo["id"]
    repo_path = repo["path"]
    encoded   = urllib.parse.quote(repo_path, safe="")

    mr_authors = set()
    mr_page = 1
    mr_count = 0
    while True:
        url  = f"{BASE_URL}/api/v1/projects/{repo_id}/merge_requests?page={mr_page}&per_page=100&state=all"
        data = get(url)
        if not data or not data.get("content"):
            break
        for mr in data["content"]:
            uname = (mr.get("author") or {}).get("username")
            if uname:
                mr_authors.add(uname)
                mr_count += 1
        total = data.get("total") or 0
        if len(mr_authors) >= total or len(data["content"]) < 100:
            break
        mr_page += 1
        time.sleep(REQUEST_DELAY)

    issue_authors = set()
    issue_page = 1
    issue_count = 0
    while True:
        url  = f"{BASE_URL}/api/v1/issue/{encoded}/issues?page={issue_page}&per_page=100&state=all"
        data = get(url)
        if not data or not data.get("issues"):
            break
        for issue in data["issues"]:
            uname = (issue.get("author") or {}).get("username")
            if uname:
                issue_authors.add(uname)
                issue_count += 1
        total = data.get("all") or 0
        if issue_count >= total or len(data["issues"]) < 100:
            break
        issue_page += 1
        time.sleep(REQUEST_DELAY)

    return repo_path, mr_authors, issue_authors, mr_count, issue_count


def collect_activities():
    """
    遍历所有仓库，并发抓取 MR 和 Issue 的作者用户名。
    用于将"开发者"进一步区分为"贡献者"（有 MR）和"提问者"（有 Issue，无 MR）。
    结果保存到 data/activity_users.json。
    """
    print("\n=== 步骤 3.5：采集 MR / Issue 作者 ===")

    repos = load_json(DATA_DIR / "repos.json")
    if not repos:
        print("  请先运行 python collector.py repos")
        return

    mr_authors = set()
    issue_authors = set()

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=len(repos)) as pool:
        futures = {pool.submit(_fetch_repo_activities, repo): repo for repo in repos}
        for future in as_completed(futures):
            try:
                repo_path, repo_mr, repo_issue, mr_count, issue_count = future.result()
                mr_authors.update(repo_mr)
                issue_authors.update(repo_issue)
                print(f"  {repo_path}: MR作者 +{mr_count}  Issue作者 +{issue_count}  "
                      f"（累计 MR={len(mr_authors)} Issue={len(issue_authors)}）")
            except Exception as e:
                repo_path = futures[future]["path"]
                print(f"  ✗ {repo_path}: {e}")

    result = {
        "mr_authors":    sorted(mr_authors),
        "issue_authors": sorted(issue_authors),
    }
    save_json(DATA_DIR / "activity_users.json", result)
    elapsed = time.time() - t_start
    print(f"\n  ✓ MR 作者 {len(mr_authors)} 位，Issue 作者 {len(issue_authors)} 位（耗时 {elapsed:.0f}s）")
    print(f"    已保存到 data/activity_users.json")
    return result


def _fetch_repo_forks(repo, forks_dir):
    """采集单个仓库的 Fork 明细（供线程池调用）。"""
    repo_id = repo["id"]
    repo_path = repo["path"]
    safe_name = repo_path.replace("/", "__")
    cache_file = forks_dir / f"{safe_name}.json"

    if cache_file.exists():
        forks = load_json(cache_file) or []
        return repo_path, forks, True

    forks = []
    page = 1
    per_page = 100
    while True:
        url = f"{BASE_URL}/api/v1/projects/{repo_id}/forks?page={page}&per_page={per_page}"
        data = get(url)
        items = (data or {}).get("content") or []
        for item in items:
            creator = item.get("creator") or {}
            forks.append({
                "id": item.get("id"),
                "namespace": item.get("namespace", ""),
                "name": item.get("name", ""),
                "web_url": item.get("web_url", ""),
                "http_url_to_repo": item.get("http_url_to_repo", ""),
                "created_at": item.get("created_at", ""),
                "creator_username": creator.get("username") or ((item.get("namespace", "").split("/")[0]) if "/" in item.get("namespace", "") else ""),
                "creator_nick_name": creator.get("nick_name") or creator.get("name") or "",
                "forked_from": (item.get("forked_from_project") or {}).get("path_with_namespace", repo_path),
            })
        if not items or page >= ((data or {}).get("page_count") or 1):
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    save_json(cache_file, forks)
    return repo_path, forks, False


def collect_forks():
    """
    采集目标仓库的 Fork 明细，保存到 data/forks/{repo}.json。
    多仓库并发采集，用于识别 D0 关注者中的 Fork 用户。
    """
    print("\n=== 步骤 3.8：采集 Fork 明细 ===")

    repos = load_json(DATA_DIR / "repos.json")
    if not repos:
        print("  请先运行 python collector.py repos")
        return

    forks_dir = DATA_DIR / "forks"
    forks_dir.mkdir(exist_ok=True)

    result = {}
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=len(repos)) as pool:
        futures = {pool.submit(_fetch_repo_forks, repo, forks_dir): repo for repo in repos}
        for future in as_completed(futures):
            try:
                repo_path, forks, cached = future.result()
                result[repo_path] = forks
                if cached:
                    print(f"  {repo_path}: 使用缓存（{len(forks)} 条）")
                else:
                    print(f"  {repo_path}: 共 {len(forks)} 条 Fork")
            except Exception as e:
                repo_path = futures[future]["path"]
                print(f"  ✗ {repo_path}: {e}")

    elapsed = time.time() - t_start
    print(f"\n  ✓ 各仓库 Fork 明细已保存到 data/forks/（耗时 {elapsed:.0f}s）")
    return result


# ─── 补充步骤：对已有画像重新抓取贡献并重分类 ─────────────────────────────────

def reclassify_users():
    """
    对已采集的用户画像中，之前因有原创仓库而跳过贡献抓取的用户，
    补充抓取 total_contributions，然后重新运行 classify_user。
    结果原地更新 data/user_profiles.json。
    """
    print("\n=== 补充：重新采集贡献数据并重分类 ===")

    profiles_file = DATA_DIR / "user_profiles.json"
    profiles = load_json(profiles_file)
    if not profiles:
        print("  缺少 user_profiles.json，请先运行 python collector.py users")
        return

    # 找出需要补充抓取的用户：original_repo_count > 0 但 total_contributions == 0
    # 这类用户之前因为有仓库而跳过了贡献抓取
    need_refetch = [p for p in profiles if p.get("original_repo_count", 0) > 0 and p.get("total_contributions", 0) == 0]
    print(f"  需要补充抓取贡献的用户：{len(need_refetch)} 位")

    for i, p in enumerate(need_refetch):
        uname = p["user_name"]
        url = f"{BASE_URL}/uc/api/v1/events/{uname}/contributions"
        data = get(url)
        if data and isinstance(data, dict) and "error_code" not in data:
            p["total_contributions"] = sum(v for v in data.values() if isinstance(v, int))
        if (i + 1) % 20 == 0 or i == len(need_refetch) - 1:
            print(f"  [{i+1}/{len(need_refetch)}] {uname}: contributions={p['total_contributions']}")
        time.sleep(USER_REQUEST_DELAY)

    # 加载 MR/Issue 作者数据（若存在）
    activity = load_json(DATA_DIR / "activity_users.json") or {}
    mr_authors    = set(activity.get("mr_authors", []))
    issue_authors = set(activity.get("issue_authors", []))
    if mr_authors or issue_authors:
        print(f"  已加载活动数据：MR作者 {len(mr_authors)} 位，Issue作者 {len(issue_authors)} 位")

    # 重新分类所有用户
    changed = 0
    for p in profiles:
        old_type = p.get("user_type")
        new_type = classify_user(p, mr_authors, issue_authors)
        if old_type != new_type:
            changed += 1
        p["user_type"] = new_type

    save_json(profiles_file, profiles)
    print(f"\n  ✓ 重分类完成，共 {changed} 位用户类型发生变化，已保存到 data/user_profiles.json")

    # 打印新的分布
    type_counts = {}
    for p in profiles:
        t = p.get("user_type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    print("\n  新类型分布：")
    for t, n in sorted(type_counts.items()):
        print(f"    {t}: {n} ({n/len(profiles)*100:.1f}%)")


# ─── 步骤 4：采集各仓库 Issue 详情 ───────────────────────────────────────────

def _fetch_repo_issues(repo, issues_dir):
    """采集单个仓库的全部 Issue（供线程池调用）。"""
    repo_path = repo["path"]
    encoded   = urllib.parse.quote(repo_path, safe="")
    safe_name = repo_path.replace("/", "__")
    cache_file = issues_dir / f"{safe_name}.json"

    if cache_file.exists():
        existing = load_json(cache_file) or []
        return repo_path, existing, True

    all_issues = []
    total      = None
    page       = 1

    while True:
        url  = f"{BASE_URL}/api/v1/issue/{encoded}/issues?page={page}&per_page=100&state=all"
        data = get(url)
        if not data or not data.get("issues"):
            break

        if total is None:
            total = data.get("all") or 0

        for issue in data["issues"]:
            closed_raw = issue.get("closed_at") or ""
            all_issues.append({
                "iid":        issue.get("iid"),
                "state":      issue.get("state", "opened"),
                "created_at": (issue.get("created_at") or "")[:10],
                "closed_at":  closed_raw[:10] if closed_raw else "",
                "author":     (issue.get("author") or {}).get("username", ""),
            })

        if total and len(all_issues) >= total or len(data["issues"]) < 100:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    save_json(cache_file, all_issues)
    return repo_path, all_issues, False


def collect_issues():
    """
    采集所有仓库的完整 Issue 列表，保存创建时间和关闭时间，
    多仓库并发采集，结果按仓库保存到 data/issues/{repo}.json。
    """
    print("\n=== 步骤 4：采集各仓库 Issue 详情 ===")

    repos = load_json(DATA_DIR / "repos.json")
    if not repos:
        print("  请先运行 python collector.py repos")
        return

    issues_dir = DATA_DIR / "issues"
    issues_dir.mkdir(exist_ok=True)

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=len(repos)) as pool:
        futures = {pool.submit(_fetch_repo_issues, repo, issues_dir): repo for repo in repos}
        for future in as_completed(futures):
            try:
                repo_path, issues, cached = future.result()
                if cached:
                    print(f"  {repo_path}: 使用缓存（{len(issues)} 条）")
                else:
                    repo = futures[future]
                    print(f"  {repo_path}: 共 {len(issues)} 条（open={repo['open_issues_count']}）")
            except Exception as e:
                repo_path = futures[future]["path"]
                print(f"  ✗ {repo_path}: {e}")

    elapsed = time.time() - t_start
    print(f"\n  ✓ 各仓库 Issue 已保存到 data/issues/（耗时 {elapsed:.0f}s）")


# ─── 步骤 5：采集各仓库 MR 详情 ───────────────────────────────────────────────

def _fetch_repo_mrs(repo, mrs_dir):
    """采集单个仓库的全部 MR（供线程池调用）。"""
    repo_id   = repo["id"]
    repo_path = repo["path"]
    safe_name = repo_path.replace("/", "__")
    cache_file = mrs_dir / f"{safe_name}.json"

    if cache_file.exists():
        existing = load_json(cache_file) or []
        return repo_path, existing, True

    all_mrs = []
    total   = None
    page    = 1

    while True:
        url  = f"{BASE_URL}/api/v1/projects/{repo_id}/merge_requests?page={page}&per_page=100&state=all"
        data = get(url)
        if not data or not data.get("content"):
            break

        if total is None:
            total = data.get("total") or 0

        for mr in data["content"]:
            merged_raw = mr.get("merged_at") or ""
            closed_raw = mr.get("closed_at") or ""
            all_mrs.append({
                "iid":        mr.get("iid"),
                "state":      mr.get("state", "opened"),
                "created_at": (mr.get("created_at") or "")[:10],
                "merged_at":  merged_raw[:10] if merged_raw else "",
                "closed_at":  closed_raw[:10] if closed_raw else "",
                "author":     (mr.get("author") or {}).get("username", ""),
            })

        if (total and len(all_mrs) >= total) or len(data["content"]) < 100:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    save_json(cache_file, all_mrs)
    return repo_path, all_mrs, False


def collect_mrs():
    """
    采集所有仓库的完整 MR 列表，保存创建时间、合并时间、状态和作者，
    用于计算 MR 趋势和周粒度活跃度分析。
    多仓库并发采集，结果按仓库保存到 data/mrs/{repo}.json。
    """
    print("\n=== 步骤 5：采集各仓库 MR 详情 ===")

    repos = load_json(DATA_DIR / "repos.json")
    if not repos:
        print("  请先运行 python collector.py repos")
        return

    mrs_dir = DATA_DIR / "mrs"
    mrs_dir.mkdir(exist_ok=True)

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=len(repos)) as pool:
        futures = {pool.submit(_fetch_repo_mrs, repo, mrs_dir): repo for repo in repos}
        for future in as_completed(futures):
            try:
                repo_path, mrs, cached = future.result()
                if cached:
                    print(f"  {repo_path}: 使用缓存（{len(mrs)} 条）")
                else:
                    repo = futures[future]
                    print(f"  {repo_path}: 共 {len(mrs)} 条 MR（open_mr={repo['open_mr_count']}）")
            except Exception as e:
                repo_path = futures[future]["path"]
                print(f"  ✗ {repo_path}: {e}")

    elapsed = time.time() - t_start
    print(f"\n  ✓ 各仓库 MR 已保存到 data/mrs/（耗时 {elapsed:.0f}s）")


# ─── 步骤 6：生成周粒度活跃度数据 ─────────────────────────────────────────────

def generate_issue_summary():
    """
    基于 data/issues/ 中的数据，按仓库统计平均 Issue 解决天数。
    结果保存到 data/issue_summary.json，供前端图表使用。
    """
    print("\n=== 生成 Issue 解决时间汇总 ===")
    active_paths = set(active_repo_paths())

    issues_dir = DATA_DIR / "issues"
    if not issues_dir.exists():
        print("  缺少 data/issues/ 目录，请先运行 python collector.py issues")
        return

    repos_data = []
    for f in sorted(issues_dir.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if repo_path not in active_paths:
            continue
        issues = load_json(f) or []
        closed = [i for i in issues if i.get("state") == "closed"
                  and i.get("closed_at") and i.get("created_at")]
        opened = sum(1 for i in issues if i.get("state") == "opened")
        if len(closed) < 5:
            continue
        days_list = []
        for i in closed:
            try:
                d = (datetime.fromisoformat(i["closed_at"]) -
                     datetime.fromisoformat(i["created_at"])).days
                if d >= 0:
                    days_list.append(d)
            except (ValueError, TypeError):
                continue
        if not days_list:
            continue
        repos_data.append({
            "name":     repo_path.split("/")[1],
            "path":     repo_path,
            "avg_days": round(sum(days_list) / len(days_list), 1),
            "closed":   len(closed),
            "opened":   opened,
        })

    repos_data.sort(key=lambda x: x["avg_days"])
    result = {"repos": repos_data, "generated_at": datetime.now().strftime("%Y-%m-%d")}
    save_json(DATA_DIR / "issue_summary.json", result)
    print(f"  ✓ 共 {len(repos_data)} 个仓库，已保存到 data/issue_summary.json")
    return result


def generate_mr_summary():
    """
    基于 data/mrs/ 中的数据，按仓库统计 merged / open MR 数量（忽略 closed）。
    结果保存到 data/mr_summary.json，供前端图表使用。
    """
    print("\n=== 生成 MR 状态汇总 ===")
    active_paths = set(active_repo_paths())

    mrs_dir = DATA_DIR / "mrs"
    if not mrs_dir.exists():
        print("  缺少 data/mrs/ 目录，请先运行 python collector.py mrs")
        return

    repos_data = []
    all_authors = set()
    for f in sorted(mrs_dir.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if repo_path not in active_paths:
            continue
        mrs = load_json(f) or []
        merged = sum(1 for m in mrs if m.get("state") == "merged")
        open_  = sum(1 for m in mrs if m.get("state") == "opened")
        total  = merged + open_
        for m in mrs:
            if m.get("author"):
                all_authors.add(m["author"])
        if total > 0:
            repos_data.append({
                "name":   repo_path.split("/")[1],
                "path":   repo_path,
                "merged": merged,
                "open":   open_,
                "total":  total,
            })

    repos_data.sort(key=lambda x: x["total"], reverse=True)
    result = {
        "repos": repos_data,
        "unique_authors": len(all_authors),
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
    }
    save_json(DATA_DIR / "mr_summary.json", result)
    print(f"  ✓ 共 {len(repos_data)} 个仓库，{len(all_authors)} 位唯一 MR 提交者，已保存到 data/mr_summary.json")
    return result


def generate_weekly_activity():
    """
    基于 data/mrs/ 中的数据，按 ISO 周统计各仓库的 MR 创建数量。
    结果保存到 data/weekly_activity.json，供前端热力图使用。
    """
    print("\n=== 生成周粒度活跃度数据 ===")
    active_paths = set(active_repo_paths())

    mrs_dir = DATA_DIR / "mrs"
    if not mrs_dir.exists():
        print("  缺少 data/mrs/ 目录，请先运行 python collector.py mrs")
        return

    # repo_path -> {week_str -> count}
    repo_weekly = {}

    for f in sorted(mrs_dir.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if repo_path not in active_paths:
            continue
        mrs = load_json(f) or []
        weekly = {}
        for mr in mrs:
            created = mr.get("created_at", "")
            if not created or len(created) < 10:
                continue
            try:
                dt = datetime.fromisoformat(created)
            except (ValueError, AttributeError):
                try:
                    dt = datetime.strptime(created[:10], "%Y-%m-%d")
                except ValueError:
                    continue
            year, week, _ = dt.isocalendar()
            week_str = f"{year}-W{week:02d}"
            weekly[week_str] = weekly.get(week_str, 0) + 1
        repo_weekly[repo_path] = weekly

    # 收集所有出现的周并排序
    all_weeks = set()
    for weekly in repo_weekly.values():
        all_weeks.update(weekly.keys())
    sorted_weeks = sorted(all_weeks)

    # 按总 MR 数降序排列仓库
    repo_totals = [(path, sum(w.values())) for path, w in repo_weekly.items()]
    repo_totals.sort(key=lambda x: x[1], reverse=True)

    result = {
        "weeks": sorted_weeks,
        "repos": [
            {
                "name":  path.split("/")[1],
                "path":  path,
                "total": total,
                "data":  [repo_weekly[path].get(w, 0) for w in sorted_weeks],
            }
            for path, total in repo_totals
        ],
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
    }

    save_json(DATA_DIR / "weekly_activity.json", result)
    print(f"  ✓ 共 {len(sorted_weeks)} 个周，{len(result['repos'])} 个仓库，已保存到 data/weekly_activity.json")
    return result


# ─── 概览聚合数据 ────────────────────────────────────────────────────────────

def generate_overview_data():
    """
    聚合全组织 Star 时间线数据，按月统计各类型用户的新增 star 数。
    结果保存到 data/org_timeline.json，供前端直接使用。
    """
    print("\n=== 生成概览聚合数据 ===")
    active_paths = set(active_repo_paths())

    profiles = load_json(DATA_DIR / "user_profiles.json") or []
    profile_map = {p["user_name"]: p.get("user_type", "die_hard_fan") for p in profiles}

    stars_dir = DATA_DIR / "stars"
    if not stars_dir.exists():
        print("  缺少 data/stars/ 目录，请先运行 python collector.py stars")
        return

    # month -> type -> count（每月新增 star 事件，含跨仓库重复）
    monthly = {}
    total_events = 0

    for f in sorted(stars_dir.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if repo_path not in active_paths:
            continue
        users = load_json(f) or []
        for u in users:
            created_at = u.get("created_at", "")
            if not created_at or len(created_at) < 7:
                continue
            ym    = created_at[:7]
            uname = u.get("user_name", "")
            utype = profile_map.get(uname, "die_hard_fan")
            if ym not in monthly:
                monthly[ym] = {"contributor": 0, "questioner": 0, "developer": 0,
                               "star_enthusiast": 0, "die_hard_fan": 0}
            monthly[ym][utype] = monthly[ym].get(utype, 0) + 1
            total_events += 1

    # 按月排序并计算累计
    sorted_months = sorted(monthly.keys())
    cumulative = 0
    result = []
    for ym in sorted_months:
        m = monthly[ym]
        new = sum(m.values())
        cumulative += new
        result.append({"month": ym, "new_stars": new, "cumulative": cumulative, **m})

    save_json(DATA_DIR / "org_timeline.json", result)
    print(f"  ✓ 共 {len(result)} 个月，{total_events} 条 star 事件，已保存到 data/org_timeline.json")
    return result


# ─── 步骤 4b：生成前端精简用户文件 ───────────────────────────────────────────

def generate_users_slim():
    """
    合并 user_profiles.json 和 all_star_users.json，只保留前端实际使用的字段，
    输出 data/users_slim.json，减少前端传输体积。
    """
    print("\n=== 生成前端精简用户数据 ===")

    profiles  = load_json(DATA_DIR / "user_profiles.json") or []
    all_users = load_json(DATA_DIR / "all_star_users.json") or []

    profile_map = {p["user_name"]: p for p in profiles}

    result = []
    for u in all_users:
        name = u["user_name"]
        p    = profile_map.get(name, {})
        result.append({
            "user_name":           name,
            "nick_name":           u.get("nick_name", ""),
            "starred_repos":       u.get("starred_repos", []),
            "user_type":           p.get("user_type", ""),
            "fans_count":          p.get("fans_count"),
            "original_repo_count": p.get("original_repo_count"),
            "total_contributions": p.get("total_contributions"),
        })

    save_json(DATA_DIR / "users_slim.json", result)
    orig  = (DATA_DIR / "user_profiles.json").stat().st_size + (DATA_DIR / "all_star_users.json").stat().st_size
    slim  = (DATA_DIR / "users_slim.json").stat().st_size
    print(f"  ✓ {len(result)} 位用户，{slim//1024}KB（原两文件合计 {orig//1024}KB），已保存到 data/users_slim.json")
    return result


def generate_dlevel_summary():
    """
    生成 D0/D1/D2 分层汇总数据，保存到 data/dlevel_summary.json。
    D0：Star/Fork 用户（排除 D1/D2）
    D1：Issue 作者或 PR 作者（排除 D2）
    D2：至少合入 1 个 PR 的用户
    """
    print("\n=== 生成 D0/D1/D2 汇总数据 ===")

    repos = load_json(DATA_DIR / "repos.json") or []
    users_slim = load_json(DATA_DIR / "users_slim.json") or []
    stars_dir = DATA_DIR / "stars"
    forks_dir = DATA_DIR / "forks"
    issues_dir = DATA_DIR / "issues"
    mrs_dir = DATA_DIR / "mrs"
    if not repos or not stars_dir.exists() or not forks_dir.exists() or not issues_dir.exists() or not mrs_dir.exists():
        print("  缺少必要数据，请先运行 repos/stars/forks/issues/mrs/users-slim")
        return

    user_meta = {u["user_name"]: {
        "nick_name": u.get("nick_name", ""),
        "fans_count": u.get("fans_count"),
        "original_repo_count": u.get("original_repo_count"),
        "total_contributions": u.get("total_contributions"),
        "starred_repos": u.get("starred_repos", []),
    } for u in users_slim}

    priority = {"d0": 0, "d1": 1, "d2": 2}
    repo_counts = {}
    repo_users = {}
    global_levels = {}
    monthly = {}

    for repo in repos:
        repo_path = repo["path"]
        safe_name = repo_path.replace("/", "__")
        stars = load_json(stars_dir / f"{safe_name}.json") or []
        forks = load_json(forks_dir / f"{safe_name}.json") or []
        issues = load_json(issues_dir / f"{safe_name}.json") or []
        mrs = load_json(mrs_dir / f"{safe_name}.json") or []

        star_map = {}
        for s in stars:
            uname = s.get("user_name")
            if not uname:
                continue
            star_map[uname] = {
                "star_time": s.get("created_at", ""),
                "nick_name": s.get("nick_name", ""),
            }
            user_meta.setdefault(uname, {"nick_name": s.get("nick_name", ""), "fans_count": None, "original_repo_count": None, "total_contributions": None, "starred_repos": []})

        fork_map = {}
        for f in forks:
            uname = f.get("creator_username")
            if not uname:
                continue
            fork_map[uname] = {
                "fork_time": f.get("created_at", ""),
                "nick_name": f.get("creator_nick_name", ""),
            }
            user_meta.setdefault(uname, {"nick_name": f.get("creator_nick_name", ""), "fans_count": None, "original_repo_count": None, "total_contributions": None, "starred_repos": []})

        issue_authors = {i.get("author") for i in issues if i.get("author")}
        pr_authors = {m.get("author") for m in mrs if m.get("author")}
        merged_authors = {m.get("author") for m in mrs if m.get("author") and m.get("state") == "merged" and m.get("merged_at")}

        all_repo_usernames = set(star_map) | set(fork_map) | issue_authors | pr_authors | merged_authors
        users = []
        counts = {"d0": 0, "d1": 0, "d2": 0, "total": 0}
        for uname in sorted(all_repo_usernames):
            if uname in merged_authors:
                level = "d2"
            elif uname in issue_authors or uname in pr_authors:
                level = "d1"
            else:
                level = "d0"
            counts[level] += 1
            counts["total"] += 1
            meta = user_meta.get(uname, {})
            sources = []
            if uname in star_map:
                sources.append("star")
            if uname in fork_map:
                sources.append("fork")
            if uname in issue_authors:
                sources.append("issue")
            if uname in pr_authors:
                sources.append("pr")
            if uname in merged_authors:
                sources.append("merged_pr")
            star_time = star_map.get(uname, {}).get("star_time", "")
            users.append({
                "user_name": uname,
                "nick_name": meta.get("nick_name") or star_map.get(uname, {}).get("nick_name") or fork_map.get(uname, {}).get("nick_name") or uname,
                "level": level,
                "sources": sources,
                "star_time": star_time,
                "fans_count": meta.get("fans_count"),
                "original_repo_count": meta.get("original_repo_count"),
                "total_contributions": meta.get("total_contributions"),
                "starred_repos": meta.get("starred_repos", []),
            })
            if uname not in global_levels or priority[level] > priority[global_levels[uname]]:
                global_levels[uname] = level

            if star_time and len(star_time) >= 7:
                ym = star_time[:7]
                monthly.setdefault(ym, {"d0": 0, "d1": 0, "d2": 0})
                monthly[ym][level] += 1

        repo_counts[repo_path] = counts
        repo_users[repo_path] = users

    global_counts = {"d0": 0, "d1": 0, "d2": 0, "total": len(global_levels)}
    for level in global_levels.values():
        global_counts[level] += 1

    star_timeline = []
    cumulative = 0
    for ym in sorted(monthly):
        new_stars = monthly[ym]["d0"] + monthly[ym]["d1"] + monthly[ym]["d2"]
        cumulative += new_stars
        star_timeline.append({
            "month": ym,
            "d0": monthly[ym]["d0"],
            "d1": monthly[ym]["d1"],
            "d2": monthly[ym]["d2"],
            "new_stars": new_stars,
            "cumulative": cumulative,
        })

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
        "global_counts": global_counts,
        "repo_counts": repo_counts,
        "repo_users": repo_users,
        "star_timeline": star_timeline,
    }
    save_json(DATA_DIR / "dlevel_summary.json", result)
    print(f"  ✓ 已保存 D0/D1/D2 汇总到 data/dlevel_summary.json")
    return result


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
    elif cmd == "activities":
        collect_activities()
    elif cmd == "issues":
        collect_issues()
    elif cmd == "forks":
        collect_forks()
    elif cmd == "mrs":
        collect_mrs()
    elif cmd == "issue-summary":
        generate_issue_summary()
    elif cmd == "users-slim":
        generate_users_slim()
    elif cmd == "mr-summary":
        generate_mr_summary()
    elif cmd == "weekly":
        generate_weekly_activity()
    elif cmd == "reclassify":
        reclassify_users()
    elif cmd == "overview":
        generate_overview_data()
    elif cmd == "dlevels":
        generate_dlevel_summary()
    elif cmd == "all":
        t_all = time.time()

        # Layer 1: repos（其他所有步骤的基础）
        collect_repos()

        # Layer 2: stars / forks / issues / mrs / activities 只依赖 repos，并发执行
        print("\n--- 并发采集 stars / forks / issues / mrs / activities ---")
        with ThreadPoolExecutor(max_workers=5) as pool:
            layer2 = {
                pool.submit(collect_stars): "stars",
                pool.submit(collect_forks): "forks",
                pool.submit(collect_issues): "issues",
                pool.submit(collect_mrs): "mrs",
                pool.submit(collect_activities): "activities",
            }
            for future in as_completed(layer2):
                name = layer2[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"  ✗ {name} 失败: {e}")

        # Layer 3: users（依赖 stars）
        collect_users()

        # Layer 4: reclassify（依赖 users + activities）
        reclassify_users()

        # Layer 5: overview / users-slim 可并发
        with ThreadPoolExecutor(max_workers=2) as pool:
            layer5 = {
                pool.submit(generate_overview_data): "overview",
                pool.submit(generate_users_slim): "users-slim",
            }
            for future in as_completed(layer5):
                name = layer5[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"  ✗ {name} 失败: {e}")

        # Layer 6: 聚合数据可并发
        with ThreadPoolExecutor(max_workers=4) as pool:
            layer6 = {
                pool.submit(generate_dlevel_summary): "dlevels",
                pool.submit(generate_issue_summary): "issue-summary",
                pool.submit(generate_mr_summary): "mr-summary",
                pool.submit(generate_weekly_activity): "weekly",
            }
            for future in as_completed(layer6):
                name = layer6[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"  ✗ {name} 失败: {e}")

        generate_report()
        print(f"\n{'='*50}")
        print(f"  全量采集完成，总耗时 {time.time() - t_all:.0f}s")
        print(f"{'='*50}")
    elif cmd == "report":
        generate_report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
