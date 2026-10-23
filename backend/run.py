"""Local launcher; the bound socket is retained until Uvicorn takes it over."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn
from server import app

HOST = '127.0.0.1'
PREFERRED_PORT = 8756


def _bind_listener(preferred: int) -> socket.socket:
    for port in dict.fromkeys([preferred, 0]):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((HOST, port))
            return listener
        except OSError:
            listener.close()
            if port == 0:
                raise
    raise OSError('No available local port')


def _open_browser_when_ready(url: str, port: int) -> bool:
    for _ in range(300):
        try:
            with socket.create_connection((HOST, port), timeout=.2):
                webbrowser.open(url)
                return True
        except OSError:
            time.sleep(.1)
    return False


def main():
    if sys.argv[1:] == ['--check-runtime']:
        # Exercise the real TLS stack in the frozen build without opening a
        # connection, reading an API key or spending any tokens.
        import httpx
        with httpx.Client():
            pass
        print('HTTPS runtime OK', flush=True)
        return
    preferred = int(os.environ.get('STORY_ENGINE_PORT', PREFERRED_PORT))
    with _bind_listener(preferred) as listener:
        port = listener.getsockname()[1]
        url = f'http://{HOST}:{port}'
        print(f'Story Engine: {url}', flush=True)
        if os.environ.get('STORY_ENGINE_NO_BROWSER') != '1':
            threading.Thread(target=_open_browser_when_ready, args=(url, port), daemon=True).start()
        server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level='info'))
        server.run(sockets=[listener])


if __name__ == '__main__':
    main()
