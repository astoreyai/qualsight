from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    configured = os.environ.get("QUALSIGHT_REPOSITORY_ROOT")
    if configured is None:
        return Path(__file__).resolve().parents[1]
    root = Path(configured).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("QUALSIGHT_REPOSITORY_ROOT must resolve to a directory")
    return root


PROJECT_ROOT = _project_root()
RECONCILIATION_PATH = (
    PROJECT_ROOT
    / "resources"
    / "manifests"
    / "lfw-full-v1-shared-crop-lineage-reconciliation.json"
)
REGISTRY_PATH = PROJECT_ROOT / "resources" / "resource-registry.json"
HISTORICAL_MANIFEST_PATH = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/artifacts/local/runs/"
    "20260814-branch-b/aligned_crop_runs/lfw-full-v1/manifest.json"
)
GENERATOR_SOURCE_PATH = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/research/paper39/"
    "scripts/extract_aligned_crops.py"
)
SCRFD_ORIGINAL_PATH = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/packages/openfiqa/"
    "src/openfiqa/models/scrfd.py"
)
SOURCE_SEAL_PATH = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/artifacts/local/runs/"
    "20260814-branch-b/source_seals/usfiqa-component-source-seal-v2/manifest.json"
)
SEALED_SCRFD_PATH = SOURCE_SEAL_PATH.parent / "repository" / Path(
    "packages/openfiqa/src/openfiqa/models/scrfd.py"
)
OWNER_DECISION_PATH = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-local-noncommercial-research-2026-08-14.json"
)
HISTORICAL_MANIFEST_SHA256 = (
    "b379a01f75dc9e47d2146f2c99a2d0c1b0e8a03dc9288c0f70ded1d82802f23e"
)
GENERATOR_SOURCE_SHA256 = (
    "6a44a96e4846a687ca44fb78e3e3592f675acd318232659b91a7049820f3c77c"
)
# The mutable external generator path was rewritten by a process outside
# QualSight at 2026-08-17T07:23:09-05:00 (same size, new digest). The historical
# digest above is what the C09 classification was derived from; the current
# bytes are bound below and must still satisfy the same AST predicates.
GENERATOR_SOURCE_CURRENT_SHA256 = (
    "4cd584ef8603f0d1a04682a9667a9240e69881c0e283ea4f53b9f7f6795dc6f8"
)
GENERATOR_SOURCE_CURRENT_MTIME_NS = 1786969389620795328
GENERATOR_DRIFT_CLASSIFICATION = (
    "MUTABLE_EXTERNAL_PATH_REWRITTEN_AFTER_ACCEPTANCE_HISTORICAL_BYTES_NOT_PRESERVED_"
    "CURRENT_BYTES_REVALIDATED_BY_AST"
)
HISTORICAL_SCRFD_SHA256 = (
    "48b30da81ee67360bec8bf75dcfc2f9a27f9e263f5c94110a42659144bec4aae"
)
SOURCE_SEAL_SHA256 = (
    "782b5d7b296b646c924dfce1849f6f3ee994077de4a2d301149910a885dd7e7e"
)
OWNER_DECISION_SHA256 = (
    "48b11e76ac1d7288c40f0052fa000ae72f7edd4c930db3057751c7c7f841a3fa"
)
REGISTRY_SHA256 = (
    "ce34349c8c9474eae236bcfd771b4195fc95ca7cdea088df982080696dec5f05"
)
REGISTRY_SIZE = 162426
REQUIRED_SCRFD_STATE = "APPROVED_LOCAL_RESEARCH_USE"
REQUIRED_SCRFD_SCOPES = [
    "QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1",
    "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
]
USE_PERMISSION_RECEIPT = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-owner-use-permission-granted-2026-08-17.json"
)
PRODUCER_ID = (
    "SCRFD10G_ADAPTIVE_LARGEST_NORMCROP112_UNRESOLVED_MODE_"
    "JPEG95_ENCODED_HISTORICAL_V1"
)
TARGET_CONDITION_ID = "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1"
TARGET_MAPPING_STATUS = (
    "BLOCKED_PENDING_BYTE_BOUND_ARCFACE_TEMPLATE_AND_AUTHORIZED_"
    "BGR_DECODE_MATCHER_CONSUMPTION"
)
EXCLUDED_CONDITION_ID = (
    "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1"
)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1"
SCRFD_RESOURCE_ID = "insightface-scrfd-10g-local-candidate"
SCRFD_MODEL_PATH = Path("/home/aaron/.insightface/models/buffalo_l/det_10g.onnx")
SCRFD_MODEL_SHA256 = (
    "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91"
)
SCRFD_MODEL_SIZE = 16923827
HISTORICAL_PREPROCESSING_TEXT = (
    "OpenCV BGR input, 640 by 640 detector canvas, highest-confidence face, "
    "five-point 112 by 112 norm_crop."
)
LIMITATIONS = [
    "This receipt corrects only the detector sizing, face selection, alignment-call shape, and JPEG-encoding classification of the historical LFW shared-crop producer without modifying that historical artifact.",
    "The mutable original SCRFD source path is not execution evidence; only the exact source-seal snapshot is used to derive the historical behavior.",
    "The generator does not specify the norm_crop mode, so this receipt does not claim that the historical producer used the ArcFace template.",
    "JPEG decoding during verification or future matcher consumption is outside this producer receipt.",
    "This receipt does not authorize or validate crop consumption, image contents, detector outputs, matcher outputs, third-party rights, or a scientific Run.",
    "The mutable generator path was rewritten outside QualSight on 2026-08-17 after acceptance; the historical bytes were not preserved, so the classification is now re-derived from the current bytes by the same AST predicates and the historical digest is retained only as recorded history.",
]
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024


