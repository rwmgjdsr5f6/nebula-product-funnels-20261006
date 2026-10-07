"""访问时段 + 转化时限 + 两种明细开关同时使用时，跨午夜事件的
统计与配对一致性回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite>
        --within-seconds N
        --visit-from YYYY-MM-DDTHH:MM:SS --visit-before YYYY-MM-DDTHH:MM:SS
        [--include-users] [--include-pairs]

固定合成样例共十三条事件，时间均按 UTC 解释：23 点时刻属于
2026-10-06，零点时刻属于 2026-10-07。仅使用 Python 3 标准库；每个
场景使用独立临时目录中的 JSONL 与 SQLite，不依赖预存数据库、网络或
第三方包，测试结束不遗留任何临时文件。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 跨午夜验收样例：十三条事件（u1 七条、u2 两条、u3 两条、u4 两条）。
# u1：visit 23:58:30 / 23:59:00 / 23:59:30 / 次日 00:00:00，
#     signup 23:59:00 / 次日 00:00:30 / 次日 00:01:00
# u2：visit 23:59:40，signup 次日 00:00:40
# u3：visit 与 signup 同在 23:59:10（相等时刻不算转化）
# u4：visit 与 signup 都在次日零点之后（visit 恰在窗口终点，被排除）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:58:30"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:00"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T23:59:30"},
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-07T00:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T23:59:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T00:00:30"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-07T00:01:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T23:59:40"},
    {"user_id": "u2", "event": "signup", "timestamp": "2026-10-07T00:00:40"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T23:59:10"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T23:59:10"},
    {"user_id": "u4", "event": "visit", "timestamp": "2026-10-07T00:00:00"},
    {"user_id": "u4", "event": "signup", "timestamp": "2026-10-07T00:00:30"},
]

# 访问时段 [23:59:00, 次日 00:00:00)：含起点、不含终点。
WINDOW_FROM = "2026-10-06T23:59:00"
WINDOW_BEFORE = "2026-10-07T00:00:00"

EXPECTED_VISIT_IDS = ["u1", "u2", "u3"]
EXPECTED_CONVERTED_IDS = ["u1", "u2"]
EXPECTED_PAIRS = [
    {
        "user_id": "u1",
        "visit_timestamp": "2026-10-06T23:59:30",
        "signup_timestamp": "2026-10-07T00:00:30",
    },
    {
        "user_id": "u2",
        "visit_timestamp": "2026-10-06T23:59:40",
        "signup_timestamp": "2026-10-07T00:00:40",
    },
]

# 固定打乱行序（不使用随机数，保证可重复）：按 4k+1 取模 13 轮转。
SHUFFLED_EVENTS = [ACCEPTANCE_EVENTS[i % 13] for i in (1, 5, 9, 0, 4, 8, 12, 3, 7, 11, 2, 6, 10)]

METRIC_KEYS = {"visit_users", "converted_users", "conversion_rate"}
DETAIL_KEYS = METRIC_KEYS | {
    "visit_user_ids",
    "converted_user_ids",
    "conversion_pairs",
}
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

    def windowed_report(self, db, within_seconds, *flags):
        return self.report(
            db,
            "--within-seconds",
            str(within_seconds),
            "--visit-from",
            WINDOW_FROM,
            "--visit-before",
            WINDOW_BEFORE,
            *flags,
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

    # -- 导入 -------------------------------------------------------------

    def test_import_reports_thirteen(self):
        db, result = self.import_events(
            ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl"
        )
        self.assertSuccessSilent(result)
        self.assertEqual(json.loads(result.stdout), {"imported": 13})

    # -- 60 秒时限 + 访问时段 + 两种明细 ----------------------------------

    def test_60_seconds_window_with_both_details(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        result = self.windowed_report(db, 60, "--include-users", "--include-pairs")
        self.assertSuccessSilent(result)
        payload = self.parse_single_json_object(result.stdout)

        # 段内 visit：u1（23:59:00、23:59:30）、u2（23:59:40）、u3（23:59:10）；
        # u4 的 visit 恰在终点 00:00:00，终点不含，被排除。
        self.assertEqual(set(payload), DETAIL_KEYS)
        self.assert_metrics(payload, 3, 2, 2 / 3)
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], EXPECTED_CONVERTED_IDS)
        self.assertEqual(payload["conversion_pairs"], EXPECTED_PAIRS)

    def test_60_seconds_pairs_cross_midnight_with_full_dates(self):
        # u1：段内 23:59:30 visit 与次日 00:00:30 signup 间隔恰 60 秒（上界含
        # 等值）；其 23:59:00 visit 与同刻 signup 相等不算，与 00:00:30 相隔
        # 90 秒超限。u2：23:59:40 -> 00:00:40 恰 60 秒。
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        result = self.windowed_report(db, 60, "--include-pairs")
        self.assertSuccessSilent(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), PAIR_KEYS)
        pairs = payload["conversion_pairs"]
        self.assertEqual([pair["user_id"] for pair in pairs], ["u1", "u2"])
        for pair in pairs:
            self.assertEqual(
                set(pair), {"user_id", "visit_timestamp", "signup_timestamp"}
            )
        self.assertEqual(pairs, EXPECTED_PAIRS)
        # 时间戳包含完整日期且跨午夜两天，沿用现有字段名与定宽 ISO 格式。
        self.assertEqual(pairs[0]["visit_timestamp"], "2026-10-06T23:59:30")
        self.assertEqual(pairs[0]["signup_timestamp"], "2026-10-07T00:00:30")
        self.assertEqual(pairs[1]["visit_timestamp"], "2026-10-06T23:59:40")
        self.assertEqual(pairs[1]["signup_timestamp"], "2026-10-07T00:00:40")
        # 数组长度等于转化人数，编号不重复且与 converted_user_ids 一致。
        self.assertEqual(len(pairs), payload["converted_users"])
        self.assertEqual(len({pair["user_id"] for pair in pairs}), len(pairs))

    def test_pairs_consistent_with_user_lists(self):
        # 配对编号集合等于 converted_user_ids，且转化编号都在访问编号中。
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        payload = self.parse_single_json_object(
            self.windowed_report(db, 60, "--include-users", "--include-pairs").stdout
        )
        pair_ids = [pair["user_id"] for pair in payload["conversion_pairs"]]
        self.assertEqual(pair_ids, payload["converted_user_ids"])
        self.assertEqual(
            len(payload["visit_user_ids"]), payload["visit_users"]
        )
        self.assertEqual(
            len(payload["converted_user_ids"]), payload["converted_users"]
        )
        for user_id in payload["converted_user_ids"]:
            self.assertIn(user_id, payload["visit_user_ids"])

    # -- 59 秒时限：跨午夜配对恰好 60 秒，全部落网外 ----------------------

    def test_59_seconds_window_zero_conversions(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        result = self.windowed_report(db, 59, "--include-users", "--include-pairs")
        self.assertSuccessSilent(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), DETAIL_KEYS)
        # 访问人数与访问编号不变；转化人数、比例为 0，转化编号与配对为空。
        self.assert_metrics(payload, 3, 0, 0)
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["conversion_pairs"], [])

    # -- 去掉明细开关：只输出原有三个汇总字段，数值不变 -------------------

    def test_without_details_only_metrics_unchanged(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        for within, converted, rate in ((60, 2, 2 / 3), (59, 0, 0)):
            with self.subTest(within=within):
                result = self.windowed_report(db, within)
                self.assertSuccessSilent(result)
                payload = self.parse_single_json_object(result.stdout)
                self.assertEqual(set(payload), METRIC_KEYS)
                self.assert_metrics(payload, 3, converted, rate)

    def test_detail_flags_do_not_change_metrics(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        flag_sets = (
            (),
            ("--include-users",),
            ("--include-pairs",),
            ("--include-users", "--include-pairs"),
        )
        for within in (60, 59):
            plain = self.parse_single_json_object(
                self.windowed_report(db, within).stdout
            )
            for flags in flag_sets:
                with self.subTest(within=within, flags=flags):
                    payload = self.parse_single_json_object(
                        self.windowed_report(db, within, *flags).stdout
                    )
                    for key in METRIC_KEYS:
                        self.assertEqual(payload[key], plain[key])

    # -- 行序无关 + 重复导入（追加语义） ----------------------------------

    def test_shuffled_and_reimport_give_same_60_seconds_result(self):
        # 先导入固定打乱行序的一批，再把原序批次追加导入一遍：
        # 重复事件不增加人数，60 秒报告结果须与单次导入完全一致。
        path = self.write_jsonl("shuffled.jsonl", SHUFFLED_EVENTS)
        db = self.db_path("shuffled.sqlite")
        first = run_funnel("import", path, "--db", db)
        self.assertSuccessSilent(first)
        self.assertEqual(json.loads(first.stdout), {"imported": 13})
        second_path = self.write_jsonl("cross-midnight.jsonl", ACCEPTANCE_EVENTS)
        second = run_funnel("import", second_path, "--db", db)
        self.assertSuccessSilent(second)
        self.assertEqual(json.loads(second.stdout), {"imported": 13})

        result = self.windowed_report(db, 60, "--include-users", "--include-pairs")
        self.assertSuccessSilent(result)
        payload = self.parse_single_json_object(result.stdout)
        self.assertEqual(set(payload), DETAIL_KEYS)
        self.assert_metrics(payload, 3, 2, 2 / 3)
        self.assertEqual(payload["visit_user_ids"], EXPECTED_VISIT_IDS)
        self.assertEqual(payload["converted_user_ids"], EXPECTED_CONVERTED_IDS)
        self.assertEqual(payload["conversion_pairs"], EXPECTED_PAIRS)

        # 追加后共 26 行事件记录，报告不得改写任何记录。
        self.assertEqual(len(self.snapshot_events(db)), 26)

    # -- 报告只读 ---------------------------------------------------------

    def test_reports_keep_events_unchanged(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="cross-midnight.jsonl")
        before = self.snapshot_events(db)
        self.windowed_report(db, 60, "--include-users", "--include-pairs")
        self.windowed_report(db, 59, "--include-users", "--include-pairs")
        self.windowed_report(db, 60)
        self.windowed_report(db, 59)
        self.report(db)
        self.assertEqual(self.snapshot_events(db), before)
        self.assertEqual(len(before), 13)

    # -- 参数错误：只传 --visit-from 时即便两种明细都开也拒绝 -------------

    def test_visit_from_alone_rejected_with_both_details(self):
        db = self.db_path("never-created.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(
            db,
            "--visit-from",
            WINDOW_FROM,
            "--within-seconds",
            "60",
            "--include-users",
            "--include-pairs",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        # 标准错误指出两个参数须成对使用。
        self.assertIn("--visit-from", result.stderr)
        self.assertIn("--visit-before", result.stderr)
        self.assertIn("成对使用", result.stderr)
        # 参数校验先于数据库访问：原本不存在的数据库不被创建。
        self.assertFalse(os.path.exists(db))


if __name__ == "__main__":
    unittest.main()
