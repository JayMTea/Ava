"""The `ava` command must exist, and mean one thing.

README.md tells a forker to run `ava setup && ava doctor && ava up`. For most of
this repo's life that command did not exist for them: there was no console
script, so `ava` was "command not found" unless the owner had aliased it, and
only `./bin/ava` (a relative launcher, used by CONTRIBUTING.md but never by the
user-facing docs) worked. `pyproject.toml` now declares the entry point, and
these tests keep the two launchers pointing at the same function.

The second half is about the module allowlist, which is load-bearing in a way
that is easy to miss. Two failure modes sit on opposite sides of it:

  * TOO WIDE — `packages = find:` would sweep the repo root's agent/, config/,
    connectors/, data/, docs/ and tools/ directories into the distribution as
    importable top-level packages, shadowing common names and shipping the
    owner's runtime state.

  * TOO NARROW — setuptools' editable finder honours the list STRICTLY; it does
    not fall back to putting the repo root on sys.path. A module imported by
    name but left off the list therefore works perfectly from the clone (where
    CWD covers for it) and raises ModuleNotFoundError from anywhere else. The
    first draft of the list did this to a module `ava verify` imports inside
    the command body, so the break was invisible to import-time checks and to
    every test run from the repo root.

So the list is checked from both ends: nothing declared that isn't real, and
nothing imported by name that isn't declared.
"""

import ast
import pathlib
import re
import subprocess
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", pattern], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def _cfg() -> dict:
    with open(PYPROJECT, "rb") as fh:
        return tomllib.load(fh)


def test_console_script_and_bin_launcher_agree():
    """`ava` from PATH and `./bin/ava` must run the same entry point."""
    scripts = _cfg().get("project", {}).get("scripts", {})
    assert scripts.get("ava") == "app.cli:main", (
        "pyproject.toml [project.scripts] must declare `ava = \"app.cli:main\"`.\n"
        f"Found: {scripts!r}\n"
        "README.md documents `ava setup && ava doctor && ava up`; without this "
        "entry point that is 'command not found' for every forker."
    )

    launcher = (ROOT / "bin" / "ava").read_text()
    assert "-m app.cli" in launcher, (
        "bin/ava no longer execs app/cli.py, so the two documented ways to run "
        "the CLI have diverged. Point both at app.cli:main or retire one."
    )

    # The declared callable has to exist and be usable as a console script,
    # i.e. return an int for sys.exit(). Parsed rather than imported: importing
    # ava_cli pulls in the settings module, which freezes AVA_HOME.
    tree = ast.parse((ROOT / "app/cli.py").read_text())
    main = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None
    )
    assert main is not None, "app/cli.py has no top-level main() for the console script."
    returns_value = any(
        isinstance(n, ast.Return) and n.value is not None for n in ast.walk(main)
    )
    assert returns_value, (
        "app.cli.main() must return an exit code. A console script wrapper calls "
        "sys.exit(main()), so returning None makes every failure exit 0 — the "
        "opposite of what `ava doctor` in a CI script needs."
    )


def test_declared_packages_and_modules_all_exist():
    """Nothing in the allowlist may be a name that isn't there."""
    tool = _cfg().get("tool", {}).get("setuptools", {})
    offenders = []

    for pkg in tool.get("packages", []):
        top, *tail = pkg.split(".")
        location = ROOT / tool.get("package-dir", {}).get(top, top)
        if not (location.joinpath(*tail) / "__init__.py").is_file():
            offenders.append(f"packages: {pkg!r} has no {pkg.replace('.', '/')}/__init__.py")

    for mod in tool.get("py-modules", []):
        if not (ROOT / f"{mod}.py").is_file():
            offenders.append(f"py-modules: {mod!r} has no {mod}.py at the repo root")

    assert not offenders, (
        "pyproject.toml declares names that do not exist:\n  "
        + "\n  ".join(offenders)
        + "\n\nA stale name makes `pip install -e .` fail outright. Delete the "
        "entry, or restore the file it names."
    )


def test_every_application_package_is_declared():
    """An editable install must expose every real application package."""
    declared = set(_cfg()["tool"]["setuptools"]["packages"])
    actual = {
        p.removesuffix("/__init__.py").replace("/", ".")
        for p in _tracked("*/__init__.py")
        if p.startswith("app/")
    }
    agent_dir = "agent-platform/integrations/ava/ava_agent"
    actual |= {
        "ava_agent" + p[len(agent_dir):].removesuffix("/__init__.py").replace("/", ".")
        for p in _tracked(f"{agent_dir}/**/__init__.py") + _tracked(f"{agent_dir}/__init__.py")
    }
    assert actual == declared
    assert _cfg()["tool"]["setuptools"]["py-modules"] == []


