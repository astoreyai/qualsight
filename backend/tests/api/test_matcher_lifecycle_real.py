"""Matcher asset lifecycle over the exact registered AdaFace checkpoint and sources."""

from __future__ import annotations

import json
import inspect
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from qualsight.api import Settings, create_app
from qualsight.api.app import DegradationAnalysisRequestBody, UtilityAnalysisRequest
from qualsight.cli import _selected_axis
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID
from qualsight.matchers.conditions import ConditionId
from qualsight.matchers.runtime import MatcherId


ROOT = Path(__file__).resolve().parents[3]
MATCHER_ID = "adaface-ir101-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class MatcherLifecycleRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-lifecycle-api-"))
        self.previous_device = os.environ.get("QUALSIGHT_MATCHER_DEVICE")
        os.environ["QUALSIGHT_MATCHER_DEVICE"] = "cpu"
        self.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
        )
        self.client = TestClient(
            create_app(self.settings), base_url="http://127.0.0.1", raise_server_exceptions=False
        )
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        if self.previous_device is None:
            os.environ.pop("QUALSIGHT_MATCHER_DEVICE", None)
        else:
            os.environ["QUALSIGHT_MATCHER_DEVICE"] = self.previous_device
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_pull_load_unload_and_durable_system_events(self) -> None:
        initial = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[MATCHER_ID]
        self.assertEqual(initial["checkpoint"]["state"], "VERIFIED")
        self.assertEqual(initial["runtime"]["state"], "UNLOADED")
        self.assertTrue(initial["checkpoint_acquisition"]["available"])
        self.assertEqual(initial["checkpoint_acquisition"]["state"], "ADMITTED_LOCAL_NONCOMMERCIAL")

        pulled = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/pull-sources")
        self.assertEqual(pulled.status_code, 200, pulled.text)
        self.assertEqual([item["state"] for item in pulled.json()["asset"]["architecture_sources"]], ["VERIFIED"])

        loaded = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/load")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        runtime = loaded.json()["asset"]["runtime"]
        self.assertEqual(runtime["state"], "LOADED")
        self.assertEqual(runtime["device"], "cpu")
        self.assertEqual(runtime["parameter_count"], 65_150_912)

        unloaded = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/unload")
        self.assertEqual(unloaded.status_code, 200, unloaded.text)
        self.assertEqual(unloaded.json()["asset"]["runtime"]["state"], "UNLOADED")
        events = self.client.get("/api/system-events").json()
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "MATCHER_UNLOADED",
                "MATCHER_UNLOAD_REQUESTED",
                "MATCHER_LOADED",
                "MATCHER_LOAD_REQUESTED",
                "MATCHER_ARCHITECTURE_SOURCES_PULLED",
                "MATCHER_ARCHITECTURE_SOURCES_PULL_REQUESTED",
            ],
        )
        self.assertTrue(all(event["evidence_sha256"] for event in events))

    def test_closed_addon_catalog_contains_only_real_admitted_capabilities(self) -> None:
        response = self.client.get("/api/addons")
        self.assertEqual(response.status_code, 200, response.text)
        catalog = response.json()
        self.assertEqual(catalog["mode"], "CLOSED_ALLOWLIST")
        self.assertEqual(len(catalog["matchers"]), 6)
        self.assertEqual(len(catalog["datasets"]), 4)
        self.assertEqual(len(catalog["preprocessing"]), 3)
        self.assertEqual(len(catalog["transforms"]), 7)
        self.assertEqual(catalog["connections"], [])
        self.assertEqual(catalog["connection_policy"]["state"], "NOT_ADMITTED_IN_BETA")
        profiles = {item["addon_id"]: item for item in catalog["preprocessing"]}
        prealigned = profiles["AUTHOR_PREALIGNED_ARCFACE112_INMEMORY_V1"]
        self.assertEqual(prealigned["runtime_state"], "READY")
        self.assertEqual(prealigned["input_representation"], "AUTHOR_PREALIGNED_ARCFACE112")
        self.assertIsNone(prealigned["detector_resource_id"])
        self.assertTrue(
            all(
                item["input_representation"] == "SOURCE_IMAGE_REQUIRING_DETECTION"
                for addon_id, item in profiles.items()
                if addon_id != prealigned["addon_id"]
            )
        )

        matcher_ids = {item["addon_id"] for item in catalog["matchers"]}
        self.assertEqual(
            matcher_ids,
            {
                "adaface-ir101-local-candidate",
                "elasticface-arc-r100-local-candidate",
                "magface-iresnet100-ms1mv2-local-candidate",
                "topofr-r100-glint360k-local-candidate",
                "transface-b-glint360k-local-candidate",
                "transface-s-glint360k-local-candidate",
            },
        )
        datasets = {item["addon_id"]: item for item in catalog["datasets"]}
        dataset = datasets["lfw-funneled-local-candidate"]
        self.assertEqual(dataset["sample_count"], 13_233)
        self.assertEqual(dataset["identity_count"], 5_749)
        self.assertFalse(dataset["managed"])
        self.assertFalse(dataset["acquisition"]["available"])
        self.assertEqual(dataset["acquisition"]["refusal_code"], "ACQUISITION_CONTRACT_MISSING")
        for addon_id, sample_count, pair_count in (
            ("magface-lfw-verification-v1", 12_000, 6_000),
            ("magface-cfp-fp-verification-v1", 14_000, 7_000),
            ("magface-agedb-30-verification-v1", 12_000, 6_000),
        ):
            protocol_dataset = datasets[addon_id]
            self.assertEqual(protocol_dataset["subtype"], "PAIR_VERIFICATION_DATASET")
            self.assertEqual(protocol_dataset["sample_count"], sample_count)
            self.assertEqual(protocol_dataset["pair_count"], pair_count)
            self.assertIsNone(protocol_dataset["identity_count"])
            self.assertEqual(protocol_dataset["runtime_state"], "MISSING")
            self.assertTrue(protocol_dataset["acquisition"]["available"])
            self.assertEqual(protocol_dataset["acquisition"]["archive_sha256"], "d16039c1aa5b801b3a121c1748e3b49ccefe3e5b72f3f918d8a0cd09bb597b96")

        profiles = self.client.get("/api/preprocessing-profiles")
        self.assertEqual(profiles.status_code, 200, profiles.text)
        self.assertEqual(profiles.json(), catalog["preprocessing"])
        self.assertEqual(
            {item["addon_id"] for item in profiles.json()},
            {
                "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
                "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                "AUTHOR_PREALIGNED_ARCFACE112_INMEMORY_V1",
            },
        )
        self.assertTrue(
            all(
                set(item["leaf_ids_by_matcher"]) == matcher_ids
                and item["selection_scope"] == "PER_RUN"
                for item in profiles.json()
            )
        )
        self.assertEqual(
            {item["operator"]: item["parameter"] for item in catalog["transforms"]},
            {
                "jpeg": "quality",
                "gaussian_blur": "sigma",
                "downscale": "factor",
                "gamma": "gamma",
                "contrast": "alpha",
                "rotation": "degrees",
                "occlusion": "fraction",
            },
        )
        self.assertTrue(all(item["selection_scope"] == "PER_DERIVED_SET" for item in catalog["transforms"]))
        transform_contracts = {item["operator"]: item["parameter_contract"] for item in catalog["transforms"]}
        self.assertEqual(transform_contracts["jpeg"]["value_type"], "integer")
        self.assertEqual((transform_contracts["jpeg"]["minimum"], transform_contracts["jpeg"]["maximum"]), (1, 100))
        self.assertTrue(transform_contracts["downscale"]["minimum_exclusive"])
        self.assertTrue(transform_contracts["downscale"]["maximum_exclusive"])
        self.assertEqual(catalog["analysis_parameters"]["target_fmr"]["maximum"], 1)
        self.assertTrue(catalog["analysis_parameters"]["target_fmr"]["maximum_exclusive"])
        self.assertFalse(catalog["analysis_parameters"]["target_fmr"]["allow_inf_nan"])
        self.assertEqual(catalog["analysis_parameters"]["bootstrap_replicates"]["maximum"], 100_000)
        self.assertEqual(catalog["analysis_parameters"]["owner_pair_seed"]["value_type"], "integer")
        self.assertFalse(catalog["analysis_parameters"]["matcher_threshold"]["allow_inf_nan"])
        self.assertFalse(catalog["analysis_parameters"]["quality_threshold"]["allow_inf_nan"])
        forbidden_executable_fields = {"module", "package", "command", "executable", "hook", "entry_point", "url"}
        for item in [*catalog["matchers"], *catalog["datasets"], *catalog["preprocessing"], *catalog["transforms"]]:
            self.assertFalse(forbidden_executable_fields & set(item), item)

    def test_verification_dataset_import_endpoint_refuses_when_real_bytes_are_not_acquired(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={
                "name": "Pair dataset admission boundary",
                "description": "Verify that an unacquired real pair dataset cannot be imported.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/open").status_code, 200)

        response = self.client.post(
            f"/api/projects/{project_id}/dataset-assets/magface-lfw-verification-v1/import"
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("not ready", response.json()["detail"].lower())
        self.assertEqual(self.client.get(f"/api/projects/{project_id}/datasets").json(), [])
        self.assertEqual(self.client.get(f"/api/projects/{project_id}/protocols").json(), [])

    def test_numeric_threshold_contracts_reject_nonfinite_values_at_the_api_boundary(self) -> None:
        registered_sample = self.client.get("/api/samples/registered")
        self.assertEqual(registered_sample.status_code, 200, registered_sample.text)
        sample = registered_sample.json()[0]
        registered_resource_id = sample["resource_id"]

        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            for field in ("matcher_threshold", "quality_threshold", "target_fmr"):
                with self.subTest(model="utility", field=field, value=repr(nonfinite)):
                    with self.assertRaises(ValidationError):
                        UtilityAnalysisRequest(
                            matcher_run_id=registered_resource_id,
                            quality_job_id=registered_resource_id,
                            **{field: nonfinite},
                        )
            with self.subTest(model="degradation", value=repr(nonfinite)):
                with self.assertRaises(ValidationError):
                    DegradationAnalysisRequestBody(
                        derived_set_id=registered_resource_id,
                        quality_job_id=registered_resource_id,
                        matcher_id=MATCHER_ID,
                        condition_id=ConditionId.ADAPTIVE_LARGEST_JPEG95.value,
                        matcher_threshold=nonfinite,
                    )

        project = self.client.post(
            "/api/projects",
            json={
                "name": "Finite matcher threshold boundary",
                "description": "API validation against the published add-on contract.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        opened = self.client.post(f"/api/projects/{project_id}/open")
        self.assertEqual(opened.status_code, 200, opened.text)
        imported = self.client.post(
            f"/api/projects/{project_id}/datasets/import-registered",
            json={"resource_id": "lfw-funneled-local-candidate", "verify_hashes": False},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        dataset_id = imported.json()["dataset_id"]
        protocol = self.client.post(
            f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2"
        )
        self.assertEqual(protocol.status_code, 201, protocol.text)
        external_resource_id = next(
            item["resource_id"]
            for item in self.client.get("/api/resources").json()
            if item["kind"] == "EXTERNAL_SCORE_ARTIFACT"
        )
        external = self.client.post(
            f"/api/projects/{project_id}/protocols/{protocol.json()['protocol_id']}/import-external-scores",
            json={"resource_id": external_resource_id},
        )
        self.assertEqual(external.status_code, 201, external.text)
        matcher_run_id = external.json()["run_id"]
        batch = self.client.post(
            f"/api/projects/{project_id}/datasets/{dataset_id}/quality-batch",
            json={"selection": {"kind": "subset_fixture"}, "chunks": 1, "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(batch.status_code, 202, batch.text)
        quality_job_id = batch.json()["job_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            quality_job = self.client.get(f"/api/jobs/{quality_job_id}").json()
            if quality_job["state"] in {"COMPLETED", "CANCELLED", "FAILED"} and quality_job["ended_at"]:
                break
            time.sleep(0.5)
        else:
            self.fail(f"quality job {quality_job_id} did not finish before the finite-threshold gate timed out")
        self.assertEqual(quality_job["state"], "COMPLETED", quality_job)

        encoded_matcher_run_id = json.dumps(matcher_run_id)
        encoded_quality_job_id = json.dumps(quality_job_id)
        encoded_dataset_id = json.dumps(dataset_id)
        encoded_matcher_id = json.dumps(MATCHER_ID)
        encoded_condition_id = json.dumps(ConditionId.ADAPTIVE_LARGEST_JPEG95.value)
        for token in ("NaN", "1e309", "-1e309"):
            for field in ("matcher_threshold", "quality_threshold", "target_fmr"):
                utility = self.client.post(
                    f"/api/projects/{project_id}/utility-analyses",
                    content=(
                        f'{{"matcher_run_id":{encoded_matcher_run_id},"quality_job_id":{encoded_quality_job_id},'
                        f'"{field}":{token}}}'
                    ),
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(utility.status_code, 422, utility.text)
                self.assertIn(field, utility.text)
            degradation = self.client.post(
                f"/api/projects/{project_id}/degradation-analyses",
                content=(
                    f'{{"derived_set_id":{encoded_dataset_id},"quality_job_id":{encoded_quality_job_id},'
                    f'"matcher_id":{encoded_matcher_id},"condition_id":{encoded_condition_id},'
                    f'"matcher_threshold":{token}}}'
                ),
                headers={"content-type": "application/json"},
            )
            self.assertEqual(degradation.status_code, 422, degradation.text)
            self.assertIn("matcher_threshold", degradation.text)
            errors = self.client.get(
                f"/api/matcher-runs/{matcher_run_id}/errors",
                params={"threshold": token},
            )
            self.assertEqual(errors.status_code, 422, errors.text)
            self.assertIn("threshold", errors.text)
            for score_filter in ("min_score", "max_score"):
                comparisons = self.client.get(
                    f"/api/matcher-runs/{matcher_run_id}/comparisons",
                    params={score_filter: token},
                )
                self.assertEqual(comparisons.status_code, 422, comparisons.text)
                self.assertIn(score_filter, comparisons.text)
            for erc_parameter in ("threshold", "target_fmr"):
                erc = self.client.get(
                    f"/api/matcher-runs/{matcher_run_id}/erc",
                    params={"quality_job_id": quality_job_id, erc_parameter: token},
                )
                self.assertEqual(erc.status_code, 422, erc.text)
                self.assertIn(erc_parameter, erc.text)

    def test_cli_matrix_axes_deduplicate_repeated_catalog_flags_in_order(self) -> None:
        self.assertEqual(
            _selected_axis(
                [MatcherId.ADAFACE_IR101.value, MatcherId.ADAFACE_IR101.value, MatcherId.TRANSFACE_S.value],
                MatcherId,
            ),
            (MatcherId.ADAFACE_IR101, MatcherId.TRANSFACE_S),
        )
        self.assertEqual(
            _selected_axis(
                [ConditionId.ADAPTIVE_LARGEST_JPEG95.value, ConditionId.ADAPTIVE_LARGEST_JPEG95.value],
                ConditionId,
            ),
            (ConditionId.ADAPTIVE_LARGEST_JPEG95,),
        )

    def test_legacy_external_score_matrix_migrates_as_one_expected_leaf(self) -> None:
        migration_dir = self.data_dir / "legacy-external-migration"
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=migration_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
        )
        with TestClient(
            create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False
        ) as writer:
            project = writer.post(
                "/api/projects",
                json={"name": "external migration contract", "description": "Legacy external-score status gate.", "purpose_scope": RESEARCH_SCOPE},
            ).json()
            project_id = project["project_id"]
            self.assertEqual(writer.post(f"/api/projects/{project_id}/open").status_code, 200)
            imported = writer.post(
                f"/api/projects/{project_id}/datasets/import-registered",
                json={"resource_id": "lfw-funneled-local-candidate", "verify_hashes": False},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            protocol = writer.post(
                f"/api/projects/{project_id}/datasets/{imported.json()['dataset_id']}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol.status_code, 201, protocol.text)
            external_resource = next(
                item["resource_id"] for item in writer.get("/api/resources").json() if item["kind"] == "EXTERNAL_SCORE_ARTIFACT"
            )
            external = writer.post(
                f"/api/projects/{project_id}/protocols/{protocol.json()['protocol_id']}/import-external-scores",
                json={"resource_id": external_resource},
            )
            self.assertEqual(external.status_code, 201, external.text)
            matrix_id = external.json()["matrix_id"]

        database_path = migration_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database_path)
        try:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(matcher_leaves)") if row[1] != "expected_leaf_count"]
            projection = ", ".join(f'"{column}"' for column in columns)
            connection.execute(f"CREATE TABLE matcher_leaves_legacy AS SELECT {projection} FROM matcher_leaves")
            connection.execute("DROP TABLE matcher_leaves")
            connection.execute("ALTER TABLE matcher_leaves_legacy RENAME TO matcher_leaves")
            connection.commit()
        finally:
            connection.close()

        with TestClient(
            create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False
        ) as restarted:
            status = restarted.get(f"/api/matrices/{matrix_id}")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["progress"], {"phase": "completed", "detail": "1/1 leaves recorded"})

    def test_matrix_rejects_a_non_cartesian_addon_selection(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={"name": "add-on selection contract", "description": "Closed catalog boundary gate.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        project_id = project["project_id"]
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/open").status_code, 200)
        imported = self.client.post(
            f"/api/projects/{project_id}/datasets/import-registered",
            json={"resource_id": "lfw-funneled-local-candidate", "verify_hashes": True},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        protocol = self.client.post(
            f"/api/projects/{project_id}/datasets/{imported.json()['dataset_id']}/protocols/freeze-lfw-view2"
        )
        self.assertEqual(protocol.status_code, 201, protocol.text)
        response = self.client.post(
            f"/api/projects/{project_id}/protocols/{protocol.json()['protocol_id']}/matrix",
            params={"fixture_pairs_only": "true", "leaves": "QS-C22.AS,QS-C22.EN"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["detail"],
            "matrix leaf selection must name a complete matcher x preprocessing Cartesian product",
        )
        empty = self.client.post(
            f"/api/projects/{project_id}/protocols/{protocol.json()['protocol_id']}/matrix",
            params={"fixture_pairs_only": "true", "leaves": " , "},
        )
        self.assertEqual(empty.status_code, 422, empty.text)
        self.assertEqual(empty.json()["detail"], "matrix leaf selection must name at least one leaf")

    def test_event_persistence_failure_prevents_matcher_state_mutation(self) -> None:
        pulled = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/pull-sources")
        self.assertEqual(pulled.status_code, 200, pulled.text)
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER refuse_system_event BEFORE INSERT ON system_events "
                "WHEN NEW.event_type = 'MATCHER_LOADED' "
                "BEGIN SELECT RAISE(ABORT, 'completion event persistence refused by lifecycle gate'); END"
            )
            connection.commit()
            response = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/load")
            self.assertEqual(response.status_code, 500, response.text)
            status = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[MATCHER_ID]
            self.assertEqual(status["runtime"]["state"], "UNLOADED")
        finally:
            connection.execute("DROP TRIGGER IF EXISTS refuse_system_event")
            connection.commit()
            connection.close()

    def test_completion_event_gate_excludes_a_concurrent_real_matcher_lease(self) -> None:
        pulled = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/pull-sources")
        self.assertEqual(pulled.status_code, 200, pulled.text)

        route = next(
            route
            for route in self.client.app.routes
            if getattr(route, "path", None) == "/api/matcher-assets/{matcher_id}/load"
            and "POST" in getattr(route, "methods", set())
        )
        manager = inspect.getclosurevars(route.endpoint).nonlocals["matcher_manager"]()
        database_path = self.data_dir / "qualsight.sqlite3"
        setup_connection = sqlite3.connect(database_path)
        try:
            setup_connection.execute(
                "CREATE TRIGGER refuse_system_event BEFORE INSERT ON system_events "
                "WHEN NEW.event_type = 'MATCHER_LOADED' "
                "BEGIN SELECT RAISE(ABORT, 'completion event persistence refused by concurrency gate'); END"
            )
            setup_connection.commit()
        finally:
            setup_connection.close()

        request_finished = threading.Event()
        lease_entered = threading.Event()
        release_lease = threading.Event()
        response_box: list[object] = []
        lease_errors: list[BaseException] = []

        def request_load() -> None:
            try:
                response_box.append(self.client.post(f"/api/matcher-assets/{MATCHER_ID}/load"))
            finally:
                request_finished.set()

        def run_scientific_lease() -> None:
            try:
                with manager.lease(MATCHER_ID):
                    lease_entered.set()
                    release_lease.wait(timeout=45)
            except BaseException as error:
                lease_errors.append(error)

        request_thread = threading.Thread(target=request_load, name="lifecycle-load-request")
        lease_thread = threading.Thread(target=run_scientific_lease, name="scientific-matcher-lease")
        blocker = sqlite3.connect(database_path, timeout=5)
        reader = sqlite3.connect(database_path, timeout=5)
        entered_before_completion_release = False
        try:
            request_thread.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                requested = reader.execute(
                    "SELECT 1 FROM system_events WHERE event_type = 'MATCHER_LOAD_REQUESTED' LIMIT 1"
                ).fetchone()
                if requested is not None:
                    break
                time.sleep(0.01)
            else:
                self.fail("the real load request did not persist its intent event")

            blocker.execute("BEGIN IMMEDIATE")
            lease_thread.start()
            entered_before_completion_release = lease_entered.wait(timeout=15)
        finally:
            release_lease.set()
            blocker.rollback()
            blocker.close()
            reader.close()
            request_thread.join(timeout=45)
            lease_thread.join(timeout=45)
            cleanup_connection = sqlite3.connect(database_path)
            try:
                cleanup_connection.execute("DROP TRIGGER IF EXISTS refuse_system_event")
                cleanup_connection.commit()
            finally:
                cleanup_connection.close()
            if not lease_thread.is_alive():
                if manager.status(MATCHER_ID)["runtime"]["state"] == "LOADED":
                    manager.unload(MATCHER_ID)

        self.assertFalse(request_thread.is_alive(), "the lifecycle request did not terminate")
        self.assertFalse(lease_thread.is_alive(), "the scientific matcher lease did not terminate")
        self.assertEqual(lease_errors, [])
        self.assertEqual(len(response_box), 1)
        self.assertEqual(response_box[0].status_code, 500, response_box[0].text)
        self.assertFalse(
            entered_before_completion_release,
            "a scientific matcher lease entered after load mutation but before completion-event failure and rollback",
        )

    def test_unload_completion_event_failure_restores_the_loaded_runtime(self) -> None:
        pulled = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/pull-sources")
        self.assertEqual(pulled.status_code, 200, pulled.text)
        loaded = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/load")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER refuse_system_event BEFORE INSERT ON system_events "
                "WHEN NEW.event_type = 'MATCHER_UNLOADED' "
                "BEGIN SELECT RAISE(ABORT, 'completion event persistence refused by lifecycle gate'); END"
            )
            connection.commit()
            response = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/unload")
            self.assertEqual(response.status_code, 500, response.text)
            status = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[MATCHER_ID]
            self.assertEqual(status["runtime"]["state"], "LOADED")
        finally:
            connection.execute("DROP TRIGGER IF EXISTS refuse_system_event")
            connection.commit()
            connection.close()

    def test_repeated_load_completion_event_failure_preserves_the_preloaded_runtime(self) -> None:
        pulled = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/pull-sources")
        self.assertEqual(pulled.status_code, 200, pulled.text)
        loaded = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/load")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER refuse_system_event BEFORE INSERT ON system_events "
                "WHEN NEW.event_type = 'MATCHER_LOADED' "
                "BEGIN SELECT RAISE(ABORT, 'completion event persistence refused by lifecycle gate'); END"
            )
            connection.commit()
            response = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/load")
            self.assertEqual(response.status_code, 500, response.text)
            status = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[MATCHER_ID]
            self.assertEqual(status["runtime"]["state"], "LOADED")
        finally:
            connection.execute("DROP TRIGGER IF EXISTS refuse_system_event")
            connection.commit()
            connection.close()

    def test_source_pull_completion_event_failure_restores_preexisting_source_state(self) -> None:
        initial = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[MATCHER_ID]
        self.assertEqual([item["state"] for item in initial["architecture_sources"]], ["MISSING"])
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER refuse_system_event BEFORE INSERT ON system_events "
                "WHEN NEW.event_type = 'MATCHER_ARCHITECTURE_SOURCES_PULLED' "
                "BEGIN SELECT RAISE(ABORT, 'completion event persistence refused by lifecycle gate'); END"
            )
            connection.commit()
            response = self.client.post(f"/api/matcher-assets/{MATCHER_ID}/pull-sources")
            self.assertEqual(response.status_code, 500, response.text)
            status = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[MATCHER_ID]
            self.assertEqual([item["state"] for item in status["architecture_sources"]], ["MISSING"])
        finally:
            connection.execute("DROP TRIGGER IF EXISTS refuse_system_event")
            connection.commit()
            connection.close()

    def test_checkpoint_completion_event_failure_removes_new_managed_bytes_and_receipt(self) -> None:
        matcher_id = "elasticface-arc-r100-local-candidate"
        initial = {item["matcher_id"]: item for item in self.client.get("/api/matcher-assets").json()}[matcher_id]
        managed_path = Path(initial["checkpoint_acquisition"]["managed_path"])
        self.assertFalse(managed_path.exists())
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER refuse_system_event BEFORE INSERT ON system_events "
                "WHEN NEW.event_type = 'MATCHER_CHECKPOINT_ACQUISITION_COMPLETED' "
                "BEGIN SELECT RAISE(ABORT, 'completion event persistence refused by lifecycle gate'); END"
            )
            connection.commit()
            response = self.client.post(f"/api/matcher-assets/{matcher_id}/pull-checkpoint")
            self.assertEqual(response.status_code, 500, response.text)
            self.assertFalse(managed_path.exists())
            receipt_root = self.data_dir / "acquisition-receipts" / matcher_id
            self.assertEqual(list(receipt_root.glob("*.json")), [])
        finally:
            connection.execute("DROP TRIGGER IF EXISTS refuse_system_event")
            connection.commit()
            connection.close()

    def test_local_browser_boundary_refuses_cross_site_mutation_and_dns_rebinding(self) -> None:
        cross_site = self.client.post(
            f"/api/matcher-assets/{MATCHER_ID}/load",
            headers={"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(cross_site.status_code, 403, cross_site.text)
        rebound = self.client.get("/api/health", headers={"Host": "attacker.invalid"})
        self.assertEqual(rebound.status_code, 400, rebound.text)
        test_only_host = self.client.get("/api/health", headers={"Host": "testserver"})
        self.assertEqual(test_only_host.status_code, 400, test_only_host.text)
        same_site = self.client.get("/api/health", headers={"Host": "127.0.0.1:8480"})
        self.assertEqual(same_site.status_code, 200, same_site.text)


if __name__ == "__main__":
    unittest.main()
