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
- 父目录不存在、目标是目录、保存阶段写入失败：退出码 2、标准输出
  为空、标准错误包含输出路径及原因
- 参数校验或报告查询失败时不创建或改写输出文件；保存阶段（报告已
  生成、目标尚未更新时）发生确定的 OSError：已有目标保留原字节，
  原本不存在的目标仍不存在，临时文件不残留
- 失败均不改变数据库事件记录；import 命令不接受 --output

只通过公开命令 ``python -m funnel import|report`` 观察行为。仅使用
Python 3 标准库；每个场景使用独立临时目录，全部用户编号均为虚构，
测试结束不遗留任何文件。保存失败不靠目录权限：测试把一份仅测试用
sitecustomize.py 放入独立目录并经 PYTHONPATH 注入（解释器标准启动
机制），在原子替换点包装 os.replace 抛出一次确定的 OSError，Windows
与普通 Linux 下均可复现，不需要管理员身份，也不改动任何产品代码。
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

# 保存阶段失败注入使用的环境变量：指向一个测试临时目录。子进程中的
# sitecustomize 只在该变量存在时生效，哨兵文件固定写在该目录下。
ENV_HOOK_DIR = "FUNNEL_TEST_REPLACE_HOOK_DIR"
SENTINEL_NAME = "replace-fired.flag"
# 与 SITECUSTOMIZE_SOURCE 中抛出的 OSError 文本保持一致。
INJECTED_REASON = "injected save failure for funnel report tests"

# 仅测试使用的 sitecustomize.py：经 PYTHONPATH 由解释器在启动时自动导入
# （标准库 site 机制，非产品代码、非第三方软件），只在上述环境变量存在
# 时生效。它把 os.replace 包装成：第一次被调用时（即产品把完整报告写入
# 同目录临时文件、准备替换目标的保存点），先写哨兵文件再抛出一次确定的
# OSError——此刻目标尚未被更新；随后恢复原 os.replace，保证同进程内不会
# 有第二次注入。os.replace 是产品原子保存唯一的“更新目标”动作，包装它
# 不依赖目录权限，Windows 与普通 Linux 下均可复现。
SITECUSTOMIZE_SOURCE = """\
import os

_hook_dir = os.environ.get(%r)
if _hook_dir:
    _sentinel = os.path.join(_hook_dir, %r)
    _real_replace = os.replace
    _state = {"fired": False}

    def _replace_once(src, dst, *args, **kwargs):
        if not _state["fired"]:
            _state["fired"] = True
            try:
                with open(_sentinel, "w", encoding="utf-8") as _fh:
                    _fh.write("fired\\n")
            except OSError:
                pass
            raise OSError(%r)
        return _real_replace(src, dst, *args, **kwargs)

    os.replace = _replace_once
""" % (
    ENV_HOOK_DIR,
    SENTINEL_NAME,
    INJECTED_REASON,
)


