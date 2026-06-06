"""Environment detection used to suppress telemetry in CI and test runs.

Telemetry is opt-out (on by default), but we never phone home from a CI job or a
test runner: those machines are not consenting humans and they would skew the
aggregate usage numbers. Detection is best-effort and intentionally broad.
"""

from __future__ import annotations

import os

# Presence (truthy) of any of these marks a CI environment. ``CI`` /
# ``CONTINUOUS_INTEGRATION`` are the generic markers; the rest are
# provider-specific so we still detect CI when a provider does not set ``CI``.
_CI_ENV_VARS = (
    "CI",
    "CONTINUOUS_INTEGRATION",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "TRAVIS",
    "CIRCLECI",
    "APPVEYOR",
    "TF_BUILD",  # Azure Pipelines
    "TEAMCITY_VERSION",
    "BUILDKITE",
    "DRONE",
    "JENKINS_URL",
    "HUDSON_URL",
    "BAMBOO_BUILDKEY",
    "CODEBUILD_BUILD_ID",
)

# Test runners that set a marker env var while a test is executing.
_TEST_ENV_VARS = ("PYTEST_CURRENT_TEST",)


def is_truthy(value: str | None) -> bool:
    """True when *value* is set to anything other than empty / a falsey literal."""
    return value is not None and value.strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }


def is_ci() -> bool:
    """True when running under a recognised CI provider."""
    return any(is_truthy(os.getenv(name)) for name in _CI_ENV_VARS)


def is_test() -> bool:
    """True when running under a recognised test runner.

    Honors both the Patter-specific ``PATTER_ENV=test`` and the cross-ecosystem
    ``NODE_ENV=test`` so the suppression knob behaves identically to the TS SDK.
    """
    if any(os.getenv(name) is not None for name in _TEST_ENV_VARS):
        return True
    return (
        os.getenv("PATTER_ENV", "").strip().lower() == "test"
        or os.getenv("NODE_ENV", "").strip().lower() == "test"
    )
