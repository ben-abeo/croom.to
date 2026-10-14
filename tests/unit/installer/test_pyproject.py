"""pyproject.toml: what the tools are told matches what the package needs and supports."""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def pyproject():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_tools_target_the_oldest_python_the_package_supports():
    """The TV service uses asyncio.timeout, which is Python 3.11: that is what the package asks for, and what mypy, ruff and black
    are told to expect (a target below it has them pass code that does not run on the oldest Python allowed)."""
    config = pyproject()
    assert config["project"]["requires-python"] == ">=3.11"
    assert config["tool"]["mypy"]["python_version"] == "3.11"
    assert config["tool"]["ruff"]["target-version"] == "py311"
    assert "py310" not in config["tool"]["black"]["target-version"]


def test_the_tv_and_touchpad_libraries_are_dependencies_and_evdev_only_where_linux_has_it():
    """aiowebostv talks to the TV, wakeonlan wakes it, and evdev makes the room Pi's virtual mouse (it builds only on Linux)."""
    from packaging.requirements import Requirement
    requirements = {requirement.name: requirement for requirement in map(Requirement, pyproject()["project"]["dependencies"])}
    assert {"aiowebostv", "wakeonlan", "evdev"} <= set(requirements)
    assert requirements["aiowebostv"].marker is None and requirements["wakeonlan"].marker is None
    marker = requirements["evdev"].marker
    assert marker is not None and marker.evaluate({"sys_platform": "linux"}) and not marker.evaluate({"sys_platform": "darwin"})
