"""访问时段筛选下三种输出（汇总、编号明细、配对明细、日期分组）一致性的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite>
        --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-08T00:00:00
        --within-seconds 60 --include-users --include-pairs
        --group-by visit-date

固定合成样例的逐人设定与预期推导见
docs/verification-window-grouped-consistency.md。

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

# 验收样例：十条事件，年份均为 2026，时间戳无时区后缀，统一按 UTC 解释。
# u1：5 日 23:59:00 visit（段外历史，不计入也不参与配对）、
#     6 日 10:00:00 visit（恰在起点，含起点，计入且决定归组）、
#     7 日 23:59:00 与 23:59:30 各 visit 一次、8 日 00:00:30 signup
#     （与 23:59:30 的 visit 间隔恰 60 秒，含等值，转化）
# u2：7 日 12:00:00 同时刻 visit 与 signup（相等时刻不算转化）
# u3：8 日 00:00:00 visit（恰在终点，终点不含，被排除）、00:00:30 signup
# u4：仅 6 日 12:00:00 visit（不转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-05T23:59:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T23:59:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T23:59:30"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T00:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-07T12:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-08T00:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-08T00:00:30"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T12:00:00"},
]

# 访问时段：含起点、不含终点。
WINDOW_FROM = "2026-10-06T10:00:00"
WINDOW_BEFORE = "2026-10-08T00:00:00"

EXPECTED_GROUP_KEYS = {
    "visit_date",
    "visit_users",
    "converted_users",
    "conversion_rate",
}

EXPECTED_PAIR_KEYS = {"user_id", "visit_timestamp", "signup_timestamp"}


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


class WindowGroupedReportConsistencyTests(unittest.TestCase):
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

    def import_acceptance(self):
        """导入固定样例到新库，核对 imported 为 10，返回数据库路径。"""
        path = self.write_jsonl("events.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path()
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 10})
        return db

    def report(self, db, *extra_args):
        return run_funnel("report", "--db", db, *extra_args)

    def full_report(self, db, within_seconds):
        """三种输出同时启用的时段报告。"""
        return self.report(
            db,
            "--visit-from",
            WINDOW_FROM,
            "--visit-before",
            WINDOW_BEFORE,
            "--within-seconds",
            str(within_seconds),
            "--include-users",
            "--include-pairs",
            "--group-by",
            "visit-date",
        )

    @staticmethod
    def parse_single_json_object(stdout):
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        assert nonempty_lines, "标准输出为空，无法解析 JSON 对象: %r" % stdout
        assert len(nonempty_lines) == 1, "标准输出包含多行，不是单个 JSON 对象: %r" % stdout
        obj = json.loads(nonempty_lines[0])
        assert isinstance(obj, dict), "标准输出不是 JSON 对象: %r" % stdout
        return obj

    def parse_success_payload(self, result):
        """成功报告：退出码 0、标准错误为空、标准输出为单个 JSON 对象。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_object(result.stdout)

    def assertGroups(self, payload, expected):
        """expected 为 (visit_date, visit_users, converted_users, rate) 元组列表，
        顺序即期望的数组顺序（按日期升序）。"""
        groups = payload["visit_date_groups"]
        self.assertIsInstance(groups, list)
        self.assertEqual(len(groups), len(expected))
        for group, (day, visits, converted, rate) in zip(groups, expected):
            self.assertEqual(set(group), EXPECTED_GROUP_KEYS)
            self.assertEqual(group["visit_date"], day)
            self.assertEqual(group["visit_users"], visits)
            self.assertEqual(group["converted_users"], converted)
            self.assertEqual(group["conversion_rate"], rate)
        # 按日期升序，且各组两种人数之和分别等于汇总人数。
        days = [group["visit_date"] for group in groups]
        self.assertEqual(days, sorted(days))
        self.assertEqual(
            sum(group["visit_users"] for group in groups), payload["visit_users"]
        )
        self.assertEqual(
            sum(group["converted_users"] for group in groups),
            payload["converted_users"],
        )

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例：--within-seconds 60 ------------------------------------

    def test_acceptance_window_60s_all_outputs_consistent(self):
        db = self.import_acceptance()
        payload = self.parse_success_payload(self.full_report(db, 60))

        # 汇总：段内访问 u1/u2/u4 共 3 人；仅 u1 转化（间隔恰 60 秒，含等值）。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)

        # 编号明细：访问编号依次为 u1、u2、u4，转化编号仅 u1。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u4"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])

        # 配对明细：仅 u1，最晚有效 visit 配对最早有效 signup。
        pairs = payload["conversion_pairs"]
        self.assertIsInstance(pairs, list)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(set(pairs[0]), EXPECTED_PAIR_KEYS)
        self.assertEqual(
            pairs,
            [
                {
                    "user_id": "u1",
                    "visit_timestamp": "2026-10-07T23:59:30",
                    "signup_timestamp": "2026-10-08T00:00:30",
                }
            ],
        )

        # 日期分组：u1 的段外历史不影响归组，最早段内 visit 在 6 日，
        # 仍归 6 日组；u4 同组，u2 归 7 日组；u3 终点访问被排除不出现。
        self.assertGroups(
            payload,
            [
                ("2026-10-06", 2, 1, 0.5),
                ("2026-10-07", 1, 0, 0),
            ],
        )

    # -- 同一样例：--within-seconds 59 ------------------------------------

    def test_acceptance_window_59s_zeroes_all_conversions(self):
        db = self.import_acceptance()
        payload = self.parse_success_payload(self.full_report(db, 59))

        # 访问人数与归组不变；u1 的间隔 60 秒超出 59 秒上界，转化全部归零。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 0)
        self.assertEqual(payload["conversion_rate"], 0)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u4"])
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["conversion_pairs"], [])
        self.assertGroups(
            payload,
            [
                ("2026-10-06", 2, 0, 0),
                ("2026-10-07", 1, 0, 0),
            ],
        )

    # -- 缺少配对参数 ------------------------------------------------------

    def test_visit_from_alone_rejected_and_records_unchanged(self):
        db = self.import_acceptance()
        before = self.snapshot_events(db)
        result = self.report(
            db,
            "--visit-from",
            WINDOW_FROM,
            "--within-seconds",
            "60",
            "--include-users",
            "--include-pairs",
            "--group-by",
            "visit-date",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        # 标准错误指出缺少的配对参数及原因，不绑定错误全文。
        self.assertIn("--visit-from", result.stderr)
        self.assertIn("--visit-before", result.stderr)
        self.assertIn("成对", result.stderr)
        # 已有记录不变。
        self.assertEqual(self.snapshot_events(db), before)

    # -- 报告只读：前后事件逐行一致 ----------------------------------------

    def test_reports_keep_events_unchanged(self):
        db = self.import_acceptance()
        before = self.snapshot_events(db)
        self.assertEqual(len(before), 10)
        self.full_report(db, 60)
        self.full_report(db, 59)
        self.report(db, "--visit-from", WINDOW_FROM)  # 参数错误也不改动记录
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
