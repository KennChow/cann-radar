import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DashboardScopeTests(unittest.TestCase):
    def test_repo_config_contains_requested_repos_and_unified_goals(self):
        cfg = json.loads((ROOT / 'config' / 'repos.json').read_text(encoding='utf-8'))
        repos = cfg['repos']
        enabled_paths = [repo['path'] for repo in repos if repo.get('enabled', True)]
        self.assertEqual(enabled_paths, [
            'cann/ge',
            'cann/hixl',
            'cann/graph-autofusion',
            'cann/torchtitan-npu',
            'Ascend/torchair',
        ])
        for repo in repos:
            self.assertNotIn('operation_metrics', repo)
            for goal in repo['goals']:
                self.assertIn('metric', goal)
                self.assertIn('label', goal)
                self.assertIn('targets', goal)
                self.assertIsInstance(goal['targets'], list)
                self.assertGreater(len(goal['targets']), 0)
                self.assertNotIn('target', goal)

        goals = {
            repo['path']: {goal['metric']: goal for goal in repo['goals']}
            for repo in repos
        }
        self.assertEqual([t['target'] for t in goals['cann/ge']['star']['targets']], [500, 1000])
        self.assertEqual(goals['cann/hixl']['star']['targets'][0]['target'], 700)
        self.assertEqual(goals['Ascend/torchair']['star']['targets'][0]['target'], 700)
        self.assertEqual(goals['Ascend/torchair']['d1']['targets'][0]['target'], 100)
        self.assertEqual(goals['cann/ge']['d1']['targets'][1]['target'], 130)
        self.assertEqual(goals['cann/graph-autofusion']['d2']['targets'][2]['target'], 30)
        self.assertEqual(goals['cann/hixl']['d1']['targets'][1]['target'], 150)
        self.assertEqual(goals['cann/torchtitan-npu']['d1']['targets'][0]['target'], 100)
        for repo_goals in goals.values():
            self.assertEqual(repo_goals['d1']['label'], '外部D1 开发者数量')
            self.assertEqual(repo_goals['d2']['label'], '外部D2 开发者数量')

    def test_repos_json_only_contains_requested_repos(self):
        repos = json.loads((ROOT / 'data' / 'repos.json').read_text(encoding='utf-8'))
        paths = [repo['path'] for repo in repos]
        self.assertEqual(paths, [
            'cann/ge',
            'cann/hixl',
            'cann/graph-autofusion',
            'cann/torchtitan-npu',
            'Ascend/torchair',
        ])

    def test_collector_uses_config_file_for_repo_scope(self):
        collector = (ROOT / 'collector.py').read_text(encoding='utf-8')
        self.assertIn('config/repos.json', collector)
        self.assertIn('load_repo_config', collector)
        self.assertIn('active_repo_paths', collector)

    def test_dashboard_reads_goals_from_config_data_not_hardcoded_constant(self):
        html = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertIn("loadJSON('config/repos.json')", html)
        self.assertNotIn('const REPO_GOALS = {', html)
        self.assertIn('repoConfigMap', html)
        self.assertNotIn('operation_metrics', html)
        self.assertIn('getGoalMetricValue', html)
        self.assertIn('externalTypeCounts', html)
        self.assertIn("getDeveloperSource(user) === 'external'", html)
        self.assertIn("d1: { current: externalTypeCounts.d1 || 0, display: '外部D1'", html)
        self.assertIn("d2: { current: externalTypeCounts.d2 || 0, display: '外部D2'", html)
        self.assertIn('formatMetricTargetLabel', html)
        self.assertIn('运营目标', html)

    def test_internal_developers_list_is_gitignored(self):
        # 名单通过私仓 / CI Secret 注入；本地可存在，但必须被 gitignore
        gitignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('config/internal_developers.txt', gitignore)

    def test_data_update_workflows_inject_private_list(self):
        gh = (ROOT / '.github' / 'workflows' / 'update-data.yml').read_text(encoding='utf-8')
        self.assertIn('cann-radar-private', gh)
        self.assertIn('PRIVATE_REPO_PAT', gh)
        self.assertIn('repository_dispatch', gh)

        gc = (ROOT / '.gitcode' / 'workflows' / 'update-data.yml').read_text(encoding='utf-8')
        self.assertIn('cann-radar-private', gc)
        self.assertIn('PRIVATE_REPO_PAT', gc)

    def test_dashboard_supports_user_source_filters_and_sorting(self):
        html = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn("loadText('config/internal_developers.txt')", html)
        self.assertIn("user.developer_source", html)
        self.assertIn('开发者来源', html)
        self.assertIn('filter-level', html)
        self.assertIn('filter-developer-source', html)
        self.assertNotIn('filter-username', html)
        self.assertNotIn('filter-nickname', html)
        self.assertNotIn('filter-reset', html)
        self.assertIn('sortUsers', html)
        self.assertIn('data-sort-key="fans_count"', html)
        self.assertIn('data-sort-key="original_repo_count"', html)
        self.assertIn('data-sort-key="total_contributions"', html)
        self.assertIn('data-sort-key="starred_repo_count"', html)
        self.assertIn('data-sort-key="star_time"', html)

    def test_dashboard_uses_d_level_labels_and_meaning_text(self):
        html = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertIn('D0 关注者', html)
        self.assertIn('D1 贡献者', html)
        self.assertIn('D2 PR贡献者', html)
        self.assertIn('Star / Watch / Fork 仓库', html)
        self.assertIn('提交 Issue / 评论 issue / 提交 PR', html)
        self.assertIn('至少合入了 1 个 PR', html)

    def test_dlevel_summary_contains_expected_structure(self):
        summary = json.loads((ROOT / 'data' / 'dlevel_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(summary['global_counts'].keys()), ['d0', 'd1', 'd2', 'total'])
        self.assertEqual(sorted(summary['repo_counts'].keys()), [
            'Ascend/torchair',
            'cann/ge',
            'cann/graph-autofusion',
            'cann/hixl',
            'cann/torchtitan-npu',
        ])
        self.assertIn('repo_users', summary)
        self.assertIn('star_timeline', summary)


if __name__ == '__main__':
    unittest.main()
