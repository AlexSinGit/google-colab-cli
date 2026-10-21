# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import sys
from unittest.mock import MagicMock, patch

try:
    import termios
except ImportError:
    # Windows has no termios; tests requiring raw-terminal mode are skipped.
    termios = None  # type: ignore

import colab_cli.console as console_mod
from colab_cli.console import connect_console, on_message, on_open
from colab_cli.state import SessionState
import pytest

needs_termios = pytest.mark.skipif(
    termios is None, reason="requires termios (POSIX only)"
)


@pytest.fixture(autouse=True)
def no_msvcrt():
    """Runs tests on the POSIX/piped path unless they opt into msvcrt.

    On Windows the real msvcrt would otherwise block reading the console.
    """
    with patch.object(console_mod, "msvcrt", None):
        yield


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev",
        endpoint="some-endpoint",
    )


@needs_termios
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.fileno")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_initialization(
    mock_isatty,
    mock_fileno,
    mock_get_term_size,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    # Setup mocks
    mock_isatty.return_value = True
    mock_fileno.return_value = 0
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_tcgetattr.return_value = ["fake_attrs"]
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance

    # We don't want run_forever to actually block or start threads in the test
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # 1. Verify URL transformation
    expected_url = "wss://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev/colab/tty?colab-runtime-proxy-token=test-token"
    mock_ws_app.assert_called_once()
    assert mock_ws_app.call_args[1]["url"] == expected_url

    # 2. Verify raw mode setup and teardown
    mock_tcgetattr.assert_called_once_with(sys.stdin.fileno())
    mock_setraw.assert_called_once_with(sys.stdin.fileno(), termios.TCSANOW)

    # Teardown should happen in a finally block
    mock_tcsetattr.assert_called_once_with(
        sys.stdin.fileno(), termios.TCSANOW, ["fake_attrs"]
    )


