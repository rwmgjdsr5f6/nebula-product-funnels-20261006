"""仓库根目录 sample.jsonl 离线演示样例的回归测试。

固定 README「样例」一节公开的样例与预期：

- sample.jsonl 位于项目根目录，无 BOM 的 UTF-8，包含五行 JSON 事件并以
  换行结束；事件顺序固定为 u1 10:00 visit、u1 11:00 signup、u2 10:00
  visit、u3 09:00 signup、u3 10:00 visit（全部发生在 2026-10-06），
  字段沿用既有格式（user_id / event / timestamp，无额外字段）
- 从项目根目录执行
  ``python -m funnel import sample.jsonl --db <新库>``：
  退出码 0、标准错误为空、标准输出只有一个单行 JSON 对象
  ``{"imported": 5}``；目标库原先不存在时由导入创建
- 随后执行 ``python -m funnel report --db <库>``：
  退出码 0、标准错误为空、标准输出只有一个单行 JSON 对象
  ``{"visit_users": 3, "converted_users": 1,
  "conversion_rate": 0.3333333333333333}``
- u1 是唯一转化用户；u2 只有 visit 未注册；u3 的 signup（09:00）早于
  visit（10:00），计访问但不计转化
- 再次导入同一样例仍返回 imported 5（追加语义，不去重行），库中记录
  增加到十条；重复事件不增加人数，普通报告与 --include-users 明细不变
- 输入无法读取、数据库无法访问、报告数据库不存在：退出码 2、标准输出
  为空、标准错误包含相应路径与原因

测试只通过公开命令 ``python -m funnel import|report`` 观察行为，并直接
读取仓库交付的 sample.jsonl 本身（不另造一份同内容输入替代它）。仅使用
Python 3 标准库；每个场景使用独立临时目录，全部用户编号均为虚构，
测试结束不遗留任何文件，不依赖预存数据库、第三方包或网络。
"""

import codecs
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_PATH = os.path.join(PROJECT_ROOT, "sample.jsonl")

# 样例五行记录的固定内容与物理顺序（2026-10-06，UTC）。
EXPECTED_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]

EXPECTED_REPORT = {
    "visit_users": 3,
    "converted_users": 1,
    "conversion_rate": 0.3333333333333333,
}


