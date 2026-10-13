import pytest

from media_ingest.cli import main


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "ingest" in capsys.readouterr().out


def test_ingest_reports_config_errors(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "missing.json"), "ingest"]) == 2
    assert "ERROR: Project config not found" in capsys.readouterr().err
