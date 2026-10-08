# 本地产品事件漏斗台

整理产品事件，分析转化漏斗及用户分组。面向本地单机使用，仅依赖 Python 3 标准库与 SQLite。

## 使用说明

导入 JSONL 事件（向数据库追加，可重复执行）：

```sh
python -m funnel import sample.jsonl --db events.sqlite
```

统计全库 visit → signup 两步漏斗：

```sh
python -m funnel report --db events.sqlite
```

只统计访问后 N 秒内完成注册的转化（可选，仅影响本次报告，不改写数据库）：

```sh
python -m funnel report --db events.sqlite --within-seconds 60
```

只统计指定访问时间段内有访问的用户（可选，与 `--within-seconds` 可同时使用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite \
  --visit-from 2026-10-06T10:00:00 --visit-before 2026-10-06T11:00:00
```

`--visit-from` 与 `--visit-before` 必须成对使用，均为 `YYYY-MM-DDTHH:MM:SS` 格式并视为 UTC，所有数字位置只接受 ASCII 0 到 9（全角数字、阿拉伯印度数字及混排一律拒绝，不自动转换、裁剪或补零）；范围含起点、不含终点，起点须严格早于终点。

在汇总之外列出计入统计的用户编号（可选，与 `--within-seconds`、`--visit-from/--visit-before` 可同时使用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --include-users
```

在汇总之外列出转化用户的配对明细（可选，不带取值，可单独使用，也可与 `--include-users` 及上述筛选项合用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --include-pairs
```

在汇总之外追加整体转化耗时统计（可选，不带取值，可独立使用，不依赖 `--include-pairs`，也可与上述所有开关合用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --include-latency
```

按访问日期分组追加统计（可选，取值目前只接受 `visit-date`，与 `--within-seconds`、`--visit-from/--visit-before`、`--include-users`、`--include-pairs`、`--include-latency` 可同时使用，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --group-by visit-date
```

按组核对转化配对明细（可选，不带取值，仅与 `--group-by visit-date` 合用时有效，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --group-by visit-date --include-group-pairs
```

按组统计转化耗时（可选，不带取值，仅与 `--group-by visit-date` 合用时有效，不自动开启顶层耗时或其他明细，仅影响本次报告）：

```sh
python -m funnel report --db events.sqlite --group-by visit-date --include-group-latency
```

把完整报告同时保存到本地 JSON 文件（可选，仅影响本次报告，不改写数据库）：

```sh
python -m funnel report --db events.sqlite --output report.json
```

`--output` 后接本地文件路径：相对路径按当前工作目录解释；目标不存在时创建文件，已存在普通文件则整体覆盖（不追加多个报告）；父目录由使用者事先准备，程序不会创建。文件采用 UTF-8 编码，包含一个完整 JSON 对象并以换行结束，解析后的内容与本次标准输出完全一致，不包含导出时间、路径或其他额外字段——访问时段、转化时限、用户编号、配对、耗时与日期分组等全部参数照常合用，保存哪些字段由这些参数共同决定。保存成功时退出码仍为 0，标准输出仍只返回原来的单行报告 JSON，标准错误为空；不传 `--output` 时输出与错误行为保持不变。

`--output` 缺少取值或取空字符串时退出码 2、标准输出为空、标准错误指出 `--output` 及原因，且在数据库访问前拒绝。输出路径与 `--db` 按当前系统规则规范化后得到同一绝对路径时，同样在数据库访问前拒绝，标准错误同时指出 `--output` 与 `--db`。父目录不存在、目标是目录或目标无法写入时退出码 2、标准输出为空、标准错误包含输出路径及原因。参数校验或报告查询失败时不创建或改写输出文件；保存失败时已有目标保留原内容，原本不存在的目标不留下不完整文件。

## 事件格式

UTF-8 JSONL，每个非空行是一个 JSON 对象，必填字段：

- `user_id`：非空字符串，按原值区分用户；接受中文、大小写、空白与补充平面字符（合法代理对转义如 `😀` 与直接写入的 😀 解析后是同一编号），但不允许含未配对的 Unicode 代理码点（U+D800 至 U+DFFF，如孤立的 `\ud800`）
- `event`：仅接受 `visit` 或 `signup`
- `timestamp`：`YYYY-MM-DDTHH:MM:SS` 有效时间，统一视为 UTC；所有数字位置只接受 ASCII 0 到 9，全角数字（`２０２６`）、阿拉伯印度数字（`٢٠٢٦`）及混排（`２026`）一律拒绝，不自动转换、裁剪或补零；不接受时区后缀或小数秒

