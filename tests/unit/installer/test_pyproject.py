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
