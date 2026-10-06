"""user_id 含未配对 Unicode 代理码点时导入失败的回归测试。

背景：输入文件的物理字节已是合法 UTF-8（因此不触发严格 UTF-8 解码
错误），但 JSON 字符串转义（如 ``"\\ud800"``）经 ``json`` 解析后会留下
U+D800 至 U+DFFF 的孤立代理码点。固定公开约定（与非法 JSON、字段错误、
非法 UTF-8 字节同一条失败路径）：

- 解析后的 user_id 只要含 U+D800 至 U+DFFF 中任一码点：退出码 2、
  标准输出为空、标准错误给出输入路径、从 1 开始的首个错误物理行号、
  user_id（以可安全输出的 \\uXXXX 转义形式）与“未配对代理码点”原因；
  不输出异常堆栈
- 按物理行顺序只报告首个错误；空白行仍占行号；合法前缀整批不留存；
  更早行的非法 JSON、字段错误或非法 UTF-8 字节保留原有原因；LF 与
  CRLF 同样处理，末行没有换行符时仍能正确定位
- 目标数据库路径原先不存在时，失败的导入不创建数据库或附属文件
- 合法代理对转义（``\\ud83d\\ude00``）与直接写入的补充平面字符（😀）
  表示同一 user_id：二者各出现一次时 imported=2，报告访问/转化均为
  1 人，用户明细只有一个编号；正常中文、大小写、空白编号不受影响
- 只校验 user_id：额外字段即使值含孤立代理转义也被忽略，不扩大范围

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

# 失败样例 surrogate.jsonl 的五条物理行：
#   第 1 行：new-user 10:00:00 visit（合法）
#   第 2 行：空白行（不导入，但占据一个物理行号）
#   第 3 行：new-user 10:00:30 signup（合法）
#   第 4 行：事件与时间沿用第 1 行，仅 user_id 改为 JSON 串 "\ud800"
#             （ASCII 源码文本 \ud800，解析后是孤立高代理 U+D800）
#   第 5 行：只有左花括号（非法 JSON，但不得越过第 4 行被报告）
NEW_USER_VISIT = json.dumps(
    {"user_id": "new-user", "event": "visit", "timestamp": "2026-10-06T10:00:00"},
    ensure_ascii=False,
)
NEW_USER_SIGNUP = json.dumps(
    {"user_id": "new-user", "event": "signup", "timestamp": "2026-10-06T10:00:30"},
    ensure_ascii=False,
)
LONE_HIGH_USER_LINE = (
    '{"user_id": "\\ud800", "event": "visit", '
    '"timestamp": "2026-10-06T10:00:00"}'
)
BAD_JSON_LINE = "{"

SURROGATE_FAILURE_LINES = [
    NEW_USER_VISIT,
    "",
    NEW_USER_SIGNUP,
    LONE_HIGH_USER_LINE,
    BAD_JSON_LINE,
]
SURROGATE_FAILURE_BYTES = ("\n".join(SURROGATE_FAILURE_LINES) + "\n").encode("utf-8")

# 末行没有换行符：第 5 行 "{" 是最后一个物理行，首个错误仍须定位在第 4 行。
NO_TRAILING_NEWLINE_BYTES = "\n".join(SURROGATE_FAILURE_LINES).encode("utf-8")

# CRLF 文件：行号语义与 LF 完全一致。
SURROGATE_FAILURE_CRLF_BYTES = (
    "\r\n".join(SURROGATE_FAILURE_LINES) + "\r\n"
).encode("utf-8")

# 其他形态的未配对代理码点（user_id 内为 JSON 转义源码文本）。
LONE_LOW_USER_LINE = (
    '{"user_id": "\\udc00", "event": "visit", '
    '"timestamp": "2026-10-06T10:00:00"}'
)
REVERSED_SURROGATES_LINE = (
    '{"user_id": "\\ude00\\ud83d", "event": "visit", '
    '"timestamp": "2026-10-06T10:00:00"}'
)
INTERLEAVED_SURROGATE_LINE = (
    '{"user_id": "a\\ud800b", "event": "visit", '
    '"timestamp": "2026-10-06T10:00:00"}'
)

# 额外字段 note 的值含孤立代理转义：校验范围只限 user_id，本行必须合法。
SURROGATE_IN_EXTRA_FIELD_LINE = (
    '{"user_id": "ok-user", "event": "visit", '
    '"timestamp": "2026-10-06T10:00:00", "note": "\\ud800"}'
)

# 成功样例：同一用户 😀（U+1F600）的 visit 与 signup——
# 第 1 行用合法代理对转义 😀（ASCII 源码文本），
# 第 2 行直接写入 UTF-8 补充平面字符 😀；解析后必须是同一个编号。
ESCAPED_PAIR_VISIT_LINE = (
    '{"user_id": "\\ud83d\\ude00", "event": "visit", '
    '"timestamp": "2026-10-06T10:00:00"}'
)
DIRECT_ASTRAL_SIGNUP = {
    "user_id": "😀",
    "event": "signup",
    "timestamp": "2026-10-06T10:00:30",
}
ASTRAL_USER_ID = "😀"

# 第 2 行是非法 UTF-8 字节，第 3 行才是未配对代理码点：必须报第 2 行的
# UTF-8 原因，后续代理码点错误不得覆盖更早行的原因。
EARLIER_UTF8_ERROR_BYTES = b"\n".join(
    [NEW_USER_VISIT.encode("utf-8"), b"\xff", LONE_HIGH_USER_LINE.encode("utf-8")]
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
        """按原始字节写入输入文件（可表达 CRLF/无末行换行/非法字节）。"""
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def write_raw_line(self, name, line):
        """写入单行 JSONL 文本（用于保留 \\uXXXX 源码转义）。"""
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
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

    def assert_seed_report_unchanged(self, db):
        """种子用户的两步报告仍为访问 1 人、转化 1 人、比例 1。"""
        result = run_funnel("report", "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["visit_users"], 1)
        self.assertEqual(payload["converted_users"], 1)
        self.assertEqual(payload["conversion_rate"], 1)

    # -- 验收样例：第 4 行 user_id 为 "\ud800"，第 5 行才是非法 JSON -------

    def test_lone_high_surrogate_reports_line_4_and_fails_atomically(self):
        db = self.seed_database()
        path = self.write_bytes("surrogate.jsonl", SURROGATE_FAILURE_BYTES)

        before_rows, before_count = self.snapshot_events(db)
        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

        # 标准错误必须给出输入路径、第 4 行、user_id、未配对代理码点原因，
        # user_id 以可安全输出的 \ud800 转义形式出现，且无异常堆栈。
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("user_id", result.stderr)
        self.assertIn("\\ud800", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 只报告首个错误：不得越过第 4 行去报告第 5 行的非法 JSON，
        # 也不得报告合法的第 1、3 行。
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("第 1 行", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertNotIn("非法 JSON", result.stderr)

        # 整批不写入：失败前后逐行一致；合法前缀（new-user 两条）不留存。
        after_rows, after_count = self.snapshot_events(db)
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_count, before_count)
        self.assertEqual(after_rows, EXPECTED_SEED_ROWS)
        self.assertFalse(any(row[0] == "new-user" for row in after_rows))

        # 已有数据库的报告结果不变。
        self.assert_seed_report_unchanged(db)

    # -- 末行没有换行符：行号语义与 LF 一致 -------------------------------

    def test_last_line_without_newline_keeps_line_number(self):
        db = self.seed_database()
        path = self.write_bytes("surrogate-no-final-newline.jsonl", NO_TRAILING_NEWLINE_BYTES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("第 5 行", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_seed_report_unchanged(db)

    # -- CRLF 文件同样按物理行定位 ----------------------------------------

    def test_crlf_file_reports_same_line_number(self):
        db = self.seed_database()
        path = self.write_bytes("surrogate-crlf.jsonl", SURROGATE_FAILURE_CRLF_BYTES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_seed_report_unchanged(db)

    # -- 目标路径原先不存在：失败导入不创建任何数据库文件 ------------------

    def test_failed_import_does_not_create_database(self):
        db = self.db_path("never-existed.sqlite")
        self.assertFalse(os.path.exists(db))
        path = self.write_bytes("surrogate-fresh.jsonl", SURROGATE_FAILURE_BYTES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(path, result.stderr)
        self.assertIn("第 4 行", result.stderr)
        self.assertIn("\\ud800", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 数据库主文件不得被创建（journal/wal/shm 等附属文件同理：
        # 临时目录中除输入 JSONL 外不应出现任何其他文件）。
        self.assertFalse(os.path.exists(db))
        self.assertEqual(
            sorted(os.listdir(self.tmpdir)),
            [os.path.basename(path)],
        )

    # -- 孤立低代理、逆序代理对、普通字符夹带孤立代理 ----------------------

    def test_lone_low_surrogate_fails(self):
        db = self.seed_database()
        path = self.write_raw_line("lone-low.jsonl", LONE_LOW_USER_LINE)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 1 行", result.stderr)
        self.assertIn("\\udc00", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_seed_report_unchanged(db)

    def test_reversed_surrogate_pair_fails(self):
        db = self.seed_database()
        path = self.write_raw_line("reversed.jsonl", REVERSED_SURROGATES_LINE)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 1 行", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        # 首个码点是低代理 U+DE00；逆序序列不得被当作合法代理对。
        self.assertIn("U+DE00", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_seed_report_unchanged(db)

    def test_surrogate_interleaved_with_normal_chars_fails(self):
        db = self.seed_database()
        path = self.write_raw_line("interleaved.jsonl", INTERLEAVED_SURROGATE_LINE)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("第 1 行", result.stderr)
        # user_id 回显为 'a\ud800b'：普通字符与转义码点都在。
        self.assertIn("a\\ud800b", result.stderr)
        self.assertIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_seed_report_unchanged(db)

    # -- 更早行的非法 UTF-8 字节保留原有原因 -------------------------------

    def test_earlier_utf8_error_keeps_its_own_reason(self):
        db = self.seed_database()
        path = self.write_bytes("earlier-utf8.jsonl", EARLIER_UTF8_ERROR_BYTES)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # 第 2 行是非法 UTF-8 字节，第 3 行才是代理码点错误：
        # 必须沿用第 2 行的 UTF-8 原因，不得报未配对代理码点。
        self.assertIn("第 2 行", result.stderr)
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("第 3 行", result.stderr)
        self.assertNotIn("未配对代理码点", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_seed_report_unchanged(db)

    # -- 额外字段含同类转义不扩大校验范围 ----------------------------------

    def test_surrogate_escape_in_extra_field_is_ignored(self):
        db = self.seed_database()
        path = self.write_raw_line("extra-field.jsonl", SURROGATE_IN_EXTRA_FIELD_LINE)

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 1})

        rows, count = self.snapshot_events(db)
        self.assertEqual(count, 3)
        self.assertIn(
            ("ok-user", "visit", "2026-10-06T10:00:00"),
            rows,
        )

    # -- 合法代理对转义与直接 😀 是同一用户 --------------------------------

    def test_escaped_pair_and_direct_astral_char_are_same_user(self):
        db = self.db_path()
        path = os.path.join(self.tmpdir, "astral.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ESCAPED_PAIR_VISIT_LINE + "\n")
            fh.write(json.dumps(DIRECT_ASTRAL_SIGNUP, ensure_ascii=False) + "\n")

        result = run_funnel("import", path, "--db", db)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"imported": 2})

        # 两行的 user_id 落库后必须完全相等：都是 😀，共一个编号。
        rows, count = self.snapshot_events(db)
        self.assertEqual(count, 2)
        self.assertEqual(
            rows,
            [
                (ASTRAL_USER_ID, "signup", "2026-10-06T10:00:30"),
                (ASTRAL_USER_ID, "visit", "2026-10-06T10:00:00"),
            ],
        )
        with sqlite3.connect(db) as conn:
            distinct_ids = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM events")]
        self.assertEqual(distinct_ids, [ASTRAL_USER_ID])

        # 报告：访问 1 人、转化 1 人、比例 1。
        report = run_funnel("report", "--db", db)
        self.assertEqual(report.returncode, 0, msg=report.stderr)
        self.assertEqual(
            json.loads(report.stdout),
            {"visit_users": 1, "converted_users": 1, "conversion_rate": 1},
        )

        # --include-users：两个明细数组都只有同一个补充平面编号。
        users_report = run_funnel("report", "--db", db, "--include-users")
        self.assertEqual(users_report.returncode, 0, msg=users_report.stderr)
        users_payload = json.loads(users_report.stdout)
        self.assertEqual(users_payload["visit_user_ids"], [ASTRAL_USER_ID])
        self.assertEqual(users_payload["converted_user_ids"], [ASTRAL_USER_ID])

        # --include-pairs：该用户一条配对明细，行为与既有规则一致。
        pairs_report = run_funnel("report", "--db", db, "--include-pairs")
        self.assertEqual(pairs_report.returncode, 0, msg=pairs_report.stderr)
        pairs_payload = json.loads(pairs_report.stdout)
        self.assertEqual(
            pairs_payload["conversion_pairs"],
            [
                {
                    "user_id": ASTRAL_USER_ID,
                    "visit_timestamp": "2026-10-06T10:00:00",
                    "signup_timestamp": "2026-10-06T10:00:30",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
