"""转化成员统一来源的重构回归测试。

背景：report 的转化人数、--include-users 的 converted_user_ids 与
--group-by visit-date 各组的转化成员，重构前分别查询同一批转化用户；
重构后三处共用一次 DISTINCT 查询得到的同一份成员集合（人数取 len、
明细取 sorted、日期组取交集）。本模块只通过公开命令验证重构后公开行为
完全不变，并重点核对三处转化成员彼此勾稽：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]
                              [--visit-from ... --visit-before ...]
                              [--include-users] [--include-pairs]
                              [--include-latency]
                              [--group-by visit-date
                                 [--include-group-pairs]
                                 [--include-group-latency]]

仅使用 Python 3 标准库；每个场景使用独立临时目录中的 JSONL 与 SQLite，
不依赖预存数据库、网络或第三方包，测试结束不遗留任何数据库文件。
可用现有方式执行：

    python3 -m unittest tests.test_conversion_members_unified
    python3 -m unittest discover -s tests
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验收样例：六条事件（与需求描述逐条对应）。
# u1：6 日 10:00:00 与 7 日 10:00:00 各 visit 一次，7 日 10:00:30 signup
#     （归到 6 日组；转化使用 7 日那次 visit，不限于归组用的那次）
# u2：仅 6 日 11:00:00 visit（不转化）
# u3：7 日 12:00:00 同刻 visit 与 signup（相等不满足“严格晚于”，不转化）
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
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"},
]

# 多日、多转化成员的勾稽样例（不给 --within-seconds，不限间隔）：
# a：6 日 visit、7 日 signup（注册晚于归组日）-> 转化，归 6 日组
# b：7 日 visit 后 10 秒 signup -> 转化，归 7 日组
# c：仅 6 日 visit -> 不转化
# d：6 日 visit 与 signup 同刻 -> 不转化
# e：8 日 visit、9 日 signup -> 转化，归 8 日组
MULTI_DAY_EVENTS = [
    {"user_id": "a", "event": "visit", "timestamp": "2026-10-06T09:00:00"},
    {"user_id": "a", "event": "signup", "timestamp": "2026-10-07T09:00:00"},
    {"user_id": "b", "event": "visit", "timestamp": "2026-10-07T09:00:00"},
    {"user_id": "b", "event": "signup", "timestamp": "2026-10-07T09:00:10"},
    {"user_id": "c", "event": "visit", "timestamp": "2026-10-06T15:00:00"},
    {"user_id": "d", "event": "visit", "timestamp": "2026-10-06T18:00:00"},
    {"user_id": "d", "event": "signup", "timestamp": "2026-10-06T18:00:00"},
    {"user_id": "e", "event": "visit", "timestamp": "2026-10-08T09:00:00"},
    {"user_id": "e", "event": "signup", "timestamp": "2026-10-09T09:00:00"},
]

# 时限边界样例：u60 间隔恰好 60 秒（含上界，转化）；u61 间隔 61 秒（不转化）。
BOUNDARY_EVENTS = [
    {"user_id": "u60", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u60", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
    {"user_id": "u61", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u61", "event": "signup", "timestamp": "2026-10-06T10:01:01"},
]

# 段内 visit、signup 晚于时段终点：仍属转化成员。
AFTER_WINDOW_EVENTS = [
    {"user_id": "late", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
    {"user_id": "late", "event": "signup", "timestamp": "2026-10-08T00:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u6", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}


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


class ConversionMembersUnifiedTests(unittest.TestCase):
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

    def assert_success_single_line(self, result):
        """成功协议：退出码 0、标准错误为空、标准输出恰为一行 JSON 对象。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_object(result.stdout)

    def assert_metrics(self, payload, visit_users, converted_users, rate):
        self.assertEqual(payload["visit_users"], visit_users)
        self.assertEqual(payload["converted_users"], converted_users)
        self.assertEqual(payload["conversion_rate"], rate)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    def groups_by_date(self, payload):
        return {group["visit_date"]: group for group in payload["visit_date_groups"]}

    # -- 验收样例：人数、明细、日期组共用同一批转化成员 -------------------

    def test_acceptance_grouped_detailed_within_60(self):
        db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        result = self.report(
            db,
            "--group-by", "visit-date",
            "--include-users",
            "--within-seconds", "60",
        )
        payload = self.assert_success_single_line(result)
        # 汇总：3 人访问、1 人转化、比例 1/3。
        self.assert_metrics(payload, 3, 1, 1 / 3)
        # 转化编号只有 u1，且人数即编号数。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(len(payload["converted_user_ids"]), payload["converted_users"])
        # 6 日组 2、1、0.5；7 日组 1、0、0；u1 归 6 日组。
        groups = self.groups_by_date(payload)
        self.assertEqual(set(groups), {"2026-10-06", "2026-10-07"})
        self.assertEqual(groups["2026-10-06"]["visit_users"], 2)
        self.assertEqual(groups["2026-10-06"]["converted_users"], 1)
        self.assertEqual(groups["2026-10-06"]["conversion_rate"], 0.5)
        self.assertEqual(groups["2026-10-06"]["visit_user_ids"], ["u1", "u2"])
        self.assertEqual(groups["2026-10-06"]["converted_user_ids"], ["u1"])
        self.assertEqual(groups["2026-10-07"]["visit_users"], 1)
        self.assertEqual(groups["2026-10-07"]["converted_users"], 0)
        self.assertEqual(groups["2026-10-07"]["conversion_rate"], 0)
        self.assertEqual(groups["2026-10-07"]["visit_user_ids"], ["u3"])
        self.assertEqual(groups["2026-10-07"]["converted_user_ids"], [])
        # 字段出现顺序即装配顺序（公开输出的一部分）。
        self.assertEqual(
            list(payload),
            [
                "visit_users",
                "converted_users",
                "conversion_rate",
                "visit_user_ids",
                "converted_user_ids",
                "visit_date_groups",
            ],
        )
        self.assertEqual(
            list(groups["2026-10-06"]),
            [
                "visit_date",
                "visit_users",
                "converted_users",
                "conversion_rate",
                "visit_user_ids",
                "converted_user_ids",
            ],
        )

    def test_acceptance_window_7th_reassigns_u1(self):
        # 访问时段限制为 7 日全天：汇总 2、1、0.5，仅 7 日组，u1 改归该组。
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(
            db,
            "--group-by", "visit-date",
            "--include-users",
            "--within-seconds", "60",
            "--visit-from", "2026-10-07T00:00:00",
            "--visit-before", "2026-10-08T00:00:00",
        )
        payload = self.assert_success_single_line(result)
        self.assert_metrics(payload, 2, 1, 0.5)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        groups = payload["visit_date_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["visit_date"], "2026-10-07")
        self.assertEqual(groups[0]["visit_users"], 2)
        self.assertEqual(groups[0]["converted_users"], 1)
        self.assertEqual(groups[0]["conversion_rate"], 0.5)
        self.assertEqual(groups[0]["visit_user_ids"], ["u1", "u3"])
        self.assertEqual(groups[0]["converted_user_ids"], ["u1"])

    # -- 去掉明细开关：汇总不变，未启用字段不出现 -------------------------

    def test_summary_identical_with_or_without_detail_flags(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 同一数据库，四种开关组合下三项汇总必须完全一致。
        variants = (
            (),
            ("--within-seconds", "60"),
            ("--group-by", "visit-date"),
            ("--group-by", "visit-date", "--include-users",
             "--include-pairs", "--include-latency",
             "--include-group-pairs", "--include-group-latency"),
        )
        metrics = None
        for extra in variants:
            with self.subTest(extra=extra):
                payload = self.assert_success_single_line(self.report(db, *extra))
                current = (
                    payload["visit_users"],
                    payload["converted_users"],
                    payload["conversion_rate"],
                )
                if metrics is None:
                    metrics = current
                else:
                    self.assertEqual(current, metrics)
        self.assertEqual(metrics, (3, 1, 1 / 3))

    def test_disabled_fields_do_not_appear(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        # 无任何开关：只有三个汇总键。
        plain = self.assert_success_single_line(self.report(db, "--within-seconds", "60"))
        self.assertEqual(set(plain), METRIC_KEYS)
        # 仅分组：顶层无编号/配对/耗时，组对象只有四个键。
        grouped = self.assert_success_single_line(
            self.report(db, "--group-by", "visit-date", "--within-seconds", "60")
        )
        self.assertEqual(set(grouped), METRIC_KEYS | {"visit_date_groups"})
        self.assertNotIn("visit_user_ids", grouped)
        self.assertNotIn("converted_user_ids", grouped)
        self.assertNotIn("conversion_pairs", grouped)
        self.assertNotIn("conversion_latency", grouped)
        for group in grouped["visit_date_groups"]:
            self.assertEqual(
                set(group),
                {"visit_date", "visit_users", "converted_users", "conversion_rate"},
            )
        # 仅 --include-users：无分组、无配对、无耗时。
        detailed = self.assert_success_single_line(
            self.report(db, "--include-users", "--within-seconds", "60")
        )
        self.assertEqual(
            set(detailed),
            METRIC_KEYS | {"visit_user_ids", "converted_user_ids"},
        )
        self.assertNotIn("visit_date_groups", detailed)
        self.assertNotIn("conversion_pairs", detailed)
        self.assertNotIn("conversion_latency", detailed)
        # --include-pairs 不向组内追加字段；--include-latency 只在顶层。
        pairs_grouped = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-pairs", "--include-latency",
            )
        )
        self.assertIn("conversion_pairs", pairs_grouped)
        self.assertIn("conversion_latency", pairs_grouped)
        self.assertNotIn("visit_user_ids", pairs_grouped)
        for group in pairs_grouped["visit_date_groups"]:
            self.assertNotIn("conversion_pairs", group)
            self.assertNotIn("conversion_latency", group)
            self.assertNotIn("visit_user_ids", group)
        # 组内开关只控制组内字段，不自动开启顶层对应字段。
        group_only = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-group-pairs", "--include-group-latency",
            )
        )
        self.assertNotIn("conversion_pairs", group_only)
        self.assertNotIn("conversion_latency", group_only)
        for group in group_only["visit_date_groups"]:
            self.assertIn("conversion_pairs", group)
            self.assertIn("conversion_latency", group)

    # -- 行序、重复事件与重复导入：成员集合不变 ---------------------------

    def test_members_invariant_under_shuffle_and_duplicates(self):
        for name, events in (
            ("shuffled.jsonl", SHUFFLED_EVENTS),
            ("dupes.jsonl", EVENTS_WITH_DUPLICATES),
        ):
            with self.subTest(jsonl=name):
                db = self.import_events(events, jsonl_name=name)
                payload = self.assert_success_single_line(
                    self.report(
                        db,
                        "--group-by", "visit-date",
                        "--include-users",
                        "--within-seconds", "60",
                    )
                )
                self.assert_metrics(payload, 3, 1, 1 / 3)
                self.assertEqual(payload["converted_user_ids"], ["u1"])
                groups = self.groups_by_date(payload)
                self.assertEqual(groups["2026-10-06"]["converted_user_ids"], ["u1"])
                self.assertEqual(groups["2026-10-07"]["converted_user_ids"], [])

    def test_members_invariant_under_reimport(self):
        path = self.write_jsonl("main.jsonl", ACCEPTANCE_EVENTS)
        db = self.db_path("reimport.sqlite")
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        self.assertEqual(run_funnel("import", path, "--db", db).returncode, 0)
        payload = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-users",
                "--within-seconds", "60",
            )
        )
        self.assert_metrics(payload, 3, 1, 1 / 3)
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        groups = self.groups_by_date(payload)
        self.assertEqual(groups["2026-10-06"]["converted_user_ids"], ["u1"])
        self.assertEqual(groups["2026-10-07"]["converted_user_ids"], [])

    # -- 三处转化成员（人数 / 顶层编号 / 各日期组）互相勾稽 ---------------

    def test_count_ids_and_groups_share_one_member_set(self):
        db = self.import_events(MULTI_DAY_EVENTS, jsonl_name="multi.jsonl")
        payload = self.assert_success_single_line(
            self.report(db, "--group-by", "visit-date", "--include-users")
        )
        # 5 人访问、3 人转化（a、b、e）；d 同刻不转化，c 无注册。
        self.assert_metrics(payload, 5, 3, 3 / 5)
        top_ids = payload["converted_user_ids"]
        self.assertEqual(top_ids, ["a", "b", "e"])  # Unicode 码点字典序
        # 人数即统一成员集合的大小。
        self.assertEqual(payload["converted_users"], len(top_ids))
        self.assertTrue(set(top_ids) <= set(payload["visit_user_ids"]))

        groups = self.groups_by_date(payload)
        # 6 日组：a 转化（注册在次日，仍算），c、d 不转化。
        self.assertEqual(groups["2026-10-06"]["visit_user_ids"], ["a", "c", "d"])
        self.assertEqual(groups["2026-10-06"]["converted_user_ids"], ["a"])
        # 7 日组、8 日组各一人且转化。
        self.assertEqual(groups["2026-10-07"]["visit_user_ids"], ["b"])
        self.assertEqual(groups["2026-10-07"]["converted_user_ids"], ["b"])
        self.assertEqual(groups["2026-10-08"]["visit_user_ids"], ["e"])
        self.assertEqual(groups["2026-10-08"]["converted_user_ids"], ["e"])

        # 勾稽一：各组转化人数之和等于汇总转化人数。
        self.assertEqual(
            sum(g["converted_users"] for g in payload["visit_date_groups"]),
            payload["converted_users"],
        )
        # 勾稽二：各组转化编号（集合并）恰为顶层转化编号集合。
        group_id_sets = [
            set(g["converted_user_ids"]) for g in payload["visit_date_groups"]
        ]
        self.assertEqual(set().union(*group_id_sets), set(top_ids))
        # 勾稽三：各组成员互不重复（每用户只归一个日期组），故长度之和
        # 恰等于顶层编号数——三处说的是同一批转化用户。
        self.assertEqual(
            sum(len(ids) for ids in group_id_sets), len(top_ids)
        )
        # 访问侧同样勾稽。
        visit_group_sets = [
            set(g["visit_user_ids"]) for g in payload["visit_date_groups"]
        ]
        self.assertEqual(
            set().union(*visit_group_sets), set(payload["visit_user_ids"])
        )
        self.assertEqual(
            sum(len(ids) for ids in visit_group_sets),
            len(payload["visit_user_ids"]),
        )

    def test_pair_and_latency_members_match_unified_set(self):
        # 全部明细开关同开：配对与耗时的成员必须与统一转化成员集合一致。
        db = self.import_events(ACCEPTANCE_EVENTS)
        payload = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-users", "--include-pairs", "--include-latency",
                "--include-group-pairs", "--include-group-latency",
                "--within-seconds", "60",
            )
        )
        member_set = set(payload["converted_user_ids"])
        self.assertEqual(member_set, {"u1"})
        # 顶层配对：每成员一条，编号集合与统一集合相同。
        pair_ids = [pair["user_id"] for pair in payload["conversion_pairs"]]
        self.assertEqual(set(pair_ids), member_set)
        self.assertEqual(len(pair_ids), payload["converted_users"])
        # 配对选择语义保留：最早有效 signup 配对最晚有效 visit（7 日那次）。
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
        # 顶层耗时与配对同源，30 秒。
        self.assertEqual(
            payload["conversion_latency"],
            {"min_seconds": 30, "max_seconds": 30, "mean_seconds": 30},
        )
        groups = self.groups_by_date(payload)
        # 6 日组持有唯一成员的配对与耗时；7 日组空配对、三项 None。
        self.assertEqual(
            [p["user_id"] for p in groups["2026-10-06"]["conversion_pairs"]],
            ["u1"],
        )
        self.assertEqual(
            groups["2026-10-06"]["conversion_latency"],
            {"min_seconds": 30, "max_seconds": 30, "mean_seconds": 30},
        )
        self.assertEqual(groups["2026-10-07"]["conversion_pairs"], [])
        self.assertEqual(
            groups["2026-10-07"]["conversion_latency"],
            {"min_seconds": None, "max_seconds": None, "mean_seconds": None},
        )
        # 各组配对合并后恰为顶层配对（组间不重复用户）。
        group_pair_sets = [
            set(p["user_id"] for p in g["conversion_pairs"])
            for g in payload["visit_date_groups"]
        ]
        self.assertEqual(set().union(*group_pair_sets), member_set)
        self.assertEqual(
            sum(len(ids) for ids in group_pair_sets), len(member_set)
        )

    # -- 转化语义边界：注册可晚于段终点、时限含上界、同刻不转化 ----------

    def test_signup_after_window_end_still_a_member(self):
        db = self.import_events(AFTER_WINDOW_EVENTS, jsonl_name="late.jsonl")
        payload = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-users",
                "--visit-from", "2026-10-06T00:00:00",
                "--visit-before", "2026-10-07T00:00:00",
            )
        )
        self.assert_metrics(payload, 1, 1, 1.0)
        self.assertEqual(payload["converted_user_ids"], ["late"])
        groups = self.groups_by_date(payload)
        self.assertEqual(groups["2026-10-06"]["converted_user_ids"], ["late"])

    def test_within_seconds_upper_bound_inclusive(self):
        db = self.import_events(BOUNDARY_EVENTS, jsonl_name="bound.jsonl")
        # 恰好 60 秒计入：u60 转化、u61 不转化。
        within = self.assert_success_single_line(
            self.report(db, "--include-users", "--within-seconds", "60")
        )
        self.assert_metrics(within, 2, 1, 0.5)
        self.assertEqual(within["converted_user_ids"], ["u60"])
        # 放宽到 61 秒：两人都转化，成员按码点序为 u60、u61。
        wider = self.assert_success_single_line(
            self.report(db, "--include-users", "--within-seconds", "61")
        )
        self.assert_metrics(wider, 2, 2, 1.0)
        self.assertEqual(wider["converted_user_ids"], ["u60", "u61"])
        # 不限间隔同样两人转化。
        unbounded = self.assert_success_single_line(self.report(db, "--include-users"))
        self.assert_metrics(unbounded, 2, 2, 1.0)

    def test_equal_timestamps_never_convert(self):
        # u3 同刻 visit/signup：有访问、无转化，仅转化编号为空。
        equal_events = [
            {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
            {"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"},
        ]
        db = self.import_events(equal_events, jsonl_name="equal.jsonl")
        payload = self.assert_success_single_line(
            self.report(db, "--include-users", "--within-seconds", "60")
        )
        self.assertEqual(payload["visit_users"], 1)
        self.assertEqual(payload["converted_users"], 0)
        self.assertEqual(payload["conversion_rate"], 0)
        self.assertEqual(payload["visit_user_ids"], ["u3"])
        self.assertEqual(payload["converted_user_ids"], [])
        # 验收全库中 u1 仍转化，但同刻的 u3 必不在统一转化成员集合里。
        full_db = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        full = self.assert_success_single_line(
            self.report(full_db, "--include-users", "--within-seconds", "60")
        )
        self.assertEqual(full["converted_user_ids"], ["u1"])
        self.assertNotIn("u3", full["converted_user_ids"])

    # -- 零访问 / 无转化 ---------------------------------------------------

    def test_zero_visits_zeroes_and_empty_collections(self):
        db = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup_only.jsonl")
        payload = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-users", "--include-pairs", "--include-latency",
            )
        )
        # 三项汇总为零；编号、配对与分组为空，耗时三项 None。
        self.assert_metrics(payload, 0, 0, 0)
        self.assertEqual(payload["visit_user_ids"], [])
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["conversion_pairs"], [])
        self.assertEqual(payload["visit_date_groups"], [])
        self.assertEqual(
            payload["conversion_latency"],
            {"min_seconds": None, "max_seconds": None, "mean_seconds": None},
        )

    def test_window_without_visits_gives_empty_members_and_groups(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        payload = self.assert_success_single_line(
            self.report(
                db,
                "--group-by", "visit-date",
                "--include-users",
                "--visit-from", "2026-10-10T00:00:00",
                "--visit-before", "2026-10-11T00:00:00",
            )
        )
        self.assert_metrics(payload, 0, 0, 0)
        self.assertEqual(payload["visit_user_ids"], [])
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["visit_date_groups"], [])

    # -- 错误协议 ----------------------------------------------------------

    def test_invalid_args_exit_2_before_db_access(self):
        db = self.db_path("not-created.sqlite")
        self.assertFalse(os.path.exists(db))
        cases = [
            ("--within-seconds", "0"),
            ("--within-seconds", "60\n"),
            ("--group-by", "signup-date"),
            ("--visit-from", "2026-10-07T00:00:00"),  # 缺少配对终点
            ("--visit-from", "2026-10-08T00:00:00",
             "--visit-before", "2026-10-07T00:00:00"),  # 起点晚于终点
            ("--include-group-pairs",),
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = self.report(db, *extra)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotEqual(result.stderr, "")
                self.assertNotIn("Traceback", result.stderr)
                # 参数错误先于数据库访问：不创建数据库文件。
                self.assertFalse(
                    os.path.exists(db),
                    msg="参数 %r 不应创建数据库 %s" % (extra, db),
                )

    def test_missing_db_exit_2_with_path_and_reason(self):
        db = self.db_path("missing.sqlite")
        result = self.report(
            db, "--group-by", "visit-date", "--include-users"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))

    # -- 报告不改动事件记录 ------------------------------------------------

    def test_reports_keep_events_unchanged(self):
        db = self.import_events(EVENTS_WITH_DUPLICATES, jsonl_name="dupes.jsonl")
        before = self.snapshot_events(db)
        # 成功报告（多种开关组合）与一次参数错误报告之后，记录逐行一致。
        self.report(db)
        self.report(db, "--within-seconds", "60", "--include-users")
        self.report(
            db,
            "--group-by", "visit-date",
            "--include-users", "--include-pairs", "--include-latency",
            "--include-group-pairs", "--include-group-latency",
        )
        self.report(db, "--within-seconds", "0")  # 参数错误，退出码 2
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