@needs_termios
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_piped_input(
    mock_isatty,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    mock_isatty.return_value = False
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # In a piped environment, we should not attempt to use termios or tty
    mock_tcgetattr.assert_not_called()
    mock_setraw.assert_not_called()
    mock_tcsetattr.assert_not_called()


@patch("colab_cli.console.os.get_terminal_size")
def test_on_open_sends_terminal_size(mock_get_term_size):
    mock_ws = MagicMock()
    mock_get_term_size.return_value = os.terminal_size((100, 40))

    on_open(mock_ws)

    # Verify that the initial terminal size is sent
    mock_ws.send.assert_called_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload == {"cols": 100, "rows": 40}


@patch("colab_cli.console.sys.stdout.buffer.write")
@patch("colab_cli.console.sys.stdout.buffer.flush")
def test_on_message_writes_to_stdout(mock_flush, mock_write):
    mock_ws = MagicMock()
    test_data = "Hello \x1b[34mWorld\x1b[0m"
    message_json = json.dumps({"data": test_data})

    on_message(mock_ws, message_json)

    # Verify that the data is written exactly as received
    mock_write.assert_called_once_with(test_data.encode("utf-8"))
    mock_flush.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_piped_sends_exit_and_closes_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is piped and reaches EOF, the read thread should send 'exit\\n'
    to the remote shell and then close the websocket from the client side.

    The remote shell at /colab/tty is wrapped in tmux which swallows the bare
    \\x04 (Ctrl-D) we used to send, so EOF used to leave the websocket open
    indefinitely. Sending 'exit\\n' + ws.close() guarantees clean termination.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = False
    # Simulate piped stdin: returns one line then EOF
    mock_stdin.read.side_effect = ["e", "c", "h", "o", " ", "h", "i", "\n", ""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    # on_open spawns the read thread; we want it to run synchronously here
    # so we patch threading.Thread to call target immediately and join().
    real_thread = []

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target
            real_thread.append(self)

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread):
        # Use a tiny grace period for the test
        with patch("colab_cli.console.PIPED_EOF_GRACE_SECONDS", 0.01):
            on_open(mock_ws)

    # Collect what was sent to the websocket
    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]

    # Initial send is the terminal size; everything after is stdin chars or our exit string.
    # Verify "exit\n" was sent on EOF (one send per character)
    assert {"data": "exit\n"} in sent_payloads, (
        f"Expected 'exit\\n' to be sent on piped EOF, got: {sent_payloads}"
    )

    # Verify we closed the websocket from the client side
    mock_ws.close.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_tty_does_not_close_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is a real TTY and read() returns empty (which happens on
    Ctrl-D in raw mode), we should NOT inject 'exit\\n' or close the websocket
    \u2014 the user is in interactive mode and may have intended Ctrl-D as a literal
    char. The websocket lifecycle is owned by the remote shell in this case.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    # TTY EOF is rare but possible; should be passed through transparently
    mock_stdin.read.side_effect = [""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread):
        on_open(mock_ws)

    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert {"data": "exit\n"} not in sent_payloads
    mock_ws.close.assert_not_called()


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_no_termios_degrades_gracefully(
    mock_isatty, mock_ws_app, mock_session
):
    """Windows has no termios/tty/SIGWINCH. connect_console must not crash.

    Regression test for `ModuleNotFoundError: No module named 'termios'`
    which broke every `colab` command on Windows via
    cli -> execution -> console imports.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with (
        patch.object(console_mod, "termios", None),
        patch.object(console_mod, "tty", None),
        patch("colab_cli.console.threading.Thread"),
    ):
        connect_console(mock_session)

    mock_ws_instance.run_forever.assert_called_once()


class _SyncThread:
    """Runs a thread target inline so the read loop can be asserted on."""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["a"], "a"),
        (["\r"], "\r"),
        (["\x03"], "\x03"),
        (["\x08"], "\x7f"),
        (["\xe0", "H"], "\x1b[A"),
        (["\xe0", "K"], "\x1b[D"),
        (["\x00", ";"], "\x1bOP"),
        (["\xe0", "s"], ""),
    ],
    ids=["char", "enter", "ctrl-c", "backspace", "up", "left", "f1", "unmapped"],
)
def test_read_windows_key_translates_to_xterm(keys, expected):
    """Windows scan codes become the xterm sequences the remote tmux expects."""
    fake_msvcrt = MagicMock()
    fake_msvcrt.getwch.side_effect = keys
    with patch.object(console_mod, "msvcrt", fake_msvcrt):
        assert console_mod.read_windows_key() == expected


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
def test_read_stdin_windows_tty_sends_keys(mock_isatty, mock_get_term_size):
    """A Windows console TTY reads keypresses via msvcrt, not sys.stdin."""
    mock_isatty.return_value = True
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    fake_msvcrt = MagicMock()
    # Ctrl-Z must be forwarded, not treated as EOF; the error ends the loop.
    fake_msvcrt.getwch.side_effect = ["l", "\x1a", "\xe0", "H", OSError()]
    mock_ws = MagicMock()

    console_mod._is_running = True
    with (
        patch.object(console_mod, "msvcrt", fake_msvcrt),
        patch("colab_cli.console.threading.Thread", _SyncThread),
    ):
        on_open(mock_ws)

    sent = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert sent[1:] == [{"data": "l"}, {"data": "\x1a"}, {"data": "\x1b[A"}]


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_windows_sets_and_restores_console_mode(
    mock_isatty, mock_ws_app, mock_session
):
    """On a Windows TTY the console mode is changed, then always restored."""
    mock_isatty.return_value = True
    mock_ws_app.return_value.run_forever.side_effect = RuntimeError("boom")
    restore = MagicMock()

    with (
        patch.object(console_mod, "termios", None),
        patch.object(console_mod, "tty", None),
        patch.object(console_mod, "msvcrt", MagicMock()),
        patch.object(
            console_mod, "enable_windows_console_mode", return_value=restore
        ) as enable,
        patch("colab_cli.console.threading.Thread") as thread,
        pytest.raises(RuntimeError),
    ):
        connect_console(mock_session)

    enable.assert_called_once()
    restore.assert_called_once()
    assert thread.call_args.kwargs["target"] is console_mod.watch_terminal_size


@patch("colab_cli.console.send_terminal_size")
@patch("colab_cli.console.os.get_terminal_size")
def test_watch_terminal_size_sends_only_on_change(mock_get_size, mock_send):
    """The resize poller (Windows has no SIGWINCH) reports changes only."""
    mock_get_size.side_effect = [
        os.terminal_size((80, 24)),
        os.terminal_size((80, 24)),
        os.terminal_size((120, 40)),
    ]
    stop = MagicMock()
    stop.wait.side_effect = [False, False, True]
    ws = MagicMock()

    console_mod._is_running = True
    console_mod.watch_terminal_size(ws, stop)

    mock_send.assert_called_once_with(ws)


@pytest.mark.skipif(os.name != "nt", reason="Windows console API only")
def test_enable_windows_console_mode_returns_restore():
    """Without an attached console this is a safe no-op, never a crash."""
    restore = console_mod.enable_windows_console_mode()
    restore()
