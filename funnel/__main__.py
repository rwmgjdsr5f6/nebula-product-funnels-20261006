"""命令行入口：

    python -m funnel import <events.jsonl> --db <events.sqlite>
    python -m funnel report --db <events.sqlite> [--within-seconds N]
                              [--visit-from YYYY-MM-DDTHH:MM:SS
                               --visit-before YYYY-MM-DDTHH:MM:SS]
                              [--include-users] [--include-pairs]
                              [--include-latency]
                              [--group-by visit-date [--include-group-pairs]
                                                         [--include-group-latency]]
                              [--output FILE]

仅使用 Python 3 标准库与 SQLite，处理本地虚构用户事件。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
# 报告时间段参数专用：严格锚定首尾，前后空白与末尾 LF 都不接受
# （TIMESTAMP_RE 的 $ 可匹配末尾 LF 之前的位置，不能直接复用）。
VISIT_BOUND_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\Z")
VALID_EVENTS = ("visit", "signup")
# 严格锚定到完整参数值的首尾：必须使用 \A/\Z 而不是 ^/$——Python 正则中
# 的 $ 可匹配末尾 LF 之前的位置，^[0-9]+$ 会把 "60\n" 误判为合法值。
WITHIN_SECONDS_RE = re.compile(r"\A[0-9]+\Z")

# SQLite INTEGER 的上限。合法时间戳（公元 1 至 9999 年）之间的最大间隔约
# 3.2e11 秒，远小于该上限，因此把窗口钳制到此值不会改变任何统计结果，
# 同时避免绑定超大 Python 整数时触发 OverflowError。
MAX_SQLITE_INTEGER = 2**63 - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    user_id   TEXT NOT NULL,
    event     TEXT NOT NULL,
    timestamp TEXT NOT NULL
)
"""


class LineError(Exception):
    """单行记录校验失败，携带从 1 开始的物理行号与原因。"""

    def __init__(self, line_no, reason):
        self.line_no = line_no
        self.reason = reason
        super().__init__(reason)


