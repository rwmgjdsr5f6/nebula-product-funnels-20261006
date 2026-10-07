"""--include-group-pairs 组内配对明细开关的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> --group-by visit-date
                              --include-group-pairs [--within-seconds N]
                              [--visit-from ... --visit-before ...]
                              [--include-users] [--include-pairs]

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
#     （归 6 日组；60 秒时限内唯一配对使用 7 日那次 visit——配对可跨日期，
#     但用户不按配对时间重新归组）
# u2：仅 6 日 11:00:00 visit（不转化）
# u3：7 日 12:00:00 同时刻 visit 与 signup（严格晚于不成立，不转化）
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
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T10:00:30"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

# 排序样例：5 人归 6 日组、1 人归 7 日组，其中 u10 与 "张三" 转化；
# 编号含大小写、前导空白、中文与多位数字，验证组内配对数组同样按
# Unicode 码点字典序升序（"u10" < "张三" 按码点而非数值）。
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

# 跨日配对样例：u1 的最早合格 visit 在 6 日，有效配对用的是 8 日 visit；
# u2 归 7 日组且不转化。u1 的配对只能出现在 6 日组。
CROSS_DAY_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T09:00:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-08T09:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-08T09:00:30"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-07T09:00:00"},
]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}
GROUP_BASE_KEYS = METRIC_KEYS | {"visit_date"}
GROUP_ID_KEYS = {"visit_user_ids", "converted_user_ids"}
GROUP_PAIR_KEY = "conversion_pairs"
PAIR_ENTRY_KEYS = {"user_id", "visit_timestamp", "signup_timestamp"}


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

    def assertGroupShape(self, group, with_users):
        # 组对象恰为五个键（或与 --include-users 同开时八个键）。
        expected = set(GROUP_BASE_KEYS) | {GROUP_PAIR_KEY}
        if with_users:
            expected |= GROUP_ID_KEYS
        self.assertEqual(set(group), expected)
        for pair in group[GROUP_PAIR_KEY]:
            self.assertEqual(set(pair), PAIR_ENTRY_KEYS)
            self.assertEqual(
                list(pair),
                ["user_id", "visit_timestamp", "signup_timestamp"],
            )

    def assertGroupsReconcileWithTotals(self, payload, with_users=False):
        """组内配对与组人数、（可选的）顶层配对勾稽。"""
        groups = payload["visit_date_groups"]
        merged = []
        seen = []
        for group in groups:
            pairs = group[GROUP_PAIR_KEY]
            # 数组长度等于本组转化人数。
            self.assertEqual(len(pairs), group["converted_users"])
            ids = [pair["user_id"] for pair in pairs]
            # 组内按 Unicode 码点字典序升序，且每人至多一次。
            self.assertEqual(ids, sorted(ids))
            self.assertEqual(len(ids), len(set(ids)))
            if with_users:
                # 配对编号是本组转化编号数组的有序副本。
                self.assertEqual(ids, group["converted_user_ids"])
            merged.extend(pairs)
            seen.extend(ids)
        # 组间不重复用户。
        self.assertEqual(len(seen), len(set(seen)))
        # 各组合并后人数等于汇总转化人数。
        self.assertEqual(len(merged), payload["converted_users"])
        if "conversion_pairs" in payload:
            # 与顶层配对同开：各组配对按日期顺序合并后再按 user_id 排序，
            # 应与顶层数组逐元素完全一致。
            merged_sorted = sorted(merged, key=lambda pair: pair["user_id"])
            self.assertEqual(merged_sorted, payload["conversion_pairs"])

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 任务指定验收 -----------------------------------------------------

    def test_acceptance_group_pairs_within_60(self):
        # python -m funnel report --db events.sqlite --group-by visit-date
        #     --include-group-pairs --within-seconds 60
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
        # 汇总：3 人访问、1 人转化、比例 1/3。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)
        # 新开关不自动开启顶层配对或编号明细。
        self.assertNotIn("conversion_pairs", payload)
        self.assertNotIn("visit_user_ids", payload)
        self.assertNotIn("converted_user_ids", payload)
        groups = payload["visit_date_groups"]
        self.assertEqual([g["visit_date"] for g in groups], ["2026-10-06", "2026-10-07"])
        sixth, seventh = groups
        self.assertGroupShape(sixth, with_users=False)
        self.assertGroupShape(seventh, with_users=False)
        # 6 日组：u1 归此组，配对用的是 7 日 10:00:00 的 visit。
        self.assertEqual(sixth["visit_users"], 2)
        self.assertEqual(sixth["converted_users"], 1)
        self.assertEqual(sixth[GROUP_PAIR_KEY], [U1_PAIR])
        # 7 日组：u3 同刻注册不转化，组内配对为空数组而非缺键。
        self.assertEqual(seventh["visit_users"], 1)
        self.assertEqual(seventh["converted_users"], 0)
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])
        # 字段顺序：组内配对位于比例之后（无编号数组时即组对象末位）。
        self.assertEqual(
            list(sixth),
            [
                "visit_date",
                "visit_users",
                "converted_users",
                "conversion_rate",
                "conversion_pairs",
            ],
        )
        self.assertGroupsReconcileWithTotals(payload)

    # -- 开关独立性 -------------------------------------------------------

    def test_group_pairs_do_not_enable_top_level_fields(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(
            set(payload), METRIC_KEYS | {"visit_date_groups"}
        )
        for group in payload["visit_date_groups"]:
            self.assertEqual(set(group), GROUP_BASE_KEYS | {GROUP_PAIR_KEY})

    def test_group_pairs_combined_with_include_users(self):
        # 与 --include-users 同开：组对象同时带两个编号数组与配对数组，
        # 配对编号数组与组内转化编号一致。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-users",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        # 顶层只多出编号数组，没有顶层配对。
        self.assertNotIn("conversion_pairs", payload)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        sixth, seventh = payload["visit_date_groups"]
        self.assertGroupShape(sixth, with_users=True)
        self.assertEqual(
            list(sixth),
            [
                "visit_date",
                "visit_users",
                "converted_users",
                "conversion_rate",
                "visit_user_ids",
                "converted_user_ids",
                "conversion_pairs",
            ],
        )
        self.assertEqual(sixth["visit_user_ids"], ["u1", "u2"])
        self.assertEqual(sixth["converted_user_ids"], ["u1"])
        self.assertEqual(sixth[GROUP_PAIR_KEY], [U1_PAIR])
        self.assertEqual(seventh["visit_user_ids"], ["u3"])
        self.assertEqual(seventh["converted_user_ids"], [])
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])
        self.assertGroupsReconcileWithTotals(payload, with_users=True)

    def test_group_pairs_combined_with_top_pairs_match_exactly(self):
        # 与顶层 --include-pairs 同开：各组合并排序后与顶层数组完全一致。
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-pairs",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        expected_top = [
            {
                "user_id": "u10",
                "visit_timestamp": "2026-10-06T10:00:00",
                "signup_timestamp": "2026-10-06T10:00:30",
            },
            {
                "user_id": "张三",
                "visit_timestamp": "2026-10-06T10:00:00",
                "signup_timestamp": "2026-10-06T10:00:30",
            },
        ]
        self.assertEqual(payload["conversion_pairs"], expected_top)
        sixth, seventh = payload["visit_date_groups"]
        self.assertEqual(sixth[GROUP_PAIR_KEY], expected_top)
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])
        self.assertGroupsReconcileWithTotals(payload)

    def test_include_pairs_alone_still_does_not_add_group_pairs(self):
        # 旧开关行为不变：只开 --include-pairs + 分组，组内无配对数组。
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
            self.assertEqual(set(group), GROUP_BASE_KEYS)
            self.assertNotIn(GROUP_PAIR_KEY, group)

    # -- 归组与配对语义 ---------------------------------------------------

    def test_pair_visit_on_other_date_stays_in_earliest_group(self):
        # u1 归 6 日组，配对用的 visit 发生在 8 日：配对仍只进 6 日组，
        # 不按配对时间重新归组；7 日组的 u2 不转化，配对为空。
        db = self.import_events(CROSS_DAY_EVENTS, jsonl_name="cross.jsonl")
        result = self.report(db, "--group-by", "visit-date", "--include-group-pairs")
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        groups = payload["visit_date_groups"]
        self.assertEqual([g["visit_date"] for g in groups], ["2026-10-06", "2026-10-07"])
        sixth, seventh = groups
        self.assertEqual(
            sixth[GROUP_PAIR_KEY],
            [
                {
                    "user_id": "u1",
                    "visit_timestamp": "2026-10-08T09:00:00",
                    "signup_timestamp": "2026-10-08T09:00:30",
                }
            ],
        )
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])
        self.assertGroupsReconcileWithTotals(payload)

    def test_group_pairs_sorted_by_code_point_and_keep_raw_values(self):
        # 组内数组按 user_id 原值的 Unicode 码点字典序升序：u10 < 张三，
        # 区分大小写、保留空白与中文、不按数字大小。
        db = self.import_events(SORT_EVENTS, jsonl_name="sort.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        sixth, seventh = payload["visit_date_groups"]
        self.assertEqual(
            [pair["user_id"] for pair in sixth[GROUP_PAIR_KEY]],
            ["u10", "张三"],
        )
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])
        self.assertGroupsReconcileWithTotals(payload)

    def test_within_seconds_upper_bound_inclusive(self):
        # 间隔恰为 60 秒计入组内配对；61 秒则配对消失、组转化为 0。
        events = [
            {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
        ]
        db = self.import_events(events)
        ok = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--within-seconds",
            "60",
        )
        payload = self.parse_single_json_object(ok.stdout)
        self.assertEqual(
            payload["visit_date_groups"][0][GROUP_PAIR_KEY],
            [
                {
                    "user_id": "u1",
                    "visit_timestamp": "2026-10-06T10:00:00",
                    "signup_timestamp": "2026-10-06T10:01:00",
                }
            ],
        )
        too_tight = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--within-seconds",
            "59",
        )
        self.assertEqual(too_tight.returncode, 0, msg="stderr=%r" % too_tight.stderr)
        payload = self.parse_single_json_object(too_tight.stdout)
        group = payload["visit_date_groups"][0]
        self.assertEqual(group["converted_users"], 0)
        self.assertEqual(group[GROUP_PAIR_KEY], [])

    def test_no_within_seconds_means_unbounded(self):
        # 不设时限：u1 的 30 秒配对保持，u3 同刻仍不转化。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        sixth, seventh = payload["visit_date_groups"]
        self.assertEqual(sixth[GROUP_PAIR_KEY], [U1_PAIR])
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])

    def test_window_reassigns_group_and_pair(self):
        # 时段限定 7 日全天：u1 的最早合格 visit 变为 7 日，人改归 7 日组，
        # 配对也随人进入 7 日组；u2 被整人排除。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--within-seconds",
            "60",
            "--visit-from",
            "2026-10-07T00:00:00",
            "--visit-before",
            "2026-10-08T00:00:00",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        groups = payload["visit_date_groups"]
        self.assertEqual(len(groups), 1)
        seventh = groups[0]
        self.assertEqual(seventh["visit_date"], "2026-10-07")
        self.assertEqual(seventh["visit_users"], 2)
        self.assertEqual(seventh["converted_users"], 1)
        self.assertEqual(seventh[GROUP_PAIR_KEY], [U1_PAIR])

    def test_no_qualifying_visits_gives_empty_groups(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        result = self.report(
            db, "--group-by", "visit-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["visit_date_groups"], [])

    # -- 行序、重复事件、重复导入不变量 -----------------------------------

    def test_invariant_under_shuffle_duplicates_and_reimport(self):
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
                    "--include-group-pairs",
                    "--within-seconds",
                    "60",
                )
                self.assertEqual(
                    result.returncode, 0, msg="stderr=%r" % result.stderr
                )
                payload = self.parse_single_json_object(result.stdout)
                sixth, seventh = payload["visit_date_groups"]
                self.assertEqual(sixth[GROUP_PAIR_KEY], [U1_PAIR])
                self.assertEqual(seventh[GROUP_PAIR_KEY], [])
                self.assertGroupsReconcileWithTotals(payload)

        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        result = self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--within-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        payload = self.parse_single_json_object(result.stdout)
        sixth, seventh = payload["visit_date_groups"]
        self.assertEqual(sixth[GROUP_PAIR_KEY], [U1_PAIR])
        self.assertEqual(seventh[GROUP_PAIR_KEY], [])
        self.assertGroupsReconcileWithTotals(payload)

    # -- 缺少分组依赖：退出码 2、不建库 -----------------------------------

    def test_group_pairs_without_group_by_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--include-group-pairs", "--within-seconds", "60")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误同时指出新开关与其依赖项。
        self.assertIn("--include-group-pairs", result.stderr)
        self.assertIn("--group-by", result.stderr)
        self.assertIn("visit-date", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_group_pairs_without_group_by_does_not_create_db(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--include-group-pairs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--include-group-pairs", result.stderr)
        self.assertIn("--group-by", result.stderr)
        # 依赖校验先于数据库存在性检查与一切数据库访问。
        self.assertFalse(
            os.path.exists(db),
            msg="缺少 --group-by 时不应创建数据库文件 %s" % db,
        )

    def test_group_pairs_with_bad_group_by_still_rejected(self):
        # 分组取值非法时仍由既有的 --group-by 校验拒绝（退出码 2）。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db, "--group-by", "signup-date", "--include-group-pairs"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--group-by", result.stderr)

    # -- 报告只读 ---------------------------------------------------------

    def test_group_pairs_report_keeps_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES)
        before = self.snapshot_events(db)
        self.report(
            db,
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--within-seconds",
            "60",
        )
        self.report(db, "--group-by", "visit-date", "--include-group-pairs")
        self.report(db, "--include-group-pairs")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
