"""Disposable native serve process and the Desktop's authenticated WS transport."""

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time


class NativeGateway:
    def __init__(self, home):
        self.home = home
        self.process = self.socket = self.log = None
        self.request_id = 0

    def start(self):
        from hermes_cli._launchers import runtime_command
        from websockets.sync.client import connect

        token = secrets.token_urlsafe(32)
        self.log = os.fdopen(
            os.open(
                self.home / "capabilities-server.log",
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            ),
            "w",
        )
        command = runtime_command(
            Path("/opt/hermes"),
            ["serve", "--host", "127.0.0.1", "--port", "19119", "--no-open"],
            home=self.home,
            python="/opt/hermes/.venv/bin/python",
        )
        self.process = subprocess.Popen(
            command,
            env={**os.environ, "HERMES_DASHBOARD_SESSION_TOKEN": token},
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    "Native serve exited; inspect the private capabilities-server.log"
                )
            try:
                self.socket = connect("ws://127.0.0.1:19119/api/ws?token=" + token, open_timeout=2)
                return
            except (OSError, TimeoutError):
                time.sleep(0.5)
        raise TimeoutError("Native serve did not become ready")

    def call(self, method, **params):
        self.request_id += 1
        self.socket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": method,
                    "params": params,
                }
            )
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            for line in self.socket.recv(timeout=max(1, deadline - time.monotonic())).splitlines():
                response = json.loads(line)
                if response.get("id") == self.request_id:
                    assert "error" not in response, response
                    return response["result"]
        raise TimeoutError("Native RPC did not respond")

    def close(self):
        if self.socket:
            self.socket.close()
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if self.log:
            self.log.close()
