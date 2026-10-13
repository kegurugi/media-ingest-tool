import pytest

from media_ingest.cli import main


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "ingest" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["verify", "X"], ["log", "export"]])
def test_commands_are_stubbed(argv, capsys):
    assert main(argv) == 2
    assert "not implemented yet" in capsys.readouterr().err


def test_ingest_reports_config_errors(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "missing.json"), "ingest"]) == 2
    assert "ERROR: Project config not found" in capsys.readouterr().err
