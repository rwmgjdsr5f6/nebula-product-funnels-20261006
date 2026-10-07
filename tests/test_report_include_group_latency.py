"""--include-group-latency 组内转化耗时开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> --group-by visit-date
                              --include-group-latency [--include-latency]
                              [--within-seconds N]

仅使用 Python 3 标准库；每个场景使用独立临时目录中的 JSONL 与 SQLite，
不依赖预存数据库、网络或第三方包，测试结束不遗留任何数据库文件。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验收样例：七条事件，时间均为 2026 年 10 月的 UTC。
# u1：6 日 23:59:00、7 日 10:00:00、10:00:30 各 visit 一次，7 日 10:01:00
#     signup（归 6 日组；最早有效 signup 10:01:00 配对最晚可配对 visit
#     10:00:30，耗时 30 秒；配对 visit 在 7 日也不改变归组）
# u2：6 日 12:00:00 visit、12:01:00 signup（归 6 日组，耗时 60 秒）
# u3：仅 7 日 11:00:00 visit（归 7 日组，不转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:30"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T12:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T12:01:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T11:00:00"},
]
GROUP_6_LATENCY = {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}
NULL_LATENCY = {"min_seconds": None, "max_seconds": None, "mean_seconds": None}

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (6, 0, 5, 2, 1, 4, 3)]

# 混入完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:30"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T12:01:00"},
]

# 组内均值不取整样例：u1 耗时 30 秒、u2 耗时 61 秒，同归 6 日组，均值 45.5。
MEAN_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

GROUP_BASE_KEYS = {
    "visit_date",
    "visit_users",
    "converted_users",
    "conversion_rate",
}
GROUP_LATENCY_KEYS = GROUP_BASE_KEYS | {"conversion_latency"}
LATENCY_FIELD_KEYS = {"min_seconds", "max_seconds", "mean_seconds"}


def run_funnel(*cli_args):
    """在项目目录执行 python -m funnel，返回完成的进程结果。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "funnel", *cli_args],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class IncludeGroupLatencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助函数 ---------------------------------------------------------

    def write_jsonl(self, name, events):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def db_path(self, name="events.sqlite"):
        return os.path.join(self.tmpdir, name)

    def import_events(self, events, db=None, jsonl_name="events.jsonl"):
        if db is None:
            db = self.db_path()
        path = self.write_jsonl(jsonl_name, events)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        return db

    def report(self, db, *extra_args):
        return run_funnel("report", "--db", db, *extra_args)

    @staticmethod
    def parse_single_json_object(stdout):
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        assert nonempty_lines, "标准输出为空，无法解析 JSON 对象: %r" % stdout
        assert len(nonempty_lines) == 1, "标准输出包含多行，不是单个 JSON 对象: %r" % stdout
        obj = json.loads(nonempty_lines[0])
        assert isinstance(obj, dict), "标准输出不是 JSON 对象: %r" % stdout
        return obj

    def assertGroupLatency(self, payload, expected):
        """expected 为 (visit_date, converted_users, latency_dict) 元组列表。

        同时核对：组对象键集合恰为基础四键加 conversion_latency；耗时对象
        只含三个字段；字段类型（min/max 为 int 或 None，mean 为 float 或
        None）与取值相符。
        """
        groups = payload["visit_date_groups"]
        self.assertEqual(len(groups), len(expected))
        for group, (day, converted, latency) in zip(groups, expected):
            self.assertEqual(set(group), GROUP_LATENCY_KEYS)
            self.assertEqual(group["visit_date"], day)
            self.assertEqual(group["converted_users"], converted)
            self.assertEqual(set(group["conversion_latency"]), LATENCY_FIELD_KEYS)
            self.assertEqual(group["conversion_latency"], latency)
            if converted == 0:
                self.assertIs(group["conversion_latency"]["min_seconds"], None)
                self.assertIs(group["conversion_latency"]["max_seconds"], None)
                self.assertIs(group["conversion_latency"]["mean_seconds"], None)
            else:
                self.assertIsInstance(
                    group["conversion_latency"]["min_seconds"], int
                )
                self.assertIsInstance(
                    group["conversion_latency"]["max_seconds"], int
                )
                self.assertIsInstance(
                    group["conversion_latency"]["mean_seconds"], float
                )

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_group_latency_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-latency",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 汇总：3 人访问、2 人转化。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 2)
        # 新开关不自动开启顶层耗时或其他明细。
        self.assertNotIn("conversion_latency", payload)
        self.assertNotIn("conversion_pairs", payload)
        self.assertNotIn("visit_user_ids", payload)
        # 6 日：u1 贡献 30 秒、u2 贡献 60 秒，min=30、max=60、mean=45；
        # 7 日：仅 u3 访问且不转化，三项均为 null。
        self.assertGroupLatency(
            payload,
            [
                ("2026-10-06", 2, GROUP_6_LATENCY),
                ("2026-10-07", 0, NULL_LATENCY),
            ],
        )

    def test_acceptance_pairing_visit_may_be_next_day(self):
        # 配对 visit 落在归组日期的后一天，耗时仍计入 6 日组，不重新归组。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        days = [g["visit_date"] for g in payload["visit_date_groups"]]
        self.assertEqual(days, ["2026-10-06", "2026-10-07"])
        self.assertEqual(
            payload["visit_date_groups"][0]["conversion_latency"],
            {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0},
        )
        self.assertEqual(
            payload["visit_date_groups"][1]["conversion_latency"], NULL_LATENCY
        )

    # -- 开关独立性 -------------------------------------------------------

    def test_group_latency_does_not_auto_enable_top_level_latency(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-latency"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertNotIn("conversion_latency", payload)
        for group in payload["visit_date_groups"]:
            self.assertEqual(set(group), GROUP_LATENCY_KEYS)

    def test_with_top_level_latency_both_appear(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-group-latency",
                "--include-latency",
            ).stdout
        )
        # 两个转化用户都在 6 日组，故顶层与 6 日组耗时对象一致。
        self.assertEqual(payload["conversion_latency"], GROUP_6_LATENCY)
        self.assertGroupLatency(
            payload,
            [
                ("2026-10-06", 2, GROUP_6_LATENCY),
                ("2026-10-07", 0, NULL_LATENCY),
            ],
        )

    def test_top_level_latency_alone_adds_nothing_inside_groups(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--include-latency",
                "--group-by", "visit-date",
                "--include-users",
                "--include-group-pairs",
            ).stdout
        )
        self.assertIn("conversion_latency", payload)
        for group in payload["visit_date_groups"]:
            self.assertNotIn("conversion_latency", group)
            self.assertNotIn("min_seconds", group)

    def test_without_flag_groups_have_no_latency_field(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        for extra in (
            ("--group-by", "visit-date"),
            ("--group-by", "visit-date", "--include-latency"),
            ("--group-by", "visit-date", "--include-pairs"),
            ("--group-by", "visit-date", "--include-users"),
            ("--group-by", "visit-date", "--include-group-pairs"),
        ):
            with self.subTest(extra=extra):
                result = self.report(db, *extra, "--within-seconds", "60")
                self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
                payload = self.parse_single_json_object(result.stdout)
                for group in payload["visit_date_groups"]:
                    self.assertNotIn("conversion_latency", group)
                    self.assertNotIn("min_seconds", group)

    def test_metrics_identical_with_and_without_flag(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        plain = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--within-seconds", "60"
            ).stdout
        )
        detailed = self.parse_single_json_object(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-group-latency",
                "--within-seconds", "60",
            ).stdout
        )
        for key in ("visit_users", "converted_users", "conversion_rate"):
            self.assertEqual(detailed[key], plain[key])
        for plain_group, detailed_group in zip(
            plain["visit_date_groups"], detailed["visit_date_groups"]
        ):
            for key in GROUP_BASE_KEYS:
                self.assertEqual(detailed_group[key], plain_group[key])

    # -- 组内耗时语义 -----------------------------------------------------

    def test_group_latency_not_shortest_pair(self):
        # 口径同顶层：最早有效 signup 配对最晚可配对 visit，不取最短间隔。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:01:59"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
        ]
        db = self.import_events(events, jsonl_name="pick.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        self.assertGroupLatency(
            payload,
            [
                (
                    "2026-10-06",
                    1,
                    {"min_seconds": 30, "max_seconds": 30, "mean_seconds": 30.0},
                )
            ],
        )

    def test_group_latency_unrounded_mean(self):
        db = self.import_events(MEAN_EVENTS, jsonl_name="mean.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        latency = payload["visit_date_groups"][0]["conversion_latency"]
        self.assertEqual(latency, {"min_seconds": 30, "max_seconds": 61,
                                   "mean_seconds": 45.5})
        self.assertIsInstance(latency["min_seconds"], int)
        self.assertIsInstance(latency["max_seconds"], int)
        self.assertIsInstance(latency["mean_seconds"], float)

    def test_group_latency_within_seconds_boundary(self):
        # 上界含等值：60 秒窗口下 u1 30 秒、u2 60 秒都计入；
        # 30 秒窗口下只剩 u1（30 秒），u2 被排除。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-group-latency",
                "--within-seconds", "30",
            ).stdout
        )
        self.assertGroupLatency(
            payload,
            [
                ("2026-10-06", 1,
                 {"min_seconds": 30, "max_seconds": 30, "mean_seconds": 30.0}),
                ("2026-10-07", 0, NULL_LATENCY),
            ],
        )

    def test_group_latency_visit_window(self):
        # 段外访问不参与归组与配对；注册允许晚于终点。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T00:00:00"},
        ]
        db = self.import_events(events, jsonl_name="window.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-group-latency",
                "--visit-from", "2026-10-06T00:00:00",
                "--visit-before", "2026-10-07T00:00:00",
            ).stdout
        )
        # 2026-10-06 23:59:00 -> 2026-10-08 00:00:00 为 86460 秒。
        self.assertGroupLatency(
            payload,
            [
                ("2026-10-06", 1,
                 {"min_seconds": 86460, "max_seconds": 86460,
                  "mean_seconds": 86460.0}),
            ],
        )

    # -- 不变量与空结果 ---------------------------------------------------

    def test_shuffled_and_duplicate_events_give_same_group_latency(self):
        for name, events in (
            ("shuffled.jsonl", SHUFFLED_EVENTS),
            ("dupes.jsonl", EVENTS_WITH_DUPLICATES),
        ):
            with self.subTest(jsonl=name):
                db = self.import_events(events, jsonl_name=name)
                payload = self.parse_single_json_object(
                    self.report(
                        db,
                        "--group-by", "visit-date",
                        "--include-group-latency",
                        "--within-seconds", "60",
                    ).stdout
                )
                self.assertGroupLatency(
                    payload,
                    [
                        ("2026-10-06", 2, GROUP_6_LATENCY),
                        ("2026-10-07", 0, NULL_LATENCY),
                    ],
                )

    def test_reimport_same_batch_keeps_group_latency(self):
        path = self.write_jsonl("group.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-group-latency",
                "--within-seconds", "60",
            ).stdout
        )
        self.assertGroupLatency(
            payload,
            [
                ("2026-10-06", 2, GROUP_6_LATENCY),
                ("2026-10-07", 0, NULL_LATENCY),
            ],
        )

    def test_no_qualifying_visits_gives_empty_groups(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-latency"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_date_groups"], [])

    # -- 错误协议与只读性 -------------------------------------------------

    def test_flag_without_group_by_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        before = self.snapshot_events(db)
        result = self.report(db, "--include-group-latency", "--within-seconds", "60")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误指出新开关及依赖项。
        self.assertIn("--include-group-latency", result.stderr)
        self.assertIn("--group-by", result.stderr)
        self.assertEqual(self.snapshot_events(db), before)

    def test_flag_without_group_by_does_not_create_db(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-group-latency")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--include-group-latency", result.stderr)
        self.assertIn("--group-by", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_missing_db_still_follows_path_error_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-latency"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--group-by", "visit-date", "--include-group-latency")
        self.report(
            db,
            "--group-by", "visit-date",
            "--include-group-latency",
            "--include-latency",
            "--include-group-pairs",
            "--include-pairs",
            "--include-users",
            "--within-seconds", "60",
        )
        self.report(db, "--include-group-latency")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