def test_allowlist_is_explicit_and_excludes_repo_root_directories():
    """No auto-discovery, and no shipping the repo root's data directories."""
    raw = PYPROJECT.read_text()
    assert "find" not in _cfg().get("tool", {}).get("setuptools", {}), (
        "[tool.setuptools] must not use auto-discovery. The repo root holds "
        "agent/, config/, connectors/, data/, docs/ and tools/ — find-based "
        "discovery makes those importable top-level packages."
    )
    assert "packages" in raw and "py-modules" in raw, (
        "Both `packages` and `py-modules` must be declared explicitly. "
        "Omitting either re-enables setuptools' automatic discovery."
    )

    packages = set(_cfg()["tool"]["setuptools"]["packages"])
    forbidden = {"agent", "runtime", "config", "connectors", "data", "docs", "tools", "scripts", "tests"}
    leaked = {p for p in packages if p.split(".")[0] in forbidden}
    assert not leaked, (
        f"These must never be importable top-level packages: {sorted(leaked)}.\n"
        "They are repo directories holding runtime state, owner config and "
        "documentation — not library code — and their names collide with common "
        "third-party modules."
    )


def test_editable_install_only():
    """The build metadata must not imply a working non-editable wheel."""
    raw = PYPROJECT.read_text()
    assert "EDITABLE INSTALL ONLY" in raw, (
        "pyproject.toml lost the comment recording that only `pip install -e .` "
        "is supported. A non-editable wheel would leave app.backend.settings."
        "CODE_ROOT pointing into site-packages, where config/ava.example.yaml, "
        "app/frontend/dist, agent-platform/integrations/ava/sandbox/install.sh and connectors/_template do not exist. "
        "Making those work needs package-data plumbing, which is a separate project."
    )


def test_documented_editable_install_is_actually_installable():
    """If a tracked file tells someone to `pip install -e .`, it must work.

    This is the pairing that broke. `deploy/install.sh` printed a four-step
    Apple Silicon recipe — venv, `pip install -r config/dependencies/runtime.txt && pip install
    -e .`, `ava setup && ava doctor && ava up` — as the ONLY path it offers a Mac
    user, while pyproject.toml carried tooling config and no build backend. Step
    three died with "Getting requirements to build editable did not run
    successfully", so the instruction and the metadata each looked reasonable
    alone and were never true together.
    """
    cfg = _cfg()
    tellers = [
        p for p in _tracked("*")
        if pathlib.Path(p).suffix.lower() in {".md", ".sh", ".yml", ".yaml", ".txt"}
        and (ROOT / p).is_file()
        and re.search(r"pip3?\s+install\s+-e\s+\.", (ROOT / p).read_text(errors="replace"))
    ]
    if not tellers:
        return  # nothing promises it, so nothing to back up

    assert "build-system" in cfg and cfg.get("project"), (
        "These tracked files instruct `pip install -e .`:\n  "
        + "\n  ".join(sorted(tellers))
        + "\n\nbut pyproject.toml has no [build-system]/[project] table, so that "
        "command fails. Either add the packaging metadata or change the "
        "instructions to ./bin/ava — they cannot disagree."
    )


def test_version_and_dependencies_are_not_restated():
    """One version file, one dependency list."""
    project = _cfg()["project"]
    dynamic = set(project.get("dynamic", []))
    assert {"version", "dependencies"} <= dynamic, (
        "version and dependencies must stay `dynamic`, sourced from VERSION and "
        "requirements.txt. Restating either here creates a second copy that "
        "drifts silently — the same failure as the two ruff pins CI used to carry."
    )
    assert "version" not in project and "dependencies" not in project, (
        "A static version/dependencies key overrides the dynamic one."
    )

    dyn = _cfg()["tool"]["setuptools"]["dynamic"]
    assert (ROOT / dyn["version"]["file"]).is_file(), "VERSION file named by pyproject is missing."
    for f in dyn["dependencies"]["file"]:
        assert (ROOT / f).is_file(), f"Dependency file named by pyproject is missing: {f}"
