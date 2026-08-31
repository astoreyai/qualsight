#!/usr/bin/env python3
"""Validate QualSight's declarative core Metric Registry."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_REGISTRY_BYTES = 1_000_000
MAX_METRICS = 256
MAX_LIST_ITEMS = 128
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
METRIC_ID_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
REQUIREMENT_ID_RE = re.compile(r"^QS-REQ-[0-9]{3}[A-Z]?$")
LOCATOR_RE = re.compile(r"^([^:]+):([1-9][0-9]*)(?:-([1-9][0-9]*))?$")
CORE_METRIC_IDS = {
    "biometric.verification.fmr",
    "biometric.verification.fnmr",
    "biometric.verification.roc",
    "biometric.verification.det",
    "biometric.verification.eer",
    "biometric.quality.utility.erc",
}
PHASE3_METRIC_IDS = {
    "biometric.quality.utility.au-erc",
    "biometric.quality.utility.partial-au-erc",
    "biometric.quality.utility.risk-coverage",
    "biometric.quality.utility.quality-conditioned-fnmr",
    "biometric.quality.utility.quality-conditioned-fmr",
    "biometric.quality.utility.quality-error-association",
    "biometric.quality.utility.failure-predictor",
    "biometric.policy.threshold-analysis",
}
IDENTIFICATION_METRIC_IDS = {
    "biometric.identification.fpir",
    "biometric.identification.fnir",
    "biometric.identification.tpir",
    "biometric.identification.rank-k",
    "biometric.identification.cmc",
}
REQUIRED_SOURCE_REQUIREMENTS = {
    "QS-REQ-017B",
    "QS-REQ-015",
    "QS-REQ-016",
    "QS-REQ-018",
    "QS-REQ-020",
    "QS-REQ-021",
    "QS-REQ-024",
    "QS-REQ-028A",
    "QS-REQ-029",
    "QS-REQ-043",
}
AUTHORITY_FILES = {
    "prd-0.3": {
        "path": "PRD.md",
        "version": "0.3",
        "sha256": "cbdd00e9a43c67ae1e803b08cb1729fae23f842a79e3be5e7f18246c3bbf3025",
    },
    "requirements-0.2": {
        "path": "REQUIREMENTS.md",
        "version": "0.2",
        "sha256": "90e34be2362d172c47de7b97f92e2ba41cffe0b2a0e045667569a1b836e91d23",
    },
}
ALLOWED_DOMAINS = {
    "QUALITY",
    "VERIFICATION",
    "IDENTIFICATION",
    "ACQUISITION",
    "PAD",
    "ROBUSTNESS",
    "CALIBRATION",
    "DEMOGRAPHICS",
    "SYSTEM",
}
ALLOWED_TYPES = {"RATE", "CURVE", "OPERATING_POINT", "AREA", "ASSOCIATION", "CLASSIFIER_SUMMARY"}
ALLOWED_DIRECTIONS = {"LOWER", "HIGHER", "NOT_APPLICABLE_TO_CURVE"}
CORE_METADATA = {
    "biometric.verification.fmr": ("VERIFICATION", "RATE", "LOWER"),
    "biometric.verification.fnmr": ("VERIFICATION", "RATE", "LOWER"),
    "biometric.verification.roc": (
        "VERIFICATION",
        "CURVE",
        "NOT_APPLICABLE_TO_CURVE",
    ),
    "biometric.verification.det": (
        "VERIFICATION",
        "CURVE",
        "NOT_APPLICABLE_TO_CURVE",
    ),
    "biometric.verification.eer": ("VERIFICATION", "OPERATING_POINT", "LOWER"),
    "biometric.quality.utility.erc": (
        "QUALITY",
        "CURVE",
        "NOT_APPLICABLE_TO_CURVE",
    ),
}
FORBIDDEN_MARKER_RE = re.compile(
    r"\b(?:TODO|FIXME|TBD)\b|\[date to be inserted\]|\bN records\b|\bX met\b",
    re.IGNORECASE,
)
UNSUPPORTED_CLAIM_RE = re.compile(
    r"\b(?:certified|certification|compliant|compliance|conformant)\b",
    re.IGNORECASE,
)
CORE_CONTRACTS = {
    "biometric.verification.fmr": {
        "canonical_name": "False Match Rate",
        "aliases": ("FMR",),
        "required_inputs": (
            "comparison_ground_truth",
            "matcher_decision",
            "matcher_threshold",
            "score_direction",
            "threshold_equality_rule",
            "processing_failure_policy",
        ),
        "aggregation_unit": "NON_MATED_COMPARISON_TRIAL",
        "threshold_semantics": {
            "threshold_kind": "MATCHER_THRESHOLD",
            "decision_rule_source": "RECORDED_PROTOCOL",
            "score_direction_required": True,
            "equality_rule_required": True,
            "threshold_reestimation": "NOT_APPLICABLE",
        },
        "confidence_interval_methods": (
            "BINOMIAL_WHEN_INDEPENDENCE_IS_JUSTIFIED",
            "SUBJECT_AWARE_BOOTSTRAP",
        ),
        "references": (
            ("prd-0.3", "PRD.md:577-578"),
            ("requirements-0.2", "REQUIREMENTS.md:54-55"),
            ("requirements-0.2", "REQUIREMENTS.md:58"),
            ("requirements-0.2", "REQUIREMENTS.md:64"),
            ("requirements-0.2", "REQUIREMENTS.md:70"),
        ),
        "source_requirements": (
            "QS-REQ-015",
            "QS-REQ-016",
            "QS-REQ-018",
            "QS-REQ-024",
            "QS-REQ-029",
        ),
        "prose_sha256": "dc1e55c6322504d90867133a134d65db4b9bf3d8125ae8faa9de37ab44e9a268",
    },
    "biometric.verification.fnmr": {
        "canonical_name": "False Non-Match Rate",
        "aliases": ("FNMR",),
        "required_inputs": (
            "comparison_ground_truth",
            "matcher_decision",
            "matcher_threshold",
            "score_direction",
            "threshold_equality_rule",
            "processing_failure_policy",
        ),
        "aggregation_unit": "MATED_COMPARISON_TRIAL",
        "threshold_semantics": {
            "threshold_kind": "MATCHER_THRESHOLD",
            "decision_rule_source": "RECORDED_PROTOCOL",
            "score_direction_required": True,
            "equality_rule_required": True,
            "threshold_reestimation": "NOT_APPLICABLE",
        },
        "confidence_interval_methods": (
            "BINOMIAL_WHEN_INDEPENDENCE_IS_JUSTIFIED",
            "SUBJECT_AWARE_BOOTSTRAP",
        ),
        "references": (
            ("prd-0.3", "PRD.md:580-584"),
            ("requirements-0.2", "REQUIREMENTS.md:54-55"),
            ("requirements-0.2", "REQUIREMENTS.md:58"),
            ("requirements-0.2", "REQUIREMENTS.md:64"),
            ("requirements-0.2", "REQUIREMENTS.md:70"),
        ),
        "source_requirements": (
            "QS-REQ-015",
            "QS-REQ-016",
            "QS-REQ-018",
            "QS-REQ-024",
            "QS-REQ-029",
        ),
        "prose_sha256": "d914149c8928c34dc185dd2d985d3772d24fdc2069377d85c066e17af2c7a859",
    },
    "biometric.verification.roc": {
        "canonical_name": "Receiver Operating Characteristic",
        "aliases": ("ROC",),
        "required_inputs": (
            "fmr_by_threshold",
            "fnmr_by_threshold",
            "matcher_threshold_set",
            "score_direction",
            "threshold_equality_rule",
            "processing_failure_policy",
        ),
        "aggregation_unit": "MATCHER_THRESHOLD_OPERATING_POINT",
        "threshold_semantics": {
            "threshold_kind": "MATCHER_THRESHOLD_SET",
            "decision_rule_source": "RECORDED_PROTOCOL",
            "score_direction_required": True,
            "equality_rule_required": True,
            "threshold_reestimation": "NOT_APPLICABLE",
        },
        "confidence_interval_methods": ("SUBJECT_AWARE_BOOTSTRAP",),
        "references": (
            ("prd-0.3", "PRD.md:583-590"),
            ("requirements-0.2", "REQUIREMENTS.md:54-55"),
            ("requirements-0.2", "REQUIREMENTS.md:58"),
            ("requirements-0.2", "REQUIREMENTS.md:64"),
            ("requirements-0.2", "REQUIREMENTS.md:70"),
        ),
        "source_requirements": (
            "QS-REQ-015",
            "QS-REQ-016",
            "QS-REQ-018",
            "QS-REQ-024",
            "QS-REQ-029",
        ),
        "prose_sha256": "7d595c5ac9f2ff9420bf4c2013b7979f3d65847369167ce2aba0b303807d5ced",
    },
    "biometric.verification.det": {
        "canonical_name": "Detection Error Tradeoff",
        "aliases": ("DET",),
        "required_inputs": (
            "fmr_by_threshold",
            "fnmr_by_threshold",
            "matcher_threshold_set",
            "score_direction",
            "threshold_equality_rule",
            "processing_failure_policy",
        ),
        "aggregation_unit": "MATCHER_THRESHOLD_OPERATING_POINT",
        "threshold_semantics": {
            "threshold_kind": "MATCHER_THRESHOLD_SET",
            "decision_rule_source": "RECORDED_PROTOCOL",
            "score_direction_required": True,
            "equality_rule_required": True,
            "threshold_reestimation": "NOT_APPLICABLE",
        },
        "confidence_interval_methods": ("SUBJECT_AWARE_BOOTSTRAP",),
        "references": (
            ("prd-0.3", "PRD.md:589-590"),
            ("requirements-0.2", "REQUIREMENTS.md:54-55"),
            ("requirements-0.2", "REQUIREMENTS.md:58"),
            ("requirements-0.2", "REQUIREMENTS.md:64"),
            ("requirements-0.2", "REQUIREMENTS.md:70"),
        ),
        "source_requirements": (
            "QS-REQ-015",
            "QS-REQ-016",
            "QS-REQ-018",
            "QS-REQ-024",
            "QS-REQ-029",
        ),
        "prose_sha256": "8daabb64eadf06034e1b90a5b97cbf240d190927d0e52674b59ea6036d521c48",
    },
    "biometric.verification.eer": {
        "canonical_name": "Equal Error Rate",
        "aliases": ("EER",),
        "required_inputs": (
            "fmr_by_threshold",
            "fnmr_by_threshold",
            "matcher_threshold_set",
            "eer_estimation_method",
            "score_direction",
            "threshold_equality_rule",
            "processing_failure_policy",
        ),
        "aggregation_unit": "MATCHER_OPERATING_POINT",
        "threshold_semantics": {
            "threshold_kind": "MATCHER_THRESHOLD_SET",
            "decision_rule_source": "RECORDED_PROTOCOL",
            "score_direction_required": True,
            "equality_rule_required": True,
            "threshold_reestimation": "EER_ESTIMATION_METHOD",
        },
        "confidence_interval_methods": ("SUBJECT_AWARE_BOOTSTRAP",),
        "references": (
            ("prd-0.3", "PRD.md:592-596"),
            ("requirements-0.2", "REQUIREMENTS.md:54-55"),
            ("requirements-0.2", "REQUIREMENTS.md:58"),
            ("requirements-0.2", "REQUIREMENTS.md:64"),
            ("requirements-0.2", "REQUIREMENTS.md:70"),
        ),
        "source_requirements": (
            "QS-REQ-015",
            "QS-REQ-016",
            "QS-REQ-018",
            "QS-REQ-024",
            "QS-REQ-029",
        ),
        "prose_sha256": "6c65958c88a26e38b3bc1fbe84d0dd27ca24dc692909e0bb5ccd589e14971133",
    },
    "biometric.quality.utility.erc": {
        "canonical_name": "Error-versus-Reject Curve",
        "aliases": ("ERC", "Error-versus-Discard Curve", "EDC"),
        "required_inputs": (
            "registered_base_error_metric",
            "comparison_ground_truth",
            "matcher_decision",
            "fixed_matcher_threshold",
            "score_direction",
            "threshold_equality_rule",
            "pair_quality_value",
            "pair_quality_policy_id",
            "quality_direction",
            "processing_failure_policy",
            "rejection_domain",
        ),
        "aggregation_unit": "PAIR_QUALITY_COMPARISON_UNIT",
        "threshold_semantics": {
            "threshold_kind": "FIXED_MATCHER_THRESHOLD_AND_SWEPT_QUALITY_CUTOFF",
            "decision_rule_source": "RECORDED_PROTOCOL",
            "score_direction_required": True,
            "equality_rule_required": True,
            "threshold_reestimation": "FORBIDDEN_ACROSS_CURVE",
        },
        "confidence_interval_methods": (
            "SUBJECT_AWARE_BOOTSTRAP",
            "PAIRED_SUBJECT_BOOTSTRAP",
        ),
        "references": (
            ("prd-0.3", "PRD.md:706-725"),
            ("prd-0.3", "PRD.md:739-753"),
            ("prd-0.3", "PRD.md:797-825"),
            ("requirements-0.2", "REQUIREMENTS.md:55"),
            ("requirements-0.2", "REQUIREMENTS.md:58"),
            ("requirements-0.2", "REQUIREMENTS.md:60-61"),
            ("requirements-0.2", "REQUIREMENTS.md:64"),
            ("requirements-0.2", "REQUIREMENTS.md:68"),
            ("requirements-0.2", "REQUIREMENTS.md:70"),
            ("requirements-0.2", "REQUIREMENTS.md:84"),
        ),
        "source_requirements": (
            "QS-REQ-016",
            "QS-REQ-018",
            "QS-REQ-020",
            "QS-REQ-021",
            "QS-REQ-024",
            "QS-REQ-028A",
            "QS-REQ-029",
            "QS-REQ-043",
        ),
        "prose_sha256": "df0fda7279926d145449038321c368a208f15552ec5fa0d1e187d987b387f52a",
    },
}


PHASE3_CONTRACTS = {'biometric.policy.threshold-analysis': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                         'aliases': ('operating policy analysis', 'quality policy impact'),
                                         'canonical_name': 'Threshold and policy analysis',
                                         'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                         'SUBJECT_AWARE_BOOTSTRAP'),
                                         'prose_sha256': 'ba17fba57cb2ddf8f5fec79be3411111a938676a4f8eb9ce53fd48f907f8077f',
                                         'references': (('prd-0.3', 'PRD.md:797-825'),
                                                        ('prd-0.3', 'PRD.md:608-632'),
                                                        ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                        ('requirements-0.2', 'REQUIREMENTS.md:64'),
                                                        ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                        ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                         'required_inputs': ('fixed_matcher_threshold',
                                                             'quality_policy_threshold',
                                                             'pair_quality_policy_id',
                                                             'quality_measure_id',
                                                             'score_direction',
                                                             'threshold_equality_rule',
                                                             'processing_failure_policy'),
                                         'source_requirements': ('QS-REQ-016',
                                                                 'QS-REQ-024',
                                                                 'QS-REQ-029',
                                                                 'QS-REQ-043'),
                                         'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                 'equality_rule_required': True,
                                                                 'score_direction_required': True,
                                                                 'threshold_kind': 'MATCHER_THRESHOLD_AND_QUALITY_POLICY_THRESHOLD_REPORTED_SEPARATELY',
                                                                 'threshold_reestimation': 'FORBIDDEN_DURING_POLICY_ANALYSIS'}},
 'biometric.quality.utility.au-erc': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                      'aliases': ('AU-ERC', 'AUERC'),
                                      'canonical_name': 'Area under the Error-versus-Reject Curve',
                                      'confidence_interval_methods': ('SUBJECT_AWARE_BOOTSTRAP',
                                                                      'PAIRED_SUBJECT_BOOTSTRAP'),
                                      'prose_sha256': '7fbadf3548e6ba5ffc591f8a4f31e956bb90d748b38ab65cfba18f5e3e0f65d6',
                                      'references': (('prd-0.3', 'PRD.md:706-725'),
                                                     ('prd-0.3', 'PRD.md:739-753'),
                                                     ('prd-0.3', 'PRD.md:934-969'),
                                                     ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                     ('requirements-0.2', 'REQUIREMENTS.md:60'),
                                                     ('requirements-0.2', 'REQUIREMENTS.md:61'),
                                                     ('requirements-0.2', 'REQUIREMENTS.md:68'),
                                                     ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                     ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                      'required_inputs': ('registered_erc_curve',
                                                          'integration_domain',
                                                          'integration_method',
                                                          'pair_quality_policy_id',
                                                          'fixed_matcher_threshold',
                                                          'processing_failure_policy'),
                                      'source_requirements': ('QS-REQ-016',
                                                              'QS-REQ-020',
                                                              'QS-REQ-021',
                                                              'QS-REQ-028A',
                                                              'QS-REQ-029',
                                                              'QS-REQ-043'),
                                      'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                              'equality_rule_required': True,
                                                              'score_direction_required': True,
                                                              'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                              'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}},
 'biometric.quality.utility.failure-predictor': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                                 'aliases': ('failure prediction',
                                                             'quality failure predictor'),
                                                 'canonical_name': 'Quality as failure predictor',
                                                 'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                                 'SUBJECT_AWARE_BOOTSTRAP'),
                                                 'prose_sha256': '124f3b5b9793f51ad374b5b1657682a5a4fdd35b7e62407132f52c09c3ada9fb',
                                                 'references': (('prd-0.3', 'PRD.md:706-725'),
                                                                ('prd-0.3', 'PRD.md:608-632'),
                                                                ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                                ('requirements-0.2', 'REQUIREMENTS.md:60'),
                                                                ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                                ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                                 'required_inputs': ('pair_quality_value',
                                                                     'outcome_indicator',
                                                                     'fixed_matcher_threshold',
                                                                     'quality_policy_threshold',
                                                                     'pair_quality_policy_id',
                                                                     'processing_failure_policy'),
                                                 'source_requirements': ('QS-REQ-016',
                                                                         'QS-REQ-020',
                                                                         'QS-REQ-029',
                                                                         'QS-REQ-043'),
                                                 'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                         'equality_rule_required': True,
                                                                         'score_direction_required': True,
                                                                         'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                                         'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}},
 'biometric.quality.utility.partial-au-erc': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                              'aliases': ('pAU-ERC', 'partial AU-ERC'),
                                              'canonical_name': 'Partial area under the Error-versus-Reject '
                                                                'Curve',
                                              'confidence_interval_methods': ('SUBJECT_AWARE_BOOTSTRAP',
                                                                              'PAIRED_SUBJECT_BOOTSTRAP'),
                                              'prose_sha256': '0081dd2243216d26d4109247eaac12b2a1f5dbc29f9a1aa4343e31e7edd8f9fb',
                                              'references': (('prd-0.3', 'PRD.md:706-725'),
                                                             ('prd-0.3', 'PRD.md:739-753'),
                                                             ('prd-0.3', 'PRD.md:934-969'),
                                                             ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                             ('requirements-0.2', 'REQUIREMENTS.md:60'),
                                                             ('requirements-0.2', 'REQUIREMENTS.md:61'),
                                                             ('requirements-0.2', 'REQUIREMENTS.md:68'),
                                                             ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                             ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                              'required_inputs': ('registered_erc_curve',
                                                                  'integration_domain',
                                                                  'integration_method',
                                                                  'pair_quality_policy_id',
                                                                  'fixed_matcher_threshold',
                                                                  'processing_failure_policy'),
                                              'source_requirements': ('QS-REQ-016',
                                                                      'QS-REQ-020',
                                                                      'QS-REQ-021',
                                                                      'QS-REQ-028A',
                                                                      'QS-REQ-029',
                                                                      'QS-REQ-043'),
                                              'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                      'equality_rule_required': True,
                                                                      'score_direction_required': True,
                                                                      'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                                      'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}},
 'biometric.quality.utility.quality-conditioned-fmr': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                                       'aliases': ('FMR | quality bin', 'conditional FMR'),
                                                       'canonical_name': 'Quality-conditioned false match '
                                                                         'rate',
                                                       'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                                       'SUBJECT_AWARE_BOOTSTRAP'),
                                                       'prose_sha256': '3563aee94e661c1f4e6b67e61980798a4523fa1caa404eba75c7dcc837b91081',
                                                       'references': (('prd-0.3', 'PRD.md:706-725'),
                                                                      ('prd-0.3', 'PRD.md:608-632'),
                                                                      ('requirements-0.2',
                                                                       'REQUIREMENTS.md:55'),
                                                                      ('requirements-0.2',
                                                                       'REQUIREMENTS.md:60'),
                                                                      ('requirements-0.2',
                                                                       'REQUIREMENTS.md:70'),
                                                                      ('requirements-0.2',
                                                                       'REQUIREMENTS.md:84')),
                                                       'required_inputs': ('registered_base_error_metric',
                                                                           'fixed_matcher_threshold',
                                                                           'pair_quality_policy_id',
                                                                           'bin_edges',
                                                                           'processing_failure_policy'),
                                                       'source_requirements': ('QS-REQ-016',
                                                                               'QS-REQ-020',
                                                                               'QS-REQ-029',
                                                                               'QS-REQ-043'),
                                                       'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                               'equality_rule_required': True,
                                                                               'score_direction_required': True,
                                                                               'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                                               'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}},
 'biometric.quality.utility.quality-conditioned-fnmr': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                                        'aliases': ('FNMR | quality bin', 'conditional FNMR'),
                                                        'canonical_name': 'Quality-conditioned false '
                                                                          'non-match rate',
                                                        'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                                        'SUBJECT_AWARE_BOOTSTRAP'),
                                                        'prose_sha256': '1fa77aee51c2bec9bff624a2eef1f24f7f4b00b199944e3a155a24a5286cd7f8',
                                                        'references': (('prd-0.3', 'PRD.md:706-725'),
                                                                       ('prd-0.3', 'PRD.md:608-632'),
                                                                       ('requirements-0.2',
                                                                        'REQUIREMENTS.md:55'),
                                                                       ('requirements-0.2',
                                                                        'REQUIREMENTS.md:60'),
                                                                       ('requirements-0.2',
                                                                        'REQUIREMENTS.md:70'),
                                                                       ('requirements-0.2',
                                                                        'REQUIREMENTS.md:84')),
                                                        'required_inputs': ('registered_base_error_metric',
                                                                            'fixed_matcher_threshold',
                                                                            'pair_quality_policy_id',
                                                                            'bin_edges',
                                                                            'processing_failure_policy'),
                                                        'source_requirements': ('QS-REQ-016',
                                                                                'QS-REQ-020',
                                                                                'QS-REQ-029',
                                                                                'QS-REQ-043'),
                                                        'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                                'equality_rule_required': True,
                                                                                'score_direction_required': True,
                                                                                'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                                                'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}},
 'biometric.quality.utility.quality-error-association': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                                         'aliases': ('quality-error correlation',
                                                                     'quality-utility association'),
                                                         'canonical_name': 'Quality-error association',
                                                         'confidence_interval_methods': ('FISHER_Z_LARGE_SAMPLE',
                                                                                         'WALD_NORMAL',
                                                                                         'SUBJECT_AWARE_BOOTSTRAP'),
                                                         'prose_sha256': '89b081b6b57de14e4c5caac3ca8fc1ccb0484d8b6619dd0ecd7eb3aee5ad0720',
                                                         'references': (('prd-0.3', 'PRD.md:706-725'),
                                                                        ('prd-0.3', 'PRD.md:934-969'),
                                                                        ('requirements-0.2',
                                                                         'REQUIREMENTS.md:60'),
                                                                        ('requirements-0.2',
                                                                         'REQUIREMENTS.md:68'),
                                                                        ('requirements-0.2',
                                                                         'REQUIREMENTS.md:70'),
                                                                        ('requirements-0.2',
                                                                         'REQUIREMENTS.md:84')),
                                                         'required_inputs': ('pair_quality_value',
                                                                             'comparison_score',
                                                                             'error_indicator',
                                                                             'fixed_matcher_threshold',
                                                                             'pair_quality_policy_id',
                                                                             'processing_failure_policy',
                                                                             'model_support_rule'),
                                                         'source_requirements': ('QS-REQ-020',
                                                                                 'QS-REQ-028A',
                                                                                 'QS-REQ-029',
                                                                                 'QS-REQ-043'),
                                                         'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                                 'equality_rule_required': True,
                                                                                 'score_direction_required': True,
                                                                                 'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                                                 'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}},
 'biometric.quality.utility.risk-coverage': {'aggregation_unit': 'PAIR_QUALITY_COMPARISON_UNIT',
                                             'aliases': ('risk-coverage', 'RC curve'),
                                             'canonical_name': 'Risk-Coverage Curve',
                                             'confidence_interval_methods': ('SUBJECT_AWARE_BOOTSTRAP',
                                                                             'PAIRED_SUBJECT_BOOTSTRAP'),
                                             'prose_sha256': 'e96a02dc1c951bb52e558ec1111efdfc04441b949722dcffcd2e2df7ba6949e6',
                                             'references': (('prd-0.3', 'PRD.md:706-725'),
                                                            ('prd-0.3', 'PRD.md:739-753'),
                                                            ('requirements-0.2', 'REQUIREMENTS.md:60'),
                                                            ('requirements-0.2', 'REQUIREMENTS.md:61'),
                                                            ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                            ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                             'required_inputs': ('registered_erc_curve',
                                                                 'pair_quality_policy_id',
                                                                 'fixed_matcher_threshold',
                                                                 'processing_failure_policy'),
                                             'source_requirements': ('QS-REQ-020',
                                                                     'QS-REQ-021',
                                                                     'QS-REQ-029',
                                                                     'QS-REQ-043'),
                                             'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                                     'equality_rule_required': True,
                                                                     'score_direction_required': True,
                                                                     'threshold_kind': 'FIXED_MATCHER_THRESHOLD_AND_RECORDED_QUALITY_CUTOFF',
                                                                     'threshold_reestimation': 'FORBIDDEN_ACROSS_ANALYSIS'}}}


IDENTIFICATION_CONTRACTS = {'biometric.identification.cmc': {'aggregation_unit': 'IDENTIFICATION_SEARCH',
                                  'aliases': ('CMC', 'CMC curve'),
                                  'canonical_name': 'Cumulative match characteristic',
                                  'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                  'SUBJECT_AWARE_BOOTSTRAP'),
                                  'prose_sha256': '48933d6e2b61ff9fc8e3bfd27333185e0a3e16dc338ca5eff3bd5b030f269589',
                                  'references': (('prd-0.3', 'PRD.md:634-661'),
                                                 ('requirements-0.2', 'REQUIREMENTS.md:57'),
                                                 ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                 ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                  'required_inputs': ('identification_protocol',
                                                      'search_mode',
                                                      'candidate_list_length',
                                                      'score_direction',
                                                      'threshold_equality_rule',
                                                      'processing_failure_policy',
                                                      'true_mate_identity'),
                                  'source_requirements': ('QS-REQ-017B', 'QS-REQ-029', 'QS-REQ-043'),
                                  'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                          'equality_rule_required': True,
                                                          'score_direction_required': True,
                                                          'threshold_kind': 'MATCHER_THRESHOLD_ON_CANDIDATE_SCORES',
                                                          'threshold_reestimation': 'FORBIDDEN_ACROSS_OPERATING_POINTS'}},
 'biometric.identification.fnir': {'aggregation_unit': 'IDENTIFICATION_SEARCH',
                                   'aliases': ('FNIR',),
                                   'canonical_name': 'False negative identification rate',
                                   'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                   'SUBJECT_AWARE_BOOTSTRAP'),
                                   'prose_sha256': '7e48e6199ed7f59e6748ee4e9e92a0d2164f23579047ddea8ecc94b5fcf4d2e8',
                                   'references': (('prd-0.3', 'PRD.md:634-661'),
                                                  ('prd-0.3', 'PRD.md:608-632'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:57'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                   'required_inputs': ('identification_protocol',
                                                       'search_mode',
                                                       'candidate_list_length',
                                                       'score_direction',
                                                       'threshold_equality_rule',
                                                       'processing_failure_policy',
                                                       'fixed_matcher_threshold',
                                                       'true_mate_identity'),
                                   'source_requirements': ('QS-REQ-016',
                                                           'QS-REQ-017B',
                                                           'QS-REQ-029',
                                                           'QS-REQ-043'),
                                   'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                           'equality_rule_required': True,
                                                           'score_direction_required': True,
                                                           'threshold_kind': 'MATCHER_THRESHOLD_ON_CANDIDATE_SCORES',
                                                           'threshold_reestimation': 'FORBIDDEN_ACROSS_OPERATING_POINTS'}},
 'biometric.identification.fpir': {'aggregation_unit': 'IDENTIFICATION_SEARCH',
                                   'aliases': ('FPIR',),
                                   'canonical_name': 'False positive identification rate',
                                   'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                   'SUBJECT_AWARE_BOOTSTRAP'),
                                   'prose_sha256': '14c98931e45a88c20ff09bd97f858246c59aaffb17684c1c13339ae97bf74608',
                                   'references': (('prd-0.3', 'PRD.md:634-661'),
                                                  ('prd-0.3', 'PRD.md:608-632'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:57'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                   'required_inputs': ('identification_protocol',
                                                       'search_mode',
                                                       'candidate_list_length',
                                                       'score_direction',
                                                       'threshold_equality_rule',
                                                       'processing_failure_policy',
                                                       'fixed_matcher_threshold'),
                                   'source_requirements': ('QS-REQ-016',
                                                           'QS-REQ-017B',
                                                           'QS-REQ-029',
                                                           'QS-REQ-043'),
                                   'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                           'equality_rule_required': True,
                                                           'score_direction_required': True,
                                                           'threshold_kind': 'MATCHER_THRESHOLD_ON_CANDIDATE_SCORES',
                                                           'threshold_reestimation': 'FORBIDDEN_ACROSS_OPERATING_POINTS'}},
 'biometric.identification.rank-k': {'aggregation_unit': 'IDENTIFICATION_SEARCH',
                                     'aliases': ('rank-k', 'rank-1 hit rate'),
                                     'canonical_name': 'Rank-k identification rate',
                                     'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                     'SUBJECT_AWARE_BOOTSTRAP'),
                                     'prose_sha256': 'f2726b408d16eb6384c8e9696c2b5160a5d8c1844e201e2d525b600e00fbf0ba',
                                     'references': (('prd-0.3', 'PRD.md:634-661'),
                                                    ('requirements-0.2', 'REQUIREMENTS.md:55'),
                                                    ('requirements-0.2', 'REQUIREMENTS.md:57'),
                                                    ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                    ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                     'required_inputs': ('identification_protocol',
                                                         'search_mode',
                                                         'candidate_list_length',
                                                         'score_direction',
                                                         'threshold_equality_rule',
                                                         'processing_failure_policy',
                                                         'true_mate_identity',
                                                         'rank_values'),
                                     'source_requirements': ('QS-REQ-016',
                                                             'QS-REQ-017B',
                                                             'QS-REQ-029',
                                                             'QS-REQ-043'),
                                     'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                             'equality_rule_required': True,
                                                             'score_direction_required': True,
                                                             'threshold_kind': 'MATCHER_THRESHOLD_ON_CANDIDATE_SCORES',
                                                             'threshold_reestimation': 'FORBIDDEN_ACROSS_OPERATING_POINTS'}},
 'biometric.identification.tpir': {'aggregation_unit': 'IDENTIFICATION_SEARCH',
                                   'aliases': ('TPIR',),
                                   'canonical_name': 'True positive identification rate',
                                   'confidence_interval_methods': ('CLOPPER_PEARSON_EXACT_BINOMIAL',
                                                                   'SUBJECT_AWARE_BOOTSTRAP'),
                                   'prose_sha256': '2ec59f75904f93a54a4e8da6456089d99543af1b71b1ea8dfe2af42effcc0432',
                                   'references': (('prd-0.3', 'PRD.md:634-661'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:57'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:70'),
                                                  ('requirements-0.2', 'REQUIREMENTS.md:84')),
                                   'required_inputs': ('identification_protocol',
                                                       'search_mode',
                                                       'candidate_list_length',
                                                       'score_direction',
                                                       'threshold_equality_rule',
                                                       'processing_failure_policy',
                                                       'fixed_matcher_threshold',
                                                       'true_mate_identity'),
                                   'source_requirements': ('QS-REQ-017B', 'QS-REQ-029', 'QS-REQ-043'),
                                   'threshold_semantics': {'decision_rule_source': 'RECORDED_PROTOCOL',
                                                           'equality_rule_required': True,
                                                           'score_direction_required': True,
                                                           'threshold_kind': 'MATCHER_THRESHOLD_ON_CANDIDATE_SCORES',
                                                           'threshold_reestimation': 'FORBIDDEN_ACROSS_OPERATING_POINTS'}}}


class MetricRegistryValidationError(ValueError):
    """Raised when a registry violates the frozen declarative contract."""


def _fail(location: str, message: str) -> None:
    raise MetricRegistryValidationError(f"{location}: {message}")


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    return value


def _sequence(value: Any, location: str, maximum: int = MAX_LIST_ITEMS) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    if not value or len(value) > maximum:
        _fail(location, f"must contain between 1 and {maximum} items")
    return value


def _string(value: Any, location: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        _fail(location, "must be a string")
    if not value or value != value.strip():
        _fail(location, "must be nonempty and have no surrounding whitespace")
    if len(value) > maximum:
        _fail(location, f"exceeds {maximum} characters")
    return value


def _strings(value: Any, location: str) -> list[str]:
    items = _sequence(value, location)
    result = [_string(item, f"{location}[{index}]") for index, item in enumerate(items)]
    if len(result) != len(set(result)):
        _fail(location, "must not contain duplicates")
    return result


def _boolean(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        _fail(location, "must be a boolean")
    return value


def _strict_keys(
    value: dict[str, Any], location: str, required: set[str], allowed: set[str] | None = None
) -> None:
    accepted = required if allowed is None else allowed
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - accepted)
    if missing:
        _fail(location, f"missing keys: {', '.join(missing)}")
    if unknown:
        _fail(location, f"unknown keys: {', '.join(unknown)}")


def _semver(value: Any, location: str) -> str:
    version = _string(value, location, 32)
    if not SEMVER_RE.fullmatch(version):
        _fail(location, "must be a semantic version")
    return version


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MetricRegistryValidationError(f"JSON: duplicate object key {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        _fail("registry", "must be a regular file")
    if resolved.stat().st_size > MAX_REGISTRY_BYTES:
        _fail("registry", f"exceeds {MAX_REGISTRY_BYTES} bytes")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MetricRegistryValidationError(f"JSON: {error}") from error
    return _mapping(value, "registry")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_text_policy(value: Any, location: str) -> None:
    if isinstance(value, str):
        if FORBIDDEN_MARKER_RE.search(value):
            _fail(location, "contains an unresolved marker")
        if UNSUPPORTED_CLAIM_RE.search(value):
            _fail(location, "contains an unsupported certification or compliance claim")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_text_policy(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_text_policy(item, f"{location}.{key}")


def _requirement_row_lines() -> dict[str, int]:
    rows: dict[str, int] = {}
    requirement_path = PROJECT_ROOT / "REQUIREMENTS.md"
    for line_number, line in enumerate(
        requirement_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        match = re.match(r"^\| (QS-REQ-[0-9]{3}[A-Z]?) \|", line)
        if match is not None:
            rows[match.group(1)] = line_number
    return rows


def _validate_requirement_reference_coverage(
    requirements: list[str],
    references: list[dict[str, Any]],
    location: str,
) -> None:
    covered_lines: set[int] = set()
    for reference in references:
        if reference["source_id"] != "requirements-0.2":
            continue
        match = LOCATOR_RE.fullmatch(reference["locator"])
        if match is None:
            raise AssertionError("reference shape was validated before coverage")
        _, start_text, end_text = match.groups()
        start = int(start_text)
        end = int(end_text or start_text)
        covered_lines.update(range(start, end + 1))
    row_lines = _requirement_row_lines()
    missing = [
        requirement
        for requirement in requirements
        if row_lines.get(requirement) not in covered_lines
    ]
    if missing:
        _fail(location, f"references do not cover requirement rows: {', '.join(missing)}")


def _validate_authorities(value: Any) -> dict[str, dict[str, Any]]:
    records = _sequence(value, "registry.authority_documents", len(AUTHORITY_FILES))
    if len(records) != len(AUTHORITY_FILES):
        _fail("registry.authority_documents", "must contain the two frozen local authorities")
    authorities: dict[str, dict[str, Any]] = {}
    for index, raw_record in enumerate(records):
        location = f"registry.authority_documents[{index}]"
        record = _mapping(raw_record, location)
        _strict_keys(record, location, {"source_id", "path", "version", "sha256"})
        source_id = _string(record["source_id"], f"{location}.source_id", 128)
        if source_id in authorities:
            _fail(f"{location}.source_id", "must be unique")
        expected = AUTHORITY_FILES.get(source_id)
        if expected is None:
            _fail(f"{location}.source_id", "is not a frozen local authority")
        for field in ("path", "version", "sha256"):
            actual = _string(record[field], f"{location}.{field}", 128)
            if actual != expected[field]:
                _fail(f"{location}.{field}", f"must equal {expected[field]}")
        authority_path = (PROJECT_ROOT / expected["path"]).resolve(strict=True)
        if authority_path.parent != PROJECT_ROOT or not authority_path.is_file():
            _fail(f"{location}.path", "must resolve to the expected repository-root file")
        if _sha256(authority_path) != expected["sha256"]:
            _fail(f"{location}.sha256", "does not match the current authority bytes")
        authorities[source_id] = record
    if set(authorities) != set(AUTHORITY_FILES):
        _fail("registry.authority_documents", "does not contain the exact authority set")
    return authorities


def _validate_requirements(value: Any, location: str) -> list[str]:
    requirements = _strings(value, location)
    requirement_text = (PROJECT_ROOT / "REQUIREMENTS.md").read_text(encoding="utf-8")
    for requirement in requirements:
        if not REQUIREMENT_ID_RE.fullmatch(requirement):
            _fail(location, f"invalid requirement ID {requirement}")
        if f"| {requirement} |" not in requirement_text:
            _fail(location, f"requirement ID {requirement} is absent from REQUIREMENTS.md")
    return requirements


def _validate_references(
    value: Any, location: str, authorities: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    raw_references = _sequence(value, location)
    references: list[dict[str, Any]] = []
    for index, raw_reference in enumerate(raw_references):
        reference_location = f"{location}[{index}]"
        reference = _mapping(raw_reference, reference_location)
        _strict_keys(reference, reference_location, {"source_id", "locator"})
        source_id = _string(reference["source_id"], f"{reference_location}.source_id", 128)
        authority = authorities.get(source_id)
        if authority is None:
            _fail(f"{reference_location}.source_id", "must resolve to a frozen local authority")
        locator = _string(reference["locator"], f"{reference_location}.locator", 256)
        match = LOCATOR_RE.fullmatch(locator)
        if match is None:
            _fail(f"{reference_location}.locator", "must be path:start or path:start-end")
        locator_path, start_text, end_text = match.groups()
        if locator_path != authority["path"]:
            _fail(f"{reference_location}.locator", "path must match its authority document")
        line_count = len((PROJECT_ROOT / locator_path).read_text(encoding="utf-8").splitlines())
        start = int(start_text)
        end = int(end_text or start_text)
        if start > end or end > line_count:
            _fail(f"{reference_location}.locator", "line range is outside the authority document")
        references.append(reference)
    if len({(item["source_id"], item["locator"]) for item in references}) != len(references):
        _fail(location, "must not contain duplicate references")
    return references


def _validate_threshold_semantics(value: Any, location: str) -> dict[str, Any]:
    semantics = _mapping(value, location)
    required = {
        "threshold_kind",
        "decision_rule_source",
        "score_direction_required",
        "equality_rule_required",
        "threshold_reestimation",
    }
    _strict_keys(semantics, location, required)
    _string(semantics["threshold_kind"], f"{location}.threshold_kind", 128)
    if _string(semantics["decision_rule_source"], f"{location}.decision_rule_source", 128) != "RECORDED_PROTOCOL":
        _fail(f"{location}.decision_rule_source", "must equal RECORDED_PROTOCOL")
    _boolean(semantics["score_direction_required"], f"{location}.score_direction_required")
    _boolean(semantics["equality_rule_required"], f"{location}.equality_rule_required")
    _string(semantics["threshold_reestimation"], f"{location}.threshold_reestimation", 128)
    return semantics


def _validate_formula(value: Any, metric_id: str, location: str) -> dict[str, Any]:
    formula = _mapping(value, location)
    kind = _string(formula.get("kind"), f"{location}.kind", 64)
    if kind == "RATE":
        keys = {
            "kind",
            "formula_version",
            "numerator_population",
            "numerator_event",
            "denominator_population",
            "denominator_policy",
        }
        _strict_keys(formula, location, keys)
        expected = {
            "biometric.verification.fmr": (
                "NON_MATED",
                "ACCEPT",
                "INCLUDED_NON_MATED_TRIALS",
            ),
            "biometric.verification.fnmr": ("MATED", "REJECT", "INCLUDED_MATED_TRIALS"),
        }.get(metric_id)
        if expected is None:
            _fail(location, "RATE is not valid for this core metric")
        actual = tuple(
            _string(formula[field], f"{location}.{field}", 128)
            for field in ("numerator_population", "numerator_event", "denominator_population")
        )
        if actual != expected:
            _fail(location, "rate populations and event do not match the metric definition")
        if _string(formula["denominator_policy"], f"{location}.denominator_policy", 128) != "RECORDED_FAILURE_POLICY":
            _fail(f"{location}.denominator_policy", "must equal RECORDED_FAILURE_POLICY")
    elif kind == "THRESHOLD_CURVE":
        keys = {
            "kind",
            "formula_version",
            "x_metric_id",
            "x_definition_version",
            "y_operation",
            "y_metric_id",
            "y_definition_version",
            "threshold_sweep",
        }
        _strict_keys(formula, location, keys)
        if metric_id not in {"biometric.verification.roc", "biometric.verification.det"}:
            _fail(location, "THRESHOLD_CURVE is not valid for this core metric")
        if formula["x_metric_id"] != "biometric.verification.fmr" or formula["y_metric_id"] != "biometric.verification.fnmr":
            _fail(location, "threshold curves must reference core FMR and FNMR")
        expected_operation = "ONE_MINUS" if metric_id.endswith(".roc") else "IDENTITY"
        if formula["y_operation"] != expected_operation:
            _fail(f"{location}.y_operation", f"must equal {expected_operation}")
        if formula["threshold_sweep"] != "RECORDED_MATCHER_THRESHOLD_SET":
            _fail(f"{location}.threshold_sweep", "must use the recorded matcher threshold set")
        _semver(formula["x_definition_version"], f"{location}.x_definition_version")
        _semver(formula["y_definition_version"], f"{location}.y_definition_version")
    elif kind == "EQUALITY_OPERATING_POINT":
        keys = {
            "kind",
            "formula_version",
            "left_metric_id",
            "left_definition_version",
            "relation",
            "right_metric_id",
            "right_definition_version",
            "empirical_estimator",
        }
        _strict_keys(formula, location, keys)
        if metric_id != "biometric.verification.eer":
            _fail(location, "EQUALITY_OPERATING_POINT is not valid for this core metric")
        expected = {
            "left_metric_id": "biometric.verification.fmr",
            "relation": "EQUALS",
            "right_metric_id": "biometric.verification.fnmr",
            "empirical_estimator": "REQUIRES_RECORDED_VERSIONED_METHOD",
        }
        for field, expected_value in expected.items():
            if formula[field] != expected_value:
                _fail(f"{location}.{field}", f"must equal {expected_value}")
        _semver(formula["left_definition_version"], f"{location}.left_definition_version")
        _semver(formula["right_definition_version"], f"{location}.right_definition_version")
    elif kind == "REJECTION_CURVE":
        keys = {
            "kind",
            "formula_version",
            "x_quantity",
            "y_quantity",
            "rejection_order",
            "tie_policy",
            "matcher_threshold_policy",
            "base_error_metric",
        }
        _strict_keys(formula, location, keys)
        if metric_id != "biometric.quality.utility.erc":
            _fail(location, "REJECTION_CURVE is not valid for this core metric")
        expected = {
            "x_quantity": "REJECTED_FRACTION",
            "y_quantity": "RETAINED_BASE_ERROR_RATE",
            "rejection_order": "WORST_PAIR_QUALITY_FIRST",
            "tie_policy": "REJECT_EQUAL_QUALITY_TOGETHER",
            "matcher_threshold_policy": "FIXED_ACROSS_CURVE",
            "base_error_metric": "REQUIRED_REGISTERED_INPUT",
        }
        for field, expected_value in expected.items():
            if formula[field] != expected_value:
                _fail(f"{location}.{field}", f"must equal {expected_value}")
    elif kind == "CURVE_AREA":
        _strict_keys(formula, location, {"kind", "formula_version", "curve_metric_id", "curve_definition_version", "integration_domain", "integration_method"})
        if metric_id not in {"biometric.quality.utility.au-erc", "biometric.quality.utility.partial-au-erc"}:
            _fail(location, "CURVE_AREA is not valid for this metric")
        if formula["curve_metric_id"] != "biometric.quality.utility.erc":
            _fail(f"{location}.curve_metric_id", "areas must integrate the registered ERC")
        expected_domain = "RECORDED_REJECTION_INTERVAL" if metric_id.endswith(".au-erc") else "RECORDED_PARTIAL_REJECTION_INTERVAL"
        if formula["integration_domain"] != expected_domain:
            _fail(f"{location}.integration_domain", f"must equal {expected_domain}")
        if formula["integration_method"] != "TRAPEZOID_OVER_RECORDED_STEP_POINTS":
            _fail(f"{location}.integration_method", "must equal TRAPEZOID_OVER_RECORDED_STEP_POINTS")
        _semver(formula["curve_definition_version"], f"{location}.curve_definition_version")
    elif kind == "COVERAGE_CURVE":
        _strict_keys(formula, location, {"kind", "formula_version", "source_curve_metric_id", "source_curve_definition_version", "x_quantity", "y_quantity"})
        if metric_id != "biometric.quality.utility.risk-coverage":
            _fail(location, "COVERAGE_CURVE is not valid for this metric")
        if formula["source_curve_metric_id"] != "biometric.quality.utility.erc" or formula["x_quantity"] != "RETAINED_COVERAGE_FRACTION" or formula["y_quantity"] != "RETAINED_BASE_ERROR_RATE":
            _fail(location, "risk-coverage must re-parameterize the registered ERC by retained coverage")
        _semver(formula["source_curve_definition_version"], f"{location}.source_curve_definition_version")
    elif kind == "CONDITIONAL_RATE":
        _strict_keys(formula, location, {"kind", "formula_version", "base_metric_id", "base_definition_version", "conditioning_variable", "bin_edges"})
        expected_base = {"biometric.quality.utility.quality-conditioned-fnmr": "biometric.verification.fnmr", "biometric.quality.utility.quality-conditioned-fmr": "biometric.verification.fmr"}.get(metric_id)
        if expected_base is None:
            _fail(location, "CONDITIONAL_RATE is not valid for this metric")
        if formula["base_metric_id"] != expected_base or formula["conditioning_variable"] != "PAIR_QUALITY_BIN" or formula["bin_edges"] != "RECORDED_WITH_RESULT":
            _fail(location, "conditional rate must condition the core rate on recorded pair-quality bins")
        _semver(formula["base_definition_version"], f"{location}.base_definition_version")
    elif kind == "ASSOCIATION_STATISTICS":
        _strict_keys(formula, location, {"kind", "formula_version", "predictor", "outcome", "statistics"})
        if metric_id != "biometric.quality.utility.quality-error-association":
            _fail(location, "ASSOCIATION_STATISTICS is not valid for this metric")
        if _strings(formula["statistics"], f"{location}.statistics") != ["SPEARMAN_RANK_CORRELATION", "KENDALL_TAU_B", "LOGISTIC_REGRESSION_COEFFICIENT", "ODDS_RATIO"]:
            _fail(f"{location}.statistics", "association statistics set is frozen")
    elif kind == "BINARY_PREDICTOR_SUMMARY":
        _strict_keys(formula, location, {"kind", "formula_version", "predictor", "outcome", "statistics", "cutoff_source"})
        if metric_id != "biometric.quality.utility.failure-predictor":
            _fail(location, "BINARY_PREDICTOR_SUMMARY is not valid for this metric")
        if _strings(formula["statistics"], f"{location}.statistics") != ["ROC_AUC", "PR_AUC_AVERAGE_PRECISION", "SENSITIVITY", "SPECIFICITY", "PRECISION", "RECALL"]:
            _fail(f"{location}.statistics", "predictor statistics set is frozen")
        if formula["cutoff_source"] != "RECORDED_QUALITY_POLICY_THRESHOLD":
            _fail(f"{location}.cutoff_source", "must equal RECORDED_QUALITY_POLICY_THRESHOLD")
    elif kind == "IDENTIFICATION_RATE":
        _strict_keys(formula, location, {"kind", "formula_version", "numerator_population", "numerator_event", "denominator_population", "denominator_policy"})
        expected = {"biometric.identification.fpir": ("NON_MATED_SEARCHES", "ANY_CANDIDATE_AT_OR_ABOVE_THRESHOLD", "INCLUDED_NON_MATED_SEARCHES"),
                    "biometric.identification.fnir": ("MATED_SEARCHES", "TRUE_MATE_NOT_RETURNED_AT_OR_ABOVE_THRESHOLD_WITHIN_LIST", "INCLUDED_MATED_SEARCHES"),
                    "biometric.identification.tpir": ("MATED_SEARCHES", "TRUE_MATE_RETURNED_AT_OR_ABOVE_THRESHOLD_WITHIN_LIST", "INCLUDED_MATED_SEARCHES")}.get(metric_id)
        if expected is None:
            _fail(location, "IDENTIFICATION_RATE is not valid for this metric")
        if (formula["numerator_population"], formula["numerator_event"], formula["denominator_population"]) != expected or formula["denominator_policy"] != "RECORDED_FAILURE_POLICY":
            _fail(location, "identification rate populations and event do not match the metric definition")
    elif kind == "RANK_RATE":
        _strict_keys(formula, location, {"kind", "formula_version", "numerator_population", "numerator_event", "denominator_population", "denominator_policy", "rank_values"})
        if metric_id != "biometric.identification.rank-k" or formula["numerator_event"] != "TRUE_MATE_RANK_LE_K" or formula["rank_values"] != "RECORDED_WITH_RESULT":
            _fail(location, "RANK_RATE must be the closed-set rank-k definition with recorded rank values")
    elif kind == "RANK_CURVE":
        _strict_keys(formula, location, {"kind", "formula_version", "x_quantity", "y_metric_id", "y_definition_version", "rank_sweep"})
        if metric_id != "biometric.identification.cmc" or formula["y_metric_id"] != "biometric.identification.rank-k" or formula["x_quantity"] != "RANK":
            _fail(location, "RANK_CURVE must sweep rank over the registered rank-k definition")
        _semver(formula["y_definition_version"], f"{location}.y_definition_version")
    elif kind == "POLICY_OPERATING_POINT":
        _strict_keys(formula, location, {"kind", "formula_version", "matcher_threshold_kind", "quality_threshold_kind", "reported_quantities", "matcher_threshold_reestimation"})
        if metric_id != "biometric.policy.threshold-analysis":
            _fail(location, "POLICY_OPERATING_POINT is not valid for this metric")
        if formula["matcher_threshold_kind"] != "MATCHER_THRESHOLD" or formula["quality_threshold_kind"] != "QUALITY_POLICY_THRESHOLD" or formula["matcher_threshold_reestimation"] != "FORBIDDEN":
            _fail(location, "policy operating point must keep matcher and quality-policy thresholds distinct and fixed")
        if _strings(formula["reported_quantities"], f"{location}.reported_quantities") != ["RETENTION_RATE", "REJECTION_RATE", "FMR", "FNMR", "NUMBER_REJECTED", "ERRORS_PREVENTED", "SUCCESSFUL_COMPARISONS_REMOVED", "PER_QUALITY_MEASURE_IMPACT"]:
            _fail(f"{location}.reported_quantities", "reported quantity set is frozen")
    else:
        _fail(f"{location}.kind", f"unsupported formula kind {kind}")
    _semver(formula["formula_version"], f"{location}.formula_version")
    return formula


def _validate_metric(
    value: Any,
    index: int,
    authorities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    location = f"registry.metrics[{index}]"
    metric = _mapping(value, location)
    required = {
        "metric_id",
        "definition_version",
        "canonical_name",
        "aliases",
        "domain",
        "metric_type",
        "definition",
        "formula",
        "higher_or_lower_is_better",
        "required_inputs",
        "aggregation_unit",
        "threshold_semantics",
        "confidence_interval_methods",
        "source_standard",
        "source_version",
        "references",
        "source_requirements",
        "implementation",
        "implementation_version",
        "limitations",
        "warnings",
    }
    _strict_keys(metric, location, required)
    metric_id = _string(metric["metric_id"], f"{location}.metric_id", 128)
    if not METRIC_ID_RE.fullmatch(metric_id):
        _fail(f"{location}.metric_id", "must be a lowercase dotted identifier")
    _semver(metric["definition_version"], f"{location}.definition_version")
    canonical_name = _string(
        metric["canonical_name"], f"{location}.canonical_name", 128
    )
    aliases = _strings(metric["aliases"], f"{location}.aliases")
    domain = _string(metric["domain"], f"{location}.domain", 64)
    if domain not in ALLOWED_DOMAINS:
        _fail(f"{location}.domain", f"unsupported domain {domain}")
    metric_type = _string(metric["metric_type"], f"{location}.metric_type", 64)
    if metric_type not in ALLOWED_TYPES:
        _fail(f"{location}.metric_type", f"unsupported metric type {metric_type}")
    _string(metric["definition"], f"{location}.definition")
    formula = _validate_formula(metric["formula"], metric_id, f"{location}.formula")
    direction = _string(metric["higher_or_lower_is_better"], f"{location}.higher_or_lower_is_better", 64)
    if direction not in ALLOWED_DIRECTIONS:
        _fail(f"{location}.higher_or_lower_is_better", f"unsupported direction {direction}")
    if metric_id in CORE_METADATA and (domain, metric_type, direction) != CORE_METADATA[metric_id]:
        _fail(location, "domain, metric type, or direction does not match the core definition")
    required_inputs = _strings(metric["required_inputs"], f"{location}.required_inputs")
    aggregation_unit = _string(
        metric["aggregation_unit"], f"{location}.aggregation_unit", 128
    )
    semantics = _validate_threshold_semantics(metric["threshold_semantics"], f"{location}.threshold_semantics")
    confidence_methods = _strings(
        metric["confidence_interval_methods"],
        f"{location}.confidence_interval_methods",
    )
    if _string(metric["source_standard"], f"{location}.source_standard", 128) != "QUALSIGHT_PRD":
        _fail(f"{location}.source_standard", "must identify the local QualSight PRD authority")
    if _string(metric["source_version"], f"{location}.source_version", 32) != "0.3":
        _fail(f"{location}.source_version", "must equal 0.3")
    references = _validate_references(metric["references"], f"{location}.references", authorities)
    if {reference["source_id"] for reference in references} != set(AUTHORITY_FILES):
        _fail(f"{location}.references", "must cite both frozen local authority documents")
    requirements = _validate_requirements(metric["source_requirements"], f"{location}.source_requirements")
    _validate_requirement_reference_coverage(
        requirements,
        references,
        f"{location}.references",
    )
    implementation = _mapping(metric["implementation"], f"{location}.implementation")
    _strict_keys(implementation, f"{location}.implementation", {"kind", "artifact"})
    if implementation != {
        "kind": "DEFINITION_ONLY",
        "artifact": "metrics/metric-registry.json",
    }:
        _fail(f"{location}.implementation", "must identify only the declarative registry artifact")
    _semver(metric["implementation_version"], f"{location}.implementation_version")
    limitations = _strings(metric["limitations"], f"{location}.limitations")
    warnings = _strings(metric["warnings"], f"{location}.warnings")
    if metric_id == "biometric.quality.utility.erc":
        required_erc_inputs = {
            "registered_base_error_metric",
            "fixed_matcher_threshold",
            "score_direction",
            "threshold_equality_rule",
            "pair_quality_policy_id",
            "quality_direction",
            "processing_failure_policy",
            "rejection_domain",
        }
        missing = sorted(required_erc_inputs - set(required_inputs))
        if missing:
            _fail(f"{location}.required_inputs", f"missing ERC inputs: {', '.join(missing)}")
        if semantics["threshold_reestimation"] != "FORBIDDEN_ACROSS_CURVE":
            _fail(f"{location}.threshold_semantics", "ERC must forbid matcher-threshold re-estimation")
    if metric_id == "biometric.verification.eer" and "eer_estimation_method" not in required_inputs:
        _fail(f"{location}.required_inputs", "EER must require its empirical estimation method")
    if formula["formula_version"] != metric["definition_version"]:
        _fail(f"{location}.formula.formula_version", "must equal definition_version")
    if metric["implementation_version"] != metric["definition_version"]:
        _fail(f"{location}.implementation_version", "must equal definition_version")
    if not set(requirements) <= REQUIRED_SOURCE_REQUIREMENTS:
        _fail(f"{location}.source_requirements", "contains a requirement outside this registry slice")
    expected_contract = CORE_CONTRACTS.get(metric_id) or PHASE3_CONTRACTS.get(metric_id) or IDENTIFICATION_CONTRACTS.get(metric_id)
    if expected_contract is None:
        _fail(location, "has no versioned semantic contract")
    observed_contract = {
        "canonical_name": canonical_name,
        "aliases": tuple(aliases),
        "required_inputs": tuple(required_inputs),
        "aggregation_unit": aggregation_unit,
        "threshold_semantics": semantics,
        "confidence_interval_methods": tuple(confidence_methods),
        "references": tuple(
            (reference["source_id"], reference["locator"])
            for reference in references
        ),
        "source_requirements": tuple(requirements),
        "prose_sha256": _canonical_sha256(
            {
                "definition": metric["definition"],
                "limitations": limitations,
                "warnings": warnings,
            }
        ),
    }
    if observed_contract != expected_contract:
        differing_fields = sorted(
            field
            for field in expected_contract
            if observed_contract[field] != expected_contract[field]
        )
        _fail(
            location,
            "versioned semantic contract differs in: " + ", ".join(differing_fields),
        )
    return metric


def validate_registry(registry: dict[str, Any]) -> dict[str, Any]:
    _validate_text_policy(registry, "registry")
    root_keys = {
        "schema_version",
        "registry_id",
        "registry_version",
        "authority_documents",
        "source_requirements",
        "metrics",
    }
    _strict_keys(registry, "registry", root_keys)
    if _semver(registry["schema_version"], "registry.schema_version") != "1.0.0":
        _fail("registry.schema_version", "unsupported schema version")
    if _string(registry["registry_id"], "registry.registry_id", 128) != "qualsight-core-metrics":
        _fail("registry.registry_id", "must equal qualsight-core-metrics")
    if _semver(registry["registry_version"], "registry.registry_version") != "1.2.0":
        _fail("registry.registry_version", "unsupported registry version")
    authorities = _validate_authorities(registry["authority_documents"])
    requirements = _validate_requirements(registry["source_requirements"], "registry.source_requirements")
    if set(requirements) != REQUIRED_SOURCE_REQUIREMENTS:
        _fail("registry.source_requirements", "must contain the exact core-slice requirement set")
    raw_metrics = _sequence(registry["metrics"], "registry.metrics", MAX_METRICS)
    metrics = [
        _validate_metric(metric, index, authorities) for index, metric in enumerate(raw_metrics)
    ]
    metric_ids = [metric["metric_id"] for metric in metrics]
    if len(metric_ids) != len(set(metric_ids)):
        _fail("registry.metrics", "contains duplicate metric IDs")
    if set(metric_ids) != CORE_METRIC_IDS | PHASE3_METRIC_IDS | IDENTIFICATION_METRIC_IDS:
        _fail("registry.metrics", "must contain the six Phase-0 core metrics, the eight Phase-3 quality-utility metrics, and the five identification metrics")
    versions_by_id = {metric["metric_id"]: metric["definition_version"] for metric in metrics}
    for index, metric in enumerate(metrics):
        formula = metric["formula"]
        if formula["kind"] == "THRESHOLD_CURVE":
            links = (
                (formula["x_metric_id"], formula["x_definition_version"]),
                (formula["y_metric_id"], formula["y_definition_version"]),
            )
        elif formula["kind"] == "EQUALITY_OPERATING_POINT":
            links = (
                (formula["left_metric_id"], formula["left_definition_version"]),
                (formula["right_metric_id"], formula["right_definition_version"]),
            )
        else:
            links = ()
        for linked_id, linked_version in links:
            if versions_by_id.get(linked_id) != linked_version:
                _fail(
                    f"registry.metrics[{index}].formula",
                    f"{linked_id}@{linked_version} does not resolve in this registry",
                )
    names_and_aliases: list[str] = []
    for metric in metrics:
        names_and_aliases.extend([metric["canonical_name"], *metric["aliases"]])
    normalized = [value.casefold() for value in names_and_aliases]
    if len(normalized) != len(set(normalized)):
        _fail("registry.metrics", "canonical names and aliases must be globally unique")
    versions = {metric["definition_version"] for metric in metrics}
    if versions != {"1.0.0"}:
        _fail("registry.metrics", "the core, utility, and identification definitions must use version 1.0.0")
    return {
        "registry_id": registry["registry_id"],
        "registry_version": registry["registry_version"],
        "metrics": len(metrics),
        "definition_versions": sorted(versions),
        "authorities": len(authorities),
        "requirements": len(requirements),
    }


def validate_registry_file(path: Path) -> dict[str, Any]:
    return validate_registry(_load_json(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the QualSight core Metric Registry.")
    parser.add_argument("registry", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = validate_registry_file(args.registry)
    except (OSError, MetricRegistryValidationError) as error:
        print(f"METRIC_REGISTRY_INVALID: {error}", file=sys.stderr)
        return 1
    versions = ",".join(summary["definition_versions"])
    print(
        "METRIC_REGISTRY_VALID "
        f"id={summary['registry_id']} version={summary['registry_version']} "
        f"metrics={summary['metrics']} definition_versions={versions} "
        f"authorities={summary['authorities']} requirements={summary['requirements']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
