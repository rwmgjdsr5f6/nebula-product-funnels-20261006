"""导入失败原子性的回归测试。

固定公开约定（见 README）：

- 任一行非法则整次导入失败：退出码 2、标准输出为空，标准错误给出
  首个错误行的输入路径、物理行号与原因，不输出异常堆栈；
- 失败导入不写入任何记录，数据库已有记录（含用户编号、事件类型、时间、
  记录数量）完全不变；
- 目标数据库路径原先不存在时，失败导入不创建数据库文件；
- 空白行不导入，但仍参与物理行号计算。

只通过公开入口观察结果：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite>

不调用内部函数、不固定整段错误文案或临时路径；每个场景使用独立临时目录
中的合成 JSONL 与虚构用户编号，仅依赖 Python 3 标准库，结束后自行清理。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 固定种子数据：seed-user 先 visit 后 signup，两步报告应为 1 人访问、1 人转化。
SEED_EVENTS = [
    {"user_id": "seed-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "seed-user", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
]

# 失败文件前三行在两个场景中相同：
#   第 1 行：new-user 10:00:00 visit（合法）
#   第 2 行：空白（不导入，但物理行号照常计数）
#   第 3 行：new-user 10:00:30 signup（合法）
# 因此首个错误若报“第 4 行”，即证明空白行参与了物理行号计算
# （若跳过空白行，同一错误会被报成第 3 行）。
PREFIX_LINES = [
    json.dumps(
        {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
        ensure_ascii=False,
    ),
    "",
    json.dumps(
        {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
        ensure_ascii=False,
    ),
]

# 场景一：第 4 行是单独的 “{”（非法 JSON），第 5 行是缺少必填字段的对象。
BAD_JSON_LINES = PREFIX_LINES + [
    "{",
    json.dumps(
        {"user_id": "new-user", "event": "visit"},  # 缺少 timestamp
        ensure_ascii=False,
    ),
]

# 场景二：第 4 行 event、timestamp 合法但缺少 user_id，第 5 行才是非法 JSON。
MISSING_USER_ID_LINES = PREFIX_LINES + [
    json.dumps(
        {"event": "visit", "timestamp": "2026-10-06T10:01:00"},
        ensure_ascii=False,
    ),
    "{bad-json",
]

# 失败后数据库应当只剩种子记录（按 user_id, event, timestamp 排序）。
EXPECTED_SEED_ROWS = [
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
]


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


class ImportAtomicityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助函数 ---------------------------------------------------------

    def db_path(self, name="events.sqlite"):
        return os.path.join(self.tmpdir, name)

    def write_raw_jsonl(self, name, physical_lines):
        """按给定物理行写入 JSONL（允许空字符串表示空白行）。"""
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(physical_lines) + "\n")
        return path

    def write_seed_jsonl(self, name="seed.jsonl"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for event in SEED_EVENTS:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def seed_database(self, db=None):
        """先成功导入固定种子数据，确认 imported 为 2，返回数据库路径。"""
        if db is None:
            db = self.db_path()
        path = self.write_seed_jsonl()
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="种子导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {"imported": 2})
        return db

    def snapshot_events(self, db):
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()

    def assert_failed_import_contract(self, result, input_path, reason_fragment):
        """失败导入的公开约定：退出 2、标准输出为空、标准错误定位首个错误行。"""
        self.assertEqual(
            result.returncode,
            2,
            msg="预期退出码 2:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(result.stdout, "")
        # 标准错误包含输入路径、第 4 行与该场景的原因片段。
        self.assertIn(input_path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn(reason_fragment, result.stderr)
        # 必须停在首个错误行，不能转而报告第 5 行。
        self.assertNotIn("第 5 行", result.stderr)
        # 不允许把异常堆栈直接抛给用户。
        self.assertNotIn("Traceback", result.stderr)

    def assert_db_unchanged_after_failure(self, db, before):
        """失败前后逐行比较事件记录：用户编号、事件类型、时间、数量完全一致。"""
        after = self.snapshot_events(db)
        self.assertEqual(after, before)
        self.assertEqual(after, EXPECTED_SEED_ROWS)
        self.assertEqual(len(after), 2)
        # new-user 的两条合法前置记录均不得留下。
        with sqlite3.connect(db) as conn:
            new_rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events WHERE user_id = ?",
                ("new-user",),
            ).fetchall()
        self.assertEqual(new_rows, [])

    def assert_seed_report_still_one_to_one(self, db):
        """已有 seed-user 的两步报告仍为访问 1 人、转化 1 人、比例 1。"""
        result = run_funnel("report", "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["visit_users"], 1)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1)

    # -- 场景一：第 4 行非法 JSON，第 5 行缺字段 ---------------------------

    def test_invalid_json_line_aborts_whole_batch(self):
        db = self.seed_database()
        before = self.snapshot_events(db)
        path = self.write_raw_jsonl("bad_json.jsonl", BAD_JSON_LINES)

        result = run_funnel("import", path, "--db", db)

        self.assert_failed_import_contract(result, path, "非法 JSON")
        self.assert_db_unchanged_after_failure(db, before)
        self.assert_seed_report_still_one_to_one(db)

    # -- 场景二：第 4 行缺 user_id，第 5 行非法 JSON -----------------------

    def test_missing_user_id_line_reported_before_later_bad_json(self):
        db = self.seed_database()
        before = self.snapshot_events(db)
        path = self.write_raw_jsonl("missing_user_id.jsonl", MISSING_USER_ID_LINES)

        result = run_funnel("import", path, "--db", db)

        self.assert_failed_import_contract(result, path, "user_id")
        self.assert_db_unchanged_after_failure(db, before)
        self.assert_seed_report_still_one_to_one(db)

    # -- 目标数据库不存在：不创建任何文件 ---------------------------------

    def test_failed_import_does_not_create_missing_database(self):
        db = self.db_path("never_created.sqlite")
        self.assertFalse(os.path.exists(db))
        path = self.write_raw_jsonl("bad_json_fresh.jsonl", BAD_JSON_LINES)

        result = run_funnel("import", path, "--db", db)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("非法 JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(
            os.path.exists(db),
            msg="失败导入不应在 %s 创建数据库文件" % db,
        )


if __name__ == "__main__":
    unittest.main()
