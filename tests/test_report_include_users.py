"""--include-users 明细开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--include-users]

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
# u1：10:00:00 visit，10:01:00 signup（间隔 60 秒）
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

# 排序样例：编号含大小写、前导空白、中文与数字形态，验证按 Unicode 码点
# 字典序升序（" " < "A" < "a" < "u"，"u10" < "u2" 按字符而非数值）。
SORT_EVENTS = [
    {"user_id": "u10", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "Alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": " 空格", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u10", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]
SORTED_VISIT_IDS = [" 空格", "Alice", "alice", "u10", "u2", "张三"]
SORTED_CONVERTED_IDS = ["u10", "张三"]

# 时段样例：段内访问的 signup 可以晚于终点；段外 visit 不参与配对。
WINDOW_EVENTS = [
    {"user_id": "in", "event": "visit", "timestamp": "2026-10-06T10:30:00"},
    {"user_id": "in", "event": "signup", "timestamp": "2026-10-06T12:00:00"},
    {"user_id": "out", "event": "visit", "timestamp": "2026-10-06T09:00:00"},
    {"user_id": "out", "event": "signup", "timestamp": "2026-10-06T09:01:00"},
]

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


class IncludeUsersTests(unittest.TestCase):
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

    def assertReport(self, result, visit_users, converted_users, rate,
                     visit_ids, converted_ids):
        """成功明细报告：退出码 0、标准错误为空、字段与取值全部相符。"""
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
        self.assertReport(
            self.report(db, "--include-users"),
            3, 1, 0.3333333333333333, ["u1", "u2", "u3"], ["u1"],
        )

    def test_acceptance_within_60_keeps_u1(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertReport(
            self.report(db, "--within-seconds", "60", "--include-users"),
            3, 1, 0.3333333333333333, ["u1", "u2", "u3"], ["u1"],
        )

    def test_acceptance_within_59_empties_converted(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 间隔 60 秒超出 59 秒窗口：有访问却无人转化，仅转化数组为空。
        self.assertReport(
            self.report(db, "--within-seconds", "59", "--include-users"),
            3, 0, 0, ["u1", "u2", "u3"], [],
        )

    def test_without_flag_outputs_only_metrics(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db)
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), METRIC_KEYS)

    def test_metrics_identical_between_modes(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        for extra in ((), ("--within-seconds", "60"), ("--within-seconds", "59")):
            with self.subTest(extra=extra):
                plain = self.parse_single_json_object(self.report(db, *extra).stdout)
                detailed = self.parse_single_json_object(
                    self.report(db, *(extra + ("--include-users",))).stdout
                )
                for key in METRIC_KEYS:
                    self.assertEqual(detailed[key], plain[key])

    # -- 排序与去重 -------------------------------------------------------

    def test_ids_sorted_by_code_point(self):
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        self.assertReport(
            self.report(db, "--include-users"),
            6, 2, 0.3333333333333333, SORTED_VISIT_IDS, SORTED_CONVERTED_IDS,
        )

    def test_shuffled_and_duplicate_events_give_same_ids(self):
        db = self.import_events(
            SHUFFLED_EVENTS + ACCEPTANCE_EVENTS, jsonl_name="shuffled.jsonl"
        )
        self.assertReport(
            self.report(db, "--include-users"),
            3, 1, 0.3333333333333333, ["u1", "u2", "u3"], ["u1"],
        )

    def test_reimport_same_batch_keeps_ids(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertReport(
            self.report(db, "--include-users"),
            3, 1, 0.3333333333333333, ["u1", "u2", "u3"], ["u1"],
        )

    # -- 访问时段与空结果 -------------------------------------------------

    def test_visit_window_filters_ids(self):
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        # 段内只有 in 的 visit；其 signup 晚于终点仍计入转化。
        self.assertReport(
            self.report(
                db,
                "--visit-from", "2026-10-06T10:00:00",
                "--visit-before", "2026-10-06T11:00:00",
                "--include-users",
            ),
            1, 1, 1.0, ["in"], ["in"],
        )

    def test_no_matching_visits_gives_empty_arrays(self):
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        self.assertReport(
            self.report(
                db,
                "--visit-from", "2026-10-07T00:00:00",
                "--visit-before", "2026-10-08T00:00:00",
                "--include-users",
            ),
            0, 0, 0, [], [],
        )

    def test_signup_only_db_reports_zero_with_empty_arrays(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertReport(self.report(db, "--include-users"), 0, 0, 0, [], [])

    # -- 错误协议与只读性 -------------------------------------------------

    def test_invalid_within_seconds_still_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db, "--within-seconds", "0", "--include-users")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)

    def test_missing_db_still_rejected(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-users")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--include-users")
        self.report(db, "--within-seconds", "60", "--include-users")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
