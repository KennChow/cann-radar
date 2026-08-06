import json
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import stale_issue_notify as issue_notify
import stale_mr_notify as mr_notify


class NotifyLogicTests(unittest.TestCase):
    def test_linked_issue_self_assignment_is_repo_scoped(self):
        linked = {('org/repo-a', '7'): {'alice'}}
        issue = {'iid': 7, 'author': 'alice', 'assignees': ['bob']}
        self.assertTrue(issue_notify._is_self_assigned(issue, 'org/repo-a', linked))
        self.assertFalse(issue_notify._is_self_assigned(issue, 'org/repo-b', linked))

    def test_issue_threshold_is_inclusive(self):
        issue = {'iid': 1, 'state': 'opened', 'author': 'alice',
                 'assignees': ['bob'], 'created_at': '2026-01-01',
                 'working_days_open': 10, 'title': 'bug', 'labels': []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'org__repo.json').write_text(json.dumps([issue]), encoding='utf-8')
            with patch.object(issue_notify, 'ISSUES_DIR', path):
                matched, _ = issue_notify.scan_stale_issues(10, {'org/repo'}, {})
        self.assertEqual([item['iid'] for item in matched], [1])

    def test_same_day_never_resends(self):
        today = date(2026, 8, 4)
        notified = {'key': {'notified_at': '2026-08-04T08:00:00', 'count': 2}}
        self.assertFalse(issue_notify._check_issue_notify_status('key', notified, today)[0])
        self.assertFalse(mr_notify._check_mr_notify_status('key', notified, today)[0])

    def test_test_mode_does_not_mutate_mr_tracking(self):
        data = {'notified': {}}
        authors = {'alice': ('alice@example.com', [{
            'repo': 'org/repo', 'iid': 1, 'title': 'bug', 'days_open': 10,
            'web_url': 'https://example.com/1', 'labels': [], 'notify_stage': 1,
        }])}
        args = Namespace(dry_run=False, test='test@example.com', stale_days=10)
        with patch.object(mr_notify, 'send_one_email'):
            sent, failed = mr_notify._send_personal_emails(authors, object(), data, args)
        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual(data['notified'], {})

    def test_issue_tracking_requires_all_email_recipients(self):
        issue = {'repo': 'org/repo', 'iid': 1}
        key = issue_notify._issue_key('org/repo', 1)
        required = {key: {'alice', 'bob'}}
        self.assertEqual(
            issue_notify._fully_delivered_issues([issue], required, {key: {'alice'}}), [])
        self.assertEqual(
            issue_notify._fully_delivered_issues(
                [issue], required, {key: {'alice', 'bob'}}), [issue])

    def test_email_html_escapes_external_content_and_rejects_bad_url(self):
        issue = {
            'repo': '<repo>', 'iid': 1,
            'title': '<script>alert(1)</script>', 'days_open': 10,
            'web_url': 'javascript:alert(1)', 'labels': ['<img>'],
            'notify_stage': 1,
        }
        rendered = issue_notify.build_html_email('<alice>', [issue], 10)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('javascript:', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('达到 10 个工作日', rendered)


    def test_scan_preserves_author_for_self_assignment_filter(self):
        issue = {
            "iid": 7, "state": "opened", "author": "alice",
            "assignees": ["alice"], "created_at": "2026-08-01",
            "working_days_open": 1, "title": "bug", "labels": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "org__repo.json").write_text(
                json.dumps([issue]), encoding="utf-8")
            with patch.object(issue_notify, "ISSUES_DIR", path), \
                 patch.object(issue_notify, "_working_days_since", return_value=10):
                matched, _ = issue_notify.scan_stale_issues(10, {"org/repo"}, {})
        self.assertEqual(matched[0]["author"], "alice")
        self.assertTrue(issue_notify._is_self_assigned(
            matched[0], matched[0]["repo"], {}))

    def test_notification_ignores_frozen_cached_working_days(self):
        issue = {
            "iid": 1, "state": "opened", "author": "alice",
            "assignees": ["bob"], "created_at": "2026-08-01",
            "working_days_open": 1, "title": "bug", "labels": [],
        }
        mr = {
            "iid": 2, "state": "opened", "draft": False, "author": "alice",
            "created_at": "2026-08-01", "working_days_open": 1,
            "title": "fix", "labels": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issues_dir, mrs_dir = root / "issues", root / "mrs"
            issues_dir.mkdir(); mrs_dir.mkdir()
            (issues_dir / "org__repo.json").write_text(json.dumps([issue]), encoding="utf-8")
            (mrs_dir / "org__repo.json").write_text(json.dumps([mr]), encoding="utf-8")
            with patch.object(issue_notify, "ISSUES_DIR", issues_dir), \
                 patch.object(issue_notify, "_working_days_since", return_value=10):
                issues, _ = issue_notify.scan_stale_issues(10, {"org/repo"}, {})
            with patch.object(mr_notify, "MRS_DIR", mrs_dir), \
                 patch.object(mr_notify, "_working_days_since", return_value=10):
                mrs, _ = mr_notify.scan_stale_mrs(10, {"org/repo"}, {})
        self.assertEqual(issues[0]["days_open"], 10)
        self.assertEqual(mrs["alice"][0]["days_open"], 10)


if __name__ == '__main__':
    unittest.main()
