from __future__ import annotations

import datetime
import logging
import random
import socket
import subprocess
import sys
import threading
from pathlib import PurePath
from time import sleep
from types import TracebackType
from typing import IO, Callable, Generator, List, Optional, TextIO, Type, Union

import pytest

__all__ = (
    "ShinyAppProc",
    "create_app_fixture",
    "create_doc_example_fixture",
    "create_example_fixture",
    "local_app",
    "run_shiny_app",
)

here = PurePath(__file__).parent


def random_port():
    while True:
        port = random.randint(1024, 49151)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except Exception:
                # Let's just assume that port was in use; try again
                continue


class OutputStream:
    """Designed to wrap an IO[str] and accumulate the output using a bg thread

    Also allows for blocking waits for particular lines."""

    def __init__(self, io: IO[str], desc: Optional[str] = None):
        self._io = io
        self._closed = False
        self._lines: List[str] = []
        self._cond = threading.Condition()
        self._thread = threading.Thread(
            group=None, target=self._run, daemon=True, name=desc
        )

        self._thread.start()

    def _run(self):
        """Pump lines into self._lines in a tight loop."""

        try:
            while not self._io.closed:
                try:
                    line = self._io.readline()
                except ValueError:
                    # This is raised when the stream is closed
                    break
                if line is None:
                    break
                if line != "":
                    with self._cond:
                        self._lines.append(line)
                        self._cond.notify_all()
        finally:
            # If we got here, we're finished reading self._io and need to signal any
            # waiters that we're done and they'll never hear from us again.
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def wait_for(self, predicate: Callable[[str], bool], timeoutSecs: float) -> bool:
        timeoutAt = datetime.datetime.now() + datetime.timedelta(seconds=timeoutSecs)
        pos = 0
        with self._cond:
            while True:
                while pos < len(self._lines):
                    if predicate(self._lines[pos]):
                        return True
                    pos += 1
                if self._closed:
                    return False
                else:
                    remaining = (timeoutAt - datetime.datetime.now()).total_seconds()
                    if remaining < 0 or not self._cond.wait(timeout=remaining):
                        # Timed out
                        raise TimeoutError(
                            "Timeout while waiting for Shiny app to become ready"
                        )

    def __str__(self):
        with self._cond:
            return "".join(self._lines)


def dummyio() -> TextIO:
    io = TextIO()
    io.close()
    return io


class ShinyAppProc:
    def __init__(self, proc: subprocess.Popen[str], port: int):
        self.proc = proc
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self.stdout = OutputStream(proc.stdout or dummyio())
        self.stderr = OutputStream(proc.stderr or dummyio())
        threading.Thread(group=None, target=self._run, daemon=True).start()

    def _run(self) -> None:
        self.proc.wait()
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        if self.proc.stderr is not None:
            self.proc.stderr.close()

    def close(self) -> None:
        sleep(0.5)
        self.proc.terminate()

    def __enter__(self) -> ShinyAppProc:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ):
        self.close()

    def wait_until_ready(self, timeoutSecs: float) -> None:
        if self.stderr.wait_for(
            lambda line: "Uvicorn running on" in line, timeoutSecs=timeoutSecs
        ):
            return
        else:
            raise RuntimeError("Shiny app exited without ever becoming ready")


def run_shiny_app(
    app_file: Union[str, PurePath],
    *,
    port: int = 0,
    cwd: Optional[str] = None,
    wait_for_start: bool = True,
    timeout_secs: float = 10,
    bufsize: int = 64 * 1024,
) -> ShinyAppProc:
    if port == 0:
        port = random_port()

    child = subprocess.Popen(
        [sys.executable, "-m", "shiny", "run", "--port", str(port), str(app_file)],
        bufsize=bufsize,
        executable=sys.executable,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        encoding="utf-8",
    )

    # TODO: Detect early exit

    sa = ShinyAppProc(child, port)
    if wait_for_start:
        sa.wait_until_ready(timeout_secs)
    return sa


def create_app_fixture(app: Union[PurePath, str], scope: str = "module"):
    def fixture_func():
        sa = run_shiny_app(app, wait_for_start=False)
        try:
            with sa:
                sa.wait_until_ready(30)
                yield sa
        finally:
            logging.warning("Application output:\n" + str(sa.stderr))

    return pytest.fixture(
        scope=scope,  # type: ignore
    )(fixture_func)


def create_example_fixture(example_name: str, scope: str = "module"):
    """Used to create app fixtures from apps in py-shiny/examples"""
    return create_app_fixture(here / "../examples" / example_name / "app.py", scope)


def create_doc_example_fixture(example_name: str, scope: str = "module"):
    """Used to create app fixtures from apps in py-shiny/shiny/examples"""
    return create_app_fixture(
        here / "../shiny/examples" / example_name / "app.py", scope
    )


@pytest.fixture(scope="module")
def local_app(request: pytest.FixtureRequest) -> Generator[ShinyAppProc, None, None]:
    sa = run_shiny_app(PurePath(request.path).parent / "app.py", wait_for_start=False)
    try:
        with sa:
            sa.wait_until_ready(30)
            yield sa
    finally:
        logging.warning("Application output:\n" + str(sa.stderr))
