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
import logging
import os
import signal
import sys
import threading
import time

try:
    import termios
    import tty
except ImportError:
    # Windows has neither termios nor tty. connect_console() below uses the
    # msvcrt console API there, and line-buffered input everywhere else.
    termios = None  # type: ignore
    tty = None  # type: ignore
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore
from urllib.parse import urlparse

import websocket

from colab_cli.state import SessionState

logger = logging.getLogger(__name__)

# Global flag to stop the read thread when the websocket closes
_is_running = False
_last_error = None

# When stdin is piped and reaches EOF, we send "exit\n" to the remote shell and
# then wait this many seconds for any remaining output (the shell's goodbye,
# tmux teardown messages, etc.) to flush before closing the websocket from the
# client side. Empirically 0.5s is enough for the typical /colab/tty backend
# wrapped in tmux + bash; bumping it just delays exit, lowering it risks
# truncating tail output.
PIPED_EOF_GRACE_SECONDS = 0.5

# Windows has no SIGWINCH, so the console polls for size changes instead.
RESIZE_POLL_SECONDS = 0.5

# msvcrt.getwch() reports special keys as a "\x00"/"\xe0" prefix followed by
# a scan code; map those to the xterm sequences the remote tmux expects.
_WINDOWS_KEY_SEQUENCES = {
    "H": "\x1b[A",  # Up
    "P": "\x1b[B",  # Down
    "M": "\x1b[C",  # Right
    "K": "\x1b[D",  # Left
    "G": "\x1b[H",  # Home
    "O": "\x1b[F",  # End
    "I": "\x1b[5~",  # Page Up
    "Q": "\x1b[6~",  # Page Down
    "R": "\x1b[2~",  # Insert
    "S": "\x1b[3~",  # Delete
    ";": "\x1bOP",  # F1
    "<": "\x1bOQ",  # F2
    "=": "\x1bOR",  # F3
    ">": "\x1bOS",  # F4
    "?": "\x1b[15~",  # F5
    "@": "\x1b[17~",  # F6
    "A": "\x1b[18~",  # F7
    "B": "\x1b[19~",  # F8
    "C": "\x1b[20~",  # F9
    "D": "\x1b[21~",  # F10
}

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def on_message(ws, message):
    """Callback for when a message is received from the server."""
    try:
        data = json.loads(message)
        if "data" in data:
            # The backend sends raw ANSI escape sequences and string content.
            # We write it directly to stdout buffer to avoid python print() formatting.
            sys.stdout.buffer.write(data["data"].encode("utf-8"))
            sys.stdout.buffer.flush()
    except Exception as e:
        logger.debug(f"Error parsing message: {e}")


def on_error(ws, error):
    """Callback for when a websocket error occurs."""
    global _last_error
    _last_error = error
    logger.error(f"WebSocket Error: {error}")


def on_close(ws, close_status_code, close_msg):
    """Callback for when the websocket is closed."""
    global _is_running
    _is_running = False


def send_terminal_size(ws):
    """Sends the current terminal size to the remote backend."""
    try:
        size = os.get_terminal_size()
        payload = json.dumps({"cols": size.columns, "rows": size.lines})
        ws.send(payload)
    except Exception as e:
        logger.debug(f"Failed to send terminal size: {e}")


def read_windows_key():
    """Reads one keypress from the Windows console as terminal input.

    Uses ``msvcrt.getwch`` rather than ``sys.stdin.read`` because the latter
    treats Ctrl-Z as end-of-file, which would end the session.
    """
    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        # Unmapped special keys (e.g. Ctrl+arrows) are dropped.
        return _WINDOWS_KEY_SEQUENCES.get(msvcrt.getwch(), "")
    if char == "\x08":
        # Backspace: Unix terminals send DEL.
        return "\x7f"
    return char


