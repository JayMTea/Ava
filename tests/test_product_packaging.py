"""Exercise packaging with untracked private canaries, not developer identities."""
from pathlib import Path
import subprocess

import pytest

from deploy.scripts.build_context import create


def test_public_context_excludes_unknown_private_files(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / ".dockerignore").write_text("/overlay\n/frontend/src/overlay\n**/.env\n")
    (source / "public.py").write_text("print('product')\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "-qm", "fixture"], cwd=source, check=True)
    for name in (".env", "private-app/token.txt", "branding/owner.png", "domains.yaml", "ava-data-slot/secret"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_CANARY")
    target = tmp_path / "context"
    assert create(target, source=source)["private"] is False
    assert {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()} == {
        ".dockerignore", "public.py", "build-context.json"}
    assert not any(b"PRIVATE_CANARY" in p.read_bytes() for p in target.rglob("*") if p.is_file())
    with pytest.raises(ValueError, match="new directory"):
        create(target, source=source)
    private = tmp_path / "private"
    private.mkdir()
    (private / "panel.tsx").write_text("export const Panel = () => null;\n")
    (private / ".env").write_text("TOKEN=DO_NOT_PACKAGE")
    own = tmp_path / "custom"
    assert create(own, source=source, additions={"frontend/src/overlay": private})["private"]
    assert (own / "frontend/src/overlay/panel.tsx").is_file()
    assert not (own / "frontend/src/overlay/.env").exists()


def test_shipped_connector_index_matches_product_manifests():
    import json
    root = Path(__file__).resolve().parents[1] / "connectors"
    from gitfiles import tracked
    shipped = {Path(name).parts[1] for name in tracked("connectors/*/connector.yaml")
               if not Path(name).parts[1].startswith("_")}
    assert set(json.loads((root / "builtins.json").read_text())) == shipped
