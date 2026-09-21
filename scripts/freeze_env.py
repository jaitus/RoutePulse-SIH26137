"""Environment manifest + evidence hash — reviewer P1-12 and P1-14.

Two jobs:

  1. Record exactly what produced the numbers: Python build, OS, CPU, pinned
     package versions, git commit, and whether the working tree was dirty.
     "Reproducible" is a claim about an environment, and an unpinned
     requirements file does not describe one.

  2. Hash every committed evidence file. That is what makes the final evidence
     package auditable: if a JSON changes after the freeze, the manifest stops
     matching and somebody has to say why.

Run:  python scripts/freeze_env.py            -> out/environment.json
      python scripts/freeze_env.py --final    -> also writes out/final/MANIFEST.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys

import importlib.metadata as md

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")

PACKAGES = ["fastapi", "uvicorn", "ortools", "requests", "networkx", "numpy",
            "scipy", "pydantic", "starlette", "pytest", "httpx"]

# The artefacts a claim may be quoted from. Anything not on this list is not
# evidence, it is working material.
EVIDENCE = ["bench_30seed.json", "latency.json", "scenarios.json",
            "convergence.json", "energy.json", "sb.json", "oracles.json",
            "security.json"]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def environment() -> dict:
    return {
        "python": sys.version.split()[0],
        "python_build": " ".join(platform.python_build()),
        "implementation": platform.python_implementation(),
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "packages": {p: (md.version(p) if _has(p) else None) for p in PACKAGES},
        "git_commit": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "sumo": shutil.which("sumo") is not None,
    }


def _has(p: str) -> bool:
    try:
        md.version(p)
        return True
    except md.PackageNotFoundError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true",
                    help="copy the evidence set into out/final/ and hash it")
    args = ap.parse_args()

    env = environment()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "environment.json"), "w", encoding="utf-8") as f:
        json.dump(env, f, indent=1)

    print(f"python   : {env['python']} ({env['implementation']})")
    print(f"os       : {env['os']} / {env['machine']}")
    print(f"cpu      : {env['processor']} x{env['cpu_count']}")
    print(f"commit   : {env['git_commit'][:12] or 'n/a'}"
          f"{'  DIRTY' if env['git_dirty'] else ''}")
    print(f"sumo     : {'on PATH' if env['sumo'] else 'NOT INSTALLED (not used)'}")
    print("packages :")
    for p, v in env["packages"].items():
        print(f"  {p:<12} {v or 'missing'}")

    if not args.final:
        print(f"\nwritten: {os.path.join(OUT, 'environment.json')}")
        return

    final = os.path.join(OUT, "final")
    os.makedirs(final, exist_ok=True)
    manifest = {"environment": env, "files": {}, "missing": []}
    print("\nevidence package:")
    for name in EVIDENCE:
        src = os.path.join(OUT, name)
        if not os.path.exists(src):
            manifest["missing"].append(name)
            print(f"  {name:<24} MISSING")
            continue
        dst = os.path.join(final, name)
        shutil.copy2(src, dst)
        digest = sha256(dst)
        manifest["files"][name] = {"sha256": digest,
                                   "bytes": os.path.getsize(dst)}
        print(f"  {name:<24} {digest[:16]}…  {os.path.getsize(dst):>9,} B")

    with open(os.path.join(final, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    print(f"\nwritten: {os.path.join(final, 'MANIFEST.json')}")
    if manifest["missing"]:
        print(f"WARNING: {len(manifest['missing'])} experiment(s) not present. "
              f"The package is INCOMPLETE and says so.")


if __name__ == "__main__":
    main()
