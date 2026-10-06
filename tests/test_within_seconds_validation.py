"""--within-seconds 参数边界的回归测试。

固定 README 已公开的取值规则（见 README“统计规则”一节）：

- 完整参数值只接受字符 0 至 9 组成且数值大于零的十进制整数，允许前导零；
  不裁剪或忽略任何其他字符
- 空串、空格、制表符、LF、CRLF、正负号、小数、单位后缀、全角数字一律拒绝，
  无论这些字符位于开头、中间还是末尾（本测试对换行一律传入真实换行字符）
- 非法参数统一：退出码 2、标准输出为空、标准错误指出 --within-seconds 及原因，
  不输出异常堆栈，也不得出现 argparse 的内部包装文案
  （invalid parse_within_seconds value）
    * 含非 ASCII 数字字符或为空：原因说明只接受 ASCII 数字的正整数
    * 仅由零组成：原因说明数值应大于零
- 数据库路径不存在时参数错误优先：不创建数据库或 journal/wal/shm 等附属文件；
  数据库已存在时，其事件记录与数量保持原样
- 合法值不设数值或位数上限：00060 与 60 等价，9223372036854775808 与
  五千个字符 9 均成功处理

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部函数。仅使用 Python 3 标准库；每个场景使用独立临时目录中的
JSONL 与 SQLite，全部用户编号均为虚构，测试结束不遗留任何文件，
不依赖预存数据、第三方包或网络。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验收样例：2026-10-06 的四条事件（UTC，时间字符串无时区后缀）。
# u1：10:00:00 visit，10:01:00 signup（间隔 60 秒，窗口上界包含）
# u2：10:00:00 visit，10:01:01 signup（间隔 61 秒，60 秒窗口内不计）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
]

# 超过 SQLite INTEGER 上限（2**63 - 1）的合法大窗口：参数不设数值或位数上限。
HUGE_WITHIN_SECONDS = "9223372036854775808"  # 2**63
HUGE_WITHIN_SECONDS_LONG = "9" * 5000  # 5000 个字符 9

# 含非 ASCII 数字字符（或为空）的非法值：原因必须说明只接受 ASCII 数字的正整数。
# 覆盖空串、空格、制表符、LF、CRLF、正负号、小数、单位后缀、全角数字，
# 且这些字符分别出现在开头、中间与末尾；换行均为真实 LF/CRLF 字符。
NON_DIGIT_VALUES = [
    # 空串与纯空白
    "",
    " ",
    "\t",
    # 空格 / 制表符位于开头、中间、末尾
    " 60",
    "6 0",
    "60 ",
    "\t60",
    "6\t0",
    "60\t",
    # 真实 LF 位于开头、中间、末尾
    "\n",
    "\n60",
    "6\n0",
    "60\n",
    # 真实 CRLF 位于开头、中间、末尾，以及单独 CR
    "\r\n60",
    "6\r\n0",
    "60\r\n",
    "60\r",
    # 正号、负号位于开头、中间、末尾（-0 也不是“仅由零组成”）
    "+60",
    "6+0",
    "60+",
    "-60",
    "-0",
    "6-0",
    "60-",
    # 小数
    "1.5",
    ".5",
    "60.",
    "6.0",
    "0.0",
    # 单位后缀 / 科学计数 / 进制前缀
    "60s",
    "60sec",
    "s60",
    "6s0",
    "1m",
    "1e3",
    "0x3c",
    # 全角数字（U+FF10 至 U+FF19），以及与 ASCII 数字混排
    "６０",
    "０",
    "6０",
    "６0",
    # 本次缺陷的三个重点值：正数与超长正整数末尾的真实 LF
    "60\n",
    "000\n",
    HUGE_WITHIN_SECONDS_LONG + "\n",
]

# 仅由零组成的非法值：原因必须说明数值应大于零（不得落入 ASCII 数字原因）。
ALL_ZERO_VALUES = ["0", "00", "000", "0000"]

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


class WithinSecondsValidationTests(unittest.TestCase):
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

    def import_acceptance_events(self, db=None, jsonl_name="events.jsonl"):
        if db is None:
            db = self.db_path()
        path = self.write_jsonl(jsonl_name, ACCEPTANCE_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 4})
        return db

    def report(self, db, within_seconds):
        return run_funnel("report", "--db", db, "--within-seconds", within_seconds)

    def assert_param_error(self, result, reason_token):
        """非法参数的统一公开约定：退出码 2、标准输出为空、标准错误指出
        --within-seconds 与对应原因；无异常堆栈、无 argparse 内部包装文案。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)
        self.assertIn(reason_token, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("invalid parse_within_seconds", result.stderr)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, count

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

    # -- 含非 ASCII 数字字符或为空：原因只接受 ASCII 数字正整数 ------------

    def test_non_digit_values_rejected_with_ascii_digit_reason(self):
        db = self.import_acceptance_events()
        for value in NON_DIGIT_VALUES:
            with self.subTest(value=value):
                self.assert_param_error(self.report(db, value), "ASCII")

    def test_trailing_lf_positive_value_is_rejected(self):
        # 缺陷主场景：末尾真实 LF 的正数必须被拒绝，而不是当作合法窗口。
        db = self.import_acceptance_events()
        self.assert_param_error(self.report(db, "60\n"), "ASCII")

    def test_trailing_lf_long_value_is_rejected(self):
        # 缺陷主场景：五千个字符 9 后接真实 LF 同样拒绝，不允许任何位数豁免。
        db = self.import_acceptance_events()
        self.assert_param_error(
            self.report(db, HUGE_WITHIN_SECONDS_LONG + "\n"), "ASCII"
        )

    def test_all_zero_with_trailing_lf_uses_ascii_digit_reason(self):
        # "000\n" 含 LF，不属于“仅由零组成”：必须走 ASCII 数字原因，
        # 不得脱离约定输出 argparse 的内部 ValueError 包装。
        db = self.import_acceptance_events()
        result = self.report(db, "000\n")
        self.assert_param_error(result, "ASCII")
        self.assertNotIn("大于零", result.stderr)

    # -- 仅由零组成：原因数值应大于零 -------------------------------------

    def test_all_zero_values_rejected_with_positive_reason(self):
        db = self.import_acceptance_events()
        for value in ALL_ZERO_VALUES:
            with self.subTest(value=value):
                result = self.report(db, value)
                self.assert_param_error(result, "大于零")

    # -- 非法参数先于数据库访问：路径不存在时不创建任何文件 ----------------

    def test_invalid_param_with_missing_db_creates_nothing(self):
        # 三个重点换行值（含五千个字符 9 后接真实 LF）在数据库路径不存在时，
        # 都必须先返回参数错误。
        for value in ("60\n", "000\n", HUGE_WITHIN_SECONDS_LONG + "\n"):
            with self.subTest(value=value[:20]):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                db = os.path.join(tmp.name, "missing.sqlite")
                existing_before = sorted(os.listdir(tmp.name))
                result = run_funnel(
                    "report", "--db", db, "--within-seconds", value
                )
                self.assert_param_error(result, "ASCII")
                self.assertFalse(os.path.exists(db))
                # 主文件之外，journal/wal/shm 等附属文件同样不得出现。
                self.assertEqual(sorted(os.listdir(tmp.name)), existing_before)

    # -- 数据库已存在：非法参数不改变事件记录与数量 ------------------------

    def test_invalid_params_keep_existing_events_unchanged(self):
        db = self.import_acceptance_events()
        before_rows, before_count = self.snapshot_events(db)
        for value in ("60\n", "000\n", HUGE_WITHIN_SECONDS_LONG + "\n", "0", "６０"):
            with self.subTest(value=value[:20]):
                result = self.report(db, value)
                self.assertEqual(result.returncode, 2)
                after_rows, after_count = self.snapshot_events(db)
                self.assertEqual(after_rows, before_rows)
                self.assertEqual(after_count, before_count)

    # -- 合法值不设数值或位数上限，前导零与原值等价 ------------------------

    def test_acceptance_60_seconds_window(self):
        db = self.import_acceptance_events()
        # u1 间隔恰好 60 秒（上界包含）转化；u2 间隔 61 秒不转化。
        self.assert_metrics(self.report(db, "60"), 2, 1, 0.5)

    def test_acceptance_leading_zeros_equal_60(self):
        db = self.import_acceptance_events()
        self.assert_metrics(self.report(db, "00060"), 2, 1, 0.5)

    def test_acceptance_59_seconds_window_excludes_boundary(self):
        db = self.import_acceptance_events()
        # u1 间隔 60 秒超出 59 秒窗口：访问 2 人、转化 0 人、比例 0。
        self.assert_metrics(self.report(db, "59"), 2, 0, 0)

    def test_huge_and_omitted_window_count_both_users(self):
        db = self.import_acceptance_events()
        self.assert_metrics(self.report(db, HUGE_WITHIN_SECONDS), 2, 2, 1.0)
        self.assert_metrics(self.report(db, HUGE_WITHIN_SECONDS_LONG), 2, 2, 1.0)
        # 省略窗口时不限间隔。
        result = run_funnel("report", "--db", db)
        self.assert_metrics(result, 2, 2, 1.0)

    def test_legal_values_remain_accepted(self):
        # 一位正整数、前导零的 1 与 2**63 均不得因修复被误拒。
        db = self.import_acceptance_events()
        for value in ("1", "0000000000000000000001", "9" * 100, HUGE_WITHIN_SECONDS):
            with self.subTest(value=value[:20]):
                result = self.report(db, value)
                self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
