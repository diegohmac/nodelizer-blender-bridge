# SPDX-License-Identifier: GPL-3.0-or-later
"""The connection to the host app: a TCP socket to 127.0.0.1 only.

A background thread connects, says HELLO with the session token and reads lines. Messages are
handed to Blender's main thread through a queue, since bpy may only be used there. Files stay
the source of truth: if the socket is down, results are still written to the session folder and
the host picks them up later.
"""

from __future__ import annotations

import queue
import socket
import threading
import time

from . import protocol

LOOPBACK = "127.0.0.1"
CONNECT_TIMEOUT = 5.0
# Waits between attempts grow up to this while the host isn't there.
MOST_RETRY = 30.0


class Client:
    def __init__(self, port: int, token: str, hello: dict):
        if not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError("bad port")
        if not protocol.valid_token(token):
            raise ValueError("bad token")
        self._port = port
        self._hello = hello
        self._socket = None
        self._write = threading.Lock()
        self._stop = threading.Event()
        self.incoming: queue.Queue = queue.Queue()
        self.state = "connecting"
        self._thread = threading.Thread(target=self._run, name="nodelizer-bridge", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def send(self, message: dict) -> bool:
        """Sends one message. False when the host isn't connected; nothing is queued."""
        sock = self._socket
        if sock is None or not self.connected:
            return False
        try:
            data = protocol.encode(message)
            with self._write:
                sock.sendall(data)
            return True
        except (OSError, ValueError):
            return False

    def _run(self):
        wait = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                wait = 1.0
            except OSError:
                pass
            self._socket = None
            if self.state == "rejected" or self._stop.is_set():
                return
            self.state = "disconnected"
            self._stop.wait(wait)
            wait = min(wait * 2, MOST_RETRY)

    def _session(self):
        sock = socket.create_connection((LOOPBACK, self._port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(None)
        self._socket = sock
        with self._write:
            sock.sendall(protocol.encode(self._hello))
        buffer = b""
        welcomed = False
        while not self._stop.is_set():
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > protocol.MAX_LINE_BYTES and b"\n" not in buffer:
                break
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = protocol.decode(line)
                if message is None:
                    continue
                if not welcomed:
                    if message["type"] == "REJECTED":
                        self.state = "rejected"
                        self.incoming.put(message)
                        sock.close()
                        return
                    if message["type"] != "WELCOME":
                        continue
                    welcomed = True
                    self.state = "connected"
                self.incoming.put(message)
        sock.close()


def wait_for(predicate, timeout: float, step: float = 0.05) -> bool:
    """Polls `predicate` until it's true or `timeout` seconds pass. For tests."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
