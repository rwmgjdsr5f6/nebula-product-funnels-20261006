"""visit -> signup 两步漏斗报告窗口语义的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]

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

# 主样例：统一使用 2026-10-06 的 UTC 时间（时间字符串无时区后缀，按 UTC 解释）。
# u1：10:00:00 visit，10:01:00 signup（间隔 60 秒，窗口上界包含）
# u2：10:00:00 visit，10:01:01 signup（间隔 61 秒，60 秒窗口内不计）
# u3：09:59:00 signup，10:00:00 visit（signup 严格早于 visit，永不转化）
# u4：10:00:00 visit 与 signup 同一时刻（相等不算转化）
# u5：09:58:00、10:00:00 各 visit 一次，10:00:30 signup（任一次 visit 满足即可）
# u6：只有 10:00:30 signup（无 visit，不计访问人数）
MAIN_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:59:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u4", "event": "signup", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u5", "event": "visit", "timestamp": "2026-10-06T09:58:00"},
    {"user_id": "u5", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u5", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [MAIN_EVENTS[i] for i in (11, 0, 5, 10, 3, 7, 1, 8, 6, 2, 9, 4)]

# 主样例中混入若干完全重复的事件行。
EVENTS_WITH_DUPLICATES = MAIN_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u5", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

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

# 超过 SQLite INTEGER 上限（2**63 - 1）的合法大窗口：参数不设数值或位数上限。
HUGE_WITHIN_SECONDS = "9223372036854775808"  # 2**63
HUGE_WITHIN_SECONDS_LONG = "9" * 5000  # 5000 个字符 9

# 非法 --within-seconds 取值：0、负数、小数、带单位、全角数字。
INVALID_WITHIN_SECONDS = ["0", "-1", "1.5", "60s", "６０"]

# 边界非法取值：空串、空白、正负号、单位后缀、全角数字，以及位于开头、
# 中间、末尾的真实 LF / CRLF / 制表符。完整参数值只允许字符 0-9，
# 不裁剪、不忽略任何其他字符（含末尾换行）。
INVALID_WITHIN_SECONDS_BOUNDARY = [
    "",  # 空串
    " ",  # 空格
    "\t",  # 制表符
    "\n",  # 仅 LF
    "\r\n",  # 仅 CRLF
    "+60",  # 正号
    "-60",  # 负号
    "1.5",  # 小数
    "60s",  # 单位后缀
    "６０",  # 全角数字
    "60\n",  # 末尾真实 LF
    "000\n",  # 全零加末尾 LF：同样按非法字符拒绝，不得脱离参数错误协议
    "9" * 5000 + "\n",  # 五千个 9 加末尾 LF：与短值同一拒绝结果
    "\n60",  # 开头 LF
    "6\n0",  # 中间 LF
    "60\r\n",  # 末尾 CRLF
    "60\t",  # 末尾制表符
    " 60",  # 开头空格
    "60 ",  # 末尾空格
]

# 仅由零组成的取值：原因必须指出数值应大于零。
ZERO_ONLY_WITHIN_SECONDS = ["0", "000", "0" * 5000]

# 验收四事件样例：2026-10-06 UTC。
# u1：10:00:00 visit，10:01:00 signup（间隔 60 秒，窗口上界包含）
# u2：10:00:00 visit，10:01:01 signup（间隔 61 秒，60 秒窗口内不计）
FOUR_EVENT_ACCEPTANCE = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
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


class FunnelReportTests(unittest.TestCase):
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

    def report(self, db, within_seconds=None):
        cli_args = ["report", "--db", db]
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

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 窗口语义：主样例 -------------------------------------------------

    def test_within_60_seconds(self):
        db = self.import_events(MAIN_EVENTS)
        # u1（恰好 60 秒，上界包含）与 u5（30 秒）转化；u2 间隔 61 秒不计。
        self.assertReportMetrics(self.report(db, 60), 5, 2, 0.4)

    def test_within_00060_equals_60(self):
        db = self.import_events(MAIN_EVENTS)
        # 前导零参数与 60 结果相同。
        self.assertReportMetrics(self.report(db, "00060"), 5, 2, 0.4)

    def test_no_window(self):
        db = self.import_events(MAIN_EVENTS)
        # 不传窗口：u1、u2、u5 转化；u3 signup 早于 visit、u4 同时刻均不计。
        self.assertReportMetrics(self.report(db), 5, 3, 0.6)

    def test_shuffled_rows_give_same_metrics(self):
        db = self.import_events(SHUFFLED_EVENTS, jsonl_name="shuffled.jsonl")
        self.assertReportMetrics(self.report(db, 60), 5, 2, 0.4)
        self.assertReportMetrics(self.report(db), 5, 3, 0.6)

    def test_duplicate_events_give_same_metrics(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        # 重复事件按用户去重，人数与比例不变。
        self.assertReportMetrics(self.report(db, 60), 5, 2, 0.4)
        self.assertReportMetrics(self.report(db), 5, 3, 0.6)

    def test_reimport_same_batch_appends_without_changing_metrics(self):
        path = self.write_jsonl("main.jsonl", MAIN_EVENTS)
        db = self.db_path("reimport.sqlite")
        first = run_funnel("import", path, "--db", db)
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        # 再次导入同一批数据（追加语义），统计结果保持一致。
        second = run_funnel("import", path, "--db", db)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertReportMetrics(self.report(db, 60), 5, 2, 0.4)
        self.assertReportMetrics(self.report(db), 5, 3, 0.6)

    # -- 大窗口：超过 SQLite INTEGER 上限的 N 也正常出报告 ----------------

    def test_acceptance_import_five_events(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path()
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 5})

    def test_huge_window_above_sqlite_integer_max(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 2**63：仅 u1 转化；u3 的 signup 早于 visit，再大窗口也不算。
        self.assertReportMetrics(
            self.report(db, HUGE_WITHIN_SECONDS), 3, 1, 0.3333333333333333
        )

    def test_huge_window_5000_nines(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 5000 个字符 9：位数不设上限，结果与 60 秒窗口一致。
        self.assertReportMetrics(
            self.report(db, HUGE_WITHIN_SECONDS_LONG), 3, 1, 0.3333333333333333
        )

    def test_acceptance_leading_zero_window(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertReportMetrics(self.report(db, "00060"), 3, 1, 0.3333333333333333)

    def test_acceptance_window_59_seconds(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 间隔 60 秒超出 59 秒窗口：访问 3 人、转化 0 人、比例 0。
        self.assertReportMetrics(self.report(db, 59), 3, 0, 0)

    def test_huge_window_matches_unbounded_report(self):
        db = self.import_events(MAIN_EVENTS)
        # 大窗口等价于不限间隔：u1、u2、u5 转化，u3 逆序、u4 同时刻均不计。
        self.assertReportMetrics(self.report(db, HUGE_WITHIN_SECONDS), 5, 3, 0.6)
        self.assertReportMetrics(self.report(db, HUGE_WITHIN_SECONDS_LONG), 5, 3, 0.6)

    def test_huge_window_signup_only_db_reports_all_zero(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertReportMetrics(self.report(db, HUGE_WITHIN_SECONDS), 0, 0, 0)

    def test_huge_window_missing_db_follows_path_error_protocol(self):
        db = self.db_path("missing-huge.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, HUGE_WITHIN_SECONDS)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_huge_window_keeps_events_unchanged(self):
        db = self.import_events(MAIN_EVENTS)
        before = self.snapshot_events(db)
        self.report(db, HUGE_WITHIN_SECONDS)
        self.report(db, HUGE_WITHIN_SECONDS_LONG)
        self.assertEqual(self.snapshot_events(db), before)

    # -- 报告确定输出 -----------------------------------------------------

    def test_report_stdout_is_single_json_object(self):
        db = self.import_events(MAIN_EVENTS)
        result = self.report(db, 60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        # 不依赖字段顺序：只要求整段输出是一个可解析的 JSON 对象。
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), EXPECTED_METRIC_KEYS)

    def test_signup_only_db_reports_all_zero(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertReportMetrics(self.report(db), 0, 0, 0)
        self.assertReportMetrics(self.report(db, 60), 0, 0, 0)

    # -- 非法窗口参数 -----------------------------------------------------

    def test_invalid_within_seconds_rejected(self):
        db = self.import_events(MAIN_EVENTS)
        for value in INVALID_WITHIN_SECONDS:
            with self.subTest(value=value):
                result = self.report(db, value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误指出 --within-seconds 及具体原因。
                self.assertIn("--within-seconds", result.stderr)
                self.assertIn("必须", result.stderr)

    def test_invalid_value_does_not_create_db(self):
        for value in INVALID_WITHIN_SECONDS:
            with self.subTest(value=value):
                db = self.db_path("missing-%s.sqlite" % str(ord(value[0])))
                self.assertFalse(os.path.exists(db))
                result = self.report(db, value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse(
                    os.path.exists(db),
                    msg="非法参数 %r 不应创建数据库文件 %s" % (value, db),
                )

    # -- 参数边界：换行、空白与其他非数字字符 ------------------------------

    def assertInvalidWithinSeconds(self, result, reason):
        """非法窗口参数的统一错误协议：退出码 2、标准输出为空、
        标准错误指出 --within-seconds 及原因，不输出异常堆栈。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_boundary_invalid_values_rejected_with_existing_db(self):
        db = self.import_events(FOUR_EVENT_ACCEPTANCE, jsonl_name="four.jsonl")
        before = self.snapshot_events(db)
        for value in INVALID_WITHIN_SECONDS_BOUNDARY:
            with self.subTest(value=value):
                result = self.report(db, value)
                # 含非 ASCII 数字字符或为空：原因只接受 ASCII 数字正整数。
                self.assertInvalidWithinSeconds(result, "只含 ASCII 数字")
        # 数据库已有记录保持原样。
        self.assertEqual(self.snapshot_events(db), before)

    def test_boundary_invalid_values_do_not_create_db(self):
        for index, value in enumerate(INVALID_WITHIN_SECONDS_BOUNDARY):
            with self.subTest(value=value):
                db = self.db_path("missing-boundary-%d.sqlite" % index)
                self.assertFalse(os.path.exists(db))
                result = self.report(db, value)
                # 数据库路径不存在时也先返回参数错误。
                self.assertInvalidWithinSeconds(result, "只含 ASCII 数字")
                # 不创建数据库或附属文件。
                self.assertFalse(os.path.exists(db))
                self.assertFalse(os.path.exists(db + "-journal"))
                self.assertFalse(os.path.exists(db + "-wal"))
                self.assertFalse(os.path.exists(db + "-shm"))

    def test_zero_only_values_report_greater_than_zero_reason(self):
        db = self.import_events(FOUR_EVENT_ACCEPTANCE, jsonl_name="four.jsonl")
        for value in ZERO_ONLY_WITHIN_SECONDS:
            with self.subTest(value=value):
                result = self.report(db, value)
                # 仅由零组成：原因说明数值应大于零。
                self.assertInvalidWithinSeconds(result, "大于零")

    def test_trailing_lf_values_follow_same_rejection(self):
        # 60、000、五千个 9 后接真实 LF：与各自无换行前缀的非法结果一致。
        db = self.import_events(FOUR_EVENT_ACCEPTANCE, jsonl_name="four.jsonl")
        for value in ("60\n", "000\n", "9" * 5000 + "\n"):
            with self.subTest(value=value):
                result = self.report(db, value)
                self.assertInvalidWithinSeconds(result, "只含 ASCII 数字")

    # -- 四事件验收样例 ---------------------------------------------------

    def test_four_event_acceptance_windows(self):
        db = self.import_events(FOUR_EVENT_ACCEPTANCE, jsonl_name="four.jsonl")
        # 窗口 60：u1 恰好 60 秒计入（上界包含），u2 间隔 61 秒不计。
        self.assertReportMetrics(self.report(db, 60), 2, 1, 0.5)
        # 前导零写法与 60 等价。
        self.assertReportMetrics(self.report(db, "00060"), 2, 1, 0.5)
        # 窗口 59：两人都不在窗口内。
        self.assertReportMetrics(self.report(db, 59), 2, 0, 0)
        # 超大合法窗口与省略窗口：两人都转化。
        self.assertReportMetrics(self.report(db, HUGE_WITHIN_SECONDS), 2, 2, 1)
        self.assertReportMetrics(self.report(db, HUGE_WITHIN_SECONDS_LONG), 2, 2, 1)
        self.assertReportMetrics(self.report(db), 2, 2, 1)

    # -- 报告不改动已有记录 ----------------------------------------------

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.report(db, 60)
        self.report(db)
        self.assertEqual(self.snapshot_events(db), before)
        # 非法参数的报告同样不得改写记录。
        self.report(db, "0")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
