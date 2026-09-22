# dmAsync / dmSQLAlchemy 异步链路坑点审计

> 审计对象：dmSQLAlchemy 2.0.17（`dm+dmAsync://` 方言）+ dmAsync 1.0.0（PyPI `dmasync-1.0.0-py3-none-any.whl`）
> 方法：解包源码走查 + 假数据库端到端 harness 实证（stub dmPython + 真实 dmAsync + 真实 dmSQLAlchemy + SQLAlchemy 2.0.54）
> 复核：2026-09-22 用**真实 DM8 容器**（`zmaster-dm8`，`localhost:5236`）逐条复测，结论见 §6
> 日期：2026-09-22

## 0. 先回答"开源吗"

| 层 | 来源 | 开源? |
|---|---|---|
| SQLAlchemy 方言层 `dm+dmAsync` | `DamengDB/dmSQLAlchemy` → `dmSQLAlchemy2.0/dmSQLAlchemy/dmasync.py` | ✅ 开源（就是我们之前修 slot bug 的仓库） |
| **`dmAsync` 异步驱动** | PyPI `dmAsync`（包名 dmasync，1.0.0，**12KB 纯 Python**） | ✅ 有得下、可直接解包读（本报告已全文读完） |
| `dmPython` 同步驱动 | PyPI + GitHub `DamengDB/dmPython` | ✅ 开源 |
| 达梦客户端 `libdmdpi` | wheel 内 `dmpython.libs/` | ❌ 闭源（-70089 加密库加载就在这一层） |

`dmAsync` 结构：`connection.py`(890行) + `pool.py` + `utils.py`。本质是把 dmPython 同步 API 用
`asyncio.to_thread` 包了一层（风格类似 asyncmy/psycopg 的移植）。

## 1. 你发现的 login_timeout 问题 —— 根因与正确用法（已实证）

**默认 5 秒的来源**：`dmAsync/connection.py` L38-41 与 L566：

```python
def connect(host=None, ..., login_timeout=5, loop=None, **kwargs):
```

不传 `login_timeout` 时 dmSQLAlchemy 的 opts 里没有这个键 → 落到 dmAsync 默认 **5**。
harness 实证：`connect_args={"login_timeout": 30}` → `dmPython.connect(..., login_timeout=30)` ✓；
不传 → `login_timeout: 5` ✓。

**为什么默认 5 秒会踩（已用真实库修正归因）**：
`dmAsync.Connection.__init__` → `init_connect_with_param` 里 **`dmPython.connect()` 是同步阻塞调用**，
且发生在协程里 → **TCP 建连 + 登录直接阻塞事件循环线程**（只有 execute/fetch 走 `asyncio.to_thread`）。
这会让并发建连被**串行化并加剧**问题，**但它不是主因**：真实库实测**纯裸 `dmPython` 串行连接**
（无 asyncio、无连接池、无队列）在 `login_timeout=5` 下同样复现 `-70019`（59/60，早前一轮 137/150）。
因此主因是本环境下 **5 秒对 DM 登录握手本身偏紧**，并发只是放大器（并发 50/60 vs 串行 58/60）。
`login_timeout=30` 实测 60/60。生产建议：显式放宽 `login_timeout` + 预热连接池 + 控制建连并发 +
建连失败重试；不要依赖默认值。

> ⚠️ 注意：因为建连是阻塞的，`asyncio.gather` 并不会真正并行建连，而是把它们排在事件循环上，
> 所以"并发建连"在本驱动下本身就会自伤，进一步支持"控制建连并发"的建议。

**三种传法速查**：

| 传法 | 结果 |
|---|---|
| `create_async_engine(url, connect_args={"login_timeout": 30})` | ✅ 生效（你的做法，正确） |
| `create_async_engine("dm+dmAsync://.../?login_timeout=30")` | ✅ 生效（URL query，实验确认） |
| `create_async_engine(url, login_timeout=30)` | ❌ `TypeError: Invalid argument(s) 'login_timeout' sent to create_engine()` |

## 2. 坑点清单

### 🔴 高危