def run_funnel(*cli_args, cwd=None, extra_env=None, pythonpath_prepend=()):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    prefix = os.pathsep.join([*pythonpath_prepend, PROJECT_ROOT])
    env["PYTHONPATH"] = prefix + os.pathsep + env.get("PYTHONPATH", "")
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

        # 独立的注入目录：放一份测试专用 sitecustomize.py，经 PYTHONPATH
        # 让子进程解释器自动加载；哨兵文件也写在这里，与输出目录分离，
        # 便于核验“输出目录不残留本次保存产生的中间文件”。
        self.hook_dir = os.path.join(self.tmpdir, "replace-hook")
        os.mkdir(self.hook_dir)
        with open(
            os.path.join(self.hook_dir, "sitecustomize.py"), "w", encoding="utf-8"
        ) as fh:
            fh.write(SITECUSTOMIZE_SOURCE)
        self.sentinel = os.path.join(self.hook_dir, SENTINEL_NAME)

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, *extra, cwd=None):
        return run_funnel("report", "--db", self.db, *extra, cwd=cwd)

    def report_with_save_failure(self, out_path):
        """带保存阶段失败注入运行一次 report。

        返回 (result, fired)：fired 依据哨兵文件确认注入的 OSError 确实
        在子进程保存点触发（而非因其他原因失败）。
        """
        self.assertFalse(os.path.exists(self.sentinel))
        result = run_funnel(
            "report",
            "--db",
            self.db,
            "--output",
            out_path,
            extra_env={ENV_HOOK_DIR: self.hook_dir},
            # sitecustomize.py 所在目录置于 PYTHONPATH 最前，由解释器启动
            # 时自动导入；PROJECT_ROOT 紧随其后，funnel 包解析不受影响。
            pythonpath_prepend=(self.hook_dir,),
        )
        return result, os.path.exists(self.sentinel)

    def event_count(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def assert_save_failure_result(self, result, fired, out_path):
        """保存失败的统一出口：退出码 2、标准输出完全为空，标准错误包含
        输出路径与确定的失败原因，且没有 Traceback。"""
        self.assertTrue(fired, "保存点的 OSError 未实际触发")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(out_path, result.stderr)
        self.assertIn(INJECTED_REASON, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def assert_recovered_success(self, out_path, existed_before):
        """恢复正常保存条件后再次请求报告：退出码 0、标准错误为空、标准
        输出只有一个单行 JSON 对象；文件 UTF-8 保存同一对象并以一个 LF
        结束，解析后与标准输出相等，三项指标不变。"""
        # 不传注入环境变量即恢复正常保存条件。
        result = self.report("--output", out_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), [json.dumps(EXPECTED_REPORT)])
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertFalse(result.stdout.endswith("\n\n"))
        raw = self.read_output(out_path)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertEqual(json.loads(raw.decode("utf-8")), EXPECTED_REPORT)
        # 文件整体 = 标准输出那一行（含结尾 LF），旧文件被整体覆盖。
        self.assertEqual(raw, result.stdout.encode("utf-8"))
        if existed_before:
            self.assertNotIn(b"ORIGINAL", raw)

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

    # -- 路径类失败与保存阶段失败（确定性 OSError 注入）------------------

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

    def test_save_failure_preserves_existing_bytes_then_overwrites(self):
        # 场景一：目标已存在且含固定原始字节。在报告已准备好、目标尚未
        # 被更新的保存点注入一次确定的 OSError：旧文件字节必须原样保留，
        # 输出目录不残留原子保存的临时文件，数据库事件记录不变。
        out_dir = os.path.join(self.tmpdir, "out-existing")
        os.mkdir(out_dir)
        target = os.path.join(out_dir, "report.json")
        original = b"\xef\xbb\xbfORIGINAL\xc3\xa9\r\nfixed bytes\n"
        with open(target, "wb") as fh:
            fh.write(original)

        before_events = self.event_count()
        result, fired = self.report_with_save_failure(target)
        self.assert_save_failure_result(result, fired, target)
        # 已有目标的字节逐字节不变。
        self.assertEqual(self.read_output(target), original)
        # 输出目录只剩目标本身：没有 .funnel-report-* 临时文件残留。
        self.assertEqual(sorted(os.listdir(out_dir)), [os.path.basename(target)])
        # 数据库事件记录保持不变。
        self.assertEqual(self.event_count(), before_events)

        # 恢复正常保存条件后用同一数据库与目标再次请求：整体覆盖成功。
        self.assert_recovered_success(target, existed_before=True)
        self.assertEqual(self.event_count(), before_events)
        self.assertEqual(sorted(os.listdir(out_dir)), [os.path.basename(target)])

    def test_save_failure_missing_target_stays_absent_then_creates(self):
        # 场景二：父目录存在但目标不存在。保存点注入失败后，目标仍不
        # 存在，输出目录不残留临时文件；恢复后同一请求正常创建文件。
        out_dir = os.path.join(self.tmpdir, "out-missing")
        os.mkdir(out_dir)
        target = os.path.join(out_dir, "report.json")
        self.assertFalse(os.path.exists(target))

        before_events = self.event_count()
        result, fired = self.report_with_save_failure(target)
        self.assert_save_failure_result(result, fired, target)
        # 缺失目标仍不存在。
        self.assertFalse(os.path.exists(target))
        # 输出目录为空：临时文件已被清理，不残留中间文件。
        self.assertEqual(sorted(os.listdir(out_dir)), [])
        self.assertEqual(self.event_count(), before_events)

        # 恢复正常保存条件后同一请求：文件正常创建。
        self.assert_recovered_success(target, existed_before=False)
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(self.event_count(), before_events)
        self.assertEqual(sorted(os.listdir(out_dir)), [os.path.basename(target)])

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
