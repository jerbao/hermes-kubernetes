"""Serve cached, read-only quotas beside CLIProxyAPI, with no caller-controlled probes."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from hermes_kubernetes.quota.proxy import ProxyClient


class Cache:
    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()
        self.value = {"schema": 1, "fetched_at": None, "providers": {}, "status": "pending"}

    def refresh(self):
        try:
            value = self.client.collect()
        except (OSError, ValueError, KeyError, TypeError):
            # Never keep a successful old reading looking current after an upstream failure.
            value = {
                "schema": 1,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "providers": {},
                "status": "unavailable",
            }
        with self.lock:
            self.value = value

    def read(self):
        with self.lock:
            return self.value

    def run(self):
        while True:
            self.refresh()
            time.sleep(120)


def handler(cache):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                status, value = 200, {"status": "ok"}
            elif self.path == "/v1/usage":
                status, value = 200, cache.read()
            else:
                status, value = 404, {"error": "Not found"}
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8318)
    args = parser.parse_args()
    cache = Cache(ProxyClient("http://127.0.0.1:8317", args.key_file.read_text().strip()))
    threading.Thread(target=cache.run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", args.port), handler(cache)).serve_forever()


if __name__ == "__main__":
    main()
