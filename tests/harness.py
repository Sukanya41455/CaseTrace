from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


class FixtureServer:
    def __init__(
        self,
        scenario: str = "normal",
        port: int | None = None,
        launcher: str = "module",
    ) -> None:
        self.scenario = scenario
        self.port = port or self._available_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.launcher = launcher
        self._process: subprocess.Popen[str] | None = None

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def start(self) -> FixtureServer:
        if self._process is not None:
            return self
        module = "casetrace.cli" if self.launcher == "cli" else "casetrace.fixture.app"
        command = [sys.executable, "-m", module]
        if self.launcher == "cli":
            command.append("fixture")
        command.extend(
            [
                "--scenario",
                self.scenario,
                "--port",
                str(self.port),
            ]
        )
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                stdout, stderr = self._process.communicate()
                raise RuntimeError(f"fixture exited before becoming ready: {stdout}\n{stderr}")
            try:
                with urllib.request.urlopen(self.url, timeout=0.25) as response:
                    if response.status == 200:
                        return self
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        self.stop()
        raise TimeoutError(f"fixture did not become ready at {self.url}")

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    @staticmethod
    def _available_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