def enable_windows_console_mode():
    """Prepares the Windows console for a remote terminal session.

    Enables ANSI escape processing on stdout and disables Ctrl-C processing on
    stdin so the keypress reaches the remote shell instead of killing us.

    Returns:
      A callback that restores the original console modes.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    saved = []
    for std_handle, set_bits, clear_bits in (
        (_STD_INPUT_HANDLE, 0, _ENABLE_PROCESSED_INPUT),
        (_STD_OUTPUT_HANDLE, _ENABLE_VIRTUAL_TERMINAL_PROCESSING, 0),
    ):
        handle = kernel32.GetStdHandle(std_handle)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            continue
        saved.append((handle, mode.value))
        kernel32.SetConsoleMode(handle, (mode.value | set_bits) & ~clear_bits)

    def restore():
        for handle, mode in saved:
            kernel32.SetConsoleMode(handle, mode)

    return restore


def watch_terminal_size(ws, stop):
    """Sends the terminal size whenever it changes, until ``stop`` is set."""
    try:
        last = os.get_terminal_size()
    except OSError:
        return
    while not stop.wait(RESIZE_POLL_SECONDS):
        try:
            size = os.get_terminal_size()
        except OSError:
            continue
        if size != last and _is_running:
            send_terminal_size(ws)
        last = size


def on_open(ws):
    """Callback for when the websocket connection is opened."""
    global _is_running
    _is_running = True

    # Send initial terminal size
    send_terminal_size(ws)

    # Setup the background thread to read from stdin
    def read_stdin():
        is_tty = sys.stdin.isatty()
        use_msvcrt = msvcrt is not None and is_tty
        while _is_running:
            try:
                if use_msvcrt:
                    char = read_windows_key()
                    if not char:
                        continue
                    ws.send(json.dumps({"data": char}))
                    continue
                # Read a single character (or escape sequence byte)
                char = sys.stdin.read(1)
                if not char:
                    if not is_tty:
                        # Piped input has reached EOF. The remote /colab/tty
                        # endpoint wraps bash in tmux which intercepts \x04
                        # (Ctrl-D) as a literal character, so it never exits.
                        # Instead send "exit\n" so bash voluntarily terminates,
                        # wait a short grace period for the shell's goodbye
                        # output to drain back to us, then close the websocket
                        # ourselves to guarantee the client unblocks.
                        try:
                            ws.send(json.dumps({"data": "exit\n"}))
                        except Exception:
                            pass
                        time.sleep(PIPED_EOF_GRACE_SECONDS)
                        try:
                            ws.close()
                        except Exception:
                            pass
                    break
                ws.send(json.dumps({"data": char}))
            except Exception:
                break

    thread = threading.Thread(target=read_stdin, daemon=True)
    thread.start()


def connect_console(session: SessionState):
    """
    Connects to the Colab TTY endpoint and sets up a raw terminal session.
    """
    global _is_running, _last_error
    _last_error = None

    # Construct the WebSocket URL from the base URL
    parsed = urlparse(session.url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/colab/tty?colab-runtime-proxy-token={session.token}"

    is_tty = sys.stdin.isatty()
    fd = None
    old_settings = None
    can_raw = termios is not None and tty is not None and is_tty
    if can_raw:
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
        except Exception:
            fd = None
            old_settings = None
            can_raw = False

    ws = websocket.WebSocketApp(
        url=ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def handle_sigwinch(signum, frame):
        """Handle window resize events."""
        if _is_running:
            send_terminal_size(ws)

    sigwinch = getattr(signal, "SIGWINCH", None)
    restore_windows_console = None
    stop_resize_watch = threading.Event()
    try:
        if can_raw:
            tty.setraw(fd, termios.TCSANOW)
            if sigwinch is not None:
                signal.signal(sigwinch, handle_sigwinch)
        elif msvcrt is not None and is_tty:
            restore_windows_console = enable_windows_console_mode()
            threading.Thread(
                target=watch_terminal_size,
                args=(ws, stop_resize_watch),
                daemon=True,
            ).start()

        # This is a blocking call until the connection is closed
        ws.run_forever()

        if _last_error:
            # Re-raise or wrap terminal errors
            err_msg = str(_last_error)
            if "404" in err_msg or "401" in err_msg:
                # We raise a standard exception that the caller can recognize
                raise RuntimeError(f"Connection failed: {err_msg}")
    finally:
        stop_resize_watch.set()
        if restore_windows_console is not None:
            restore_windows_console()
        if can_raw:
            # Always ensure the terminal is restored to its original state
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            except Exception:
                pass
            # Restore the default signal handler for resize
            if sigwinch is not None:
                try:
                    signal.signal(sigwinch, signal.SIG_DFL)
                except Exception:
                    pass
        print("\r\nConnection closed.")
