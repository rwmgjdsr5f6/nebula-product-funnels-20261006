"""report --include-users 用户编号明细的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]
                              [--visit-from ... --visit-before ...]
                              [--include-users]

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

# 验收样例：2026-10-06 的五条事件。
# u1：10:00:00 visit，10:01:00 signup（间隔 60 秒，60 秒窗口上界包含）
# u2：只有 10:00:00 visit
# u3：09:59:00 signup，10:00:00 visit（signup 早于 visit，任何窗口都不转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:59:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (4, 0, 3, 1, 2)]

# 主样例中混入完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
]

# 排序样例：编号含大写、数字、空白与中文，验证按 Unicode 码点字典序升序、
# 区分大小写、不按数字大小排序、空白与中文原样保留。
# 码点序："U10" < "U2"（'1' < '2'，不按数字大小）、"U..." < "u..."（大写在小写前）、
# "u 1"（空格 U+0020）< "u1"（'1' U+0031）、中文码点排在 ASCII 之后。
ORDER_EVENTS = [
    {"user_id": "U10", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "U2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u 1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "用户甲", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "用户甲", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u 1", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]
EXPECTED_ORDER_VISIT_IDS = ["U10", "U2", "u 1", "u1", "用户甲"]
EXPECTED_ORDER_CONVERTED_IDS = ["u 1", "用户甲"]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}
DETAIL_KEYS = METRIC_KEYS | {"visit_user_ids", "converted_user_ids"}


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


class IncludeUsersReportTests(unittest.TestCase):
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

    def assertDetailedReport(
        self, result, visit_users, converted_users, rate, visit_ids, converted_ids
    ):
        """成功明细报告：退出码 0、标准错误为空、单行 JSON 且五个字段相符。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), DETAIL_KEYS)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)
        self.assertEqual(payload["visit_user_ids"], visit_ids)
        self.assertEqual(payload["converted_user_ids"], converted_ids)
        # 数组长度等于对应人数，转化数组成员都在访问数组中。
        self.assertEqual(len(visit_ids), visit_users)
        self.assertEqual(len(converted_ids), converted_users)
        self.assertTrue(set(converted_ids) <= set(visit_ids))

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_include_users(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertDetailedReport(
            self.report(db, "--include-users"),
            3,
            1,
            0.3333333333333333,
            ["u1", "u2", "u3"],
            ["u1"],
        )

    def test_acceptance_include_users_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 间隔恰好 60 秒，上界包含。
        self.assertDetailedReport(
            self.report(db, "--include-users", "--within-seconds", "60"),
            3,
            1,
            0.3333333333333333,
            ["u1", "u2", "u3"],
            ["u1"],
        )

    def test_acceptance_include_users_within_59(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 间隔 60 秒超出 59 秒窗口：有访问却无人转化，仅转化数组为空。
        self.assertDetailedReport(
            self.report(db, "--include-users", "--within-seconds", "59"),
            3,
            0,
            0,
            ["u1", "u2", "u3"],
            [],
        )

    def test_include_users_with_visit_window(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 明细沿用访问时段筛选：段内 visit 参与配对，signup 可以晚于终点。
        self.assertDetailedReport(
            self.report(
                db,
                "--include-users",
                "--visit-from",
                "2026-10-06T10:00:00",
                "--visit-before",
                "2026-10-06T10:00:30",
            ),
            3,
            1,
            0.3333333333333333,
            ["u1", "u2", "u3"],
            ["u1"],
        )
        # 时段只覆盖 u1 的 visit（10:00:00 含起点、10:00:00 之后不含他人）。
        self.assertDetailedReport(
            self.report(
                db,
                "--include-users",
                "--visit-from",
                "2026-10-06T09:59:00",
                "--visit-before",
                "2026-10-06T10:00:00",
            ),
            0,
            0,
            0,
            [],
            [],
        )

    # -- 汇总一致性 -------------------------------------------------------

    def test_metrics_match_between_modes(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        for extra in ((), ("--within-seconds", "60"), ("--within-seconds", "59")):
            with self.subTest(extra=extra):
                plain = self.parse_single_json_object(self.report(db, *extra).stdout)
                detailed = self.parse_single_json_object(
                    self.report(db, "--include-users", *extra).stdout
                )
                # 不传开关时仍只有原来三个字段。
                self.assertEqual(set(plain), METRIC_KEYS)
                self.assertEqual(set(detailed), DETAIL_KEYS)
                for key in METRIC_KEYS:
                    self.assertEqual(detailed[key], plain[key])

    # -- 确定性与不变量 ---------------------------------------------------

    def test_shuffled_rows_give_same_ids(self):
        db = self.import_events(SHUFFLED_EVENTS, jsonl_name="shuffled.jsonl")
        self.assertDetailedReport(
            self.report(db, "--include-users"),
            3,
            1,
            0.3333333333333333,
            ["u1", "u2", "u3"],
            ["u1"],
        )

    def test_duplicate_events_give_same_ids(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        self.assertDetailedReport(
            self.report(db, "--include-users"),
            3,
            1,
            0.3333333333333333,
            ["u1", "u2", "u3"],
            ["u1"],
        )

    def test_reimport_same_batch_give_same_ids(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        first = run_funnel("import", path, "--db", db)
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        second = run_funnel("import", path, "--db", db)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertDetailedReport(
            self.report(db, "--include-users"),
            3,
            1,
            0.3333333333333333,
            ["u1", "u2", "u3"],
            ["u1"],
        )

    # -- 排序规则 ---------------------------------------------------------

    def test_ids_sorted_by_code_point(self):
        db = self.import_events(ORDER_EVENTS, jsonl_name="order.jsonl")
        self.assertDetailedReport(
            self.report(db, "--include-users"),
            5,
            2,
            0.4,
            EXPECTED_ORDER_VISIT_IDS,
            EXPECTED_ORDER_CONVERTED_IDS,
        )
        # 明确不是数字大小序："U10" 排在 "U2" 之前（'1' < '2'）。
        payload = self.parse_single_json_object(
            self.report(db, "--include-users").stdout
        )
        self.assertLess(
            payload["visit_user_ids"].index("U10"),
            payload["visit_user_ids"].index("U2"),
        )

    # -- 空结果 -----------------------------------------------------------

    def test_signup_only_db_reports_zero_with_empty_arrays(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertDetailedReport(
            self.report(db, "--include-users"), 0, 0, 0, [], []
        )

    # -- 报告不改动已有记录 ----------------------------------------------

    def test_include_users_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.report(db, "--include-users")
        self.report(db, "--include-users", "--within-seconds", "60")
        self.assertEqual(self.snapshot_events(db), before)

    # -- 错误路径不受开关影响 --------------------------------------------

    def test_include_users_missing_db(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-users")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_include_users_invalid_within_seconds(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db, "--include-users", "--within-seconds", "0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)


if __name__ == "__main__":
    unittest.main()
