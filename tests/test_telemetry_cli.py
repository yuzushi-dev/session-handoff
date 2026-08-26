import builtins
import importlib.util
import json
import multiprocessing
import os
import socket
import stat
import threading
import time
from types import SimpleNamespace
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import server.telemetry as telemetry


ROOT = Path(__file__).parents[1]
CONFIG = Path(".config/session-handoff/telemetry.json")


def load_cli():
    loader = SourceFileLoader("session_handoff_cli", str(ROOT / "bin/session-handoff"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeStdin:
    def __init__(self, interactive):
        self.interactive = interactive

    def isatty(self):
        return self.interactive


def fake_setup(monkeypatch, cli, home, answers, *, interactive=True):
    fake_client = home / "bin/codex"
    fake_client.parent.mkdir(parents=True, exist_ok=True)
    fake_client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_client.chmod(0o755)
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(home))
    monkeypatch.setattr(cli.shutil, "which", lambda _: str(fake_client))
    monkeypatch.setattr(cli, "install_setup", lambda *args, **kwargs: {"already_configured": False})
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(interactive))
    answers = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))


def test_status_is_disabled_by_default(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))

    assert cli._telemetry(["status"]) == 0
    assert "disabled" in capsys.readouterr().out.lower()


def test_enable_requires_interactive_explicit_yes(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "yes")

    assert cli._telemetry(["enable"]) == 0
    config = json.loads((tmp_path / CONFIG).read_text(encoding="utf-8"))
    assert config["enabled"] is True
    assert "Telemetry consent recorded." in capsys.readouterr().out


def test_enable_prompt_points_to_docs_instead_of_inline_disclosure(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    seen_prompt = {}

    def input_fn(prompt):
        seen_prompt["text"] = prompt
        return "no"

    monkeypatch.setattr(builtins, "input", input_fn)

    assert cli._telemetry(["enable"]) == 0
    assert "docs/telemetry.md" in seen_prompt["text"]
    assert "Collected fields" not in seen_prompt["text"]


def test_enable_reenables_disabled_marker_with_explicit_yes(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "yes")

    assert cli._telemetry(["enable"]) == 0

    config = telemetry.load_config(tmp_path)
    assert config["enabled"] is True
    assert config["schema_version"] == 1
    assert config["prompted_consent_version"] == 1
    assert config["consent_version"] == 1
    assert config["endpoint"] == telemetry.ENDPOINT


def test_missing_config_generation_changes_after_create_delete_during_consent(tmp_path):
    generation_path = tmp_path / CONFIG.parent / telemetry._GENERATION_NAME

    def create_then_delete(_prompt):
        assert generation_path.exists()
        telemetry.write_config(tmp_path, telemetry.disabled_config())
        created_generation = int(generation_path.read_text(encoding="ascii"))
        (tmp_path / CONFIG).unlink()
        telemetry.write_config(tmp_path, telemetry.disabled_config())
        recreated_generation = int(generation_path.read_text(encoding="ascii"))
        assert recreated_generation > created_generation
        (tmp_path / CONFIG).unlink()
        return "yes"

    with pytest.raises(telemetry.TelemetryConfigError, match="changed"):
        telemetry.request_consent(tmp_path, interactive=True, input_fn=create_then_delete)

    assert not (tmp_path / CONFIG).exists()


@pytest.mark.parametrize("answer", ["", "no", "NO", "y"])
def test_declined_consent_writes_disabled_marker(tmp_path, monkeypatch, answer):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: answer)

    assert cli._telemetry(["enable"]) == 0
    assert json.loads((tmp_path / CONFIG).read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "enabled": False,
        "prompted_consent_version": 1,
    }


@pytest.mark.parametrize("exception", [EOFError, KeyboardInterrupt])
def test_consent_input_failure_writes_disabled_marker(tmp_path, monkeypatch, exception):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))

    def fail(_prompt):
        raise exception

    monkeypatch.setattr(builtins, "input", fail)
    assert cli._telemetry(["enable"]) == 0
    assert telemetry.load_config(tmp_path)["enabled"] is False


def test_enable_cannot_opt_in_noninteractive_or_from_environment(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setenv("SESSION_HANDOFF_TELEMETRY", "1")
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(False))

    assert cli._telemetry(["enable"]) != 0
    assert not (tmp_path / CONFIG).exists()


@pytest.mark.parametrize(
    ("config_factory", "field", "value"),
    [
        (telemetry.disabled_config, "schema_version", True),
        (telemetry.disabled_config, "prompted_consent_version", True),
        (telemetry.enabled_config, "schema_version", True),
        (telemetry.enabled_config, "prompted_consent_version", True),
        (telemetry.enabled_config, "consent_version", True),
    ],
)
def test_config_rejects_boolean_integer_versions(tmp_path, config_factory, field, value):
    config = config_factory()
    config[field] = value

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, config)


@pytest.mark.parametrize("value", [0, 1])
def test_config_rejects_integer_enabled_flag(tmp_path, value):
    config = telemetry.disabled_config()
    config["enabled"] = value

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, config)


