"""非法 UTF-8 字节导致导入失败的回归测试。

固定公开约定（与 JSON/字段错误同一条失败路径）：

- 任一物理行的字节不是合法 UTF-8：退出码 2、标准输出为空、标准错误给出
  输入路径、从 1 开始的首个错误物理行号与 UTF-8 编码无效的原因；
  不输出异常堆栈，不替换字节、不跳过坏行、不尝试其他编码
- 按物理行顺序只报告首个错误；空白行仍占行号；更早行的 JSON/字段错误
  沿用原有原因，不被后续行的编码错误覆盖
- LF 与 CRLF 文件同样处理；末行没有换行符时仍能正确定位
- 失败的导入整批不写入：已有数据库记录内容与数量不变；目标路径原先
  不存在时不创建数据库或附属文件
- 合法 UTF-8（含中文用户编号）仍按原值导入

只通过公开命令 ``python -m funnel import|report`` 观察行为，不直接调用
任何内部函数。仅使用 Python 3 标准库；每个场景使用独立临时目录中的
JSONL 与 SQLite，全部用户编号均为虚构，测试结束不遗留任何文件，
不依赖预存数据、第三方包或网络。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 固定种子数据：seed-user 在 10:00 visit、10:01 signup（间隔 60 秒，转化）。
SEED_EVENTS = [
    {"user_id": "seed-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    {"user_id": "seed-user", "event": "signup", "timestamp": "2026-10-06T10:01:00"},
]

# 失败后数据库应当仍然只剩种子两行（按 user_id, event, timestamp 排序）。
EXPECTED_SEED_ROWS = [
    ("seed-user", "signup", "2026-10-06T10:01:00"),
    ("seed-user", "visit", "2026-10-06T10:00:00"),
]

# 验收样例 damaged.jsonl 的前三条物理行（UTF-8 字节）：
#   第 1 行：new-user 10:00:00 visit（合法）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：new-user 10:00:30 signup（合法）
NEW_USER_VISIT = json.dumps(
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    ensure_ascii=False,
).encode("utf-8")
NEW_USER_SIGNUP = json.dumps(
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    ensure_ascii=False,
).encode("utf-8")

# 第 4 行仅含十六进制字节 FF（非法 UTF-8 起始字节）。
DAMAGED_BYTES = b"\n".join([NEW_USER_VISIT, b"", NEW_USER_SIGNUP, b"\xff"]) + b"\n"

# 末行没有换行符，且最后一个字符是被截断的三字节 UTF-8 序列（"中" 的前两字节）。
TRUNCATED_LAST_LINE_BYTES = b"\n".join(
    [NEW_USER_VISIT, b"", NEW_USER_SIGNUP, b'{"user_id": "\xe4\xb8']
)

# CRLF 文件：第 4 行同样只含字节 FF。
DAMAGED_CRLF_BYTES = b"\r\n".join([NEW_USER_VISIT, b"", NEW_USER_SIGNUP, b"\xff"]) + b"\r\n"

# 第 4 行是非法 JSON，第 5 行才是编码错误：必须报第 4 行的原有原因。
EARLIER_JSON_ERROR_BYTES = b"\n".join(
    [NEW_USER_VISIT, b"", NEW_USER_SIGNUP, b"{", b"\xff"]
) + b"\n"


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


class ImportUtf8Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助函数 ---------------------------------------------------------

    def db_path(self, name="events.sqlite"):
        return os.path.join(self.tmpdir, name)

    def write_jsonl(self, name, events):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def write_bytes(self, name, data):
        """按原始字节写入输入文件（可表达非法 UTF-8 与 CRLF/无末行换行）。"""
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def seed_database(self):
        """先成功导入固定种子数据，返回数据库路径。"""
        db = self.db_path()
        path = self.write_jsonl("seed.jsonl", SEED_EVENTS)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(
            result.returncode,
            0,
            msg="种子导入失败:\nstdout=%r\nstderr=%r" % (result.stdout, result.stderr),
        )
        self.assertEqual(json.loads(result.stdout), {"imported": 2})
        return db

    def snapshot_events(self, db):
        """返回 (逐行记录快照, 记录总数)，用于失败前后逐行比对。"""
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT user_id, event, timestamp FROM events "
                "ORDER BY user_id, event, timestamp"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, count

    def assert_failed_import_is_atomic(self, db, path, line_no, reason_tokens):
        """失败导入的完整公开约定：退出码/输出/行号/原因 + 数据库逐行不变。"""
        before_rows, before_count = self.snapshot_events(db)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

        # 标准错误必须给出输入路径、首个错误物理行号与原因，且无异常堆栈。
        self.assertIn(path, result.stderr)
        self.assertIn("第 %d 行" % line_no, result.stderr)
        for token in reason_tokens:
            self.assertIn(token, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 整批不写入：用户编号、事件类型、时间、记录数量完全一致。
        after_rows, after_count = self.snapshot_events(db)
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_count, before_count)
        return result

    def assert_seed_report_unchanged(self, db):
        """种子用户的两步报告仍为访问 1 人、转化 1 人、比例 1。"""
        result = run_funnel("report", "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["visit_users"], 1)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1)

    # -- 验收样例：第 4 行仅含字节 FF -------------------------------------

    def test_damaged_file_reports_line_4_utf8_error(self):
        db = self.seed_database()
        path = self.write_bytes("damaged.jsonl", DAMAGED_BYTES)

        result = self.assert_failed_import_is_atomic(db, path, 4, ["UTF-8"])

        # 只报告首个错误：不得出现后续行号，也不得报告合法的前三行。
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("第 1 行", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)

        # 失败后表中恰好是种子两行，new-user 的任何合法前置记录都不得留下。
        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_SEED_ROWS)
        self.assertEqual(count, 2)
        self.assertFalse(any(row[0] == "new-user" for row in rows))

        # 原有报告结果不变：访问 1 人、转化 1 人、比例 1。
        self.assert_seed_report_unchanged(db)

    # -- 末行无换行符且多字节字符被截断 ------------------------------------

    def test_truncated_multibyte_on_last_line_without_newline(self):
        db = self.seed_database()
        path = self.write_bytes("truncated.jsonl", TRUNCATED_LAST_LINE_BYTES)

        # 末行（第 4 行）没有换行符，仍须正确定位为第 4 行编码错误。
        self.assert_failed_import_is_atomic(db, path, 4, ["UTF-8"])
        self.assert_seed_report_unchanged(db)

    # -- CRLF 文件同样按物理行定位 -----------------------------------------

    def test_crlf_file_reports_same_line_number(self):
        db = self.seed_database()
        path = self.write_bytes("damaged-crlf.jsonl", DAMAGED_CRLF_BYTES)

        self.assert_failed_import_is_atomic(db, path, 4, ["UTF-8"])
        self.assert_seed_report_unchanged(db)

    # -- 更早行的 JSON 错误不被后续编码错误覆盖 ------------------------------

    def test_earlier_json_error_keeps_its_own_reason(self):
        db = self.seed_database()
        path = self.write_bytes("earlier-json-error.jsonl", EARLIER_JSON_ERROR_BYTES)

        # 第 4 行 "{" 是非法 JSON，第 5 行才是编码错误：
        # 必须沿用第 4 行的原有原因，不得报编码错误。
        result = self.assert_failed_import_is_atomic(db, path, 4, ["非法 JSON"])
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("UTF-8", result.stderr)
        self.assert_seed_report_unchanged(db)

    # -- 目标路径原先不存在：失败导入不创建任何数据库文件 --------------------

    def test_failed_import_does_not_create_database(self):
        db = self.db_path("never-existed.sqlite")
        self.assertFalse(os.path.exists(db))
        path = self.write_bytes("damaged-fresh.jsonl", DAMAGED_BYTES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 数据库主文件不得被创建（journal/wal/shm 等附属文件同理：
        # 临时目录中除输入 JSONL 外不应出现任何其他文件）。
        self.assertFalse(os.path.exists(db))
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)),
            [os.path.basename(path)],
        )

    # -- 合法 UTF-8 中文用户编号仍按原值导入 ---------------------------------

    def test_valid_utf8_chinese_user_id_imported_verbatim(self):
        db = self.seed_database()
        events = [
            {"user_id": "用户甲", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
            {"user_id": "用户甲", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
        ]
        path = self.write_jsonl("chinese.jsonl", events)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 2})

        # 中文用户编号按原值落库，不做任何转义或改写。
        rows, count = self.snapshot_events(db)
        self.assertEqual(count, 4)
        self.assertIn(("用户甲", "visit", "2026-10-06T10:00:00"), rows)
        self.assertIn(("用户甲", "signup", "2026-10-06T10:00:30"), rows)

        # 报告把中文用户计为一个新访问且转化的用户。
        report = run_funnel("report", "--db", db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["visit_users"], 2)
        self.assertEqual(payload["converted_users"], 2)
        self.assertEqual(payload["conversion_rate"], 1)


if __name__ == "__main__":
    unittest.main()
