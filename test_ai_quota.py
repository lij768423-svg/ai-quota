import builtins
import http.client
import io
import json
import os
import queue
import re
import socket
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

import ai_quota

NOW = 1_800_000_000.0


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write((record if isinstance(record, str) else json.dumps(record)) + "\n")
    os.utime(path, (NOW, NOW))


def token_count(at, primary=None, secondary=None, total=None, last=None, plan="plus"):
    payload = {"type": "token_count"}
    if primary is not None:
        payload["rate_limits"] = {
            "limit_id": "codex",
            "plan_type": plan,
            "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": int(NOW + 3600)},
            "secondary": {
                "used_percent": secondary,
                "window_minutes": 10080,
                "resets_at": int(NOW + 86400),
            },
        }
    if total is not None:
        payload["info"] = {"total_token_usage": total, "last_token_usage": last}
    return {"timestamp": iso(at), "type": "event_msg", "payload": payload}


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


class MergeWindowTest(unittest.TestCase):
    def test_stale_lower_value_does_not_win(self):
        current = ai_quota.merge_window(None, 42, 1000, 10)
        self.assertEqual(ai_quota.merge_window(current, 39, 1000, 20)["used_percent"], 42)

    def test_reset_jitter_counts_as_same_window(self):
        current = ai_quota.merge_window(None, 42, 10000, 10)
        self.assertEqual(ai_quota.merge_window(current, 39, 10060, 20)["used_percent"], 42)

    def test_new_window_replaces_higher_old_value(self):
        current = ai_quota.merge_window(None, 90, 1000, 10)
        merged = ai_quota.merge_window(current, 3, 20000, 20)
        self.assertEqual((merged["used_percent"], merged["resets_at"]), (3, 20000))

    def test_older_window_is_ignored_in_any_order(self):
        current = ai_quota.merge_window(None, 3, 20000, 20)
        self.assertEqual(ai_quota.merge_window(current, 90, 1000, 10)["used_percent"], 3)

    def test_touch_refreshes_time_without_changing_value(self):
        current = ai_quota.merge_window(None, 42, 1000, 10)
        merged = ai_quota.merge_window(current, 42, 1000, 30, touch=True)
        self.assertEqual((merged["used_percent"], merged["seen_at"]), (42, 30))
        self.assertEqual(ai_quota.merge_window(current, 42, 1000, 30)["seen_at"], 10)

    def test_missing_fields_are_ignored(self):
        self.assertIsNone(ai_quota.merge_window(None, None, 1000, 10))
        self.assertIsNone(ai_quota.merge_window(None, 5, None, 10))


