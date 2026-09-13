from unittest.mock import patch
import pytest
from ava_bridge import provision_material as material


def test_material_roundtrip_and_retirement_are_scoped(tmp_path):
    with patch.object(material.settings, "agent_state_dir", return_value=str(tmp_path)):
        a = "mcp_server_connectors/apps/demo/demo_find.mjs"
        b = "policies/generated/demo.yaml"
        material.install({a: "export default {};", b: "name: ava-demo"})
        assert material.collect() == {a: "export default {};", b: "name: ava-demo"}
        material.install({}, connector="demo", scopes={"servers"})
        assert material.collect() == {b: "name: ava-demo"}


@pytest.mark.parametrize("name", ["../../etc/passwd", "/tmp/anything", "policies/generated/../bad.yaml",
                                 "mcp_server_connectors/_server.mjs", "connectors/demo/connector.yaml"])
def test_host_code_and_traversal_cannot_cross_the_material_boundary(name, tmp_path):
    with patch.object(material.settings, "agent_state_dir", return_value=str(tmp_path)):
        with pytest.raises(ValueError):
            material.install({name: "bad"})
        assert list(tmp_path.iterdir()) == []


def test_material_size_is_bounded():
    with pytest.raises(ValueError):
        material.validate({"policies/generated/demo.yaml": "x" * material.MAX_BYTES})
