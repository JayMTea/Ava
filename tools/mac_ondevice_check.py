#!/usr/bin/env python3
"""Ava on-device validation — RUN THIS ON THE REAL MAC STUDIO.

Read-only. Nothing is written, downloaded, or started. It exercises Ava's real
hardware/routing code against the actual machine and probes the local engine, so
you can confirm — before trusting a fresh install — that:

  * the HAL detects Apple Silicon + unified memory and reports honest numbers,
  * model-fit recommends a tier that MATCHES a Mac-servable engine,
  * a local OpenAI-compatible engine (Ollama/LM Studio/MLX/llama.cpp) is reachable,
  * (voice, optional) torch sees the Metal/MPS backend,
  * you are NOT about to be steered into the vLLM-only default (the known
    high-RAM-Mac trap — see the audit report).

Usage on the Mac (inside the repo, venv active):
    python mac_ondevice_check.py
    python mac_ondevice_check.py --engine-url http://127.0.0.1:11434/v1 --smoke

Exit 0 = ready; 1 = a hard problem a user would hit; warnings never fail the run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Run from anywhere: put the repo root (this file's grandparent) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, WARN, BAD = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"
_hard = 0
_warn = 0


def ok(msg):
    print(f"  {OK} {msg}")


def warn(msg):
    global _warn
    _warn += 1
    print(f"  {WARN} {msg}")


def bad(msg):
    global _hard
    _hard += 1
    print(f"  {BAD} {msg}")


def head(t):
    print(f"\n\033[1m{t}\033[0m")


def probe(url, timeout=1.5):
    try:
        import requests
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-url", default="http://127.0.0.1:11434/v1",
                    help="OpenAI-compatible base URL of your local engine")
    ap.add_argument("--smoke", action="store_true",
                    help="send one tiny /chat/completions if a model is up")
    ap.add_argument("--model", default="", help="model id for --smoke")
    args = ap.parse_args()

    print("=== Ava on-device validation (Mac) ===")
    try:
        from ava_bridge import hwinfo, model_fit
    except Exception as e:
        bad(f"cannot import ava_bridge (run from repo root, venv active): {e}")
        sys.exit(1)

    # 1) Platform + memory (REAL) --------------------------------------------
    head("1. Hardware abstraction layer (real readings)")
    pid = hwinfo.platform_id()
    (ok if pid == "darwin-apple" else bad)(f"platform_id() = {pid}")
    if pid != "darwin-apple":
        warn("not Apple Silicon — this harness is meant for the Mac Studio")

    sm = hwinfo.system_mem()
    if sm.readable:
        ok(f"system memory: {sm.total_gb:.0f} GB total, "
           f"{sm.free_gb:.0f} GB free  (source={sm.source})")
    else:
        bad("system memory unreadable — is psutil installed? (pip install psutil)")

    fm = hwinfo.fit_memory()
    if fm.readable:
        ok(f"fit memory (gating pool): {fm.total_gb:.0f} GB total, "
           f"{fm.free_gb:.0f} GB free  (source={fm.source})")
        if fm.source and fm.source.startswith("vram"):
            warn("fit memory came from VRAM — unexpected on Apple Silicon")
    else:
        bad("fit memory unreadable — model routing will not memory-gate")

    g = hwinfo.gpu()
    if g.name:
        ok(f"GPU: {g.name}  (mem_total={g.mem_total_gb} GB)")
        if g.util is None:
            print("        util/temp/power are None by design on Apple (no "
                  "unprivileged API) — dashboard GPU meters will be blank.")
    else:
        warn("no GPU name from sysctl — dashboard will show a generic label")

    print("\n  hwinfo.snapshot():")
    print("    " + json.dumps(hwinfo.snapshot(), indent=2).replace("\n", "\n    "))

    # 2) Model-fit tier vs. a Mac-servable engine ----------------------------
    head("2. Model-fit recommendation")
    if fm.total_gb:
        tier, hint = model_fit.recommend_tier(fm.total_gb)
        ok(f"recommended tier: {tier} — {hint}")
        # Post-fix: `ava models pull --auto` is now platform-aware and substitutes
        # an Ollama model on Apple Silicon. Belt-and-suspenders: verify the ACTIVE
        # config isn't still pointing at a vLLM backend (e.g. an old/hand-edited
        # ava.yaml), since vLLM can't serve here.
        try:
            from ava_bridge import settings
            prim = settings.get("inference.primary")
            backends = settings.get("inference.backends", {}) or {}
            eng = (backends.get(prim, {}) or {}).get("engine", "").lower()
            if eng == "vllm":
                bad(f"active backend '{prim}' uses engine 'vllm' — it CANNOT serve "
                    "on Apple Silicon. Re-run `ava setup` (seeds Ollama) or edit "
                    "inference.backends to an ollama/mlx/lmstudio engine.")
            elif eng:
                ok(f"active backend '{prim}' uses a Mac-servable engine: {eng}")
            else:
                warn("no active inference backend configured yet — run `ava setup`")
        except Exception as e:
            warn(f"couldn't read inference config: {e}")
    else:
        warn("no fit-memory total — tier recommendation skipped")

    # 3) Local engine reachability -------------------------------------------
    head("3. Local inference engine")
    engine_root = args.engine_url.rstrip("/")
    models_url = engine_root + "/models"
    ollama_tags = "http://127.0.0.1:11434/api/tags"
    if probe(models_url):
        ok(f"OpenAI-compatible engine answering at {models_url}")
    elif probe(ollama_tags):
        ok("Ollama is running (:11434) but --engine-url /models didn't answer; "
           "point --engine-url at http://127.0.0.1:11434/v1")
    else:
        warn(f"no local engine answering at {engine_root}. Install one first: "
             "`brew install ollama && ollama serve` then `ollama pull llama3.1:8b`, "
             "or LM Studio / MLX. The setup wizard will show 'no backend' until then.")

    # 4) Optional inference smoke test ---------------------------------------
    if args.smoke:
        head("4. Inference smoke test")
        model = args.model
        if not model:
            warn("--smoke needs --model <id>; skipping the actual call")
        else:
            try:
                import requests
                r = requests.post(
                    engine_root + "/chat/completions",
                    json={"model": model, "max_tokens": 8,
                          "messages": [{"role": "user", "content": "say hi"}]},
                    timeout=60)
                if r.status_code == 200:
                    txt = r.json()["choices"][0]["message"]["content"]
                    ok(f"model replied: {txt!r}")
                else:
                    bad(f"engine returned HTTP {r.status_code}: {r.text[:200]}")
            except Exception as e:
                bad(f"smoke call failed: {e}")

    # 5) Voice stack (optional) ----------------------------------------------
    head("5. Voice stack (optional — only if you plan to enable voice)")
    try:
        import torch
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            ok("torch sees the Metal/MPS backend (Whisper STT can use the GPU)")
        else:
            warn("torch installed but MPS unavailable — STT will run on CPU")
    except Exception:
        print("        torch not installed (voice extra) — skip if not using voice")

    # ── verdict ──────────────────────────────────────────────────────────────
    head("Verdict")
    if _hard:
        print(f"  {BAD} {_hard} hard problem(s), {_warn} warning(s). "
              "Fix the ✗ items before onboarding a user.")
        sys.exit(1)
    print(f"  {OK} No hard failures. {_warn} warning(s) to review above.")
    print("  Heed the tier/engine warnings: on this Mac, pull an Ollama or MLX "
          "model and configure inference.backends by hand — do NOT use the "
          "vLLM default or `pull --auto`.")
    sys.exit(0)


if __name__ == "__main__":
    main()
