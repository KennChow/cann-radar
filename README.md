# CANN GitCode 数据分析与超期通知

对指定 GitCode 仓库的 Star / Fork / Issue / MR 数据进行采集与可视化分析，跟踪社区用户构成、参与深度和运营目标达成情况。同时自动扫描超期未关闭的 MR 和 Issue，通过邮件提醒相关开发者及时处理。

在线查看：https://kennchow.github.io/cann-stars/

## 功能概览

### 组织概览
- 仓库 Star / Fork / Issue / D0-D1-D2 用户数量
- 全组织 Star 用户类型分布（双环饼图 + 分类说明）
- 各仓库非开发者占比（仅含 Star ≥ 100 的仓库）
- 全组织 Star 增长趋势（按月堆叠 + 累计折线）
- 各仓库用户类型构成（百分比堆叠横向柱状图）
- 各仓库 MR 周活跃度热力图（近 26 周）
- 各仓库 MR 总数 Top 20（merged + open 堆叠）
- 参与讨论的外部开发者：总数、趋势折线和名单（基于 `config/discussions.yml` 抓取，排除内部名单）

### 仓库详情
- 切换单个仓库，查看用户类型饼图 + Star 目标进度
- 各类用户 Star 时间趋势（按月堆叠 + 累计折线）
- Issue 分析：总量 / 已关闭 / 开放中 / 平均解决天数、创建关闭趋势
- MR 分析：总量 / 已合并 / 开放中 / 平均合并天数、创建合并趋势、状态分布
- 可筛选的用户列表（D-Level / 开发者来源列头多选筛选，数值列排序 + 分页）

### 其他
- 深色 / 浅色主题切换
- PC + 移动端响应式布局

## 用户分层标准（D-Level）

| 层级 | 含义 | 判断依据 |
|------|------|----------|
| **D0** | 关注者 | Star 或 Fork 了仓库，但无 Issue/MR 活动 |
| **D1** | 参与者 | 在仓库提过 Issue 或提交过 MR（未合入） |
| **D2** | 贡献者 | 至少有 1 个 MR 被合入 |

同一用户在多个仓库取最高层级。

## 如何增减仓库

所有仓库配置集中在 `config/repos.yml`，**不需要改代码**。

### 添加仓库

在 `repos` 数组中追加一条：

