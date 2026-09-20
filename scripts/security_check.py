"""Security verification — the hardening claims, actually exercised.

Every item the server's docstring claims is tested here against a RUNNING
instance. A security control that is described but never exercised is a
comment, and this project's whole method is that a claim gets a measurement.

Run the server, then:

    python scripts/security_check.py                    # open-mode checks
    ROUTEPULSE_API_KEY=secret python -m uvicorn server.app:app --port 8001
    python scripts/security_check.py --base http://127.0.0.1:8001 --key secret

Exit code is non-zero if any check fails, so it can gate a commit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REQUIRED_HEADERS = {
    "content-security-policy": "default-src 'self'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}

PASS, FAIL = "PASS", "FAIL"


def call(base, path, method="GET", body=None, headers=None, timeout=40):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={**({"Content-Type": "application/json"} if body is not None else {}),
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except urllib.error.URLError as e:
        return 0, {}, str(e).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default=os.environ.get("ROUTEPULSE_API_KEY", ""))
    ap.add_argument("--rate", action="store_true",
                    help="also exercise the rate limiter (slow, and it will "
                         "leave the limiter saturated for a minute)")
    args = ap.parse_args()
    B = args.base
    results: list[tuple[str, str, str]] = []

    def check(name, ok, detail=""):
        results.append((PASS if ok else FAIL, name, detail))

    status, _hd, _b = call(B, "/api/health")
    if status == 0:
        print(f"server not reachable at {B} — start it first")
        return 2

    # ---- 1. security headers on every kind of response
    for path in ("/", "/api/health", "/static/app.css"):
        _s, hd, _ = call(B, path)
        for k, needle in REQUIRED_HEADERS.items():
            check(f"header {k} on {path}", needle in (hd.get(k) or ""),
                  hd.get(k) or "absent")
    _s, hd, _ = call(B, "/")
    csp = hd.get("content-security-policy", "")
    check("CSP forbids inline script", "'unsafe-inline'" not in csp
          and "script-src 'self'" in csp, csp)
    check("CSP forbids inline style", "style-src 'self'" in csp
          and "'unsafe-inline'" not in csp, csp)

    # ---- 2. interactive API docs are not exposed
    for path in ("/docs", "/redoc", "/openapi.json"):
        s, _h, _b = call(B, path)
        check(f"{path} not served", s == 404, f"HTTP {s}")

    # ---- 3. input bounds. Each of these is a denial of service or an
    #         out-of-area write if it is accepted.
    bounded = [
        ("instance size", "/api/boot?n=100000", "GET", None),
        ("negative vehicles", "/api/boot?k=-3", "GET", None),
        ("solver budget", "/api/replan?budget=999", "POST", None),
        ("unknown engine", "/api/replan?engines=rm-rf", "POST", None),
    ]
    key_hdr = {"X-API-Key": args.key} if args.key else None
    for name, path, method, body in bounded:
        s, _h, _b = call(B, path, method, body, headers=key_hdr)
        check(f"rejects out-of-range {name}", s == 422, f"HTTP {s}")

    body_checks = [
        ("latitude off the globe", {"lat": 999, "lon": 77.6, "kind": "closure"}),
        ("unknown event kind", {"lat": 12.97, "lon": 77.59, "kind": "'; DROP TABLE"}),
        ("absurd incident radius", {"lat": 12.97, "lon": 77.59, "radius_m": 1e9}),
        ("negative multiplier", {"lat": 12.97, "lon": 77.59, "kind": "congestion",
                                 "multiplier": -5}),
        ("coordinate outside the served network",
         {"lat": 48.8566, "lon": 2.3522, "kind": "closure"}),
    ]
    # Schema-level rejections happen before authentication, but the
    # service-area check runs inside the handler and is therefore BEHIND the
    # key. Send it, or this test measures the auth layer twice and the bounds
    # layer never.
    auth_hdr = {"X-API-Key": args.key} if args.key else None
    for name, body in body_checks:
        s, _h, _b = call(B, "/api/event", "POST", body, headers=auth_hdr)
        check(f"rejects {name}", s == 422, f"HTTP {s}")

    # ---- 4. errors do not leak internals
    _s, _h, raw = call(B, "/api/event", "POST", {"lat": 999, "lon": 0})
    txt = raw.decode("utf-8", "replace")
    leaks = [w for w in ("Traceback", "site-packages", "File \"", os.sep + "routepulse")
             if w in txt]
    check("error body leaks no traceback or path", not leaks, ", ".join(leaks) or "clean")

    # ---- 5. authentication, when configured
    s, _h, b = call(B, "/api/health")
    auth = json.loads(b.decode()).get("auth", "")
    if args.key:
        s, _h, _b = call(B, "/api/reset", "POST")
        check("mutating endpoint refuses a missing key", s == 401, f"HTTP {s}")
        s, _h, _b = call(B, "/api/reset", "POST", headers={"X-API-Key": "wrong"})
        check("mutating endpoint refuses a wrong key", s == 401, f"HTTP {s}")
        s, _h, _b = call(B, "/api/reset", "POST", headers={"X-API-Key": args.key})
        check("mutating endpoint accepts the right key", s == 200, f"HTTP {s}")
        check("health reports auth as enforced", "required" in auth, auth)
    else:
        check("open mode is DECLARED, not hidden", "OPEN" in auth, auth)
        results.append(("NOTE", "no API key configured",
                        "re-run with ROUTEPULSE_API_KEY set to test enforcement"))

    # ---- 6. rate limiting (opt-in: it saturates the bucket for a minute)
    if args.rate:
        hits = []
        for _ in range(60):
            s, _h, _b = call(B, "/api/replan?budget=0.06", "POST",
                             headers={"X-API-Key": args.key} if args.key else None)
            hits.append(s)
            if s == 429:
                break
        check("expensive endpoint is rate limited", 429 in hits,
              f"{len(hits)} requests before {'429' if 429 in hits else 'giving up'}")
        time.sleep(1)

    # ---------------------------------------------------------------- report
    width = max(len(n) for _v, n, _d in results) + 2
    failed = 0
    print(f"target: {B}\n")
    for verdict, name, detail in results:
        if verdict == FAIL:
            failed += 1
        print(f"  [{verdict:^4}] {name:<{width}} {detail}")
    total = sum(1 for v, _n, _d in results if v in (PASS, FAIL))
    print(f"\n{total - failed}/{total} checks passed"
          + (f"  ({failed} FAILED)" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
