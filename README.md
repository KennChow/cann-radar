# CANN GitCode 数据分析

对指定 GitCode 仓库的 Star / Fork / Issue / MR 数据进行采集与可视化分析，跟踪社区用户构成、参与深度和运营目标达成情况。

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

所有仓库配置集中在 `config/repos.json`，**不需要改代码**。

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
| `goals` | 统一运营目标配置，可选，数组可为空 `[]` |

### 移除仓库

将 `enabled` 设为 `false`，或直接删除该条目。

### 生效方式

- **线上**：将 `config/repos.json` 的改动推送到 `main` 分支，CI 会自动触发全量采集和部署
- **本地**：修改后运行 `python collector.py all`

> **注意**：`path` 的大小写必须与 GitCode API 返回的 `path_with_namespace` 一致（可先运行 `python collector.py repos` 查看 `data/repos.json` 中的实际值）。

## 如何调整运营目标

编辑 `config/repos.json` 中对应仓库的 `goals` 数组。Star、D1、D2 都使用同一种结构：

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
python collector.py report       # 输出文字报告
```

`all` 命令按依赖关系分层并发执行，采集效率约为串行的 3~5 倍。

**环境要求**：Python 3.8+，无需第三方依赖。

### 自动更新

CI（GitHub Actions）每天 22:00 CST 自动运行全量采集并提交数据到 `main`，触发 GitHub Pages 部署。也可在 Actions 页面手动触发。

## 本地预览

```bash
python -m http.server 8080
# 浏览器打开 http://localhost:8080
```

## 项目结构

```
config/repos.json        # 仓库配置（增减仓库、设定目标）
config/internal_developers.txt # 内部开发者用户名名单
collector.py             # 数据采集器
index.html               # 前端页面（单文件，含所有图表逻辑）
data/                    # 采集数据（自动生成，已纳入版本控制）
  repos.json             # 仓库基本信息
  stars/                 # 各仓库 Star 用户列表
  forks/                 # 各仓库 Fork 明细
  issues/                # 各仓库 Issue 详情
  mrs/                   # 各仓库 MR 详情
  dlevel_summary.json    # D0/D1/D2 汇总（前端主数据源）
  ...
.github/workflows/
  update-data.yml        # 每日自动采集
  deploy.yml             # GitHub Pages 部署
```

## 免责声明

- 本项目仅用于**学习、研究与社区分析**目的，不用于任何商业用途。
- 所有数据均来源于 [gitcode.com](https://gitcode.com) 的公开页面与公开 API，未涉及任何需要登录授权才能访问的私有数据。
- 数据采集遵循合理频率限制，不对目标服务器造成额外负担。
- 本项目展示的用户分层基于公开行为数据的统计推断，**不代表对任何个人的评价**，仅供参考。
- 如相关数据涉及隐私问题或违反平台使用条款，请联系作者删除。