```json
{
  "path": "组织名/仓库名",
  "display_name": "页面显示名",
  "enabled": true,
  "goals": [
    {
      "metric": "star",
      "label": "Star 数",
      "targets": [
        { "label": "2026年上半年达到500", "target": 500 }
      ]
    },
    {
      "metric": "d1",
      "label": "外部D1 开发者数量",
      "targets": [
        { "date": "2026-06-30", "target": 100 }
      ]
    },
    {
      "metric": "d2",
      "label": "外部D2 开发者数量",
      "targets": [
        { "date": "2026-06-30", "target": 10 }
      ]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `path` | GitCode 仓库路径（注意大小写需与 API 返回一致，通常为小写） |
| `display_name` | 前端展示名称 |
| `enabled` | 设为 `false` 可临时隐藏，无需删除配置 |
| `notify` | 设为 `true` 启用该仓库的超期 MR/Issue 邮件通知 |
| `admin` | 该仓库管理员邮箱（接收汇总报告，逗号分隔多人） |
| `wait_feedback` | 设为 `true` 启用自动打 `wait-feedback` 标签（有回复的 issue） |

### 移除仓库

将 `enabled` 设为 `false`，或直接删除该条目。

### 生效方式

- **线上**：将 `config/repos.yml` 的改动推送到 `main` 分支，CI 会自动触发全量采集和部署
- **本地**：修改后运行 `python collector.py all`

> **注意**：`path` 的大小写必须与 GitCode API 返回的 `path_with_namespace` 一致（可先运行 `python collector.py repos` 查看 `data/repos.json` 中的实际值）。

## 如何调整运营目标

编辑 `config/repos.yml` 中对应仓库的 `goals` 数组。Star、D1、D2 都使用同一种结构：

```json
"goals": [
  {
    "metric": "star",
    "label": "Star 数",
    "targets": [
      { "label": "2026年上半年达到500", "target": 500 },
      { "label": "2026年底达到1000", "target": 1000 }
    ]
  },
  {
    "metric": "d1",
    "label": "外部D1 开发者数量",
    "targets": [
      { "date": "2026-06-30", "target": 100 },
      { "date": "2026-09-30", "target": 150 },
      { "date": "2026-12-30", "target": 200 }
    ]
  },
  {
    "metric": "d2",
    "label": "外部D2 开发者数量",
    "targets": [
      { "date": "2026-06-30", "target": 10 },
      { "date": "2026-09-30", "target": 20 },
      { "date": "2026-12-30", "target": 30 }
    ]
  }
]
```

- `metric`：当前支持 `star` / `d1` / `d2`
- `label`：指标名称，显示在仓库详情页
- `targets`：阶段性目标；每个目标需要 `target`，可用 `label` 或 `date` 展示目标节点
- `star` 使用仓库当前 Star 数计算进度；`d1` / `d2` 使用仓库用户列表中排除内部开发者后的外部 D1 / D2 人数计算进度
- 修改后推送到 `main` 即可生效；如果 D-Level 当前人数依赖新增仓库或最新行为数据，CI 会重新采集数据

## 如何维护内部开发者名单

内部开发者名单维护在 `config/internal_developers.txt`，每行一个 GitCode 用户名。仓库用户列表会按用户名精确匹配该名单：

- 命中名单：显示为 `内部开发者`
- 未命中名单：显示为 `外部开发者`

修改 `config/internal_developers.txt` 并推送到 `main` 后，会触发自动采集和部署流程。

> **注意**：网页看板仍使用 `internal_developers.txt` 判定内外。邮件通知则基于 `config/gitcode_2_mail.txt`（私仓注入）：在该文件中有有效邮箱映射的用户视为可通知用户，两列均为 `null` 或不在映射中的用户将汇总发给管理员处理。

## 超期通知

每天 22:00 CST 自动扫描各仓库的超期 MR 和 Issue，通过邮件提醒开发者及时处理。

### 通知规则

| 项目 | MR | Issue |
|---|---|---|
| 条件 | opened + 非草稿(draft) + 超期 10 个工作日以上 | opened + **非Requirement** + 非自提 + 超期 10 个工作日以上 |
| 通知对象 | MR 作者 | Issue 的 assignees（负责人） |
| 升级机制 | 首次提醒本人；之后每日持续提醒直到关闭 | 同 MR |
| 去重 | 按 issue/MR 维度，已通知的不会重复 | 同 MR |
| 工作日 | 排除周末 + 中国法定节假日（`chinese-calendar`） | 同 MR |

### Requirement 判定（不参与非Requirement 超期通知）

Issue 满足以下任一条件视为 Requirement：
- `issue_type == "需求"`（v5 API 直接返回）
- 标题含 `requirement`、`feature` 或 `[rfc]`（不区分大小写）
- labels 含 `requirement` 或 `feature`（不区分大小写）

### 自提 Issue 判定（不发送通知）

满足以下任一条件视为自提，不发送邮件：
- Issue 作者是 assignees 之一
- Issue 关联的 MR 中，有 MR 作者与 Issue 作者为同一人（通过 `e2e_issues` 检测）

### 管理员汇总报告

每天 22:00，`admin_summary.py` 读取 MR 和 Issue 扫描结果，按管理员邮箱合并发送（同一人管理多仓合并为一封），收件人为该仓库的 `admin` 列表。表格按提交人/负责人分组并标注数量，数量降序排列。

汇总报告包含该仓库**所有** open 超期 MR/Issue，每行标注状态：

| 状态 | 含义 |
|---|---|
| 新增 | 首次出现，已向开发者发送个人提醒 |
| 跟踪中 | 已提醒过，开发者仍在处理中 |
| 持续提醒中 | 首次后每日持续提醒 |

### 自动打 wait-feedback 标签

对 `repos.yml` 中 `wait_feedback: true` 的仓库，每日定时扫描 opened 且非 Requirement 的 issue，如果最后一条评论的作者不是 issue 创建者（表示有人回复），则通过 API 自动添加 `wait-feedback` 标签，配合 cannbot 在 14 天后自动关闭已回复的 issue。

需要私仓配置 `gitcode_token_rw.txt`（写权限 token）。

```bash
# 本地测试
python add_wait_feedback_label.py --dry-run
```

### 手动触发

在 GitHub Actions 页面 → `Daily Data Update` → `Run workflow`，可填写参数手动触发通知：

- `run_stale_notify` / `run_issue_notify`：启用 MR / Issue 通知
- `stale_days` / `issue_stale_days`：超期阈值
- `test_email` / `issue_test_email`：测试模式（仅发 1 封样本）
- `admin_report_to`：覆盖管理员邮箱

### 本地测试

```bash
# MR 通知
python stale_mr_notify.py --dry-run
python stale_mr_notify.py --test your_email@example.com