def run_funnel(*cli_args, cwd=None):
    """在指定工作目录执行 python -m funnel，返回完成的进程结果。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "funnel", *cli_args],
        cwd=cwd if cwd is not None else PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def assert_single_json_line(test_case, stdout, payload):
    """标准输出恰为一个单行 JSON 对象加一个 LF，没有第二行。"""
    test_case.assertEqual(stdout, json.dumps(payload) + "\n")
    test_case.assertEqual(stdout.splitlines(), [json.dumps(payload)])


class SampleFileTests(unittest.TestCase):
    """直接核对仓库交付的 sample.jsonl 本身。"""

    def test_sample_file_exists(self):
        self.assertTrue(
            os.path.isfile(SAMPLE_PATH),
            "项目根目录缺少交付样例 sample.jsonl：%s" % SAMPLE_PATH,
        )

    def test_sample_is_utf8_without_bom_and_ends_with_single_lf(self):
        with open(SAMPLE_PATH, "rb") as fh:
            raw = fh.read()
        # 无 BOM：文件不得以 UTF-8 BOM（EF BB BF）开头。
        self.assertFalse(raw.startswith(codecs.BOM_UTF8))
        # 全部字节必须是合法 UTF-8。
        text = raw.decode("utf-8")
        # 以恰好一个 LF 结束，没有 CRLF 或多余空行。
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertNotIn(b"\r", raw)
        # 五个 JSON 行 + 结尾换行：按 LF 切分得到五个非空行与一个末尾空串。
        pieces = raw.split(b"\n")
        self.assertEqual(len(pieces), 6)
        self.assertEqual(pieces[-1], b"")
        self.assertTrue(all(piece.strip() for piece in pieces[:-1]))
        # 文本视图与字节视图一致：五个非空物理行。
        self.assertEqual(len([line for line in text.splitlines() if line.strip()]), 5)

    def test_sample_five_records_match_expected_values_in_order(self):
        with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
            lines = [line for line in fh.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 5)
        records = []
        for line in lines:
            obj = json.loads(line)
            # 字段沿用既有格式：恰好三个必填字段，值与类型按原样保留。
            self.assertEqual(set(obj.keys()), {"user_id", "event", "timestamp"})
            self.assertIsInstance(obj["user_id"], str)
            self.assertIsInstance(obj["event"], str)
            self.assertIsInstance(obj["timestamp"], str)
            records.append(obj)
        # 顺序固定：u1 visit、u1 signup、u2 visit、u3 signup（早于其
        # visit）、u3 visit。逐行比对字段与原值，不用集合忽略顺序。
        self.assertEqual(records, EXPECTED_EVENTS)
        self.assertEqual(
            [r["timestamp"] for r in records],
            [
                "2026-10-06T10:00:00",
                "2026-10-06T11:00:00",
                "2026-10-06T10:00:00",
                "2026-10-06T09:00:00",
                "2026-10-06T10:00:00",
            ],
        )


class SampleWorkflowTests(unittest.TestCase):
    """通过公开命令复核 README 的样例工作流（独立临时目录）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db = os.path.join(self.tmpdir, "events.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def event_count(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def test_import_then_report_matches_documented_expectation(self):
        # 与 README 完全一致的用法：从项目根目录用相对路径 sample.jsonl，
        # 数据库落在原先不存在的独立临时目录中（父目录可写）。
        self.assertFalse(os.path.exists(self.db))
        result = run_funnel(
            "import", "sample.jsonl", "--db", self.db, cwd=PROJECT_ROOT
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        assert_single_json_line(self, result.stdout, {"imported": 5})
        # 目标库原先不存在，导入成功后被创建。
        self.assertTrue(os.path.isfile(self.db))
        self.assertEqual(self.event_count(), 5)

        report = run_funnel("report", "--db", self.db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(report.stderr, "")
        assert_single_json_line(self, report.stdout, EXPECTED_REPORT)

    def test_conversion_members_u1_only_u2_unsigned_u3_signup_before_visit(self):
        result = run_funnel(
            "import", SAMPLE_PATH, "--db", self.db, cwd=self.tmpdir
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        report = run_funnel("report", "--db", self.db, "--include-users")
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(report.stderr, "")
        payload = json.loads(report.stdout)
        # 汇总数值不变。
        self.assertEqual(
            {k: payload[k] for k in ("visit_users", "converted_users", "conversion_rate")},
            EXPECTED_REPORT,
        )
        # u1 是唯一转化用户；u2 访问但未注册；u3 注册早于访问，
        # 仍计访问而不计转化。
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])

    def test_reimport_appends_five_rows_but_report_dedupes_users(self):
        first = run_funnel("import", SAMPLE_PATH, "--db", self.db, cwd=self.tmpdir)
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(json.loads(first.stdout), {"imported": 5})
        self.assertEqual(self.event_count(), 5)

        # 再次导入同一样例：追加五行（不去重物理记录），仍返回 imported 5。
        second = run_funnel("import", SAMPLE_PATH, "--db", self.db, cwd=self.tmpdir)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertEqual(second.stderr, "")
        assert_single_json_line(self, second.stdout, {"imported": 5})
        self.assertEqual(self.event_count(), 10)

        # 重复事件与重复导入不增加人数：普通报告与转化明细保持不变。
        report = run_funnel("report", "--db", self.db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(report.stderr, "")
        assert_single_json_line(self, report.stdout, EXPECTED_REPORT)

        users = run_funnel("report", "--db", self.db, "--include-users")
        self.assertEqual(users.returncode, 0, msg=users.stderr)
        payload = json.loads(users.stdout)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])

    # -- 错误约定：退出码 2、标准输出为空、标准错误含路径与原因 ----------

    def test_unreadable_input_reports_path_and_reason_without_creating_db(self):
        missing = os.path.join(self.tmpdir, "no-such-input.jsonl")
        ghost_db = os.path.join(self.tmpdir, "ghost.sqlite")
        self.assertFalse(os.path.exists(missing))
        result = run_funnel(
            "import", missing, "--db", ghost_db, cwd=self.tmpdir
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(missing, result.stderr)
        self.assertIn("无法读取输入文件", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # 目标库原本不存在时不创建文件。
        self.assertFalse(os.path.exists(ghost_db))

    def test_inaccessible_db_reports_path_and_reason(self):
        # --db 指向一个已存在的目录时 SQLite 无法打开数据库文件。
        result = run_funnel(
            "import", SAMPLE_PATH, "--db", self.tmpdir, cwd=self.tmpdir
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(self.tmpdir, result.stderr)
        self.assertIn("数据库无法访问", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_report_on_missing_db_reports_path_and_reason(self):
        missing = os.path.join(self.tmpdir, "missing.sqlite")
        result = run_funnel("report", "--db", missing, cwd=self.tmpdir)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(missing, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
