"""--visit-from / --visit-before 访问时间段筛选的回归测试。

固定公开约定：

- 两参数必须成对使用；取值必须是 YYYY-MM-DDTHH:MM:SS 有效时间（视为 UTC），
  不接受时区后缀、小数秒或前后空白；起点须严格早于终点
- 范围含起点、不含终点；访问人数按段内存在 visit 的 user_id 原值去重；
  转化只允许与段内 visit 配对，signup 可以晚于终点
- 可与 --within-seconds 叠加：注册须严格晚于段内访问，间隔上界仍包含
- 参数错误统一：退出码 2、标准输出为空、标准错误指出相关参数与原因，
  不输出异常堆栈；参数错误先于数据库访问，不创建数据库或改动记录
- 筛选只影响本次报告，不改写事件记录

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部函数。仅使用 Python 3 标准库；每个场景使用独立临时目录中的
JSONL 与 SQLite，测试结束不遗留任何文件。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验收样例：2026-10-06 的八条事件（UTC，时间字符串无时区后缀）。
# u1：10:00:00 visit，11:00:00 signup（signup 晚于段终点仍可与段内 visit 配对）
# u2：仅 10:30:00 visit
# u3：11:00:00 visit（不在 [10:00, 11:00) 段内），11:00:30 signup
# u4：09:59:30 visit（段外，不能参与配对），10:00:15 signup，10:00:30 visit（段内）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:30:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T11:00:30"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T09:59:30"},
    {"user_id": "u4", "event": "signup", "timestamp": "2026-10-06T10:00:15"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
]

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (5, 0, 7, 2, 4, 1, 6, 3)]

# 混入完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
]

VISIT_FROM = "2026-10-06T10:00:00"
VISIT_BEFORE = "2026-10-06T11:00:00"

# 格式非法的取值：时区后缀、小数秒、前后空白、真实控制字符、空串。
INVALID_FORMAT_VALUES = [
    "2026-10-06T10:00:00Z",
    "2026-10-06T10:00:00+00:00",
    "2026-10-06T10:00:00.5",
    "2026-10-06T10:00:00.000",
    " 2026-10-06T10:00:00",
    "2026-10-06T10:00:00 ",
    "\t2026-10-06T10:00:00",
    "2026-10-06T10:00:00\t",
    "2026-10-06T10:00:00\n",
    "\n2026-10-06T10:00:00",
    "2026-10-06T10:00:00\r\n",
    "2026-10-06 T10:00:00",
    "2026-10-06",
    "10:00:00",
    "",
]

# 格式合法但不是有效时间的取值。
INVALID_TIME_VALUES = [
    "2026-02-30T10:00:00",
    "2026-10-06T25:00:00",
    "2026-10-06T10:60:00",
    "2026-13-06T10:00:00",
    "0000-10-06T10:00:00",
]

EXPECTED_METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}


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


class VisitWindowReportTests(unittest.TestCase):
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

    def windowed_report(self, db, *extra_args):
        return self.report(
            db, "--visit-from", VISIT_FROM, "--visit-before", VISIT_BEFORE, *extra_args
        )

    def assert_metrics(self, result, visit_users, converted_users, rate):
        """成功报告：退出码 0、标准错误为空、标准输出为单个 JSON 对象且指标相符。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        nonempty_lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(nonempty_lines), 1, msg="stdout=%r" % result.stdout)
        payload = json.loads(nonempty_lines[0])
        self.assertEqual(set(payload), EXPECTED_METRIC_KEYS)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)

    def assert_param_error(self, result, *tokens):
        """参数错误的统一公开约定：退出码 2、标准输出为空、标准错误指出
        相关参数与原因，不输出异常堆栈。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        for token in tokens:
            self.assertIn(token, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收主场景 -------------------------------------------------------

    def test_acceptance_windowed_report(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 段内 visit：u1（10:00 含起点）、u2、u4（10:00:30）共 3 人；
        # u3 的 visit 在终点（不含）不计。转化仅 u1：signup 晚于段终点仍计入；
        # u4 的 signup 只早于段内 visit，段外 visit 不能参与配对。
        self.assert_metrics(self.windowed_report(db), 3, 1, 0.3333333333333333)

    def test_acceptance_windowed_with_within_seconds(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 叠加 --within-seconds 60：u1 间隔 3600 秒超出，转化与比例归零，
        # 访问人数不变。
        self.assert_metrics(
            self.windowed_report(db, "--within-seconds", "60"), 3, 0, 0
        )

    def test_acceptance_unfiltered_report_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 不传时间段参数：保持原有全库语义。
        self.assert_metrics(self.report(db), 4, 3, 0.75)

    def test_shuffled_and_duplicate_events_give_same_metrics(self):
        db = self.import_events(SHUFFLED_EVENTS, jsonl_name="shuffled.jsonl")
        self.assert_metrics(self.windowed_report(db), 3, 1, 0.3333333333333333)
        db2 = self.import_events(
            EVENTS_WITH_DUPLICATES, db=self.db_path("dupes.sqlite"),
            jsonl_name="dupes.jsonl",
        )
        self.assert_metrics(self.windowed_report(db2), 3, 1, 0.3333333333333333)

    def test_reimport_same_batch_keeps_windowed_metrics(self):
        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assert_metrics(self.windowed_report(db), 3, 1, 0.3333333333333333)

    # -- 边界语义 ---------------------------------------------------------

    def test_window_bounds_are_start_inclusive_end_exclusive(self):
        events = [
            # 恰好起点：计入
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            # 恰好终点：不计入
            {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"},
            # 终点前一秒：计入
            {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:59:59"},
            # 起点前一秒：不计入
            {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T09:59:59"},
        ]
        db = self.import_events(events)
        self.assert_metrics(self.windowed_report(db), 2, 0, 0)

    def test_signup_after_window_end_still_converts(self):
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T09:00:00"},
        ]
        db = self.import_events(events)
        self.assert_metrics(self.windowed_report(db), 1, 1, 1.0)

    def test_within_seconds_boundary_inclusive_with_window(self):
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            # 间隔恰好 60 秒：上界包含
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
            {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            # 间隔 61 秒：不计
            {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
            {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            # 同一时刻：不算转化
            {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u4", "event": "signup", "timestamp": "2026-10-06T09:59:00"},
            # 逆序：signup 早于 visit，不算转化
            {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
        ]
        db = self.import_events(events)
        self.assert_metrics(
            self.windowed_report(db, "--within-seconds", "60"), 4, 1, 0.25
        )

    def test_no_in_window_visit_reports_all_zero(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--visit-from", "2026-10-06T20:00:00",
            "--visit-before", "2026-10-06T21:00:00",
        )
        self.assert_metrics(result, 0, 0, 0)

    def test_out_of_window_visit_cannot_pair(self):
        # 用户只有段外 visit 与段内 signup：不计访问也不计转化。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T09:00:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:30:00"},
        ]
        db = self.import_events(events)
        self.assert_metrics(self.windowed_report(db), 0, 0, 0)

    # -- 成对与顺序校验 ---------------------------------------------------

    def test_missing_pair_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--visit-from", VISIT_FROM)
        self.assert_param_error(result, "--visit-before")
        result = self.report(db, "--visit-before", VISIT_BEFORE)
        self.assert_param_error(result, "--visit-from")

    def test_equal_or_reversed_bounds_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db, "--visit-from", VISIT_FROM, "--visit-before", VISIT_FROM
        )
        self.assert_param_error(result, "--visit-from", "--visit-before")
        result = self.report(
            db, "--visit-from", VISIT_BEFORE, "--visit-before", VISIT_FROM
        )
        self.assert_param_error(result, "--visit-from", "--visit-before")

    # -- 取值格式校验 -----------------------------------------------------

    def test_invalid_format_values_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        for value in INVALID_FORMAT_VALUES:
            with self.subTest(value=value):
                result = self.report(
                    db, "--visit-from", value, "--visit-before", VISIT_BEFORE
                )
                self.assert_param_error(result, "--visit-from", "格式")
                result = self.report(
                    db, "--visit-from", VISIT_FROM, "--visit-before", value
                )
                self.assert_param_error(result, "--visit-before", "格式")

    def test_invalid_time_values_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        for value in INVALID_TIME_VALUES:
            with self.subTest(value=value):
                result = self.report(
                    db, "--visit-from", value, "--visit-before", VISIT_BEFORE
                )
                self.assert_param_error(result, "--visit-from", "有效时间")

    # -- 参数错误先于数据库访问 --------------------------------------------

    def test_param_error_with_missing_db_creates_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "missing.sqlite")
        existing_before = sorted(os.listdir(tmp.name))
        bad_arg_sets = [
            ("--visit-from", VISIT_FROM),
            ("--visit-before", VISIT_BEFORE),
            ("--visit-from", VISIT_FROM, "--visit-before", VISIT_FROM),
            ("--visit-from", VISIT_BEFORE, "--visit-before", VISIT_FROM),
            ("--visit-from", "2026-10-06T10:00:00Z",
             "--visit-before", VISIT_BEFORE),
            ("--visit-from", "2026-02-30T10:00:00",
             "--visit-before", VISIT_BEFORE),
        ]
        for extra in bad_arg_sets:
            with self.subTest(extra=extra):
                result = run_funnel("report", "--db", db, *extra)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse(os.path.exists(db))
                # journal/wal/shm 等附属文件同样不得出现。
                self.assertEqual(sorted(os.listdir(tmp.name)), existing_before)

    def test_valid_params_missing_db_follows_path_error_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.windowed_report(db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))

    # -- 报告不改动已有记录 ------------------------------------------------

    def test_windowed_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.windowed_report(db)
        self.windowed_report(db, "--within-seconds", "60")
        # 非法参数的报告同样不得改写记录。
        self.report(db, "--visit-from", VISIT_FROM)
        self.report(db, "--visit-from", VISIT_BEFORE, "--visit-before", VISIT_FROM)
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
