"""--include-group-pairs 组内配对明细开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> --group-by visit-date
                              [--include-group-pairs] [--within-seconds N]

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

# 验收样例：六条事件，时间均为 2026 年 10 月的 UTC。
# u1：6 日 10:00:00 与 7 日 10:00:00 各 visit 一次，7 日 10:00:30 signup
#     （归到 6 日组；组内配对可使用 7 日那次 visit，不按配对时间重新归组）
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
U1_PAIR = {
    "user_id": "u1",
    "visit_timestamp": "2026-10-07T10:00:00",
    "signup_timestamp": "2026-10-07T10:00:30",
}

# 固定打乱行序（不使用随机数，保证可重复）。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i] for i in (4, 0, 5, 2, 3, 1)]

# 混入完全重复的事件行。
EVENTS_WITH_DUPLICATES = ACCEPTANCE_EVENTS + [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
]

# 多转化用户样例：两人归 6 日组、一人归 7 日组，编号含大小写、前导空白、
# 中文与多位数字，验证组内数组同样按 Unicode 码点升序，且各组配对合并
# 排序后与顶层 conversion_pairs 完全一致。
MULTI_EVENTS = [
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": "u10", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u10", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    {"user_id": " 空格", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "张三", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "张三", "event": "signup", "timestamp": "2026-10-07T10:00:30"},
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
GROUP_PAIR_KEYS = GROUP_BASE_KEYS | {"conversion_pairs"}


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


class IncludeGroupPairsTests(unittest.TestCase):
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

    def assertGroupPairs(self, payload, expected):
        """expected 为 (visit_date, conversion_pairs) 元组列表，逐组核对。

        同时核对：组对象键集合恰为基础四键加 conversion_pairs；每个配对
        对象只含三个字段；组内数组按 user_id 的 Unicode 码点升序、长度
        等于本组转化人数；各组配对合并排序后与顶层 conversion_pairs
        （若开启）完全一致，组间不重复用户。
        """
        groups = payload["visit_date_groups"]
        self.assertEqual(len(groups), len(expected))
        seen_user_ids = []
        for group, (day, pairs) in zip(groups, expected):
            self.assertEqual(set(group), GROUP_PAIR_KEYS)
            self.assertEqual(group["visit_date"], day)
            self.assertEqual(group["conversion_pairs"], pairs)
            for pair in pairs:
                self.assertEqual(
                    set(pair), {"user_id", "visit_timestamp", "signup_timestamp"}
                )
            self.assertEqual(len(pairs), group["converted_users"])
            ids = [pair["user_id"] for pair in pairs]
            self.assertEqual(ids, sorted(ids))
            seen_user_ids.extend(ids)
        # 组间不重复用户。
        self.assertEqual(len(seen_user_ids), len(set(seen_user_ids)))
        if "conversion_pairs" in payload:
            merged = sorted(
                (pair for group in groups for pair in group["conversion_pairs"]),
                key=lambda pair: pair["user_id"],
            )
            self.assertEqual(merged, payload["conversion_pairs"])

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 验收样例 ---------------------------------------------------------

    def test_acceptance_group_pairs_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        # 汇总：3 人访问、1 人转化、比例 1/3；新开关不自动开启顶层配对。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)
        self.assertNotIn("conversion_pairs", payload)
        self.assertNotIn("visit_user_ids", payload)
        # 6 日组仅有 u1 的 7 日访问与注册配对；7 日组配对为空。
        self.assertGroupPairs(
            payload,
            [("2026-10-06", [U1_PAIR]), ("2026-10-07", [])],
        )

    def test_acceptance_group_pairs_with_include_pairs(self):
        # 与顶层配对同时开启：各组合并排序后与顶层数组完全一致。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-pairs",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["conversion_pairs"], [U1_PAIR])
        self.assertGroupPairs(
            payload,
            [("2026-10-06", [U1_PAIR]), ("2026-10-07", [])],
        )

    # -- 开关独立性 -------------------------------------------------------

    def test_group_pairs_do_not_auto_enable_other_details(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        # 顶层不追加配对与编号数组，组内也不追加编号数组。
        self.assertEqual(
            set(payload),
            {"visit_users", "converted_users", "conversion_rate", "visit_date_groups"},
        )
        for group in payload["visit_date_groups"]:
            self.assertEqual(set(group), GROUP_PAIR_KEYS)

    def test_without_flag_groups_have_no_pair_field(self):
        # 不启用新开关时保留全部现有输出：组对象维持原有键。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        for extra in (
            ("--group-by", "visit-date"),
            ("--group-by", "visit-date", "--include-pairs"),
            ("--group-by", "visit-date", "--include-users"),
        ):
            with self.subTest(extra=extra):
                result = self.report(db, *extra, "--within-seconds", "60")
                self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
                payload = self.parse_single_json_object(result.stdout)
                for group in payload["visit_date_groups"]:
                    self.assertNotIn("conversion_pairs", group)

    def test_metrics_identical_with_and_without_flag(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        plain = self.parse_single_json_object(
            self.report(db, "--group-by", "visit-date", "--within-seconds", "60").stdout
        )
        detailed = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-pairs",
                "--within-seconds",
                "60",
            ).stdout
        )
        for key in ("visit_users", "converted_users", "conversion_rate"):
            self.assertEqual(detailed[key], plain[key])
        for plain_group, detailed_group in zip(
            plain["visit_date_groups"], detailed["visit_date_groups"]
        ):
            for key in GROUP_BASE_KEYS:
                self.assertEqual(detailed_group[key], plain_group[key])

    # -- 组内配对语义 -----------------------------------------------------

    def test_pair_visit_may_fall_on_other_date(self):
        # 配对访问可以在归组日期之外：u1 归 6 日组，配对取 7 日的 visit，
        # 不能据配对时间把 u1 重新归到 7 日组。
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-pairs"
            ).stdout
        )
        group_6th, group_7th = payload["visit_date_groups"]
        self.assertEqual(group_6th["visit_date"], "2026-10-06")
        self.assertEqual(group_6th["visit_users"], 2)
        self.assertEqual(group_6th["conversion_pairs"], [U1_PAIR])
        self.assertEqual(group_7th["visit_date"], "2026-10-07")
        self.assertEqual(group_7th["visit_users"], 1)
        self.assertEqual(group_7th["conversion_pairs"], [])

    def test_group_pairs_earliest_signup_latest_visit(self):
        # 组内配对沿用顶层口径：最早有效 signup 配对最晚有效 visit。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:30"},
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:03:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:02:00"},
        ]
        db = self.import_events(events, jsonl_name="pick.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db, "--group-by", "visit-date", "--include-group-pairs"
            ).stdout
        )
        self.assertGroupPairs(
            payload,
            [
                (
                    "2026-10-06",
                    [
                        {
                            "user_id": "u1",
                            "visit_timestamp": "2026-10-06T10:00:30",
                            "signup_timestamp": "2026-10-06T10:01:00",
                        }
                    ],
                )
            ],
        )

    def test_group_pairs_within_seconds_boundary(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        # u1 的 7 日 visit 与 signup 间隔恰为 30 秒：上界含等值。
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-pairs",
                "--within-seconds",
                "30",
            ).stdout
        )
        self.assertGroupPairs(
            payload, [("2026-10-06", [U1_PAIR]), ("2026-10-07", [])]
        )
        # 29 秒窗口下 u1 不再转化，各组配对均为空。
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-pairs",
                "--within-seconds",
                "29",
            ).stdout
        )
        self.assertGroupPairs(
            payload, [("2026-10-06", []), ("2026-10-07", [])]
        )

    def test_group_pairs_visit_window(self):
        # 段外访问不配对，注册允许晚于终点：u1 段内 visit、段外 signup
        # 仍算转化并出现在组内配对中。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T00:00:00"},
        ]
        db = self.import_events(events, jsonl_name="window.jsonl")
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-pairs",
                "--visit-from",
                "2026-10-06T00:00:00",
                "--visit-before",
                "2026-10-07T00:00:00",
            ).stdout
        )
        self.assertGroupPairs(
            payload,
            [
                (
                    "2026-10-06",
                    [
                        {
                            "user_id": "u1",
                            "visit_timestamp": "2026-10-06T23:59:00",
                            "signup_timestamp": "2026-10-08T00:00:00",
                        }
                    ],
                )
            ],
        )

    def test_group_pairs_sorted_and_merge_matches_top_level(self):
        db = self.import_events(MULTI_EVENTS, jsonl_name="multi.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-pairs",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        # 顶层按码点升序：" 空格" 不转化，"u10" < "u2" < "张三" 按字符。
        self.assertEqual(
            [pair["user_id"] for pair in payload["conversion_pairs"]],
            ["u10", "u2", "张三"],
        )
        self.assertGroupPairs(
            payload,
            [
                (
                    "2026-10-06",
                    [
                        {
                            "user_id": "u10",
                            "visit_timestamp": "2026-10-06T10:00:00",
                            "signup_timestamp": "2026-10-06T10:00:30",
                        },
                        {
                            "user_id": "u2",
                            "visit_timestamp": "2026-10-06T10:00:00",
                            "signup_timestamp": "2026-10-06T10:00:30",
                        },
                    ],
                ),
                (
                    "2026-10-07",
                    [
                        {
                            "user_id": "张三",
                            "visit_timestamp": "2026-10-07T10:00:00",
                            "signup_timestamp": "2026-10-07T10:00:30",
                        }
                    ],
                ),
            ],
        )

    # -- 不变量与空结果 ---------------------------------------------------

    def test_shuffled_and_duplicate_events_give_same_group_pairs(self):
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
                        "--include-group-pairs",
                        "--within-seconds",
                        "60",
                    ).stdout
                )
                self.assertGroupPairs(
                    payload,
                    [("2026-10-06", [U1_PAIR]), ("2026-10-07", [])],
                )

    def test_reimport_same_batch_keeps_group_pairs(self):
        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        payload = self.parse_single_json_object(
            self.report(
                db,
                "--group-by",
                "visit-date",
                "--include-group-pairs",
                "--within-seconds",
                "60",
            ).stdout
        )
        self.assertGroupPairs(
            payload, [("2026-10-06", [U1_PAIR]), ("2026-10-07", [])]
        )

    def test_no_qualifying_visits_gives_empty_groups(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_date_groups"], [])

    # -- 错误协议与只读性 -------------------------------------------------

    def test_flag_without_group_by_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        before = self.snapshot_events(db)
        result = self.report(db, "--include-group-pairs", "--within-seconds", "60")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误指出新开关及依赖项。
        self.assertIn("--include-group-pairs", result.stderr)
        self.assertIn("--group-by", result.stderr)
        self.assertEqual(self.snapshot_events(db), before)

    def test_flag_without_group_by_does_not_create_db(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-group-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--include-group-pairs", result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_missing_db_still_follows_path_error_protocol(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--group-by", "visit-date", "--include-group-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertFalse(os.path.exists(db))

    def test_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        before = self.snapshot_events(db)
        self.report(db, "--group-by", "visit-date", "--include-group-pairs")
        self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-pairs",
            "--include-users",
            "--within-seconds",
            "60",
        )
        self.report(db, "--include-group-pairs")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
