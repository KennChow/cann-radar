import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import issue_response_notify as notify


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def issue(**overrides):
    value = {
        "repo": "cann/ge", "iid": 7, "title": "bug", "state": "opened",
        "author": "alice", "created_at": "2026-09-07T00:00:00+00:00",
        "assignees": ["bob"], "comment_count": 0,
        "web_url": "https://gitcode.com/cann/ge/issues/7",
    }
    value.update(overrides)
    return value


def comment(cid, author, created_at, user_type="User"):
    return {
        "id": cid, "created_at": created_at,
        "user": {"login": author, "type": user_type},
    }


class IssueResponseNotifyTests(unittest.TestCase):
    def test_self_handled_by_assignee_or_linked_pr_author(self):
        self.assertTrue(notify._is_self_handled(issue(assignees=["alice"]), set()))
        self.assertTrue(notify._is_self_handled(issue(), {"alice"}))
        self.assertFalse(notify._is_self_handled(issue(), {"charlie"}))

    def test_initial_rule_is_inclusive_and_author_comments_do_not_respond(self):
        summary = notify._comment_summary([
            comment(1, "alice", "2026-09-07T10:00:00+00:00"),
        ], "alice")
        event = notify._classify_waiting_event(issue(), summary, NOW, 12, 3)
        self.assertEqual(event[0], "initial")

    def test_initial_rule_does_not_fire_before_threshold(self):
        event = notify._classify_waiting_event(
            issue(created_at="2026-09-07T00:00:01+00:00"),
            notify._comment_summary([], "alice"), NOW, 12, 3,
        )
        self.assertIsNone(event)

    def test_followup_rule_uses_latest_author_comment_and_three_hour_boundary(self):
        summary = notify._comment_summary([
            comment(1, "bob", "2026-09-07T07:00:00+00:00"),
            comment(2, "alice", "2026-09-07T09:00:00+00:00"),
        ], "alice")
        event = notify._classify_waiting_event(issue(), summary, NOW, 12, 3)
        self.assertEqual(event[:2], ("followup", "1"))

    def test_followup_does_not_fire_when_responder_is_latest(self):
        summary = notify._comment_summary([
            comment(1, "alice", "2026-09-07T07:00:00+00:00"),
            comment(2, "bob", "2026-09-07T08:00:00+00:00"),
        ], "alice")
        self.assertIsNone(notify._classify_waiting_event(issue(), summary, NOW, 12, 3))

    def test_bot_comment_is_ignored(self):
        summary = notify._comment_summary([
            comment(1, "service", "2026-09-07T08:00:00+00:00", "Bot"),
        ], "alice")
        self.assertFalse(summary["has_non_author_response"])
        self.assertEqual(summary["latest_comment_author"], "")

    def test_consecutive_author_comments_share_one_wait_round(self):
        first = notify._comment_summary([
            comment(10, "bob", "2026-09-07T01:00:00+00:00"),
            comment(11, "alice", "2026-09-07T08:00:00+00:00"),
        ], "alice")
        second = notify._comment_summary([
            comment(10, "bob", "2026-09-07T01:00:00+00:00"),
            comment(11, "alice", "2026-09-07T08:00:00+00:00"),
            comment(12, "alice", "2026-09-07T09:00:00+00:00"),
        ], "alice")
        self.assertEqual(
            notify._classify_waiting_event(issue(), first, NOW, 12, 3)[1],
            notify._classify_waiting_event(issue(), second, NOW, 12, 3)[1],
        )

    def test_recipient_matrix(self):
        mail_map = {"bob": "bob@example.com"}
        escalation = ["xgz@example.com", "hyc@example.com", "wrq@example.com"]
        initial, missing = notify._event_recipients(issue(), "initial", mail_map, escalation)
        self.assertEqual(initial, ["bob@example.com"] + escalation)
        self.assertEqual(missing, [])
        followup, _ = notify._event_recipients(issue(), "followup", mail_map, escalation)
        self.assertEqual(followup, ["bob@example.com"])
        unassigned, _ = notify._event_recipients(
            issue(assignees=[]), "followup", mail_map, escalation,
        )
        self.assertEqual(unassigned, escalation)

    def test_scan_reuses_comment_summary_when_count_is_unchanged(self):
        cached = {
            "version": 1,
            "issues": {
                "cann__ge!7": {
                    "repo": "cann/ge", "comment_count": 0,
                    "comments": notify._comment_summary([], "alice"),
                }
            },
        }
        with patch.object(notify, "fetch_open_issues", return_value=[issue()]), \
             patch.object(notify, "fetch_issue_comments") as fetch_comments, \
             patch.object(notify, "fetch_linked_pr_authors", return_value=set()):
            events = notify.scan_events(
                ["cann/ge"], cached, {}, NOW, 12, 3, set(),
            )
        fetch_comments.assert_not_called()
        self.assertEqual(len(events), 1)

    def test_completed_event_is_not_returned_again(self):
        summary = notify._comment_summary([], "alice")
        state = {
            "version": 1,
            "issues": {
                "cann__ge!7": {
                    "repo": "cann/ge", "comment_count": 0, "comments": summary,
                    "notifications": {
                        "initial:2026-09-07T00:00:00+00:00": {
                            "delivered": ["xgz@example.com"], "completed": True,
                        }
                    },
                }
            },
        }
        with patch.object(notify, "fetch_open_issues", return_value=[issue()]), \
             patch.object(notify, "fetch_issue_comments") as fetch_comments, \
             patch.object(notify, "fetch_linked_pr_authors", return_value=set()):
            events = notify.scan_events(["cann/ge"], state, {}, NOW, 12, 3, set())
        fetch_comments.assert_not_called()
        self.assertEqual(events, [])

    def test_html_escapes_issue_fields_and_rejects_bad_url(self):
        body = notify.build_html_email(
            issue(title="<script>x</script>", web_url="javascript:x"), "initial", 12,
        )
        self.assertNotIn("<script>", body)
        self.assertNotIn("javascript:", body)
        self.assertIn("&lt;script&gt;", body)


if __name__ == "__main__":
    unittest.main()
