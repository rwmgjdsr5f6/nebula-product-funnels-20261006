"""--include-group-latency 组内转化耗时开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> --group-by visit-date
                              --include-group-latency [--within-seconds N]

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
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验收样例：七条事件，时间均为 2026 年 10 月的 UTC。
# u1：6 日 23:59:00 visit，7 日 10:00:00、10:00:30 各 visit 一次，
#     7 日 10:01:00 signup（按最早合格 visit 归 6 日组；配对取能与最早
#     signup 配对的最晚 visit，即 7 日 10:00:30，耗时 30 秒——配对访问
#     落在次日也不移动所属组）
# u2：6 日 12:00:00 visit、12:01:00 signup（归 6 日组，耗时 60 秒）
# u3：仅 7 日 11:00:00 visit（归 7 日组，无转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:30"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T12:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T12:01:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T11:00:00"},
]
GROUP_6TH_LATENCY = {
    "min_seconds": 30,
    "max_seconds": 60,
    "mean_seconds": 45.0,
}
NULL_LATENCY = {"min_seconds": None, "max_seconds": None, "mean_seconds": None}

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (6, 0, 5, 2, 4, 1, 3)]

# 混入完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:30"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T12:01:00"},
]

# 两个日期组都有转化的样例：6 日组 a=30 秒、b=61 秒（平均 45.5，不取整），
# 7 日组 c=5 秒。顶层平均为 (30+61+5)/3 = 32.0，与任一组都不同，
# 可据此核对组内统计只数本组转化用户。
TWO_GROUP_EVENTS = [
    {"user_id": "a", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "a", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "b", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "b", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
    {"user_id": "c", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "c", "event": "signup", "timestamp": "2026-10-07T10:00:05"},
]

# 最早有效 signup 配对最晚可配对 visit：最早 signup 10:01:00 配最晚
# visit 10:00:30，锁定耗时 30 秒；更晚的 signup 10:02:00（其配对最短
# 间隔 90 秒）不参与，也不是取全部配对的最短间隔。
PICK_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:03:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
]

# 注册晚于访问时段终点：段内 visit 6 日 23:59:00，signup 8 日 00:00:00，
# 耗时 1 天零 1 分钟 = 86460 秒。
WINDOW_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T00:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

GROUP_BASE_KEYS = {
    "visit_date",
    "visit_users",
    "converted_users",
    "conversion_rate",
}
GROUP_LATENCY_KEYS = GROUP_BASE_KEYS | {"conversion_latency"}
TOP_LEVEL_BASE_KEYS = {"visit_users", "converted_users", "conversion_rate"}
LATENCY_SUBKEYS = {"min_seconds", "max_seconds", "mean_seconds"}


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


class IncludeGroupLatencyTests(unittest.TestCase):
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

    def assertGroupLatencies(self, payload, expected):
        """expected 为 (visit_date, latency_dict) 元组列表，逐组核对。

        同时核对：组对象键集合恰为基础四键加 conversion_latency；耗时
        对象只含三个字段；组内转化人数为 0 时三项均为 None。
        """
        groups = payload["visit_date_groups"]
        self.assertEqual(len(groups), len(expected))
        for group, (day, latency) in zip(groups, expected):
            self.assertEqual(group["visit_date"], day)
            self.assertEqual(set(group), GROUP_LATENCY_KEYS)
            self.assertEqual(set(group["conversion_latency"]), LATENCY_SUBKEYS)
            self.assertEqual(group["conversion_latency"], latency)
            if group["converted_users"] == 0:
                self.assertEqual(group["conversion_latency"], NULL_LATENCY)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_seven_events_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-latency",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 汇总：3 人访问、2 人转化；新开关不自动开启任何顶层明细。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 2)
        self.assertEqual(payload["conversion_rate"], 2 / 3)
        self.assertNotIn("conversion_latency", payload)
        self.assertNotIn("conversion_pairs", payload)
        self.assertNotIn("visit_user_ids", payload)
        # 6 日组 u1=30、u2=60 → 30/60/45；7 日组 u3 不转化 → 三项 null。
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", GROUP_6TH_LATENCY), ("2026-10-07", NULL_LATENCY)],
        )
        group_6th, group_7th = payload["visit_date_groups"]
        self.assertEqual(group_6th["visit_users"], 2)
        self.assertEqual(group_6th["converted_users"], 2)
        self.assertEqual(group_6th["conversion_rate"], 1.0)
        self.assertEqual(group_7th["visit_users"], 1)
        self.assertEqual(group_7th["converted_users"], 0)
        self.assertEqual(group_7th["conversion_rate"], 0.0)

    def test_acceptance_seven_events_within_60_types(self):
        # min/max 为整数秒，均值为浮点；无转化时三项均为 None。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--within-seconds",
                "60",
            ).stdout
        )
        latency = payload["visit_date_groups"][0]["conversion_latency"]
        self.assertIsInstance(latency["min_seconds"], int)
        self.assertIsInstance(latency["max_seconds"], int)
        self.assertIsInstance(latency["mean_seconds"], float)

    def test_acceptance_without_within_seconds_matches(self):
        # 不传 --within-seconds：配对时限不限，u1 仍取 30 秒（最早 signup
        # 配最晚 visit 的归约与窗口无关），结果与 60 秒窗口一致。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        self.assertEqual(payload["converted_users"], 2)
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", GROUP_6TH_LATENCY), ("2026-10-07", NULL_LATENCY)],
        )

    # -- 归组与配对口径 ---------------------------------------------------

    def test_pair_visit_next_day_does_not_move_group(self):
        # u1 的耗时来自 7 日的 visit，但本人仍归 6 日组；7 日组只有
        # 无转化的 u3。若错误地按配对访问日期归组，u1 会落进 7 日组。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--within-seconds",
                "60",
            ).stdout
        )
        group_6th, group_7th = payload["visit_date_groups"]
        self.assertEqual(group_6th["visit_date"], "2026-10-06")
        self.assertEqual(group_6th["visit_users"], 2)
        self.assertEqual(group_6th["conversion_latency"], GROUP_6TH_LATENCY)
        self.assertEqual(group_7th["visit_date"], "2026-10-07")
        self.assertEqual(group_7th["visit_users"], 1)
        self.assertEqual(group_7th["conversion_latency"], NULL_LATENCY)

    def test_within_seconds_boundary_inclusive(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        # 恰为 60 秒：u2 的 60 秒计入（上界含等值），6 日组两个转化。
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--within-seconds",
                "60",
            ).stdout
        )
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", GROUP_6TH_LATENCY), ("2026-10-07", NULL_LATENCY)],
        )
        # 59 秒窗口：u2 被排除，6 日组只剩 u1 的 30 秒；7 日组仍全 null。
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--within-seconds",
                "59",
            ).stdout
        )
        self.assertEqual(payload["converted_users"], 1)
        self.assertGroupLatencies(
            payload,
            [
                ("2026-10-06", {"min_seconds": 30, "max_seconds": 30,
                                "mean_seconds": 30.0}),
                ("2026-10-07", NULL_LATENCY),
            ],
        )

    def test_earliest_signup_latest_visit_in_group(self):
        # 组内耗时沿用顶层口径：最早有效 signup 10:01:00 配能配对它的
        # 最晚 visit 10:00:30，贡献 30 秒，不取其他配对的更短间隔。
        db = self.import_events(PICK_EVENTS, jsonl_name="pick.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", {"min_seconds": 30, "max_seconds": 30,
                             "mean_seconds": 30.0})],
        )

    def test_group_mean_is_unrounded_and_scoped_per_group(self):
        # 6 日组平均 (30+61)/2 = 45.5（真除不取整）；7 日组仅 5 秒；
        # 任何一组都不是全体 32.0 的顶层平均。
        db = self.import_events(TWO_GROUP_EVENTS, jsonl_name="two.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        self.assertGroupLatencies(
            payload,
            [
                ("2026-10-06", {"min_seconds": 30, "max_seconds": 61,
                                "mean_seconds": 45.5}),
                ("2026-10-07", {"min_seconds": 5, "max_seconds": 5,
                                "mean_seconds": 5.0}),
            ],
        )

    def test_visit_window_signup_may_fall_after_end(self):
        # 段内 visit（6 日 23:59，含起点、不含终点）配段外 signup（8 日）：
        # 注册允许晚于段终点，耗时 86460 秒；无其他日期组。
        db = self.import_events(WINDOW_EVENTS, jsonl_name="window.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--visit-from",
                "2026-10-06T00:00:00",
                "--visit-before",
                "2026-10-07T00:00:00",
            ).stdout
        )
        self.assertEqual(payload["visit_users"], 1)
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", {"min_seconds": 86460, "max_seconds": 86460,
                             "mean_seconds": 86460.0})],
        )

    def test_visit_window_excludes_out_of_range_pair_visits(self):
        # 时段 [6 日 00:00, 7 日 00:00)：u1 的两次 7 日 visit 在段外，
        # 唯一段内 visit 23:59 与 signup 相隔 10 小时余，60 秒窗口下不转化；
        # u2 段内 visit + 60 秒 signup 仍转化。7 日访问（u3）整组不出现。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--within-seconds",
                "60",
                "--visit-from",
                "2026-10-06T00:00:00",
                "--visit-before",
                "2026-10-07T00:00:00",
            ).stdout
        )
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 1)
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", {"min_seconds": 60, "max_seconds": 60,
                             "mean_seconds": 60.0})],
        )

    # -- 开关独立性与字段控制 ---------------------------------------------

    def test_flag_does_not_auto_enable_other_details(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-latency"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        # 顶层只有三汇总字段加分组长数组，无顶层耗时/配对/编号。
        self.assertEqual(
            set(payload), TOP_LEVEL_BASE_KEYS | {"visit_date_groups"}
        )
        self.assertNotIn("conversion_latency", payload)
        # 组对象只有基础四键加组内耗时，无组内配对与编号。
        for group in payload["visit_date_groups"]:
            self.assertEqual(set(group), GROUP_LATENCY_KEYS)
            self.assertNotIn("conversion_pairs", group)
            self.assertNotIn("visit_user_ids", group)

    def test_without_flag_groups_have_no_latency_field(self):
        # 不启用新开关时保留全部现有输出：组对象维持原有键。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        for extra in (
            ("--group-by", "visit-date"),
            ("--group-by", "visit-date", "--include-pairs"),
            ("--group-by", "visit-date", "--include-users"),
            ("--group-by", "visit-date", "--include-latency"),
            ("--group-by", "visit-date", "--include-group-pairs"),
        ):
            with self.subTest(extra=extra):
                result = self.report(db, *extra, "--within-seconds", "60")
                self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
                payload = self.parse_single_json_object(result.stdout)
                for group in payload["visit_date_groups"]:
                    self.assertNotIn("conversion_latency", group)
                    self.assertNotIn("min_seconds", group)

    def test_metrics_identical_with_and_without_flag(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        common = ("--group-by", "visit-date", "--within-seconds", "60")
        plain = self.parse_single_json_object(self.report(db, *common).stdout)
        detailed = self.parse_single_json_object(
            self.report(db, *common, "--include-group-latency").stdout
        )
        for key in ("visit_users", "converted_users", "conversion_rate"):
            self.assertEqual(detailed[key], plain[key])
        for plain_group, detailed_group in zip(
            plain["visit_date_groups"], detailed["visit_date_groups"]
        ):
            for key in GROUP_BASE_KEYS:
                self.assertEqual(detailed_group[key], plain_group[key])

    def test_top_level_latency_independent_from_group_latency(self):
        # --include-latency 仍只控制顶层：两开关各管各的字段。
        db = self.import_events(TWO_GROUP_EVENTS, jsonl_name="two.jsonl")
        # 只开组内耗时：顶层无耗时对象。
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        self.assertNotIn("conversion_latency", payload)
        # 两开关同开：顶层是全体 3 人的聚合（5/61/32.0），组内仍各自统计。
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--include-latency",
            ).stdout
        )
        self.assertEqual(
            payload["conversion_latency"],
            {"min_seconds": 5, "max_seconds": 61, "mean_seconds": 32.0},
        )
        self.assertGroupLatencies(
            payload,
            [
                ("2026-10-06", {"min_seconds": 30, "max_seconds": 61,
                                "mean_seconds": 45.5}),
                ("2026-10-07", {"min_seconds": 5, "max_seconds": 5,
                                "mean_seconds": 5.0}),
            ],
        )

    def test_group_latency_consistent_with_group_pairs(self):
        # 与 --include-group-pairs 同开：组内耗时必须与组内配对逐人时间差
        # 完全一致（源自同一份归约）。
        db = self.import_events(TWO_GROUP_EVENTS, jsonl_name="two.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--include-group-pairs",
            ).stdout
        )
        fmt = "%Y-%m-%dT%H:%M:%S"
        for group in payload["visit_date_groups"]:
            self.assertIn("conversion_pairs", group)
            self.assertIn("conversion_latency", group)
            durations = [
                (
                    datetime.strptime(pair["signup_timestamp"], fmt)
                    - datetime.strptime(pair["visit_timestamp"], fmt)
                ).total_seconds()
                for pair in group["conversion_pairs"]
            ]
            latency = group["conversion_latency"]
            self.assertEqual(len(durations), group["converted_users"])
            if durations:
                self.assertEqual(latency["min_seconds"], int(min(durations)))
                self.assertEqual(latency["max_seconds"], int(max(durations)))
                self.assertEqual(
                    latency["mean_seconds"],
                    sum(durations) / len(durations),
                )

    # -- 空结果 -----------------------------------------------------------

    def test_no_qualifying_visits_gives_empty_groups(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-latency"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_date_groups"], [])

    def test_no_conversion_group_gives_nulls_other_group_kept(self):
        # 一组无转化三项 null、另一组正常统计；同时存在于同一输出中。
        db = self.import_events(
            TWO_GROUP_EVENTS
            + [
                {"user_id": "d", "event": "visit",
                 "timestamp": "2026-10-08T10:00:00"},
            ],
            jsonl_name="null_group.jsonl",
        )
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-latency"
            ).stdout
        )
        self.assertGroupLatencies(
            payload,
            [
                ("2026-10-06", {"min_seconds": 30, "max_seconds": 61,
                                "mean_seconds": 45.5}),
                ("2026-10-07", {"min_seconds": 5, "max_seconds": 5,
                                "mean_seconds": 5.0}),
                ("2026-10-08", NULL_LATENCY),
            ],
        )

    # -- 行序、重复事件与重复导入 -----------------------------------------

    def test_shuffled_and_duplicate_events_give_same_group_latency(self):
        for name, events in (
            ("shuffled.jsonl", SHUFFLED_EVENTS),
            ("dupes.jsonl", EVENTS_WITH_DUPLICATES),
        ):
            with self.subTest(jsonl=name):
                db = self.import_events(events, jsonl_name=name)
                payload = self.parse_single_json_object(
                    self.report(
                        db,
                        "--group-by",
                        "visit-date",
                        "--include-group-latency",
                        "--within-seconds",
                        "60",
                    ).stdout
                )
                self.assertGroupLatencies(
                    payload,
                    [
                        ("2026-10-06", GROUP_6TH_LATENCY),
                        ("2026-10-07", NULL_LATENCY),
                    ],
                )

    def test_reimport_same_batch_keeps_group_latency(self):
        path = self.write_jsonl("group.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-latency",
                "--within-seconds",
                "60",
            ).stdout
        )
        self.assertGroupLatencies(
            payload,
            [("2026-10-06", GROUP_6TH_LATENCY), ("2026-10-07", NULL_LATENCY)],
        )

    # -- 错误协议与只读性 -------------------------------------------------

    def test_flag_without_group_by_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="group.jsonl")
        before = self.snapshot_events(db)
        result = self.report(db, "--include-group-latency", "--within-seconds", "60")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误指出新开关及依赖项。
        self.assertIn("--include-group-latency", result.stderr)
        self.assertIn("--group-by", result.stderr)
        self.assertEqual(self.snapshot_events(db), before)

    def test_flag_without_group_by_does_not_create_db(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-group-latency")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--include-group-latency", result.stderr)
        self.assertIn("--group-by", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_missing_db_still_follows_path_error_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--group-by", "visit-date", "--include-group-latency")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--group-by", "visit-date", "--include-group-latency")
        self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-latency",
            "--include-latency",
            "--include-pairs",
            "--include-group-pairs",
            "--include-users",
            "--within-seconds",
            "60",
        )
        self.report(db, "--include-group-latency")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
