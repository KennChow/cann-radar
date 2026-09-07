import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collection_failure_notify
import collector


class DataSafetyTests(unittest.TestCase):
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
        response = (root / '.github/workflows/issue-response-notify.yml').read_text(encoding='utf-8')
        gitcode = (root / '.gitcode/workflows/update-data.yml').read_text(encoding='utf-8')
        self.assertIn('needs: test', update)
        self.assertIn('needs: test', deploy)
        self.assertIn('needs: test', response)
        self.assertIn('python -m pytest -q', response)
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