class CodexTest(TempHome):
    def path(self, name):
        return os.path.join(self.home, ".codex", "sessions", "2026", name)

    def test_takes_highest_percent_and_latest_time(self):
        write_jsonl(
            self.path("a.jsonl"),
            [
                token_count(NOW - 300, primary=42, secondary=80),
                token_count(NOW - 200, primary=39, secondary=79),
                "not json {",
                {"timestamp": iso(NOW), "payload": {"type": "agent_message", "text": "token_count"}},
            ],
        )
        limits, _ = ai_quota.read_codex(self.home, NOW)
        windows = limits["codex"]["windows"]
        self.assertEqual(limits["codex"]["plan"], "plus")
        self.assertEqual(windows["5 小时"]["used_percent"], 42)
        self.assertEqual(windows["每周"]["used_percent"], 80)
        self.assertEqual(windows["5 小时"]["seen_at"], NOW - 200)

    def test_tokens_skip_cache_and_repeated_totals(self):
        last = {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 50}
        total = {"total_tokens": 1050}
        write_jsonl(
            self.path("a.jsonl"),
            [
                token_count(NOW - 300, total=total, last=last),
                token_count(NOW - 200, total=total, last=last),
            ],
        )
        _, events = ai_quota.read_codex(self.home, NOW)
        self.assertEqual(events, [(NOW - 300, 150)])

    def test_forked_session_does_not_double_count(self):
        last = {"input_tokens": 100, "output_tokens": 10}
        event = token_count(NOW - 300, total={"total_tokens": 110}, last=last)
        write_jsonl(self.path("a.jsonl"), [event])
        write_jsonl(self.path("b.jsonl"), [event])
        _, events = ai_quota.read_codex(self.home, NOW)
        self.assertEqual(len(events), 1)

    def test_plan_change_drops_old_plan_windows(self):
        upgraded = token_count(NOW - 100, primary=21, plan="prolite")
        upgraded["payload"]["rate_limits"]["primary"] = {
            "used_percent": 21,
            "window_minutes": 10080,
            "resets_at": int(NOW + 5 * 86400),
        }
        upgraded["payload"]["rate_limits"]["secondary"] = None
        # 新套餐写在先读到的文件里，旧套餐写在后读到的文件里，结果不能受读取顺序影响。
        write_jsonl(self.path("a.jsonl"), [upgraded])
        write_jsonl(self.path("b.jsonl"), [token_count(NOW - 3600, primary=18, secondary=82)])
        limits, _ = ai_quota.read_codex(self.home, NOW)
        self.assertEqual(limits["codex"]["plan"], "prolite")
        self.assertEqual(list(limits["codex"]["windows"]), ["每周"])
        self.assertEqual(limits["codex"]["windows"]["每周"]["used_percent"], 21)
        codex = next(s for s in ai_quota.collect(home=self.home, now=NOW)["quota"] if s["service"] == "codex")
        self.assertEqual((codex["plan"], codex["label"]), ("Pro", "Codex Pro"))

    def test_plan_names(self):
        self.assertEqual(ai_quota.codex_plan_name("prolite"), "Pro")
        self.assertEqual(ai_quota.codex_plan_name("plus"), "Plus")
        self.assertIsNone(ai_quota.codex_plan_name(None))

    def test_files_older_than_seven_days_are_skipped(self):
        path = self.path("old.jsonl")
        write_jsonl(path, [token_count(NOW - 300, primary=42, secondary=80)])
        old = NOW - ai_quota.SEVEN_DAYS - 10
        os.utime(path, (old, old))
        self.assertEqual(ai_quota.read_codex(self.home, NOW)[0], {})


class ClaudeTokensTest(TempHome):
    def test_dedupes_by_message_id_across_files(self):
        def assistant(message_id, output, at):
            return {
                "type": "assistant",
                "timestamp": iso(at),
                "message": {
                    "id": message_id,
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 100000,
                        "output_tokens": output,
                    },
                },
            }

        root = os.path.join(self.home, ".claude", "projects", "p")
        write_jsonl(os.path.join(root, "a.jsonl"), [assistant("m1", 1, NOW - 100), assistant("m1", 20, NOW - 100)])
        write_jsonl(os.path.join(root, "sub", "b.jsonl"), [assistant("m1", 20, NOW - 100), assistant("m2", 5, NOW - 50)])
        events = ai_quota.read_claude_tokens(self.home, NOW)
        self.assertEqual(sorted(t for _, t in events), [20, 35])


class GrokTokensTest(TempHome):
    def test_each_turn_counts_once_without_cache(self):
        def turn(prompt_id, at):
            return {
                "timestamp": at,
                "params": {
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "prompt_id": prompt_id,
                        "usage": {"inputTokens": 1000, "cachedReadTokens": 900, "outputTokens": 10},
                    }
                },
            }

        path = os.path.join(self.home, ".grok", "sessions", "s", "id", "updates.jsonl")
        write_jsonl(path, [turn("a", NOW - 100), turn("a", NOW - 100), turn("b", (NOW - 50) * 1000)])
        events = ai_quota.read_grok_tokens(self.home, NOW)
        self.assertEqual(sorted(events), [(NOW - 100, 110), (NOW - 50, 110)])


