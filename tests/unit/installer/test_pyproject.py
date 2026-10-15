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
    from packaging.version import Version
    assert Version("1.6.0") in requirements["evdev"].specifier and Version("1.5.9") not in requirements["evdev"].specifier   # (1.6 builds on 3.11)


def test_mypy_does_not_ask_for_evdevs_stubs():
    """evdev is imported where the room Pi's virtual mouse is made (a room without a touchpad runs without it, and so does a machine
    that only runs the checks): mypy is told not to mind that it is not there."""
    assert {"module": "evdev", "ignore_missing_imports": True} in pyproject()["tool"]["mypy"]["overrides"]


def test_the_contributor_prerequisites_and_the_debian_control_file_ask_for_the_python_pyproject_does():
    """CONTRIBUTING's prerequisite and the .deb's Depends say 3.11, as requires-python does (the README's badge is pinned beside the README
    tests), and the Depends has what compiling evdev takes: it has no wheel, so pip builds its C extension on the device."""
    repo = PYPROJECT.parent
    contributing = (repo / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "- **Python 3.11+** installed" in contributing
    assert "Python 3.10" not in contributing
    control = (repo / "packaging" / "debian" / "control").read_text(encoding="utf-8")
    depends = control.split("\nDepends:", 1)[1].split("\nRecommends:", 1)[0]
    entries = [entry.strip() for entry in depends.replace("\n", " ").split(",")]
    for needed in ("python3 (>= 3.11)", "python3-dev", "gcc"):
        assert needed in entries, needed
    assert "python3 (>= 3.10)" not in control
