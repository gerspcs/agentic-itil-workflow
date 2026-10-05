#!/usr/bin/env python3
"""Live viewer server. Standard library only.

    python3 ui/server.py            # http://127.0.0.1:8099

It serves the page, and a few JSON endpoints that read one process instance
from the Camunda 8 REST API. Safety rules:
  * listens on 127.0.0.1 only;
  * the only actions are the ones below (start a scenario or a typed incident, answer a human task);
  * every POST needs the per-session token the page embeds (X-UI-Token).
Nothing here touches your TypeSafe key: the worker owns that.
"""

import argparse
import json
import mimetypes
import os
import secrets
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lab

HERE = lab.HERE
TOKEN = secrets.token_urlsafe(16)
STORY = json.load(open(os.path.join(HERE, "story.json")))
STATE = {"pi": None}

STATIC = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css",
          "/app.js": "app.js", "/story.json": "story.json", "/diagram.svg": "diagram.svg",
          "/sample-recording.json": "sample-recording.json"}
ALLOWED_HOSTS = set()   # filled in main(): the only Host headers we answer to


class Handler(BaseHTTPRequestHandler):
    server_version = "AgenticTriageViewer/1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # Refuse any Host we did not bind to: this blocks DNS-rebinding pages from reading the
        # per-session token out of "/" and then driving the lab.
        if self.headers.get("Host", "") in ALLOWED_HOSTS:
            return True
        self._send(403, {"ok": False, "message": "Unexpected Host header."})
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?")[0]
        if path in STATIC:
            fp = os.path.join(HERE, STATIC[path])
            if not os.path.exists(fp):
                return self._send(404, {"ok": False})
            data = open(fp, "rb").read()
            if STATIC[path] == "index.html":
                data = data.replace(b"__TOKEN__", TOKEN.encode())
            return self._send(200, data, mimetypes.guess_type(fp)[0] or "text/plain")
        if path == "/api/model":
            return self._send(200, {"ok": True, **lab.MODEL, "engine": lab.engine_up(), "rest": lab.REST})
        if path == "/api/state":
            return self._state()
        self._send(404, {"ok": False})

    def _state(self):
        if not lab.engine_up():
            return self._send(200, {"ok": False, "error": "Cannot reach the Camunda engine at %s. "
                                    "Start it with scripts/lab.sh engine." % lab.REST})
        if not STATE["pi"]:
            return self._send(200, {"ok": True, "pi": None})
        try:
            snap = lab.snapshot(STATE["pi"])
        except urllib.error.URLError as e:
            return self._send(200, {"ok": False, "error": str(e)})
        self._send(200, snap)

    def do_POST(self):
        if not self._host_ok():
            return
        if self.headers.get("X-UI-Token") != TOKEN:
            return self._send(403, {"ok": False, "message": "Bad or missing token. Reload the page."})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"ok": False, "message": "Bad JSON."})
        try:
            if self.path == "/api/start":
                custom = str(body.get("incident") or "").strip()
                if custom:
                    sc = {"incident": custom[:500], "fixOutcome": None}
                else:
                    sc = STORY["scenarios"].get(str(body.get("scenario")))
                if not sc:
                    return self._send(400, {"ok": False, "message": "Unknown scenario."})
                extra = {"fixOutcome": sc["fixOutcome"]} if sc.get("fixOutcome") else {}
                STATE.update(pi=lab.start_incident(sc["incident"], extra))
                return self._send(200, {"ok": True, "pi": STATE["pi"]})
            if self.path == "/api/human":
                if not STATE["pi"]:
                    return self._send(400, {"ok": False, "message": "No incident is running."})
                el = lab.complete_human_task(STATE["pi"], body)
                return self._send(200, {"ok": True, "element": el})
        except (RuntimeError, urllib.error.URLError, KeyError) as e:
            return self._send(200, {"ok": False, "message": str(e)})
        self._send(404, {"ok": False})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    ALLOWED_HOSTS.update({"127.0.0.1:%d" % args.port, "localhost:%d" % args.port})
    print("Live viewer on http://127.0.0.1:%d  (engine REST: %s)" % (args.port, lab.REST))
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
