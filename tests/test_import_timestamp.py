"""导入时间戳校验的回归测试。

固定公开约定：

- ``timestamp`` 必须恰好是 ``YYYY-MM-DDTHH:MM:SS`` 形态的有效日历时间
  （统一视为 UTC）：闰日必须真实存在（2024-02-29 合法、2026-02-29
  非法），小时不允许 24；不接受时区后缀（如 ``Z``）或小数秒；
  字段必须是字符串且不得缺失
- 成功导入：退出码 0，标准错误为空，标准输出仅一行 ``{"imported": N}``，
  时间文本按原值落库；合法闰日与跨月时间能正常参与既有 visit -> signup
  漏斗（signup 严格晚于 visit 即算转化）
- 任一行非法：退出码 2、标准输出为空、标准错误给出输入路径、首个错误
  的物理行号与 timestamp 相关原因；不报告后续行，也不输出异常堆栈
- 失败的导入整批不写入：已有数据库逐行不变（合法前缀也不得留下）；
  目标数据库路径原先不存在时不创建数据库或任何附属文件
- 空白行不导入，但仍参与物理行号计算

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部校验函数。仅使用 Python 3 标准库；每个场景使用独立临时目录中的
JSONL 与 SQLite，全部用户编号均为虚构，测试结束不遗留任何文件，
不依赖预存数据、第三方包或网络。期望值全部来自固定事件本身，
不从被测程序的输出反推。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 固定合法样例（2024 为闰年，跨 2 月 / 3 月）：
#   u1 在 2024-02-29T23:59:59 visit，一分钟后 2024-03-01T00:00:00 signup；
#   u2 仅在 2024-02-29T12:00:00 visit，不转化。
# 因此漏斗期望值固定为：访问 2 人、转化 1 人、比例 0.5。
VALID_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2024-02-29T23:59:59"},
    {"user_id": "u1", "event": "signup", "timestamp": "2024-03-01T00:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2024-02-29T12:00:00"},
]

# 导入后库内应恰有这三条原值记录（按 user_id, event, timestamp 排序）。
EXPECTED_VALID_ROWS = [
    ("u1", "signup", "2024-03-01T00:00:00"),
    ("u1", "visit", "2024-02-29T23:59:59"),
    ("u2", "visit", "2024-02-29T12:00:00"),
]

# 每个拒绝文件共用的物理行结构：
#   第 1 行：new-user 的一条合法新用户访问（合法前缀，失败时不得留存）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：待拒绝记录（除 timestamp 外其余字段始终合法）
#   第 4 行：非法 JSON（不得被报告，证明首个错误即停在第 3 行）
NEW_USER_VISIT_LINE = json.dumps(
    {"user_id": "new-user", "event": "visit", "timestamp": "2024-02-28T08:00:00"},
    ensure_ascii=False,
)
INVALID_JSON_LINE = "{"

# (场景名, 第 3 行 JSON 对象, 标准错误必须包含的原因片段)。
REJECTION_CASES = [
    (
        "not_a_leap_day",
        {"user_id": "new-user", "event": "visit", "timestamp": "2026-02-29T10:00:00"},
        ["不是有效时间"],
    ),
    (
        "hour_24",
        {"user_id": "new-user", "event": "visit", "timestamp": "2024-03-01T24:00:00"},
        ["不是有效时间"],
    ),
    (
        "timezone_suffix_z",
        {"user_id": "new-user", "event": "visit", "timestamp": "2024-03-01T10:00:00Z"},
        ["格式"],
    ),
    (
        "fractional_seconds",
        {"user_id": "new-user", "event": "visit", "timestamp": "2024-03-01T10:00:00.5"},
        ["格式"],
    ),
    (
        "integer_timestamp",
        {"user_id": "new-user", "event": "visit", "timestamp": 20240301100000},
        ["必须是字符串"],
    ),
    (
        "missing_timestamp",
        {"user_id": "new-user", "event": "visit"},
        ["缺少", "timestamp"],
    ),
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

    def db_path(self, name):
        return os.path.join(self.tmpdir, name)

    def write_jsonl(self, name, events):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def write_rejection_file(self, name, bad_record):
        """按固定四行物理结构写入拒绝场景输入文件。"""
        path = os.path.join(self.tmpdir, name)
        lines = [
            NEW_USER_VISIT_LINE,
            "",
            json.dumps(bad_record, ensure_ascii=False),
            INVALID_JSON_LINE,
        ]
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

    def assert_rejected_at_line_3(self, path, result, reason_tokens):
        """失败命令的统一公开约定：退出码/输出/路径/行号/原因。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 3 行", result.stderr)
        # 所有场景都是 timestamp 问题，错误信息必须点名字段。
        self.assertIn("timestamp", result.stderr)
        for token in reason_tokens:
            self.assertIn(token, result.stderr)
        # 不得越过第 3 行去报告第 4 行的非法 JSON，也不得吐出异常堆栈。
        self.assertNotIn("第 4 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- 合法闰日与跨月时间：原值落库并参与既有漏斗 ------------------------

    def test_valid_leap_day_and_month_boundary_import_and_report(self):
        db = self.db_path("valid.sqlite")
        path = self.write_jsonl("valid.jsonl", VALID_EVENTS)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        # 标准输出恰为唯一一行 JSON：{"imported": 3}。
        self.assertEqual(result.stdout.splitlines(), ['{"imported": 3}'])
        self.assertEqual(json.loads(result.stdout), {"imported": 3})

        # 库内恰有三条记录，时间文本逐字保存（不做归一化或改写）。
        rows, count = self.snapshot_events(db)
        self.assertEqual(count, 3)
        self.assertEqual(rows, EXPECTED_VALID_ROWS)

        # 既有 report 命令：期望值来自上面三条固定事件——
        # u1、u2 各访问一次（2 人），仅 u1 的 signup 严格晚于其 visit（1 人），
        # 比例固定为 0.5，证明合法闰日与跨月时间能参与漏斗。
        report = run_funnel("report", "--db", db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(report.stderr, "")
        payload = json.loads(report.stdout)
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 0.5)

    # -- 拒绝场景 × 已有三条合法记录的库：逐行不变，合法前缀不留存 ----------

    def test_invalid_timestamps_rejected_against_existing_database(self):
        db = self.db_path("existing.sqlite")
        seed_path = self.write_jsonl("seed.jsonl", VALID_EVENTS)
        seed = run_funnel("import", seed_path, "--db", db)
        self.assertEqual(seed.returncode, 0, msg=seed.stderr)
        self.assertEqual(seed.stderr, "")

        for index, (case_name, bad_record, reason_tokens) in enumerate(
            REJECTION_CASES, start=1
        ):
            with self.subTest(case=case_name):
                path = self.write_rejection_file(
                    "bad-%02d-%s.jsonl" % (index, case_name), bad_record
                )
                before_rows, before_count = self.snapshot_events(db)

                result = run_funnel("import", path, "--db", db)
                self.assert_rejected_at_line_3(path, result, reason_tokens)

                # 整批不写入：失败前后逐行一致。
                after_rows, after_count = self.snapshot_events(db)
                self.assertEqual(after_rows, before_rows)
                self.assertEqual(after_count, before_count)

        # 六个场景全部跑完后，库里仍恰好是最初三条合法记录，
        # 第 1 行的 new-user 合法访问从未留存。
        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_VALID_ROWS)
        self.assertEqual(count, 3)
        self.assertFalse(
            any(row[0] == "new-user" for row in rows),
            msg="失败的导入不得留下 new-user 记录: %r" % (rows,),
        )

    # -- 拒绝场景 × 不存在的库：不创建数据库，合法前缀不留存 ----------------

    def test_invalid_timestamps_rejected_without_creating_database(self):
        for index, (case_name, bad_record, reason_tokens) in enumerate(
            REJECTION_CASES, start=1
        ):
            with self.subTest(case=case_name):
                db = self.db_path("absent-%02d-%s.sqlite" % (index, case_name))
                self.assertFalse(os.path.exists(db))
                path = self.write_rejection_file(
                    "fresh-bad-%02d-%s.jsonl" % (index, case_name), bad_record
                )

                result = run_funnel("import", path, "--db", db)
                self.assert_rejected_at_line_3(path, result, reason_tokens)

                # 数据库主文件及 journal/wal/shm 等附属文件均不得出现：
                # 临时目录中只能留下各场景的输入 JSONL。
                self.assertFalse(os.path.exists(db))
                leftovers = sorted(os.listdir(self.tmpdir))
                self.assertTrue(leftovers)
                self.assertEqual(
                    [name for name in leftovers if not name.endswith(".jsonl")],
                    [],
                    msg="失败导入创建了非输入文件: %r" % (leftovers,),
                )


if __name__ == "__main__":
    unittest.main()
