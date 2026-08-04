import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import add_wait_feedback_label as wait_feedback
import collection_failure_notify
import collector


class DataSafetyTests(unittest.TestCase):
    def test_comments_are_paginated_and_latest_is_selected_by_time(self):
        first = [
            {'id': i, 'created_at': f'2026-01-01T00:{i:02d}:00Z'}
            for i in range(2)
        ]
        second = [{'id': 3, 'created_at': '2026-01-02T00:00:00Z'}]
        with patch.object(wait_feedback, 'api_get', side_effect=[first, second]) as get:
            comments, error = wait_feedback.fetch_all_comments(
                'org', 'repo', 1, 'token', per_page=2,
            )
        self.assertIsNone(error)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(wait_feedback.latest_comment(comments)['id'], 3)

    def test_comment_api_failure_is_not_treated_as_no_comments(self):
        with patch.object(wait_feedback, 'api_get', return_value=None):
            comments, error = wait_feedback.fetch_all_comments('org', 'repo', 1, 'token')
        self.assertEqual(comments, [])
        self.assertIn('请求失败', error)

    def test_issue_pagination_failure_does_not_write_partial_cache(self):
        page = [{'number': i, 'state': 'opened'} for i in range(100)]
        repo = {'path': 'org/repo'}
        with tempfile.TemporaryDirectory() as tmp:
            issues_dir = Path(tmp)
            with patch.object(collector, '_load_token', return_value='token'), \
                 patch.object(collector, 'get', side_effect=[page, None]):
                with self.assertRaises(RuntimeError):
                    collector._fetch_repo_issues(repo, issues_dir)
            self.assertFalse((issues_dir / 'org__repo.json').exists())

    def test_collector_json_writes_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'data.json'
            target.write_text('{"old": true}', encoding='utf-8')
            collector.save_json(target, {'new': True})
            self.assertEqual(collector.load_json(target), {'new': True})
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_workflows_run_tests_before_mutating_or_deploying(self):
        root = Path(__file__).resolve().parents[1]
        update = (root / '.github/workflows/update-data.yml').read_text(encoding='utf-8')
        deploy = (root / '.github/workflows/deploy.yml').read_text(encoding='utf-8')
        gitcode = (root / '.gitcode/workflows/update-data.yml').read_text(encoding='utf-8')
        self.assertIn('needs: test', update)
        self.assertIn('needs: test', deploy)
        self.assertIn('python -m pytest -q', gitcode)

    def test_legacy_admin_email_builders_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        mr = (root / 'stale_mr_notify.py').read_text(encoding='utf-8')
        issue = (root / 'stale_issue_notify.py').read_text(encoding='utf-8')
        self.assertNotIn('def build_admin_report_html', mr)
        self.assertNotIn('def build_admin_report_html', issue)


    def test_failed_repo_restores_last_good_data_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, fallback = root / "data", root / "last-good"
            target = current / "issues" / "org__repo.json"
            old = fallback / "issues" / "org__repo.json"
            old.parent.mkdir(parents=True)
            old.write_text('[{"iid": 1}]', encoding="utf-8")
            failures = current / "collection_failures.json"
            with patch.object(collector, "DATA_DIR", current), \
                 patch.object(collector, "FALLBACK_DATA_DIR", fallback), \
                 patch.object(collector, "COLLECTION_FAILURES_PATH", failures):
                restored = collector.restore_fallback(
                    target, "issues", "org/repo", RuntimeError("timeout"))
            self.assertEqual(restored, [{"iid": 1}])
            self.assertTrue(collector.load_json(failures)[0]["fallback_used"])

    def test_wait_feedback_skips_failed_issue_repos(self):
        data = [{"category": "issues", "repo": "org/repo"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "failures.json"
            path.write_text(__import__("json").dumps(data), encoding="utf-8")
            with patch.object(wait_feedback, "COLLECTION_FAILURES_PATH", path):
                self.assertEqual(wait_feedback.load_failed_issue_repos(), {"org/repo"})

    def test_failure_alert_defaults_to_requested_recipient(self):
        self.assertEqual(collection_failure_notify.DEFAULT_RECIPIENT,
                         "caoqiancheng@huawei.com")
        rendered = collection_failure_notify.build_html([{
            "repo": "<repo>", "category": "issues", "error": "<timeout>",
            "fallback_used": True,
        }])
        self.assertIn("&lt;repo&gt;", rendered)
        self.assertNotIn("<timeout>", rendered)


if __name__ == '__main__':
    unittest.main()
