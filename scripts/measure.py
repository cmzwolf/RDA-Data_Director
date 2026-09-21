#!/usr/bin/env python3
"""Sweep the live suite across installed Ollama models.

Written in Python rather than shell deliberately: macOS ships bash 3.2, where
`mapfile` does not exist and an empty array under `set -u` is an unbound
variable. A measurement harness that only runs on some machines is not a
measurement harness.

Each run appends to tests/results/measurements.jsonl tagged with the model that
produced it. Results across model families are the point: four families agreeing
is evidence about our specification, whereas one model agreeing with itself is
evidence about sampling.

    python3 scripts/measure.py                    # all installed, 5 repeats
    python3 scripts/measure.py --repeats 20
    python3 scripts/measure.py --models qwen3.8:27b-mlx gemma4:31b-mlx
    python3 scripts/measure.py --timeout 900 -k injection
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = os.environ.get("DD_OLLAMA_ENDPOINT", "http://localhost:11434")

# Embedding models cannot answer a chat request. Skipped rather than run and
# reported as a failure, which would be a false negative about the model.
EMBEDDING_HINTS = ("embed", "embedding")


def installed_models(endpoint: str) -> list[str]:
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=10) as fh:
            data = json.load(fh)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot reach Ollama at {endpoint}: {exc}", file=sys.stderr)
        return []
    return sorted(m["name"] for m in data.get("models", []))


def is_embedding(name: str) -> bool:
    return any(h in name.lower() for h in EMBEDDING_HINTS)


def run_one(model: str, repeats: int, timeout: int, endpoint: str,
            extra: list[str]) -> int:
    env = dict(os.environ)
    env.update({
        "DD_LIVE_TESTS": "1",
        "DD_LIVE_REPEATS": str(repeats),
        "DD_OLLAMA_MODEL": model,
        "DD_OLLAMA_ENDPOINT": endpoint,
        "DD_OLLAMA_TIMEOUT": str(timeout),
    })
    cmd = [sys.executable, "-m", "pytest", "tests/live", "-q", "-s", *extra]
    return subprocess.call(cmd, env=env)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=5,
                    help="injection-test repeats per model (default 5)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-call model timeout in seconds (default 600)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--models", nargs="*", default=None,
                    help="explicit model tags; default is every installed model")
    ap.add_argument("-k", dest="keyword", default=None,
                    help="pass a -k expression through to pytest")
    args = ap.parse_args()

    models = args.models if args.models else [
        m for m in installed_models(args.endpoint) if not is_embedding(m)
    ]
    if not models:
        print("no usable models found; is Ollama running?", file=sys.stderr)
        return 1

    extra = ["-k", args.keyword] if args.keyword else []
    print(f"sweeping {len(models)} model(s) at {args.endpoint}, "
          f"{args.repeats} repeats, {args.timeout}s timeout")

    failed = []
    for model in models:
        print(f"\n== {model}", flush=True)
        # A model that times out or errors must not abort the sweep: a partial
        # result across families is more useful than none.
        if run_one(model, args.repeats, args.timeout, args.endpoint, extra) != 0:
            failed.append(model)
            print(f"-- {model}: some tests failed or timed out", flush=True)

    print("\nresults appended to tests/results/measurements.jsonl")
    if failed:
        print(f"models with failures: {failed}")
    subprocess.call([sys.executable, "scripts/summarise_measurements.py"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
