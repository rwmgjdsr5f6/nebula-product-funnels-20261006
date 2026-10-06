"""user_id 含未配对 Unicode 代理码点导致导入失败的回归测试。

固定公开约定（与非法 JSON/字段错误/非法 UTF-8 字节同一条失败路径）：

- 解析后的 user_id 只要含 U+D800 至 U+DFFF 中的码点（孤立高代理、孤立低
  代理、逆序代理对，或普通字符中夹带的孤立代理）：退出码 2、标准输出为空、
  标准错误给出输入路径、从 1 开始的首个错误物理行号、user_id 与
  “未配对代理码点”原因；不输出异常堆栈
- 按物理行顺序只报告首个错误；空白行仍占行号；合法前缀整批不写入；
  更早行的非法 JSON/字段错误/非法 UTF-8 字节仍沿用原有原因，不被代理
  码点错误覆盖
- LF 与 CRLF 文件同样处理；末行没有换行符时仍能正确定位
- 失败的导入不创建任何数据库文件（目标路径原先不存在时）
- 合法代理对转义 "😀" 与直接写入的 😀 是同一个补充平面编号，
  不得误拒绝，也不得拆成两个用户；中文、大小写、空白用户编号照常按原值
  导入；额外字段即使含此类转义也被忽略，不扩大校验范围

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

# 验收样例 surrogate.jsonl 的前五条物理行（原始字节）：
#   第 1 行：new-user 10:00:00 visit（合法）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：new-user 10:00:30 signup（合法）
#   第 4 行：沿用第 1 行的事件与时间，仅把 user_id 改为 JSON 转义
#             "\ud800"（孤立高代理；JSON 解析后是码点 U+D800）
#   第 5 行：只有 "{"（非法 JSON，但晚于第 4 行，不得被报告）
NEW_USER_VISIT = json.dumps(
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    ensure_ascii=False,
).encode("utf-8")
NEW_USER_SIGNUP = json.dumps(
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    ensure_ascii=False,
).encode("utf-8")
LONE_HIGH_SURROGATE_LINE = (
    b'{"user_id": "\\ud800", "event": "visit", "timestamp": "2026-10-06T10:00:00"}'
)
BARE_BRACE_LINE = b"{"

SURROGATE_BYTES = b"\n".join(
    [
        NEW_USER_VISIT,
        b"",
        NEW_USER_SIGNUP,
        LONE_HIGH_SURROGATE_LINE,
        BARE_BRACE_LINE,
    ]
) + b"\n"

# 末行（第 4 行）没有换行符，仍须定位为第 4 行。
SURROGATE_LAST_LINE_NO_NEWLINE_BYTES = b"\n".join(
    [NEW_USER_VISIT, b"", NEW_USER_SIGNUP, LONE_HIGH_SURROGATE_LINE]
)

# CRLF 文件：第 4 行同样是孤立高代理行。
SURROGATE_CRLF_BYTES = b"\r\n".join(
    [
        NEW_USER_VISIT,
        b"",
        NEW_USER_SIGNUP,
        LONE_HIGH_SURROGATE_LINE,
        BARE_BRACE_LINE,
    ]
) + b"\r\n"

# 第 4 行是非法 JSON，第 5 行才是代理码点错误：必须报第 4 行的原有原因。
EARLIER_JSON_ERROR_BYTES = b"\n".join(
    [
        NEW_USER_VISIT,
        b"",
        NEW_USER_SIGNUP,
        BARE_BRACE_LINE,
        LONE_HIGH_SURROGATE_LINE,
    ]
) + b"\n"

# 第 4 行缺必填字段 event，第 5 行才是代理码点错误：必须报第 4 行字段原因。
EARLIER_FIELD_ERROR_BYTES = b"\n".join(
    [
        NEW_USER_VISIT,
        b"",
        NEW_USER_SIGNUP,
        json.dumps(
            {"user_id": "x9", "timestamp": "2026-10-06T10:00:00"},
            ensure_ascii=False,
        ).encode("utf-8"),
        LONE_HIGH_SURROGATE_LINE,
    ]
) + b"\n"

# 其余孤立代理形态：孤立低代理、逆序代理对、普通字符中夹带孤立高代理。
LONE_LOW_SURROGATE_LINE = (
    b'{"user_id": "\\udc00", "event": "visit", "timestamp": "2026-10-06T10:00:00"}'
)
REVERSED_SURROGATE_LINE = (
    b'{"user_id": "\\udc00\\ud800", "event": "visit",'
    b' "timestamp": "2026-10-06T10:00:00"}'
)
EMBEDDED_SURROGATE_LINE = (
    b'{"user_id": "a\\ud800b", "event": "visit",'
    b' "timestamp": "2026-10-06T10:00:00"}'
)

# 合法 😀（U+1F600）：visit 用合法代理对转义书写，signup 直接写
# UTF-8 字符；两种写法是同一个编号，导入 2 条且报告只有一个用户。
GRIN_PAIR_ESCAPE_LINE = (
    b'{"user_id": "\\ud83d\\ude00", "event": "visit",'
    b' "timestamp": "2026-10-06T10:00:00"}'
)
GRIN_DIRECT_LINE = (
    '{"user_id": "\U0001f600", "event": "signup",'
    ' "timestamp": "2026-10-06T10:00:30"}'
).encode("utf-8")
GRIN_BYTES = b"\n".join([GRIN_PAIR_ESCAPE_LINE, GRIN_DIRECT_LINE]) + b"\n"

# 额外字段 note 的值含孤立代理转义：user_id 合法，额外字段被忽略，必须成功。
EXTRA_FIELD_SURROGATE_LINE = (
    b'{"user_id": "note-user", "event": "visit",'
    b' "timestamp": "2026-10-06T10:00:00",'
    b' "note": "\\ud800"}'
)

# 中文、大小写与空白编号仍按原值接受（与补充平面编号一起入库）。
NORMAL_USER_IDS = ["用户甲", "Alice", "alice", " Lead Space "]


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


class ImportSurrogateTests(unittest.TestCase):
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
        """按原始字节写入输入文件（可精确控制转义写法、CRLF 与末行换行）。"""
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

        # 标准错误必须给出输入路径、错误物理行号、user_id 与原因，且无堆栈。
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

    def assert_surrogate_failure_at_line_4(self, data, filename):
        """在种子库上跑给定输入，断言第 4 行代理码点错误且整批原子失败。"""
        db = self.seed_database()
        path = self.write_bytes(filename, data)

        result = self.assert_failed_import_is_atomic(
            db, path, 4, ["user_id", "未配对代理码点"]
        )

        # 只报告首个错误：不得出现后续行号，也不得报告合法的前三行。
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("第 1 行", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)

        # 失败后表中恰好是种子两行，new-user 的任何合法前置记录都不得留下。
        rows, count = self.snapshot_events(db)
        self.assertEqual(rows, EXPECTED_SEED_ROWS)
        self.assertEqual(count, 2)
        self.assertFalse(any(row[0] == "new-user" for row in rows))

        self.assert_seed_report_unchanged(db)

    # -- 验收样例：第 4 行 user_id 为 "\ud800"，第 5 行是 "{" -------------

    def test_lone_high_surrogate_reports_line_4_atomically(self):
        # 必须报第 4 行的未配对代理码点，而非第 5 行的非法 JSON；
        # 前两条 new-user 事件均不留下。
        self.assert_surrogate_failure_at_line_4(
            SURROGATE_BYTES, "surrogate.jsonl"
        )

    # -- 末行无换行符仍按物理行定位 ---------------------------------------

    def test_surrogate_on_last_line_without_newline(self):
        self.assert_surrogate_failure_at_line_4(
            SURROGATE_LAST_LINE_NO_NEWLINE_BYTES, "surrogate-nonl.jsonl"
        )

    # -- CRLF 文件行号语义一致 --------------------------------------------

    def test_crlf_file_reports_same_line_number(self):
        self.assert_surrogate_failure_at_line_4(
            SURROGATE_CRLF_BYTES, "surrogate-crlf.jsonl"
        )

    # -- 更早行的原有错误原因不被代理码点错误覆盖 ---------------------------

    def test_earlier_invalid_json_keeps_its_own_reason(self):
        db = self.seed_database()
        path = self.write_bytes("earlier-json.jsonl", EARLIER_JSON_ERROR_BYTES)

        # 第 4 行 "{" 是非法 JSON，第 5 行才是代理码点错误。
        result = self.assert_failed_import_is_atomic(
            db, path, 4, ["非法 JSON"]
        )
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("未配对代理码点", result.stderr)
        self.assert_seed_report_unchanged(db)

    def test_earlier_field_error_keeps_its_own_reason(self):
        db = self.seed_database()
        path = self.write_bytes("earlier-field.jsonl", EARLIER_FIELD_ERROR_BYTES)

        # 第 4 行缺 event，第 5 行才是代理码点错误。
        result = self.assert_failed_import_is_atomic(
            db, path, 4, ["event"]
        )
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("未配对代理码点", result.stderr)
        self.assert_seed_report_unchanged(db)

    # -- 其余孤立代理形态 ---------------------------------------------------

    def test_lone_low_surrogate_rejected(self):
        self.assert_surrogate_failure_at_line_4(
            b"\n".join(
                [
                    NEW_USER_VISIT,
                    b"",
                    NEW_USER_SIGNUP,
                    LONE_LOW_SURROGATE_LINE,
                ]
            )
            + b"\n",
            "lone-low.jsonl",
        )

    def test_reversed_surrogate_pair_rejected(self):
        self.assert_surrogate_failure_at_line_4(
            b"\n".join(
                [
                    NEW_USER_VISIT,
                    b"",
                    NEW_USER_SIGNUP,
                    REVERSED_SURROGATE_LINE,
                ]
            )
            + b"\n",
            "reversed.jsonl",
        )

    def test_surrogate_embedded_among_normal_chars_rejected(self):
        self.assert_surrogate_failure_at_line_4(
            b"\n".join(
                [
                    NEW_USER_VISIT,
                    b"",
                    NEW_USER_SIGNUP,
                    EMBEDDED_SURROGATE_LINE,
                ]
            )
            + b"\n",
            "embedded.jsonl",
        )

    # -- 目标路径原先不存在：失败导入不创建任何数据库文件 --------------------

    def test_failed_import_does_not_create_database(self):
        db = self.db_path("never-existed.sqlite")
        self.assertFalse(os.path.exists(db))
        path = self.write_bytes("surrogate-fresh.jsonl", SURROGATE_BYTES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("user_id", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 数据库主文件及 journal/wal/shm 等附属文件都不得出现：
        # 临时目录中除输入 JSONL 外不应有任何其他文件。
        self.assertFalse(os.path.exists(db))
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)),
            [os.path.basename(path)],
        )

    # -- 合法代理对两种写法是同一编号：visit + signup 只算一个用户 -----------

    def test_escaped_and_literal_supplementary_id_are_same_user(self):
        path = self.write_bytes("grin.jsonl", GRIN_BYTES)
        db = self.db_path()

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 2})

        # 两条记录按同一编号落库：表里只有一个不同的 user_id。
        rows, count = self.snapshot_events(db)
        self.assertEqual(count, 2)
        self.assertEqual({row[0] for row in rows}, {"\U0001f600"})
        self.assertIn(
            ("\U0001f600", "visit", "2026-10-06T10:00:00"), rows
        )
        self.assertIn(
            ("\U0001f600", "signup", "2026-10-06T10:00:30"), rows
        )

        # 报告：访问 1 人、转化 1 人、比例 1；用户明细只有一个编号。
        report = run_funnel("report", "--db", db, "--include-users")
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["visit_users"], 1)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1)
        self.assertEqual(payload["visit_user_ids"], ["\U0001f600"])
        self.assertEqual(payload["converted_user_ids"], ["\U0001f600"])

    # -- 额外字段中的孤立代理转义被忽略，不扩大校验范围 -----------------------

    def test_lone_surrogate_in_extra_field_is_ignored(self):
        db = self.seed_database()
        path = self.write_bytes(
            "extra-field.jsonl", EXTRA_FIELD_SURROGATE_LINE + b"\n"
        )

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 1})

        rows, count = self.snapshot_events(db)
        self.assertEqual(count, 3)
        self.assertIn(
            ("note-user", "visit", "2026-10-06T10:00:00"), rows
        )

    # -- 中文、大小写、空白与补充平面编号仍按原值区分用户 ---------------------

    def test_normal_user_ids_still_imported_verbatim(self):
        events = [
            {
                "user_id": user_id,
                "event": "visit",
                "timestamp": "2026-10-06T10:00:00",
            }
            for user_id in NORMAL_USER_IDS
        ]
        path = self.write_jsonl("normal.jsonl", events)
        db = self.db_path()

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout), {"imported": 4})

        # 四个编号按原值互不合并：Alice 与 alice 是两个用户，
        # 首尾空白保留，中文编号原样落库。
        rows, _ = self.snapshot_events(db)
        self.assertEqual({row[0] for row in rows}, set(NORMAL_USER_IDS))

        report = run_funnel("report", "--db", db, "--include-users")
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["visit_users"], 4)
        # 按 Unicode 码点字典序排列。
        self.assertEqual(payload["visit_user_ids"], sorted(NORMAL_USER_IDS))


if __name__ == "__main__":
    unittest.main()