# Issue 通知
python stale_issue_notify.py --dry-run
python stale_issue_notify.py --test your_email@example.com

# 管理员汇总报告
python admin_summary.py --dry-run
python admin_summary.py --test your_email@example.com
```

## 数据采集

```bash
# 全量采集（推荐，自动处理依赖和并发）
python collector.py all

# 或分步执行
python collector.py repos        # 采集仓库基本信息
python collector.py stars        # 采集各仓库 Star 用户列表
python collector.py users        # 采集用户画像（贡献数、仓库数等）
python collector.py activities   # 采集各仓库 MR / Issue 作者
python collector.py forks        # 采集各仓库 Fork 明细
python collector.py issues       # 采集各仓库 Issue 详情
python collector.py mrs          # 采集各仓库 MR 详情
python collector.py reclassify   # 重新分类用户
python collector.py overview     # 生成概览聚合数据
python collector.py users-slim   # 生成前端精简用户数据
python collector.py dlevels      # 生成 D0/D1/D2 汇总
python collector.py weekly       # 生成周粒度活跃度数据
python collector.py discussions  # 采集 GitCode 讨论参与者
python collector.py report       # 输出文字报告
```

`all` 命令按依赖关系分层并发执行，采集效率约为串行的 3~5 倍。

**环境要求**：Python 3.8+，需要 `PyYAML` 和 `chinese-calendar`（CI 自动安装，本地运行需手动安装）。

### 自动更新

CI（GitHub Actions）每天 22:00 CST 自动运行全量采集并提交数据到 `main`，触发 GitHub Pages 部署。同一定时任务也会执行 MR 和 Issue 的超期通知扫描。也可在 Actions 页面手动触发。

## 讨论参与者采集

讨论链接维护在 `config/discussions.yml`：

```json
{
  "discussions": [
    { "url": "https://gitcode.com/org/cann/discussions/85", "enabled": true, "label": "DeepSeek V4 讨论" }
  ]
}
```

- `url`：GitCode 组织讨论链接，格式为 `https://gitcode.com/org/<org>/discussions/<number>`
- `enabled`：可选，默认 `true`；置为 `false` 暂时排除某条讨论
- `label`：可选展示名

`python collector.py discussions` 会抓取每个讨论的顶层评论与回复，按用户名全局去重，排除 `config/internal_developers.txt` 中的内部开发者后，得到“外部讨论参与者”名单。结果写入 `data/discussion_participants.json`，包含名单、参与计数和按日趋势（保留最近 180 天）。首页“参与讨论的外部开发者”区域消费该数据。

## 本地预览

```bash
python -m http.server 8080
# 浏览器打开 http://localhost:8080
```

## 项目结构

```
config/repos.yml          # 仓库配置（增减仓库、设定目标、启用通知）
config/discussions.yml    # 讨论链接配置（外部讨论参与者采集）
config/internal_developers.txt # 内部开发者用户名名单
collector.py              # 数据采集器
stale_mr_notify.py        # 超期 MR 个人通知
stale_issue_notify.py     # 超期 Issue 个人通知
admin_summary.py          # 合并 MR+Issue 管理员汇总报告
add_wait_feedback_label.py # 对有回复的 issue 自动打标签
index.html                # 前端页面（单文件，含所有图表逻辑）
data/                     # 采集数据（自动生成，已纳入版本控制）
  repos.json              # 仓库基本信息（采集输出）
  stars/                  # 各仓库 Star 用户列表
  forks/                  # 各仓库 Fork 明细
  issues/                 # 各仓库 Issue 详情（含 assignees、working_days_open）
  mrs/                    # 各仓库 MR 详情（含 labels、working_days_open）
  stale_mr_notified.json  # MR 通知追踪记录
  stale_issue_notified.json # Issue 通知追踪记录
  dlevel_summary.json     # D0/D1/D2 汇总（前端主数据源）
  discussion_participants.json # 讨论参与者汇总与日级趋势
  ...
.github/workflows/
  update-data.yml         # 每日自动采集 + 超期通知
  deploy.yml              # GitHub Pages 部署
```

## 免责声明

- 本项目仅用于**学习、研究与社区分析**目的，不用于任何商业用途。
- 所有数据均来源于 [gitcode.com](https://gitcode.com) 的公开页面与公开 API，未涉及任何需要登录授权才能访问的私有数据。
- 数据采集遵循合理频率限制，不对目标服务器造成额外负担。
- 本项目展示的用户分层基于公开行为数据的统计推断，**不代表对任何个人的评价**，仅供参考。
- 如相关数据涉及隐私问题或违反平台使用条款，请联系作者删除。
