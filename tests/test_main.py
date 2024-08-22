"""Test cases for the __main__ module."""

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    """Fixture for invoking command-line interfaces."""
    return CliRunner()


class TestCLI:
    """Test cases for the command-line interface."""

    def test_main_succeeds(self, runner: CliRunner) -> None:
        return


__all__ = ("TestCLI",)