class GrokLimitsTest(TempHome):
    def path(self):
        return os.path.join(self.home, ".grok", "logs", "unified.jsonl")

    def event(self, at, percent, start, end, tier="SuperGrok", msg="billing: fetched credits config"):
        return {
            "ts": iso(at),
            "msg": msg,
            "ctx": {
                "subscriptionTier": tier,
                "config": {
                    "creditUsagePercent": percent,
                    "currentPeriod": {
                        "type": "USAGE_PERIOD_TYPE_WEEKLY",
                        "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                        "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
                    },
                },
            },
        }

    def test_logged_out_records_are_skipped_and_latest_period_wins(self):
        old_end = NOW + 3600
        new_end = NOW + 5 * 86400
        write_jsonl(
            self.path(),
            [
                self.event(NOW - 200, None, old_end - 7 * 86400, old_end, tier="Free"),
                self.event(NOW - 100, 17.0, new_end - 7 * 86400, new_end),
                self.event(NOW - 50, 14.0, new_end - 7 * 86400, new_end),
                self.event(NOW - 40, None, old_end - 7 * 86400, old_end, tier="Free"),
                "not json fetched credits config",
            ],
        )
        limits = ai_quota.read_grok_limits(self.home, NOW)
        window = limits["windows"]["每周"]
        self.assertEqual(limits["plan"], "SuperGrok")
        self.assertEqual(window["used_percent"], 17.0)
        self.assertEqual(window["resets_at"], int(new_end))
        self.assertEqual(window["seen_at"], NOW - 50)

    def test_other_messages_with_same_words_are_ignored(self):
        end = NOW + 86400
        write_jsonl(self.path(), [self.event(NOW - 10, 50.0, end - 7 * 86400, end, msg="not fetched credits config")])
        self.assertEqual(ai_quota.read_grok_limits(self.home, NOW)["windows"], {})

    def test_report_uses_tier_as_label_and_shows_quota(self):
        end = NOW + 86400
        write_jsonl(self.path(), [self.event(NOW - 10, 17.0, end - 7 * 86400, end)])
        report = ai_quota.collect(home=self.home, now=NOW)
        grok = next(s for s in report["quota"] if s["service"] == "grok")
        self.assertEqual(grok["label"], "SuperGrok")
        self.assertIsNone(grok["note"])
        self.assertEqual(grok["windows"][0]["window_seconds"], ai_quota.SEVEN_DAYS)
        self.assertIn("SuperGrok", ai_quota.render(report))

    def test_labels_and_period_names(self):
        self.assertEqual(ai_quota.grok_label(None), "Grok")
        self.assertEqual(ai_quota.grok_label("Pro"), "Grok Pro")
        self.assertEqual(ai_quota.grok_window_name(0, 30 * 86400), "30 天")
        self.assertEqual(ai_quota.grok_window_name(None, 100), "当前周期")


class ClaudePlanTest(TempHome):
    def setUp(self):
        super().setUp()
        ai_quota._claude_plan_cache.update(checked_at=None, plan=None)
        self.addCleanup(ai_quota._claude_plan_cache.update, checked_at=None, plan=None)

    def result(self, stdout):
        return mock.Mock(stdout=stdout)

    def test_temporary_home_never_runs_real_command(self):
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(ai_quota.read_claude_plan(NOW, self.home))
        run.assert_not_called()

    def test_keeps_only_plan_and_caches_for_an_hour(self):
        output = json.dumps({"email": "person@example.com", "orgId": "x", "subscriptionType": "max"})
        with mock.patch("ai_quota.os.path.expanduser", return_value=self.home), mock.patch(
            "subprocess.run", return_value=self.result(output)
        ) as run:
            self.assertEqual(ai_quota.read_claude_plan(NOW, self.home), "max")
            self.assertEqual(ai_quota.read_claude_plan(NOW + 3599, self.home), "max")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args[0][0][1:], ["auth", "status"])
            ai_quota.read_claude_plan(NOW + 3601, self.home)
            self.assertEqual(run.call_count, 2)
        self.assertNotIn("person@example.com", json.dumps(ai_quota._claude_plan_cache))

    def test_failures_mean_no_plan(self):
        with mock.patch("ai_quota.os.path.expanduser", return_value=self.home):
            for side_effect in (OSError("missing"), subprocess.TimeoutExpired("claude", 5)):
                ai_quota._claude_plan_cache.update(checked_at=None, plan=None)
                with mock.patch("subprocess.run", side_effect=side_effect):
                    self.assertIsNone(ai_quota.read_claude_plan(NOW, self.home))
            for output in ("not json", "[]"):
                ai_quota._claude_plan_cache.update(checked_at=None, plan=None)
                with mock.patch("subprocess.run", return_value=self.result(output)):
                    self.assertIsNone(ai_quota.read_claude_plan(NOW, self.home))

    def test_report_shows_plan_as_badge_and_label(self):
        with mock.patch("ai_quota.read_claude_plan", return_value="max"):
            report = ai_quota.collect(home=self.home, now=NOW)
        claude = report["quota"][0]
        self.assertEqual((claude["name"], claude["plan"], claude["label"]), ("Claude", "Max", "Claude Max"))

    def test_report_without_plan_has_plain_name(self):
        claude = ai_quota.collect(home=self.home, now=NOW)["quota"][0]
        self.assertEqual((claude["name"], claude["plan"], claude["label"]), ("Claude", None, "Claude"))