| # | 坑 | 证据 | 应对 |
|---|---|---|---|
| H1 | **`text()` 批量 executemany 直接崩**：`do_executemany` → `parse_module.async_do_executemany_return` 访问 `context.invoked_statement.table`，TextClause 没有 `.table` → `AttributeError` | harness P2 实测 | 批量只用 **Core `insert()`**（P5 实测 OK）；或循环单条 execute。`pandas.to_sql` 用 Core insert 可用 |
| H2 | **`async_do_executemany_return` 无条件假定是 INSERT**：`else` 分支直接取 `context.invoked_statement.table` 并拼 `RETURNING ... INTO ?` | 源码 `extensions.py` L184-198；真实库实测 | ⚠️ 实测 `text()` 的 UPDATE/DELETE 批量会在 `.table` 处**先崩**（与 H1 同一个 `AttributeError`），并非"改写成非法 SQL"。只有 Core + RETURNING（`out_parameters` 非空）才会走到拼 SQL 分支，该分支未实证。总之批量只用于 INSERT |
| H3 | **建连同步阻塞事件循环** + `login_timeout` 默认 5s → `-70019`；**主因是 5s 偏紧，不是排队**（裸连接串行也复现） | dmAsync `connection.py` L38/L566 + 真实库串行/并发/裸连接对照（§6） | 显式 `connect_args={"login_timeout":30}`（实测 60/60）+ 预热池 + 控制建连并发 + 建连重试 |
| H4 | **dmAsync 漏声明依赖 `async_timeout`**：`dmAsync/pool.py` import 它，METADATA 只写了 `dmPython` → 新环境 `import dmAsync` 即 `ModuleNotFoundError` | 实测踩到 + METADATA | 部署时显式 `pip install async_timeout`，写进 requirements |

### 🟠 中危

| # | 坑 | 证据 | 应对 |
|---|---|---|---|
| M1 | **`arraysize` 作为 dialect kwarg 被静默吞**：传 `arraysize=100`，`dialect.arraysize` 恒为 50（`DMDialect_dmAsync.__init__` 调父类时**位置参数错位**：`super().__init__(al, ct, arraysize, encoding_errors)` 对应父类 `(al, ct, autocommit, connection_timeout, arraysize=50)`） | harness/实验实测（传100得50；同步方言对照得100） | 不依赖 dialect-level arraysize；要设就在游标级设 |
| M2 | **`encoding_errors` kwarg 污染 `connection_timeout`**：传 `encoding_errors='strict'` → `dialect.connection_timeout='strict'` → 被塞进 `dmPython.connect(connection_timeout='strict')` | 实验实测 | 别传 `encoding_errors` |
| M3 | **`connection_timeout`/`autocommit` 作为 dialect kwarg 直接 TypeError**（`got multiple values for argument`，同 M1 错位所致） | 实验实测 | 走 `connect_args` 或 URL query |
| M4 | **服务端游标是死代码**：`DMExecutionContextAsync_dmasync` 里 L1456 的 `create_cursor = default.DefaultExecutionContext.create_cursor` 被 L1473 的 `def create_cursor` 覆盖 → `_is_server_side` 恒 False | 源码 L1455-1479 + 真实库实测 | `execute(stmt.execution_options(stream_results=True))` 能跑但**客户端全量缓冲**（实测 `_is_server_side=False`）；**`AsyncConnection.stream(...)` 直接 `AssertionError: assert result.context._is_server_side`**。大结果集自己分页/`fetchmany`，不要用 `conn.stream()` |
| M5 | **`fetchone`/`fetchall` 走同步 `self._cursor.raw.fetchone()/fetchall()`**（绕过 dmAsync 的 to_thread），大结果集在事件循环线程上同步拉取 | dmasync.py L1330-1348 源码 | 大结果集分批 fetchmany（fetchmany 也没走 to_thread……同文件，量力而行）；或限流 |
| M6 | **事件循环亲和**：dmAsync.Connection 创建时 `get_running_loop()` 绑定 loop 并在其上建 future | dmAsync connection.py L541-550 | 引擎/连接别跨 loop 复用（pytest-asyncio function-scoped loop、FastAPI 多 worker 场景注意） |
| M7 | `is_disconnect` 把 **-70019** 归类为断连自动重置/重试——与 Linux 加密库问题（-70019 表象）叠加时会造成"静默重试后才报错"的迷惑行为 | dmpython.py L507 | 用 dmsslshim 治好加密库加载，排除干扰 |

### 🟡 低危 / 脆弱点

- **`AsyncConnection._connect` 的 `Connection(*kwargs)` 用 dict 的**键**当位置参数**（垃圾赋值但暂无害）；connect_args 超过 16 个键会 `TypeError: too many positional arguments`。传参多时悠着点。
- dmAsync `Connection._cursor()` 里 `Cursor(self, impl, timeout, isolation_level)` **位置错位**（isolation_level 落进 `echo` 槽），当前 SQLAlchemy 路径恰好不传才无害。
- `dsn` 参数被 dmSQLAlchemy 的 `AsyncConnection` **静默丢弃**（靠 host+port 兜底）；只传 dsn 不传 host 的用法会连到 localhost。
- `user=""` 静默变成 `SYSDBA`；`password=None` 报错但 `password=""` 放行。
- TPC/2PC、`cancel`、`reset`、`set_session` 全是 `NotImplementedError`（SQLAlchemy 两阶段提交路径不可用）。
- 名称归一化分支（`str_case_sensitive=True` 库）里 `normalize_name` 对非 str 直接 `AttributeError`，无类型防护（stub 实测踩到；正常库返回字符串不触发）。
- 每次建连固定多跑 3-4 条 SQL（`SELECT MODE$ FROM V$instance`、`SET_SESSION_IDENTITY_CHECK`、两次 `SELECT USER FROM DUAL`）+ 每次 reset 一次 rollback + 一条事务隔离级别查询——池化场景的固定开销，压测时记得算进去。
- `Connection.__init__` 重复 `create_future()` 两遍；对象泄漏时报 `Unclosed connection` ResourceWarning 噪音。

