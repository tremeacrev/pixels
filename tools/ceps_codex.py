"""Fixed, isolated Codex launch configuration for ceps.

Only the supervisor chooses a role. Neither prompts nor broker clients can
choose a model, reasoning effort, provider, or native subagent configuration.
The caller owns process groups, output collection, and interruption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping


PARENT_MODEL = "gpt-6-astra"
PARENT_EFFORT = "ultra"
CHILD_MODEL = "gpt-6-luna"
CHILD_EFFORT = "max"
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


class CodexError(RuntimeError):
    """A fixed model or isolated runtime could not be established."""


def validate_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Require both exact model IDs and their highest advertised efforts.

    A changed catalog fails closed instead of silently selecting a replacement
    model or continuing below a newly advertised maximum reasoning level.
    """
    if not isinstance(catalog, Mapping) or not isinstance(catalog.get("models"), list):
        raise CodexError("Codex returned an invalid model catalog.")
    found: dict[str, Any] = {}
    for role, model, effort in (
        ("parent", PARENT_MODEL, PARENT_EFFORT),
        ("child", CHILD_MODEL, CHILD_EFFORT),
    ):
        matches = [item for item in catalog["models"]
                   if isinstance(item, dict) and item.get("slug") == model]
        if len(matches) != 1:
            raise CodexError(f"Required model {model} is missing or ambiguous.")
        item = matches[0]
        levels = item.get("supported_reasoning_levels", [])
        if not isinstance(levels, list):
            raise CodexError(f"Model {model} has an invalid reasoning-level catalog.")
        advertised = [level.get("effort") for level in levels if isinstance(level, dict)]
        if effort not in advertised:
            raise CodexError(f"Required model {model} does not advertise {effort} reasoning.")
        if any(level not in _EFFORTS for level in advertised):
            raise CodexError(f"Model {model} advertises an unknown reasoning level; update ceps.")
        if max(advertised, key=_EFFORTS.index) != effort:
            raise CodexError(f"Model {model} now supports reasoning above {effort}; update ceps.")
        if item.get("visibility") not in (None, "list"):
            raise CodexError(f"Required model {model} is not available in the model picker.")
        found[role] = {"model": model, "reasoning_effort": effort,
                       "wire_reasoning_effort": "xhigh" if role == "parent" else "max"}
    found["catalog_sha256"] = hashlib.sha256(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return found


def _private_write(path: Path, content: str) -> None:
    """Publish a complete owner-only file; never follow an existing symlink."""
    fd, temporary = tempfile.mkstemp(prefix=".ceps-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class RuntimeConfig:
    home: Path
    source_codex_home: Path
    codex: str
    metadata: dict[str, Any]
    _auth_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Refresh a non-refreshable ChatGPT credential snapshot for this launch.

        The account client using the original home remains the sole refresh
        owner. Never copy its refresh token: independent refreshers can rotate
        it and break the user's login. Expired workers must stop and restart
        after the supervisor's account client refreshes the original login.
        """
        try:
            auth = json.loads((self.source_codex_home / "auth.json").read_text())
        except (OSError, ValueError) as exc:
            raise CodexError("ceps requires readable file-based ChatGPT login credentials.") from exc
        if not isinstance(auth, dict):
            raise CodexError("The ChatGPT login file has an invalid format.")
        tokens = auth.get("tokens")
        if auth.get("auth_mode") not in (None, "chatgpt") or not isinstance(tokens, dict):
            raise CodexError("ceps requires ChatGPT login; API-key billing is not supported.")
        required = ("access_token", "id_token", "account_id")
        if any(not isinstance(tokens.get(key), str) or not tokens[key] for key in required):
            raise CodexError("The ChatGPT login is incomplete; refresh it before running ceps.")
        snapshot = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                    "tokens": {key: tokens[key] for key in required},
                    "last_refresh": auth.get("last_refresh")}
        snapshot["tokens"]["refresh_token"] = ""
        identity = hashlib.sha256(tokens["account_id"].encode()).hexdigest()
        with self._auth_lock:
            prior = self.metadata.get("chatgpt_account_sha256")
            if prior is not None and prior != identity:
                raise CodexError("The ChatGPT account changed during this ceps run; stopping.")
            self.metadata["chatgpt_account_sha256"] = identity
            _private_write(self.home / "auth.json", json.dumps(snapshot))
        inherited = dict(os.environ if base_env is None else base_env)
        clean = {key: value for key, value in inherited.items()
                 if not key.startswith(("CODEX_", "OPENAI_", "AZURE_OPENAI_"))}
        # Certificate configuration does not change the provider or credentials.
        if "CODEX_CA_CERTIFICATE" in inherited:
            clean["CODEX_CA_CERTIFICATE"] = inherited["CODEX_CA_CERTIFICATE"]
        clean["CODEX_HOME"] = str(self.home)
        clean["NO_COLOR"] = "1"
        clean["GIT_ALLOW_PROTOCOL"] = ""
        return clean

    def clear_credentials(self) -> None:
        """Remove the short-lived credential snapshot after all writers stop."""
        (self.home / "auth.json").unlink(missing_ok=True)


def prepare_runtime(
    runtime_dir: str | Path,
    source_codex_home: str | Path,
    catalog_data: Mapping[str, Any],
    codex: str = "codex",
) -> RuntimeConfig:
    metadata = validate_catalog(catalog_data)
    executable = shutil.which(codex)
    if executable is None:
        raise CodexError("Codex executable was not found.")
    home = Path(runtime_dir).resolve()
    source = Path(source_codex_home).resolve()
    if home == source:
        raise CodexError("The ceps runtime must not replace the user's Codex home.")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    # This file is intentionally empty. Every effective option comes from the
    # launcher, which also marks the worktree untrusted to skip project config.
    _private_write(home / "config.toml", "# ceps uses explicit launch configuration.\n")
    _private_write(home / "models.json", json.dumps({"models": catalog_data["models"]}))
    return RuntimeConfig(home, source, str(Path(executable).resolve()), metadata)


def build_exec_command(
    runtime: RuntimeConfig,
    role: str,
    cwd: str | Path,
    broker_socket: str | Path | None = None,
    output_file: str | Path | None = None,
    output_schema: str | Path | None = None,
) -> list[str]:
    """Build a fixed invocation; callers supply no model/config override list."""
    if role not in ("parent", "child"):
        raise CodexError("Worker role must be parent or child.")
    if role == "parent" and broker_socket is None:
        raise CodexError("The Astra parent requires the ceps delegation broker.")
    if role == "child" and broker_socket is not None:
        raise CodexError("Luna workers cannot receive delegation credentials.")
    root = Path(cwd).resolve()
    if runtime.home == root or root in runtime.home.parents:
        raise CodexError("The ceps runtime must be outside the writable worktree.")
    model, effort = ((PARENT_MODEL, PARENT_EFFORT) if role == "parent"
                     else (CHILD_MODEL, CHILD_EFFORT))
    command = [runtime.codex, "exec", "--strict-config", "--ignore-user-config",
               "--ignore-rules", "--json", "--color", "never", "--ephemeral",
               "--sandbox", "workspace-write" if role == "parent" else "read-only",
               "--cd", str(root), "--model", model]
    fixed = {
        "model_provider": "openai",
        "model_reasoning_effort": effort,
        "review_model": model,
        "approval_policy": "never",
        "approvals_reviewer": "user",
        "model_catalog_json": str(runtime.home / "models.json"),
        "agents.enabled": False,
        "features.multi_agent": False,
        "features.multi_agent_v2": False,
        "features.hooks": False,
        "features.memories": False,
        "features.plugins": False,
        "features.apps": False,
        "features.guardian_approval": False,
        "features.skill_mcp_dependency_install": False,
        "memories.generate_memories": False,
        "memories.use_memories": False,
        "check_for_update_on_startup": False,
        "sandbox_workspace_write.network_access": False,
        "sandbox_workspace_write.exclude_tmpdir_env_var": True,
        "sandbox_workspace_write.exclude_slash_tmp": True,
        "shell_environment_policy.inherit": "core",
        "shell_environment_policy.exclude": ["*TOKEN*", "*KEY*", "CODEX_*", "OPENAI_*"],
        "shell_environment_policy.set.GIT_ALLOW_PROTOCOL": "",
        "allow_login_shell": False,
        "analytics.enabled": False,
        "web_search": "disabled",
    }
    if role == "parent":
        bridge = runtime.home / "ceps_mcp.py"
        if not bridge.is_file():
            raise CodexError("The private ceps MCP bridge is missing.")
        fixed.update({
            "mcp_servers.ceps.command": str(Path(sys.executable).resolve()),
            "mcp_servers.ceps.args": [str(bridge), "--socket", str(Path(broker_socket).resolve())],
            "mcp_servers.ceps.required": True,
            "mcp_servers.ceps.startup_timeout_sec": 10,
            "mcp_servers.ceps.tool_timeout_sec": 3800,
            "mcp_servers.ceps.enabled_tools": ["delegate"],
            "mcp_servers.ceps.tools.delegate.approval_mode": "approve",
        })
    for key, value in fixed.items():
        command.extend(["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"])
    # Codex's -c dotted-key splitter does not honor quotes around path
    # components. A complete TOML inline table preserves dots in directories.
    project_key = json.dumps(str(root), ensure_ascii=False)
    command.extend(["-c", f'projects={{ {project_key} = {{ trust_level = "untrusted" }} }}'])
    if output_file is not None:
        command.extend(["--output-last-message", str(Path(output_file).resolve())])
    if output_schema is not None:
        command.extend(["--output-schema", str(Path(output_schema).resolve())])
    command.append("-")
    return command


def validate_launch_configuration(
    runtime: RuntimeConfig,
    cwd: str | Path,
    broker_socket: str | Path,
    timeout: float = 30,
) -> None:
    """Have installed Codex parse both actual configurations without inference.

    A private app-server parses the configuration and answers ``config/read``;
    no thread or turn is created. MCP connectivity remains the required
    server's startup check during an actual parent launch.
    """
    for role in ("parent", "child"):
        launch = build_exec_command(runtime, role, cwd,
                                    broker_socket if role == "parent" else None)
        command = [runtime.codex, "app-server", "--strict-config", "--stdio"]
        for index, argument in enumerate(launch[:-1]):
            if argument == "-c":
                command.extend(["-c", launch[index + 1]])
        for flag, key in (("--model", "model"), ("--sandbox", "sandbox_mode")):
            command.extend(["-c", f"{key}={json.dumps(launch[launch.index(flag) + 1])}"])
        process = subprocess.Popen(command, cwd=Path(cwd).resolve(), env=runtime.env(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        responses: queue.Queue[Any] = queue.Queue()
        diagnostics: list[str] = []

        def read_stdout() -> None:
            for line in process.stdout:
                try:
                    responses.put(json.loads(line))
                except ValueError:
                    pass
            responses.put(None)

        def read_stderr() -> None:
            for line in process.stderr:
                diagnostics.append(line[-1500:])
                del diagnostics[:-4]

        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout

        def request(number: int, method: str, params: dict[str, Any]) -> Any:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number,
                                            "method": method, "params": params}) + "\n")
            process.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexError("Codex configuration preflight timed out.")
                try:
                    response = responses.get(timeout=remaining)
                except queue.Empty as exc:
                    raise CodexError("Codex configuration preflight timed out.") from exc
                if response is None:
                    raise CodexError("Codex configuration preflight exited unexpectedly.")
                if response.get("id") == number:
                    if "error" in response:
                        raise CodexError("Codex rejected the configuration preflight request.")
                    if "result" not in response:
                        raise CodexError("Codex returned an invalid configuration response.")
                    return response["result"]

        failure: Exception | None = None
        try:
            request(1, "initialize", {"clientInfo": {"name": "ceps_config_check", "version": "1"}})
            process.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
            process.stdin.flush()
            result = request(2, "config/read", {"cwd": str(Path(cwd).resolve())})
            effective = result.get("config", {}) if isinstance(result, dict) else {}
            expected = {
                "model": PARENT_MODEL if role == "parent" else CHILD_MODEL,
                "model_reasoning_effort": PARENT_EFFORT if role == "parent" else CHILD_EFFORT,
                "approval_policy": "never",
            }
            if any(effective.get(key) != value for key, value in expected.items()):
                raise CodexError("Resolved model, reasoning, or approval settings do not match the fixed policy.")
            if not isinstance(effective.get("agents"), dict) or effective["agents"].get("enabled") is not False:
                raise CodexError("Resolved configuration does not disable native subagents.")
        except (CodexError, OSError) as exc:
            failure = exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            for reader in readers:
                reader.join(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        if failure is not None:
            detail = "".join(diagnostics).strip()[-1500:]
            raise CodexError(f"Codex rejected the fixed {role} configuration: {detail or failure}") from failure
