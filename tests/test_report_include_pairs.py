"""--include-pairs 配对明细开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--include-pairs]

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

# 验收样例：2026-10-06 的八条事件。
# u1：10:00:00、10:00:30 各一次 visit，10:01:00、10:02:00 各一次 signup
#     （最早有效 signup 为 10:01:00，与其配对的最晚 visit 为 10:00:30）
# u2：visit 与 signup 同在 10:00:00（相等时刻不算转化）
# u3：09:59:00 signup、10:00:00 visit（signup 早于 visit，任何窗口都不转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:59:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]
ACCEPTANCE_PAIRS = [
    {
        "user_id": "u1",
        "visit_timestamp": "2026-10-06T10:00:30",
        "signup_timestamp": "2026-10-06T10:01:00",
    }
]

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (7, 2, 5, 0, 4, 1, 6, 3)]

# 排序样例：多个转化用户，验证数组按 user_id 原值的 Unicode 码点字典序
# 升序（" " < "A" < "a" < "u"，"u10" < "u2" 按字符而非数值）。
SORT_EVENTS = [
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u10", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u10", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "Alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "Alice", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": " 空格", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": " 空格", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "张三", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]
SORTED_PAIR_IDS = [" 空格", "Alice", "u10", "u2", "张三"]

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
PAIR_KEYS = METRIC_KEYS | {"conversion_pairs"}


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


class IncludePairsTests(unittest.TestCase):
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

    def assertPairsReport(self, result, visit_users, converted_users, rate, pairs):
        """成功配对明细报告：退出码 0、标准错误为空、字段与取值全部相符。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), PAIR_KEYS)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)
        self.assertEqual(payload["conversion_pairs"], pairs)
        # 每个对象只含三个字段，数组长度等于转化人数，编号不重复。
        for pair in pairs:
            self.assertEqual(
                set(pair), {"user_id", "visit_timestamp", "signup_timestamp"}
            )
        self.assertEqual(len(pairs), converted_users)
        self.assertEqual(len({pair["user_id"] for pair in pairs}), len(pairs))

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_include_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertPairsReport(
            self.report(db, "--include-pairs"),
            3, 1, 0.3333333333333333, ACCEPTANCE_PAIRS,
        )

    def test_acceptance_pairs_with_include_users(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db, "--include-pairs", "--include-users")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["conversion_pairs"], ACCEPTANCE_PAIRS)
        # 配对明细中的编号集合等于 converted_user_ids。
        self.assertEqual(
            sorted(pair["user_id"] for pair in payload["conversion_pairs"]),
            payload["converted_user_ids"],
        )

    def test_without_flag_outputs_only_metrics(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.parse_single_json_object(self.report(db).stdout)
        self.assertEqual(set(payload), METRIC_KEYS)

    def test_metrics_identical_between_modes(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        for extra in ((), ("--within-seconds", "60"), ("--within-seconds", "45")):
            with self.subTest(extra=extra):
                plain = self.parse_single_json_object(self.report(db, *extra).stdout)
                detailed = self.parse_single_json_object(
                    self.report(db, *(extra + ("--include-pairs",))).stdout
                )
                for key in METRIC_KEYS:
                    self.assertEqual(detailed[key], plain[key])

    # -- 配对选择规则 -----------------------------------------------------

    def test_earliest_signup_then_latest_visit(self):
        # 验收样例之外再验证一次：最早 signup 确定后，visit 取能与其配对的
        # 最晚一次，而不是全局最晚 visit。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:03:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
        ]
        db = self.import_events(events, jsonl_name="pick.jsonl")
        self.assertPairsReport(
            self.report(db, "--include-pairs"),
            1, 1, 1.0,
            [
                {
                    "user_id": "u1",
                    "visit_timestamp": "2026-10-06T10:00:30",
                    "signup_timestamp": "2026-10-06T10:01:00",
                }
            ],
        )

    def test_within_seconds_boundary_inclusive(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 的 10:00:00 visit 与 10:01:00 signup 间隔恰为 60 秒：上界含等值，
        # 但 10:00:30 的 visit 更晚，配对仍取 10:00:30。
        self.assertPairsReport(
            self.report(db, "--within-seconds", "60", "--include-pairs"),
            3, 1, 0.3333333333333333, ACCEPTANCE_PAIRS,
        )

    def test_within_seconds_filters_pair_candidates(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 45 秒窗口下只有 10:00:30 visit 能与 10:01:00 signup 配对。
        self.assertPairsReport(
            self.report(db, "--within-seconds", "45", "--include-pairs"),
            3, 1, 0.3333333333333333, ACCEPTANCE_PAIRS,
        )
        # 29 秒窗口下 u1 不再转化，配对数组为空。
        self.assertPairsReport(
            self.report(db, "--within-seconds", "29", "--include-pairs"),
            3, 0, 0, [],
        )

    # -- 排序与去重 -------------------------------------------------------

    def test_pairs_sorted_by_code_point(self):
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        result = self.report(db, "--include-pairs")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(
            [pair["user_id"] for pair in payload["conversion_pairs"]],
            SORTED_PAIR_IDS,
        )
        self.assertEqual(len(payload["conversion_pairs"]), payload["converted_users"])

    def test_shuffled_and_duplicate_events_give_same_pairs(self):
        db = self.import_events(
            SHUFFLED_EVENTS + ACCEPTANCE_EVENTS, jsonl_name="shuffled.jsonl"
        )
        self.assertPairsReport(
            self.report(db, "--include-pairs"),
            3, 1, 0.3333333333333333, ACCEPTANCE_PAIRS,
        )

    def test_reimport_same_batch_keeps_pairs(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertPairsReport(
            self.report(db, "--include-pairs"),
            3, 1, 0.3333333333333333, ACCEPTANCE_PAIRS,
        )

    # -- 访问时段与空结果 -------------------------------------------------

    def test_visit_window_filters_pairs(self):
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        # 段内只有 in 的 visit；其 signup 晚于终点仍计入转化与配对。
        self.assertPairsReport(
            self.report(
                db,
                "--visit-from", "2026-10-06T10:00:00",
                "--visit-before", "2026-10-06T11:00:00",
                "--include-pairs",
            ),
            1, 1, 1.0,
            [
                {
                    "user_id": "in",
                    "visit_timestamp": "2026-10-06T10:30:00",
                    "signup_timestamp": "2026-10-06T12:00:00",
                }
            ],
        )

    def test_no_matching_visits_gives_empty_pairs(self):
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        self.assertPairsReport(
            self.report(
                db,
                "--visit-from", "2026-10-07T00:00:00",
                "--visit-before", "2026-10-08T00:00:00",
                "--include-pairs",
            ),
            0, 0, 0, [],
        )

    def test_signup_only_db_reports_zero_with_empty_pairs(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertPairsReport(self.report(db, "--include-pairs"), 0, 0, 0, [])

    # -- 错误协议与只读性 -------------------------------------------------

    def test_invalid_within_seconds_still_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db, "--within-seconds", "0", "--include-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)

    def test_unpaired_visit_window_still_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(
            db, "--visit-from", "2026-10-06T10:00:00", "--include-pairs"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--visit-before", result.stderr)

    def test_missing_db_still_rejected(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--include-pairs")
        self.report(db, "--within-seconds", "60", "--include-pairs", "--include-users")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
