"""访问时段、转化时限与两种明细开关同时使用时的跨午夜回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> --visit-from ... --visit-before ...
                              --within-seconds N [--include-users] [--include-pairs]

固定合成样例共 13 条事件，时间均按 UTC 解释：23 点时刻属于 2026-10-06，
零点时刻属于 2026-10-07。

u1：visit 23:58:30、23:59:00、23:59:30、次日 00:00:00；
    signup 23:59:00、次日 00:00:30、次日 00:01:00
u2：visit 23:59:40，signup 次日 00:00:40
u3：visit 与 signup 同在 23:59:10（相等时刻不算转化）
u4：visit 次日 00:00:00，signup 次日 00:00:30（访问落在窗口终点，被排除）

报告访问时段为 [2026-10-06T23:59:00, 2026-10-07T00:00:00)（含起点不含终点）。

仅使用 Python 3 标准库；每个场景使用独立临时目录中的 JSONL 与 SQLite，
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

D6 = "2026-10-06"
D7 = "2026-10-07"

# 跨午夜固定样例：13 条事件（4+3+1+1+2+2）。
EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": D6 + "T23:58:30"},
    {"user_id": "u1", "event": "visit", "timestamp": D6 + "T23:59:00"},
    {"user_id": "u1", "event": "visit", "timestamp": D6 + "T23:59:30"},
    {"user_id": "u1", "event": "visit", "timestamp": D7 + "T00:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": D6 + "T23:59:00"},
    {"user_id": "u1", "event": "signup", "timestamp": D7 + "T00:00:30"},
    {"user_id": "u1", "event": "signup", "timestamp": D7 + "T00:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": D6 + "T23:59:40"},
    {"user_id": "u2", "event": "signup", "timestamp": D7 + "T00:00:40"},
    {"user_id": "u3", "event": "visit", "timestamp": D6 + "T23:59:10"},
    {"user_id": "u3", "event": "signup", "timestamp": D6 + "T23:59:10"},
    {"user_id": "u4", "event": "visit", "timestamp": D7 + "T00:00:00"},
    {"user_id": "u4", "event": "signup", "timestamp": D7 + "T00:00:30"},
]

# 固定打乱行序（不使用随机数，保证可重复）：0..12 的一个置换。
SHUFFLED_EVENTS = [
    EVENTS[i] for i in (12, 0, 7, 3, 10, 5, 1, 8, 11, 4, 9, 2, 6)
]

VISIT_FROM = D6 + "T23:59:00"
VISIT_BEFORE = D7 + "T00:00:00"
WINDOW_ARGS = ("--visit-from", VISIT_FROM, "--visit-before", VISIT_BEFORE)

EXPECTED_VISIT_IDS = ["u1", "u2", "u3"]
EXPECTED_CONVERTED_IDS = ["u1", "u2"]
EXPECTED_PAIRS = [
    {
        "user_id": "u1",
        # 最早有效 signup 为次日 00:00:30，与其配对的最晚段内 visit 为
        # 23:59:30（间隔恰好 60 秒，窗口上界包含）。
        "visit_timestamp": D6 + "T23:59:30",
        "signup_timestamp": D7 + "T00:00:30",
    },
    {
        "user_id": "u2",
        "visit_timestamp": D6 + "T23:59:40",
        "signup_timestamp": D7 + "T00:00:40",
    },
]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}
DETAIL_KEYS = METRIC_KEYS | {
    "visit_user_ids",
    "converted_user_ids",
    "conversion_pairs",
}


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


class FilteredPairsTests(unittest.TestCase):
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
        return db, result

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

    def assertSuccess(self, result):
        """成功路径：退出码 0、标准错误为空。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")

    def assertMidnightMetrics(self, payload):
        """跨午夜窗口下的汇总数值：访问 3、转化 2、比例 2/3。"""
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 2)
        self.assertAlmostEqual(payload["conversion_rate"], 2 / 3)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    # -- 导入 -------------------------------------------------------------

    def test_import_thirteen_events(self):
        db, result = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        self.assertSuccess(result)
        self.assertEqual(json.loads(result.stdout), {"imported": 13})

    # -- 60 秒时限 + 时段 + 两种明细：跨午夜统计与配对 --------------------

    def test_window_60_seconds_with_both_details(self):
        db, _ = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        result = self.report(
            db,
            *WINDOW_ARGS,
            "--within-seconds", "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertSuccess(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), DETAIL_KEYS)
        self.assertMidnightMetrics(payload)
        # 段内访问编号：u1（23:59:00、23:59:30 在段内）、u2、u3；
        # u4 的访问恰在终点 00:00:00，不含终点故排除。
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], EXPECTED_CONVERTED_IDS)
        self.assertEqual(payload["conversion_pairs"], EXPECTED_PAIRS)
        # 配对数组按编号升序，每对只含沿用的三个字段，时间戳带完整日期。
        pair_ids = [pair["user_id"] for pair in payload["conversion_pairs"]]
        self.assertEqual(pair_ids, sorted(pair_ids))
        self.assertEqual(pair_ids, EXPECTED_CONVERTED_IDS)
        for pair in payload["conversion_pairs"]:
            self.assertEqual(
                set(pair), {"user_id", "visit_timestamp", "signup_timestamp"}
            )
        # 配对编号集合与转化人数、converted_user_ids 完全一致。
        self.assertEqual(len(payload["conversion_pairs"]), payload["converted_users"])

    def test_pairs_consistent_with_user_lists_across_midnight(self):
        """配对一致性：跨午夜配对中的用户与 converted_user_ids 一一对应，
        且每次配对的 signup 严格晚于 visit、间隔不超过 60 秒、visit 落在段内。"""
        db, _ = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        result = self.report(
            db,
            *WINDOW_ARGS,
            "--within-seconds", "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertSuccess(result)
        payload = self.parse_single_json_object(result.stdout)
        pairs = payload["conversion_pairs"]
        self.assertEqual(
            [pair["user_id"] for pair in pairs], payload["converted_user_ids"]
        )
        self.assertTrue(
            set(payload["converted_user_ids"]) <= set(payload["visit_user_ids"])
        )
        for pair in pairs:
            # visit 落在半开时段 [from, before) 内，signup 严格晚于 visit。
            self.assertGreaterEqual(pair["visit_timestamp"], VISIT_FROM)
            self.assertLess(pair["visit_timestamp"], VISIT_BEFORE)
            self.assertGreater(pair["signup_timestamp"], pair["visit_timestamp"])

    # -- 59 秒时限：跨午夜恰好 60 秒的两对全部失败 ------------------------

    def test_window_59_seconds_zero_conversions(self):
        db, _ = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        result = self.report(
            db,
            *WINDOW_ARGS,
            "--within-seconds", "59",
            "--include-users",
            "--include-pairs",
        )
        self.assertSuccess(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), DETAIL_KEYS)
        # 访问人数与访问编号不变，转化人数与比例为 0，两个转化数组为空。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 0)
        self.assertEqual(payload["conversion_rate"], 0)
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["conversion_pairs"], [])

    # -- 去掉明细开关：只输出原有三个汇总字段，数值不变 -------------------

    def test_without_details_only_metrics_unchanged(self):
        db, _ = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        result = self.report(db, *WINDOW_ARGS, "--within-seconds", "60")
        self.assertSuccess(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), METRIC_KEYS)
        self.assertMidnightMetrics(payload)

    def test_detail_flags_do_not_change_metrics(self):
        db, _ = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        for extra in (
            ("--include-users",),
            ("--include-pairs",),
            ("--include-users", "--include-pairs"),
        ):
            with self.subTest(extra=extra):
                plain = self.parse_single_json_object(
                    self.report(db, *WINDOW_ARGS, "--within-seconds", "60").stdout
                )
                detailed = self.parse_single_json_object(
                    self.report(
                        db, *WINDOW_ARGS, "--within-seconds", "60", *extra
                    ).stdout
                )
                for key in METRIC_KEYS:
                    self.assertEqual(detailed[key], plain[key])

    # -- 固定打乱行序 + 重复导入（追加语义）结果不变 ----------------------

    def test_shuffled_rows_and_reimport_give_same_report(self):
        path = self.write_jsonl("midnight-shuffled.jsonl", SHUFFLED_EVENTS)
        db = self.db_path("reimport.sqlite")
        first = run_funnel("import", path, "--db", db)
        self.assertSuccess(first)
        self.assertEqual(json.loads(first.stdout), {"imported": 13})
        # 再次导入同一批乱序数据：追加语义，去重后统计不变。
        second = run_funnel("import", path, "--db", db)
        self.assertSuccess(second)
        self.assertEqual(json.loads(second.stdout), {"imported": 13})
        result = self.report(
            db,
            *WINDOW_ARGS,
            "--within-seconds", "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertSuccess(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertMidnightMetrics(payload)
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], EXPECTED_CONVERTED_IDS)
        self.assertEqual(payload["conversion_pairs"], EXPECTED_PAIRS)

    # -- 错误协议：只传 --visit-from 缺少 --visit-before ------------------

    def test_missing_visit_before_rejected_without_creating_db(self):
        db = self.db_path("never-created.sqlite")
        self.assertFalse(os.path.exists(db))
        # 两种明细开关同时开启也不能改变参数校验结论。
        result = self.report(
            db,
            "--visit-from", VISIT_FROM,
            "--within-seconds", "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 标准错误指出两个参数须成对使用，并点出缺少的参数名。
        self.assertIn("--visit-from", result.stderr)
        self.assertIn("--visit-before", result.stderr)
        self.assertIn("必须成对使用", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # 原本不存在的数据库不得被创建。
        self.assertFalse(os.path.exists(db))

    # -- 报告只读：不改写事件记录 ----------------------------------------

    def test_report_keeps_events_unchanged(self):
        db, _ = self.import_events(EVENTS, jsonl_name="midnight.jsonl")
        before = self.snapshot_events(db)
        self.report(
            db, *WINDOW_ARGS, "--within-seconds", "60",
            "--include-users", "--include-pairs",
        )
        self.report(db, *WINDOW_ARGS, "--within-seconds", "59", "--include-pairs")
        self.report(db, *WINDOW_ARGS, "--within-seconds", "60")
        self.assertEqual(self.snapshot_events(db), before)


if __name__ == "__main__":
    unittest.main()
