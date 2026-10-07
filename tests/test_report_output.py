"""report --output 保存报告的回归测试。

固定 README 已公开的 --output 规则：

- 成功：退出码 0，标准输出仍只有单行报告 JSON，标准错误为空；
  文件为 UTF-8、单个完整 JSON 对象并以一个 LF 结束，解析后与标准
  输出完全一致，不增加导出时间、路径或其他字段
- 目标不存在则创建；已存在普通文件整体覆盖，不追加多个报告
- 相对路径按当前工作目录解释；父目录须事先存在
- 缺少取值或取空字符串：退出码 2、标准输出为空、标准错误指出
  --output 及原因，且先于数据库访问（不创建缺失的数据库）
- 输出路径与 --db 规范化后为同一绝对路径：提前拒绝，标准错误
  同时指出两个参数
- 父目录不存在、目标是目录、目标无法写入：退出码 2、标准输出
  为空、标准错误包含输出路径及原因
- 参数校验或报告查询失败时不创建或改写输出文件；保存失败时已有
  目标保留原内容，原本不存在的目标不留下不完整文件
- 失败均不改变数据库事件记录；import 命令不接受 --output

只通过公开命令 ``python -m funnel import|report`` 观察行为。仅使用
Python 3 标准库；每个场景使用独立临时目录，全部用户编号均为虚构，
测试结束不遗留任何文件。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE_EVENTS = [
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


class ReportOutputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db = os.path.join(self.tmpdir, "events.sqlite")
        jsonl = os.path.join(self.tmpdir, "events.jsonl")
        with open(jsonl, "w", encoding="utf-8") as fh:
            for event in SAMPLE_EVENTS:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        result = run_funnel("import", jsonl, "--db", self.db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, *extra, cwd=None):
        return run_funnel("report", "--db", self.db, *extra, cwd=cwd)

    def read_output(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def assert_empty_stdout_param_error(self, result, *tokens):
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        for token in tokens:
            self.assertIn(token, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- 成功保存 ---------------------------------------------------------

    def test_output_file_equals_stdout_and_ends_with_single_lf(self):
        out_path = os.path.join(self.tmpdir, "report.json")
        result = self.report("--output", out_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        # 标准输出仍是单行报告 JSON。
        self.assertEqual(result.stdout.splitlines(), [json.dumps(EXPECTED_REPORT)])
        raw = self.read_output(out_path)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertEqual(json.loads(raw.decode("utf-8")), EXPECTED_REPORT)
        # 文件内容 = 标准输出那一行 + 一个 LF；解析后与标准输出完全一致。
        self.assertEqual(raw, result.stdout.encode("utf-8"))

    def test_output_creates_missing_file_in_existing_parent(self):
        nested = os.path.join(self.tmpdir, "out")
        os.mkdir(nested)
        out_path = os.path.join(nested, "deep", "report.json")
        # 父目录不由程序创建：缺失父目录先拒绝。
        missing_parent = os.path.join(nested, "deep")
        result = self.report("--output", out_path)
        self.assert_empty_stdout_param_error(result, out_path, "父目录不存在")
        self.assertFalse(os.path.exists(out_path))
        self.assertFalse(os.path.isdir(missing_parent))
        # 父目录存在后正常创建文件。
        os.mkdir(missing_parent)
        result = self.report("--output", out_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(self.read_output(out_path)), EXPECTED_REPORT)

    def test_output_overwrites_existing_file_without_appending(self):
        out_path = os.path.join(self.tmpdir, "report.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("PREVIOUS CONTENT LONGER THAN THE NEW REPORT\n")
        result = self.report("--output", out_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        raw = self.read_output(out_path)
        self.assertNotIn(b"PREVIOUS", raw)
        self.assertEqual(raw.count(b"visit_users"), 1)
        self.assertEqual(json.loads(raw), EXPECTED_REPORT)

    def test_relative_path_resolved_from_cwd(self):
        result = run_funnel(
            "report",
            "--db",
            self.db,
            "--output",
            "rel-report.json",
            cwd=self.tmpdir,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        out_path = os.path.join(self.tmpdir, "rel-report.json")
        self.assertEqual(
            json.loads(self.read_output(out_path)),
            json.loads(result.stdout),
        )

    def test_output_with_all_detail_flags_matches_stdout(self):
        out_path = os.path.join(self.tmpdir, "full.json")
        result = self.report(
            "--within-seconds",
            "7200",
            "--visit-from",
            "2026-10-06T00:00:00",
            "--visit-before",
            "2026-10-07T00:00:00",
            "--include-users",
            "--include-pairs",
            "--include-latency",
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-group-latency",
            "--output",
            out_path,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            json.loads(self.read_output(out_path)), json.loads(result.stdout)
        )
        self.assertEqual(result.stderr, "")

    def test_empty_report_is_saved_with_zero_rules(self):
        empty_db = os.path.join(self.tmpdir, "empty.sqlite")
        empty_jsonl = os.path.join(self.tmpdir, "empty.jsonl")
        open(empty_jsonl, "w", encoding="utf-8").close()
        imp = run_funnel("import", empty_jsonl, "--db", empty_db)
        self.assertEqual(imp.returncode, 0, msg=imp.stderr)
        out_path = os.path.join(self.tmpdir, "empty-report.json")
        result = run_funnel(
            "report", "--db", empty_db, "--output", out_path
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # 零访问：整数 0（沿用现有规则）。
        self.assertEqual(
            json.loads(self.read_output(out_path)),
            {"visit_users": 0, "converted_users": 0, "conversion_rate": 0},
        )

    # -- 参数错误：先于数据库访问 -----------------------------------------

    def test_missing_value_rejected_before_db_access(self):
        ghost_db = os.path.join(self.tmpdir, "ghost.sqlite")
        result = run_funnel("report", "--db", ghost_db, "--output")
        self.assert_empty_stdout_param_error(result, "--output")
        self.assertFalse(os.path.exists(ghost_db))

    def test_empty_string_value_rejected_before_db_access(self):
        ghost_db = os.path.join(self.tmpdir, "ghost.sqlite")
        result = run_funnel("report", "--db", ghost_db, "--output", "")
        self.assert_empty_stdout_param_error(result, "--output")
        self.assertFalse(os.path.exists(ghost_db))

    def test_output_same_path_as_db_rejected_with_both_params(self):
        result = self.report("--output", self.db)
        self.assert_empty_stdout_param_error(result, "--output", "--db")

    def test_output_same_path_as_db_different_spellings(self):
        result = run_funnel(
            "report",
            "--db",
            os.path.join(self.tmpdir, ".", "events.sqlite"),
            "--output",
            self.db,
        )
        self.assert_empty_stdout_param_error(result, "--output", "--db")

    def test_output_same_path_rejected_before_missing_db_is_created(self):
        ghost = os.path.join(self.tmpdir, "ghost.sqlite")
        result = run_funnel("report", "--db", ghost, "--output", ghost)
        self.assert_empty_stdout_param_error(result, "--output", "--db")
        self.assertFalse(os.path.exists(ghost))

    # -- 路径类失败 -------------------------------------------------------

    def test_missing_parent_reports_path_and_reason(self):
        out_path = os.path.join(self.tmpdir, "no-such-dir", "r.json")
        result = self.report("--output", out_path)
        self.assert_empty_stdout_param_error(result, out_path)
        self.assertFalse(os.path.exists(out_path))

    def test_target_is_directory_rejected(self):
        target = os.path.join(self.tmpdir, "adir")
        os.mkdir(target)
        result = self.report("--output", target)
        self.assert_empty_stdout_param_error(result, target)

    def test_unwritable_target_preserves_existing_content(self):
        ro_dir = os.path.join(self.tmpdir, "ro")
        os.mkdir(ro_dir)
        target = os.path.join(ro_dir, "r.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL")
        os.chmod(ro_dir, 0o555)
        try:
            result = self.report("--output", target)
        finally:
            os.chmod(ro_dir, 0o755)
        self.assert_empty_stdout_param_error(result, target)
        self.assertEqual(self.read_output(target), b"ORIGINAL")

    def test_unwritable_missing_target_leaves_no_incomplete_file(self):
        ro_dir = os.path.join(self.tmpdir, "ro2")
        os.mkdir(ro_dir)
        target = os.path.join(ro_dir, "new.json")
        os.chmod(ro_dir, 0o555)
        try:
            result = self.report("--output", target)
        finally:
            os.chmod(ro_dir, 0o755)
        self.assert_empty_stdout_param_error(result, target)
        # 目标本身与临时文件都不得残留。
        self.assertEqual(sorted(os.listdir(ro_dir)), [])

    # -- 其他失败不改文件、不改数据库 -------------------------------------

    def test_param_validation_failure_does_not_touch_existing_file(self):
        out_path = os.path.join(self.tmpdir, "report.json")
        self.assertEqual(self.report("--output", out_path).returncode, 0)
        before = self.read_output(out_path)
        result = self.report(
            "--output", out_path,
            "--visit-from", "2026-10-06T10:00:00",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read_output(out_path), before)

    def test_query_failure_does_not_create_output(self):
        corrupt = os.path.join(self.tmpdir, "corrupt.sqlite")
        with open(corrupt, "wb") as fh:
            fh.write(b"not a sqlite database\n")
        out_path = os.path.join(self.tmpdir, "never.json")
        result = run_funnel("report", "--db", corrupt, "--output", out_path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(corrupt, result.stderr)
        self.assertFalse(os.path.exists(out_path))

    def test_failures_keep_events_unchanged(self):
        def count():
            with sqlite3.connect(self.db) as conn:
                return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        before = count()
        cases = [
            ("--output", self.db),  # 与 --db 同路径
            ("--output", os.path.join(self.tmpdir, "missing-dir", "r.json")),
            ("--output", os.path.join(self.tmpdir, "adir")),
        ]
        os.mkdir(os.path.join(self.tmpdir, "adir"))
        for extra in cases:
            result = self.report(*extra)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertEqual(count(), before)

    # -- 未指定 --output 与 import 保持现状 -------------------------------

    def test_without_output_behavior_unchanged(self):
        result = self.report()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, json.dumps(EXPECTED_REPORT) + "\n")

    def test_import_does_not_accept_output(self):
        jsonl = os.path.join(self.tmpdir, "events.jsonl")
        result = run_funnel(
            "import", jsonl, "--db", self.db, "--output",
            os.path.join(self.tmpdir, "x.json"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "x.json")))


if __name__ == "__main__":
    unittest.main()