def test_telemetry_write_failure_is_reported_without_traceback(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "yes")

    def fail(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(telemetry, "_write_config_locked", fail)

    assert cli._telemetry(["enable"]) == 1
    error = capsys.readouterr().err
    assert "read-only filesystem" in error
    assert "Traceback" not in error


def test_disable_purge_removes_local_telemetry_state_and_preserves_marker(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    state = tmp_path / ".local/state/session-handoff"
    state.mkdir(parents=True)
    for name in ("telemetry-counters.json", "telemetry-queue.jsonl", "last-operation-summary.json"):
        (state / name).write_text("private", encoding="utf-8")

    assert cli._telemetry(["disable", "--purge"]) == 0
    assert json.loads((tmp_path / CONFIG).read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "enabled": False,
        "prompted_consent_version": 1,
    }
    assert not any(state.iterdir())


def test_disable_purge_recovers_from_corrupt_config(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    path.write_text('{"enabled": "yes"}\n', encoding="utf-8")

    assert cli._telemetry(["disable", "--purge"]) == 0
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_invalid_config_is_reported_without_overwrite(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    path.write_text('{"enabled": "yes"}\n', encoding="utf-8")

    assert cli._telemetry(["status"]) != 0
    assert "invalid" in capsys.readouterr().err.lower()
    assert json.loads(path.read_text(encoding="utf-8")) == {"enabled": "yes"}


def test_setup_yes_prompts_for_telemetry_only_after_base_setup(tmp_path, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, ["yes", "yes"])

    assert cli._setup(["--client", "codex"]) == 0
    assert telemetry.load_config(tmp_path)["enabled"] is True


def test_setup_no_records_decline(tmp_path, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, ["yes", "no"])

    assert cli._setup(["--client", "codex"]) == 0
    assert telemetry.load_config(tmp_path)["enabled"] is False


def test_setup_yes_flag_never_enables_telemetry(tmp_path, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, [], interactive=True)

    assert cli._setup(["--client", "codex", "--yes"]) == 0
    assert not (tmp_path / CONFIG).exists()


def test_noninteractive_setup_never_enables_or_prompts(tmp_path, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, [], interactive=False)

    assert cli._setup(["--client", "codex", "--yes"]) == 0
    assert not (tmp_path / CONFIG).exists()


def test_noninteractive_setup_without_yes_does_not_traceback_or_enable(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, [], interactive=False)

    assert cli._setup(["--client", "codex"]) != 0
    output = capsys.readouterr()
    assert "Traceback" not in output.err
    assert not (tmp_path / CONFIG).exists()


def test_setup_prompt_eof_does_not_traceback_or_enable(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, [], interactive=True)

    def fail(_prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", fail)

    assert cli._setup(["--client", "codex"]) == 0
    output = capsys.readouterr()
    assert "Traceback" not in output.err
    assert not (tmp_path / CONFIG).exists()


def test_reinstall_preserves_recorded_choice_without_reprompt(tmp_path, monkeypatch):
    cli = load_cli()
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    fake_setup(monkeypatch, cli, tmp_path, ["yes"], interactive=True)

    assert cli._setup(["--client", "codex"]) == 0
    assert telemetry.load_config(tmp_path)["enabled"] is True


@pytest.mark.parametrize("exception", [EOFError, KeyboardInterrupt])
def test_setup_consent_input_failure_records_decline(tmp_path, monkeypatch, exception):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, [], interactive=True)
    answers = iter(["yes"])

    def input_fn(_prompt):
        try:
            return next(answers)
        except StopIteration:
            raise exception

    monkeypatch.setattr(builtins, "input", input_fn)

    assert cli._setup(["--client", "codex"]) == 0
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_reinstall_after_telemetry_decline_does_not_reprompt(tmp_path, monkeypatch):
    cli = load_cli()
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    fake_setup(monkeypatch, cli, tmp_path, ["yes"], interactive=True)

    assert cli._setup(["--client", "codex"]) == 0
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_reinstall_with_existing_setup_does_not_prompt_or_enable_telemetry(tmp_path, monkeypatch):
    cli = load_cli()
    fake_setup(monkeypatch, cli, tmp_path, ["yes"], interactive=True)
    monkeypatch.setattr(cli, "install_setup", lambda *args, **kwargs: {"already_configured": True})
    prompts = iter(["yes"])

    def input_fn(prompt):
        try:
            return next(prompts)
        except StopIteration:
            raise AssertionError("telemetry consent must not be requested")

    monkeypatch.setattr(builtins, "input", input_fn)

    assert cli._setup(["--client", "codex"]) == 0
    assert not (tmp_path / CONFIG).exists()


def test_config_write_is_atomic_and_mode_0600(tmp_path):
    path = telemetry.write_config(tmp_path, telemetry.disabled_config())

    assert path == tmp_path / CONFIG
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".*.tmp"))


def test_telemetry_lock_fifo_is_rejected_without_blocking(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    lock.unlink()
    os.mkfifo(lock)

    try:
        _assert_nonblocking_controlled_error(tmp_path)
    finally:
        lock.unlink()


def test_disable_purge_preserves_regular_telemetry_lock(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    assert lock.is_file()

    telemetry.disable(tmp_path, purge=True)

    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_disable_purge_rejects_hardlinked_state_file_without_unlinking_external_file(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    state = tmp_path / ".local/state/session-handoff"
    state.mkdir(parents=True)
    external = tmp_path / "external-state"
    external.write_text("private", encoding="utf-8")
    external.chmod(0o600)
    state_file = state / "telemetry-queue.jsonl"
    os.link(external, state_file)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.disable(tmp_path, purge=True)

    assert external.read_text(encoding="utf-8") == "private"
    assert state_file.exists()
    assert state_file.stat().st_nlink == 2


def test_fencing_epoch_stays_monotonic_across_every_config_mutation(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    lock.write_text("7", encoding="ascii")

    telemetry.write_config(tmp_path, telemetry.enabled_config())
    assert lock.read_text(encoding="ascii") == "8"

    telemetry.disable(tmp_path)
    assert lock.read_text(encoding="ascii") == "9"

    telemetry.disable(tmp_path, purge=True)
    assert lock.read_text(encoding="ascii") == "10"


def test_fencing_epoch_high_water_is_scoped_to_canonical_home(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    telemetry._MARKER_HIGH_WATER.clear()
    telemetry.write_config(first, telemetry.disabled_config())
    first_lock = first / CONFIG.parent / ".telemetry.lock"
    first_lock.write_text("7", encoding="ascii")
    telemetry.write_config(first, telemetry.enabled_config())

    telemetry.write_config(second, telemetry.disabled_config())
    second_lock = second / CONFIG.parent / ".telemetry.lock"
    second_lock.write_text("1", encoding="ascii")
    telemetry._MARKER_HIGH_WATER[(second_lock.stat().st_dev, second_lock.stat().st_ino)] = 7

    telemetry.write_config(second, telemetry.enabled_config())

    assert telemetry.load_config(second)["enabled"] is True


def test_marker_mutation_uses_the_already_locked_descriptor(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    opened = telemetry._open_secure_directory(tmp_path, telemetry.CONFIG_PATH.parent, create=False)
    directory, directory_fd = opened
    try:
        with telemetry._config_directory_lock(directory, directory_fd) as lock_fd:
            original_open = telemetry._open_relative

            def reject_lock_reopen(fd, path, name, flags, mode=0o600):
                if name == ".telemetry.lock":
                    raise AssertionError("locked marker was reopened by pathname")
                return original_open(fd, path, name, flags, mode)

            monkeypatch.setattr(telemetry, "_open_relative", reject_lock_reopen)
            assert telemetry._bump_lock_marker(lock_fd) == 2
    finally:
        os.close(directory_fd)


def _serialized_marker_writer(home, entered, release, result, wait_for_release):
    opened = telemetry._open_secure_directory(home, telemetry.CONFIG_PATH.parent, create=False)
    directory, directory_fd = opened
    try:
        with telemetry._config_directory_lock(directory, directory_fd) as lock_fd:
            entered.set()
            if wait_for_release:
                release.wait(5)
            result.put(telemetry._bump_lock_marker(lock_fd))
    finally:
        os.close(directory_fd)


def test_two_marker_writers_do_not_overlap(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()
    second_entered = context.Event()
    release = context.Event()
    result = context.Queue()
    first = context.Process(
        target=_serialized_marker_writer,
        args=(tmp_path, first_entered, release, result, True),
    )
    second = context.Process(
        target=_serialized_marker_writer,
        args=(tmp_path, second_entered, release, result, False),
    )
    first.start()
    assert first_entered.wait(2)
    second.start()
    time.sleep(0.1)
    assert not second_entered.is_set()
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted((result.get(timeout=1), result.get(timeout=1))) == [2, 3]


@pytest.mark.parametrize("marker", ["0", "not-a-number"])
def test_invalid_external_marker_fails_closed_without_reset(tmp_path, marker):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    lock.write_text(marker, encoding="ascii")

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.enabled_config())

    assert lock.read_text(encoding="ascii") == marker


def test_regressive_marker_is_rejected_and_cannot_replay_an_old_epoch(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    lock.write_text("7", encoding="ascii")
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    assert lock.read_text(encoding="ascii") == "8"

    lock.write_text("7", encoding="ascii")
    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.disable(tmp_path)
    assert lock.read_text(encoding="ascii") == "7"


def test_consent_config_deleted_during_prompt_is_failure(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))

    def delete_config(_prompt):
        (tmp_path / CONFIG).unlink()
        return "yes"

    monkeypatch.setattr(builtins, "input", delete_config)

    assert cli._telemetry(["enable"]) == 1
    captured = capsys.readouterr()
    assert "consent recorded" not in captured.out.lower()
    assert "configuration error" in captured.err.lower()


def test_hardlinked_telemetry_lock_is_rejected_without_mutating_external_file(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    external = tmp_path / "external-lock"
    external.write_text("41", encoding="ascii")
    lock.unlink()
    os.link(external, lock)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.enabled_config())

    assert external.read_text(encoding="ascii") == "41"
    assert lock.stat().st_nlink == 2


@pytest.mark.parametrize("primitive", ["flock", "fsync"])
def test_telemetry_primitive_failure_is_controlled_and_leaves_no_temp_artifact(
    tmp_path, monkeypatch, primitive
):
    if primitive == "flock":
        monkeypatch.setattr(telemetry.fcntl, primitive, lambda *_args: (_ for _ in ()).throw(TypeError("broken flock")))
    else:
        monkeypatch.setattr(telemetry.os, primitive, lambda *_args: (_ for _ in ()).throw(OSError("broken fsync")))

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.disabled_config())

    config_parent = tmp_path / CONFIG.parent
    assert not (config_parent / CONFIG.name).exists()
    assert not list(config_parent.glob(".*.tmp"))
    assert telemetry._CONFIG_THREAD_LOCK.acquire(timeout=0.1)
    telemetry._CONFIG_THREAD_LOCK.release()


def _hold_telemetry_lock(home, ready, release):
    opened = telemetry._open_secure_directory(home, telemetry.CONFIG_PATH.parent, create=False)
    directory, directory_fd = opened
    try:
        with telemetry._config_directory_lock(directory, directory_fd):
            ready.set()
            release.wait(5)
    finally:
        os.close(directory_fd)


def _purge_result(home, result):
    try:
        telemetry.disable(home, purge=True)
    except BaseException as exc:
        result.put(type(exc).__name__)
    else:
        result.put("ok")


def test_purge_lock_timeout_keeps_lock_and_does_not_overwrite_marker(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_telemetry_lock, args=(tmp_path, ready, release))
    holder.start()
    assert ready.wait(2)

    result = context.Queue()
    purger = context.Process(target=_purge_result, args=(tmp_path, result))
    purger.start()
    purger.join(3)
    release.set()
    holder.join(3)
    purger.join(3)

    assert not purger.is_alive()
    assert not holder.is_alive()
    assert result.get(timeout=1) == "TelemetryConfigError"
    lock = tmp_path / CONFIG.parent / ".telemetry.lock"
    assert lock.is_file()
    assert telemetry.load_config(tmp_path)["enabled"] is True


def test_home_ancestor_swap_during_root_open_fails_closed(tmp_path, monkeypatch):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    requested_home = alias / "home"
    canonical_home = requested_home.resolve()
    original_home = telemetry._home
    calls = 0

    def race_home(home=None):
        nonlocal calls
        result = original_home(home)
        calls += 1
        if calls == 1:
            real_parent.rename(tmp_path / "real-parent.moved")
            real_parent.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(telemetry, "_home", race_home)

    _assert_controlled_config_error(
        lambda: telemetry.write_config(requested_home, telemetry.disabled_config())
    )
    assert not (outside / "home/.config/session-handoff/telemetry.json").exists()
    assert canonical_home == tmp_path / "real-parent/home"


def test_existing_config_mode_is_reduced_to_0600_on_load(tmp_path):
    path = telemetry.write_config(tmp_path, telemetry.disabled_config())
    path.chmod(0o644)

    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()
    assert path.stat().st_mode & 0o777 == 0o600


def test_enabled_endpoint_is_pinned_to_documented_first_party_endpoint(tmp_path):
    config = telemetry.enabled_config("2026-08-26T00:00:00Z")
    assert config["endpoint"] == telemetry.ENDPOINT

    config["endpoint"] = "https://attacker.example/collect"
    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, config)


def test_config_symlink_is_rejected_before_read_or_write(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(telemetry.disabled_config()), encoding="utf-8")
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_config(tmp_path)
    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.disabled_config())
    assert path.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == telemetry.disabled_config()


def test_config_directory_symlink_is_rejected_before_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_parent = tmp_path / ".config/session-handoff"
    config_parent.parent.mkdir()
    config_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.disabled_config())
    assert not (outside / "telemetry.json").exists()


def test_state_directory_symlink_fails_purge_without_following_link(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "telemetry-counters.json"
    protected.write_text("private", encoding="utf-8")
    state = tmp_path / ".local/state/session-handoff"
    state.parent.mkdir(parents=True)
    state.symlink_to(outside, target_is_directory=True)

    assert cli._telemetry(["disable", "--purge"]) == 1
    assert protected.read_text(encoding="utf-8") == "private"
    assert state.is_symlink()
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_disable_purge_closes_state_fd_when_state_setup_fails(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    (tmp_path / ".local/state/session-handoff").mkdir(parents=True)
    original_ensure_directory = telemetry._ensure_directory

    def fail_state_setup(descriptor, path):
        if path.parent.name == "state":
            raise telemetry.TelemetryConfigError("state setup failed")
        original_ensure_directory(descriptor, path)

    monkeypatch.setattr(telemetry, "_ensure_directory", fail_state_setup)
    before = len(os.listdir("/proc/self/fd"))

    for _ in range(5):
        with pytest.raises(telemetry.TelemetryConfigError, match="state setup failed"):
            telemetry.disable(tmp_path, purge=True)

    assert len(os.listdir("/proc/self/fd")) == before


def test_disable_purge_closes_state_fd_when_config_setup_fails(tmp_path, monkeypatch):
    state = tmp_path / telemetry.STATE_PATH
    state.mkdir(parents=True)
    original_open_secure_directory = telemetry._open_secure_directory

    def fail_config_setup(root, relative, *, create):
        if relative == telemetry.CONFIG_PATH.parent:
            raise telemetry.TelemetryConfigError("config setup failed")
        return original_open_secure_directory(root, relative, create=create)

    monkeypatch.setattr(telemetry, "_open_secure_directory", fail_config_setup)
    before = len(os.listdir("/proc/self/fd"))

    for _ in range(5):
        with pytest.raises(telemetry.TelemetryConfigError, match="config setup failed"):
            telemetry.disable(tmp_path, purge=True)

    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("cleanup_failure", ["unlock", "close"])
def test_config_lock_cleanup_releases_thread_lock_after_cleanup_failure(
    tmp_path, monkeypatch, cleanup_failure
):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    original_flock = telemetry.fcntl.flock
    original_close = telemetry.os.close
    lock_fd = None
    failed = False

    if cleanup_failure == "unlock":
        def fail_unlock(fd, operation):
            nonlocal failed
            if operation == telemetry.fcntl.LOCK_UN and not failed:
                failed = True
                raise OSError("unlock failed")
            return original_flock(fd, operation)

        monkeypatch.setattr(telemetry.fcntl, "flock", fail_unlock)
    else:
        original_open_relative = telemetry._open_relative

        def track_lock_fd(directory_fd, directory, name, flags, mode=0o600):
            nonlocal lock_fd
            descriptor = original_open_relative(directory_fd, directory, name, flags, mode)
            if name == ".telemetry.lock":
                lock_fd = descriptor
            return descriptor

        def fail_lock_close(fd):
            nonlocal failed
            if fd == lock_fd and not failed:
                failed = True
                raise OSError("close failed")
            return original_close(fd)

        monkeypatch.setattr(telemetry, "_open_relative", track_lock_fd)
        monkeypatch.setattr(telemetry.os, "close", fail_lock_close)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.enabled_config())

    monkeypatch.undo()
    assert telemetry._CONFIG_THREAD_LOCK.acquire(timeout=0.1)
    telemetry._CONFIG_THREAD_LOCK.release()
    telemetry.write_config(tmp_path, telemetry.disabled_config())


def test_disable_wins_over_consent_answer_acquired_before_disable(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    original_fchmod = telemetry.os.fchmod
    telemetry.os.fchmod = lambda _fd, _mode: None
    answer_acquired = threading.Event()
    allow_answer = threading.Event()
    result = []
    errors = []

    def input_fn(_prompt):
        answer_acquired.set()
        assert allow_answer.wait(2)
        return "yes"

    def consent():
        try:
            result.append(telemetry.request_consent(tmp_path, interactive=True, input_fn=input_fn))
        except BaseException as exc:
            errors.append(exc)

    try:
        thread = threading.Thread(target=consent)
        thread.start()
        assert answer_acquired.wait(2)

        telemetry.disable(tmp_path)
        allow_answer.set()
        thread.join(2)
    finally:
        telemetry.os.fchmod = original_fchmod

    assert not thread.is_alive()
    assert errors == []
    assert result == [telemetry.disabled_config()]
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_uninstall_eof_cancels_without_traceback(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))

    def fail(_prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", fail)

    assert cli._uninstall([]) == 0
    assert "Traceback" not in capsys.readouterr().err


def test_missing_fcntl_flock_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "fcntl",
        SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8),
    )

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.disabled_config())


def test_unremovable_state_file_fails_purge_and_keeps_disabled_marker(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    state = tmp_path / ".local/state/session-handoff"
    state.mkdir(parents=True)
    target = state / "telemetry-counters.json"
    target.write_text("private", encoding="utf-8")
    original_unlink = telemetry._unlink_relative

    def refuse_target(directory_fd, directory, name):
        if name == target.name:
            raise OSError("permission denied")
        return original_unlink(directory_fd, directory, name)

    monkeypatch.setattr(telemetry, "_unlink_relative", refuse_target)

    assert cli._telemetry(["disable", "--purge"]) == 1
    assert target.read_text(encoding="utf-8") == "private"
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_purge_replaces_config_symlink_without_touching_target(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(telemetry.enabled_config()), encoding="utf-8")
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)

    assert cli._telemetry(["disable", "--purge"]) == 0
    assert not path.is_symlink()
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()
    assert json.loads(outside.read_text(encoding="utf-8"))["enabled"] is True


def test_non_utf8_config_is_reported_without_traceback(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{\xff\xfe}\n")

    assert cli._telemetry(["status"]) == 1
    error = capsys.readouterr().err
    assert "invalid" in error.lower()
    assert "Traceback" not in error


def test_purge_rejects_config_directory_symlink_without_touching_target(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    config_parent = tmp_path / ".config/session-handoff"
    config_parent.parent.mkdir()
    config_parent.symlink_to(outside, target_is_directory=True)

    assert cli._telemetry(["disable", "--purge"]) == 1
    assert config_parent.is_symlink()
    assert not (outside / "telemetry.json").exists()


def test_purge_removes_only_stale_telemetry_temps_in_config_directory(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    config_parent = tmp_path / CONFIG.parent
    stale = config_parent / ".telemetry.json.stale.tmp"
    stale.write_text("stale", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / ".telemetry.json.outside.tmp"
    protected.write_text("protected", encoding="utf-8")
    linked = config_parent / ".telemetry.json.link.tmp"
    linked.symlink_to(protected)

    assert cli._telemetry(["disable", "--purge"]) == 1
    assert not stale.exists()
    assert protected.read_text(encoding="utf-8") == "protected"
    assert linked.is_symlink()


def test_purge_removes_all_telemetry_state_temps(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    state = tmp_path / telemetry.STATE_PATH
    state.mkdir(parents=True)
    names = [
        ".telemetry.json.state.tmp",
        ".telemetry-counters.json.state.tmp",
        ".telemetry-queue.jsonl.state.tmp",
        ".last-operation-summary.json.state.tmp",
    ]
    for name in names:
        (state / name).write_text("temporary", encoding="utf-8")
        (state / name).chmod(0o600)

    telemetry.disable(tmp_path, purge=True)

    assert not any((state / name).exists() for name in names)


def test_purge_does_not_delete_temp_during_active_config_write(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    config_parent = tmp_path / CONFIG.parent
    entered = threading.Event()
    release = threading.Event()
    writer_errors = []
    purge_errors = []
    original_fsync = telemetry.os.fsync

    def pause_after_flush(fd):
        original_fsync(fd)
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(telemetry.os, "fsync", pause_after_flush)

    def write():
        try:
            telemetry.write_config(tmp_path, telemetry.enabled_config())
        except BaseException as exc:
            writer_errors.append(exc)

    def purge():
        try:
            telemetry.disable(tmp_path, purge=True)
        except BaseException as exc:
            purge_errors.append(exc)

    writer = threading.Thread(target=write)
    writer.start()
    assert entered.wait(2)
    purger = threading.Thread(target=purge)
    purger.start()
    time.sleep(0.05)
    release.set()
    writer.join(2)
    purger.join(2)

    assert not writer.is_alive()
    assert not purger.is_alive()
    assert writer_errors == []
    assert purge_errors == []
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()
    assert not list(config_parent.glob(".*.tmp"))


def test_config_write_rejects_target_symlink_created_after_initial_check(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    path = tmp_path / CONFIG
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    original_create_temp = telemetry._create_temp

    def race(directory_fd, directory):
        descriptor, temporary = original_create_temp(directory_fd, directory)
        path.unlink()
        path.symlink_to(outside)
        return descriptor, temporary

    monkeypatch.setattr(telemetry, "_create_temp", race)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.write_config(tmp_path, telemetry.enabled_config())

    assert path.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"
    assert not list(path.parent.glob(".*.tmp"))


def test_home_ancestor_is_canonicalized_before_telemetry_writes(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    requested_home = alias / "home"

    telemetry.write_config(requested_home, telemetry.disabled_config())

    assert telemetry._home(requested_home) == requested_home.resolve()
    assert telemetry.config_path(requested_home) == requested_home.resolve() / CONFIG


def _assert_controlled_config_error(operation):
    with pytest.raises(telemetry.TelemetryConfigError):
        operation()


def _load_config_result(home, result):
    try:
        telemetry.load_config(home)
    except BaseException as exc:
        result.put((type(exc).__name__, str(exc)))
    else:
        result.put(("ok", ""))


def _assert_nonblocking_controlled_error(home):
    if os.name == "nt":
        pytest.skip("secure telemetry filesystem tests require POSIX dir_fd/O_NOFOLLOW/fchmod")
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    worker = context.Process(target=_load_config_result, args=(home, result))
    worker.start()
    worker.join(1)
    if worker.is_alive():
        worker.terminate()
        worker.join()
        pytest.fail("non-regular telemetry config blocked")
    assert result.get(timeout=1)[0] == "TelemetryConfigError"


@pytest.mark.parametrize("kind", ["fifo", "socket", "device", "directory"])
def test_nonregular_config_is_rejected_without_blocking(tmp_path, kind):
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    listener = None
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
    elif kind == "device":
        if os.name == "nt" or not hasattr(os, "mknod"):
            pytest.skip("device-node test requires POSIX mknod")
        try:
            os.mknod(path, stat.S_IFCHR | 0o600, os.makedev(1, 3))
        except PermissionError:
            pytest.skip("device-node test requires mknod permission")
    else:
        path.mkdir()
    try:
        _assert_nonblocking_controlled_error(tmp_path)
        _assert_controlled_config_error(lambda: telemetry.write_config(tmp_path, telemetry.disabled_config()))
    finally:
        if listener is not None:
            listener.close()


def test_existing_config_and_state_directories_are_restricted_to_0700(tmp_path):
    config_parent = tmp_path / CONFIG.parent
    config_parent.mkdir(parents=True)
    config_parent.chmod(0o777)
    state = tmp_path / ".local/state/session-handoff"
    state.mkdir(parents=True)
    for parent in (tmp_path / ".local", tmp_path / ".local/state", state):
        parent.chmod(0o777)

    telemetry.write_config(tmp_path, telemetry.disabled_config())
    telemetry.disable(tmp_path, purge=True)

    for directory in (config_parent, tmp_path / ".local", tmp_path / ".local/state", state):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_existing_home_permissions_are_not_changed(tmp_path):
    tmp_path.chmod(0o755)

    telemetry.write_config(tmp_path, telemetry.disabled_config())

    assert tmp_path.stat().st_mode & 0o777 == 0o755


def test_directory_hardening_failure_is_controlled(tmp_path, monkeypatch):
    original_fchmod = telemetry.os.fchmod

    def fail_fchmod(fd, mode):
        if mode == 0o700:
            raise OSError("cannot restrict telemetry directory")
        return original_fchmod(fd, mode)

    monkeypatch.setattr(telemetry.os, "fchmod", fail_fchmod)
    _assert_controlled_config_error(lambda: telemetry.write_config(tmp_path, telemetry.disabled_config()))


def _swap_config_ancestor(tmp_path, outside):
    config_root = tmp_path / ".config"
    moved = tmp_path / ".config.real"
    config_root.rename(moved)
    config_root.symlink_to(outside, target_is_directory=True)


def test_config_ancestor_replacement_cannot_escape_root(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    outside = tmp_path / "outside"
    (outside / "session-handoff").mkdir(parents=True)
    config_parent = tmp_path / CONFIG.parent
    original_open = telemetry.os.open
    swapped = False

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is None and Path(path) == config_parent:
            swapped = True
            _swap_config_ancestor(tmp_path, outside)
        return original_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else original_open(path, flags, mode)

    monkeypatch.setattr(telemetry.os, "open", race_open)
    _assert_controlled_config_error(lambda: telemetry.write_config(tmp_path, telemetry.enabled_config()))
    assert not (outside / "session-handoff/telemetry.json").exists()


def test_state_ancestor_replacement_cannot_escape_root_during_purge(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    state_parent = tmp_path / ".local/state/session-handoff"
    state_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "telemetry-counters.json"
    protected.write_text("private", encoding="utf-8")
    state_root = tmp_path / ".local"
    moved = tmp_path / ".local.real"
    original_open = telemetry.os.open
    swapped = False

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is None and Path(path) == state_parent:
            swapped = True
            state_root.rename(moved)
            state_root.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else original_open(path, flags, mode)

    monkeypatch.setattr(telemetry.os, "open", race_open)
    _assert_controlled_config_error(lambda: telemetry.disable(tmp_path, purge=True))
    assert protected.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("primitive", ["open", "mkdir", "stat", "unlink", "rename"])
def test_missing_secure_filesystem_primitive_fails_closed(tmp_path, monkeypatch, primitive):
    supported = set(telemetry.os.supports_dir_fd)
    supported.discard(getattr(telemetry.os, primitive))
    monkeypatch.setattr(telemetry.os, "supports_dir_fd", supported)
    _assert_controlled_config_error(lambda: telemetry.write_config(tmp_path, telemetry.disabled_config()))
    assert not (tmp_path / CONFIG).exists()


@pytest.mark.parametrize("primitive", ["fchmod", "fsync", "O_NOFOLLOW"])
def test_missing_required_secure_primitive_fails_closed(tmp_path, monkeypatch, primitive):
    if primitive == "fchmod":
        monkeypatch.setattr(telemetry.os, primitive, None)
    else:
        monkeypatch.delattr(telemetry.os, primitive)
    _assert_controlled_config_error(lambda: telemetry.write_config(tmp_path, telemetry.disabled_config()))
    assert not (tmp_path / CONFIG).exists()


def test_replace_uses_dir_fd_rename_api(tmp_path, monkeypatch):
    original_replace = telemetry.os.replace
    monkeypatch.setattr(telemetry.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("path replace used")))
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()
    monkeypatch.setattr(telemetry.os, "replace", original_replace)


def test_telemetry_preview_renders_otlp_without_upload(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(
        {
            "schema_version": 1,
            "event": "operation_summary",
            "day_utc": "2026-08-25",
            "plugin_version": "0.5",
            "operation": "handoff",
            "source_client": "codex",
            "target_client": "claude",
            "result": "success",
            "failure_stage": "none",
            "duration_bucket": "lt_1s",
            "handoff_bytes_bucket": "lt_4k",
            "redaction_bucket": "zero",
            "dropped_events_bucket": "zero",
            "normalized_fields_bucket": "zero",
        },
        tmp_path,
        now="2026-08-26T00:00:00Z",
    )
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("preview uploaded"))

    assert cli._telemetry(["preview"]) == 0
    output = capsys.readouterr().out
    assert "session_handoff.daily_aggregate" in output
    assert "Idempotency-Key" in output
    assert "Authorization" not in output


def test_telemetry_preview_uses_exact_upload_request_bytes_and_headers(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(
        {
            "schema_version": 1,
            "event": "operation_summary",
            "day_utc": "2026-08-25",
            "plugin_version": "0.5",
            "operation": "handoff",
            "source_client": "codex",
            "target_client": "claude",
            "result": "success",
            "failure_stage": "none",
            "duration_bucket": "lt_1s",
            "handoff_bytes_bucket": "lt_4k",
            "redaction_bucket": "zero",
            "dropped_events_bucket": "zero",
            "normalized_fields_bucket": "zero",
        },
        tmp_path,
        now="2026-08-26T00:00:00Z",
    )
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    expected = telemetry.build_request(
        telemetry.enabled_config()["endpoint"],
        telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z"),
    )

    assert cli._telemetry(["preview"]) == 0
    output = capsys.readouterr().out
    assert expected.data.hex() in output
    assert all(name in output for name in expected.headers)


def test_telemetry_preview_with_disabled_config_does_not_leak_endpoint(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    event = {
        "schema_version": 1,
        "event": "operation_summary",
        "day_utc": "2026-08-25",
        "plugin_version": "0.5",
        "operation": "handoff",
        "source_client": "codex",
        "target_client": "claude",
        "result": "success",
        "failure_stage": "none",
        "duration_bucket": "lt_1s",
        "handoff_bytes_bucket": "lt_4k",
        "redaction_bucket": "zero",
        "dropped_events_bucket": "zero",
        "normalized_fields_bucket": "zero",
    }
    telemetry._store_queue(tmp_path, [telemetry._aggregate_row(event, 1)])

    assert cli._telemetry(["preview"]) == 0
    output = capsys.readouterr().out
    assert "Telemetry is disabled; no upload request." in output
    assert telemetry.ENDPOINT not in output


def test_telemetry_flush_does_not_inherit_provider_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "secret")
    observed = {}

    def popen(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return SimpleNamespace()

    cli = load_cli()
    monkeypatch.setattr(telemetry.subprocess, "Popen", popen)

    assert cli._telemetry(["flush"]) == 0
    assert not any(key.endswith(("_API_KEY", "_SECRET_ACCESS_KEY", "_TOKEN")) for key in observed["env"])
    assert "OPENAI_API_KEY" not in observed["env"]
    assert "ANTHROPIC_API_KEY" not in observed["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in observed["env"]
    assert not any(key in observed["env"] for key in ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING"))
    assert observed["env"] == {}
    assert observed["command"][-4] == "--queue-path"
    assert observed["command"][-2] == "--config-path"


def _recent_operation_event():
    return {
        "schema_version": 1,
        "event": "operation_summary",
        "day_utc": telemetry.datetime.now(telemetry.timezone.utc).date().isoformat(),
        "plugin_version": "0.5",
        "operation": "handoff",
        "source_client": "codex",
        "target_client": "codex",
        "result": "success",
        "failure_stage": "none",
        "duration_bucket": "lt_1s",
        "handoff_bytes_bucket": "lt_4k",
        "redaction_bucket": "zero",
        "dropped_events_bucket": "zero",
        "normalized_fields_bucket": "zero",
    }


def test_telemetry_report_records_enum_only_feedback_without_upload(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(_recent_operation_event(), tmp_path)
    monkeypatch.setattr(telemetry, "spawn_detached_flush", lambda *_args: pytest.fail("report uploaded"))

    assert cli._telemetry(["report", "--category", "constraint", "--severity", "recoverable"]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event == {
        "schema_version": 1,
        "event": "context_feedback",
        "day_utc": event["day_utc"],
        "plugin_version": "0.5",
        "operation": "handoff",
        "source_client": "codex",
        "target_client": "codex",
        "feedback_category": "constraint",
        "feedback_severity": "recoverable",
    }
    assert not (tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME).exists()


def test_telemetry_report_is_disabled_and_rejects_free_text(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.disabled_config())

    assert cli._telemetry(["report", "--category", "other", "--severity", "blocked"]) == 1
    assert "disabled" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli._telemetry(
            ["report", "--category", "other", "--severity", "blocked", "--text", "secret"]
        )


def test_last_operation_summary_expires_and_is_removed(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(
        _recent_operation_event(), tmp_path, now="2026-08-24T00:00:00Z"
    )

    assert telemetry.load_last_operation_summary(tmp_path, now="2026-08-26T00:00:01Z") is None
    assert not (tmp_path / telemetry.STATE_PATH / telemetry._LAST_SUMMARY_NAME).exists()


def test_telemetry_report_deduplicates_same_feedback(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(_recent_operation_event(), tmp_path)
    args = ["report", "--category", "constraint", "--severity", "recoverable"]

    assert cli._telemetry(args) == 0
    assert cli._telemetry(args) == 1


class FakeStdout:
    def __init__(self, interactive):
        self.interactive = interactive
        self.buffer = []

    def isatty(self):
        return self.interactive

    def write(self, text):
        self.buffer.append(text)


def test_postinstall_skips_without_a_real_tty(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(False))
    monkeypatch.setattr(cli.sys, "stdout", FakeStdout(True))

    assert cli._telemetry_postinstall() == 0
    assert not (tmp_path / CONFIG).exists()


def test_postinstall_skip_env_var_short_circuits_even_with_a_real_tty(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setenv("SESSION_HANDOFF_SKIP_TELEMETRY_PROMPT", "1")
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(cli.sys, "stdout", FakeStdout(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")))

    assert cli._telemetry_postinstall() == 0
    assert not (tmp_path / CONFIG).exists()


def test_postinstall_interactive_yes_enables_telemetry(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(cli.sys, "stdout", FakeStdout(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "yes")

    assert cli._telemetry_postinstall() == 0
    config = json.loads((tmp_path / CONFIG).read_text(encoding="utf-8"))
    assert config["enabled"] is True


def test_postinstall_declined_consent_records_disabled_marker(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(cli.sys, "stdout", FakeStdout(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "no")

    assert cli._telemetry_postinstall() == 0
    config = json.loads((tmp_path / CONFIG).read_text(encoding="utf-8"))
    assert config["enabled"] is False


def test_postinstall_never_reprompts_once_a_config_exists(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(cli.sys, "stdout", FakeStdout(True))
    calls = []
    monkeypatch.setattr(builtins, "input", lambda _prompt: calls.append(1) or "no")

    assert cli._telemetry_postinstall() == 0
    assert cli._telemetry_postinstall() == 0
    assert len(calls) == 1


def test_postinstall_swallows_unexpected_errors(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin(True))
    monkeypatch.setattr(cli.sys, "stdout", FakeStdout(True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: (_ for _ in ()).throw(RuntimeError("boom")))

    assert cli._telemetry_postinstall() == 0