## 3. 实测矩阵（harness）

| 场景 | 结果 |
|---|---|
| P1 单条 `text()` SELECT | ✅ 正常（隐式"二次 await"契约成立，无协程泄漏警告） |
| P2 `text()` + executemany 批量 | ❌ `AttributeError: 'TextClause' object has no attribute 'table'` |
| P3 `stream_results=True` | ⚠️ 跑通但按客户端缓冲执行，服务端游标未生效 |
| P4 `isolation_level="READ COMMITTED"` | ✅ 正常（`SET SESSION CHARACTERISTICS ...` 正确执行） |
| P5 Core `insert()` + executemany | ✅ 正常（RETURNING 重写路径） |
| login_timeout 传递 | ✅ connect_args=30 → `dmPython.connect(login_timeout=30)`；缺省 → 5 |

## 4. 附：一个能跑但要知道的隐式契约

`AsyncAdapt_dmasync_cursor._execute_async` 里 `self._cursor.execute(...)` 返回的**协程不在该层 await**，
而是向上返回、由方言 `do_execute` 的 `await_only(cursor.execute(...))` 二次接住。当前组合下能正常工作
（实测无 "coroutine never awaited"），但这是 dmSQLAlchemy 与 SQLAlchemy 基类之间**没有文档的私有契约**——
SQLAlchemy 升级改变 `AsyncAdapt_dbapi_cursor.execute` 的消费方式时会静默坏掉。升级 SQLAlchemy 版本后
建议重跑一遍本 harness。

> ⚠️ 复核备注：原文引用的 harness 路径 `.openclaw/tmp/e2e_async_test.py` **当前不存在**，需补回或重建；
> 且该 harness 是 **stub dmPython 假库**，涉及时序/并发/超时的结论不可靠（本次 H3 归因即被真实库推翻）。
> 建议：凡时序/并发类结论，一律强制在真实 DM 上复测（见 §6 脚本 `verify.py` / `--stress`）。

## 5. 建议

1. requirements 显式加 `async_timeout`；`login_timeout` 一律显式传（connect_args），别依赖默认。
2. 批量写入统一走 Core `insert()`；禁止 `text()` + 参数列表批量。
3. 建连并发做预热/排队控制（H3）。
4. 向上游（DamengDB/dmSQLAlchemy issues + dmAsync 包）反馈：M1-M3 的位置参数错位、H1/H2 的
   executemany 假设、H4 的依赖漏声明，都是可以直接提 PR 的小改动。

## 6. 真实库复核证据（2026-09-22，DM8 容器 `zmaster-dm8`）

环境：dmpython 2.5.38 / dmasync 1.0.0 / **dmSQLAlchemy 2.0.17.post1** / sqlalchemy 2.0.54，Python 3.14。
入口：`tmp/testdameng/` 下 `uv run python verify.py`（能力矩阵）与 `uv run python verify.py --stress N`（连接稳定性）。

### 6.1 结论修正
- **H3**：主因修正为"`login_timeout=5` 对本环境 DM 登录握手偏紧"，**并发只是放大器**；无 asyncio 的裸串行也复现，故"并发排队超时"不成立。
- **H2**：`text()` 路径实测与 H1 **同一个** `AttributeError: 'TextClause' object has no attribute 'table'`，并非"改写成非法 SQL"。
- **M4**：`conn.stream(...)` 实测**直接 `AssertionError`**，不是"跑通但缓冲"；只有 `execute(stmt.execution_options(stream_results=True))` 才是"跑通但客户端全量缓冲"。

### 6.2 H3 根因对照（每项 60 次**新建物理连接**）
| 场景 | 结果 |
|---|---|
| 裸 dmPython 串行 `login_timeout=5` | 59/60（更早一轮 137/150 ≈ 9% 失败） |
| async 串行新建引擎 `lt=5` | 58/60 |
| async 并发新建引擎 `lt=5`（gather 60） | 50/60 |
| async 串行新建引擎 `lt=30` | **60/60** |
| 复用同一 engine 连接池 `lt=5`（400 次） | 0 失败 |

