"""不依赖真实数据库的轻量回归断言。

注意：导入 dmAsync 会导入 dmPython；未安装 dmPython 时整体跳过。
"""
import importlib.util
import inspect
import unittest

HAS_DMPYTHON = importlib.util.find_spec("dmPython") is not None


@unittest.skipUnless(HAS_DMPYTHON, "dmPython 未安装，跳过")
class DefaultsTest(unittest.TestCase):
    def test_login_timeout_default_is_30(self):
        import dmAsync

        self.assertEqual(
            inspect.signature(dmAsync.connect).parameters["login_timeout"].default,
            30,
            "补丁 1：connect() 的 login_timeout 默认值应为 30",
        )
        self.assertEqual(
            inspect.signature(dmAsync.Connection.__init__)
            .parameters["login_timeout"]
            .default,
            30,
            "补丁 1：Connection.__init__ 的 login_timeout 默认值应为 30",
        )

    def test_async_timeout_declared(self):
        # pool.py 运行时 import async_timeout，未安装时给出明确信号
        self.assertIsNotNone(
            importlib.util.find_spec("async_timeout"),
            "补丁 2：需要 async_timeout 依赖",
        )

    def test_cursor_echo_false_positional(self):
        # 补丁 4：Connection._cursor 传给 Cursor 的第 4 个位置参数应为 False（echo），
        # isolation_level 归位命名参数；否则隔离级别会被丢弃并污染 echo。
        from dmAsync import connection as conn_mod

        src = inspect.getsource(conn_mod.Connection._cursor)
        self.assertIn(
            "Cursor(self, impl, timeout, False, isolation_level)",
            src,
            "补丁 4：应显式传 echo=False，避免 isolation_level 落入 echo 槽",
        )


if __name__ == "__main__":
    unittest.main()