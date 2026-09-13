"""The coverage map that makes P7-T4's acceptance criterion checkable.

Section 23 names nine families. "All families pass with the specified
expected behaviour" is only a meaningful claim if something verifies that
each family still *has* a test - otherwise a renamed class or a deleted
module silently drops a family and every remaining test still goes green.

`FAMILY_COVERAGE` below maps each family to the test classes that assert
it, wherever they live (several families were already covered by suites
that predate this task - those are pointed at rather than duplicated). The
test then imports each target and asserts it really exists.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = [pytest.mark.security]

# family -> ((module, class-name), ...)
FAMILY_COVERAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "image_quality": (
        (
            "tests.security.adversarial.test_family_image_and_coverage",
            "TestImageQualityRoutesToReview",
        ),
        (
            "tests.security.adversarial.test_family_image_and_coverage",
            "TestDegradationReallyLowersRealOcrConfidence",
        ),
    ),
    "coverage": (
        (
            "tests.security.adversarial.test_family_image_and_coverage",
            "TestCoverageIsInsufficientDataNotPass",
        ),
    ),
    "conflict": (
        ("tests.security.adversarial.test_family_conflict", "TestConflictDetectorItself"),
        (
            "tests.security.adversarial.test_family_conflict",
            "TestConflictRoutesToMandatoryReview",
        ),
    ),
    "fabrication_bait": (
        (
            "tests.security.adversarial.test_family_fabrication_and_multilingual",
            "TestFabricationBait",
        ),
    ),
    "multilingual": (
        (
            "tests.security.adversarial.test_family_fabrication_and_multilingual",
            "TestMultilingual",
        ),
    ),
    "prompt_injection": (
        (
            "tests.security.adversarial.test_family_prompt_injection",
            "TestVerdictsAreUnchangedByInjection",
        ),
        (
            "tests.security.adversarial.test_family_prompt_injection",
            "TestNoChannelForAnInjectionToAct",
        ),
        ("tests.unit.test_extraction_prompt", "TestInjectionCorpus"),
    ),
    "file_attacks": (
        ("tests.security.adversarial.test_family_file_and_abuse", "TestFileAttacks"),
        ("tests.unit.test_ingestion", "TestMagicSniffing"),
        ("tests.unit.test_ingestion", "TestPdfValidation"),
        (
            "tests.integration.test_ingestion_upload_completion",
            "TestAdversarialUploads",
        ),
    ),
    "tenancy": (
        ("tests.security.test_tenant_isolation", "TestCrossTenantResourceAccess"),
        ("tests.security.test_tenant_isolation", "TestAuthorizationSurface"),
        ("tests.security.test_tenant_isolation", "TestEnumeration"),
    ),
    "load_abuse": (
        ("tests.security.adversarial.test_family_file_and_abuse", "TestLoadAndAbuse"),
    ),
}

# Exactly the nine rows of IMPLEMENTATION.md section 23's table.
SECTION_23_FAMILIES = frozenset(
    {
        "image_quality",
        "coverage",
        "conflict",
        "fabrication_bait",
        "multilingual",
        "prompt_injection",
        "file_attacks",
        "tenancy",
        "load_abuse",
    }
)


def test_every_section_23_family_is_mapped() -> None:
    assert set(FAMILY_COVERAGE) == SECTION_23_FAMILIES


@pytest.mark.parametrize("family", sorted(SECTION_23_FAMILIES))
def test_every_family_has_at_least_one_real_test_class(family: str) -> None:
    targets = FAMILY_COVERAGE[family]
    assert targets, f"family {family!r} has no test mapped"

    for module_path, class_name in targets:
        module = importlib.import_module(module_path)
        test_class = getattr(module, class_name, None)
        assert test_class is not None, (
            f"{family}: {module_path}.{class_name} no longer exists - "
            "a family lost its coverage"
        )
        test_methods = [name for name in dir(test_class) if name.startswith("test_")]
        assert test_methods, f"{family}: {module_path}.{class_name} has no test methods"
