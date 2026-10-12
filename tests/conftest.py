import json
from pathlib import Path

import pytest

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "project.example.json"


@pytest.fixture
def example_config_dict():
    return json.loads(EXAMPLE_CONFIG.read_text())


@pytest.fixture
def drives(tmp_path):
    """Two mounted destination drives."""
    a = tmp_path / "SHOW01_DRV01A"
    b = tmp_path / "SHOW01_DRV01B"
    a.mkdir()
    b.mkdir()
    return [a, b]
