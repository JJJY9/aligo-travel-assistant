"""
部署与运维配置回归（无需 API Key、不访问网络）

- docker-compose：Postgres 健康检查走 TCP（避开 initdb 临时 unix socket），
  Redis 开启 AOF 并挂卷（删除墓碑 / 会话纪元跨重启仍有保护）；
- 依赖：PyYAML 显式声明，tzdata 不再只限 Windows（Linux 容器同样需要）；
- README：手动安装版本与 requirements.txt 一致、compose 命令带 --wait、
  健康检查按 body 的 status 判断降级、wheel 初始化入口可执行。

运行：python tests/test_deploy_configs.py
"""
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 没有 tomllib
    tomllib = None  # type: ignore[assignment]

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def _read(rel: str) -> str:
    return Path(project_root, rel).read_text(encoding="utf-8")


def _yaml(rel: str):
    return yaml.safe_load(_read(rel))


def _requirement_pins(text: str) -> set[tuple[str, str]]:
    pins: set[tuple[str, str]] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(
            r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([0-9][A-Za-z0-9_.\-]*)", line
        )
        if match:
            pins.add((match.group(1).lower(), match.group(2)))
    return pins


def test_compose_postgres_healthcheck_uses_tcp():
    compose = _yaml("docker-compose.yml")
    command = compose["services"]["postgres"]["healthcheck"]["test"]
    joined = " ".join(str(part) for part in command)
    # 不带 -h 的 pg_isready 走镜像默认 unix socket：initdb 的临时 server
    # （listen_addresses=''）也会让它返回 0，--wait 在真实 5432 就绪前返回
    assert "-h 127.0.0.1" in joined, joined
    assert "-U aligo" in joined and "-d aligo" in joined, joined
    print("✓ compose Postgres 健康检查走 TCP + 账号 / 库")


def test_compose_redis_keeps_tombstones_across_restarts():
    compose = _yaml("docker-compose.yml")
    redis = compose["services"]["redis"]
    command = [str(part) for part in redis["command"]]
    assert "--appendonly" in command, command
    assert command[command.index("--appendonly") + 1] == "yes", command
    volumes = [str(item) for item in redis.get("volumes") or []]
    assert "aligo_redisdata:/data" in volumes, volumes
    assert "aligo_redisdata" in (compose.get("volumes") or {}), compose.get("volumes")
    print("✓ compose Redis 开启 AOF 并挂卷")


def test_pyproject_declares_runtime_dependencies():
    if tomllib is None:
        pytest.skip("Python 3.10 无 tomllib；依赖清单在 Python 3.12 CI 校验")
    data = tomllib.loads(_read("pyproject.toml"))
    deps = data["project"]["dependencies"]
    names = {re.split(r"[<>=!;\[]", dep, maxsplit=1)[0].strip().lower() for dep in deps}
    assert "pyyaml" in names, "utils/skill_loader.py 顶层 import yaml，必须显式声明"
    tzdata = [dep for dep in deps if dep.lower().startswith("tzdata")]
    assert tzdata, "tzdata 必须声明"
    assert "sys_platform" not in tzdata[0], (
        "tzdata 只限 win32 会让 Linux 容器（无系统时区库）丢时区"
    )
    print("✓ pyproject 显式声明 PyYAML，tzdata 覆盖所有平台")


def test_readme_manual_pins_match_requirements():
    readme = _read("README.md")
    block = re.search(r"或者手动安装核心依赖(.*?)```", readme, re.S)
    assert block is not None, "README 缺少手动安装核心依赖代码块"
    pins = re.findall(
        r"([A-Za-z0-9_.\-]+)==([0-9][A-Za-z0-9_.\-]*)", block.group(1)
    )
    assert pins, block.group(1)
    known = _requirement_pins(_read("requirements.txt"))
    missing = [
        (name, version)
        for name, version in pins
        if (name.lower(), version) not in known
    ]
    assert not missing, f"README 手动安装版本与 requirements.txt 不一致：{missing}"
    print("✓ README 手动安装版本与 requirements.txt 一致")


def test_readme_ops_instructions_are_current():
    readme = _read("README.md")
    assert "docker compose up -d --wait" in readme, "compose 用法必须等健康检查"
    assert not re.search(r"docker compose up -d(?! --wait)", readme), (
        "存在不带 --wait 的 compose 启动命令"
    )
    assert "HTTP 状态恒为 200" in readme, "降级探测必须提示读取 body 的 status"
    assert "aligo kb-init --documents" in readme, "wheel 用户需要可执行的初始化入口"
    print("✓ README 部署 / 健康检查 / 初始化口径一致")


def test_readme_wheel_filename_not_stale():
    readme = _read("README.md")
    from utils.version import APP_VERSION

    versions = re.findall(
        r"aligo_travel_assistant-(\d+\.\d+\.\d+)-py3-none-any\.whl", readme
    )
    for version in versions:
        assert version == APP_VERSION, (
            f"README 硬编码的 wheel 版本 {version} 与 APP_VERSION {APP_VERSION} 不一致"
        )
    print("✓ README 不含过期 wheel 文件名")


def test_ci_postgres_healthcheck_uses_tcp():
    ci = _yaml(".github/workflows/ci.yml")
    options = str(ci["jobs"]["quality"]["services"]["postgres"]["options"])
    # 与 docker-compose 同口径：不带 -h 的 pg_isready 走 initdb 临时 socket，
    # 服务真实监听 5432 前就会返回 0；不带 -d 也不校验目标库存在
    assert "-h 127.0.0.1" in options, options
    assert "-U aligo" in options and "-d aligo" in options, options
    print("✓ CI Postgres 健康检查走 TCP + 账号 / 库（与 compose 一致）")


def test_ci_wheel_check_runs_outside_checkout():
    text = _read(".github/workflows/ci.yml")
    # `python -c` 会把当前目录放进 sys.path[0]：在仓库根执行只会导入
    # checkout 里的源码，wheel 缺 cli.py / static / 技能时仍然全绿
    assert 'cd "$(mktemp -d)"' in text, "打包检查必须在仓库外目录导入安装产物"
    assert "aligo --help" in text, "cli 入口需要在 CI 中执行一次"
    print("✓ CI 打包检查在仓库外验证安装产物")


def test_release_artifact_check_matches_sdist_name():
    text = _read(".github/workflows/release.yml")
    # sdist 名是 <name>-<version>.tar.gz（版本后直接 .tar.gz），与 wheel 的
    # <name>-<version>-py3-none-any.whl 不能共用带尾随连字符的前缀
    assert 'wheel_prefix = f"aligo_travel_assistant-{tag}-"' in text, text
    assert 'sdist_name = f"aligo_travel_assistant-{tag}.tar.gz"' in text, text
    print("✓ Release 产物校验区分 wheel / sdist 文件名")


def test_readme_manual_block_covers_storage_backends():
    readme = _read("README.md")
    block = re.search(r"或者手动安装核心依赖(.*?)```", readme, re.S)
    assert block is not None, "README 缺少手动安装核心依赖代码块"
    assert "redis==8.1.0" in block.group(1), (
        "手动安装清单缺 redis：启用 REDIS_URL 后会静默回退进程内实现"
    )
    assert "psycopg[binary]==3.3.9" in block.group(1), (
        "手动安装清单缺 psycopg：启用 DATABASE_URL 后会静默回退 JSON"
    )
    print("✓ README 手动安装清单覆盖生产存储后端")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    for _name, _func in sorted(globals().items()):
        if _name.startswith("test_") and callable(_func):
            _func()
    print("\n全部通过")
