import sys
import socket
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run


class LauncherTests(unittest.TestCase):
    def test_listener_stays_reserved_and_occupied_port_uses_actual_free_port(self):
        with run._bind_listener(0) as first:
            port = first.getsockname()[1]
            first.listen()
            with run._bind_listener(port) as second:
                self.assertNotEqual(second.getsockname()[1], port)
                self.assertGreater(second.getsockname()[1], 0)
                second.listen()
                with socket.create_connection((run.HOST, second.getsockname()[1]), timeout=1):
                    pass

    def test_browser_is_not_opened_when_server_never_starts(self):
        with (patch.object(run.socket, 'create_connection', side_effect=OSError('offline')),
              patch.object(run.time, 'sleep'), patch.object(run.webbrowser, 'open') as browser):
            self.assertFalse(run._open_browser_when_ready('http://localhost:1', 1))
        browser.assert_not_called()

    def test_browser_opens_after_readiness_probe(self):
        with run._bind_listener(0) as listener, patch.object(run.webbrowser, 'open') as browser:
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(run._open_browser_when_ready(f'http://localhost:{port}', port))
        browser.assert_called_once_with(f'http://localhost:{port}')
