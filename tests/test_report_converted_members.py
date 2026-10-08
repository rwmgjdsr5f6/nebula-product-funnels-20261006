"""report 转化成员统一来源的回归测试。

固定本次重构后的内部结构与其可观察结果：人数（converted_users）、
转化编号（converted_user_ids）、配对/耗时明细与 visit_date_groups
各组的转化成员全部来自同一次配对枚举归约出的同一份转化成员映射，
对同一批转化用户不再分别发 COUNT、DISTINCT 编号等重复查询。

公开行为（命令入口、参数、SQLite 结构、追加导入、JSON 字段、退出码、
标准流约定）与重构前完全一致，本文件只通过公开命令
``python -m funnel import|report`` 观察；唯一的进程内测桩仅用于统计
SQL 执行条数，不改变任何结果。验收数据为六条合成事件：

- u1：2026-10-06 10:00:00 与次日 10:00:00 各 visit 一次，次日 10:00:30
  signup（归 6 日组；转化使用 7 日那次 visit，不限于归组那次）
- u2：仅 6 日 11:00:00 visit（不转化）
- u3：7 日 12:00:00 同刻 visit 与 signup（相等不算转化）

仅使用 Python 3 标准库；每个场景使用独立临时目录，全部用户编号均为
虚构，测试结束不遗留任何文件。
"""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from funnel import __main__ as funnel_main

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

# 有访问但无人转化：u2 只访问；u3 访问与注册同刻（相等不算转化）。
NO_CONVERSION_EVENTS = [
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-07T12:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-07T12:00:00"},
]

SIGNUP_ONLY_EVENTS = [
    {"user_id": "u9", "event": "signup", "timestamp": "2026-10-06T10:00:00"},
]

ALL_DETAIL_FLAGS = [
    "--within-seconds", "60",
    "--include-users",
    "--include-pairs",
    "--include-latency",
    "--group-by", "visit-date",
    "--include-group-pairs",
    "--include-group-latency",
]


def run_funnel(*cli_args):
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


class ConvertedMembersSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 -------------------------------------------------------------

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
            result.returncode, 0,
            msg="导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        return db, path

    def report(self, db, *extra_args):
        return run_funnel("report", "--db", db, *extra_args)

    @staticmethod
    def parse_report(result):
        assert result.returncode == 0, "stderr=%r" % result.stderr
        assert result.stderr == "", "stderr=%r" % result.stderr
        lines = result.stdout.splitlines()
        assert len(lines) == 1 and lines[0], "stdout=%r" % result.stdout
        return json.loads(lines[0])

    # -- 六事件验收：人数、明细、日期组同源 -------------------------------

    def test_six_events_full_flags_within_60(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS, jsonl_name="acceptance.jsonl")
        payload = self.parse_report(
            self.report(db, "--group-by", "visit-date",
                        "--include-users", "--within-seconds", "60")
        )
        # 汇总：3 人访问、1 人转化、比例 1/3。
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1 / 3)
        # 转化编号只有 u1，按 Unicode 码点升序。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        # 6 日组 2、1、0.5（u1、u2），7 日组 1、0、0（u3）；u1 归 6 日组。
        self.assertEqual(len(payload["visit_date_groups"]), 2)
        day6, day7 = payload["visit_date_groups"]
        self.assertEqual(day6["visit_date"], "2026-10-06")
        self.assertEqual((day6["visit_users"], day6["converted_users"],
                          day6["conversion_rate"]), (2, 1, 0.5))
        self.assertEqual(day6["visit_user_ids"], ["u1", "u2"])
        self.assertEqual(day6["converted_user_ids"], ["u1"])
        self.assertEqual(day7["visit_date"], "2026-10-07")
        self.assertEqual((day7["visit_users"], day7["converted_users"],
                          day7["conversion_rate"]), (1, 0, 0))
        self.assertEqual(day7["visit_user_ids"], ["u3"])
        self.assertEqual(day7["converted_user_ids"], [])
        # 人数与编号数组长度天然勾稽（同一来源）。
        self.assertEqual(len(payload["converted_user_ids"]),
                         payload["converted_users"])
        for group in payload["visit_date_groups"]:
            self.assertEqual(len(group["converted_user_ids"]),
                             group["converted_users"])
            self.assertEqual(len(group["visit_user_ids"]),
                             group["visit_users"])

    def test_window_only_seventh_reassigns_u1(self):
        # 访问时段限定 7 日全天（含起点、不含终点）：u2 被整人排除，
        # u1 段外的 6 日 visit 不参与归组，改归 7 日组；signup 仍可配对。
        db, _ = self.import_events(ACCEPTANCE_EVENTS)
        payload = self.parse_report(
            self.report(db, "--group-by", "visit-date", "--include-users",
                        "--within-seconds", "60",
                        "--visit-from", "2026-10-07T00:00:00",
                        "--visit-before", "2026-10-08T00:00:00")
        )
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 0.5)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(len(payload["visit_date_groups"]), 1)
        group = payload["visit_date_groups"][0]
        self.assertEqual(group["visit_date"], "2026-10-07")
        self.assertEqual((group["visit_users"], group["converted_users"],
                          group["conversion_rate"]), (2, 1, 0.5))
        self.assertEqual(group["visit_user_ids"], ["u1", "u3"])
        self.assertEqual(group["converted_user_ids"], ["u1"])

    # -- 明细开关：去掉后汇总不变、未启用字段不出现 -----------------------

    def test_summary_unchanged_and_fields_absent_without_include_users(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS)
        with_users = self.parse_report(
            self.report(db, "--group-by", "visit-date",
                        "--include-users", "--within-seconds", "60")
        )
        without_users = self.parse_report(
            self.report(db, "--group-by", "visit-date",
                        "--within-seconds", "60")
        )
        # 汇总三项完全一致。
        for key in ("visit_users", "converted_users", "conversion_rate"):
            self.assertEqual(with_users[key], without_users[key])
        # 顶层与组内都不出现未启用的编号字段。
        self.assertNotIn("visit_user_ids", without_users)
        self.assertNotIn("converted_user_ids", without_users)
        self.assertEqual(
            set(without_users),
            {"visit_users", "converted_users", "conversion_rate",
             "visit_date_groups"},
        )
        for group in without_users["visit_date_groups"]:
            self.assertEqual(
                set(group),
                {"visit_date", "visit_users", "converted_users",
                 "conversion_rate"},
            )

    def test_unenabled_optional_fields_absent_on_plain_report(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS)
        payload = self.parse_report(self.report(db, "--within-seconds", "60"))
        self.assertEqual(
            set(payload),
            {"visit_users", "converted_users", "conversion_rate"},
        )

    # -- 零访问 / 有访问无转化 --------------------------------------------

    def test_zero_visits_gives_zero_summary_empty_ids_and_groups(self):
        db, _ = self.import_events(SIGNUP_ONLY_EVENTS, jsonl_name="signup.jsonl")
        payload = self.parse_report(
            self.report(db, "--group-by", "visit-date", "--include-users")
        )
        self.assertEqual(payload["visit_users"], 0)
        self.assertEqual(payload["converted_users"], 0)
        self.assertEqual(payload["conversion_rate"], 0)
        self.assertEqual(payload["visit_user_ids"], [])
        self.assertEqual(payload["converted_user_ids"], [])
        self.assertEqual(payload["visit_date_groups"], [])

    def test_visits_without_conversion_gives_empty_converted_ids(self):
        db, _ = self.import_events(NO_CONVERSION_EVENTS, jsonl_name="none.jsonl")
        payload = self.parse_report(
            self.report(db, "--group-by", "visit-date",
                        "--include-users", "--within-seconds", "60")
        )
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 0)
        self.assertEqual(payload["conversion_rate"], 0)
        self.assertEqual(payload["visit_user_ids"], ["u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], [])
        for group in payload["visit_date_groups"]:
            self.assertEqual(group["converted_users"], 0)
            self.assertEqual(group["converted_user_ids"], [])

    # -- 打乱行序 / 重复导入：报告一致 ------------------------------------

    def test_shuffled_rows_and_reimport_and_duplicates_give_same_report(self):
        base_db, base_path = self.import_events(ACCEPTANCE_EVENTS)
        baseline = self.parse_report(self.report(base_db, *ALL_DETAIL_FLAGS))

        # 1) 打乱行序后导入：完整报告逐字段相等。
        shuffled_db, _ = self.import_events(SHUFFLED_EVENTS,
                                            db=self.db_path("shuffled.sqlite"),
                                            jsonl_name="shuffled.jsonl")
        self.assertEqual(
            self.parse_report(self.report(shuffled_db, *ALL_DETAIL_FLAGS)),
            baseline,
        )

        # 2) 同一文件对同一数据库追加导入两次（追加导入方式不变）：去重后
        #    完整报告与单次导入逐字段相等。
        reimport_db = self.db_path("reimport.sqlite")
        for _ in range(2):
            result = run_funnel("import", base_path, "--db", reimport_db)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            self.parse_report(self.report(reimport_db, *ALL_DETAIL_FLAGS)),
            baseline,
        )

        # 3) 导入时就混入完全重复的事件行：报告同样一致。
        dupes_db, _ = self.import_events(
            ACCEPTANCE_EVENTS + ACCEPTANCE_EVENTS,
            db=self.db_path("dupes.sqlite"),
            jsonl_name="dupes.jsonl",
        )
        self.assertEqual(
            self.parse_report(self.report(dupes_db, *ALL_DETAIL_FLAGS)),
            baseline,
        )

    # -- 单一来源勾稽：配对/耗时/各组都与同一转化成员集合一致 ------------

    def test_pairs_latency_and_groups_reconcile_with_summary(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS)
        payload = self.parse_report(self.report(db, *ALL_DETAIL_FLAGS))
        converted_ids = set(payload["converted_user_ids"])
        self.assertEqual(payload["converted_users"], len(converted_ids))
        # 顶层配对成员与转化编号完全一致，一人一对。
        self.assertEqual(
            sorted(p["user_id"] for p in payload["conversion_pairs"]),
            sorted(converted_ids),
        )
        self.assertEqual(len(payload["conversion_pairs"]),
                         payload["converted_users"])
        # 各组配对成员的并集恰为全体转化编号（组间不重复）。
        group_pair_ids = []
        for group in payload["visit_date_groups"]:
            group_pair_ids.extend(p["user_id"] for p in group["conversion_pairs"])
        self.assertEqual(sorted(group_pair_ids), sorted(converted_ids))
        # 耗时统计与唯一配对集合同人数口径。
        latency = payload["conversion_latency"]
        self.assertEqual(latency["min_seconds"], 30)
        self.assertEqual(latency["max_seconds"], 30)
        self.assertEqual(latency["mean_seconds"], 30)

    # -- 每次报告只对转化成员发一条 JOIN 查询（进程内测桩） ---------------

    def test_report_runs_single_converted_members_query(self):
        db, _ = self.import_events(ACCEPTANCE_EVENTS)
        captured = []

        # Python 3.14 中 sqlite3.Connection 是不可变内置类型，不能直接
        # patch 其 execute；改用连接工厂子类记录 SQL，再替换模块级 connect。
        class SpyConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                captured.append(sql)
                return super().execute(sql, parameters)

        def spy_connect(database, *args, **kwargs):
            return original_connect(database, *args, factory=SpyConnection, **kwargs)

        original_connect = sqlite3.connect
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(funnel_main.sqlite3, "connect", side_effect=spy_connect):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = funnel_main.main(["report", "--db", db, *ALL_DETAIL_FLAGS])
        self.assertEqual(rc, 0, msg=stderr.getvalue())
        # 转化成员的 events 自连接（JOIN events s）在整条报告流程中只出现
        # 一次：人数、编号、配对、耗时与日期组共用同一次枚举结果。
        join_calls = [sql for sql in captured if "JOIN events s" in sql]
        self.assertEqual(len(join_calls), 1, msg=join_calls)
        # 重构后不存在单独的转化人数 COUNT 查询。
        self.assertFalse(
            any("COUNT(DISTINCT v.user_id)" in sql for sql in captured),
            msg=captured,
        )
        # 进程内结果与子进程验收口径一致。
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["converted_user_ids"], ["u1"])


if __name__ == "__main__":
    unittest.main()
