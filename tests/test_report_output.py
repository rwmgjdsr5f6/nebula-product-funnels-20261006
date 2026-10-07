"""report --output 报告文件保存的回归测试。

只通过公开命令验证行为：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--output <report.json>]

仅使用 Python 3 标准库；每个场景使用独立临时目录中的 JSONL、SQLite 与
输出文件，不依赖预存数据库、网络或第三方包，测试结束不遗留任何文件。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验收样例：与 README/sample.jsonl 一致的 2026-10-06 五条虚构事件。
# u1：10:00 visit，11:00 signup（转化）
# u2：仅 10:00 visit
# u3：09:00 signup，10:00 visit（signup 早于 visit，不转化）
ACCEPTANCE_EVENTS = [
    {"user_id": "u1", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u1", "event": "signup", "timestamp": "2026-10-06T11:00:00"},
    {"user_id": "u2", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "u3", "event": "signup", "timestamp": "2026-10-06T09:00:00"},
    {"user_id": "u3", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
]

EXPECTED_METRICS = {
    "visit_users": 3,
    "converted_users": 1,
    "conversion_rate": 0.3333333333333333,
}


def run_funnel(*cli_args, cwd=PROJECT_ROOT):
    """执行 python -m funnel，返回完成的进程结果。"""
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


class ReportOutputTests(unittest.TestCase):
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

    def output_path(self, name="report.json"):
        return os.path.join(self.tmpdir, name)

    def import_events(self, events, db=None):
        if db is None:
            db = self.db_path()
        path = self.write_jsonl("events.jsonl", events)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        return db

    def report(self, db, *extra):
        return run_funnel("report", "--db", db, *extra)

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    def assert_save_failed(self, result, output):
        """保存类失败：退出码 2、标准输出为空、标准错误含输出路径且无堆栈。"""
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(output, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- 成功保存 ---------------------------------------------------------

    def test_acceptance_save_matches_stdout(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        output = self.output_path()
        result = self.report(db, "--output", output)
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        # 标准输出仍只是原来的单行报告 JSON。
        stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(stdout_lines), 1)
        self.assertEqual(json.loads(stdout_lines[0]), EXPECTED_METRICS)
        # 文件字节与标准输出完全一致：同一行 JSON 加一个换行，UTF-8 编码。
        with open(output, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw.decode("utf-8"), result.stdout)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        # 解析后的内容与标准输出一致，不增加导出时间、路径或其他字段。
        self.assertEqual(json.loads(raw.decode("utf-8")), EXPECTED_METRICS)

    def test_save_with_all_detail_switches(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        output = self.output_path()
        result = self.report(
            db,
            "--within-seconds",
            "3600",
            "--include-users",
            "--include-pairs",
            "--include-latency",
            "--group-by",
            "visit-date",
            "--include-group-pairs",
            "--include-group-latency",
            "--output",
            output,
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        with open(output, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        printed = json.loads(result.stdout)
        self.assertEqual(saved, printed)
        # 各明细开关仍独立控制原有字段：保存的字段集合与打印完全一致。
        self.assertEqual(
            set(saved),
            {
                "visit_users",
                "converted_users",
                "conversion_rate",
                "visit_user_ids",
                "converted_user_ids",
                "conversion_pairs",
                "conversion_latency",
                "visit_date_groups",
            },
        )

    def test_save_empty_report(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        output = self.output_path()
        # 时段内无任何访问：零值报告也可以保存。
        result = self.report(
            db,
            "--visit-from",
            "2030-01-01T00:00:00",
            "--visit-before",
            "2030-01-02T00:00:00",
            "--include-users",
            "--include-pairs",
            "--include-latency",
            "--group-by",
            "visit-date",
            "--output",
            output,
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        with open(output, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved, json.loads(result.stdout))
        self.assertEqual(saved["visit_users"], 0)
        self.assertEqual(saved["converted_users"], 0)
        self.assertEqual(saved["conversion_rate"], 0)
        self.assertEqual(saved["visit_user_ids"], [])
        self.assertEqual(saved["converted_user_ids"], [])
        self.assertEqual(saved["conversion_pairs"], [])
        self.assertEqual(
            saved["conversion_latency"],
            {"min_seconds": None, "max_seconds": None, "mean_seconds": None},
        )
        self.assertEqual(saved["visit_date_groups"], [])

    def test_overwrite_existing_file_without_appending(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        output = self.output_path()
        with open(output, "w", encoding="utf-8") as fh:
            fh.write("旧内容\n旧内容第二行\n")
        first = self.report(db, "--output", output)
        self.assertEqual(first.returncode, 0, msg="stderr=%r" % first.stderr)
        with open(output, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, first.stdout)
        # 再次保存是整体覆盖，不追加多个报告。
        second = self.report(db, "--within-seconds", "59", "--output", output)
        self.assertEqual(second.returncode, 0, msg="stderr=%r" % second.stderr)
        with open(output, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, second.stdout)
        self.assertEqual(json.loads(text)["converted_users"], 0)
        self.assertNotIn("旧内容", text)
        self.assertEqual(len([l for l in text.splitlines() if l.strip()]), 1)

    def test_relative_output_path_resolves_against_cwd(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = run_funnel(
            "report", "--db", db, "--output", "relative-report.json", cwd=self.tmpdir
        )
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        output = os.path.join(self.tmpdir, "relative-report.json")
        with open(output, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), EXPECTED_METRICS)

    def test_save_keeps_events_unchanged(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        before = self.snapshot_events(db)
        output = self.output_path()
        self.assertEqual(self.report(db, "--output", output).returncode, 0)
        self.assertEqual(self.snapshot_events(db), before)

    def test_no_output_keeps_existing_behavior(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db)
        self.assertEqual(result.returncode, 0, msg="stderr=%r" % result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), EXPECTED_METRICS)
        # 未指定 --output 时不产生任何报告文件。
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)), ["events.jsonl", "events.sqlite"]
        )

    # -- 参数形态错误：先于数据库访问 --------------------------------------

    def test_missing_value_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        result = self.report(db, "--output")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--output", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_value_rejected_before_db_access(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--output", "")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--output", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # 拒绝发生在数据库访问之前：不存在的数据库不会被顺手创建。
        self.assertFalse(os.path.exists(db))

    def test_output_same_as_db_rejected(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        before = self.snapshot_events(db)
        variants = [
            db,
            os.path.join(self.tmpdir, ".", "events.sqlite"),
            os.path.join(self.tmpdir, "sub", "..", "events.sqlite"),
        ]
        os.mkdir(os.path.join(self.tmpdir, "sub"))
        for output in variants:
            with self.subTest(output=output):
                result = self.report(db, "--output", output)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误同时指出两个参数。
                self.assertIn("--output", result.stderr)
                self.assertIn("--db", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        # 数据库记录与文件内容均未改变。
        self.assertEqual(self.snapshot_events(db), before)

    def test_output_same_as_db_rejected_before_db_access(self):
        db = self.db_path("missing.sqlite")
        self.assertFalse(os.path.exists(db))
        result = self.report(db, "--output", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--output", result.stderr)
        self.assertIn("--db", result.stderr)
        self.assertFalse(os.path.exists(db))

    # -- 文件系统失败：退出码 2 且不留下副作用 ------------------------------

    def test_parent_directory_missing(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        before = self.snapshot_events(db)
        output = os.path.join(self.tmpdir, "no-such-dir", "report.json")
        result = self.report(db, "--output", output)
        self.assert_save_failed(result, output)
        self.assertFalse(os.path.exists(output))
        self.assertFalse(os.path.exists(os.path.dirname(output)))
        self.assertEqual(self.snapshot_events(db), before)

    def test_output_target_is_directory(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        before = self.snapshot_events(db)
        output = os.path.join(self.tmpdir, "a-directory")
        os.mkdir(output)
        result = self.report(db, "--output", output)
        self.assert_save_failed(result, output)
        self.assertTrue(os.path.isdir(output))
        self.assertEqual(os.listdir(output), [])
        self.assertEqual(self.snapshot_events(db), before)

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限位约束")
    def test_unwritable_target_keeps_existing_content(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        before = self.snapshot_events(db)
        outdir = os.path.join(self.tmpdir, "readonly")
        os.mkdir(outdir)
        output = os.path.join(outdir, "report.json")
        with open(output, "w", encoding="utf-8") as fh:
            fh.write("原有内容\n")
        os.chmod(outdir, 0o555)
        try:
            result = self.report(db, "--output", output)
        finally:
            os.chmod(outdir, 0o755)
        self.assert_save_failed(result, output)
        # 已有目标保留原内容，目录里不留临时文件。
        with open(output, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "原有内容\n")
        self.assertEqual(os.listdir(outdir), ["report.json"])
        self.assertEqual(self.snapshot_events(db), before)

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限位约束")
    def test_unwritable_parent_leaves_no_partial_file(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        outdir = os.path.join(self.tmpdir, "readonly")
        os.mkdir(outdir)
        output = os.path.join(outdir, "new-report.json")
        os.chmod(outdir, 0o555)
        try:
            result = self.report(db, "--output", output)
        finally:
            os.chmod(outdir, 0o755)
        self.assert_save_failed(result, output)
        # 原本不存在的目标不留下不完整文件，也没有临时文件残留。
        self.assertEqual(os.listdir(outdir), [])

    # -- 参数校验或查询失败时不创建输出文件 ----------------------------------

    def test_invalid_params_do_not_create_output(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        output = self.output_path()
        cases = [
            ("--within-seconds", "0"),
            ("--visit-from", "2026-10-06T10:00:00"),  # 缺少配对参数
            ("--include-group-pairs",),  # 缺少 --group-by visit-date
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = self.report(db, *extra, "--output", output)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse(os.path.exists(output))

    def test_invalid_params_do_not_modify_existing_output(self):
        db = self.import_events(ACCEPTANCE_EVENTS)
        output = self.output_path()
        with open(output, "w", encoding="utf-8") as fh:
            fh.write("原有内容\n")
        result = self.report(db, "--within-seconds", "abc", "--output", output)
        self.assertEqual(result.returncode, 2)
        with open(output, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "原有内容\n")

    def test_missing_db_follows_path_protocol_and_creates_no_output(self):
        db = self.db_path("missing.sqlite")
        output = self.output_path()
        result = self.report(db, "--output", output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(db, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(db))
        self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
