"""--include-latency 整体转化耗时开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> --include-latency
                            [--within-seconds N] [--include-pairs]
                            [--include-users] [--group-by visit-date
                             [--include-group-pairs]]

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

# 验收样例：2026-10-06 的十条事件。
# u1：10:00:00、10:00:30、10:01:59 各一次 visit，10:01:00、10:02:00 各一次
#     signup（最早有效 signup 10:01:00，能与其配对的最晚 visit 10:00:30，
#     耗时 30 秒；不能取 10:01:59 visit 与 10:02:00 signup 的 1 秒间隔）
# u2：10:00:00 visit、10:01:00 signup（耗时 60 秒）
# u3：visit 与 signup 同在 10:00:00（相等时刻不算转化）
# u4：仅 10:00:00 visit（无注册不转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:01:59"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]
ACCEPTANCE_LATENCY = {"min_seconds": 30, "max_seconds": 60, "mean_seconds": 45.0}

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (9, 0, 7, 4, 2, 6, 8, 1, 5, 3)]

# 平均不取整样例：两个转化用户分别耗时 30、61 秒，算术平均 45.5。
MEAN_EVENTS = [
    {"user_id": "a", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "a", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "b", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "b", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
]

VISIT_ONLY_EVENTS = [
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}
LATENCY_KEYS = METRIC_KEYS | {"conversion_latency"}
NULL_LATENCY = {"min_seconds": None, "max_seconds": None, "mean_seconds": None}


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


class IncludeLatencyTests(unittest.TestCase):
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

    def assertLatencyReport(self, result, visit_users, converted_users, rate, latency):
        """成功耗时报告：退出码 0、标准错误为空、字段与取值全部相符。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), LATENCY_KEYS)
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)
        self.assertEqual(payload["conversion_latency"], latency)
        self.assertEqual(
            set(payload["conversion_latency"]),
            {"min_seconds", "max_seconds", "mean_seconds"},
        )

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_include_latency(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 贡献 30 秒（最早 signup 10:01:00 配最晚可配对 visit 10:00:30），
        # u2 贡献 60 秒；min=30、max=60、mean=(30+60)/2=45。
        self.assertLatencyReport(
            self.report(db, "--include-latency"), 4, 2, 0.5, ACCEPTANCE_LATENCY
        )

    def test_acceptance_within_seconds_29(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 29 秒窗口下：u1 的最早 signup 10:01:00 与两次更早访问间隔分别为
        # 60、30 秒，均 > 29，该注册在当前条件下没有任何合格 visit，不再是
        # "有效 signup"；最早有效 signup 变为 10:02:00，能与其配对的最晚
        # visit 为 10:01:59，耗时恰为 1 秒。u2 间隔 60 秒被排除，u3/u4
        # 本不转化——仅 u1 转化，三项均为 1。配对归约只枚举当前条件下的
        # 有效行对，"最早 signup"始终指最早的有效 signup。
        self.assertLatencyReport(
            self.report(db, "--include-latency", "--within-seconds", "29"),
            4, 1, 0.25,
            {"min_seconds": 1, "max_seconds": 1, "mean_seconds": 1.0},
        )

    def test_within_seconds_30_excludes_u2_keeps_u1_at_30(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 30 秒窗口：u1 取 (10:00:30, 10:01:00) 间隔恰 30（上界含等值）；
        # u2 间隔 60 被排除。仅 u1 转化，耗时 30。
        self.assertLatencyReport(
            self.report(db, "--include-latency", "--within-seconds", "30"),
            4, 1, 0.25,
            {"min_seconds": 30, "max_seconds": 30, "mean_seconds": 30.0},
        )

    def test_within_seconds_29_does_not_pick_shortest_of_all_pairs(self):
        # 显式守护"不能取所有配对的最短间隔"这一口径：若错误地先按全条件
        # 锁定最早 signup、再取全局最短，或直接取该用户所有行对的最小间隔，
        # 都会与窗口过滤后的归约结果不同。用更紧的 1 秒窗口复核：唯一有效
        # 行对是 (10:01:59, 10:02:00)，耗时 1；而无窗口时 u1 必须贡献 30。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        self.assertLatencyReport(
            self.report(db, "--include-latency", "--within-seconds", "1"),
            4, 1, 0.25,
            {"min_seconds": 1, "max_seconds": 1, "mean_seconds": 1.0},
        )
        self.assertLatencyReport(
            self.report(db, "--include-latency"), 4, 2, 0.5, ACCEPTANCE_LATENCY
        )

    # -- 开关独立性与字段控制 ---------------------------------------------

    def test_without_flag_outputs_only_metrics(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.parse_single_json_object(self.report(db).stdout)
        self.assertEqual(set(payload), METRIC_KEYS)

    def test_latency_does_not_require_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # 独立使用：有 conversion_latency，无 conversion_pairs。
        payload = self.parse_single_json_object(
            self.report(db, "--include-latency").stdout
        )
        self.assertEqual(set(payload), LATENCY_KEYS)
        self.assertNotIn("conversion_pairs", payload)

    def test_latency_consistent_with_pairs_payload(self):
        from datetime import datetime

        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.parse_single_json_object(
            self.report(db, "--include-latency", "--include-pairs").stdout
        )
        fmt = "%Y-%m-%dT%H:%M:%S"
        durations = sorted(
            (
                datetime.strptime(p["signup_timestamp"], fmt)
                - datetime.strptime(p["visit_timestamp"], fmt)
            ).total_seconds()
            for p in payload["conversion_pairs"]
        )
        latency = payload["conversion_latency"]
        self.assertEqual(latency["min_seconds"], int(durations[0]))
        self.assertEqual(latency["max_seconds"], int(durations[-1]))
        self.assertEqual(
            latency["mean_seconds"], sum(durations) / len(durations)
        )
        # 每人恰好贡献一个耗时：配对数等于转化人数。
        self.assertEqual(len(payload["conversion_pairs"]), payload["converted_users"])

    def test_min_max_are_ints_mean_is_unrounded_float(self):
        db = self.import_events(MEAN_EVENTS, jsonl_name="mean.jsonl")
        payload = self.parse_single_json_object(
            self.report(db, "--include-latency").stdout
        )
        latency = payload["conversion_latency"]
        self.assertEqual(latency["min_seconds"], 30)
        self.assertEqual(latency["max_seconds"], 61)
        self.assertEqual(latency["mean_seconds"], 45.5)
        self.assertIsInstance(latency["min_seconds"], int)
        self.assertIsInstance(latency["max_seconds"], int)
        self.assertIsInstance(latency["mean_seconds"], float)

    # -- 空结果 -----------------------------------------------------------

    def test_visit_only_gives_null_latency(self):
        db = self.import_events(VISIT_ONLY_EVENTS, jsonl_name="visit_only.jsonl")
        self.assertLatencyReport(
            self.report(db, "--include-latency"), 1, 0, 0, NULL_LATENCY
        )

    def test_signup_only_gives_null_latency(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        self.assertLatencyReport(
            self.report(db, "--include-latency"), 0, 0, 0, NULL_LATENCY
        )

    # -- 分组合用：只在顶层追加，组内不追加 -------------------------------

    def test_latency_not_added_inside_groups(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--include-latency",
                "--group-by", "visit-date",
                "--include-users",
                "--include-group-pairs",
            ).stdout
        )
        # 顶层有耗时对象。
        self.assertEqual(payload["conversion_latency"], ACCEPTANCE_LATENCY)
        # 组对象不含任何耗时字段，原有字段保持不变。
        for group in payload["visit_date_groups"]:
            self.assertNotIn("conversion_latency", group)
            self.assertNotIn("min_seconds", group)
            self.assertIn("conversion_pairs", group)
            self.assertIn("visit_user_ids", group)

    # -- 行序、重复事件与重复导入 -----------------------------------------

    def test_shuffled_and_duplicate_events_give_same_latency(self):
        db = self.import_events(
            SHUFFLED_EVENTS + ACCEPTANCE_EVENTS, jsonl_name="shuffled.jsonl"
        )
        self.assertLatencyReport(
            self.report(db, "--include-latency"), 4, 2, 0.5, ACCEPTANCE_LATENCY
        )

    def test_reimport_same_batch_keeps_latency(self):
        path = self.write_jsonl("acceptance.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertLatencyReport(
            self.report(db, "--include-latency"), 4, 2, 0.5, ACCEPTANCE_LATENCY
        )

    # -- 错误协议与只读性 -------------------------------------------------

    def test_invalid_within_seconds_still_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(db, "--within-seconds", "0", "--include-latency")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--within-seconds", result.stderr)

    def test_missing_db_still_rejected(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-latency")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--include-latency")
        self.report(db, "--include-latency", "--within-seconds", "60",
                    "--include-pairs", "--group-by", "visit-date")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
