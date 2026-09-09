import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import issue_response_notify as notify


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
ESCALATION_USERS = ["Mexyy", "m0_50621083", "spring_yb"]


def rules():
    return {"escalation_users": ESCALATION_USERS}


def issue(**overrides):
    value = {
        "repo": "cann/ge", "iid": "7", "title": "bug", "state": "opened",
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


def event(kind="initial", current_issue=None, notification=None):
    current_issue = current_issue or issue()
    token = current_issue["created_at"] if kind == "initial" else "responder-1"
    return {
        "issue": current_issue, "kind": kind, "token": token,
        "event_key": f"{kind}:{token}",
        "waited_hours": 12 if kind == "initial" else 3,
        "notification": notification or {"delivered_users": {}},
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
        classified = notify._classify_waiting_event(issue(), summary, NOW, 12, 3)
        self.assertEqual(classified[0], "initial")

    def test_initial_rule_does_not_fire_before_threshold(self):
        classified = notify._classify_waiting_event(
            issue(created_at="2026-09-07T00:00:01+00:00"),
            notify._comment_summary([], "alice"), NOW, 12, 3,
        )
        self.assertIsNone(classified)

    def test_followup_rule_uses_latest_author_comment_and_three_hour_boundary(self):
        summary = notify._comment_summary([
            comment(1, "bob", "2026-09-07T07:00:00+00:00"),
            comment(2, "alice", "2026-09-07T09:00:00+00:00"),
        ], "alice")
        classified = notify._classify_waiting_event(issue(), summary, NOW, 12, 3)
        self.assertEqual(classified[:2], ("followup", "1"))

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

    def test_recipient_user_matrix(self):
        self.assertEqual(
            notify._required_recipient_users(issue(), "initial", ESCALATION_USERS),
            ["bob", *ESCALATION_USERS],
        )
        self.assertEqual(
            notify._required_recipient_users(issue(), "followup", ESCALATION_USERS),
            ["bob"],
        )
        self.assertEqual(
            notify._required_recipient_users(
                issue(assignees=[]), "followup", ESCALATION_USERS,
            ),
            ESCALATION_USERS,
        )

    def test_recipient_resolution_deduplicates_email_and_reports_missing_user(self):
        by_email, missing = notify._resolve_recipient_users(
            ["bob", "Mexyy", "m0_50621083", "spring_yb"],
            {
                "bob": "shared@example.com", "Mexyy": "shared@example.com",
                "m0_50621083": "hong@example.com",
            },
        )
        self.assertEqual(by_email["shared@example.com"], ["bob", "Mexyy"])
        self.assertEqual(by_email["hong@example.com"], ["m0_50621083"])
        self.assertEqual(missing, ["spring_yb"])

    def test_scan_reuses_comment_summary_when_count_is_unchanged(self):
        cached = {
            "version": 2,
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
            events = notify.scan_events(["cann/ge"], cached, {}, NOW, 12, 3, set())
        fetch_comments.assert_not_called()
        self.assertEqual(len(events), 1)

    def test_satisfied_event_is_returned_so_new_assignee_can_be_checked(self):
        summary = notify._comment_summary([], "alice")
        state = {
            "version": 2,
            "issues": {
                "cann__ge!7": {
                    "repo": "cann/ge", "comment_count": 0, "comments": summary,
                    "notifications": {
                        "initial:2026-09-07T00:00:00+00:00": {
                            "delivered_users": {"bob": {"email": "bob@example.com"}},
                            "satisfied": True,
                        }
                    },
                }
            },
        }
        with patch.object(
            notify, "fetch_open_issues", return_value=[issue(assignees=["carol"])]
        ), patch.object(notify, "fetch_issue_comments") as fetch_comments, \
             patch.object(notify, "fetch_linked_pr_authors", return_value=set()):
            events = notify.scan_events(["cann/ge"], state, {}, NOW, 12, 3, set())
        fetch_comments.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["issue"]["assignees"], ["carol"])

    def test_targeted_scan_does_not_prune_other_issue_state(self):
        state = {"version": 2, "issues": {"cann__ge!9": {"repo": "cann/ge"}}}
        with patch.object(notify, "fetch_issue", return_value=issue(iid="558")), \
             patch.object(notify, "fetch_issue_comments", return_value=[]), \
             patch.object(notify, "fetch_linked_pr_authors", return_value=set()):
            events = notify.scan_events(
                ["cann/ge"], state, {}, NOW, 12, 3, set(), target_iid="558",
            )
        self.assertEqual(len(events), 1)
        self.assertIn("cann__ge!9", state["issues"])

    def test_revalidation_cancels_when_issue_is_closed(self):
        with patch.object(notify, "fetch_issue", return_value=issue(state="closed")), \
             patch.object(notify, "fetch_issue_comments") as fetch_comments:
            self.assertIsNone(notify.revalidate_event(event(), {}, NOW, 12, 3, set()))
        fetch_comments.assert_not_called()

    def test_revalidation_cancels_after_a_new_response(self):
        with patch.object(notify, "fetch_issue", return_value=issue()), \
             patch.object(notify, "fetch_issue_comments", return_value=[
                 comment(1, "bob", "2026-09-07T11:30:00+00:00"),
             ]), patch.object(notify, "fetch_linked_pr_authors", return_value=set()):
            self.assertIsNone(notify.revalidate_event(event(), {}, NOW, 12, 3, set()))

    def test_revalidation_cancels_when_issue_becomes_self_handled(self):
        with patch.object(notify, "fetch_issue", return_value=issue()), \
             patch.object(notify, "fetch_issue_comments", return_value=[]), \
             patch.object(notify, "fetch_linked_pr_authors", return_value={"alice"}):
            self.assertIsNone(notify.revalidate_event(event(), {}, NOW, 12, 3, set()))

    def test_linked_pr_fetch_uses_paged_api(self):
        with patch.object(notify, "_paged_get", return_value=[
            {"user": {"login": "alice"}}, {"user": {"login": "bob"}},
        ]) as paged:
            self.assertEqual(
                notify.fetch_linked_pr_authors("cann/ge", "7", {}),
                {"alice", "bob"},
            )
        self.assertIn("pull_requests", paged.call_args.args[0])

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text("not json", encoding="utf-8")
            with patch.object(notify, "STATE_PATH", state_path):
                with self.assertRaisesRegex(RuntimeError, "避免重复发信"):
                    notify.load_state()

    def test_html_escapes_issue_fields_and_rejects_bad_url(self):
        body = notify.build_html_email(
            issue(title="<script>x</script>", web_url="javascript:x"), "initial", 12,
        )
        self.assertNotIn("<script>", body)
        self.assertNotIn("javascript:", body)
        self.assertIn("&lt;script&gt;", body)

    def test_main_dry_run_never_sends_or_saves_state(self):
        notification = {"delivered_users": {}}
        candidate = event(notification=notification)
        state = {"version": 2, "issues": {}}
        with patch.object(sys, "argv", ["issue_response_notify.py", "--dry-run"]), \
             patch.object(notify, "_load_token", return_value="token"), \
             patch.object(notify, "load_rules_config", return_value=rules()), \
             patch.object(notify, "load_notify_repos", return_value=["cann/ge"]), \
             patch.object(notify, "load_state", return_value=state), \
             patch.object(notify, "scan_events", return_value=[candidate]), \
             patch.object(notify, "_smtp_config_or_none", return_value=None), \
             patch.object(notify, "load_mail_map", return_value={"bob": "bob@example.com"}), \
             patch.object(notify, "send_one_email") as send, \
             patch.object(notify, "save_json") as save:
            self.assertEqual(notify.main(), 0)
        send.assert_not_called()
        save.assert_not_called()
        self.assertEqual(notification, {"delivered_users": {}})

    def test_main_only_sends_to_new_assignee_in_same_event(self):
        notification = {
            "delivered_users": {"bob": {"email": "bob@example.com"}},
            "satisfied": True,
        }
        refreshed = event(
            kind="followup", current_issue=issue(assignees=["carol"]),
            notification=notification,
        )
        state = {"version": 2, "issues": {}}
        with patch.object(sys, "argv", ["issue_response_notify.py"]), \
             patch.object(notify, "_load_token", return_value="token"), \
             patch.object(notify, "load_rules_config", return_value=rules()), \
             patch.object(notify, "load_notify_repos", return_value=["cann/ge"]), \
             patch.object(notify, "load_state", return_value=state), \
             patch.object(notify, "scan_events", return_value=[refreshed]), \
             patch.object(notify, "revalidate_event", return_value=refreshed), \
             patch.object(notify, "_smtp_config_or_none", return_value=object()), \
             patch.object(notify, "load_mail_map", return_value={
                 "bob": "bob@example.com", "carol": "carol@example.com",
             }), patch.object(notify, "_utc_now", return_value=NOW), \
             patch.object(notify, "send_one_email") as send, \
             patch.object(notify, "save_json") as save:
            self.assertEqual(notify.main(), 0)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "carol@example.com")
        self.assertEqual(set(notification["delivered_users"]), {"bob", "carol"})
        self.assertTrue(notification["satisfied"])
        save.assert_called_once()

    def test_main_test_mode_uses_exact_issue_and_does_not_save_state(self):
        notification = {"delivered_users": {}}
        selected = event(current_issue=issue(iid="558"), notification=notification)
        state = {"version": 2, "issues": {}}
        test_email = "hongyuecheng@huawei.com"
        with patch.object(
            sys, "argv", ["issue_response_notify.py", "--test", test_email,
                          "--repo", "cann/ge", "--issue", "558"]
        ), patch.object(notify, "_load_token", return_value="token"), \
             patch.object(notify, "load_rules_config", return_value=rules()), \
             patch.object(notify, "load_notify_repos", return_value=["cann/ge"]), \
             patch.object(notify, "load_state", return_value=state), \
             patch.object(notify, "scan_events", return_value=[selected]) as scan, \
             patch.object(notify, "revalidate_event", return_value=selected), \
             patch.object(notify, "_smtp_config_or_none", return_value=object()), \
             patch.object(notify, "load_mail_map", return_value={}), \
             patch.object(notify, "send_one_email") as send, \
             patch.object(notify, "save_json") as save:
            self.assertEqual(notify.main(), 0)
        self.assertEqual(scan.call_args.kwargs["target_iid"], "558")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], test_email)
        self.assertTrue(send.call_args.args[2].startswith("[TEST] "))
        self.assertIn("#558", send.call_args.args[3])
        save.assert_not_called()
        self.assertEqual(notification, {"delivered_users": {}})

    def test_new_feature_text_does_not_use_old_contact_abbreviation(self):
        root = Path(__file__).resolve().parents[1]
        files = [
            root / "issue_response_notify.py",
            root / "config" / "issue_response_notify.yml",
            root / "README.md",
            root / ".github" / "workflows" / "issue-response-notify.yml",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        old_abbreviation = "w" + "rq"
        self.assertNotIn(old_abbreviation, combined.lower())


if __name__ == "__main__":
    unittest.main()