class SharedCropLineageValidationError(ValueError):
    """Raised when the preserved shared-crop lineage evidence is inconsistent."""


def _fail(location: str, message: str) -> None:
    raise SharedCropLineageValidationError(f"{location}: {message}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SharedCropLineageValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    return value


def _sequence(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(location, "must be a nonempty string without surrounding whitespace")
    return value


def _strict_keys(
    value: dict[str, Any], location: str, expected: set[str]
) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        _fail(location, f"missing fields: {', '.join(missing)}")
    if extra:
        _fail(location, f"unknown fields: {', '.join(extra)}")


def _load_json(path: Path, location: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size < 1 or size > MAX_JSON_BYTES:
            _fail(location, f"JSON size {size} is outside the accepted range")
        text = path.read_text(encoding="utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"cannot read strict UTF-8 JSON from {path}: {error}")
    return _mapping(value, location)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        _fail(str(path), f"cannot hash file: {error}")
    return digest.hexdigest()


def _identity(
    value: Any,
    location: str,
    *,
    expected_path: Path,
    expected_sha256: str,
    expected_size: int,
) -> Path:
    identity = _mapping(value, location)
    _strict_keys(identity, location, {"path", "sha256", "size_bytes"})
    path_text = _string(identity["path"], f"{location}.path")
    path = Path(path_text)
    if not path.is_absolute() or path != expected_path:
        _fail(f"{location}.path", f"must equal {expected_path}")
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        _fail(location, f"cannot resolve identity: {error}")
    if not resolved.is_file():
        _fail(location, "identity does not resolve to a regular file")
    if identity["sha256"] != expected_sha256:
        _fail(f"{location}.sha256", "recorded digest differs from the contract")
    if identity["size_bytes"] != expected_size:
        _fail(f"{location}.size_bytes", "recorded size differs from the contract")
    if stat.st_size != expected_size:
        _fail(location, f"actual size drifted to {stat.st_size}")
    actual_sha256 = _sha256(resolved)
    if actual_sha256 != expected_sha256:
        _fail(location, f"actual SHA-256 drifted to {actual_sha256}")
    return resolved


def _read_source(path: Path, location: str) -> tuple[str, ast.Module]:
    try:
        size = path.stat().st_size
        if size < 1 or size > MAX_SOURCE_BYTES:
            _fail(location, f"source size {size} is outside the accepted range")
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        _fail(location, f"cannot parse Python source: {error}")
    return text, tree


def _class_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(classes) != 1:
        _fail("sealed_scrfd_source", f"must contain one class {class_name}")
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    if len(methods) != 1:
        _fail("sealed_scrfd_source", f"must contain one method {method_name}")
    return methods[0]


def _module_function(tree: ast.Module, name: str, location: str) -> ast.FunctionDef:
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(functions) != 1:
        _fail(location, f"must contain one function {name}")
    return functions[0]


def _assigned_integer(tree: ast.Module, name: str, location: str) -> int:
    values: list[int] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            and not isinstance(node.value.value, bool)
        ):
            values.append(node.value.value)
    if len(values) != 1:
        _fail(location, f"must assign one integer to {name}")
    return values[0]


def _attribute_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _validate_scrfd_ast(tree: ast.Module) -> None:
    pick = _class_method(tree, "SCRFDDetector", "_pick_det_size")
    threshold_returns: set[tuple[int, int]] = set()
    for node in ast.walk(pick):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comparison = node.test
        if (
            isinstance(comparison.left, ast.Name)
            and comparison.left.id == "max_dim"
            and len(comparison.ops) == 1
            and isinstance(comparison.ops[0], ast.LtE)
            and len(comparison.comparators) == 1
            and isinstance(comparison.comparators[0], ast.Constant)
            and isinstance(comparison.comparators[0].value, int)
        ):
            returned = [
                item.value.value
                for item in node.body
                if isinstance(item, ast.Return)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, int)
            ]
            if len(returned) == 1:
                threshold_returns.add((comparison.comparators[0].value, returned[0]))
    if threshold_returns != {(160, 128), (320, 256)}:
        _fail("sealed_scrfd_source._pick_det_size", "adaptive thresholds differ")
    default_640 = any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr == "_det_size"
        for node in ast.walk(pick)
    )
    if not default_640:
        _fail("sealed_scrfd_source._pick_det_size", "default detector size is absent")

    detect = _class_method(tree, "SCRFDDetector", "detect")
    calls = [node for node in ast.walk(detect) if isinstance(node, ast.Call)]
    if not any(_attribute_chain(call.func) == "self._pick_det_size" for call in calls):
        _fail("sealed_scrfd_source.detect", "does not use adaptive detector sizing")
    sorted_calls = [
        call
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "sorted"
    ]
    if len(sorted_calls) != 1:
        _fail("sealed_scrfd_source.detect", "must contain one face-ordering sort")
    sorted_call = sorted_calls[0]
    reverse_keywords = [
        keyword
        for keyword in sorted_call.keywords
        if keyword.arg == "reverse"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
    ]
    key_lambdas = [
        keyword.value
        for keyword in sorted_call.keywords
        if keyword.arg == "key" and isinstance(keyword.value, ast.Lambda)
    ]
    if len(reverse_keywords) != 1 or len(key_lambdas) != 1:
        _fail("sealed_scrfd_source.detect", "face sort is not descending with a key")
    if not any(isinstance(node, ast.Mult) for node in ast.walk(key_lambdas[0])):
        _fail("sealed_scrfd_source.detect", "face sort key is not bounding-box area")
    largest_assignments = [
        node
        for node in ast.walk(detect)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "largest_index"
            for target in node.targets
        )
        and isinstance(node.value, ast.Subscript)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "order"
        and isinstance(node.value.slice, ast.Constant)
        and node.value.slice.value == 0
    ]
    if len(largest_assignments) != 1:
        _fail("sealed_scrfd_source.detect", "largest face is not the first area-sorted face")
    face_detection_calls = [
        call
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "FaceDetection"
    ]
    if len(face_detection_calls) != 1:
        _fail("sealed_scrfd_source.detect", "must construct one FaceDetection result")
    indexed_keywords = {"bbox", "landmarks", "confidence"}
    for keyword in face_detection_calls[0].keywords:
        if keyword.arg in indexed_keywords and not any(
            isinstance(node, ast.Name) and node.id == "largest_index"
            for node in ast.walk(keyword.value)
        ):
            _fail(
                "sealed_scrfd_source.detect",
                f"{keyword.arg} is not selected with largest_index",
            )


