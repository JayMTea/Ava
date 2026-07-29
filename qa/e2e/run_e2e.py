"""Tier 3 orchestrator — frontend E2E against the REAL bridge (no mocked API).

Boots the fake LLM/gpusvc/app servers and one real bridge subprocess on a fresh
AVA_HOME, links demo/node_modules next to the specs (playwright + tsx live
there), then runs each spec in order against the live instance. The first spec
performs first-run setup, so ordering matters.

Run directly:  .venv/bin/python qa/e2e/run_e2e.py
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO)

from qa.bridge_proc import BridgeProc          # noqa: E402
from qa.env_recipe import QA_PASSWORD, free_port  # noqa: E402
from qa.fakes.fake_app import FakeApp          # noqa: E402
from qa.fakes.fake_gpusvc import Fakegpusvc      # noqa: E402
from qa.fakes.fake_llm import FakeLLM          # noqa: E402

# Distinct from 0 and 1 on purpose. Returning 0 when nothing ran made qa/run.sh
# print "e2e: PASS" for a tier that executed zero specs — and since demo/ is
# local-only, that is what it printed for every clone, forever. A skip that
# cannot be told apart from a pass is worse than a failure: it cannot
# self-correct, and a contributor ships a UI regression on a green board.
SKIP = 77

SPECS = ["setup-flow.spec.ts", "chat-flow.spec.ts",
         "dashboards.spec.ts", "connectors-flow.spec.ts"]
if len(sys.argv) > 1:   # debug: run a subset (setup still needed by the rest)
    SPECS = ["setup-flow.spec.ts"] + [s for s in sys.argv[1:]
                                      if s != "setup-flow.spec.ts"]


def _find_tsx() -> str | None:
    """The runner, preferring this tier's own node_modules.

    qa/e2e/package.json pins playwright + tsx, so `npm i` here makes the tier
    runnable for anyone. The demo/ fallback stays for the maintainer's box,
    where those deps already exist — but it is a fallback, not the contract:
    demo/ is local-only, which is why this tier ran nowhere but here.
    """
    for base in (os.path.join(_HERE, "node_modules"),
                 os.path.join(_REPO, "demo", "node_modules")):
        cand = os.path.join(base, ".bin", "tsx")
        if os.path.isfile(cand):
            return cand
    return None


def main() -> int:
    tsx = _find_tsx()
    if tsx is None:
        print("SKIP: no E2E runner — install it with:  cd qa/e2e && npm install")
        return SKIP
    if not os.path.isfile(os.path.join(_REPO, "frontend", "dist", "index.html")):
        print("SKIP: frontend/dist not built (cd frontend && npm run build)")
        return SKIP
    # Only needed when borrowing demo/'s modules; a local install is already here.
    link = os.path.join(_HERE, "node_modules")
    if not os.path.exists(link):
        os.symlink(os.path.dirname(os.path.dirname(tsx)), link)

    llm = FakeLLM(free_port()).start()
    gpusvc = Fakegpusvc(free_port()).start()
    app = FakeApp(free_port()).start()
    bridge = BridgeProc(llm.url, gpusvc.url).start(timeout=90)
    print(f"[e2e] bridge live at {bridge.base_url} (home {bridge.home})")

    env = dict(os.environ)
    env.update(BRIDGE_URL=bridge.base_url, QA_PASSWORD=QA_PASSWORD,
               FAKE_APP_URL=app.url)
    failed: list[str] = []
    try:
        for spec in SPECS:
            path = os.path.join(_HERE, spec)
            if not os.path.isfile(path):
                continue
            print(f"[e2e] running {spec} …", flush=True)
            r = subprocess.run([tsx, path], cwd=_HERE, env=env, timeout=300)
            if r.returncode != 0:
                failed.append(spec)
    finally:
        bridge.stop()
    if failed:
        print(f"[e2e] FAILED: {', '.join(failed)}")
        return 1
    print("[e2e] all specs passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
