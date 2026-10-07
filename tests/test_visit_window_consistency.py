"""访问时段筛选下三种输出（汇总、--include-users 编号明细、
--include-pairs 配对明细、--group-by visit-date 分组）一致性回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite>
        --within-seconds N
        --visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS
        --include-users --include-pairs --group-by visit-date

固定合成样例共十条事件，年份均为 2026，时间按 UTC 解释。仅使用
Python 3 标准库；每个场景使用独立临时目录中的 JSONL 与 SQLite，
不依赖预存数据库、网络或第三方包，测试结束不遗留任何临时文件。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 访问时段：含起点、不含终点（UTC）。
WINDOW_FROM = "2026-10-06T10:00:00"
WINDOW_BEFORE = "2026-10-08T00:00:00"

# 固定合成样例：十条事件（u1 五条、u2 两条、u3 两条、u4 一条）。
# u1：5 日 23:59:00 visit（段外历史，不影响归组）；
#     6 日 10:00:00 visit（段内最早，归 6 日组）；
#     7 日 23:59:00 与 23:59:30 各 visit 一次；
#     8 日 00:00:30 signup（与 23:59:30 visit 相隔恰 60 秒）。
# u2：7 日 12:00:00 同时刻 visit 与 signup（相等不算转化）。
# u3：8 日 00:00:00 visit（恰在时段终点，终点不含而被排除）、
#     8 日 00:00:30 signup。
# u4：仅 6 日 12:00:00 visit（不转化）。
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

EXPECTED_VISIT_IDS = ["u1", "u2", "u4"]
EXPECTED_CONVERTED_IDS = ["u1"]
EXPECTED_PAIRS = [
    {
        "user_id": "u1",
        "visit_timestamp": "2026-10-07T23:59:30",
        "signup_timestamp": "2026-10-08T00:00:30",
    }
]
# (visit_date, visit_users, converted_users, conversion_rate)
EXPECTED_GROUPS_60 = [
    ("2026-10-06", 2, 1, 0.5),
    ("2026-10-07", 1, 0, 0),
]
EXPECTED_GROUPS_59 = [
    ("2026-10-06", 2, 0, 0),
    ("2026-10-07", 1, 0, 0),
]
# 与 --include-users 同开时各组的 (visit_user_ids, converted_user_ids)：
# u1、u4 归 6 日组，u2 归 7 日组；59 秒时限下唯一配对恰为 60 秒，转化归零。
EXPECTED_GROUP_IDS_60 = [(["u1", "u4"], ["u1"]), (["u2"], [])]
EXPECTED_GROUP_IDS_59 = [(["u1", "u4"], []), (["u2"], [])]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}
FULL_KEYS = METRIC_KEYS | {
    "visit_user_ids",
    "converted_user_ids",
    "conversion_pairs",
    "visit_date_groups",
}
GROUP_KEYS = {"visit_date", "visit_users", "converted_users", "conversion_rate"}
# --include-users 与 --group-by 同开时，组内追加的编号明细字段。
GROUP_ID_KEYS = GROUP_KEYS | {"visit_user_ids", "converted_user_ids"}
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


class VisitWindowConsistencyTests(unittest.TestCase):
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

    def import_events(self, events=ACCEPTANCE_EVENTS, db=None,
                      jsonl_name="events.jsonl"):
        if db is None:
            db = self.db_path()
        path = self.write_jsonl(jsonl_name, events)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        return db, result

    def full_report(self, db, within_seconds):
        """三种输出同时开启的时段报告。"""
        return run_funnel(
            "report",
            "--db",
            db,
            "--group-by",
            "visit-date",
            "--include-users",
            "--include-pairs",
            "--within-seconds",
            str(within_seconds),
            "--visit-from",
            WINDOW_FROM,
            "--visit-before",
            WINDOW_BEFORE,
        )

    @staticmethod
    def parse_single_json_object(stdout):
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        assert nonempty_lines, "标准输出为空，无法解析 JSON 对象: %r" % stdout
        assert len(nonempty_lines) == 1, "标准输出包含多行，不是单个 JSON 对象: %r" % stdout
        obj = json.loads(nonempty_lines[0])
        assert isinstance(obj, dict), "标准输出不是 JSON 对象: %r" % stdout
        return obj

    def assertSuccessSilent(self, result):
        """成功协议：退出码 0、标准错误为空。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")

    def assertGroups(self, payload, expected, expected_ids=None):
        """expected 为 (visit_date, visit_users, converted_users, rate) 元组列表。

        expected_ids 提供时（--include-users 同时启用的场景），逐项核对组内
        (visit_user_ids, converted_user_ids)，并检查组间合并关系。
        """
        groups = payload["visit_date_groups"]
        self.assertIsInstance(groups, list)
        self.assertEqual(len(groups), len(expected))
        want_keys = GROUP_ID_KEYS if expected_ids is not None else GROUP_KEYS
        for index, (group, (day, visits, converted, rate)) in enumerate(
            zip(groups, expected)
        ):
            self.assertEqual(set(group), want_keys)
            self.assertEqual(group["visit_date"], day)
            self.assertEqual(group["visit_users"], visits)
            self.assertEqual(group["converted_users"], converted)
            self.assertEqual(group["conversion_rate"], rate)
            if expected_ids is not None:
                visit_ids, converted_ids = expected_ids[index]
                self.assertEqual(group["visit_user_ids"], visit_ids)
                self.assertEqual(group["converted_user_ids"], converted_ids)
                # 数组长度等于对应人数；转化数组是本组访问数组的子集。
                self.assertEqual(len(visit_ids), visits)
                self.assertEqual(len(converted_ids), converted)
                self.assertLessEqual(set(converted_ids), set(visit_ids))
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
        if expected_ids is not None:
            # 各组两类编号分别合并（排序后）等于顶层相应数组，组间没有重复编号。
            merged_visits = [uid for g in groups for uid in g["visit_user_ids"]]
            merged_converted = [
                uid for g in groups for uid in g["converted_user_ids"]
            ]
            self.assertEqual(sorted(merged_visits), payload["visit_user_ids"])
            self.assertEqual(sorted(merged_converted), payload["converted_user_ids"])
            self.assertEqual(len(merged_visits), len(set(merged_visits)))
            self.assertEqual(len(merged_converted), len(set(merged_converted)))

    def snapshot_events(self, db):
        """报告只读：逐行快照全部事件记录。"""
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 导入 -------------------------------------------------------------

    def test_import_reports_ten(self):
        _db, result = self.import_events(jsonl_name="window-consistency.jsonl")
        self.assertSuccessSilent(result)
        self.assertEqual(json.loads(result.stdout), {"imported": 10})

    # -- 60 秒时限：三种输出彼此一致 --------------------------------------

    def test_60_seconds_full_output_is_consistent(self):
        db, _ = self.import_events(jsonl_name="window-consistency.jsonl")
        result = self.full_report(db, 60)
        self.assertSuccessSilent(result)
        payload = self.parse_single_json_object(result.stdout)

        # 只含约定字段，不多不少。
        self.assertEqual(set(payload), FULL_KEYS)

        # 汇总：段内访问 3 人（u3 恰在终点的 visit 被排除），转化 1 人。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)

        # 编号明细：按 Unicode 码点升序，核对解析后的数组顺序。
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], EXPECTED_CONVERTED_IDS)
        self.assertEqual(len(payload["visit_user_ids"]), payload["visit_users"])
        self.assertEqual(
            len(payload["converted_user_ids"]), payload["converted_users"]
        )

        # 配对明细：仅 u1；最早有效 signup 配最晚有效 visit，
        # 沿用既有对象字段名与定宽 ISO 时间戳。
        pairs = payload["conversion_pairs"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(set(pairs[0]), PAIR_KEYS)
        self.assertEqual(pairs, EXPECTED_PAIRS)
        pair_ids = [pair["user_id"] for pair in pairs]
        self.assertEqual(pair_ids, payload["converted_user_ids"])

        # 分组：u1 段外有 5 日历史，仍按段内最早 visit 归 6 日组；
        # 6 日组访问 2 人转化 1 人比例 0.5，7 日组访问 1 人转化 0 人；
        # 组内编号明细与顶层编号数组一致。
        self.assertGroups(payload, EXPECTED_GROUPS_60, EXPECTED_GROUP_IDS_60)

    def test_edge_rules_unchanged_under_window(self):
        """段外历史、终点访问、同刻注册均按原规则处理。"""
        db, _ = self.import_events(jsonl_name="window-consistency.jsonl")
        payload = self.parse_single_json_object(self.full_report(db, 60).stdout)

        # u1 的段外历史（5 日 visit）不把归组提前到 5 日，也不产生 5 日组。
        self.assertEqual(
            [g["visit_date"] for g in payload["visit_date_groups"]],
            ["2026-10-06", "2026-10-07"],
        )
        # u2 同刻 visit/signup 不算转化；u3 终点 visit 被整人排除，
        # 其段外 signup 也无法与任何段内 visit 配对。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u4"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(
            [pair["user_id"] for pair in payload["conversion_pairs"]], ["u1"]
        )

    # -- 59 秒时限：唯一配对恰为 60 秒，全部转化归零 ----------------------

    def test_59_seconds_keeps_visits_and_zeroes_conversions(self):
        db, _ = self.import_events(jsonl_name="window-consistency.jsonl")
        result = self.full_report(db, 59)
        self.assertSuccessSilent(result)
        payload = self.parse_single_json_object(result.stdout)

        self.assertEqual(set(payload), FULL_KEYS)
        # 访问人数与归组不变；所有转化人数与比例归零。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 0)
        self.assertEqual(payload["conversion_rate"], 0)
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["conversion_pairs"], [])
        self.assertGroups(payload, EXPECTED_GROUPS_59, EXPECTED_GROUP_IDS_59)

    # -- 60 与 59 秒下明细开关不改变汇总数值 ------------------------------

    def test_detail_flags_do_not_change_metrics(self):
        db, _ = self.import_events(jsonl_name="window-consistency.jsonl")
        flag_sets = (
            (),
            ("--include-users",),
            ("--include-pairs",),
            ("--group-by", "visit-date"),
            ("--include-users", "--include-pairs", "--group-by", "visit-date"),
        )
        for within in (60, 59):
            plain = run_funnel(
                "report",
                "--db",
                db,
                "--within-seconds",
                str(within),
                "--visit-from",
                WINDOW_FROM,
                "--visit-before",
                WINDOW_BEFORE,
            )
            self.assertSuccessSilent(plain)
            baseline = self.parse_single_json_object(plain.stdout)
            for flags in flag_sets:
                with self.subTest(within=within, flags=flags):
                    result = run_funnel(
                        "report",
                        "--db",
                        db,
                        "--within-seconds",
                        str(within),
                        "--visit-from",
                        WINDOW_FROM,
                        "--visit-before",
                        WINDOW_BEFORE,
                        *flags,
                    )
                    self.assertSuccessSilent(result)
                    payload = self.parse_single_json_object(result.stdout)
                    for key in METRIC_KEYS:
                        self.assertEqual(payload[key], baseline[key])

    # -- 报告只读：成功时标准错误为空，记录逐行一致 -----------------------

    def test_reports_keep_events_unchanged(self):
        db, _ = self.import_events(jsonl_name="window-consistency.jsonl")
        before = self.snapshot_events(db)
        r60 = self.full_report(db, 60)
        r59 = self.full_report(db, 59)
        self.assertSuccessSilent(r60)
        self.assertSuccessSilent(r59)
        self.assertEqual(self.snapshot_events(db), before)
        self.assertEqual(len(before), 10)

    # -- 只提供访问起点：退出码 2，stdout 为空，stderr 指出缺参及原因 -----

    def test_visit_from_alone_rejected_and_records_unchanged(self):
        db, _ = self.import_events(jsonl_name="window-consistency.jsonl")
        before = self.snapshot_events(db)
        result = run_funnel(
            "report",
            "--db",
            db,
            "--group-by",
            "visit-date",
            "--include-users",
            "--include-pairs",
            "--within-seconds",
            "60",
            "--visit-from",
            WINDOW_FROM,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        # 标准错误指出缺少的配对参数及成对使用的原因（不绑定错误全文）。
        self.assertIn("--visit-from", result.stderr)
        self.assertIn("--visit-before", result.stderr)
        self.assertIn("成对使用", result.stderr)
        # 已有记录逐行不变。
        self.assertEqual(self.snapshot_events(db), before)
        self.assertEqual(len(before), 10)


if __name__ == "__main__":
    unittest.main()
