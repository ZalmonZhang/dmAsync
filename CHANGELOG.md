# Changelog

本仓库为上游 PyPI 包 `dmasync`（无公开源码仓库）的补丁快照。

## 1.0.0.post2

- `dmAsync/connection.py` `Connection._cursor`：`Cursor(conn, impl, timeout, echo, isolation_level=None)`
  的位置参数错位——原实现按位置传入 `isolation_level`，落进了 `echo` 槽，导致**隔离级别被丢弃**
  且 `cursor.echo` 被污染成隔离级别对象。现显式传 `echo=False`，`isolation_level` 归位。
  真实 DM8 实测：修复后 `cursor.echo is False`、`isolation_level=read_committed` 正确进入 `Transaction`。
  影响面：SQLAlchemy 走 `dialect.set_isolation_level`，不经此路径；该 bug 主要影响直接使用 `dmAsync` 的用户。

## 1.0.0.post1

- `dmAsync/connection.py`：`connect()` 与 `Connection.__init__` 的 `login_timeout` 默认值 `5` → `30`。
  真实 DM8 实测：默认 5s 时约 9~10% 的新建物理连接报 `-70019 网络通讯失败`；改为 30s 后 60/60。
- `dmAsync/connection.py`：删除 `Connection.__init__` 中重复的 `self._waiter = ...create_future()`。
- 包元数据：补充 `Requires-Dist: async_timeout`（`dmAsync/pool.py` 运行时依赖，原元数据漏声明）。

## 1.0.0

- 上游初始版本（作为本补丁的基线）。