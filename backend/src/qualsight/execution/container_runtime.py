"""Container execution adapter (PRD §51.9; QS-X64).

Runs a registered CLI runtime inside an immutable OCI container built by
QualSight from *registered bytes only*: the runtime resource's binary
directory (OFIQ v1.1.2 install tree) plus the workstation's own C/C++
runtime libraries that the binary links against (glibc, libstdc++, libgcc —
copied from the host OS, hashed, and recorded). The image is built ``FROM
scratch`` so it contains nothing that was not registered or recorded; the
image id and the rootfs manifest hash travel with every execution. Containers
run with ``--network none``, a read-only root filesystem, no capabilities,
``no-new-privileges``, the caller's uid/gid, and only the registered
configuration/data directories, the input roots, and the job directory
mounted (inputs read-only). Paths inside the container equal host paths, so
the registered adapter's argument contract is reused unchanged: the container
adapter wraps a ``RegisteredCLIAdapter`` for request creation and contract
validation and only replaces the process launch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .cli_runtime import RegisteredCLIAdapter, RunningExecution
from .cli_runtime import PreparedExecution
from .contracts import ContractValidationError, LifecycleEvent, LifecycleState, transition_lifecycle

CONTAINER_ADAPTER_VERSION = "1.0.0"
IMAGE_REPOSITORY = "qualsight/ofiq-runtime"


class ContainerRuntimeError(RuntimeError):
    """Raised when the container tooling or image is unavailable or drifted."""


@dataclass(frozen=True, slots=True)
class ContainerImage:
    image_id: str  # sha256:… (docker image id)
    tag: str
    rootfs_manifest_sha256: str
    rootfs_files: int
    runtime_resource_id: str
    runtime_bin_dir: str
    host_libraries: tuple[dict[str, str], ...]
    docker_version: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def docker_available() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker CLI is not installed"
    try:
        result = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"docker is not reachable: {error}"
    if result.returncode != 0:
        return False, f"docker daemon is not reachable: {result.stderr.strip()[:200]}"
    return True, result.stdout.strip()


def _linked_host_libraries(binary: Path, bin_dir: Path) -> list[Path]:
    """Shared objects the binary resolves outside its own registered directory (host C/C++ runtime)."""

    result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ContainerRuntimeError(f"ldd failed for {binary}: {result.stderr.strip()[:200]}")
    libraries: list[Path] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if "=>" in parts:
            target = parts[parts.index("=>") + 1]
        elif parts and parts[0].startswith("/"):
            target = parts[0]
        else:
            continue
        path = Path(target)
        if not path.exists():
            continue
        try:
            path.relative_to(bin_dir)
            continue  # registered runtime library, already inside the tree
        except ValueError:
            libraries.append(path)
    return sorted(set(libraries))


def build_runtime_image(*, runtime_resource: Any, work_dir: Path) -> ContainerImage:
    """Build (or reuse) the FROM-scratch image for the registered runtime; deterministic per rootfs manifest."""

    available, docker_version = docker_available()
    if not available:
        raise ContainerRuntimeError(docker_version)
    binary = Path(runtime_resource.artifact("BINARY").identity.path)
    bin_dir = binary.parent
    host_libraries = _linked_host_libraries(binary, bin_dir)
    entries: list[tuple[str, str]] = []
    for path in sorted(bin_dir.rglob("*")):
        if path.is_file():
            entries.append((f"/ofiq/bin/{path.relative_to(bin_dir).as_posix()}", _sha256(path)))
    library_records = []
    for path in host_libraries:
        resolved = path.resolve()
        entries.append((str(path), _sha256(resolved)))
        library_records.append({"path": str(path), "resolved": str(resolved), "sha256": _sha256(resolved), "size_bytes": str(resolved.stat().st_size)})
    manifest_text = "\n".join(f"{sha}  {name}" for name, sha in sorted(entries)) + "\n"
    manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    tag = f"{IMAGE_REPOSITORY}:{CONTAINER_ADAPTER_VERSION.replace('.', '')}-{manifest_sha[:16]}"
    existing = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True, check=False)
    if existing.returncode == 0 and existing.stdout.strip():
        return ContainerImage(existing.stdout.strip(), tag, manifest_sha, len(entries), runtime_resource.resource_id, str(bin_dir), tuple(library_records), docker_version)
    work_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="qualsight-image-", dir=str(work_dir)))
    try:
        rootfs = stage / "rootfs"
        (rootfs / "tmp").mkdir(parents=True)
        shutil.copytree(bin_dir, rootfs / "ofiq" / "bin", symlinks=False)
        for path in host_libraries:
            target = rootfs / path.relative_to("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path.resolve(), target)
        # World-readable tree (directories 0755, files keep the owner's exec bit and gain o+rX) so the container
        # runs as the caller's non-root uid inside a root filesystem it can traverse.
        os.chmod(stage, 0o755)
        for entry in [rootfs, *rootfs.rglob("*")]:
            mode = entry.stat().st_mode & 0o777
            if entry.is_dir():
                os.chmod(entry, 0o755)
            else:
                os.chmod(entry, mode | 0o444 | (0o111 if mode & 0o100 else 0))
        os.chmod(rootfs / "tmp", 0o1777)
        (stage / "rootfs.manifest").write_text(manifest_text, encoding="utf-8")
        (stage / "Dockerfile").write_text('FROM scratch\nCOPY rootfs/ /\nENV LD_LIBRARY_PATH=/ofiq/bin\nENTRYPOINT ["/ofiq/bin/OFIQSampleApp"]\n', encoding="utf-8")
        build = subprocess.run(["docker", "build", "-q", "-t", tag, "--label", f"qualsight.rootfs_manifest_sha256={manifest_sha}", "--label", f"qualsight.runtime_resource_id={runtime_resource.resource_id}", str(stage)], capture_output=True, text=True, check=False)
        if build.returncode != 0:
            raise ContainerRuntimeError(f"docker build failed: {build.stderr.strip()[-400:]}")
        image_id = build.stdout.strip()
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return ContainerImage(image_id, tag, manifest_sha, len(entries), runtime_resource.resource_id, str(bin_dir), tuple(library_records), docker_version)


class ContainerCLIAdapter:
    """Wrap a registered CLI adapter: identical request contract, container launch."""

    def __init__(self, *, inner: RegisteredCLIAdapter, image: ContainerImage, read_only_mounts: tuple[Path, ...], gpu: bool = False) -> None:
        self.inner = inner
        self.image = image
        self.read_only_mounts = tuple(Path(p).resolve() for p in read_only_mounts)
        self.gpu = gpu
        self.launches: list[tuple[str, ...]] = []

    def create_request(self, **kwargs: Any) -> Any:
        return self.inner.create_request(**kwargs)

    def prepare(self, request: Any) -> PreparedExecution:
        return self.inner.prepare(request)

    def environment_record(self) -> dict[str, Any]:
        return {"kind": "CONTAINER", "adapter_version": CONTAINER_ADAPTER_VERSION, "image_id": self.image.image_id, "image_tag": self.image.tag, "rootfs_manifest_sha256": self.image.rootfs_manifest_sha256, "rootfs_files": self.image.rootfs_files,
                "runtime_resource_id": self.image.runtime_resource_id, "host_libraries": list(self.image.host_libraries), "docker_server_version": self.image.docker_version, "network": "none", "read_only_rootfs": True, "capabilities": "none", "no_new_privileges": True, "gpu": self.gpu,
                "mounts": [f"{p}:ro" for p in self.read_only_mounts] + ["<job directory>:rw"], "launcher_commands": [list(cmd) for cmd in self.launches]}

    def start(self, prepared: PreparedExecution) -> RunningExecution:
        if not isinstance(prepared, PreparedExecution):
            raise ContractValidationError("container start requires a PreparedExecution")
        if prepared._adapter_token is not self.inner._preparation_token:  # noqa: SLF001 - same token discipline as the CLI adapter
            raise ContractValidationError("prepared execution was not produced by this adapter's inner CLI adapter")
        self.inner._validate_request(prepared.request)  # noqa: SLF001
        running_event = LifecycleEvent.observe(transition_lifecycle(prepared.lifecycle[-1].state, LifecycleState.RUNNING))
        arguments = prepared.request.arguments
        list_path = Path(arguments[3])
        job_dir = list_path.parent.resolve()
        image_check = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image.tag], capture_output=True, text=True, check=False)
        if image_check.returncode != 0 or image_check.stdout.strip() != self.image.image_id:
            raise ContractValidationError("container image is missing or its id drifted from the recorded image id")
        command: list[str] = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}"]
        if self.gpu:
            command += ["--gpus", "all"]
        for mount in self.read_only_mounts:
            command += ["-v", f"{mount}:{mount}:ro"]
        command += ["-v", f"{job_dir}:{job_dir}:rw", self.image.tag, *arguments]
        process = subprocess.Popen(command, cwd=str(job_dir), env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True)
        self.launches.append(tuple(command))
        # The execution record's command names the registered executable that ran inside the container
        # (the contract's invariant); the docker launcher argv is recorded in the environment record.
        executable_path = prepared.request.executable.path
        return RunningExecution(prepared=prepared, process=process, command=(executable_path, *arguments), running_event=running_event)


def container_environment_json(adapter: ContainerCLIAdapter) -> str:
    return json.dumps(adapter.environment_record(), sort_keys=True)
