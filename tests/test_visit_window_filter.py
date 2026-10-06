"""report 时间段筛选（--visit-from / --visit-before）的回归测试。

只通过公开命令验证行为：

    python -m funnel report --db <events.sqlite>
        --visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS
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

# 验收样例：统一使用 2026-10-06 的 UTC 时间（时间字符串无时区后缀，按 UTC 解释）。
# u1：10:00:00 visit，11:00:00 signup（段内访问，无窗口时转化）
# u2：10:30:00 visit（段内访问，无 signup）
# u3：11:00:00 visit（恰在终点，被排除），11:00:30 signup
# u4：09:59:30 visit（段外）、10:00:15 signup、10:00:30 visit（段内）
# 段 [10:00:00, 11:00:00) 内访问人数为 u1/u2/u4 共 3 人；只有 u1 的段内
# visit 严格早于其 signup，转化 1 人。
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

WINDOW_FROM = "2026-10-06T10:00:00"
WINDOW_BEFORE = "2026-10-06T11:00:00"

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (7, 0, 5, 3, 1, 6, 4, 2)]

EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:30:00"},
]

# signup 晚于段终点的用户：段内 visit 仍应与其配对。
LATE_SIGNUP_EVENT = [
    {"user_id": "u7", "event": "visit", "timestamp": "2026-10-06T10:05:00"},
    {"user_id": "u7", "event": "signup", "timestamp": "2026-10-06T15:00:00"},
]

# 段内无任何 visit 的数据。
OUTSIDE_EVENTS = [
    {"user_id": "u8", "event": "visit", "timestamp": "2026-10-06T08:00:00"},
    {"user_id": "u8", "event": "signup", "timestamp": "2026-10-06T08:30:00"},
    {"user_id": "u9", "event": "visit", "timestamp": "2026-10-06T12:00:00"},
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


class VisitWindowFilterTests(unittest.TestCase):
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

    def report(self, db, visit_from=None, visit_before=None, within_seconds=None):
        cli_args = ["report", "--db", db]
        if visit_from is not None:
            cli_args += ["--visit-from", visit_from]
        if visit_before is not None:
            cli_args += ["--visit-before", visit_before]
        if within_seconds is not None:
            cli_args += ["--within-seconds", str(within_seconds)]
        return run_funnel(*cli_args)

    def assertReportMetrics(self, result, visit_users, converted_users, rate):
        """成功报告：退出码 0、标准错误为空、标准输出为单个 JSON 对象且指标相符。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), EXPECTED_METRIC_KEYS)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)

    @staticmethod
    def parse_single_json_object(stdout):
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        assert nonempty_lines, "标准输出为空，无法解析 JSON 对象: %r" % stdout
        assert len(nonempty_lines) == 1, "标准输出包含多行，不是单个 JSON 对象: %r" % stdout
        obj = json.loads(nonempty_lines[0])
        assert isinstance(obj, dict), "标准输出不是 JSON 对象: %r" % stdout
        return obj

    def assertParamError(self, result, *name_fragments):
        """参数错误：退出码 2、标准输出为空、标准错误无堆栈且指出相关参数。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        for fragment in name_fragments:
            self.assertIn(fragment, result.stderr)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_visit_window_metrics(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 段 [10:00:00, 11:00:00)：u1/u2/u4 共 3 人有段内 visit；
        # u4 的段内 visit（10:00:30）晚于其 signup（10:00:15），不转化。
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE), 3, 1, 0.3333333333333333
        )

    def test_acceptance_visit_window_within_60_seconds(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # u1 间隔 3600 秒超出 60 秒窗口；段内无人转化，访问人数仍是 3。
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE, within_seconds=60), 3, 0, 0
        )

    def test_no_visit_window_reports_whole_database(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 不传时间段：u1、u3、u4 均有严格晚于某次 visit 的 signup，共 3/4。
        self.assertReportMetrics(self.report(db), 4, 3, 0.75)

    def test_imported_eight_events(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path()
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 8})

    # -- 边界：含起点、不含终点 -------------------------------------------

    def test_start_inclusive_end_exclusive(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # u3 的 visit 恰在 11:00:00（终点）：终点不含，仍只有 3 人。
        self.assertReportMetrics(
            self.report(db, "2026-10-06T11:00:00", "2026-10-06T12:00:00"),
            1,  # 仅 u3 在 [11:00:00, 12:00:00) 内有 visit
            1,  # 其 signup 11:00:30 严格晚于段内 visit
            1.0,
        )

    # -- signup 可晚于终点；段外 visit 不参与配对 --------------------------

    def test_signup_after_window_end_still_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS + LATE_SIGNUP_EVENT)
        # u7 的 signup（15:00:00）晚于段终点，仍与段内 visit（10:05:00）配对。
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE), 4, 2, 0.5
        )

    def test_outside_visit_excluded_even_when_signup_looks_later(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 段 [09:30, 10:00)：仅 u4 的 09:59:30 visit 在段内；其 signup
        # 10:00:15 晚于段终点仍可配对，u4 计访问且计转化（1/1）。
        # 对照：同一数据在段 [10:00, 11:00) 内 u4 不转化——段外 visit
        # （09:59:30 早于 signup）不能参与段内配对。
        self.assertReportMetrics(
            self.report(db, "2026-10-06T09:30:00", "2026-10-06T10:00:00"), 1, 1, 1.0
        )
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE), 3, 1, 0.3333333333333333
        )

    # -- 段内无访问：三项均为 0（整数 0） ---------------------------------

    def test_window_without_any_visit_reports_all_zero(self):
        db = self.import_events(OUTSIDE_EVENTS, jsonl_name="outside.jsonl")
        result = self.report(db, WINDOW_FROM, WINDOW_BEFORE)
        self.assertReportMetrics(result, 0, 0, 0)
        # 零访问走整数 0 分支，比例文本是 0 而不是 0.0。
        self.assertIn('"conversion_rate": 0', result.stdout)

    # -- 行序、重复事件、重复导入 ------------------------------------------

    def test_shuffled_rows_give_same_window_metrics(self):
        db = self.import_events(SHUFFLED_EVENTS, jsonl_name="shuffled.jsonl")
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE), 3, 1, 0.3333333333333333
        )
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE, within_seconds=60), 3, 0, 0
        )

    def test_duplicate_events_give_same_window_metrics(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE), 3, 1, 0.3333333333333333
        )

    def test_reimport_appends_without_changing_window_metrics(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        first = run_funnel("import", path, "--db", db)
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        second = run_funnel("import", path, "--db", db)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertReportMetrics(
            self.report(db, WINDOW_FROM, WINDOW_BEFORE), 3, 1, 0.3333333333333333
        )
        self.assertReportMetrics(self.report(db), 4, 3, 0.75)

    # -- 报告只读 ---------------------------------------------------------

    def test_windowed_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.report(db, WINDOW_FROM, WINDOW_BEFORE)
        self.report(db, WINDOW_FROM, WINDOW_BEFORE, within_seconds=60)
        self.report(db)
        self.assertEqual(self.snapshot_events(db), before)

    # -- 参数错误：成对、顺序、格式、日历有效性 ----------------------------

    def test_visit_from_alone_rejected(self):
        db = self.db_path()
        result = self.report(db, visit_from=WINDOW_FROM)
        self.assertParamError(result, "--visit-from", "--visit-before")

    def test_visit_before_alone_rejected(self):
        db = self.db_path()
        result = self.report(db, visit_before=WINDOW_BEFORE)
        self.assertParamError(result, "--visit-from", "--visit-before")

    def test_equal_bounds_rejected(self):
        db = self.db_path()
        result = self.report(db, WINDOW_FROM, WINDOW_FROM)
        self.assertParamError(result, "--visit-from", "--visit-before")

    def test_reversed_bounds_rejected(self):
        db = self.db_path()
        result = self.report(db, WINDOW_BEFORE, WINDOW_FROM)
        self.assertParamError(result, "--visit-from", "--visit-before")

    def test_invalid_bound_values_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        invalid_values = [
            "2026-10-6T10:00:00",        # 月/日非两位
            "2026-02-30T10:00:00",       # 形态合法但日历无效
            "2026-13-06T10:00:00",       # 月份越界
            "2026-10-06T10:00",          # 缺少秒
            "2026-10-06 10:00:00",       # 缺少 T
            "2026-10-06T10:00:00Z",      # 时区后缀
            "2026-10-06T10:00:00+00:00",
            "2026-10-06T10:00:00.5",     # 小数秒
            " 2026-10-06T10:00:00",      # 前空白
            "2026-10-06T10:00:00 ",      # 后空白
            "not-a-timestamp",
        ]
        for value in invalid_values:
            for side, kwargs in (
                ("from", {"visit_from": value, "visit_before": WINDOW_BEFORE}),
                ("before", {"visit_from": WINDOW_FROM, "visit_before": value}),
            ):
                with self.subTest(side=side, value=value):
                    result = self.report(db, **kwargs)
                    self.assertParamError(
                        result,
                        "--visit-%s" % side,
                    )

    def test_missing_bound_value_rejected(self):
        db = self.db_path()
        result = run_funnel(
            "report", "--db", db, "--visit-from", WINDOW_FROM, "--visit-before"
        )
        self.assertParamError(result, "--visit-before")

    # -- 参数错误先于数据库访问 --------------------------------------------

    def test_param_errors_do_not_create_database(self):
        # 缺少配对
        db = self.db_path("missing-pair.sqlite")
        self.assertFalse(os.path.exists(db))
        self.report(db, visit_from=WINDOW_FROM)
        self.assertFalse(os.path.exists(db))
        # 逆序
        db = self.db_path("missing-reversed.sqlite")
        self.report(db, WINDOW_BEFORE, WINDOW_FROM)
        self.assertFalse(os.path.exists(db))
        # 格式错误
        db = self.db_path("missing-format.sqlite")
        self.report(db, "2026-10-06T10:00:00Z", WINDOW_BEFORE)
        self.assertFalse(os.path.exists(db))

    def test_valid_params_missing_db_follows_path_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, WINDOW_FROM, WINDOW_BEFORE)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))


if __name__ == "__main__":
    unittest.main()