class AntigravityTest(TempHome):
    def log(self, lines):
        path = os.path.join(self.home, "Library", "Logs", "Antigravity", "language_server.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def tier_line(self, tier, display):
        return 'I1107 auth_provider.go:1273] [AuthProvider] SetUserTier called with userTier: "{}", tierDisplayName: "{}"'.format(tier, display)

    def service(self):
        return [s for s in ai_quota.collect(home=self.home, now=NOW)["quota"] if s["service"] == "antigravity"]

    def test_hidden_when_not_installed(self):
        self.assertEqual(self.service(), [])

    def test_empty_tier_shows_no_plan_and_honest_note(self):
        self.log([self.tier_line("", ""), "other line"])
        service = self.service()[0]
        self.assertIsNone(service["plan"])
        self.assertEqual(service["windows"], [])
        self.assertIn("agy", service["note"])

    def test_latest_real_tier_wins_over_later_empty(self):
        self.log(
            [
                self.tier_line("free-tier", "Free"),
                self.tier_line("g1-pro-tier", "Google AI Pro"),
                self.tier_line("", ""),
            ]
        )
        service = self.service()[0]
        self.assertEqual(service["plan"], "Google AI Pro")
        self.assertEqual(service["label"], "Antigravity Google AI Pro")

    def test_folder_without_log_still_lists_service(self):
        os.makedirs(os.path.join(self.home, "Library", "Logs", "Antigravity"))
        self.assertIsNone(self.service()[0]["plan"])


def agy_output(gemini_left=1.0, argon_left=0.9586):
    """按 `agy -p /usage --output-format json` 的真实结构写的样例，多加了一个不该被保存的账号字段。"""
    week = iso(NOW + 6 * 86400)
    five = iso(NOW + 3 * 3600)
    return json.dumps(
        {
            "conversation_id": "",
            "status": "SUCCESS",
            "response": "Gemini Models\tWeekly Limit Remaining\t100%\n",
            "account": "person@example.com",
            "command": {
                "name": "usage",
                "data": {
                    "description": "Within each group, models share a weekly limit and a 5-hour limit.",
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": gemini_left, "reset_time": week},
                                {"id": "gemini-5h", "window": "5h", "remaining_fraction": 1, "reset_time": five},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.25, "reset_time": week},
                                {"id": "3p-5h", "window": "5h", "status": "disabled"},
                            ],
                        },
                        {
                            "name": "Gemini 4 Argon",
                            "buckets": [
                                {"id": "argon-weekly", "window": "weekly", "remaining_fraction": argon_left, "reset_time": week},
                            ],
                        },
                    ],
                },
            },
        }
    )


