"""Run and record the complete controlled-workstation beta release gate."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple


BETA3_BROWSER_VALIDATORS = (
    "validate_comparison_studio",
    "validate_evidence_warning_privacy",
    "validate_manage_system",
    "validate_owner_purge_ui",
    "validate_stress_studio",
    "validate_structured_evidence_navigation",
    "validate_upload_working_sample",
    "validate_utility_overlay",
    "observe_console",
)
UNITTEST_VERBOSE_PREFIX = re.compile(
    rb"^(?P<method>[A-Za-z_][A-Za-z0-9_]*) \("
    rb"(?P<test_id>(?:[A-Za-z_][A-Za-z0-9_]*\.){2,}"
    rb"[A-Za-z_][A-Za-z0-9_]*)\)(?: \.\.\. ?)?"
)
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


class GateDefinition(NamedTuple):
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    environment: tuple[tuple[str, str], ...] = ()
    working_directory: Path | None = None
    evidence_path: Path | None = None


def _release_interpreter(python: str) -> Path:
    interpreter = Path(python).absolute()
    if not interpreter.is_file():
        raise ValueError(f"release interpreter is not a regular file: {interpreter}")
    return interpreter


def gate_definitions(
    root: Path,
    python: str,
    work_root: Path | None = None,
    deployment_root: Path | None = None,
    committed_source_commit: str | None = None,
) -> tuple[GateDefinition, ...]:
    root = root.resolve(strict=True)
    deployment_root = (deployment_root or root).resolve(strict=True)
    work_root = work_root or root / ".release-beta-gate-work"
    deployment_registry = (
        deployment_root / "resources" / "resource-registry.json"
    ).resolve(strict=True)
    deployment_environment = (
        ("QUALSIGHT_REPOSITORY_ROOT", str(deployment_root)),
        ("QUALSIGHT_REGISTRY_PATH", str(deployment_registry)),
        ("QUALSIGHT_RUNTIME_RESOURCE_ID", "ofiq-v1-1-2-linux-runtime"),
        (
            "QUALSIGHT_INPUT_RESOURCE_IDS",
            "ofiq-r-01-conformance-input,lfw-funneled-local-candidate",
        ),
        ("QUALSIGHT_NETWORK_GUARD", "1"),
        (
            "QUALSIGHT_SOURCE_CACHE_ROOT",
            str(deployment_root / "var" / "upstream"),
        ),
    )
    unittest_environment = (
        ("PYTHONFAULTHANDLER", "1"),
        *deployment_environment,
    )
    backend_environment = (
        ("PYTHONPATH", f"{root / 'backend' / 'src'}:{root}"),
        *unittest_environment,
        *(
            (
                ("QUALSIGHT_COMMITTED_STAGE_ROOT", str(root)),
                ("QUALSIGHT_COMMITTED_STAGE_COMMIT", committed_source_commit),
            )
            if committed_source_commit is not None
            else ()
        ),
    )
    validators = (
        (
            "resource-registry",
            "scripts.validate_resource_registry",
            (str(deployment_root / "resources" / "resource-registry.json"),),
        ),
        (
            "metric-registry",
            "scripts.validate_metric_registry",
            (str(deployment_root / "metrics" / "metric-registry.json"),),
        ),
        (
            "lfw-protocol",
            "scripts.validate_lfw_protocol",
            (
                "verify",
                "--registry",
                str(deployment_root / "resources" / "resource-registry.json"),
                "--manifest",
                str(
                    deployment_root
                    / "resources"
                    / "manifests"
                    / "lfw-local-pairs-v1.jsonl"
                ),
            ),
        ),
        (
            "lfw-protocol-authority",
            "scripts.validate_lfw_protocol_authority",
            (
                str(
                    deployment_root
                    / "resources"
                    / "manifests"
                    / "lfw-official-protocol-authority-v1.json"
                ),
                "--registry",
                str(deployment_root / "resources" / "resource-registry.json"),
                "--pairs",
                str(
                    deployment_root
                    / "resources"
                    / "manifests"
                    / "lfw-local-pairs-v1.jsonl"
                ),
            ),
        ),
        (
            "scrfd-release-provenance",
            "scripts.validate_scrfd_release_provenance",
            (
                str(
                    deployment_root
                    / "resources"
                    / "manifests"
                    / "insightface-buffalo-l-release-provenance-v1.json"
                ),
                "--registry",
                str(deployment_root / "resources" / "resource-registry.json"),
            ),
        ),
        (
            "shared-crop-lineage",
            "scripts.validate_shared_crop_lineage",
            (
                str(
                    deployment_root
                    / "resources"
                    / "manifests"
                    / "lfw-full-v1-shared-crop-lineage-reconciliation.json"
                ),
                "--registry",
                str(deployment_root / "resources" / "resource-registry.json"),
            ),
        ),
        ("atg-governance", "scripts.validate_atg_documents", ()),
    )
    return (
        GateDefinition(
            "root-tests",
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(deployment_root / "tests"),
                "-v",
            ),
            600,
            unittest_environment,
            working_directory=deployment_root,
        ),
        GateDefinition(
            "backend-tests",
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(deployment_root / "backend" / "tests"),
                "-t",
                str(deployment_root / "backend"),
                "-v",
            ),
            5400,
            backend_environment,
        ),
        GateDefinition(
            "frontend-toolchain",
            ("pnpm", "--dir", str(root), "run", "verify:toolchain"),
            120,
        ),
        GateDefinition(
            "frontend-build",
            ("pnpm", "--dir", str(root), "run", "build:frontend"),
            600,
        ),
        GateDefinition(
            "dependency-audit",
            (python, "-m", "scripts.audit_dependencies", "--check"),
            300,
        ),
        *(
            GateDefinition(
                name,
                (python, "-m", module, *arguments),
                600,
                deployment_environment,
            )
            for name, module, arguments in validators
        ),
        GateDefinition(
            "documentation-workflow",
            (
                python,
                str(root / "scripts" / "docs_workflow_gate.py"),
                "--observation",
                str(work_root / "documentation-workflow.json"),
            ),
            3600,
            backend_environment,
            evidence_path=work_root / "documentation-workflow.json",
        ),
        GateDefinition(
            "offline-workflow",
            (
                python,
                str(root / "scripts" / "offline_gate.py"),
                "--observation",
                str(work_root / "offline-workflow.json"),
                "--data-dir",
                str(work_root / "data"),
                "--source-root",
                str(root),
                "--repository-root",
                str(deployment_root),
                "--console-index",
                str(root / "frontend" / "dist" / "index.html"),
                "--python",
                python,
            ),
            3600,
            backend_environment,
        ),
        GateDefinition(
            "browser-beta3-workflows",
            (
                python,
                str(root / "scripts" / "validate_release_browser.py"),
                "--root",
                str(root),
                "--work-root",
                str(work_root / "browser-work"),
                "--output",
                str(work_root / "browser-aggregate.json"),
                "--python",
                python,
            ),
            14400,
            backend_environment,
            evidence_path=work_root / "browser-aggregate.json",
        ),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalised_argv(
    root: Path,
    deployment_root: Path,
    argv: tuple[str, ...],
    work_root: Path | None = None,
) -> list[str]:
    replacements = [
        (str(root), "<COMMITTED_STAGE>"),
        (str(deployment_root), "<CONTROLLED_DEPLOYMENT>"),
    ]
    if work_root is not None:
        replacements.append((str(work_root), "<GATE_WORK>"))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    normalised: list[str] = []
    for value in argv:
        for prefix, label in replacements:
            if value.startswith(prefix):
                value = label + value[len(prefix) :]
                break
        normalised.append(value)
    return normalised


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not (set(value) - set("0123456789abcdef"))
    )


def _failure_evidence_summary(
    gate_name: str,
    evidence_body: bytes,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "bytes": len(evidence_body),
        "sha256": _sha256_bytes(evidence_body),
    }
    if gate_name == "browser-beta3-workflows":
        try:
            evidence = json.loads(evidence_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return summary
        validators = evidence.get("validators") if isinstance(evidence, dict) else None
        if isinstance(validators, list):
            for item in validators:
                if not isinstance(item, dict):
                    continue
                validator = item.get("validator")
                exit_code = item.get("exit_code")
                if (
                    validator in BETA3_BROWSER_VALIDATORS
                    and isinstance(exit_code, int)
                    and exit_code != 0
                ):
                    summary["first_failed_validator"] = validator
                    summary["validator_exit_code"] = exit_code
                    break
    return summary


def _unittest_failure_diagnostic(stderr: bytes) -> dict[str, object]:
    last_test_id: str | None = None
    failed_test_ids: list[str] = []
    for line in stderr.splitlines():
        match = UNITTEST_VERBOSE_PREFIX.match(line)
        if match is None:
            if last_test_id is not None and line.endswith((b" ... FAIL", b" ... ERROR")):
                failed_test_ids.append(last_test_id)
            continue
        test_id = match.group("test_id").decode("ascii")
        last_test_id = test_id
        if line.endswith((b" ... FAIL", b" ... ERROR")):
            failed_test_ids.append(test_id)
    diagnostic: dict[str, object] = {}
    if last_test_id is not None:
        diagnostic["last_test_id"] = last_test_id
    if failed_test_ids:
        diagnostic["failed_test_ids"] = list(dict.fromkeys(failed_test_ids))
    return diagnostic


def _linux_process_identity(pid: int) -> str | None:
    """Return the kernel start-time identity for a live Linux process."""
    try:
        body = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except OSError:
        return None
    closing_parenthesis = body.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = body[closing_parenthesis + 1 :].split()
    if len(fields) <= 19 or fields[0] == "Z":
        return None
    return fields[19]


def _linux_descendant_identities(root_pid: int) -> dict[int, str]:
    """Snapshot all descendants, including children in separate sessions."""
    descendants: dict[int, str] = {}
    pending = [root_pid]
    visited = {root_pid}
    while pending:
        parent_pid = pending.pop()
        task_root = Path("/proc") / str(parent_pid) / "task"
        try:
            task_directories = tuple(task_root.iterdir())
        except OSError:
            continue
        child_pids: set[int] = set()
        for task_directory in task_directories:
            try:
                children = (task_directory / "children").read_text(
                    encoding="ascii"
                )
            except OSError:
                continue
            child_pids.update(int(value) for value in children.split())
        for child_pid in child_pids:
            if child_pid in visited:
                continue
            visited.add(child_pid)
            identity = _linux_process_identity(child_pid)
            if identity is None:
                continue
            descendants[child_pid] = identity
            pending.append(child_pid)
    return descendants


def _set_child_subreaper(enabled: bool) -> bool:
    """Set Linux orphan adoption for the current release-gate supervisor."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("release process supervision requires Linux pidfd support")
    libc = ctypes.CDLL(None, use_errno=True)
    state = ctypes.c_int()
    result = libc.prctl(
        ctypes.c_int(PR_GET_CHILD_SUBREAPER),
        ctypes.c_ulong(ctypes.addressof(state)),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    result = libc.prctl(
        ctypes.c_int(PR_SET_CHILD_SUBREAPER),
        ctypes.c_ulong(int(enabled)),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return bool(state.value)


def _pidfd_signal_linux_processes(
    identities: dict[int, str], termination_signal: signal.Signals
) -> None:
    """Signal only the exact processes bound by their PID descriptors."""
    for pid, identity in identities.items():
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            if _linux_process_identity(pid) != identity:
                continue
            try:
                signal.pidfd_send_signal(descriptor, termination_signal)
            except ProcessLookupError:
                pass
        finally:
            os.close(descriptor)


def _new_linux_descendants(
    controller_pid: int, baseline: dict[int, str]
) -> dict[int, str]:
    return {
        pid: identity
        for pid, identity in _linux_descendant_identities(controller_pid).items()
        if baseline.get(pid) != identity
    }


def _terminate_release_process_tree(
    baseline: dict[int, str],
) -> tuple[signal.Signals, dict[int, str]]:
    """Terminate the full gate tree, including reparented new-session children."""
    controller_pid = os.getpid()
    term_signalled: dict[int, str] = {}
    term_deadline = time.monotonic() + 2
    termination_signal = signal.SIGTERM
    while True:
        descendants = _new_linux_descendants(controller_pid, baseline)
        pending_term = {
            pid: identity
            for pid, identity in descendants.items()
            if term_signalled.get(pid) != identity
        }
        if pending_term:
            _pidfd_signal_linux_processes(pending_term, signal.SIGTERM)
            term_signalled.update(pending_term)
        if not descendants:
            break
        if time.monotonic() >= term_deadline:
            termination_signal = signal.SIGKILL
            break
        time.sleep(0.02)

    all_seen = dict(term_signalled)
    if termination_signal == signal.SIGKILL:
        kill_deadline = time.monotonic() + 2
        while True:
            descendants = _new_linux_descendants(controller_pid, baseline)
            all_seen.update(descendants)
            if not descendants:
                break
            _pidfd_signal_linux_processes(descendants, signal.SIGKILL)
            if time.monotonic() >= kill_deadline:
                raise RuntimeError("release process tree survived SIGKILL")
            time.sleep(0.02)

    return termination_signal, all_seen


def _reap_adopted_processes(identities: dict[int, str], root_pid: int) -> None:
    """Reap exited processes adopted through the temporary subreaper boundary."""
    for pid in identities:
        if pid == root_pid:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            pass


def _execute_gate(
    gate: GateDefinition,
    *,
    root: Path,
    deployment_root: Path,
    temporary: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    working_directory = gate.working_directory or root
    failure_diagnostic: dict[str, object] | None = None
    previous_subreaper_state = _set_child_subreaper(True)
    descendant_baseline = _linux_descendant_identities(os.getpid())
    try:
        try:
            process = subprocess.Popen(
                gate.argv,
                cwd=working_directory,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=gate.timeout_seconds)
            except subprocess.TimeoutExpired:
                termination_signal, adopted = _terminate_release_process_tree(
                    descendant_baseline
                )
                stdout, stderr = process.communicate()
                _reap_adopted_processes(adopted, process.pid)
                exit_code = 124
                unittest_diagnostic = (
                    _unittest_failure_diagnostic(stderr)
                    if gate.name in {"root-tests", "backend-tests"}
                    else {}
                )
                failure_diagnostic = {
                    "kind": "TIMEOUT",
                    "timed_out": True,
                    "termination_signal": signal.Signals(termination_signal).name,
                    **unittest_diagnostic,
                }
            else:
                exit_code = process.returncode
                if _new_linux_descendants(os.getpid(), descendant_baseline):
                    original_exit_code = exit_code
                    termination_signal, adopted = _terminate_release_process_tree(
                        descendant_baseline
                    )
                    _reap_adopted_processes(adopted, process.pid)
                    exit_code = 125
                    failure_diagnostic = {
                        "cleanup_signal": signal.Signals(termination_signal).name,
                        "kind": "PROCESS_LEAK",
                        "original_exit_code": original_exit_code,
                        **(
                            _unittest_failure_diagnostic(stderr)
                            if gate.name in {"root-tests", "backend-tests"}
                            else {}
                        ),
                    }
                    if original_exit_code < 0:
                        try:
                            failure_diagnostic["original_termination_signal"] = (
                                signal.Signals(-original_exit_code).name
                            )
                        except ValueError:
                            failure_diagnostic["original_termination_signal"] = (
                                "UNKNOWN"
                            )
            if exit_code != 0:
                unittest_diagnostic = (
                    _unittest_failure_diagnostic(stderr)
                    if gate.name in {"root-tests", "backend-tests"}
                    else {}
                )
                if failure_diagnostic is None:
                    failure_diagnostic = {
                        "kind": "PROCESS_EXIT",
                        **unittest_diagnostic,
                    }
                    if exit_code < 0:
                        try:
                            failure_diagnostic["termination_signal"] = signal.Signals(
                                -exit_code
                            ).name
                        except ValueError:
                            failure_diagnostic["termination_signal"] = "UNKNOWN"
        except OSError as error:
            exit_code = 126
            stdout = b""
            stderr = b""
            failure_diagnostic = {
                "kind": "LAUNCH_ERROR",
                "error_type": type(error).__name__,
            }
    finally:
        _set_child_subreaper(previous_subreaper_state)
    result: dict[str, object] = {
        "name": gate.name,
        "argv": _normalised_argv(root, deployment_root, gate.argv, temporary),
        "working_directory": _normalised_argv(
            root,
            deployment_root,
            (str(working_directory),),
            temporary,
        )[0],
        "exit_code": exit_code,
        "stdout_bytes": len(stdout),
        "stdout_sha256": _sha256_bytes(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": _sha256_bytes(stderr),
    }
    if failure_diagnostic is not None:
        result["failure_diagnostic"] = failure_diagnostic
    return result


def _validate_gate_evidence(
    gate_name: str,
    evidence_body: bytes,
    temporary: Path,
) -> dict[str, object]:
    evidence = json.loads(evidence_body)
    if not isinstance(evidence, dict):
        raise ValueError(f"{gate_name} evidence is not a JSON object")

    if gate_name == "documentation-workflow":
        if (
            evidence.get("kind") != "QUALSIGHT_DOCS_WORKFLOW_GATE"
            or evidence.get("guide") != "docs/USER_GUIDE.md"
            or not _is_sha256(evidence.get("guide_sha256"))
            or not _is_sha256(evidence.get("script_sha256"))
            or not isinstance(evidence.get("blocks"), int)
            or evidence["blocks"] < 1
            or evidence.get("exit_code") != 0
            or not isinstance(evidence.get("seconds"), (int, float))
            or evidence["seconds"] < 0
            or not isinstance(evidence.get("started_at"), str)
            or not evidence["started_at"]
        ):
            raise ValueError("documentation evidence does not record a passing guide run")
        report = {
            key: evidence[key]
            for key in (
                "kind",
                "guide",
                "guide_sha256",
                "blocks",
                "script_sha256",
                "exit_code",
                "seconds",
                "started_at",
            )
        }
        return {
            "path": "<GATE_WORK>/documentation-workflow.json",
            "bytes": len(evidence_body),
            "sha256": _sha256_bytes(evidence_body),
            "report": report,
        }

    if gate_name != "browser-beta3-workflows":
        raise ValueError(f"unsupported release-gate evidence type: {gate_name}")
    validators = evidence.get("validators")
    if (
        evidence.get("schema_version") != "qualsight-beta3-browser-aggregate-v1"
        or evidence.get("status") != "PASS"
        or evidence.get("expected_validator_count") != 9
        or evidence.get("completed_validator_count") != 9
        or not isinstance(validators, list)
        or any(not isinstance(item, dict) for item in validators)
        or [item.get("validator") for item in validators]
        != list(BETA3_BROWSER_VALIDATORS)
        or any(
            item.get("exit_code") != 0
            or not isinstance(item.get("observation"), dict)
            or item["observation"].get("bytes", 0) < 1
            or not _is_sha256(item["observation"].get("sha256"))
            for item in validators
        )
        or evidence.get("privacy")
        != {
            "absolute_paths_included": False,
            "full_sha256_identities_included": True,
            "raw_observations_included": False,
            "stdout_stderr_included": False,
        }
    ):
        raise ValueError("browser aggregate does not record nine passing validators")
    encoded = json.dumps(evidence, sort_keys=True)
    forbidden_terms = (
        "/" + "home/",
        "/" + "mnt/",
        "/" + "tmp/",
        "assistant/" + "analysis",
        "assistant/" + "commentary",
        "assistant/" + "final",
        "anth" + "ropic",
        "clau" + "de code",
        "co-authored" + "-by:",
        "generated by " + "clau" + "de",
        "session" + "_id",
        "thread" + "_id",
        "turn" + "_id",
    )
    if any(term in encoded for term in forbidden_terms) or str(temporary) in encoded:
        raise ValueError("browser aggregate contains a private or ephemeral path")
    return {
        "path": "<GATE_WORK>/browser-aggregate.json",
        "bytes": len(evidence_body),
        "sha256": _sha256_bytes(evidence_body),
        "report": evidence,
    }


def _git_identity(root: Path) -> tuple[str, str]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return commit, tree


def _validate_deployment_git_state(
    deployment_root: Path,
    *,
    commit: str,
    tree: str,
    after_gates: bool,
) -> None:
    deployment_commit, deployment_tree = _git_identity(deployment_root)
    if (deployment_commit, deployment_tree) != (commit, tree):
        if after_gates:
            raise ValueError(
                "controlled deployment Git identity changed while beta gates were running"
            )
        raise ValueError(
            "controlled deployment Git identity differs from the committed release stage"
        )
    deployment_status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=deployment_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if deployment_status:
        if after_gates:
            raise ValueError(
                "controlled deployment worktree changed while beta gates were running"
            )
        raise ValueError("controlled deployment worktree is not clean")


def _write_gate_record(
    *,
    output: Path,
    record: dict[str, object],
    deployment_root: Path,
    commit: str,
    tree: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_deployment_git_state(
        deployment_root,
        commit=commit,
        tree=tree,
        after_gates=True,
    )
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def run_beta_gate(
    root: Path,
    output: Path,
    python: str,
    commit: str,
    tree: str,
    deployment_root: Path | None = None,
) -> dict[str, object]:
    root = root.resolve(strict=True)
    deployment_root = (deployment_root or root).resolve(strict=True)
    output = output.absolute()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite beta gate record: {output}")
    if len(commit) != 40 or len(tree) != 40:
        raise ValueError("commit and tree must be complete 40-character Git identities")
    _validate_deployment_git_state(
        deployment_root,
        commit=commit,
        tree=tree,
        after_gates=False,
    )
    interpreter = _release_interpreter(python)

    temporary = Path(tempfile.mkdtemp(prefix=".release-beta-gate.", dir=root.parent))
    results: list[dict[str, object]] = []
    try:
        definitions = gate_definitions(
            root,
            str(interpreter),
            temporary,
            deployment_root,
            committed_source_commit=commit,
        )
        for gate in definitions:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("QUALSIGHT_")
            }
            environment.update(gate.environment)
            result = _execute_gate(
                gate,
                root=root,
                deployment_root=deployment_root,
                temporary=temporary,
                environment=environment,
            )
            if gate.evidence_path is not None:
                evidence_body: bytes | None = None
                try:
                    evidence_body = gate.evidence_path.read_bytes()
                    result["evidence"] = _validate_gate_evidence(
                        gate.name,
                        evidence_body,
                        temporary,
                    )
                except (
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    KeyError,
                ) as error:
                    result["evidence_error_type"] = type(error).__name__
                    if evidence_body is not None:
                        result["failure_evidence"] = _failure_evidence_summary(
                            gate.name,
                            evidence_body,
                        )
                    result["exit_code"] = 1
                    result.setdefault(
                        "failure_diagnostic",
                        {
                            "kind": "EVIDENCE_ERROR",
                            "error_type": type(error).__name__,
                        },
                    )
            results.append(result)
            if result["exit_code"] != 0:
                break
        expected_gate_count = len(definitions)
        succeeded = len(results) == expected_gate_count and all(
            result["exit_code"] == 0 for result in results
        )
        record: dict[str, object] = {
            "schema_version": "qualsight-controlled-beta-gate-v1",
            "release_authority": "CONTROLLED_SINGLE_USER_WORKSTATION_BETA",
            "git_commit": commit,
            "git_tree": tree,
            "path_bindings": {
                "<COMMITTED_STAGE>": "git archive of git_commit",
                "<CONTROLLED_DEPLOYMENT>": "clean controlled workstation checkout at git_commit",
                "<GATE_WORK>": "deleted private release-gate workspace",
            },
            "runtime_registry": {
                "path": "<CONTROLLED_DEPLOYMENT>/resources/resource-registry.json",
                "sha256": hashlib.sha256(
                    (deployment_root / "resources/resource-registry.json").read_bytes()
                ).hexdigest(),
                "size_bytes": (
                    deployment_root / "resources/resource-registry.json"
                ).stat().st_size,
            },
            "interpreter": {
                "version": subprocess.run(
                    [str(interpreter), "--version"],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=True,
                    text=True,
                ).stdout.strip(),
                "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
            },
            "status": "PASS" if succeeded else "FAIL",
            "gate_count": len(results),
            "expected_gate_count": expected_gate_count,
            "results": results,
        }
        _write_gate_record(
            output=output,
            record=record,
            deployment_root=deployment_root,
            commit=commit,
            tree=tree,
        )
        return record
    finally:
        shutil.rmtree(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--deployment-root", type=Path, required=True)
    args = parser.parse_args()
    record = run_beta_gate(
        args.root,
        args.output,
        args.python,
        args.commit,
        args.tree,
        args.deployment_root,
    )
    results = record["results"]
    passed = sum(result["exit_code"] == 0 for result in results)
    failed = next(
        (result for result in results if result["exit_code"] != 0),
        None,
    )
    not_run = record["expected_gate_count"] - record["gate_count"]
    print(
        f"controlled beta release gate {record['status']} "
        f"(passed={passed} failed={int(failed is not None)} not_run={not_run})"
    )
    if failed is not None:
        print(
            "first failed gate "
            f"name={failed['name']} exit={failed['exit_code']} "
            f"stdout_bytes={failed['stdout_bytes']} "
            f"stdout_sha256={failed['stdout_sha256']} "
            f"stderr_bytes={failed['stderr_bytes']} "
            f"stderr_sha256={failed['stderr_sha256']} "
            f"evidence_error_type={failed.get('evidence_error_type', 'none')}",
            file=sys.stderr,
        )
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