def _validate_generator_ast(tree: ast.Module) -> None:
    if _assigned_integer(tree, "ALIGNED_SIZE", "generator_source") != 112:
        _fail("generator_source.ALIGNED_SIZE", "must equal 112")
    if _assigned_integer(tree, "JPEG_QUALITY", "generator_source") != 95:
        _fail("generator_source.JPEG_QUALITY", "must equal 95")
    crop_record = _module_function(tree, "_crop_record", "generator_source")
    calls = [node for node in ast.walk(crop_record) if isinstance(node, ast.Call)]
    call_names = {_attribute_chain(call.func) for call in calls}
    required_calls = {"detector.detect", "face_align.norm_crop", "cv2.imencode"}
    if not required_calls <= call_names:
        _fail("generator_source._crop_record", "crop pipeline calls differ")
    norm_calls = [
        call for call in calls if _attribute_chain(call.func) == "face_align.norm_crop"
    ]
    if len(norm_calls) != 1 or not any(
        keyword.arg == "image_size"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "ALIGNED_SIZE"
        for keyword in norm_calls[0].keywords
    ):
        _fail("generator_source._crop_record", "alignment size is not bound")
    jpeg_calls = [call for call in calls if _attribute_chain(call.func) == "cv2.imencode"]
    if len(jpeg_calls) != 1:
        _fail("generator_source._crop_record", "JPEG encoding call differs")
    jpeg_call = jpeg_calls[0]
    if not (
        jpeg_call.args
        and isinstance(jpeg_call.args[0], ast.Constant)
        and jpeg_call.args[0].value == ".jpg"
        and any(
            isinstance(node, ast.Name) and node.id == "JPEG_QUALITY"
            for node in ast.walk(jpeg_call)
        )
    ):
        _fail("generator_source._crop_record", "JPEG quality binding differs")
    source_constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for required in (
        "largest SCRFD face by bounding-box area",
        "InsightFace five-point norm_crop",
        "JPEG",
    ):
        if required not in source_constants:
            _fail("generator_source", f"contract string differs: {required}")


