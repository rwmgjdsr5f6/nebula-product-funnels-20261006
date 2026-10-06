"""报告窗口语义的回归测试。

只通过公开命令行验证：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]

主样例统一使用 2026-10-06 的 UTC 时间：

- u1：10:00:00 visit、10:01:00 signup（间隔恰为 60 秒，窗口上界包含）
- u2：10:00:00 visit、10:01:01 signup（间隔 61 秒，60 秒窗口外）
- u3：09:59:00 signup、10:00:00 visit（signup 严格早于 visit，不转化）
- u4：10:00:00 visit 与 signup 同时刻（相等不算转化）
- u5：09:58:00、10:00:00 各一次 visit，10:00:30 signup（任一次 visit 满足即可、按用户去重）
- u6：仅 10:00:30 signup（无 visit，不计入访问人数）

仅使用标准库 unittest；每个场景使用独立的临时目录与临时 SQLite 数据库，
不依赖预存数据库、网络或第三方包，测试结束不遗留文件。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DAY = "2026-10-06"


def ev(user, event, t):
    return {"user_id": user, "event": event, "timestamp": "%sT%s" % (DAY, t)}


# 固定主样例（规范行序）。
MAIN_EVENTS = [
    ev("u1", "visit", "10:00:00"),
    ev("u1", "signup", "10:01:00"),
    ev("u2", "visit", "10:00:00"),
    ev("u2", "signup", "10:01:01"),
    ev("u3", "signup", "09:59:00"),
    ev("u3", "visit", "10:00:00"),
    ev("u4", "visit", "10:00:00"),
    ev("u4", "signup", "10:00:00"),
    ev("u5", "visit", "09:58:00"),
    ev("u5", "visit", "10:00:00"),
    ev("u5", "signup", "10:00:30"),
    ev("u6", "signup", "10:00:30"),
]

EXPECTED_WITH_60 = {
    "visit_users": 5,
    "converted_users": 2,  # u1（恰好 60 秒）、u5（30 秒）
    "conversion_rate": 0.4,
}
EXPECTED_WITHOUT_WINDOW = {
    "visit_users": 5,
    "converted_users": 3,  # 再纳入间隔 61 秒的 u2
    "conversion_rate": 0.6,
}
REPORT_KEYS = {"visit_users", "converted_users", "conversion_rate"}
INVALID_WITHIN_SECONDS = ["0", "-1", "1.5", "60s", "６０"]


class FunnelCliTestBase(unittest.TestCase):
    """每个测试独立使用临时目录，结束后自动清理，不遗留数据库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_jsonl(self, name, events):
        path = self.tmp / name
        with path.open("w", encoding="utf-8") as fh:
            for row in events:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def run_funnel(self, *cli_args):
        return subprocess.run(
            [sys.executable, "-m", "funnel", *cli_args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def db_path(self, name="events.sqlite"):
        return self.tmp / name

    def import_events(self, events, db=None, file_name="events.jsonl"):
        path = self.write_jsonl(file_name, events)
        db = db or self.db_path()
        result = self.run_funnel("import", str(path), "--db", str(db))
        self.assertEqual(
            result.returncode,
            0,
            msg="导入失败: rc=%r stderr=%r" % (result.returncode, result.stderr),
        )
        return result, db

    def report(self, db, within_seconds=None):
        cli_args = ["report", "--db", str(db)]
        if within_seconds is not None:
            cli_args += ["--within-seconds", str(within_seconds)]
        return self.run_funnel(*cli_args)

    def assert_success_report(self, result, expected):
        """成功报告：退出码 0、标准错误为空、标准输出恰为一个含三项指标的 JSON 对象。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "", msg="成功报告不应写入标准错误")
        stdout = result.stdout
        non_empty_lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(non_empty_lines), 1, msg="标准输出应只有一行 JSON: %r" % stdout)
        obj = json.loads(non_empty_lines[0])  # 不可解析时直接失败
        self.assertIsInstance(obj, dict)
        self.assertEqual(set(obj.keys()), REPORT_KEYS)
        self.assertEqual(obj, expected)
        return obj


class MainSampleReportTest(FunnelCliTestBase):
    """主样例在 60 秒窗口与无窗口下的三项指标。"""

    def test_within_60_seconds(self):
        _, db = self.import_events(MAIN_EVENTS)
        self.assert_success_report(self.report(db, within_seconds=60), EXPECTED_WITH_60)

    def test_no_window(self):
        _, db = self.import_events(MAIN_EVENTS)
        self.assert_success_report(
            self.report(db), EXPECTED_WITHOUT_WINDOW
        )

    def test_leading_zeros_equivalent(self):
        _, db = self.import_events(MAIN_EVENTS)
        self.assert_success_report(
            self.report(db, within_seconds="00060"), EXPECTED_WITH_60
        )


class InvarianceTest(FunnelCliTestBase):
    """打乱行序、重复事件、再次导入同一批数据后，人数与比例保持不变。"""

    def _assert_both_windows(self, db):
        self.assert_success_report(self.report(db, within_seconds=60), EXPECTED_WITH_60)
        self.assert_success_report(self.report(db), EXPECTED_WITHOUT_WINDOW)

    def test_shuffled_row_order(self):
        # 固定的非规范行序：逆序并交错插入，不引入随机源。
        shuffled = [
            MAIN_EVENTS[11],
            MAIN_EVENTS[0],
            MAIN_EVENTS[6],
            MAIN_EVENTS[5],
            MAIN_EVENTS[10],
            MAIN_EVENTS[3],
            MAIN_EVENTS[7],
            MAIN_EVENTS[2],
            MAIN_EVENTS[9],
            MAIN_EVENTS[4],
            MAIN_EVENTS[8],
            MAIN_EVENTS[1],
        ]
        self.assertEqual(sorted(map(id, shuffled)), sorted(map(id, MAIN_EVENTS)))
        _, db = self.import_events(shuffled, file_name="shuffled.jsonl")
        self._assert_both_windows(db)

    def test_duplicate_events(self):
        # 同一批事件中混入重复 visit / signup 行。
        with_duplicates = MAIN_EVENTS + [
            MAIN_EVENTS[0],   # u1 visit
            MAIN_EVENTS[8],   # u5 09:58 visit
            MAIN_EVENTS[9],   # u5 10:00 visit
            MAIN_EVENTS[10],  # u5 signup
            MAIN_EVENTS[11],  # u6 signup
        ]
        _, db = self.import_events(with_duplicates, file_name="duplicates.jsonl")
        self._assert_both_windows(db)

    def test_reimport_same_batch(self):
        path = self.write_jsonl("main.jsonl", MAIN_EVENTS)
        db = self.db_path()
        first = self.run_funnel("import", str(path), "--db", str(db))
        second = self.run_funnel("import", str(path), "--db", str(db))
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(json.loads(first.stdout), {"imported": len(MAIN_EVENTS)})
        self.assertEqual(json.loads(second.stdout), {"imported": len(MAIN_EVENTS)})
        # 追加导入同一批数据不改变去重后的人数与比例。
        self._assert_both_windows(db)


class WindowSemanticsTest(FunnelCliTestBase):
    """对四条判定规则分别建立最小场景，失败时可直接定位不符预期的规则。"""

    def test_signup_before_visit_never_converts(self):
        # 严格先后：signup 早于 visit，任何窗口下都不转化。
        _, db = self.import_events(
            [ev("u3", "signup", "09:59:00"), ev("u3", "visit", "10:00:00")],
            file_name="before.jsonl",
        )
        expected = {"visit_users": 1, "converted_users": 0, "conversion_rate": 0}
        self.assert_success_report(self.report(db, within_seconds=60), expected)
        self.assert_success_report(self.report(db), expected)

    def test_equal_timestamp_not_conversion(self):
        # 相等时刻不算转化。
        _, db = self.import_events(
            [ev("u4", "visit", "10:00:00"), ev("u4", "signup", "10:00:00")],
            file_name="equal.jsonl",
        )
        expected = {"visit_users": 1, "converted_users": 0, "conversion_rate": 0}
        self.assert_success_report(self.report(db, within_seconds=60), expected)
        self.assert_success_report(self.report(db), expected)

    def test_window_upper_bound_inclusive(self):
        # 窗口上界包含：恰好 60 秒计入，61 秒在 60 秒窗口外、无窗口时计入。
        _, db = self.import_events(
            [
                ev("u1", "visit", "10:00:00"),
                ev("u1", "signup", "10:01:00"),
                ev("u2", "visit", "10:00:00"),
                ev("u2", "signup", "10:01:01"),
            ],
            file_name="bound.jsonl",
        )
        self.assert_success_report(
            self.report(db, within_seconds=60),
            {"visit_users": 2, "converted_users": 1, "conversion_rate": 0.5},
        )
        self.assert_success_report(
            self.report(db),
            {"visit_users": 2, "converted_users": 2, "conversion_rate": 1.0},
        )

    def test_any_visit_satisfies_and_user_dedup(self):
        # u5 有两次 visit：与 signup 相隔 150 秒和 30 秒，任一次满足即可；用户只计一次。
        _, db = self.import_events(
            [
                ev("u5", "visit", "09:58:00"),
                ev("u5", "visit", "10:00:00"),
                ev("u5", "signup", "10:00:30"),
            ],
            file_name="anyvisit.jsonl",
        )
        expected = {"visit_users": 1, "converted_users": 1, "conversion_rate": 1.0}
        self.assert_success_report(self.report(db, within_seconds=60), expected)
        self.assert_success_report(self.report(db), expected)


class SignupOnlyTest(FunnelCliTestBase):
    """仅有 signup 的数据库，有无窗口三项指标均为 0。"""

    def test_all_zero_with_and_without_window(self):
        _, db = self.import_events(
            [ev("u6", "signup", "10:00:30")], file_name="signup_only.jsonl"
        )
        zero_report = {"visit_users": 0, "converted_users": 0, "conversion_rate": 0}
        self.assert_success_report(self.report(db, within_seconds=60), zero_report)
        self.assert_success_report(self.report(db), zero_report)


class InvalidWithinSecondsTest(FunnelCliTestBase):
    """非法 --within-seconds：退出码 2、标准输出为空、标准错误指出选项与原因，且不建库。"""

    def test_each_invalid_value(self):
        _, source_db = self.import_events(MAIN_EVENTS, file_name="main.jsonl")
        for value in INVALID_WITHIN_SECONDS:
            with self.subTest(value=value):
                result = self.report(source_db, within_seconds=value)
                self.assertEqual(result.returncode, 2, msg=repr(value))
                self.assertEqual(result.stdout, "", msg=repr(value))
                self.assertIn("--within-seconds", result.stderr, msg=repr(value))
                # 选项名之后仍应给出具体原因，而不是只有用法行。
                after_option = result.stderr.split("--within-seconds", 1)[1]
                self.assertTrue(after_option.strip(), msg=repr(value))

    def test_invalid_value_does_not_create_database(self):
        missing_db = self.db_path("never.sqlite")
        self.assertFalse(missing_db.exists())
        for value in INVALID_WITHIN_SECONDS:
            with self.subTest(value=value):
                result = self.report(missing_db, within_seconds=value)
                self.assertEqual(result.returncode, 2, msg=repr(value))
                self.assertEqual(result.stdout, "", msg=repr(value))
                self.assertIn("--within-seconds", result.stderr, msg=repr(value))
                self.assertFalse(
                    missing_db.exists(),
                    msg="参数非法且数据库不存在时不应创建文件: %r" % value,
                )

    def test_existing_records_unchanged_after_failed_report(self):
        _, db = self.import_events(MAIN_EVENTS, file_name="main.jsonl")
        before_60 = self.report(db, within_seconds=60)
        before_all = self.report(db)
        for value in INVALID_WITHIN_SECONDS:
            failed = self.report(db, within_seconds=value)
            self.assertEqual(failed.returncode, 2, msg=repr(value))
        # 成功报告与失败报告均不改变已有事件：随后输出与此前完全一致。
        self.assertEqual(
            self.report(db, within_seconds=60).stdout, before_60.stdout, msg="60 秒窗口"
        )
        self.assertEqual(
            self.report(db).stdout, before_all.stdout, msg="无窗口"
        )


if __name__ == "__main__":
    unittest.main()
