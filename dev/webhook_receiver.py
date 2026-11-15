#!/usr/bin/env python3
"""Tiny webhook receiver for testing Meridian's HTTP / webhooks page. Standard library only.

    python3 dev/webhook_receiver.py --secret whsec_...          # listens on port 8765
    python3 dev/webhook_receiver.py --secret whsec_... --fail   # answers 500, to watch retries
    (on Windows PowerShell, "py" works where "python3" doesn't)

In Meridian, set the endpoint URL to http://<this PC's IP>:8765/ (find the IP with ipconfig on
Windows). Windows may ask to allow Python through the firewall the first time: allow it on
private networks. Each event is printed with whether its signature checks out.

Run it with Windows Python, not inside WSL. WSL passes "localhost" through to Windows but not the
PC's LAN address, so the reader gets "connection refused". Opening http://<this PC's IP>:8765/ in
a browser should show "Unsupported method ('GET')": that means it's listening and reachable.
"""
import argparse
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN = set()


def check(secret, ts, body, header):
    if not secret:
        return "not checked (no --secret given)"
    try:
        if abs(time.time() - int(ts)) > 300:
            return "BAD: timestamp more than 5 minutes off (check both clocks)"
    except (TypeError, ValueError):
        return "BAD: no timestamp"
    want = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return "OK" if hmac.compare_digest(want, header or "") else "BAD: signature doesn't match (wrong secret?)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--secret", default="", help="the endpoint's signing secret from the Meridian page")
    ap.add_argument("--fail", action="store_true", help="answer 500 to every request (to test retries)")
    args = ap.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            sig = check(args.secret, self.headers.get("X-Meridian-Timestamp"), body, self.headers.get("X-Meridian-Signature"))
            eid = self.headers.get("X-Meridian-Delivery")
            dup = " (duplicate: already received)" if eid in SEEN else ""
            SEEN.add(eid)
            print(f"\n{time.strftime('%H:%M:%S')}  {self.headers.get('X-Meridian-Event')}  from {self.client_address[0]}"
                  f"  signature {sig}{dup}")
            try:
                print(json.dumps(json.loads(body), indent=2))
            except ValueError:
                print(body[:500])
            code = 500 if args.fail else 200
            out = b'{"ok":false,"error":"failing on purpose (--fail)"}' if args.fail else b'{"ok":true}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    print(f"Listening on port {args.port}. Point a Meridian webhook at http://<this PC's IP>:{args.port}/   (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