def validate_reconciliation(
    reconciliation_path: Path = RECONCILIATION_PATH,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    if reconciliation_path.resolve(strict=True) != RECONCILIATION_PATH.resolve(strict=True):
        _fail("reconciliation", f"must use {RECONCILIATION_PATH}")
    receipt = _load_json(reconciliation_path, "reconciliation")
    receipt_fields = {
        "schema_version",
        "reconciliation_id",
        "recorded_at",
        "scope",
        "historical_crop_manifest",
        "generator_source",
        "generator_source_drift",
        "scrfd_execution_source",
        "owner_decision",
        "observed_contradiction",
        "reconciled_producer",
        "target_condition_mapping",
        "excluded_condition_ids",
        "resource_boundary",
        "claims",
        "limitations",
    }
    _strict_keys(receipt, "reconciliation", receipt_fields)
    fixed_scalars = {
        "schema_version": "1.1.0",
        "reconciliation_id": "lfw-full-v1-shared-crop-lineage-reconciliation",
        "scope": "STATIC_LINEAGE_CLASSIFICATION_ONLY",
    }
    for field, expected in fixed_scalars.items():
        if receipt[field] != expected:
            _fail(f"reconciliation.{field}", f"must equal {expected}")
    recorded_at = _string(receipt["recorded_at"], "reconciliation.recorded_at")
    try:
        reconciliation_time = datetime.fromisoformat(recorded_at)
    except ValueError:
        _fail("reconciliation.recorded_at", "must be ISO-8601")
    if reconciliation_time.tzinfo is None:
        _fail("reconciliation.recorded_at", "must include a UTC offset")

    historical_manifest_path = _identity(
        receipt["historical_crop_manifest"],
        "reconciliation.historical_crop_manifest",
        expected_path=HISTORICAL_MANIFEST_PATH,
        expected_sha256=HISTORICAL_MANIFEST_SHA256,
        expected_size=25294,
    )
    historical_generator = _mapping(
        receipt["generator_source"], "reconciliation.generator_source"
    )
    _strict_keys(
        historical_generator, "reconciliation.generator_source", {"path", "sha256", "size_bytes"}
    )
    if (
        historical_generator["path"] != str(GENERATOR_SOURCE_PATH)
        or historical_generator["sha256"] != GENERATOR_SOURCE_SHA256
        or historical_generator["size_bytes"] != 31363
    ):
        _fail("reconciliation.generator_source", "historical generator identity differs")
    drift = _mapping(receipt["generator_source_drift"], "reconciliation.generator_source_drift")
    _strict_keys(
        drift,
        "reconciliation.generator_source_drift",
        {"classification", "observed_mtime_ns", "current"},
    )
    if drift["classification"] != GENERATOR_DRIFT_CLASSIFICATION:
        _fail("reconciliation.generator_source_drift", "drift classification differs")
    if drift["observed_mtime_ns"] != GENERATOR_SOURCE_CURRENT_MTIME_NS:
        _fail("reconciliation.generator_source_drift", "observed drift mtime differs")
    generator_path = _identity(
        drift["current"],
        "reconciliation.generator_source_drift.current",
        expected_path=GENERATOR_SOURCE_PATH,
        expected_sha256=GENERATOR_SOURCE_CURRENT_SHA256,
        expected_size=31363,
    )
    if generator_path.stat().st_mtime_ns != GENERATOR_SOURCE_CURRENT_MTIME_NS:
        _fail("reconciliation.generator_source_drift", "the current generator bytes moved again")
    scrfd = _mapping(receipt["scrfd_execution_source"], "reconciliation.scrfd_execution_source")
    _strict_keys(
        scrfd,
        "reconciliation.scrfd_execution_source",
        {
            "manifest_recorded_original_path",
            "manifest_recorded_sha256",
            "manifest_recorded_size_bytes",
            "source_seal",
            "sealed_snapshot",
            "mutable_original_path_excluded_from_evidence",
        },
    )
    if scrfd["manifest_recorded_original_path"] != str(SCRFD_ORIGINAL_PATH):
        _fail("reconciliation.scrfd_execution_source", "historical original path differs")
    if scrfd["manifest_recorded_sha256"] != HISTORICAL_SCRFD_SHA256:
        _fail("reconciliation.scrfd_execution_source", "historical source digest differs")
    if scrfd["manifest_recorded_size_bytes"] != 6820:
        _fail("reconciliation.scrfd_execution_source", "historical source size differs")
    if scrfd["mutable_original_path_excluded_from_evidence"] is not True:
        _fail("reconciliation.scrfd_execution_source", "mutable source must be excluded")
    source_seal_path = _identity(
        scrfd["source_seal"],
        "reconciliation.scrfd_execution_source.source_seal",
        expected_path=SOURCE_SEAL_PATH,
        expected_sha256=SOURCE_SEAL_SHA256,
        expected_size=92555,
    )
    sealed_scrfd_path = _identity(
        scrfd["sealed_snapshot"],
        "reconciliation.scrfd_execution_source.sealed_snapshot",
        expected_path=SEALED_SCRFD_PATH,
        expected_sha256=HISTORICAL_SCRFD_SHA256,
        expected_size=6820,
    )

    owner = _mapping(receipt["owner_decision"], "reconciliation.owner_decision")
    owner_identity = {
        key: owner[key] for key in ("path", "sha256", "size_bytes") if key in owner
    }
    _identity(
        owner_identity,
        "reconciliation.owner_decision.identity",
        expected_path=OWNER_DECISION_PATH,
        expected_sha256=OWNER_DECISION_SHA256,
        expected_size=3381,
    )
    _strict_keys(
        owner,
        "reconciliation.owner_decision",
        {"path", "sha256", "size_bytes", "decision_evidence"},
    )
    decision_evidence = (
        "4C: compare and display both shared-crop and matcher-native preprocessing conditions."
    )
    if owner["decision_evidence"] != decision_evidence:
        _fail("reconciliation.owner_decision", "condition decision differs")
    authorization = _load_json(OWNER_DECISION_PATH, "owner_authorization")
    if decision_evidence not in _sequence(
        authorization.get("decision_evidence"), "owner_authorization.decision_evidence"
    ):
        _fail("owner_authorization", "does not contain the condition decision")
    try:
        owner_time = datetime.fromisoformat(
            _string(authorization["recorded_at"], "owner_authorization.recorded_at")
        )
    except ValueError:
        _fail("owner_authorization.recorded_at", "must be ISO-8601")
    if owner_time.tzinfo is None or owner_time > reconciliation_time:
        _fail("owner_authorization", "must predate the reconciliation")
    grants = [
        _mapping(grant, f"owner_authorization.grants[{index}]")
        for index, grant in enumerate(
            _sequence(authorization.get("grants"), "owner_authorization.grants")
        )
        if isinstance(grant, dict) and grant.get("resource_id") == SCRFD_RESOURCE_ID
    ]
    if len(grants) != 1 or grants[0].get("decision") != "APPROVED_LOCAL_NONCOMMERCIAL":
        _fail("owner_authorization", "SCRFD owner decision differs")
    if grants[0].get("scopes") != [RESEARCH_SCOPE]:
        _fail("owner_authorization", "SCRFD owner scope differs")

    historical_manifest = _load_json(historical_manifest_path, "historical_manifest")
    if historical_manifest.get("schema_version") != 1 or historical_manifest.get("dataset") != "lfw":
        _fail("historical_manifest", "identity or dataset differs")
    contract = _mapping(historical_manifest.get("contract"), "historical_manifest.contract")
    source_code = _mapping(contract.get("source_code"), "historical_manifest.contract.source_code")
    expected_source_records = {
        str(GENERATOR_SOURCE_PATH): (GENERATOR_SOURCE_SHA256, 31363),
        str(SCRFD_ORIGINAL_PATH): (HISTORICAL_SCRFD_SHA256, 6820),
    }
    for source_path, (expected_hash, expected_size) in expected_source_records.items():
        record = _mapping(
            source_code.get(source_path),
            f"historical_manifest.contract.source_code[{source_path}]",
        )
        if record != {
            "path": source_path,
            "sha256": expected_hash,
            "size_bytes": expected_size,
        }:
            _fail("historical_manifest.contract.source_code", f"record differs for {source_path}")
    detector = _mapping(contract.get("detector"), "historical_manifest.contract.detector")
    if detector.get("preprocessing") != HISTORICAL_PREPROCESSING_TEXT:
        _fail("historical_manifest.contract.detector.preprocessing", "contradictory text drifted")
    historical_detector_artifact = _mapping(
        detector.get("artifact"), "historical_manifest.contract.detector.artifact"
    )
    expected_detector_artifact = {
        "path": str(SCRFD_MODEL_PATH),
        "sha256": SCRFD_MODEL_SHA256,
        "size_bytes": SCRFD_MODEL_SIZE,
    }
    if historical_detector_artifact != expected_detector_artifact:
        _fail(
            "historical_manifest.contract.detector.artifact",
            "historical detector identity differs",
        )
    expected_observed = {
        "historical_preprocessing_text": HISTORICAL_PREPROCESSING_TEXT,
        "historical_selection_text": "largest SCRFD face by bounding-box area",
        "historical_alignment_text": "InsightFace five-point norm_crop",
        "historical_aligned_size": [112, 112],
        "historical_encoding": {"format": "JPEG", "quality": 95},
    }
    observed = _mapping(receipt["observed_contradiction"], "reconciliation.observed_contradiction")
    if observed != expected_observed:
        _fail("reconciliation.observed_contradiction", "does not match historical fields")
    if (
        contract.get("selection") != expected_observed["historical_selection_text"]
        or contract.get("alignment") != expected_observed["historical_alignment_text"]
        or contract.get("aligned_size") != expected_observed["historical_aligned_size"]
        or contract.get("encoding") != expected_observed["historical_encoding"]
    ):
        _fail("historical_manifest.contract", "executed-condition fields drifted")

    seal = _load_json(source_seal_path, "source_seal")
    if seal.get("schema_version") != 1 or seal.get("status") != "sealed":
        _fail("source_seal", "is not the sealed version-1 manifest")
    sealed_records = [
        _mapping(record, f"source_seal.files[{index}]")
        for index, record in enumerate(_sequence(seal.get("files"), "source_seal.files"))
        if isinstance(record, dict)
        and record.get("repository_relative_path")
        == "packages/openfiqa/src/openfiqa/models/scrfd.py"
    ]
    expected_sealed_record = {
        "role": "openfiqa_source",
        "original_path": str(SCRFD_ORIGINAL_PATH),
        "repository_relative_path": "packages/openfiqa/src/openfiqa/models/scrfd.py",
        "snapshot_path": str(SEALED_SCRFD_PATH),
        "sha256": HISTORICAL_SCRFD_SHA256,
        "size_bytes": 6820,
    }
    if len(sealed_records) != 1 or sealed_records[0] != expected_sealed_record:
        _fail("source_seal.files", "sealed SCRFD source record differs")

    _generator_text, generator_tree = _read_source(generator_path, "generator_source")
    _scrfd_text, scrfd_tree = _read_source(sealed_scrfd_path, "sealed_scrfd_source")
    _validate_generator_ast(generator_tree)
    _validate_scrfd_ast(scrfd_tree)

    producer = _mapping(
        receipt["reconciled_producer"], "reconciliation.reconciled_producer"
    )
    expected_producer = {
        "producer_id": PRODUCER_ID,
        "detector_input_sizing": {
            "maximum_dimension_at_most_160": 128,
            "maximum_dimension_at_most_320": 256,
            "otherwise": 640,
        },
        "face_selection": "LARGEST_BOUNDING_BOX_AREA",
        "alignment": "INSIGHTFACE_FIVE_POINT_NORM_CROP_112_MODE_NOT_EXPLICIT_IN_GENERATOR",
        "storage_boundary": "JPEG_QUALITY_95_ENCODED",
    }
    if producer != expected_producer:
        _fail("reconciliation.reconciled_producer", "producer mapping differs")
    target_mapping = _mapping(
        receipt["target_condition_mapping"],
        "reconciliation.target_condition_mapping",
    )
    expected_target_mapping = {
        "condition_id": TARGET_CONDITION_ID,
        "status": TARGET_MAPPING_STATUS,
        "missing_evidence": [
            "BYTE_BOUND_ARCFACE_TEMPLATE_FOR_HISTORICAL_NORM_CROP",
            "AUTHORIZED_BGR_DECODE_AND_MATCHER_CONSUMPTION",
        ],
    }
    if target_mapping != expected_target_mapping:
        _fail(
            "reconciliation.target_condition_mapping",
            "target condition boundary differs",
        )
    excluded = _sequence(receipt["excluded_condition_ids"], "reconciliation.excluded_condition_ids")
    if excluded != [EXCLUDED_CONDITION_ID] or TARGET_CONDITION_ID in excluded:
        _fail("reconciliation.excluded_condition_ids", "native condition exclusion differs")

    boundary = _mapping(receipt["resource_boundary"], "reconciliation.resource_boundary")
    _strict_keys(
        boundary,
        "reconciliation.resource_boundary",
        {"registry", "scrfd_resource_id", "required_state", "required_approved_scopes"},
    )
    if registry_path.resolve(strict=True) != REGISTRY_PATH.resolve(strict=True):
        _fail("registry", f"must use {REGISTRY_PATH}")
    registry_identity = _mapping(boundary["registry"], "reconciliation.resource_boundary.registry")
    _identity(
        registry_identity,
        "reconciliation.resource_boundary.registry",
        expected_path=REGISTRY_PATH,
        expected_sha256=REGISTRY_SHA256,
        expected_size=REGISTRY_SIZE,
    )
    if (
        boundary["scrfd_resource_id"] != SCRFD_RESOURCE_ID
        or boundary["required_state"] != REQUIRED_SCRFD_STATE
        or boundary["required_approved_scopes"] != REQUIRED_SCRFD_SCOPES
    ):
        _fail("reconciliation.resource_boundary", "non-promotion boundary differs")
    registry = _load_json(registry_path, "resource_registry")
    resources = [
        _mapping(resource, f"resource_registry.resources[{index}]")
        for index, resource in enumerate(
            _sequence(registry.get("resources"), "resource_registry.resources")
        )
        if isinstance(resource, dict) and resource.get("resource_id") == SCRFD_RESOURCE_ID
    ]
    if len(resources) != 1:
        _fail("resource_registry", "must contain exactly one SCRFD resource")
    resource = resources[0]
    # This lineage child never promotes SCRFD; the current state comes only from
    # the owner use-permission receipt, which must back it exactly.
    owner = resource.get("owner_authorization")
    if (
        resource.get("state") != REQUIRED_SCRFD_STATE
        or resource.get("approved_scopes") != REQUIRED_SCRFD_SCOPES
        or not isinstance(owner, dict)
        or owner.get("decision") != "APPROVED_LOCAL_NONCOMMERCIAL"
        or owner.get("redistribution_allowed") is not False
        or owner.get("evidence_artifact_path") != str(USE_PERMISSION_RECEIPT)
        or not set(REQUIRED_SCRFD_SCOPES) <= set(owner.get("scopes") or [])
    ):
        _fail("resource_registry", "SCRFD state is not backed by the owner use-permission receipt")
    if resource.get("third_party_rights_status") != "RESEARCH_NOTICE_ONLY":
        _fail("resource_registry", "SCRFD rights boundary differs")
    if resource.get("version") != f"sha256:{SCRFD_MODEL_SHA256}":
        _fail("resource_registry", "SCRFD resource version differs from the historical model")
    registered_models = [
        _mapping(artifact, f"resource_registry.SCRFD.artifacts[{index}]")
        for index, artifact in enumerate(
            _sequence(resource.get("artifacts"), "resource_registry.SCRFD.artifacts")
        )
        if isinstance(artifact, dict) and artifact.get("role") == "MODEL"
    ]
    if len(registered_models) != 1:
        _fail("resource_registry", "SCRFD must contain exactly one MODEL artifact")
    registered_detector_artifact = {
        key: registered_models[0].get(key)
        for key in ("path", "sha256", "size_bytes")
    }
    if registered_detector_artifact != historical_detector_artifact:
        _fail(
            "resource_registry",
            "registered SCRFD model differs from the historical detector artifact",
        )
    _identity(
        registered_detector_artifact,
        "resource_registry.SCRFD.MODEL",
        expected_path=SCRFD_MODEL_PATH,
        expected_sha256=SCRFD_MODEL_SHA256,
        expected_size=SCRFD_MODEL_SIZE,
    )

    claims = _mapping(receipt["claims"], "reconciliation.claims")
    expected_claims = {
        "target_condition_mapping_complete": False,
        "crop_consumption_authorized": False,
        "output_validity_claimed": False,
        "detector_or_image_execution_performed": False,
        "third_party_rights_closed": False,
    }
    if claims != expected_claims:
        _fail("reconciliation.claims", "must preserve every non-execution boundary")
    limitations = _sequence(receipt["limitations"], "reconciliation.limitations")
    if limitations != LIMITATIONS:
        _fail("reconciliation.limitations", "limitations differ")

    return {
        "reconciliation_id": fixed_scalars["reconciliation_id"],
        "historical_manifest_sha256": HISTORICAL_MANIFEST_SHA256,
        "generator_source_sha256": GENERATOR_SOURCE_SHA256,
        "source_seal_sha256": SOURCE_SEAL_SHA256,
        "sealed_scrfd_sha256": HISTORICAL_SCRFD_SHA256,
        "producer_id": PRODUCER_ID,
        "target_condition_id": TARGET_CONDITION_ID,
        "target_condition_mapping_status": TARGET_MAPPING_STATUS,
        "excluded_condition_ids": excluded,
        "historical_detector_artifact": expected_detector_artifact,
        "registered_detector_artifact": registered_detector_artifact,
        "scrfd_resource_state": resource["state"],
        "scrfd_rights_status": resource["third_party_rights_status"],
        "scrfd_approved_scopes": resource["approved_scopes"],
        "crop_consumption_authorized": claims["crop_consumption_authorized"],
        "output_validity_claimed": claims["output_validity_claimed"],
        "detector_or_image_execution_performed": claims[
            "detector_or_image_execution_performed"
        ],
        "mutable_original_source_used": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the static LFW shared-crop lineage reconciliation."
    )
    parser.add_argument("reconciliation", type=Path)
    parser.add_argument(
        "--registry", type=Path, default=REGISTRY_PATH, help="current resource registry"
    )
    args = parser.parse_args(argv)
    try:
        summary = validate_reconciliation(args.reconciliation, args.registry)
    except (OSError, SharedCropLineageValidationError) as error:
        print(f"SHARED_CROP_LINEAGE_INVALID: {error}")
        return 1
    print(
        "SHARED_CROP_LINEAGE_VALID "
        + " ".join(
            (
                f"id={summary['reconciliation_id']}",
                f"producer={summary['producer_id']}",
                f"target_condition={summary['target_condition_id']}",
                f"target_mapping={summary['target_condition_mapping_status']}",
                f"scrfd_state={summary['scrfd_resource_state']}",
                f"approved_scopes={len(summary['scrfd_approved_scopes'])}",
                "crop_consumption_authorized=false",
                "output_validity_claimed=false",
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