→ 无 asyncio、无队列的裸串行也复现 ⇒ 主因不是排队；连接池复用会掩盖问题，坑只在**新建物理连接**时暴露。

### 6.3 其它断言实测
| 断言 | 真实库结果 |
|---|---|
| H1 `text()` + executemany | ❌ `AttributeError: 'TextClause' object has no attribute 'table'` |
| H1 绕过 Core `insert()` executemany | ✅ rowcount=2 |
| H4 METADATA 漏 `async_timeout` | ✅ `dmasync-1.0.0.dist-info/METADATA` 仅 `Requires-Dist: dmPython` |
| M1 `arraysize=100` → async dialect | ✅ `dialect.arraysize=50`（sync 对照 `100`） |
| M2 `encoding_errors='strict'` | ✅ `dialect.connection_timeout='strict'` |
| M3 `connection_timeout`/`autocommit` dialect kwarg | ✅ `TypeError: ... got multiple values for argument` |
| M4 `execution_options(stream_results=True)` | ✅ 能跑，`_is_server_side=False` / `server_side=False` |
| M5 fetch 绕 `to_thread` | ✅ 源码 `dmasync.py` L1326-1341 `self._cursor.raw.fetchall()` |
| M7 `is_disconnect` 含 `-70019` | ✅ `dmpython.py` L518 |
| 参数传递探针（patch `dmPython.connect` 抓参） | ✅ 默认 `login_timeout=5, autoCommit=False, connection_timeout=0`；`connect_args` 与 URL query 均生效为 30 |

## 7. 本地源包补丁清单（post 版 wheel）

构建方式：`uvx --from wheel wheel unpack` → 改源码 → `wheel pack`，并清理 `INSTALLER/REQUESTED/direct_url.json` 等安装残留。
产物：`dmasync-1.0.0.post1-py3-none-any.whl`、`dmSQLAlchemy-2.0.17.post2-py3-none-any.whl`。

### 7.1 `dmAsync` 1.0.0 → 1.0.0.post1
| 坑 | 文件 | 改动 |
|---|---|---|
| H3 | `dmAsync/connection.py` | `connect()` / `Connection.__init__` 的 `login_timeout` 默认 **5 → 30** |
| H4 | `dmasync-1.0.0.post1.dist-info/METADATA` | 补 `Requires-Dist: async_timeout` |
| 低危 | `dmAsync/connection.py` | 删除重复的 `self._waiter = ...create_future()` |

### 7.2 `dmSQLAlchemy` 2.0.17.post1 → 2.0.17.post2
| 坑 | 文件 | 改动 |
|---|---|---|
| M1/M2/M3 | `dmSQLAlchemy/dmasync.py` | `DMDialect_dmAsync.__init__` 的 `super().__init__(...)` 改为**关键字传参**，`arraysize` 显式落默认 50；`encoding_errors`/`thick_mode` 不再透传（消除位置错位与污染） |
| H1/H2 | `dmSQLAlchemy/extensions.py` | `do_executemany_return` 与 `async_do_executemany_return` 增加守卫：非 Core INSERT（无 `.table`）的 executemany 走普通路径，不再拼 RETURNING |
| JSON（本审计外，同期发现） | `dmSQLAlchemy/extensions.py` | `NoCompatible_Mode.json_proc_decorator` 补上 `json.loads`（原生 JSON 读回 dict） |

**post 版真实库实测**：H1/H2 sync+async 的 text INSERT/UPDATE/DELETE executemany 全通；M1 `arraysize=100 → 100`、M2 不再污染 `connection_timeout`、M3 不再 `TypeError`；原生 JSON 读回 `dict`；默认 `login_timeout=30`，全新异步引擎 30/30。
`verify.py` 从 PASS 19/23 提升到 **PASS 21/23**（仅剩 C2/D4）。

### 7.3 未纳入补丁（需评估，暂不改）
- **C2/D4 ORM 单次 flush 多行 `add_all`**：卡在 RETURNING 批量回填（`insertmanyvalues` / `out_parameters` 路径），改动面大、风险高；继续用"逐条 add/commit"或 Core executemany。
- **M4 服务端游标**：`create_cursor` 覆盖可修，但 `AsyncConnection.stream()` 依赖 `_is_server_side`，需整体验证，未改。
- **M5 `fetchone/fetchall` 走同步 raw**：属 AsyncAdapt 层设计，改动侵入性大。
- **低危若干**（`_connect` 的 dict 位置参数、`dsn` 被丢弃、`Cursor(...)` 位置错位）：未动。