def parse_line(text, line_no):
    """校验一条非空 JSONL 记录，返回 (user_id, event, timestamp)。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LineError(line_no, "非法 JSON: %s" % exc.msg)
    if not isinstance(obj, dict):
        raise LineError(line_no, "记录不是 JSON 对象")

    if "user_id" not in obj:
        raise LineError(line_no, "缺少必填字段 user_id")
    user_id = obj["user_id"]
    if not isinstance(user_id, str) or user_id == "":
        raise LineError(line_no, "user_id 必须是非空字符串")
    # 拒绝未配对的 Unicode 代理码点：JSON 转义 \ud800 一类孤立高/低代理经
    # json 解析后会留下 U+D800–U+DFFF 的码点；合法代理对（如
    # 😀）已被解析成单个补充平面字符（😀），不会落到此区间。
    # 不能把原值直接写进标准错误：代理码点无法按 UTF-8 编码输出，故用 %r
    # 以 \uXXXX 转义形式回显 user_id。
    for ch in user_id:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise LineError(
                line_no,
                "user_id %r 含未配对代理码点 U+%04X：代理码点必须成对出现，"
                "不允许孤立的 U+D800 至 U+DFFF 码点" % (user_id, ord(ch)),
            )

    if "event" not in obj:
        raise LineError(line_no, "缺少必填字段 event")
    event = obj["event"]
    if not isinstance(event, str):
        raise LineError(line_no, "event 必须是字符串")
    if event not in VALID_EVENTS:
        raise LineError(line_no, "event 只接受 visit 或 signup，得到 %r" % event)

    if "timestamp" not in obj:
        raise LineError(line_no, "缺少必填字段 timestamp")
    timestamp = obj["timestamp"]
    if not isinstance(timestamp, str):
        raise LineError(line_no, "timestamp 必须是字符串")
    if not TIMESTAMP_RE.match(timestamp):
        raise LineError(
            line_no,
            "timestamp 必须是 YYYY-MM-DDTHH:MM:SS 格式，不接受时区后缀或小数秒",
        )
    try:
        datetime.strptime(timestamp, TIMESTAMP_FORMAT)
    except ValueError:
        raise LineError(line_no, "timestamp 不是有效时间: %r" % timestamp)

    return (user_id, event, timestamp)


def load_events(path):
    """读取并校验整个 JSONL 文件；任一行非法则抛出 LineError，不返回部分结果。

    以二进制逐物理行读取并严格按 UTF-8 解码：任一行的字节不是合法 UTF-8
    即抛出 LineError（携带该物理行号），不替换字节、不跳过坏行、不尝试
    其他编码。按行顺序处理，首个错误（非法 JSON、字段问题或编码错误）
    即报即停，更早行的原有原因不会被后续行的编码错误覆盖。
    """
    events = []
    with open(path, "rb") as fh:
        # 二进制迭代按 b"\n" 切分：LF 与 CRLF 一致处理，
        # 末行没有换行符时仍作为最后一行产出。
        for line_no, raw in enumerate(fh, start=1):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LineError(line_no, "UTF-8 编码无效: %s" % exc.reason)
            text = text.strip()
            if not text:
                continue  # 空白行忽略，但物理行号照常递增
            events.append(parse_line(text, line_no))
    return events


def cmd_import(args):
    try:
        events = load_events(args.file)
    except LineError as exc:
        print("%s: 第 %d 行: %s" % (args.file, exc.line_no, exc.reason), file=sys.stderr)
        return 2
    except OSError as exc:
        print("%s: 无法读取输入文件: %s" % (args.file, exc.strerror or exc), file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(args.db)
        try:
            with conn:  # 任一条写入失败都会回滚，保证本次不保存任何事件
                conn.execute(SCHEMA)
                conn.executemany(
                    "INSERT INTO events (user_id, event, timestamp) VALUES (?, ?, ?)",
                    events,
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("%s: 数据库无法访问: %s" % (args.db, exc), file=sys.stderr)
        return 2

    print(json.dumps({"imported": len(events)}))
    return 0


def parse_within_seconds(text):
    """--within-seconds 取值校验：仅接受全为 ASCII 数字且大于零的整数（允许前导零）。

    数值与位数均不设上限。超过 SQLite INTEGER 上限的窗口等价于不限间隔
    （合法时间戳之间的间隔不可能达到该量级），统一钳制到该上限返回；
    比较按去前导零后的十进制字符串进行，不依赖 int() 的位数限制。
    """
    if not WITHIN_SECONDS_RE.match(text):
        raise argparse.ArgumentTypeError(
            "必须是只含 ASCII 数字的正整数，得到 %r" % text
        )
    digits = text.lstrip("0")
    if not digits:
        raise argparse.ArgumentTypeError("必须大于零，得到 %r" % text)
    max_digits = str(MAX_SQLITE_INTEGER)
    if len(digits) > len(max_digits) or (
        len(digits) == len(max_digits) and digits > max_digits
    ):
        return MAX_SQLITE_INTEGER
    return int(digits)


def _visit_filter(args, alias=""):
    """合格 visit 的 WHERE 子句与绑定参数：段内访问规则的唯一维护点。

    返回 (where_sql, params)。where_sql 恒含 event = 'visit' 条件；提供
    时段时追加 timestamp >= ? AND timestamp < ?——含起点、不含终点（时间戳
    为定宽 ISO 文本，字典序比较与时间先后等价）；不传时段则统计全库。
    alias 非空时为列名加表别名前缀（转化查询的 v）。

    汇总、编号、配对、耗时与日期归组全部经由本函数取得访问资格，段内
    规则只在此处维护一份，不再由访问统计与转化查询各自平行维护。
    """
    prefix = alias + "." if alias else ""
    conditions = ["%sevent = 'visit'" % prefix]
    params = ()
    if args.visit_from is not None:
        conditions.append("%stimestamp >= ? AND %stimestamp < ?" % (prefix, prefix))
        params = (args.visit_from, args.visit_before)
    return "WHERE " + " AND ".join(conditions), params


def _converted_sql(args, select="COUNT(DISTINCT v.user_id)"):
    """转化查询：visit 必须在段内（如有），signup 只要求严格晚于该次
    visit，signup 自身可以晚于段终点。select 决定返回人数还是用户编号。"""
    sql = [
        "SELECT " + select,
        "FROM events v",
        "JOIN events s",
        "  ON s.user_id = v.user_id",
        " AND s.event = 'signup'",
        " AND s.timestamp > v.timestamp",
    ]
    if args.within_seconds is not None:
        # 任一 (visit, signup) 对满足：signup 严格晚于 visit 且间隔 <= N 秒
        sql.append(
            " AND CAST(strftime('%s', s.timestamp) AS INTEGER)"
            "\n                         - CAST(strftime('%s', v.timestamp) AS INTEGER) <= ?"
        )
    visit_where, _ = _visit_filter(args, alias="v")
    sql.append(visit_where)
    return "\n".join(sql)


def _converted_params(args):
    params = []
    if args.within_seconds is not None:
        params.append(args.within_seconds)
    _, visit_params = _visit_filter(args, alias="v")
    params += visit_params
    return tuple(params)


def parse_group_by(text):
    """--group-by 取值校验：目前只接受 visit-date。

    空值与其他取值都经 argparse 以退出码 2 拒绝（标准错误自带参数名
    前缀），且先于一切数据库访问发生。
    """
    if text != "visit-date":
        raise argparse.ArgumentTypeError(
            "只接受 visit-date（按最早合格 visit 的 UTC 日期分组），得到 %r" % text
        )
    return text


def parse_visit_bound(text):
    """--visit-from / --visit-before 取值校验。

    只接受恰好 YYYY-MM-DDTHH:MM:SS 形态的有效日历时间（统一视为 UTC）：
    不接受前后空白、时区后缀或小数秒；正则只保证形态，strptime 再排除
    2026-02-30 这类形态合法但日历无效的日期。校验失败经 argparse 以退出
    码 2 拒绝（标准错误自带参数名前缀），且先于一切数据库访问发生。
    """
    if not VISIT_BOUND_RE.match(text):
        raise argparse.ArgumentTypeError(
            "必须是 YYYY-MM-DDTHH:MM:SS 格式（UTC，不含空白、时区后缀或小数秒），得到 %r"
            % text
        )
    try:
        datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        raise argparse.ArgumentTypeError("不是有效时间: %r" % text)
    return text


def parse_output_path(text):
    """--output 取值校验：必须是非空字符串。

    空白不做裁剪——只由空白组成的取值同样视为空字符串拒绝。空值经
    argparse 以退出码 2 拒绝（标准错误自带 --output 参数名前缀），且
    先于一切数据库访问发生。
    """
    if text == "":
        raise argparse.ArgumentTypeError(
            "必须给出非空的本地文件路径，得到空字符串"
        )
    return text


def _output_error(output_path, reason):
    """--output 相关错误的统一出口：退出码 2、标准输出为空，标准错误
    指出输出路径与原因。"""
    print("%s: %s" % (output_path, reason), file=sys.stderr)
    return 2


def _prepare_output(output_path, db_path):
    """--output 的数据库访问前校验，返回 None（通过）或退出码 2。

    - 相对路径按当前工作目录解释：abspath 只补全 cwd，不规范化
      ``..``/符号链接，与 --db 的系统规则保持同一口径。
    - 输出路径与 --db 规范化（normcase + abspath）后指向同一绝对
      路径时提前拒绝，错误信息同时指出两个参数。
    - 父目录必须已存在（不由程序创建）；目标已存在时必须是普通文件
      而非目录。
    所有失败均先于数据库连接发生：不创建数据库、不改动事件记录，
    也不创建或改写输出文件。
    """
    output_abs = os.path.normcase(os.path.abspath(output_path))
    db_abs = os.path.normcase(os.path.abspath(db_path))
    if output_abs == db_abs:
        print(
            "--output 与 --db 不能指向同一路径：两者规范化后均为 %s"
            % output_abs,
            file=sys.stderr,
        )
        return 2

    parent = os.path.dirname(output_abs)
    if not os.path.isdir(parent):
        return _output_error(output_path, "父目录不存在: %s" % parent)
    if os.path.isdir(output_abs):
        return _output_error(output_path, "目标已存在且是目录，不是普通文件")
    # 既不是目录也不是普通文件（符号链接到目录已被上面的 isdir 拦截；
    # 残留的非常规文件如 FIFO、设备文件）不允许整体覆盖。
    if os.path.exists(output_path) and not os.path.isfile(output_path):
        return _output_error(output_path, "目标已存在但不是普通文件")
    return None


def _write_report_file(output_path, line):
    """把与标准输出完全相同的一行报告原子地写入 --output 文件。

    UTF-8 编码、内容为单个完整 JSON 对象并以一个 LF 结束；不追加导出
    时间、路径或其他任何字段。写入采用“同目录临时文件 + os.replace”：
    先把完整内容（含结尾换行）落到临时文件，替换成功前不触碰目标——
    已有普通文件在保存失败时保留原内容，目标原本不存在时失败也不
    留下不完整文件（临时文件在出错时删除）。返回 None 表示成功，
    否则返回退出码 2（标准输出已先行打印，此处只负责文件与标准错误）。
    """
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    tmp_path = None
    try:
        # delete=False：由本函数显式接管临时文件的收尾，os.replace
        # 成功后原路径已不存在，清理时忽略 FileNotFoundError。
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=".funnel-report-", delete=False
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(line.encode("utf-8"))
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, output_path)
    except OSError as exc:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return _output_error(
            output_path, "报告文件无法写入: %s" % (exc.strerror or exc)
        )
    return None



def _qualifying_visit_date_by_user(conn, visit_where, visit_params):
    """归组查询：返回 {user_id: 最早一次合格 visit 的 UTC 日期}。

    只回答"谁归哪天"这一件事：先由 visit_where 应用访问时段过滤
    （无时段即全库，含起点、不含终点），再按 user_id 取 MIN(timestamp)，
    段外 visit 不参与归组。时间戳为不含时区后缀的定宽 ISO 文本，统一按
    UTC 解释，前 10 个字符即日期 YYYY-MM-DD。GROUP BY 保证每人恰好一行，
    每个用户编号只归一个日期组。
    """
    return dict(
        conn.execute(
            "SELECT user_id, substr(MIN(timestamp), 1, 10) FROM events "
            + visit_where
            + " GROUP BY user_id",
            visit_params,
        )
    )


def _reduce_pairs(rows):
    """配对归约：每人先取时间最早的 signup，再从能与该注册有效配对的
    visit 中取时间最晚的一次。

    rows 来自与汇总完全相同的配对 SQL（仅改 select 列表）。时间戳为定宽
    ISO 文本，字典序比较与时间先后比较等价；重复事件与重复导入产生的
    相同配对不影响 min/max 结果。返回 {user_id: [signup_ts, visit_ts]}。
    """
    best_by_user = {}
    for user_id, visit_ts, signup_ts in rows:
        current = best_by_user.get(user_id)
        if current is None or signup_ts < current[0]:
            best_by_user[user_id] = [signup_ts, visit_ts]
        elif signup_ts == current[0] and visit_ts > current[1]:
            current[1] = visit_ts
    return best_by_user


def _pairs_payload(best_by_user, user_ids=None):
    """由归约结果装配 conversion_pairs 数组。

    user_ids 为 None 时取全部转化用户（顶层口径），否则只取给定子集
    （组内口径，调用方保证子集中的编号都在归约结果里）。排序在 Python
    侧进行：str 比较即 Unicode 码点字典序，区分大小写、保留空白与中文、
    不按数字大小排序。
    """
    ids = best_by_user if user_ids is None else user_ids
    return [
        {
            "user_id": user_id,
            "visit_timestamp": best_by_user[user_id][1],
            "signup_timestamp": best_by_user[user_id][0],
        }
        for user_id in sorted(ids)
    ]


def _latency_payload(best_by_user, user_ids=None):
    """由同一配对归约结果装配 conversion_latency 对象。

    每个转化用户只贡献一个耗时：直接取归约结果里该用户的那次配对
    （最早有效 signup 与能配对它的最晚 visit），以 UTC 时间差计算，
    而不是取该用户所有配对中的最短间隔。时间戳为同形态定宽 ISO 文本，
    统一按 UTC 解释，用 strptime 求差（可移植且与配对 SQL 的
    strftime('%s') 口径一致）。

    user_ids 为 None 时取全部转化用户（顶层口径），否则只取给定子集
    （组内口径，调用方保证子集中的编号都在归约结果里）。

    min/max 为整数秒；mean 为总耗时除以转化人数的算术平均，真除不取整。
    无转化（含无访问）时三项均为 None。
    """
    ids = best_by_user if user_ids is None else user_ids
    durations = [
        (
            datetime.strptime(best_by_user[user_id][0], TIMESTAMP_FORMAT)
            - datetime.strptime(best_by_user[user_id][1], TIMESTAMP_FORMAT)
        ).total_seconds()
        # 归约结果中 visit 严格早于 signup，耗时恒为正整数秒。
        for user_id in ids
    ]
    if not durations:
        return {"min_seconds": None, "max_seconds": None, "mean_seconds": None}
    total = sum(durations)
    return {
        "min_seconds": int(min(durations)),
        "max_seconds": int(max(durations)),
        "mean_seconds": total / len(durations),
    }


def _build_visit_date_groups(
    date_by_user,
    converted_user_ids,
    include_users,
    pairs_by_user=None,
    latency_by_user=None,
):
    """由归组映射与转化成员集合装配 visit_date_groups。

    这是分组结果唯一的装配点：日期成员关系只存在于 date_by_user，转化
    成员关系只存在于 converted_user_ids（与汇总同源的集合）；各组人数、
    比例与（可选的）组内编号数组全部从这两个集合就地派生，不再为同一
    统计事实平行维护计数与编号集合——人数即成员集合的 len，编号数组即
    成员集合的 sorted，二者天然勾稽。

    日期按定宽 YYYY-MM-DD 文本升序（字典序即时间先后），只列有合格访问
    用户的日期、不补空日期；无合格访问时返回空列表。组内转化成员取本组
    访问成员与转化集合的交集（转化集合本就是合格访问用户的子集，交集同时
    兜底防止组外编号计入）。比例按本组两种人数真除，分母恒为正。

    pairs_by_user 非 None 时（--include-group-pairs），每组再追加
    conversion_pairs：即本组转化成员在顶层同一归约结果中的配对子集。
    每个转化用户只归一个日期组，故各组配对合并排序后与顶层数组完全一致，
    组间不会重复用户；无转化的组得到空数组。

    latency_by_user 非 None 时（--include-group-latency），每组再追加
    conversion_latency：本组转化成员在同一归约结果中的耗时子集统计，
    口径与顶层 conversion_latency 相同（min/max 整数秒，mean 本组耗时
    之和除以本组转化人数，真除不取整）。两个开关各传各的同一份归约结果，
    互不依赖：只开组内耗时时配对明细不出现，反之亦然。每个转化用户只归
    一个日期组，故各组组内耗时的人数之和恰为总转化人数；无转化的组三项
    均为 None。
    """
    converted_members = set(converted_user_ids)
    visit_ids_by_date = {}
    for user_id, day in date_by_user.items():
        visit_ids_by_date.setdefault(day, set()).add(user_id)

    groups = []
    for day in sorted(visit_ids_by_date):
        visit_ids = visit_ids_by_date[day]
        converted_ids = visit_ids & converted_members
        group = {
            "visit_date": day,
            "visit_users": len(visit_ids),
            "converted_users": len(converted_ids),
            "conversion_rate": len(converted_ids) / len(visit_ids),
        }
        if include_users:
            # 排序在 Python 侧进行：str 比较即 Unicode 码点字典序，与顶层
            # 编号数组同一口径——区分大小写、保留空白与中文、不按数字大小。
            group["visit_user_ids"] = sorted(visit_ids)
            group["converted_user_ids"] = sorted(converted_ids)
        if pairs_by_user is not None:
            # 组内配对与顶层共用同一归约结果，只按本组转化成员过滤；
            # 数组长度即本组转化人数，排序口径与顶层相同。
            group["conversion_pairs"] = _pairs_payload(pairs_by_user, converted_ids)
        if latency_by_user is not None:
            # 组内耗时与顶层共用同一归约结果，只按本组转化成员过滤；
            # 空集合（无转化的组）自然得到三项 None，无合格访问时连组
            # 都不会生成（分组数组为空）。
            group["conversion_latency"] = _latency_payload(
                latency_by_user, converted_ids
            )
        groups.append(group)
    return groups


def cmd_report(args):
    # 时间段参数必须成对出现且起点严格早于终点。参数校验先于数据库访问：
    # 任何一项不合法都直接退出码 2，不创建数据库、不改动记录。
    if (args.visit_from is None) != (args.visit_before is None):
        missing = "--visit-before" if args.visit_from is not None else "--visit-from"
        present = "--visit-from" if missing == "--visit-before" else "--visit-before"
        print(
            "%s 与 %s 必须成对使用：已提供 %s，缺少 %s"
            % ("--visit-from", "--visit-before", present, missing),
            file=sys.stderr,
        )
        return 2
    if (
        args.visit_from is not None
        and args.visit_from >= args.visit_before
    ):
        # 时间戳为定宽 ISO 文本，字典序比较与时间先后比较等价。
        print(
            "--visit-from 必须严格早于 --visit-before：得到 --visit-from %r、--visit-before %r"
            % (args.visit_from, args.visit_before),
            file=sys.stderr,
        )
        return 2

    # --include-group-pairs 只与 --group-by visit-date 合用：单独提供时按
    # 参数错误处理，与上面的校验一样先于一切数据库访问，不创建数据库、
    # 不改动记录。
    if args.include_group_pairs and args.group_by != "visit-date":
        print(
            "--include-group-pairs 必须与 --group-by visit-date 合用："
            "已提供 --include-group-pairs，缺少 --group-by visit-date",
            file=sys.stderr,
        )
        return 2

    # --include-group-latency 同样只与 --group-by visit-date 合用，单独
    # 提供时按参数错误处理，且先于一切数据库访问，不创建数据库、不改动记录。
    if args.include_group_latency and args.group_by != "visit-date":
        print(
            "--include-group-latency 必须与 --group-by visit-date 合用："
            "已提供 --include-group-latency，缺少 --group-by visit-date",
            file=sys.stderr,
        )
        return 2

    # --output 的路径类校验与其他参数校验一样先于一切数据库访问：
    # 与 --db 同路径、父目录不存在、目标是目录等情况都直接退出码 2，
    # 不连接数据库、不创建数据库或输出文件。
    if args.output is not None:
        output_rc = _prepare_output(args.output, args.db)
        if output_rc is not None:
            return output_rc

    if not os.path.exists(args.db):
        print("%s: 数据库不存在" % args.db, file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(args.db)
        try:
            conn.execute(SCHEMA)
            # 段内访问规则的唯一来源：分母、编号明细与日期归组共用同一份
            # WHERE 子句与参数（含起点、不含终点；无时段即全库），转化查询
            # 经 _converted_sql 使用同一来源的带别名版本，段外 visit 既不
            # 计入访问人数，也不参与转化配对与归组。
            visit_where, visit_params = _visit_filter(args)
            visit_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM events " + visit_where,
                visit_params,
            ).fetchone()[0]
            converted_users = conn.execute(
                _converted_sql(args), _converted_params(args)
            ).fetchone()[0]
            if args.include_users:
                # 明细沿用与汇总完全相同的筛选条件，按 user_id 原值去重。
                # 排序在 Python 侧进行：str 比较即 Unicode 码点字典序，
                # 区分大小写、保留空白与中文、不按数字大小排序。
                visit_user_ids = sorted(
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT user_id FROM events " + visit_where,
                        visit_params,
                    )
                )
                converted_user_ids = sorted(
                    row[0]
                    for row in conn.execute(
                        _converted_sql(args, "DISTINCT v.user_id"),
                        _converted_params(args),
                    )
                )
            if (
                args.include_pairs
                or args.include_group_pairs
                or args.include_latency
                or args.include_group_latency
            ):
                # 配对明细沿用与汇总完全相同的配对条件（同一 SQL，仅改
                # select 列表），在 Python 侧归约且只归约一次：顶层数组、
                # 整体耗时统计与各日期组的组内数组/组内耗时都从这一份
                # 结果装配，它们天然勾稽——耗时逐人与顶层配对一一对应，
                # 各组配对合并排序后也必与顶层数组完全一致。
                best_by_user = _reduce_pairs(
                    conn.execute(
                        _converted_sql(args, "v.user_id, v.timestamp, s.timestamp"),
                        _converted_params(args),
                    )
                )
                if args.include_pairs:
                    conversion_pairs = _pairs_payload(best_by_user)
                if args.include_latency:
                    conversion_latency = _latency_payload(best_by_user)
            if args.group_by == "visit-date":
                # 归组与装配分两步，各自只有一个职责（见两函数文档）：
                # 1) 每人归最早合格 visit 的 UTC 日期，段外历史不影响归组；
                # 2) 各组人数、比例与编号数组全部由归组成员与转化成员两个
                #    集合就地派生。转化集合与汇总完全同源：可使用该用户任意
                #    合格访问配对（不限于归组那次），signup 严格晚于 visit
                #    且允许晚于段终点，--within-seconds 上界含等值。
                # 组内编号数组只在同时启用 --include-users 时追加；
                # --include-pairs 不向组内追加任何字段，组内配对明细由
                # --include-group-pairs 单独控制（不自动开启顶层配对），
                # 组内耗时由 --include-group-latency 单独控制（不自动
                # 开启顶层耗时或组内配对）。
                date_by_user = _qualifying_visit_date_by_user(
                    conn, visit_where, visit_params
                )
                converted_id_set = set(
                    row[0]
                    for row in conn.execute(
                        _converted_sql(args, "DISTINCT v.user_id"),
                        _converted_params(args),
                    )
                )
                visit_date_groups = _build_visit_date_groups(
                    date_by_user,
                    converted_id_set,
                    args.include_users,
                    best_by_user if args.include_group_pairs else None,
                    best_by_user if args.include_group_latency else None,
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("%s: 数据库无法访问: %s" % (args.db, exc), file=sys.stderr)
        return 2

    conversion_rate = converted_users / visit_users if visit_users else 0
    payload = {
        "visit_users": visit_users,
        "converted_users": converted_users,
        "conversion_rate": conversion_rate,
    }
    if args.include_users:
        payload["visit_user_ids"] = visit_user_ids
        payload["converted_user_ids"] = converted_user_ids
    if args.include_pairs:
        payload["conversion_pairs"] = conversion_pairs
    if args.include_latency:
        # 顶层整体耗时只由本开关控制；组内耗时由 --include-group-latency
        # 单独控制，互不自动开启（见上方分组装配）。
        payload["conversion_latency"] = conversion_latency
    if args.group_by == "visit-date":
        payload["visit_date_groups"] = visit_date_groups

    # 标准输出与文件共用同一条序列化结果：文件内容与本次标准输出解析后
    # 完全一致，不增加导出时间、路径或其他字段。
    line = json.dumps(payload)

    if args.output is not None:
        # 文件先于标准输出落盘：保存失败时退出码 2、标准输出必须为空
        # （路径类问题已在数据库访问前的 _prepare_output 拦截，这里只
        # 可能是写入/替换阶段的 OSError）。原子替换保证失败时已有目标
        # 保留原内容、原本不存在的目标不留下不完整文件。
        write_rc = _write_report_file(args.output, line + "\n")
        if write_rc is not None:
            return write_rc

    print(line)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m funnel",
        description="合成 JSONL 导入与 visit -> signup 两步漏斗报告",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="导入 JSONL 事件到 SQLite")
    p_import.add_argument("file", help="UTF-8 JSONL 事件文件路径")
    p_import.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_import.set_defaults(func=cmd_import)

    p_report = sub.add_parser("report", help="统计全库两步漏斗")
    p_report.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_report.add_argument(
        "--within-seconds",
        type=parse_within_seconds,
        default=None,
        metavar="N",
        help="只统计访问后 N 秒内完成的注册（正整数秒，允许前导零）；不传则不限间隔",
    )
    p_report.add_argument(
        "--visit-from",
        type=parse_visit_bound,
        default=None,
        metavar="YYYY-MM-DDTHH:MM:SS",
        help="只统计该起点（含）之后发生访问的用户；UTC，必须与 --visit-before 成对使用",
    )
    p_report.add_argument(
        "--visit-before",
        type=parse_visit_bound,
        default=None,
        metavar="YYYY-MM-DDTHH:MM:SS",
        help="只统计该终点（不含）之前发生访问的用户；UTC，必须与 --visit-from 成对使用",
    )
    p_report.add_argument(
        "--include-users",
        action="store_true",
        help="在汇总之外追加 visit_user_ids 与 converted_user_ids 两个编号数组"
        "（按 user_id 原值去重，按 Unicode 码点升序），不改变汇总数值；"
        "与 --group-by visit-date 合用时，每个日期组内同样追加这两个数组",
    )
    p_report.add_argument(
        "--include-pairs",
        action="store_true",
        help="在汇总之外追加 conversion_pairs 配对明细数组"
        "（每个转化用户一条：最早有效 signup 配对最晚有效 visit，"
        "按 user_id 原值的 Unicode 码点升序），不改变汇总数值",
    )
    p_report.add_argument(
        "--include-latency",
        action="store_true",
        help="在汇总之外追加 conversion_latency 整体转化耗时对象"
        "（仅含 min_seconds、max_seconds、mean_seconds）；每个转化用户只"
        "贡献一个耗时，取最早有效 signup 与能配对它的最晚 visit 的 UTC 时间差，"
        "不取所有配对的最短间隔；可独立使用，不依赖 --include-pairs，"
        "不改变汇总数值，无转化时三项均为 null",
    )
    p_report.add_argument(
        "--include-group-pairs",
        action="store_true",
        help="与 --group-by visit-date 合用时，每个日期组追加 conversion_pairs "
        "配对明细数组（口径与顶层 conversion_pairs 相同：每个转化用户一条，"
        "最早有效 signup 配对最晚有效 visit，按 user_id 原值的 Unicode 码点"
        "升序）；不自动开启顶层配对或编号明细，单独使用则以退出码 2 拒绝",
    )
    p_report.add_argument(
        "--include-group-latency",
        action="store_true",
        help="与 --group-by visit-date 合用时，每个日期组追加 conversion_latency "
        "组内转化耗时对象（仅含 min_seconds、max_seconds、mean_seconds，"
        "口径与顶层 conversion_latency 相同：每个转化用户只贡献一个耗时，"
        "取最早有效 signup 与能配对它的最晚 visit 的 UTC 时间差）；"
        "min/max 为整数秒，mean 为本组转化用户耗时之和除以人数，真除不取整，"
        "无转化的组三项均为 null；不自动开启顶层耗时、配对或编号明细，"
        "单独使用则以退出码 2 拒绝",
    )
    p_report.add_argument(
        "--group-by",
        type=parse_group_by,
        default=None,
        metavar="visit-date",
        help="按维度分组追加统计；目前只接受 visit-date（每个用户归到最早合格 "
        "visit 的 UTC 日期，在原有 JSON 中追加 visit_date_groups 数组）；"
        "与 --include-users 合用时各组再追加组内 visit_user_ids 与 "
        "converted_user_ids，与 --include-group-pairs 合用时各组再追加组内 "
        "conversion_pairs，与 --include-group-latency 合用时各组再追加组内 "
        "conversion_latency；不传则输出不变",
    )
    p_report.add_argument(
        "--output",
        type=parse_output_path,
        default=None,
        metavar="FILE",
        help="把本次报告 JSON 保存到本地文件（UTF-8，单个 JSON 对象并以换行"
        "结束，内容与标准输出完全一致，不追加其他字段）；相对路径按当前工作"
        "目录解释，目标不存在则创建，已存在普通文件则整体覆盖；父目录须事先"
        "存在。不能与 --db 指向同一路径",
    )
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
