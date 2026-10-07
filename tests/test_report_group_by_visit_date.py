"""--group-by visit-date 分组漏斗报告的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]
                              [--visit-from ... --visit-before ...]
                              [--group-by visit-date]

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

# 验收样例：六条事件，日期均在 2026 年 10 月（UTC）。
# u1：6 日 10:00:00 与 7 日 10:00:00 各 visit 一次，7 日 10:00:30 signup
#     （归组用最早合格 visit 即 6 日；转化可用 7 日那次 visit，间隔 30 秒）
# u2：仅 6 日 11:00:00 visit（不转化）
# u3：7 日 12:00:00 同时刻 visit 与 signup（相等不算转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"},
]

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (4, 0, 5, 2, 3, 1)]

# 主样例中混入若干完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

EXPECTED_GROUP_KEYS = {
    "visit_date",
    "visit_users",
    "converted_users",
    "conversion_rate",
}


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


class FunnelGroupByVisitDateTests(unittest.TestCase):
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

    def assertGroups(self, payload, expected):
        """expected 为 (visit_date, visit_users, converted_users, rate) 元组列表。"""
        groups = payload["visit_date_groups"]
        self.assertIsInstance(groups, list)
        for group in groups:
            self.assertEqual(set(group), EXPECTED_GROUP_KEYS)
        actual = [
            (g["visit_date"], g["visit_users"], g["converted_users"], g["conversion_rate"])
            for g in groups
        ]
        self.assertEqual(actual, list(expected))
        # 日期按升序排列，且各组两种人数之和分别等于汇总人数。
        dates = [g["visit_date"] for g in groups]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(
            sum(g["visit_users"] for g in groups), payload["visit_users"]
        )
        self.assertEqual(
            sum(g["converted_users"] for g in groups), payload["converted_users"]
        )

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收场景 ---------------------------------------------------------

    def test_acceptance_group_by_visit_date_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 汇总：3 人访问、1 人转化、比例 1/3。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)
        # u1 归 6 日组（最早合格 visit），其转化计入该组；u3 归 7 日组但不转化。
        self.assertGroups(
            payload,
            [
                ("2026-10-06", 2, 1, 0.5),
                ("2026-10-07", 1, 0, 0.0),
            ],
        )

    def test_acceptance_window_only_seventh(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--within-seconds",
            "60",
            "--visit-from",
            "2026-10-07T00:00:00",
            "--visit-before",
            "2026-10-08T00:00:00",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 段外（6 日）历史不影响归组：u1 归 7 日组，u2 无合格访问不归任何组。
        self.assertGroups(payload, [("2026-10-07", 2, 1, 0.5)])

    def test_no_group_by_leaves_output_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--within-seconds", "60")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(
            set(payload), {"visit_users", "converted_users", "conversion_rate"}
        )

    # -- 分组语义 ---------------------------------------------------------

    def test_signup_only_db_gives_empty_groups(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(db, "--group-by", "visit-date")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_date_groups"], [])

    def test_shuffled_rows_give_same_groups(self):
        db = self.import_events(SHUFFLED_EVENTS, jsonl_name="shuffled.jsonl")
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0.0)],
        )

    def test_duplicate_events_give_same_groups(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0.0)],
        )

    def test_reimport_same_batch_appends_without_changing_groups(self):
        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        first = run_funnel("import", path, "--db", db)
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        second = run_funnel("import", path, "--db", db)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0.0)],
        )

    def test_groups_combine_with_include_users_and_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--within-seconds",
            "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        # 原有用户与配对明细保留在顶层，字段与排序不变。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(
            payload["conversion_pairs"],
            [
                {
                    "user_id": "u1",
                    "visit_timestamp": "2026-10-07T10:00:00",
                    "signup_timestamp": "2026-10-07T10:00:30",
                }
            ],
        )
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0.0)],
        )

    # -- 非法 --group-by 取值 ----------------------------------------------

    def test_invalid_group_by_rejected_before_db_access(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        cases = [
            ("--group-by",),  # 缺值
            ("--group-by", ""),  # 空值
            ("--group-by", "signup-date"),  # 其他取值
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = self.report(db, *extra)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误指出参数与原因，且先于数据库访问（不创建缺失库）。
                self.assertIn("--group-by", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(os.path.exists(db))

    def test_missing_db_with_valid_group_by_follows_path_error_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--group-by", "visit-date")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    # -- 报告不改动已有记录 ------------------------------------------------

    def test_grouped_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        self.report(db, "--group-by", "visit-date")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