额外字段被忽略（即使其值含未配对代理转义也不校验）；空白行被忽略但计入物理行号。任一行非法（非法 JSON、非对象、缺失字段、字段类型或取值不符、timestamp 的数字位置出现非 ASCII 数字、user_id 含未配对代理码点，或该物理行的字节不是合法 UTF-8）则整次导入失败：退出码 2、标准输出为空、标准错误给出输入路径、首个错误的物理行号与原因（timestamp 错误指出字段名及“只接受 ASCII 数字”；user_id 代理码点以 `\uXXXX` 转义形式回显），数据库已有记录不变、目标库原本不存在时不创建文件。各类错误按物理行顺序只报告首个，更早行的非法 JSON、字段错误或非法 UTF-8 字节保留原有原因；不替换字节、不跳过坏行、不尝试其他编码；LF 与 CRLF 文件同样处理，末行没有换行符时仍能正确定位。

## 输出与退出码

- 导入成功：退出码 0，标准输出仅 `{"imported": N}`（本次有效记录数）
- 报告成功：退出码 0，标准输出仅 `{"visit_users": V, "converted_users": C, "conversion_rate": R}`；加 `--include-users` 时追加 `"visit_user_ids"` 与 `"converted_user_ids"` 两个字符串数组；加 `--include-pairs` 时追加 `"conversion_pairs"` 配对明细数组；加 `--include-latency` 时在**顶层**追加 `"conversion_latency"` 整体耗时对象（组内是否追加耗时由 `--include-group-latency` 单独控制）；加 `--group-by visit-date` 时追加 `"visit_date_groups"` 分组数组（各开关可合用，分组与编号同开时组对象内再带两个编号数组，分组与 `--include-group-pairs` 同开时组对象内再带配对数组，分组与 `--include-group-latency` 同开时组对象内再带组内 `conversion_latency` 耗时对象，详见统计规则）
- 输入文件无法读取、数据库无法访问、报告数据库不存在：退出码 2，标准输出为空，标准错误说明路径与原因
- `--output` 相关失败（缺少取值或空字符串、与 `--db` 同路径、父目录不存在、目标是目录、目标无法写入）：退出码 2，标准输出为空，标准错误指出输出路径（空值等参数错误同时指出 `--output`）与原因；路径类参数错误先于数据库访问，参数校验或报告查询失败时不创建或改写输出文件，保存失败时已有目标保留原内容、原本不存在的目标不留下不完整文件

## 统计规则

