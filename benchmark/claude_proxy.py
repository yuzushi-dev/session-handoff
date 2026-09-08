"""Disposable loopback proxy; existing credentials are mounted read-only."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import os
import re
import signal
import socket
import stat
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

PROXY_BINARY = Path("/home/cristina/.local/bin/claude-code-proxy")
PROXY_SHA256 = "146f2e9dde5283998eb24f3abc044513876cd04fcdb85ae11dffbc82565f78dd"


def _owns_listener(process, port, binary):
    result = subprocess.run(
        ["/usr/bin/ss", "-H", "-lntp", "sport", f"= :{port}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    addresses = {
        line.split()[3] for line in result.stdout.splitlines() if len(line.split()) >= 4
    }
    if addresses != {f"127.0.0.1:{port}"}:
        return False
    owners = {int(value) for value in re.findall(r"pid=(\d+)", result.stdout)}
    if not owners:
        return False
    try:
        return all(
            os.getpgid(pid) == process.pid
            and os.path.samefile(f"/proc/{pid}/exe", binary)
            for pid in owners
        )
    except (OSError, ProcessLookupError):
        return False


@contextmanager
def isolated_proxy(state_root, auth_path, *, authorized=False):
    if not authorized:
        raise ValueError("explicit authorization required")
    auth = Path(auth_path).absolute()
    try:
        mode = auth.lstat().st_mode
    except OSError as exc:
        raise ValueError("credentials must be an existing regular file") from exc
    if not stat.S_ISREG(mode):
        raise ValueError("credentials must be a regular file, not a symlink")
    binary = PROXY_BINARY.resolve(strict=True)
    with binary.open("rb") as executable:
        digest = hashlib.sha256()
        for chunk in iter(lambda: executable.read(1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != PROXY_SHA256:
            raise ValueError("proxy binary hash differs from the verified build")
    version = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    if version.stdout.strip() != "claude-code-proxy 0.1.22":
        raise ValueError("proxy version is not pinned 0.1.22")
    state = Path(state_root).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    command = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        str(Path.home()),
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/mnt",
        "--dir",
        "/mnt/bin",
        "--ro-bind",
        str(binary),
        "/mnt/bin/proxy",
        "--dir",
        "/mnt/home",
        "--dir",
        "/mnt/config/codex",
        "--ro-bind",
        str(auth),
        "/mnt/config/codex/auth.json",
        "--dir",
        "/mnt/state",
        "--chdir",
        "/mnt/state",
        "--",
        "/mnt/bin/proxy",
        "serve",
        "--port",
        str(port),
        "--no-monitor",
    ]
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/mnt/home",
        "LANG": "C",
        "CCP_CONFIG_DIR": "/mnt/config",
        "XDG_STATE_HOME": "/mnt/state",
        "XDG_CONFIG_HOME": "/mnt/home",
        "CCP_BIND_ADDRESS": "127.0.0.1",
        "CCP_CODEX_TRANSPORT": "http",
        "CCP_CODEX_EFFORT": "xhigh",
        "CCP_COMPACT_EFFORT": "off",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    # Do not persist proxy logs: upstream errors can contain credential material.
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("isolated proxy exited before readiness")
            try:
                with urlopen(endpoint + "/healthz", timeout=0.2) as response:
                    if response.status == 200:
                        if not _owns_listener(process, port, binary):
                            raise RuntimeError(
                                "isolated proxy listener ownership failed"
                            )
                        break
            except URLError:
                time.sleep(0.1)
        else:
            raise RuntimeError("isolated proxy readiness timed out")
        yield endpoint
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
