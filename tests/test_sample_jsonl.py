"""仓库自带 sample.jsonl 离线演示样例的回归测试。

固定公开约定（与 README「样例」一节一致）：

- 样例文件为无 BOM 的 UTF-8，包含五行 JSON 事件并以换行结束，
  字段沿用既有格式（user_id / event / timestamp）
- 五条事件均发生在 2026-10-06，按序为：u1 10:00 visit、u1 11:00 signup、
  u2 10:00 visit、u3 09:00 signup、u3 10:00 visit
- 首次导入：退出码 0，标准输出仅 ``{"imported": 5}``，标准错误为空
- 普通报告：退出码 0，标准输出仅
  ``{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}``，
  标准错误为空；u1 是唯一转化用户，u2 未注册，u3 的注册早于访问，
  仍计访问而不计转化
- 再次导入同一文件仍返回 ``{"imported": 5}``，库中增加到十条记录，
  普通报告不变（追加与用户去重语义保留）
- 输入无法读取或数据库无法访问时：退出码 2、标准输出为空、
  标准错误包含相应路径与原因

测试直接读取仓库根目录交付的 sample.jsonl，不另造同内容输入；
只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部函数。仅使用 Python 3 标准库；数据库在独立临时目录中创建，
测试结束不遗留任何文件，不依赖预存数据、第三方包或网络。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_PATH = os.path.join(PROJECT_ROOT, "sample.jsonl")

# 样例的固定内容：五条记录按文件中的物理行顺序排列，逐字段核对原值。
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


def run_funnel(*cli_args, cwd):
    """在指定目录执行 python -m funnel，返回完成的进程结果。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "funnel", *cli_args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class SampleFileTests(unittest.TestCase):
    """直接核对仓库交付的 sample.jsonl 本身的字节与记录内容。"""

    def test_sample_file_bytes_and_records(self):
        self.assertTrue(
            os.path.isfile(SAMPLE_PATH),
            msg="仓库根目录缺少交付的 sample.jsonl: %s" % SAMPLE_PATH,
        )
        with open(SAMPLE_PATH, "rb") as fh:
            raw = fh.read()

        # 无 BOM 的 UTF-8，且整个文件可以严格按 UTF-8 解码。
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), msg="sample.jsonl 不得带 BOM")
        text = raw.decode("utf-8")

        # 恰好五个非空行，文件以一个换行结束。
        self.assertTrue(text.endswith("\n"), msg="sample.jsonl 必须以换行结束")
        self.assertNotIn("\r", text, msg="sample.jsonl 应使用 LF 换行")
        lines = text.splitlines()
        self.assertEqual(len(lines), 5)

        # 逐行解析为 JSON 对象，字段与原值逐条核对（含物理行顺序）。
        records = [json.loads(line) for line in lines]
        self.assertEqual(records, EXPECTED_EVENTS)
        for record in records:
            self.assertEqual(set(record), {"user_id", "event", "timestamp"})


class SampleWorkflowTests(unittest.TestCase):
    """在独立临时目录中通过公开入口复核样例的导入与报告预期。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db = os.path.join(self.tmpdir, "events.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def import_sample(self):
        return run_funnel("import", SAMPLE_PATH, "--db", self.db, cwd=self.tmpdir)

    def report(self, *extra_args):
        return run_funnel("report", "--db", self.db, *extra_args, cwd=self.tmpdir)

    def event_rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    def assert_single_json_line(self, result, expected):
        """退出码 0、标准错误为空、标准输出是单行 JSON 且解析后等于预期。"""
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertNotIn("\n", result.stdout.rstrip("\n"))
        self.assertEqual(json.loads(result.stdout), expected)

    def test_import_then_report_matches_documented_expectations(self):
        self.assertFalse(os.path.exists(self.db))

        # 首次导入：目标库原先不存在、父目录可写，导入成功。
        self.assert_single_json_line(self.import_sample(), {"imported": 5})
        self.assertTrue(os.path.isfile(self.db))

        # 普通报告：与 README 公开预期完全一致。
        self.assert_single_json_line(self.report(), EXPECTED_REPORT)

        # 成员口径：u1 是唯一转化用户；u2 只访问未注册；u3 注册早于访问，
        # 仍计入访问但不计入转化。
        detail = self.report("--include-users")
        self.assertEqual(detail.returncode, 0, msg=detail.stderr)
        self.assertEqual(detail.stderr, "")
        payload = json.loads(detail.stdout)
        self.assertEqual(payload["visit_user_ids"], ["u1", "u2", "u3"])
        self.assertEqual(payload["converted_user_ids"], ["u1"])
        self.assertEqual(payload["visit_users"], 3)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 0.3333333333333333)

    def test_reimport_appends_and_report_stays_unchanged(self):
        self.assert_single_json_line(self.import_sample(), {"imported": 5})
        self.assertEqual(len(self.event_rows()), 5)

        # 再次导入同一文件：仍返回 imported 5，库中增加到十条记录。
        self.assert_single_json_line(self.import_sample(), {"imported": 5})
        rows = self.event_rows()
        self.assertEqual(len(rows), 10)

        # 普通报告不变：重复事件与重复导入不增加人数（追加 + 用户去重）。
        self.assert_single_json_line(self.report(), EXPECTED_REPORT)

    def test_unreadable_input_fails_with_exit_2(self):
        missing = os.path.join(self.tmpdir, "no-such-input.jsonl")
        result = run_funnel("import", missing, "--db", self.db, cwd=self.tmpdir)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(missing, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # 输入无法读取时不创建数据库文件。
        self.assertFalse(os.path.exists(self.db))

    def test_inaccessible_database_fails_with_exit_2(self):
        # 父目录不存在：数据库无法访问。
        db = os.path.join(self.tmpdir, "no-such-dir", "events.sqlite")
        result = run_funnel("import", SAMPLE_PATH, "--db", db, cwd=self.tmpdir)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 报告不存在的数据库同样退出码 2、标准输出为空、标准错误含路径。
        result = run_funnel("report", "--db", db, cwd=self.tmpdir)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
