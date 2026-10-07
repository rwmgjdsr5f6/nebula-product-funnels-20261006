"""report --group-by visit-date 分组漏斗的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]
                              [--visit-from ... --visit-before ...]
                              [--group-by visit-date]

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

# 验收样例：六条事件，日期均在 2026 年 10 月。
# u1：6 日 10:00:00 与 7 日 10:00:00 各 visit 一次，7 日 10:00:30 signup
#     （归到 6 日组；转化可使用 7 日那次 visit，不限于归组用的那次）
# u2：仅 6 日 11:00:00 visit（不转化）
# u3：7 日 12:00:00 同时刻 visit 与 signup（相等不算转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"},
]

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (4, 0, 5, 2, 3, 1)]

# 混入完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

# 非法 --group-by 取值：空值与其他维度名。
INVALID_GROUP_BY = ["", "signup-date", "VISIT-DATE", "visit_date", " visit-date"]

EXPECTED_GROUP_KEYS = {
    "visit_date",
    "visit_users",
    "converted_users",
    "conversion_rate",
}
# --group-by visit-date 与 --include-users 同时启用时，组对象追加的两个键。
EXPECTED_GROUP_ID_KEYS = {"visit_user_ids", "converted_user_ids"}
EXPECTED_GROUP_KEYS_WITH_IDS = EXPECTED_GROUP_KEYS | EXPECTED_GROUP_ID_KEYS

# 组内编号排序样例：5 人归 6 日组、1 人归 7 日组；编号含大小写、前导空白、
# 中文与多位数字，验证组内数组同样按 Unicode 码点升序（" " < "A" < "a"
# < "u"，"u10" < "u2" 按字符而非数值），与顶层编号数组同一口径。
SORT_EVENTS = [
    {"user_id": "u10", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u10", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "Alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "alice", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": " 空格", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
]
SORTED_6TH_VISIT_IDS = [" 空格", "Alice", "alice", "u10", "张三"]
SORTED_6TH_CONVERTED_IDS = ["u10", "张三"]


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


class FunnelGroupByVisitDateTests(unittest.TestCase):
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

    def assertGroups(self, payload, expected, group_ids=None):
        """expected 为 (visit_date, visit_users, converted_users, rate) 元组列表。

        group_ids 非 None 时（仅用于 --group-by + --include-users 同开的
        报告），须与 expected 等长，每项为 (visit_user_ids, converted_user_ids)，
        并核对组键集合、数组长度、子集关系以及与顶层数组的勾稽。
        """
        groups = payload["visit_date_groups"]
        self.assertIsInstance(groups, list)
        self.assertEqual(len(groups), len(expected))
        if group_ids is not None:
            self.assertEqual(len(group_ids), len(expected))
        for index, (group, item) in enumerate(zip(groups, expected)):
            day, visits, converted, rate = item
            expected_keys = (
                EXPECTED_GROUP_KEYS_WITH_IDS
                if group_ids is not None
                else EXPECTED_GROUP_KEYS
            )
            self.assertEqual(set(group), expected_keys)
            self.assertEqual(group["visit_date"], day)
            self.assertEqual(group["visit_users"], visits)
            self.assertEqual(group["converted_users"], converted)
            self.assertEqual(group["conversion_rate"], rate)
            if group_ids is not None:
                visit_ids, converted_ids = group_ids[index]
                self.assertEqual(group["visit_user_ids"], visit_ids)
                self.assertEqual(group["converted_user_ids"], converted_ids)
                # 数组长度等于对应人数，转化数组是访问数组的有序子集。
                self.assertEqual(len(visit_ids), visits)
                self.assertEqual(len(converted_ids), converted)
                self.assertTrue(set(converted_ids) <= set(visit_ids))
                # 数组本身按 Unicode 码点升序。
                self.assertEqual(visit_ids, sorted(visit_ids))
                self.assertEqual(converted_ids, sorted(converted_ids))
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
        if group_ids is not None:
            # 各组编号分别合并（集合并）后等于顶层相应数组；各组数组长度之和
            # 等于顶层数组长度，即证明组间没有重复编号（每个用户只归最早
            # 合格 visit 的日期组）。
            group_visit_sets = [
                set(group["visit_user_ids"]) for group in groups
            ]
            group_converted_sets = [
                set(group["converted_user_ids"]) for group in groups
            ]
            self.assertEqual(
                set().union(*group_visit_sets), set(payload["visit_user_ids"])
            )
            self.assertEqual(
                set().union(*group_converted_sets),
                set(payload["converted_user_ids"]),
            )
            self.assertEqual(
                sum(len(ids) for ids in group_visit_sets),
                len(payload["visit_user_ids"]),
            )
            self.assertEqual(
                sum(len(ids) for ids in group_converted_sets),
                len(payload["converted_user_ids"]),
            )

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_full_db_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 汇总：3 人访问、1 人转化、比例 1/3。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)
        # 6 日组 2 人访问 1 人转化；7 日组 1 人访问 0 人转化。
        self.assertGroups(
            payload,
            [
                ("2026-10-06", 2, 1, 0.5),
                ("2026-10-07", 1, 0, 0),
            ],
        )

    def test_acceptance_window_only_7th(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--within-seconds",
            "60",
            "--visit-from",
            "2026-10-07T00:00:00",
            "--visit-before",
            "2026-10-08T00:00:00",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 段外历史不影响归组：u1 归到 7 日（最早合格 visit），u2 被时段排除。
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 1)
        self.assertGroups(payload, [("2026-10-07", 2, 1, 0.5)])

    def test_no_group_by_leaves_output_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--within-seconds", "60")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(
            set(payload), {"visit_users", "converted_users", "conversion_rate"}
        )

    # -- 分组语义 ---------------------------------------------------------

    def test_user_assigned_to_earliest_qualifying_visit_only(self):
        # u1 在 6、7、8 日各 visit 一次：只归 6 日组，各组人数之和等于汇总。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-08T09:00:00"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T09:00:00"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T09:00:00"},
            {"user_id": "u2", "event": "visit", "timestamp": "2026-10-07T09:00:00"},
        ]
        db = self.import_events(events)
        result = self.report(db, "--group-by", "visit-date")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 1, 0, 0), ("2026-10-07", 1, 0, 0)],
        )

    def test_signup_after_window_end_still_counts_in_group(self):
        # signup 可晚于时段终点：u1 段内 visit、段外 signup 仍算转化。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T00:00:00"},
        ]
        db = self.import_events(events)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--visit-from",
            "2026-10-06T00:00:00",
            "--visit-before",
            "2026-10-07T00:00:00",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(payload, [("2026-10-06", 1, 1, 1.0)])

    def test_within_seconds_upper_bound_inclusive_in_groups(self):
        # 间隔恰好 60 秒（含等值）：u1 在 6 日组内转化。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
            {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
        ]
        db = self.import_events(events)
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(payload, [("2026-10-06", 2, 1, 0.5)])

    def test_no_qualifying_visits_gives_empty_groups(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(db, "--group-by", "visit-date")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_date_groups"], [])

    def test_window_without_visits_gives_empty_groups(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--visit-from",
            "2026-10-10T00:00:00",
            "--visit-before",
            "2026-10-11T00:00:00",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_date_groups"], [])

    def test_shuffled_and_duplicate_events_give_same_groups(self):
        for name, events in (
            ("shuffled.jsonl", SHUFFLED_EVENTS),
            ("dupes.jsonl", EVENTS_WITH_DUPLICATES),
        ):
            with self.subTest(jsonl=name):
                db = self.import_events(events, jsonl_name=name)
                result = self.report(
                    db, "--group-by", "visit-date", "--within-seconds", "60"
                )
                self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
                payload = self.parse_single_json_object(result.stdout)
                self.assertGroups(
                    payload,
                    [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
                )

    def test_reimport_same_batch_keeps_groups(self):
        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        result = self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
        )

    def test_groups_coexist_with_include_users_and_pairs(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--within-seconds",
            "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        # 原有明细保留在顶层，分组只是追加；组内编号随 --include-users 追加。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(
            payload["conversion_pairs"],
            [
                {
                    "user_id": "u1",
                    "visit_timestamp": "2026-10-07T10:00:00",
                    "signup_timestamp": "2026-10-07T10:00:30",
                }
            ],
        )
        # --include-pairs 不向组内追加任何字段：组对象恰为六个键。
        self.assertEqual(
            set(payload["visit_date_groups"][0]), EXPECTED_GROUP_KEYS_WITH_IDS
        )
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
            group_ids=[(["u1", "u2"], ["u1"]), (["u3"], [])],
        )

    # -- 组内编号明细（--group-by + --include-users） ---------------------

    def test_acceptance_group_user_ids_within_60(self):
        # 验收命令：六条事件 + --group-by visit-date --include-users
        # --within-seconds 60。6 日组 ["u1","u2"] / ["u1"]；
        # 7 日组 ["u3"] / []；汇总三人访问、一人转化。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-users",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
            group_ids=[(["u1", "u2"], ["u1"]), (["u3"], [])],
        )
        # 字段顺序即 payload 装配顺序：两编号数组在组内位于比例之后。
        self.assertEqual(
            list(payload["visit_date_groups"][0]),
            [
                "visit_date",
                "visit_users",
                "converted_users",
                "conversion_rate",
                "visit_user_ids",
                "converted_user_ids",
            ],
        )

    def test_group_ids_without_within_seconds(self):
        # 不传 --within-seconds 时转化不限间隔：u1 仍转化（30 秒），
        # u3 同刻仍不转化；组内编号与 60 秒场景一致。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db, "--group-by", "visit-date", "--include-users"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
            group_ids=[(["u1", "u2"], ["u1"]), (["u3"], [])],
        )

    def test_group_ids_window_reassigns_user(self):
        # 时段报告：u1 的 6 日 visit 在段外，改归 7 日组（段外历史不参与
        # 归组）；u2 被整人排除。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-users",
            "--within-seconds",
            "60",
            "--visit-from",
            "2026-10-07T00:00:00",
            "--visit-before",
            "2026-10-08T00:00:00",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertGroups(
            payload,
            [("2026-10-07", 2, 1, 0.5)],
            group_ids=[(["u1", "u3"], ["u1"])],
        )

    def test_group_ids_sorted_by_code_point_and_keep_raw_values(self):
        # 组内数组与顶层同一排序口径：区分大小写、保留空白与中文、不按数值。
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-users"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(
            payload["visit_user_ids"],
            [" 空格", "Alice", "alice", "u10", "u2", "张三"],
        )
        self.assertEqual(payload["converted_user_ids"], ["u10", "张三"])
        self.assertGroups(
            payload,
            [("2026-10-06", 5, 2, 2 / 5), ("2026-10-07", 1, 0, 0)],
            group_ids=[
                (SORTED_6TH_VISIT_IDS, SORTED_6TH_CONVERTED_IDS),
                (["u2"], []),
            ],
        )

    def test_group_ids_empty_when_no_qualifying_visits(self):
        # 无合格访问：分组数组为空，不补空日期；顶层两数组同样为空。
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-users"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_user_ids"], [])
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["visit_date_groups"], [])

    def test_group_ids_invariant_under_shuffle_and_duplicates(self):
        # 打乱行序、混入重复事件：组内编号不变。
        for name, events in (
            ("shuffled.jsonl", SHUFFLED_EVENTS),
            ("dupes.jsonl", EVENTS_WITH_DUPLICATES),
        ):
            with self.subTest(jsonl=name):
                db = self.import_events(events, jsonl_name=name)
                result = self.report(
                    db,
                    "--group-by",
                    "visit-date",
                    "--include-users",
                    "--within-seconds",
                    "60",
                )
                self.assertEqual(
                    result.returncode, 0, msg="stderr=%r" % result.stderr
                )
                payload = self.parse_single_json_object(result.stdout)
                self.assertGroups(
                    payload,
                    [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
                    group_ids=[(["u1", "u2"], ["u1"]), (["u3"], [])],
                )

    def test_group_ids_invariant_under_reimport(self):
        # 同一文件重复导入：去重后组内编号不变。
        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-users",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertGroups(
            payload,
            [("2026-10-06", 2, 1, 0.5), ("2026-10-07", 1, 0, 0)],
            group_ids=[(["u1", "u2"], ["u1"]), (["u3"], [])],
        )

    def test_group_without_include_users_has_no_id_fields(self):
        # 只开启分组：组对象维持四个现有键，不追加编号数组。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertNotIn("visit_user_ids", payload)
        self.assertNotIn("converted_user_ids", payload)
        for group in payload["visit_date_groups"]:
            self.assertEqual(set(group), EXPECTED_GROUP_KEYS)

    def test_include_pairs_does_not_add_pair_fields_to_groups(self):
        # --include-pairs 不向组内追加配对；同时不开 --include-users 时组内
        # 也没有编号数组。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-pairs",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertIn("conversion_pairs", payload)
        for group in payload["visit_date_groups"]:
            self.assertEqual(set(group), EXPECTED_GROUP_KEYS)

    # -- 非法 --group-by 取值 ----------------------------------------------

    def test_invalid_group_by_rejected_before_db_access(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        for value in INVALID_GROUP_BY:
            with self.subTest(value=value):
                result = self.report(db, "--group-by", value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误指出参数与原因。
                self.assertIn("--group-by", result.stderr)
                self.assertIn("visit-date", result.stderr)

    def test_group_by_missing_value_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--group-by")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--group-by", result.stderr)

    def test_invalid_group_by_does_not_create_db(self):
        for value in INVALID_GROUP_BY:
            with self.subTest(value=value):
                db = self.db_path("missing-%d.sqlite" % len(value))
                self.assertFalse(os.path.exists(db))
                result = self.report(db, "--group-by", value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse(
                    os.path.exists(db),
                    msg="非法参数 %r 不应创建数据库文件 %s" % (value, db),
                )

    def test_missing_db_follows_path_error_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--group-by", "visit-date")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))

    # -- 报告不改动已有记录 ------------------------------------------------

    def test_grouped_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        self.report(db, "--group-by", "visit-date")
        self.report(db, "--group-by", "bad-value")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