- 访问人数：发生过 `visit` 的用户去重计数
- 转化人数：存在严格晚于某次 `visit` 的 `signup` 的用户去重计数（相等时刻不算转化）
- `--within-seconds N`：在上述条件上追加"间隔不超过 N 秒"（恰好 N 秒计入）；同一用户多次访问中任意一次满足即可。仅接受全为 ASCII 数字且大于零的整数（允许前导零），否则退出码 2、标准输出为空、标准错误指出 `--within-seconds` 及原因，不创建数据库也不修改记录
- `--visit-from` / `--visit-before`：成对使用时，访问人数只计在该时间段（含起点、不含终点）内发生过 `visit` 的用户，按 `user_id` 原值去重，同一用户任一段内访问满足即计一人；转化只允许与段内 `visit` 配对（段外访问不参与），`signup` 可以晚于终点。与 `--within-seconds` 同时使用时，注册仍须严格晚于段内访问，间隔上界（含）继续生效。两值均须为 `YYYY-MM-DDTHH:MM:SS` 格式（UTC，不带前后空白、时区后缀或小数秒）、为有效日期且所有数字位置只接受 ASCII 0 到 9（全角、阿拉伯印度等非 ASCII 数字及混排一律拒绝，不自动转换、裁剪或补零）；缺少任一配对参数或参数值、起终点相等或逆序均退出码 2、标准输出为空、标准错误指出相关参数与原因，且先于数据库访问，不创建数据库或改动记录；即使带 `--output` 也不创建或改写报告文件
- 统计不依赖行序；重复事件与重复导入不增加人数
- `--include-users`：在汇总之外输出 `visit_user_ids` 与 `converted_user_ids`，分别列出计入访问与转化统计的用户编号。明细沿用与汇总完全相同的筛选条件（访问时段、`--within-seconds` 上界含等值、signup 严格晚于段内 visit），数组长度等于对应人数，转化数组的成员都在访问数组中。两个数组按 `user_id` 原值去重，再按 Unicode 码点字典序升序排列：区分大小写，保留编号中的空白与中文，不按数字大小排序。不传该开关时仍只输出三个汇总字段；两种模式的汇总数值一致。没有符合条件的访问时三项指标为 0、两个数组为空；有访问却无人转化时仅转化数组为空
- `--include-pairs`：在汇总之外输出 `conversion_pairs` 配对明细，用于核对每个转化用户的事件依据。明细沿用与汇总完全相同的配对条件（访问时段含起点不含终点、signup 严格晚于段内 visit、`--within-seconds` 上界含等值）。每个转化用户只出现一次：先在其全部有效配对中选时间最早的 `signup`，再从能与该注册有效配对的 `visit` 中选时间最晚的一次。每个对象只含 `user_id`、`visit_timestamp`、`signup_timestamp` 三个字段，时间戳沿用 `YYYY-MM-DDTHH:MM:SS` UTC 格式。数组按 `user_id` 原值的 Unicode 码点字典序升序排列，长度等于 `converted_users`；与 `--include-users` 合用时，数组中的编号集合等于 `converted_user_ids`。没有转化时数组为空；不传该开关时输出保持现状，该开关不改变汇总数值与编号明细
- `--include-latency`：在汇总之外输出 `conversion_latency` 整体转化耗时对象，仅含 `min_seconds`、`max_seconds`、`mean_seconds` 三个字段。耗时口径与 `conversion_pairs` 完全一致、源自同一份配对归约：每个转化用户只贡献一个耗时——先在当前筛选条件的全部有效配对中选时间最早的有效 `signup`，再选能与它配对的最晚 `visit`，以两者的 UTC 时间差（秒）计；**不是**取该用户所有配对中的最短间隔（因此筛选条件变化时，最早有效 `signup` 可能改变：某个注册在更紧的 `--within-seconds` 下若没有任何合格 visit，便不再是有效注册）。`min_seconds`、`max_seconds` 为整数秒；`mean_seconds` 为全部转化用户耗时之和除以转化人数的算术平均，真除不取整或人为舍入。时间戳均为 `YYYY-MM-DDTHH:MM:SS` 且统一按 UTC 解释，visit 严格早于 signup（同刻或逆序不转化），故耗时恒为正整数秒。无转化（含无访问）时三项均为 `null`。该开关可独立使用、不依赖 `--include-pairs`，与编号、配对、`--group-by visit-date`、访问时段及 `--within-seconds` 合用时只在**顶层**追加该对象；组内耗时由 `--include-group-latency` 单独控制，本开关不向组内追加任何字段，原有汇总、排序、归组与字段控制全部保留；不传该开关时输出保持现状，该开关不改变任何汇总数值
- `--group-by visit-date`：在汇总之外输出 `visit_date_groups` 分组数组，每个对象含 `visit_date`（UTC 日期 `YYYY-MM-DD`）、`visit_users`、`converted_users`、`conversion_rate`。每个用户只归入其最早一次**合格 visit** 的 UTC 日期组：合格访问沿用当前访问时段含起点、不含终点的筛选（无时段即全库），段外历史不参与归组；同一用户其他日期的访问不再产生第二个组。组内转化判定与汇总完全一致——可使用该用户任意一次合格 visit 配对，不限于归组那次，signup 严格晚于 visit 且允许晚于时段终点，`--within-seconds` 上界含等值。数组按日期升序，只列出有访问用户的日期（不补空日期）；各组访问人数、转化人数之和分别等于汇总人数，组内比例按组内人数真除。没有合格访问时数组为空。非法分组值或缺少取值退出码 2、标准输出为空、标准错误指出参数与原因，且不创建数据库
- `--group-by visit-date` 与 `--include-users` 同时启用时，每个日期组对象在上述四个字段后追加 `visit_user_ids` 与 `converted_user_ids`：两数组按 `user_id` 原值去重、再按 Unicode 码点升序（区分大小写，保留空白与中文，不按数字大小），排序口径与顶层两个编号数组一致；数组长度分别等于该组 `visit_users`、`converted_users`，组内转化数组是本组访问数组的子集，无转化的组返回空转化数组。各组访问编号合并后等于顶层 `visit_user_ids`、各组转化编号合并后等于顶层 `converted_user_ids`，且组间没有重复编号。只开启分组或只开启 `--include-users` 时输出字段维持现状；`--include-pairs` 不向组内追加配对
- `--include-group-pairs`：仅与 `--group-by visit-date` 合用，每个日期组对象追加 `conversion_pairs` 配对明细数组，用于按组核对配对。组内每个转化用户只出现一次，配对口径与顶层 `conversion_pairs` 完全一致（最早有效 `signup` 配对能匹配它的最晚 `visit`，三个字段与 UTC 时间格式相同，访问时段含起点不含终点、段外访问不配对、注册允许晚于终点、`--within-seconds` 上界含等值）。用户仍按最早合格 visit 归组，配对访问可以落在其他日期，不据配对时间重新归组。组内数组按 `user_id` 原值的 Unicode 码点字典序升序，长度等于本组 `converted_users`；无转化的组返回空数组，无合格访问时分组数组为空。与 `--include-pairs` 同时开启时，各组配对合并排序后与顶层数组完全一致，组间不重复用户。该开关不自动开启顶层配对或编号明细，原开关仍各自控制原字段；不传该开关时输出保持现状。单独使用（不带 `--group-by visit-date`）退出码 2、标准输出为空、标准错误指出该开关及依赖项，且先于数据库访问，不创建数据库或改动记录
- `--include-group-latency`：仅与 `--group-by visit-date` 合用，每个日期组对象追加 `conversion_latency` 组内转化耗时对象，仅含 `min_seconds`、`max_seconds`、`mean_seconds` 三个字段，用于按组核对耗时。耗时口径与顶层 `conversion_latency` 完全一致、源自同一份配对归约：组内每个转化用户只贡献一个耗时——先在当前筛选条件的全部有效配对中选时间最早的有效 `signup`，再选能与它配对的最晚 `visit`，以两者的 UTC 时间差（秒）计，**不是**取该用户所有配对中的最短间隔。用户仍按最早合格 visit 的 UTC 日期归组，配对访问可以落在后一天，不据配对时间移动所属组；访问时段含起点、不含终点，段外访问不参与，注册可晚于时段终点；注册严格晚于访问，`--within-seconds` 上界含等值。`min_seconds`、`max_seconds` 为整数秒；`mean_seconds` 为**本组**转化用户耗时之和除以**本组**转化人数的算术平均，真除不取整或人为舍入（多组并存时可与顶层均值不同）。无转化的日期组三项均为 `null`，无合格访问时分组数组为空。该开关不依赖也不自动开启顶层耗时（仍由 `--include-latency` 单独控制）、组内配对或任何编号明细，原开关仍各自控制原字段；不传该开关时既有输出保持现状，该开关不改变任何汇总数值。单独使用（不带 `--group-by visit-date`）退出码 2、标准输出为空、标准错误指出该开关及依赖项，且先于数据库访问，不创建数据库或改动记录；数据库不存在或不可访问仍退出码 2、标准输出为空、标准错误含路径及原因
- 转化比例 = 转化人数 / 访问人数；零访问时三项均为 0

## 样例

`sample.jsonl` 为 2026-10-06 的虚构数据：u1 在 10 点 visit、11 点 signup；u2 仅 10 点 visit；u3 在 9 点 signup、10 点 visit。依次执行上述两条命令，预期输出：

```json
{"imported": 5}
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

加上 `--output` 把同一份报告存入文件，标准输出仍是上面的单行报告 JSON：

```sh
python -m funnel report --db events.sqlite --output report.json
```

`report.json` 为 UTF-8 编码、以换行结束的单个 JSON 对象；解析后与标准输出完全一致：

```json
{"visit_users": 3, "converted_users": 1, "conversion_rate": 0.3333333333333333}
```

再次执行同一命令会整体覆盖 `report.json`，不会向其中追加多个报告；配合各明细开关保存的字段随之变化，空报告也可以保存（零值、空数组和 `null` 沿用现有规则）。
