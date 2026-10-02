"""Optional, read-only access to an existing local managed Codex app-server.

The public 0.160.0 SDK has thread/read but no thread-scoped usage snapshot.
Never consume its global notification stream or return conversation metadata.
"""

import importlib
import math
import os
from pathlib import Path
import queue
import stat
import threading

if __package__:
    from .codex_context import parse_request_context
else:
    from codex_context import parse_request_context


SDK_VERSION = "0.160.0"
SOCKET_ENV = "SESSION_HANDOFF_CODEX_APP_SERVER_SOCKET"
_STATUSES = {"notLoaded", "idle", "systemError", "active"}


class _CallTimeout(Exception):
    pass


def _reject_server_request(_method, _params):
    # The SDK's default handler accepts command and file-change approvals.
    # Raising closes its reader without sending any approval response.
    raise PermissionError("Read-only adapter cannot answer server requests")


def _bounded_call(operation, timeout, *, cancelled=None, late_cleanup=None):
    """Bound SDK calls without an executor whose shutdown can wait forever."""
    results = queue.Queue(maxsize=1)
    cancelled = cancelled if cancelled is not None else threading.Event()

    def run():
        try:
            results.put((True, operation()))
        except Exception as exc:
            results.put((False, exc))
        finally:
            # start() can finish after the caller has already attempted close().
            # This second cleanup prevents a late proxy launch from being leaked.
            if cancelled.is_set() and late_cleanup is not None:
                try:
                    late_cleanup()
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True, name="codex-app-server-read").start()
    try:
        succeeded, result = results.get(timeout=timeout)
    except queue.Empty:
        cancelled.set()
        raise _CallTimeout from None
    if not succeeded:
        raise result
    return result


class CodexAppServerAdapter:
    """Read only a caller-supplied thread ID; each call closes its proxy.

    Missing SDK/socket, errors and timeouts return None. ``unavailable_reason``
    contains a fixed diagnostic code, never server messages or private content.
    """

    def __init__(
        self,
        socket_path=None,
        *,
        startup_timeout=2.0,
        request_timeout=2.0,
        close_timeout=1.0,
    ):
        for timeout in (startup_timeout, request_timeout, close_timeout):
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("Timeouts must be finite positive numbers")
        if socket_path is None:
            socket_path = os.environ.get(SOCKET_ENV)
            if socket_path is None:
                codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
                socket_path = codex_home / "app-server-control" / "app-server-control.sock"
        self.socket_path = socket_path
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.close_timeout = close_timeout
        self.unavailable_reason = None

    def thread_context(self, thread_id):
        """Return validated runtime status, with unavailable token usage as None."""
        self.unavailable_reason = None
        if parse_request_context({"_meta": {"threadId": thread_id}}).thread_id is None:
            self.unavailable_reason = "invalid_thread_id"
            return None
        try:
            socket_path = Path(self.socket_path).expanduser()
            if not socket_path.is_absolute() or not stat.S_ISSOCK(socket_path.stat().st_mode):
                raise ValueError("Expected an existing absolute Unix socket")
        except (TypeError, ValueError, OSError):
            self.unavailable_reason = "socket_unavailable"
            return None
        try:
            sdk = importlib.import_module("openai_codex")
            if getattr(sdk, "__version__", None) != SDK_VERSION:
                self.unavailable_reason = "sdk_version_unsupported"
                return None
            sdk_client = importlib.import_module("openai_codex.client")
            client = sdk_client.CodexClient(
                config=sdk.CodexConfig(
                    launch_args_override=(
                        "codex", "app-server", "proxy", "--sock", str(socket_path)
                    ),
                    client_name="session_handoff_status",
                    client_title="Session Handoff status",
                    experimental_api=False,
                ),
                approval_handler=_reject_server_request,
            )
        except (ImportError, AttributeError):
            self.unavailable_reason = "sdk_unavailable"
            return None
        except Exception:
            self.unavailable_reason = "connection_failed"
            return None

        result = None
        phase = "startup"
        cancelled = threading.Event()

        def startup():
            client.start()
            if not cancelled.is_set():
                client.initialize()

        try:
            _bounded_call(
                startup, self.startup_timeout,
                cancelled=cancelled, late_cleanup=client.close,
            )
            phase = "request"
            response = _bounded_call(
                lambda: client.thread_read(thread_id, include_turns=False),
                self.request_timeout,
                cancelled=cancelled, late_cleanup=client.close,
            )
            thread = getattr(response, "thread", None)
            status = getattr(getattr(getattr(thread, "status", None), "root", None), "type", None)
            if getattr(thread, "id", None) != thread_id or not isinstance(status, str) or status not in _STATUSES:
                self.unavailable_reason = "invalid_response"
            else:
                result = {
                    "thread_id": thread_id,
                    "status": status,
                    "source": "codex_app_server",
                    "token_usage": None,
                }
        except _CallTimeout:
            self.unavailable_reason = f"{phase}_timeout"
        except Exception:
            self.unavailable_reason = "connection_failed"
        finally:
            try:
                _bounded_call(client.close, self.close_timeout)
            except _CallTimeout:
                self.unavailable_reason = self.unavailable_reason or "close_timeout"
                result = None
            except Exception:
                self.unavailable_reason = self.unavailable_reason or "close_failed"
                result = None
        return result
