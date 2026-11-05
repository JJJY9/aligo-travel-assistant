"""应用版本号。

发布新版本时需要同步修改：
- ``pyproject.toml`` 的 ``version``
- 本文件的 ``APP_VERSION``
- ``webapp/static/index.html`` 中静态资源的 ``?v=`` 查询参数
"""
APP_VERSION = "1.4.1"
