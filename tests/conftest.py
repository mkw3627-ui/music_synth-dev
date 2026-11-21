"""pytest 收集入口（S2 新增）。

主要职责：
1. 把 ``tests/`` 加进 ``sys.path``，使 ``from dl_harness.fake_mirrors import ...``
   可用——在 ``--import-mode=importlib``（见 ``tests/pytest.ini``）下 pytest 不会把
   ``tests/`` 放进路径，所以必须在这里显式加。
2. 提供 ``mirrors`` 夹具工厂（见下）。

安全网：仓库根目录的 ``__init__.py`` 是插件主模块，会 ``from plugin.sdk.plugin import ...``
（宿主 SDK，独立环境不存在）且目录名带连字符；默认 prepend 导入模式会把它当模块
``__init__`` 导入而报错。``tests/pytest.ini`` 已用 ``--import-mode=importlib`` 规避，
这里再预置一个空壳 ``sys.modules['__init__']``，即使有人不带该配置运行（如
``pytest -c /dev/null``）也能收集成功。
"""
from __future__ import annotations

import sys
import types
from importlib import import_module
from pathlib import Path

# 在所有业务 import 之前先把路径备好：`dl_harness` 在 tests/ 下，
# 插件根目录要能 import 到（`--import-mode=importlib` 不会自动加）。
_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 安全网：短路根插件 __init__.py 的导入（见模块 docstring）。
if "__init__" not in sys.modules:
    _dummy = types.ModuleType("__init__")
    _dummy.__file__ = "<music_synth-dev placeholder (pytest) — real plugin not loaded>"
    _dummy.__package__ = ""
    sys.modules["__init__"] = _dummy

pytest = import_module("pytest")
_fake = import_module("dl_harness.fake_mirrors")
MirrorCluster = _fake.MirrorCluster
serve_mirrors = _fake.serve_mirrors


@pytest.fixture
def mirrors():
    clusters: list[MirrorCluster] = []

    def _make(specs=1, **cfg) -> MirrorCluster:
        cluster = serve_mirrors(specs, **cfg)
        clusters.append(cluster)
        return cluster

    yield _make

    for cluster in clusters:
        cluster.stop()
