# dmAsync（补丁快照）

> **非官方仓库。** `dmAsync` 由达梦以 PyPI 包 `dmasync` 的形式分发，**没有公开源码仓库**。
> 本仓库是 `dmasync 1.0.0`（`dmAsync/`，纯 Python，约 12KB）在真实 DM8 上排查出若干
> 缺陷后形成的**补丁快照（1.0.0.post1）**，用于自带与向上游反馈，不代表官方。
> 官方分发：<https://pypi.org/project/dmasync/>

## 这是什么

`dmAsync` 是 dmPython 的 asyncio 兼容层（本质把 dmPython 的同步 API 用 `asyncio.to_thread`
包一层），`dmSQLAlchemy` 的异步方言 `dm+dmAsync://` 依赖它。

由于上游无源码仓库，本仓库直接保存补丁后的完整包源码，便于：

1. **直接使用**：`pip install .` 得到修好的 `dmAsync 1.0.0.post1`（等价于产物 wheel）。
2. **向上游反馈**：`patches/` 提供可 `git apply` 的补丁，`docs/` 提供逐条实测证据。
3. **回归**：`tests/` 提供不依赖真实数据库的默认值断言。

## 修复内容（1.0.0 → 1.0.0.post1）

| # | 问题 | 位置 | 改动 |
|---|---|---|---|
| 1 | **建连间歇性 `-70019 网络通讯失败`**：`login_timeout` 默认 5s 对本环境 DM 登录握手偏紧（主因，并发只是放大器；纯裸 `dmPython` 串行也复现） | `dmAsync/connection.py` `connect()` 与 `Connection.__init__` | 默认 **5 → 30** |
| 2 | **漏声明依赖**：`dmAsync/pool.py` `import async_timeout`，但包元数据只声明 `dmPython`，新环境 `import dmAsync` 即 `ModuleNotFoundError` | 包元数据 | 补 `Requires-Dist: async_timeout` |
| 3 | **重复创建 future**：`Connection.__init__` 连续两次 `self._waiter = ...create_future()`（前者被覆盖、泄漏） | `dmAsync/connection.py` | 删除重复的一次 |

补丁见 [`patches/0001-default-login-timeout-30-and-dedup-waiter.patch`](patches/0001-default-login-timeout-30-and-dedup-waiter.patch)。

### 实测（真实 DM8 容器，每项 60 次全新物理连接）

| 场景 | 结果 |
|---|---|
| 裸 `dmPython` 串行 `login_timeout=5` | 59/60 |
| async 串行新建引擎 `lt=5` | 58/60 |
| async 并发新建引擎 `lt=5`（gather 60） | 50/60 |
| async 串行新建引擎 `lt=30` | **60/60** |
| 复用同一 engine 连接池 `lt=5`（400 次） | 0 失败 |

> 连接池复用会掩盖问题：**只在新建物理连接时暴露**。生产建议仍显式传
> `connect_args={"login_timeout": 30}`，并做连接预热 / 控制建连并发 / 失败重试。

## 安装

```bash
pip install .
# 或在项目里固定来源：
# dmasync @ git+https://<your-host>/dmAsync.git
```

也可自行构建 wheel：

```bash
python -m build          # 产物：dist/dmAsync-1.0.0.post1-py3-none-any.whl
```

依赖：`dmPython`、`async_timeout`，Python ≥ 3.8。

## 用法（与上游一致）

```python
import dmAsync

conn = dmAsync.connect(user="SYSDBA", password="******",
                       server="localhost", port=5236,
                       login_timeout=30)   # 默认已是 30，显式传更稳
```

## 目录

| 路径 | 说明 |
|---|---|
| `dmAsync/` | 补丁后的完整包源码（`connection.py` / `pool.py` / `utils.py` / `log.py` / `__init__.py`） |
| `patches/` | 相对上游 1.0.0 的补丁（`git apply` 可用） |
| `docs/dmAsync-坑点审计.md` | 异步链路坑点审计（含真实库复核与实证矩阵） |
| `tests/` | 默认值与依赖的轻量回归断言 |

## 已知限制（本快照未修）

`dmAsync` 仍存在若干设计层面的问题（`fetchone/fetchall` 绕过 `to_thread`、服务端游标死代码、
`Connection._cursor()` 位置参数错位、`dsn` 被丢弃等），详见审计文档。它们改动侵入性大，
未纳入本补丁。

## 与 dmSQLAlchemy 的关系（重要）

本仓库**只修 `dmAsync` 自身**（`login_timeout` 默认值、`async_timeout` 依赖声明、重复 future），
**不包含任何 dmSQLAlchemy 方言侧的修复**。若你在使用 `dm+dmAsync://` 时遇到下表现象，
需要的是 dmSQLAlchemy 的补丁（见 [`docs/dmAsync-坑点审计.md`](docs/dmAsync-坑点审计.md) §7、§8），
**升级 `dmAsync` 本身不会解决**：

| 现象 | 归属 | dmSQLAlchemy 侧修复 |
|---|---|---|
| 异步建连即报 `attribute ... is read-only` | 方言 | 删除 `AsyncAdapt_*` 对基类 `__slots__` 的遮蔽声明（post1） |
| 方言 `arraysize` 被吞 / `encoding_errors` 污染 / `connection_timeout` 报 `TypeError` | 方言 | `DMDialect_dmAsync.__init__` 改关键字传参（post2） |
| `text()` + `executemany` 批量报 `'TextClause' object has no attribute 'table'` | 方言 | executemany 守卫（post2） |
| 原生 `JSON` 列读回是 `str` 而非 `dict` | 方言 | `json_proc_decorator` 补 `json.loads`（post2） |
| ORM `session.add_all([...])` 同步 `FlushError` / 异步 `TypeError` | 方言 | `insert_executemany_returning=False` + 清理死分支（post3/post4） |

## 许可证

沿用上游 `dmAsync` 的 **Mulan PSL v2**（见 [`LICENSE`](LICENSE)）。本仓库仅为补丁再分发。