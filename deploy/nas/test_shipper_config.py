"""The optional exporter requires owner configuration and cannot ship by default."""
from pathlib import Path
import yaml

ROOT = Path(__file__).parent

def test_exporter_is_opt_in_and_uses_separate_credentials():
    service = yaml.safe_load((ROOT / "shipper.compose.yml").read_text())["services"]["ava-shipper"]
    assert ":?" in service["image"]
    assert "AVA_EXPORTER_ENV:?" in service["env_file"][0]
    assert service["volumes"][0].endswith(":/src:ro")
    assert "ports" not in service

def test_example_exports_nothing_and_verifies_tls():
    config = yaml.safe_load((ROOT / "shipper.yaml").read_text())
    assert config["streams"] == []
    assert config["s3"]["verify_tls"] is True
    assert config["clickhouse"]["verify_tls"] is True
