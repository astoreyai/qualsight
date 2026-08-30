from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.capture_tree_manifest import capture
from scripts.validate_resource_registry import (
    validate_registry_file,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
OWNER_AUTHORIZATION_PATH = (
    ROOT
    / "resources"
    / "authorizations"
    / "qualsight-local-noncommercial-research-2026-08-14.json"
)
EXTERNAL_SCORES_RECEIPT_PATH = ROOT / "resources" / "authorizations" / "qualsight-owner-external-scores-option-a-2026-08-17.json"
USE_PERMISSION_RECEIPT_PATH = (
    ROOT
    / "resources"
    / "authorizations"
    / "qualsight-owner-use-permission-granted-2026-08-17.json"
)
ADDON_ACQUISITION_RECEIPT_PATH = (
    ROOT
    / "resources"
    / "authorizations"
    / "qualsight-owner-addon-acquisition-2026-08-28.json"
)
NEW_MATCHER_RESOURCE_IDS = {
    "magface-iresnet100-ms1mv2-local-candidate",
    "topofr-r100-glint360k-local-candidate",
    "transface-b-glint360k-local-candidate",
}
LOCAL_RESEARCH_SCOPES = [
    "QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1",
    "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
]
LFW_IMAGE_MANIFEST_PATH = (
    ROOT / "resources" / "manifests" / "lfw-funneled-images-v1.jsonl"
)
LFW_PROTOCOL_AUTHORITY_ARTIFACTS = {
    ROOT
    / "resources"
    / "authorizations"
    / "qualsight-lfw-protocol-evidence-review-2026-08-17.json": (
        "AUTHORIZATION",
        "415497a06bffa70c8c8a209fa7064836266eebb760e76ec24ca3d62fadd3f49d",
        1_684,
    ),
    ROOT
    / "resources"
    / "authorizations"
    / "qualsight-lfw-original-protocol-evidence-review-2026-08-17.json": (
        "AUTHORIZATION",
        "37dcb50842aaf1e7f7d6586bc6cf941e145ae0bffcef46fcfcf74948c28c8271",
        1_847,
    ),
    ROOT / "resources" / "upstream" / "lfw" / "lfw-2007.pdf": (
        "PROTOCOL",
        "87d9b19c74de1fc04c4748faec470956912ecfee39ed73213a87f5b87e516329",
        223_526,
    ),
    ROOT / "resources" / "upstream" / "lfw" / "lfw-2007.headers": (
        "DOCUMENTATION",
        "369d11f82eaab4cdaeb50abbb52d2c160bb70c78b8fd7e240924d27af0678d6d",
        324,
    ),
    ROOT / "resources" / "upstream" / "lfw" / "Huang_eccv2008-lfw.pdf": (
        "PROTOCOL",
        "004a66b3930dc6aa15d54bafbc7b734b1333669a8bcc08ea5ac93d89c251ae86",
        361_179,
    ),
    ROOT / "resources" / "upstream" / "lfw" / "Huang_eccv2008-lfw.headers": (
        "DOCUMENTATION",
        "3bed0d2ba44127d922abc2c252b54c6d98f814eb23a161e8eede8be4cc5f1538",
        324,
    ),
    ROOT / "resources" / "upstream" / "lfw" / "UM-CS-2014-003.pdf": (
        "PROTOCOL",
        "cceb20f7902b7c8094df3e14f711f7e7454e0e52cf1a4b6e1abbf70dc000b944",
        135_174,
    ),
    ROOT / "resources" / "upstream" / "lfw" / "UM-CS-2014-003.headers": (
        "DOCUMENTATION",
        "eb45195cbeb022b20f684d8001268da81e59d45a4e3a0c0e5fb6e3f1f4bab4b1",
        324,
    ),
}
OFIQ_V112_ACQUISITION_RECEIPT_PATH = Path(
    "/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/acquisition-receipt.json"
)
OFIQ_V112_MODEL_ROOT = Path(
    "/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/models-extracted/models"
)
OFIQ_V112_ONNX_ROOT = Path(
    "/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/onnxruntime-extracted/onnxruntime-linux-x64-1.18.1"
)
SCRFD_RELEASE_AUTHORIZATION_PATH = (
    ROOT / "resources" / "authorizations" / "qualsight-insightface-buffalo-l-evidence-acquisition-2026-08-17.json"
)
SCRFD_RELEASE_API_PATH = ROOT / "resources" / "upstream" / "insightface" / "release-v0.7.json"
SCRFD_RELEASE_API_HEADERS_PATH = ROOT / "resources" / "upstream" / "insightface" / "release-v0.7.headers"
SCRFD_RELEASE_ASSET_HEADERS_PATH = ROOT / "resources" / "upstream" / "insightface" / "buffalo_l-v0.7-final.headers"
SCRFD_ARCHIVE_PATH = Path("/home/aaron/.insightface/models/buffalo_l.zip")
QUALSIGHT_RESEARCH_SCOPE = (
    "QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1"
)


class ResourceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.resources = {
            resource["resource_id"]: resource for resource in cls.registry["resources"]
        }

    def test_real_registry_validates_recorded_bytes(self) -> None:
        summary = validate_registry_file(REGISTRY_PATH)
        self.assertEqual(summary["resources"], 13)
        self.assertEqual(summary["artifacts"], 205)
        self.assertEqual(summary["checks"], 18)
        self.assertEqual(
            summary["states"],
            {"APPROVED_LOCAL_RESEARCH_USE": 10, "BLOCKED": 2, "VERIFIED_LIMITED_USE": 1},
        )

    def test_ofiq_runtime_records_exact_configured_model_closure(self) -> None:
        runtime = self.resources["ofiq-v1-1-0-build-runtime"]
        closure = next(
            check for check in runtime["checks"] if check["type"] == "jaxn_model_closure"
        )
        self.assertEqual(len(closure["relative_paths"]), 12)
        self.assertEqual(
            closure["sha256"],
            "fa908247460807c7deea039c8c8bf76f0964d7bc863cd9f510766c559a80b7e7",
        )

    def test_blocked_ofiq_runtime_has_no_live_dynamic_linkage_check(self) -> None:
        runtime = self.resources["ofiq-v1-1-0-build-runtime"]
        self.assertFalse(
            any(check["type"] == "dynamic_linkage" for check in runtime["checks"])
        )

    def test_ofiq_git_source_metadata_matches_executable_identity_check(self) -> None:
        runtime = self.resources["ofiq-v1-1-0-build-runtime"]
        identity = next(
            check for check in runtime["checks"] if check["type"] == "git_identity"
        )
        self.assertEqual(runtime["source"]["local_root"], identity["root"])
        self.assertEqual(runtime["source"]["uri"], identity["remote"])
        self.assertEqual(runtime["source"]["commit"], identity["commit"])
        self.assertEqual(runtime["source"]["tag"], identity["tag"])

    def test_conformance_scope_covers_only_the_registered_real_image(self) -> None:
        conformance = self.resources["ofiq-r-01-conformance-input"]
        manifest = conformance["checks"][0]
        self.assertEqual(manifest["type"], "manifest")
        self.assertEqual(
            manifest["relative_paths"], ["data/tests/images/r-01-frontal.png"]
        )
        covered = {
            path
            for license_record in conformance["licenses"]
            for path in license_record["applies_to"]
        }
        self.assertEqual(
            covered,
            {
                "/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png"
            },
        )

    def test_effective_scopes_come_only_from_owner_receipts(self) -> None:
        approved = {
            resource["resource_id"]: resource
            for resource in self.registry["resources"]
            if resource["approved_scopes"]
        }
        self.assertEqual(
            set(approved),
            {
                "ofiq-r-01-conformance-input",
                "ofiq-v1-1-2-linux-runtime",
                "lfw-funneled-local-candidate",
                "adaface-ir101-local-candidate",
                "elasticface-arc-r100-local-candidate",
                "magface-iresnet100-ms1mv2-local-candidate",
                "topofr-r100-glint360k-local-candidate",
                "transface-b-glint360k-local-candidate",
                "transface-s-glint360k-local-candidate",
                "insightface-scrfd-10g-local-candidate",
                "external-scores-lfw-view2-insightface-buffalo-l",
            },
        )
        self.assertEqual(
            approved["ofiq-r-01-conformance-input"]["approved_scopes"],
            ["REPRODUCE_OFIQ_CONFORMANCE_TESTS"],
        )
        self.assertEqual(
            approved["ofiq-v1-1-2-linux-runtime"]["approved_scopes"],
            ["REPRODUCE_OFIQ_CONFORMANCE_TESTS", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"],
        )
        for resource_id, resource in approved.items():
            if resource_id == "ofiq-r-01-conformance-input":
                continue
            with self.subTest(resource=resource_id):
                self.assertEqual(resource["state"], "APPROVED_LOCAL_RESEARCH_USE")
                if resource_id == "external-scores-lfw-view2-insightface-buffalo-l":
                    self.assertEqual(resource["approved_scopes"], ["QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"])
                elif resource_id in NEW_MATCHER_RESOURCE_IDS:
                    self.assertEqual(resource["approved_scopes"], ["QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"])
                elif resource_id != "ofiq-v1-1-2-linux-runtime":
                    self.assertEqual(resource["approved_scopes"], LOCAL_RESEARCH_SCOPES)
                authorization = resource["owner_authorization"]
                self.assertEqual(
                    authorization["evidence_artifact_path"],
                    str(
                        EXTERNAL_SCORES_RECEIPT_PATH
                        if resource_id == "external-scores-lfw-view2-insightface-buffalo-l"
                        else ADDON_ACQUISITION_RECEIPT_PATH
                        if resource_id in NEW_MATCHER_RESOURCE_IDS
                        else USE_PERMISSION_RECEIPT_PATH
                    ),
                )
                self.assertLessEqual(
                    set(resource["approved_scopes"]), set(authorization["scopes"])
                )
                self.assertFalse(authorization["redistribution_allowed"])
                self.assertEqual(resource["blocked_reasons"], [])
                self.assertTrue(
                    resource["research_use_status"].startswith(
                        "OWNER_APPROVED_LOCAL_NONCOMMERCIAL_RESEARCH"
                    )
                )

    def test_unapproved_runtimes_remain_blocked(self) -> None:
        blocked = {
            resource["resource_id"]
            for resource in self.registry["resources"]
            if resource["state"] == "BLOCKED"
        }
        self.assertEqual(
            blocked,
            {
                "ofiq-v1-1-0-build-runtime",
                "ofiq-v1-1-2-cmake-runtime-candidate",
            },
        )

    def test_ofiq_v112_acquisition_receipt_binds_cmake_selected_archives(self) -> None:
        candidate = self.resources["ofiq-v1-1-2-cmake-runtime-candidate"]
        self.assertEqual(candidate["source"]["tag"], "v1.1.2")
        self.assertEqual(
            candidate["source"]["commit"],
            "d589ca27896a472446abc9eca8b62947ee47db19",
        )
        receipt = json.loads(
            OFIQ_V112_ACQUISITION_RECEIPT_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["retrieval_evidence_status"],
            "PROJECT_RECORDED_WITHOUT_RAW_TRANSFER_LOG",
        )
        self.assertEqual(
            receipt["build_status"]["state"],
            "NOT_OBSERVED_IN_SOURCE_CLONE",
        )
        artifacts = {artifact["path"]: artifact for artifact in candidate["artifacts"]}
        checks = {
            check["archive_path"]: check
            for check in candidate["checks"]
            if check["type"] == "archive_inventory"
        }
        for retrieval in receipt["retrievals"]:
            with self.subTest(archive=retrieval["path"]):
                artifact = artifacts[retrieval["path"]]
                self.assertEqual(artifact["role"], "ARCHIVE")
                self.assertEqual(artifact["sha256"], retrieval["sha256"])
                self.assertEqual(artifact["size_bytes"], retrieval["size_bytes"])
                validation = next(
                    item
                    for item in receipt["archive_validation"]
                    if item["path"] == retrieval["path"]
                )
                self.assertEqual(
                    checks[retrieval["path"]]["listing_sha256"],
                    validation["listing_sha256"],
                )

    def test_ofiq_v112_quarantine_model_tree_is_exact_registered_byte_equivalent(
        self,
    ) -> None:
        summary = validate_registry_file(REGISTRY_PATH)["ofiq_v112_model_bundle"]

        self.assertEqual(
            summary["reference_resource_id"], "ofiq-v1-1-0-build-runtime"
        )
        self.assertEqual(
            summary["candidate_resource_id"],
            "ofiq-v1-1-2-cmake-runtime-candidate",
        )
        self.assertEqual(
            summary["manifest_sha256"],
            "f25e7823837a5aefabf5e75eee6a6a237c91ce4a7cb725b9d1531bc6a63093a7",
        )
        self.assertEqual(summary["files"], 24)
        self.assertEqual(
            summary["role_counts"],
            {"DOCUMENTATION": 2, "LICENSE": 10, "MODEL": 12},
        )
        self.assertEqual(summary["configured_models"], 12)
        self.assertEqual(
            summary["configured_manifest_sha256"],
            "c1e41d4ccc2257c92162eb729d1d3dcc326a2742a1bff17087e1a1c11da8e175",
        )
        self.assertEqual(
            summary["configuration_path"],
            "/mnt/projects/02_perception_biometrics/OFIQ-Project-v1.1.2/data/ofiq_config.jaxn",
        )
        self.assertEqual(
            summary["configuration_data_root"],
            "/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/models-extracted",
        )
        self.assertEqual(
            summary["reference_model_root"],
            "/mnt/projects/02_perception_biometrics/OFIQ-Project/data/models",
        )
        self.assertEqual(
            summary["documented_license_expressions"],
            {
                "Apache-2.0 AND OFIQ-MIT": 2,
                "MIT AND OFIQ-MIT": 6,
                "OFIQ-MIT": 2,
            },
        )
        self.assertEqual(summary["all_candidate_model_target_edges"], 12)

    def test_ofiq_v112_quarantine_license_map_covers_each_registered_model_payload(
        self,
    ) -> None:
        candidate = self.resources["ofiq-v1-1-2-cmake-runtime-candidate"]
        model_root = f"{OFIQ_V112_MODEL_ROOT}/"
        tree_artifacts = [
            artifact
            for artifact in candidate["artifacts"]
            if artifact["path"].startswith(model_root)
        ]
        models = {
            artifact["path"]
            for artifact in tree_artifacts
            if artifact["role"] == "MODEL"
        }
        model_license_records = [
            record
            for record in candidate["licenses"]
            if record["artifact_path"].startswith(model_root)
        ]
        covered = [
            path
            for record in model_license_records
            for path in record["applies_to"]
        ]

        self.assertEqual(len(tree_artifacts), 24)
        self.assertEqual(len(models), 12)
        self.assertEqual(len(model_license_records), 10)
        self.assertEqual(len(covered), 12)
        self.assertEqual(set(covered), models)

    def test_ofiq_v112_model_evidence_does_not_promote_or_authorize_execution(
        self,
    ) -> None:
        candidate = self.resources["ofiq-v1-1-2-cmake-runtime-candidate"]

        self.assertEqual(candidate["state"], "BLOCKED")
        self.assertEqual(candidate["third_party_rights_status"], "UNRESOLVED")
        self.assertEqual(candidate["approved_scopes"], [])
        self.assertEqual(
            candidate["owner_authorization"]["decision"],
            "APPROVED_EVIDENCE_ACQUISITION",
        )

    def test_ofiq_v112_onnxruntime_archive_registers_every_static_evidence_byte(
        self,
    ) -> None:
        summary = validate_registry_file(REGISTRY_PATH)[
            "ofiq_v112_onnxruntime_static_preflight"
        ]
        candidate = self.resources["ofiq-v1-1-2-cmake-runtime-candidate"]
        artifacts = []
        for artifact in candidate["artifacts"]:
            try:
                Path(artifact["path"]).relative_to(OFIQ_V112_ONNX_ROOT)
            except ValueError:
                continue
            artifacts.append(artifact)

        self.assertEqual(summary["regular_files"], 19)
        self.assertEqual(summary["regular_file_bytes"], 16_203_903)
        self.assertEqual(
            summary["regular_manifest_sha256"],
            "7c9039d120bfffd1cb02a92846bab9e5b618da026c12f6d1335259b197a150b4",
        )
        self.assertEqual(
            summary["role_counts"],
            {
                "DOCUMENTATION": 2,
                "IMPLEMENTATION": 12,
                "LICENSE": 2,
                "RUNTIME_LIBRARY": 1,
                "VERSION_EVIDENCE": 2,
            },
        )
        self.assertEqual(len(artifacts), 19)
        self.assertEqual(
            set(summary["regular_relative_paths"]),
            {
                Path(artifact["path"]).relative_to(OFIQ_V112_ONNX_ROOT).as_posix()
                for artifact in artifacts
            },
        )
        self.assertEqual(summary["symlinks"], {"lib/libonnxruntime.so": "libonnxruntime.so.1.18.1"})
        self.assertEqual(summary["version"], "1.18.1")
        self.assertEqual(summary["api_version"], 18)
        self.assertEqual(
            summary["package_recorded_git_commit"],
            "387127404e6c1d84b3468c387d864877ed1c67fe",
        )
        self.assertEqual(summary["package_mit_targets"], 17)
        self.assertEqual(summary["third_party_notice_targets"], 1)
        self.assertEqual(
            summary["license_sha256"],
            "2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c",
        )
        self.assertEqual(
            summary["third_party_notices_sha256"],
            "10fc98a2f4d478e981813d594e23d7e31b0430e66501a658c1ecab2a226a4b52",
        )
        self.assertEqual(
            summary["archive_path"],
            "/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/onnxruntime-linux-x64-1.18.1.tgz",
        )
        self.assertEqual(
            summary["tagged_cmake_path"],
            "/mnt/projects/02_perception_biometrics/OFIQ-Project-v1.1.2/OFIQlib/CMakeLists.ubuntu.cmake",
        )
        self.assertEqual(
            summary["static_review_authorization_path"],
            "/mnt/projects/ofiqgui/resources/authorizations/qualsight-onnxruntime-static-review-2026-08-14.json",
        )
        self.assertEqual(
            summary["static_review_authorization_decision"],
            "APPROVED_STATIC_EVIDENCE_REVIEW",
        )
        self.assertEqual(
            summary["static_review_authorization_scope"],
            "STATIC_REVIEW_OFIQ_V1_1_2_ONNX_RUNTIME_EVIDENCE",
        )
        self.assertEqual(
            candidate["use_restrictions"][0],
            "Acquisition authority remains limited to acquisition, hashing, archive validation, and license review; a separate registered owner decision permits only non-executing static review of the preserved ONNX Runtime archive, package text, headers, licenses, notices, hashes, and ELF structures.",
        )
        self.assertEqual(
            candidate["research_use_status"],
            "OWNER_APPROVED_FOR_EVIDENCE_ACQUISITION_AND_NONEXECUTING_STATIC_REVIEW_ONLY",
        )

    def test_ofiq_v112_onnxruntime_elf_preflight_is_static_and_non_promoting(
        self,
    ) -> None:
        summary = validate_registry_file(REGISTRY_PATH)[
            "ofiq_v112_onnxruntime_static_preflight"
        ]
        candidate = self.resources["ofiq-v1-1-2-cmake-runtime-candidate"]

        self.assertEqual(summary["elf_class"], "ELF64")
        self.assertEqual(summary["elf_data"], "LITTLE_ENDIAN")
        self.assertEqual(summary["elf_type"], "ET_DYN")
        self.assertEqual(summary["elf_machine"], "EM_X86_64")
        self.assertEqual(summary["soname"], "libonnxruntime.so.1.18.1")
        self.assertEqual(summary["runpath"], "$ORIGIN")
        self.assertIsNone(summary["rpath"])
        self.assertFalse(summary["has_interpreter"])
        self.assertEqual(
            summary["needed"],
            [
                "libdl.so.2",
                "librt.so.1",
                "libpthread.so.0",
                "libstdc++.so.6",
                "libm.so.6",
                "libgcc_s.so.1",
                "libc.so.6",
                "ld-linux-x86-64.so.2",
            ],
        )
        self.assertEqual(
            summary["build_id"], "bc5995be4e80f2fcbb80ae9141067094eac52943"
        )
        self.assertEqual(
            summary["required_exports"],
            {
                "OrtGetApiBase": "VERS_1.18.1",
                "OrtSessionOptionsAppendExecutionProvider_CPU": "VERS_1.18.1",
            },
        )
        self.assertEqual(
            summary["max_required_versions"],
            {"CXXABI": "1.3.11", "GLIBC": "2.27", "GLIBCXX": "3.4.22"},
        )
        self.assertEqual(candidate["state"], "BLOCKED")
        self.assertEqual(candidate["third_party_rights_status"], "UNRESOLVED")
        self.assertEqual(candidate["approved_scopes"], [])
        self.assertFalse(
            any(check["type"] == "dynamic_linkage" for check in candidate["checks"])
        )

    def test_owner_authorization_is_distinct_from_third_party_rights(self) -> None:
        self.assertEqual(self.registry["schema_version"], "1.2.0")
        candidates = {
            resource_id: self.resources[resource_id]
            for resource_id in (
                "lfw-funneled-local-candidate",
                "adaface-ir101-local-candidate",
                "elasticface-arc-r100-local-candidate",
                "transface-s-glint360k-local-candidate",
                "insightface-scrfd-10g-local-candidate",
            )
        }
        # Owner authorization is recorded separately from the factual rights
        # position; the owner accepted local, non-redistributed research use.
        expected_rights = {
            "lfw-funneled-local-candidate": "NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET",
            "adaface-ir101-local-candidate": "REPOSITORY_LICENSE_ONLY",
            "elasticface-arc-r100-local-candidate": "VERIFIED_LICENSE",
            "transface-s-glint360k-local-candidate": "NO_LICENSE_FOUND",
            "insightface-scrfd-10g-local-candidate": "RESEARCH_NOTICE_ONLY",
        }
        for resource_id, resource in candidates.items():
            with self.subTest(resource=resource_id):
                authorization = resource["owner_authorization"]
                self.assertEqual(
                    authorization["decision"], "APPROVED_LOCAL_NONCOMMERCIAL"
                )
                self.assertIn(QUALSIGHT_RESEARCH_SCOPE, authorization["scopes"])
                self.assertFalse(authorization["redistribution_allowed"])
                self.assertEqual(
                    authorization["evidence_artifact_path"],
                    str(USE_PERMISSION_RECEIPT_PATH),
                )
                self.assertEqual(
                    resource["third_party_rights_status"],
                    expected_rights[resource_id],
                )
                self.assertEqual(resource["state"], "APPROVED_LOCAL_RESEARCH_USE")
                self.assertEqual(resource["approved_scopes"], LOCAL_RESEARCH_SCOPES)

    def test_authorization_receipt_has_one_exact_grant_per_resource(self) -> None:
        for resource_id, resource in self.resources.items():
            with self.subTest(resource=resource_id):
                authorization = resource["owner_authorization"]
                receipt = json.loads(
                    Path(authorization["evidence_artifact_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                grants = {
                    grant["resource_id"]: grant for grant in receipt["grants"]
                }
                grant = grants[resource_id]
                self.assertEqual(authorization["decision"], grant["decision"])
                self.assertEqual(authorization["scopes"], grant["scopes"])
                self.assertEqual(
                    authorization["redistribution_allowed"],
                    receipt["redistribution_allowed"],
                )

    def test_manifest_generator_reproduces_registered_real_lfw_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            regenerated = Path(directory) / "lfw-funneled-images-v1.jsonl"
            count, total_bytes, tree_sha256 = capture(
                Path("/mnt/projects/datasets/lfw/lfw_funneled"),
                "*.jpg",
                regenerated,
            )
            self.assertEqual(count, 13_233)
            self.assertEqual(total_bytes, 250_937_116)
            self.assertEqual(
                tree_sha256,
                "9017b665db8c7d1397ace11db160dc7ef6f82d7a5e92bd9851d101a6f164591e",
            )
            self.assertEqual(
                regenerated.read_bytes(),
                LFW_IMAGE_MANIFEST_PATH.read_bytes(),
            )

    def test_lfw_manifest_binds_every_real_image_byte(self) -> None:
        lfw = self.resources["lfw-funneled-local-candidate"]
        tree_check = next(
            check for check in lfw["checks"] if check["type"] == "tree_manifest"
        )
        self.assertEqual(tree_check["manifest_path"], str(LFW_IMAGE_MANIFEST_PATH))
        self.assertEqual(tree_check["expected_count"], 13_233)
        self.assertEqual(
            tree_check["sha256"],
            "9017b665db8c7d1397ace11db160dc7ef6f82d7a5e92bd9851d101a6f164591e",
        )

    def test_lfw_official_protocol_evidence_is_registered_without_promotion(
        self,
    ) -> None:
        lfw = self.resources["lfw-funneled-local-candidate"]
        artifacts = {artifact["path"]: artifact for artifact in lfw["artifacts"]}
        for path, (role, sha256, size_bytes) in LFW_PROTOCOL_AUTHORITY_ARTIFACTS.items():
            with self.subTest(path=path):
                artifact = artifacts[str(path)]
                self.assertEqual(artifact["role"], role)
                self.assertEqual(artifact["sha256"], sha256)
                self.assertEqual(artifact["size_bytes"], size_bytes)

        source_evidence = set(lfw["source"]["evidence_artifacts"])
        expected_source_evidence = {
            str(path)
            for path in LFW_PROTOCOL_AUTHORITY_ARTIFACTS
            if path.suffix in {".pdf", ".headers"}
        }
        self.assertTrue(expected_source_evidence.issubset(source_evidence))
        self.assertFalse(
            any(
                "Official protocol-update bytes" in reason
                for reason in lfw["blocked_reasons"]
            )
        )
        self.assertEqual(lfw["state"], "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(
            lfw["third_party_rights_status"], "NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET"
        )
        self.assertEqual(lfw["approved_scopes"], LOCAL_RESEARCH_SCOPES)

    def test_activated_matcher_models_have_exact_local_identities(self) -> None:
        expected = {
            "elasticface-arc-r100-local-candidate": (
                "/mnt/projects/02_perception_biometrics/ofiq_29794/data/weights/elasticface/elasticface_arc.pth",
                "8ea1772a9dc0e95b77c171a1713f667f353d86ad66f2e3d418e7973360d2fb28",
            ),
            "transface-s-glint360k-local-candidate": (
                "/mnt/projects/02_perception_biometrics/ofiq_29794/data/weights/transface/transface_s_glint360k.pth",
                "8a334ecc85fd0b547fb86836f5b926abdb4ec9b137ef65ed536d7b2d781905ce",
            ),
            "insightface-scrfd-10g-local-candidate": (
                "/home/aaron/.insightface/models/buffalo_l/det_10g.onnx",
                "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
            ),
        }
        for resource_id, (path, sha256) in expected.items():
            with self.subTest(resource=resource_id):
                model = next(
                    artifact
                    for artifact in self.resources[resource_id]["artifacts"]
                    if artifact["role"] == "MODEL"
                )
                self.assertEqual(model["path"], path)
                self.assertEqual(model["sha256"], sha256)

    def test_scrfd_official_release_provenance_is_registered_without_promotion(self) -> None:
        scrfd = self.resources["insightface-scrfd-10g-local-candidate"]
        artifacts = {artifact["path"]: artifact for artifact in scrfd["artifacts"]}
        expected = {
            str(SCRFD_RELEASE_AUTHORIZATION_PATH): ("AUTHORIZATION", "4ea9fca18ed5b1a95b55d59aff5f6995eb4f064f6374ebd45a192a2b6aadb481", 2109),
            str(SCRFD_RELEASE_API_PATH): ("DOCUMENTATION", "1458ba41add0f0f022e244bbc44aaebd3b03a06fc1620b9a190725fd1de6dca0", 14330),
            str(SCRFD_RELEASE_API_HEADERS_PATH): ("DOCUMENTATION", "cf7a746f39d8e94f2858c832d04dadb8e51a33fcbc85bd24ca80b3e4677d2af3", 1320),
            str(SCRFD_RELEASE_ASSET_HEADERS_PATH): ("DOCUMENTATION", "ec2eade1010ec13f2a97b3463c13e97cf0fc0b51e8db2ca920bf6fdf5d698543", 804),
            str(SCRFD_ARCHIVE_PATH): ("ARCHIVE", "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f", 288621354),
        }
        for path, (role, sha256, size_bytes) in expected.items():
            with self.subTest(path=path):
                self.assertIn(path, artifacts)
                artifact = artifacts[path]
                self.assertEqual(artifact["role"], role)
                self.assertEqual(artifact["sha256"], sha256)
                self.assertEqual(artifact["size_bytes"], size_bytes)
        source_evidence = set(scrfd["source"]["evidence_artifacts"])
        self.assertTrue({str(SCRFD_RELEASE_API_PATH), str(SCRFD_RELEASE_API_HEADERS_PATH), str(SCRFD_RELEASE_ASSET_HEADERS_PATH), str(SCRFD_ARCHIVE_PATH)}.issubset(source_evidence))
        obsolete = "No acquisition receipt binds the exact local ONNX model"
        self.assertFalse(any(obsolete in note for note in scrfd["source"]["notes"]))
        self.assertFalse(any(obsolete in reason for reason in scrfd["blocked_reasons"]))
        self.assertEqual(scrfd["state"], "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(scrfd["third_party_rights_status"], "RESEARCH_NOTICE_ONLY")
        self.assertEqual(scrfd["approved_scopes"], LOCAL_RESEARCH_SCOPES)


if __name__ == "__main__":
    unittest.main()
