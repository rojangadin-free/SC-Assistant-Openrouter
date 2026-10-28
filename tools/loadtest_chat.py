"""Real SSE load test. Dry-run unless paid traffic is explicitly authorized."""

import argparse
import hashlib
import http.cookiejar
import json
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def events(response):
    data = []
    for line in response:
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield json.loads("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield json.loads("\n".join(data))


def consume(response, start, timeout):
    first = None
    characters = 0
    for event in events(response):
        if time.perf_counter() - start > timeout:
            raise TimeoutError("Chat deadline exceeded")
        if event.get("type") == "error":
            raise RuntimeError("Server reported an SSE error")
        if event.get("type") == "chunk" and event.get("text"):
            if first is None:
                first = time.perf_counter() - start
            characters += len(event["text"])
        if event.get("type") == "done":
            if not characters:
                raise RuntimeError("No answer text arrived")
            return {"ok": True, "ttft": first, "seconds": time.perf_counter() - start,
                    "characters": characters}
    raise RuntimeError("Stream ended without a completed answer")


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int((len(values) - 1) * fraction + 0.5)))]


def run(args):
    cookies = None
    if args.session_cookies:
        with open(args.session_cookies, encoding="utf-8") as handle:
            cookies = json.load(handle)
        if not isinstance(cookies, list) or len(cookies) < args.users or any(not isinstance(c, str) for c in cookies):
            raise ValueError("Provide one session cookie string per user")
    barrier = threading.Barrier(args.users)
    base = args.base_url.rstrip("/") + "/"

    def user(index):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        results = []
        try:
            if cookies:
                host = urllib.parse.urlparse(base).hostname
                domain = host if "." in host else host + ".local"
                jar.set_cookie(http.cookiejar.Cookie(0, "session", cookies[index], None, False,
                               domain, False, False, "/", True, False, None, True, None, None, {}))
            else:
                request = urllib.request.Request(urllib.parse.urljoin(base, "guest"), data=b"", method="POST")
                with opener.open(request, timeout=args.timeout) as response:
                    if not json.load(response).get("success"):
                        raise RuntimeError("Guest login failed")
            barrier.wait(timeout=args.timeout)
            for _ in range(args.requests_per_user):
                start = time.perf_counter()
                request = urllib.request.Request(urllib.parse.urljoin(base, "chat/get"),
                          data=urllib.parse.urlencode({"msg": args.query}).encode("utf-8"), method="POST")
                with opener.open(request, timeout=args.timeout) as response:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise RuntimeError("Chat did not return SSE")
                    results.append(consume(response, start, args.timeout))
        except Exception as exc:
            results.append({"ok": False, "error": type(exc).__name__,
                            "http_status": getattr(exc, "code", None)})
            try:
                barrier.abort()
            except Exception:
                pass
        return results

    with ThreadPoolExecutor(max_workers=args.users) as pool:
        results = [item for group in pool.map(user, range(args.users)) for item in group]
    successes = [result for result in results if result["ok"]]
    elapsed = [result["seconds"] for result in successes]
    ttft = [result["ttft"] for result in successes]
    return {"users": args.users, "completed": len(successes), "failed": len(results) - len(successes),
            "expected": args.users * args.requests_per_user,
            "ttft_p50": percentile(ttft, .5), "ttft_p95": percentile(ttft, .95),
            "completion_p50": percentile(elapsed, .5), "completion_p95": percentile(elapsed, .95),
            "errors": [r for r in results if not r["ok"]]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--users", type=int, default=30)
    parser.add_argument("--requests-per-user", type=int, default=1)
    parser.add_argument("--query", default="What are the requirements for enrollment at Samar College?")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--session-cookies", help="JSON array of test-account session cookies; never printed")
    parser.add_argument("--allow-paid-traffic", action="store_true",
                        help="Spend model credits/Pinecone reads and create guest histories or access test-account records")
    args = parser.parse_args(argv)
    url = urllib.parse.urlparse(args.base_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
        parser.error("Use an HTTP(S) app URL without embedded credentials")
    if not 1 <= args.users <= 200 or not 1 <= args.requests_per_user <= 20 or args.timeout <= 0:
        parser.error("Use 1-200 users, 1-20 requests per user and a positive timeout")
    if not args.allow_paid_traffic:
        print(json.dumps({"dry_run": True, "users": args.users, "requests": args.users * args.requests_per_user,
              "query_sha256": hashlib.sha256(args.query.encode()).hexdigest()[:12],
              "authenticated": bool(args.session_cookies),
              "warning": "No traffic sent. Live tests spend API credits and may access test-account records."}, indent=2))
        return 0
    report = run(args)
    print(json.dumps(report, indent=2))
    return 0 if report["completed"] == report["expected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
