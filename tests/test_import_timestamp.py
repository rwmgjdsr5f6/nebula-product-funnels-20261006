"""导入时间戳校验的回归测试。

固定公开约定：

- 合法时间戳（固定 ``YYYY-MM-DDTHH:MM:SS`` 格式且日历有效，含闰日与
  跨月边界）：退出码 0，标准错误为空，标准输出唯一一行 JSON
  ``{"imported": N}``，原始时间文本逐字入库，可参与既有 report 漏斗
- 非法时间戳（日历无效、形态错误、非字符串、字段缺失）：退出码 2、
  标准输出为空、标准错误给出输入路径、首个错误行的**物理行号**与
  对应原因（无效时间 / 格式错误 / 非字符串 / 缺少字段）；不报告后续
  行的非法 JSON，也不输出异常堆栈
- 失败的导入整批不写入：已有三条合法记录的库逐行不变；目标路径原先
  不存在时不创建数据库，合法前缀记录不能留存

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部校验函数。期望值全部来自固定输入事件本身，不以被测程序的输出
计算。仅使用 Python 3 标准库；每个场景在独立临时目录中自备输入并在
测试结束时清理，全部用户编号均为虚构，不依赖预存数据、第三方包或网络。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 固定合法样例：u1 在闰日 2024-02-29 末尾访问、跨月瞬间注册（间隔 1 秒，
# 转化）；u2 仅在同一闰日中午访问。三条事件的时间文本逐字固定。
VALID_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2024-02-29T23:59:59"},
    {"user_id": "u1", "event": "signup", "timestamp": "2024-03-01T00:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2024-02-29T12:00:00"},
]

# 导入成功后库内恰有的三行（按 user_id, event, timestamp 排序），
# 时间文本必须与输入原值逐字一致。
EXPECTED_VALID_ROWS = [
    ("u1", "signup", "2024-03-01T00:00:00"),
    ("u1", "visit", "2024-02-29T23:59:59"),
    ("u2", "visit", "2024-02-29T12:00:00"),
]

# 合法样例对应的既有漏斗期望：访问 2 人（u1、u2），转化 1 人（u1），
# 比例 0.5。证明闰日与跨月时间可正常参与 report 统计。
EXPECTED_REPORT = {
    "visit_users": 2,
    "converted_users": 1,
    "conversion_rate": 0.5,
}

# 每个拒绝场景共用的输入骨架（物理行号）：
#   第 1 行：合法的新用户访问（失败时不得留存）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：待拒绝记录（首个错误必须定位在此行）
#   第 4 行：非法 JSON（不得被报告）
FIRST_LINE_VISIT = {"user_id": "fresh-user", "event": "visit",
                    "timestamp": "2024-03-01T10:00:00"}
FOURTH_LINE_INVALID_JSON = "{"

# 拒绝场景：每种非法时间戳一条。record 为放在第 3 行的完整 JSON 对象
# （除 timestamp 外其余字段始终合法）；reason_tokens 为标准错误中
# 对应场景必须出现的原因片段。
REJECTION_CASES = [
    {
        "name": "invalid_calendar_date",
        # 2026 年不是闰年，2 月 29 日日历无效
        "record": {"user_id": "rej-cal", "event": "visit",
                   "timestamp": "2026-02-29T10:00:00"},
        "reason_tokens": ["不是有效时间"],
    },
    {
        "name": "invalid_clock_time",
        # 24:00:00 不是合法钟点
        "record": {"user_id": "rej-hour", "event": "visit",
                   "timestamp": "2024-03-01T24:00:00"},
        "reason_tokens": ["不是有效时间"],
    },
    {
        "name": "timezone_suffix",
        # 固定格式不接受时区后缀
        "record": {"user_id": "rej-zone", "event": "visit",
                   "timestamp": "2024-03-01T10:00:00Z"},
        "reason_tokens": ["格式"],
    },
    {
        "name": "fractional_seconds",
        # 固定格式不接受小数秒
        "record": {"user_id": "rej-frac", "event": "visit",
                   "timestamp": "2024-03-01T10:00:00.5"},
        "reason_tokens": ["格式"],
    },
    {
        "name": "integer_timestamp",
        # timestamp 必须是字符串，整数被拒绝
        "record": {"user_id": "rej-int", "event": "visit",
                   "timestamp": 1709258400},
        "reason_tokens": ["字符串"],
    },
    {
        "name": "missing_timestamp",
        # 缺少必填字段 timestamp
        "record": {"user_id": "rej-miss", "event": "visit"},
        "reason_tokens": ["缺少"],
    },
]


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


class ImportTimestampTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助函数 ---------------------------------------------------------

    def db_path(self, name="events.sqlite"):
        return os.path.join(self.tmpdir, name)

    def write_jsonl(self, name, events):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def write_rejection_input(self, name, record):
        """按固定骨架写入拒绝场景输入：合法访问、空白行、待拒绝记录、
        非法 JSON，共四个物理行。"""
        lines = [
            json.dumps(FIRST_LINE_VISIT, ensure_ascii=False),
            "",
            json.dumps(record, ensure_ascii=False),
            FOURTH_LINE_INVALID_JSON,
        ]
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def snapshot_events(self, db):
        """返回 (逐行记录快照, 记录总数)，用于失败前后逐行比对。"""
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, count

    def seed_valid_events(self, db):
        """通过公开 import 命令建立三条合法记录的库。"""
        path = self.write_jsonl("seed-valid.jsonl", VALID_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="合法样例导入失败:\nstdout=%r\nstderr=%r"
            % (result.stdout, result.stderr),
        )
        return db

    def assert_rejected(self, path, result, reason_tokens):
        """拒绝场景的完整公开约定：退出码/输出/行号/场景原因。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误必须给出输入路径、首个错误物理行号（第 3 行）、
        # 字段名 timestamp 与对应场景的原因。
        self.assertIn(path, result.stderr)
        self.assertIn("第 3 行", result.stderr)
        self.assertIn("timestamp", result.stderr)
        for token in reason_tokens:
            self.assertIn(token, result.stderr)
        # 不得越过首个错误去报告第 4 行的非法 JSON，也不得吐出异常堆栈。
        self.assertNotIn("第 4 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- 合法样例：闰日与跨月时间逐字入库并参与既有漏斗 ------------------

    def test_valid_leap_and_cross_month_timestamps(self):
        db = self.db_path()
        path = self.write_jsonl("valid.jsonl", VALID_EVENTS)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="stdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        # 标准输出唯一一行 JSON，内容恰为 {"imported": 3}。
        stdout_lines = result.stdout.splitlines()
        self.assertEqual(len(stdout_lines), 1)
        self.assertEqual(json.loads(stdout_lines[0]), {"imported": 3})

        # 库内恰有三条记录，时间文本与输入原值逐字一致。
        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_VALID_ROWS)
        self.assertEqual(count, 3)

        # 既有 report 漏斗：访问 2 人、转化 1 人、比例 0.5，
        # 证明合法闰日与跨月时间可以参与统计。
        report = run_funnel("report", "--db", db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(report.stderr, "")
        self.assertEqual(json.loads(report.stdout), EXPECTED_REPORT)

    # -- 拒绝样例：对已有三条合法记录的库与不存在的库分别验证 ------------

    def _run_rejection_case(self, case):
        record = case["record"]
        reason_tokens = case["reason_tokens"]
        path = self.write_rejection_input(
            "reject-%s.jsonl" % case["name"], record
        )

        # (1) 已有三条合法记录的库：失败后逐行不变，合法前缀不能留存。
        db = self.seed_valid_events(self.db_path("seeded.sqlite"))
        before_rows, before_count = self.snapshot_events(db)
        self.assertEqual(before_rows, EXPECTED_VALID_ROWS)

        result = run_funnel("import", path, "--db", db)
        self.assert_rejected(path, result, reason_tokens)

        after_rows, after_count = self.snapshot_events(db)
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_count, before_count)
        self.assertEqual(after_rows, EXPECTED_VALID_ROWS)
        self.assertFalse(
            any(row[0] == record.get("user_id") for row in after_rows),
            msg="失败的导入不得留下待拒绝记录: %r" % (after_rows,),
        )
        self.assertFalse(
            any(row[0] == FIRST_LINE_VISIT["user_id"] for row in after_rows),
            msg="失败的导入不得留下合法前缀记录: %r" % (after_rows,),
        )

        # 既有报告结果不受失败导入影响。
        report = run_funnel("report", "--db", db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(json.loads(report.stdout), EXPECTED_REPORT)

        # (2) 目标路径原先不存在：不创建数据库，合法前缀同样不能留存。
        fresh_db = self.db_path("fresh-%s.sqlite" % case["name"])
        self.assertFalse(os.path.exists(fresh_db))

        result = run_funnel("import", path, "--db", fresh_db)
        self.assert_rejected(path, result, reason_tokens)

        # 数据库主文件不得被创建（journal/wal/shm 等附属文件同理：
        # 临时目录中除输入 JSONL 外不应出现任何其他文件）。
        self.assertFalse(os.path.exists(fresh_db))
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)),
            sorted(["seed-valid.jsonl", os.path.basename(path),
                    "seeded.sqlite"]),
        )


def _make_rejection_test(case):
    def test(self):
        self._run_rejection_case(case)

    test.__name__ = "test_reject_%s" % case["name"]
    test.__doc__ = "拒绝 %r：退出码 2、定位第 3 行、库不变或不建库" % (
        case["record"],
    )
    return test


for _case in REJECTION_CASES:
    setattr(
        ImportTimestampTests,
        "test_reject_%s" % _case["name"],
        _make_rejection_test(_case),
    )
del _case


if __name__ == "__main__":
    unittest.main()