class AgyUsageTest(TempHome):
    def runner(self, *outputs):
        calls = []
        queue_ = list(outputs)

        def run():
            calls.append(1)
            return queue_.pop(0) if queue_ else None

        return run, calls

    def test_parse_groups_windows_and_used_percent(self):
        groups = ai_quota.parse_agy_usage(agy_output(), NOW)
        self.assertEqual([g["name"] for g in groups], ["Gemini", "Claude 和 GPT", "Gemini 4 Argon"])
        self.assertEqual(groups[0]["windows"]["每周"]["used_percent"], 0)
        self.assertEqual(groups[0]["windows"]["5 小时"]["resets_at"], int(NOW + 3 * 3600))
        self.assertEqual(list(groups[1]["windows"]), ["每周"])
        self.assertEqual(groups[1]["windows"]["每周"]["used_percent"], 75)
        self.assertEqual(groups[2]["windows"]["每周"]["used_percent"], 4.1)
        self.assertEqual(groups[2]["windows"]["每周"]["seen_at"], NOW)

    def test_bad_output_is_none(self):
        for text in ("", "not json", "[]", '{"command": {"data": {}}}'):
            self.assertIsNone(ai_quota.parse_agy_usage(text, NOW))

    def test_queries_once_per_max_age_and_saves_only_numbers(self):
        run, calls = self.runner(agy_output(), agy_output(gemini_left=0.5))
        groups, can_query = ai_quota.read_antigravity_usage(self.home, NOW, 300, run)
        self.assertTrue(can_query)
        ai_quota.read_antigravity_usage(self.home, NOW + 299, 300, run)
        self.assertEqual(len(calls), 1)
        groups, _ = ai_quota.read_antigravity_usage(self.home, NOW + 300, 300, run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(groups[0]["windows"]["每周"]["used_percent"], 50)
        with open(ai_quota.state_file(self.home, "antigravity.json"), encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("person@example.com", raw)
        self.assertNotIn("SUCCESS", raw)
        self.assertEqual(set(json.loads(raw)), {"fetched_at", "attempted_at", "groups"})

    def test_failure_keeps_last_result_and_waits_before_retry(self):
        run, calls = self.runner(agy_output(), None)
        ai_quota.read_antigravity_usage(self.home, NOW, 300, run)
        groups, _ = ai_quota.read_antigravity_usage(self.home, NOW + 400, 300, run)
        self.assertEqual(groups[2]["name"], "Gemini 4 Argon")
        ai_quota.read_antigravity_usage(self.home, NOW + 500, 300, run)
        self.assertEqual(len(calls), 2)
        state = ai_quota.load_json(ai_quota.state_file(self.home, "antigravity.json"))
        self.assertEqual((state["fetched_at"], state["attempted_at"]), (NOW, NOW + 400))

    def test_temporary_home_never_runs_real_command(self):
        with mock.patch("ai_quota.run_agy_usage", side_effect=AssertionError("ran agy")):
            self.assertEqual(ai_quota.read_antigravity_usage(self.home, NOW), (None, False))

    def test_fresh_report_uses_shorter_max_age(self):
        with mock.patch("ai_quota.read_antigravity_usage", return_value=(None, False)) as read:
            ai_quota.collect(home=self.home, now=NOW)
            ai_quota.collect(home=self.home, now=NOW, fresh=True)
        self.assertEqual(
            [c.args[2] for c in read.call_args_list],
            [ai_quota.ANTIGRAVITY_REFRESH_SECONDS, ai_quota.ANTIGRAVITY_FRESH_SECONDS],
        )

    def test_report_lists_groups_in_order(self):
        run, _ = self.runner(agy_output())
        ai_quota.read_antigravity_usage(self.home, NOW, 300, run)
        report = ai_quota.collect(home=self.home, now=NOW + 60)
        service = next(s for s in report["quota"] if s["service"] == "antigravity")
        self.assertEqual(
            [(w["group"], w["name"]) for w in service["windows"]],
            [("Gemini", "5 小时"), ("Gemini", "每周"), ("Claude 和 GPT", "每周"), ("Gemini 4 Argon", "每周")],
        )
        self.assertEqual(service["windows"][1]["window_seconds"], ai_quota.SEVEN_DAYS)
        self.assertIsNone(service["note"])
        text = ai_quota.render(report)
        self.assertIn("Claude 和 GPT 每周", text)
        self.assertIn("75%", text)

    def test_other_services_have_no_group(self):
        write_jsonl(
            os.path.join(self.home, ".codex", "sessions", "a.jsonl"),
            [token_count(NOW - 300, primary=10, secondary=20)],
        )
        codex = next(s for s in ai_quota.collect(home=self.home, now=NOW)["quota"] if s["service"] == "codex")
        self.assertEqual({w["group"] for w in codex["windows"]}, {None})


class StatuslineTest(TempHome):
    def run_with(self, payload, now=NOW):
        stream = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        return ai_quota.run_statusline(home=self.home, now=now, stream=stream)

    def payload(self, five=None, seven=None):
        limits = {}
        if five is not None:
            limits["five_hour"] = {"used_percentage": five, "resets_at": int(NOW + 3600)}
        if seven is not None:
            limits["seven_day"] = {"used_percentage": seven, "resets_at": int(NOW + 86400)}
        return {
            "session_id": "secret-session",
            "cwd": "/private/project",
            "transcript_path": "/private/transcript.jsonl",
            "rate_limits": limits,
        }

    def test_records_only_numbers(self):
        self.assertEqual(self.run_with(self.payload(23.5, 41.2)), "5h 24% · 7d 41%")
        with open(ai_quota.state_path(self.home), encoding="utf-8") as handle:
            raw = handle.read()
        for private in ("secret-session", "/private"):
            self.assertNotIn(private, raw)
        self.assertEqual(set(json.loads(raw)), {"five_hour", "seven_day"})

    def test_partial_payload_keeps_other_window(self):
        self.run_with(self.payload(10, 20))
        self.assertEqual(self.run_with(self.payload(five=11)), "5h 11% · 7d 20%")

    def test_stale_session_does_not_lower_value_or_refresh_time(self):
        self.run_with(self.payload(50, 60), now=NOW)
        self.run_with(self.payload(40, 55), now=NOW + 300)
        state = ai_quota.load_claude_state(self.home)
        self.assertEqual(state["five_hour"]["used_percent"], 50)
        self.assertEqual(state["five_hour"]["seen_at"], NOW)

    def test_unchanged_value_does_not_rewrite_file(self):
        self.run_with(self.payload(50, 60))
        path = ai_quota.state_path(self.home)
        os.utime(path, (1000, 1000))
        self.run_with(self.payload(50, 60), now=NOW + 300)
        self.assertEqual(os.path.getmtime(path), 1000)

    def test_bad_input_never_raises(self):
        self.assertEqual(self.run_with("not json"), "")
        self.assertEqual(self.run_with("[]"), "")
        self.assertEqual(self.run_with({"rate_limits": None}), "")
        self.assertEqual(self.run_with({"rate_limits": {"five_hour": "x"}}), "")

    def test_expired_window_is_hidden(self):
        self.run_with(self.payload(50, 60))
        self.assertEqual(self.run_with({}, now=NOW + 7200), "7d 60%")


class ReportTest(TempHome):
    def test_empty_home_explains_missing_data(self):
        text = ai_quota.render(ai_quota.collect(home=self.home, now=NOW))
        self.assertIn("暂无额度数据", text)
        self.assertIn("Codex：近 7 天没有额度记录", text)
        self.assertIn("Grok：近 7 天没有额度记录", text)

    def write_config(self, data):
        path = ai_quota.config_path(self.home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def test_no_local_models_without_config(self):
        report = ai_quota.collect(home=self.home, now=NOW)
        self.assertEqual([s for s in report["quota"] if s["service"] == "local"], [])

    def test_local_models_from_config_are_unlimited(self):
        self.write_config(
            {
                "local_models": [
                    {"name": "Llama Box", "detail": "my-server"},
                    {"name": " Mini "},
                    {"name": ""},
                    "not a model",
                    {"detail": "no name"},
                ]
            }
        )
        report = ai_quota.collect(home=self.home, now=NOW)
        local = [s for s in report["quota"] if s["service"] == "local"]
        self.assertEqual(
            [(s["name"], s["plan"], s["unlimited"], s["detail"]) for s in local],
            [("Llama Box", "本地", True, "my-server"), ("Mini", "本地", True, None)],
        )
        self.assertEqual(local[0]["windows"], [])
        self.assertFalse(any(s["unlimited"] for s in report["quota"] if s["service"] != "local"))
        text = ai_quota.render(report)
        self.assertIn("Llama Box：本地模型（my-server），不限额度", text)
        self.assertIn("Mini：本地模型，不限额度", text)

    def test_broken_config_is_ignored(self):
        for data in ("not json", "[]", '{"local_models": {"name": "x"}}'):
            self.write_config(data)
            report = ai_quota.collect(home=self.home, now=NOW)
            self.assertEqual([s for s in report["quota"] if s["service"] == "local"], [])

    def test_expired_window_says_reset_not_old_percent(self):
        self.addCleanup(lambda: None)
        path = os.path.join(self.home, ".codex", "sessions", "a.jsonl")
        write_jsonl(path, [token_count(NOW - 300, primary=42, secondary=80)])
        report = ai_quota.collect(home=self.home, now=NOW + 7200)
        codex = next(s for s in report["quota"] if s["service"] == "codex")
        self.assertEqual(codex["label"], "Codex Plus")
        self.assertEqual([w["expired"] for w in codex["windows"]], [True, False])
        text = ai_quota.render(report)
        self.assertIn("已重置", text)
        self.assertNotIn("42%", text)
        self.assertIn("80%", text)

    def test_rolling_windows(self):
        last = {"input_tokens": 1000, "output_tokens": 0}
        write_jsonl(
            os.path.join(self.home, ".codex", "sessions", "a.jsonl"),
            [
                token_count(NOW - 100, total={"total_tokens": 1}, last=last),
                token_count(NOW - ai_quota.FIVE_HOURS - 100, total={"total_tokens": 2}, last=last),
                token_count(NOW - ai_quota.SEVEN_DAYS - 100, total={"total_tokens": 3}, last=last),
            ],
        )
        report = ai_quota.collect(home=self.home, now=NOW)
        codex = next(t for t in report["tokens"] if t["service"] == "codex")
        self.assertEqual((codex["five_hours"], codex["seven_days"]), (1000, 2000))

    def test_color_does_not_change_alignment_inputs(self):
        write_jsonl(
            os.path.join(self.home, ".codex", "sessions", "a.jsonl"),
            [token_count(NOW - 300, primary=96, secondary=80)],
        )
        report = ai_quota.collect(home=self.home, now=NOW)
        colored = ai_quota.render(report, color=True)
        self.assertIn("\033[31m", colored)
        self.assertIn("\033[33m", colored)
        self.assertEqual(colored.replace("\033[31m", "").replace("\033[33m", "").replace("\033[0m", ""), ai_quota.render(report))

    def test_never_opens_credential_files(self):
        for relative in (".codex/auth.json", ".claude/.credentials.json", ".grok/auth.json"):
            path = os.path.join(self.home, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
        opened = []
        real_open = builtins.open

        def spy(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", spy):
            ai_quota.collect(home=self.home, now=NOW)
        self.assertFalse([p for p in opened if "auth" in p or "credential" in p])


class CachedParseTest(TempHome):
    def test_reuses_result_until_file_changes(self):
        path = os.path.join(self.home, "log.jsonl")
        write_jsonl(path, ["a"])
        calls = []

        def parser(_):
            calls.append(1)
            return len(calls)

        self.assertEqual(ai_quota.cached_parse(path, parser), 1)
        self.assertEqual(ai_quota.cached_parse(path, parser), 1)
        write_jsonl(path, ["a", "b"])
        self.assertEqual(ai_quota.cached_parse(path, parser), 2)

    def test_second_read_sees_new_log_lines(self):
        path = os.path.join(self.home, ".codex", "sessions", "a.jsonl")
        write_jsonl(path, [token_count(NOW - 300, primary=10, secondary=20)])
        self.assertEqual(ai_quota.read_codex(self.home, NOW)[0]["codex"]["windows"]["5 小时"]["used_percent"], 10)
        write_jsonl(
            path,
            [
                token_count(NOW - 300, primary=10, secondary=20),
                token_count(NOW - 100, primary=30, secondary=25),
            ],
        )
        self.assertEqual(ai_quota.read_codex(self.home, NOW)[0]["codex"]["windows"]["5 小时"]["used_percent"], 30)


class ReportStoreTest(unittest.TestCase):
    def test_caches_until_max_age_or_fresh(self):
        calls = []

        fresh_flags = []

        def fake_collect(home=None, now=None, fresh=False):
            calls.append(now)
            fresh_flags.append(fresh)
            return {"generated_at": now}

        with mock.patch("ai_quota.collect", fake_collect):
            store = ai_quota.ReportStore(max_age=1000)
            store.get()
            store.get()
            self.assertEqual(len(calls), 1)
            store.get(fresh=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(fresh_flags, [False, True])
            expired = ai_quota.ReportStore(max_age=-1)
            expired.get()
            expired.get()
            self.assertEqual(len(calls), 4)


class UiPageTest(unittest.TestCase):
    def test_report_cannot_break_out_of_script_tag(self):
        page = ai_quota.render_ui_page({"x": "</script><script>alert(1)</script>"})
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("__REPORT_JSON__", page)

    def test_page_loads_nothing_external(self):
        page = ai_quota.render_ui_page({})
        self.assertNotRegex(page, r"https?://")


class UiServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        ready = queue.Queue()
        cls.thread = threading.Thread(
            target=ai_quota.serve_ui,
            kwargs={
                "home": cls._tmp.name,
                "port": 0,
                "idle": 2,
                "check_every": 0.05,
                "on_ready": ready.put,
            },
        )
        cls.thread.start()
        cls.port = ready.get(timeout=5)

    @classmethod
    def tearDownClass(cls):
        # 闲置退出本身就是被测行为：没有请求后服务要自己停下。
        cls.thread.join(timeout=10)
        alive = cls.thread.is_alive()
        cls._tmp.cleanup()
        assert not alive, "server did not exit after idling"

    def request(self, path, host=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or "127.0.0.1:{}".format(self.port)}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return response, body

    def test_page_has_embedded_report_and_strict_policy(self):
        response, body = self.request("/")
        self.assertEqual(response.status, 200)
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        embedded = re.search(r'<script id="report" type="application/json">(.*?)</script>', body, re.S)
        self.assertIn("quota", json.loads(embedded.group(1)))

    def test_localhost_host_is_allowed(self):
        response, _ = self.request("/api/health", host="localhost:{}".format(self.port))
        self.assertEqual(response.status, 200)

    def test_report_endpoint(self):
        response, body = self.request("/api/report?fresh=1")
        self.assertEqual(response.status, 200)
        self.assertEqual({"generated_at", "quota", "tokens"}, set(json.loads(body)))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_foreign_host_is_refused(self):
        response, body = self.request("/api/report", host="evil.example:{}".format(self.port))
        self.assertEqual(response.status, 403)
        self.assertNotIn("quota", body)

    def test_no_cors_header(self):
        response, _ = self.request("/api/report")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_unknown_path_and_writes_are_rejected(self):
        self.assertEqual(self.request("/nope")[0].status, 404)
        self.assertEqual(self.request("/api/report", method="POST")[0].status, 501)

    def test_running_check(self):
        self.assertTrue(ai_quota.ui_is_running(self.port))


class UiCheckTest(unittest.TestCase):
    def test_closed_port_is_not_running(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(ai_quota.ui_is_running(port))


class FormatTest(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(ai_quota.format_tokens(999), "999")
        self.assertEqual(ai_quota.format_tokens(1500), "1.5K")
        self.assertEqual(ai_quota.format_tokens(2_500_000), "2.5M")
        self.assertEqual(ai_quota.format_tokens(3_100_000_000), "3.1B")

    def test_percent_rounds_half_up_like_the_page(self):
        self.assertEqual(
            [ai_quota.format_percent(v) for v in (4.5, 23.5, 41.2, 0, 99.6)],
            ["5%", "24%", "41%", "0%", "100%"],
        )

    def test_ago(self):
        self.assertEqual(ai_quota.format_ago(5), "刚刚")
        self.assertEqual(ai_quota.format_ago(120), "2 分钟前")
        self.assertEqual(ai_quota.format_ago(7200), "2 小时前")
        self.assertEqual(ai_quota.format_ago(172800), "2 天前")

    def test_cjk_padding(self):
        self.assertEqual(ai_quota.display_width("5 小时"), 6)
        self.assertEqual(ai_quota.pad("每周", 6), "每周  ")

    def test_reset_labels(self):
        noon = datetime(2026, 11, 6, 12, 0).timestamp()
        self.assertEqual(ai_quota.format_reset(datetime(2026, 11, 6, 22, 26).timestamp(), noon), "今天 22:26")
        self.assertEqual(ai_quota.format_reset(datetime(2026, 11, 7, 1, 0).timestamp(), noon), "明天 01:00")
        self.assertEqual(ai_quota.format_reset(datetime(2026, 11, 8, 18, 36).timestamp(), noon), "周日 18:36")
        self.assertEqual(ai_quota.format_reset(datetime(2026, 11, 20, 9, 0).timestamp(), noon), "11-20 09:00")


if __name__ == "__main__":
    unittest.main()
