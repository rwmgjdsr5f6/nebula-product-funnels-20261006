"""--include-pairs 转化配对明细开关的回归测试。

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
#     → 最早 signup 10:01:00，能与之配对的最晚 visit 是 10:00:30
# u2：visit 与 signup 同在 10:00:00（相等不算转化）
# u3：09:59:00 signup 早于 10:00:00 visit（永不转化）
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

ACCEPTANCE_PAIR = {
    "user_id": "u1",
    "visit_timestamp": "2026-10-06T10:00:30",
    "signup_timestamp": "2026-10-06T10:01:00",
}

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (7, 0, 5, 3, 1, 6, 4, 2)]

# 每人多个候选对：
# u1：10:00:00、10:03:00 visit；10:02:00、10:04:00 signup。
#     不限窗口时最早 signup 10:02 只能与 10:00 的 visit 配对（10:03 晚于它）；
#     --within-seconds 60 时 10:02 无有效配对，最早有效 signup 变为 10:04，
#     与之配对的最晚 visit 是 10:03。
SELECTION_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:03:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:04:00"},
]

# 排序样例：编号含大小写与中文，验证按 Unicode 码点字典序
# （"A" < "a" < "u"，中文排在 ASCII 之后）。
SORT_EVENTS = [
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "张三", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "alice", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "Alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "Alice", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]
SORTED_PAIR_USER_IDS = ["Alice", "alice", "u2", "张三"]

# 时段样例：段内 visit 的 signup 可晚于段终点；段外 visit 不参与配对。
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
PAIR_KEYS = {"user_id", "visit_timestamp", "signup_timestamp"}


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

    def import_events(self, events, db=None, jsonl_name="events.jsonl", repeat=1):
        if db is None:
            db = self.db_path()
        path = self.write_jsonl(jsonl_name, events)
        for _ in range(repeat):
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

    def assertPairsSuccess(self, result, visit_users, converted_users, rate,
                           pairs, include_users=False):
        """成功配对报告：退出码 0、标准错误为空、指标与配对数组全部相符。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        expected_keys = set(METRIC_KEYS) | {"conversion_pairs"}
        if include_users:
            expected_keys |= {"visit_user_ids", "converted_user_ids"}
        self.assertEqual(set(payload), expected_keys)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)
        self.assertEqual(payload["conversion_pairs"], pairs)
        # 数组长度等于转化人数，每人至多一对，对象只含三个字段。
        self.assertEqual(len(pairs), converted_users)
        user_ids = [pair["user_id"] for pair in pairs]
        self.assertEqual(len(user_ids), len(set(user_ids)))
        for pair in pairs:
            self.assertEqual(set(pair), PAIR_KEYS)
        # 按 user_id 原值的 Unicode 码点字典序升序。
        self.assertEqual(user_ids, sorted(user_ids))
        return payload

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_include_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertPairsSuccess(
            self.report(db, "--include-pairs"),
            3, 1, 0.3333333333333333, [ACCEPTANCE_PAIR],
        )

    def test_without_flag_output_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db)
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), METRIC_KEYS)
        self.assertNotIn("conversion_pairs", payload)

    def test_pairs_do_not_change_metrics(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        for extra in ((), ("--within-seconds", "30"), ("--within-seconds", "29")):
            with self.subTest(extra=extra):
                plain = self.parse_single_json_object(self.report(db, *extra).stdout)
                detailed = self.parse_single_json_object(
                    self.report(db, *(extra + ("--include-pairs",))).stdout
                )
                for key in METRIC_KEYS:
                    self.assertEqual(detailed[key], plain[key])

    def test_combined_with_include_users(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.assertPairsSuccess(
            self.report(db, "--include-pairs", "--include-users"),
            3, 1, 0.3333333333333333, [ACCEPTANCE_PAIR], include_users=True,
        )
        # 配对编号集合等于 converted_user_ids。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(
            [p["user_id"] for p in payload["conversion_pairs"]],
            payload["converted_user_ids"],
        )

    # -- 选取规则：最早 signup，再最晚 visit ------------------------------

    def test_earliest_signup_then_latest_visit(self):
        db = self.import_events(SELECTION_EVENTS, jsonl_name="selection.jsonl")
        self.assertPairsSuccess(
            self.report(db, "--include-pairs"),
            1, 1, 1.0,
            [{
                "user_id": "u1",
                "visit_timestamp": "2026-10-06T10:00:00",
                "signup_timestamp": "2026-10-06T10:02:00",
            }],
        )

    def test_within_seconds_changes_earliest_valid_signup(self):
        db = self.import_events(SELECTION_EVENTS, jsonl_name="selection.jsonl")
        # 60 秒窗口：10:02 signup 与任何 visit 间隔都超过 60 秒（120 秒），
        # 最早有效 signup 变为 10:04，与之配对的最晚 visit 是 10:03。
        self.assertPairsSuccess(
            self.report(db, "--within-seconds", "60", "--include-pairs"),
            1, 1, 1.0,
            [{
                "user_id": "u1",
                "visit_timestamp": "2026-10-06T10:03:00",
                "signup_timestamp": "2026-10-06T10:04:00",
            }],
        )

    def test_within_seconds_boundary_included(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 的选中对间隔恰为 30 秒：上界 30 仍计入，29 则无转化。
        self.assertPairsSuccess(
            self.report(db, "--within-seconds", "30", "--include-pairs"),
            3, 1, 0.3333333333333333, [ACCEPTANCE_PAIR],
        )
        self.assertPairsSuccess(
            self.report(db, "--within-seconds", "29", "--include-pairs"),
            3, 0, 0, [],
        )

    # -- 排序、去重与行序 -------------------------------------------------

    def test_pairs_sorted_by_user_id_code_point(self):
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        payload = self.assertPairsSuccess(
            self.report(db, "--include-pairs"),
            4, 4, 1.0,
            [{
                "user_id": user_id,
                "visit_timestamp": "2026-10-06T10:00:00",
                "signup_timestamp": "2026-10-06T10:00:30",
            } for user_id in SORTED_PAIR_USER_IDS],
        )
        self.assertEqual(
            [p["user_id"] for p in payload["conversion_pairs"]],
            SORTED_PAIR_USER_IDS,
        )

    def test_shuffled_duplicates_and_reimport_give_same_pairs(self):
        db = self.import_events(
            SHUFFLED_EVENTS + ACCEPTANCE_EVENTS, jsonl_name="mixed.jsonl", repeat=2
        )
        self.assertPairsSuccess(
            self.report(db, "--include-pairs"),
            3, 1, 0.3333333333333333, [ACCEPTANCE_PAIR],
        )

    # -- 访问时段与空结果 -------------------------------------------------

    def test_visit_window_filters_pairs(self):
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        # 段内只有 in 的 visit；signup 晚于终点仍可配对；
        # out 只有段外 visit，不参与配对。
        self.assertPairsSuccess(
            self.report(
                db,
                "--visit-from", "2026-10-06T10:00:00",
                "--visit-before", "2026-10-06T11:00:00",
                "--include-pairs",
            ),
            1, 1, 1.0,
            [{
                "user_id": "in",
                "visit_timestamp": "2026-10-06T10:30:00",
                "signup_timestamp": "2026-10-06T12:00:00",
            }],
        )

    def test_no_conversion_gives_empty_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertPairsSuccess(
            self.report(db, "--within-seconds", "29", "--include-pairs"),
            3, 0, 0, [],
        )

    def test_window_without_visits_gives_empty_pairs(self):
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        self.assertPairsSuccess(
            self.report(
                db,
                "--visit-from", "2026-10-07T00:00:00",
                "--visit-before", "2026-10-08T00:00:00",
                "--include-pairs",
            ),
            0, 0, 0, [],
        )

    def test_signup_only_db_gives_empty_pairs(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertPairsSuccess(self.report(db, "--include-pairs"), 0, 0, 0, [])

    # -- 错误协议与只读性 -------------------------------------------------

    def test_missing_db_still_rejected(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_invalid_within_seconds_rejected_before_db(self):
        db = self.db_path("missing-invalid.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--within-seconds", "0", "--include-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_unpaired_window_params_rejected_before_db(self):
        db = self.db_path("missing-unpaired.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--visit-from", "2026-10-06T10:00:00",
                             "--include-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--visit-before", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--include-pairs")
        self.report(db, "--include-pairs", "--include-users")
        self.report(db, "--within-seconds", "30", "--include-pairs")
        self.report(
            db,
            "--visit-from", "2026-10-06T10:00:00",
            "--visit-before", "2026-10-06T11:00:00",
            "--include-pairs",
        )
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
