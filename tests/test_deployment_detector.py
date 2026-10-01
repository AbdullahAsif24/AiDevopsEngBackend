"""Unit tests for the deployment-type detection module (detect_by_rules).

Tests use small fixture directories under ``tests/fixtures/`` rather than
mocking file reads, so they validate the real file-system scanning logic.

Run with:  pytest tests/test_deployment_detector.py -v
"""
from __future__ import annotations

import os
import pathlib
import pytest

from app.contracts import DeploymentType
from app.services.deployment_detector import detect_by_rules

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fixture_path(name: str) -> str:
    """Return an absolute path to a named fixture directory."""
    path = (FIXTURES / name).resolve()
    assert path.is_dir(), f"Fixture directory missing: {path}"
    return str(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNextJsRepo:
    """package.json with 'next' in dependencies -> VERCEL_NATIVE."""

    def test_returns_vercel_native(self):
        result = detect_by_rules(_fixture_path("nextjs-repo"))
        assert result is not None
        assert result.deployment_type == DeploymentType.VERCEL_NATIVE
        assert result.confidence == "high"
        assert "Next.js" in result.detected_framework
        assert result.needs_dockerfile is False
        assert result.detection_method == "rule_based"


class TestCraRepo:
    """react-scripts (CRA) with no server entry -> STATIC."""

    def test_returns_static(self):
        result = detect_by_rules(_fixture_path("cra-repo"))
        assert result is not None
        assert result.deployment_type == DeploymentType.STATIC
        assert result.confidence == "high"
        assert result.needs_dockerfile is False
        assert result.detection_method == "rule_based"


class TestFlaskRepo:
    """requirements.txt with flask + app.py with Flask(__name__) -> CONTAINER."""

    def test_returns_container_with_entry_point(self):
        result = detect_by_rules(_fixture_path("flask-repo"))
        assert result is not None
        assert result.deployment_type == DeploymentType.CONTAINER
        assert result.confidence == "high"
        assert result.detected_framework == "Flask"
        assert result.needs_dockerfile is True
        assert result.entry_point is not None
        assert result.detection_method == "rule_based"

    def test_entry_point_is_app_py(self):
        result = detect_by_rules(_fixture_path("flask-repo"))
        assert result is not None
        assert result.entry_point == "app.py"

    def test_listens_on_port(self):
        result = detect_by_rules(_fixture_path("flask-repo"))
        assert result is not None
        assert result.listen_port == 5000


class TestSpringBootRepo:
    """pom.xml + @SpringBootApplication class -> CONTAINER, detected_framework = Spring Boot."""

    def test_returns_container(self):
        result = detect_by_rules(_fixture_path("springboot-repo"))
        assert result is not None
        assert result.deployment_type == DeploymentType.CONTAINER
        assert result.confidence == "high"
        assert result.detected_framework == "Spring Boot"
        assert result.needs_dockerfile is True
        assert result.detection_method == "rule_based"

    def test_entry_point_is_java_class(self):
        result = detect_by_rules(_fixture_path("springboot-repo"))
        assert result is not None
        assert result.entry_point is not None
        assert result.entry_point.endswith("DemoApplication.java")


class TestEmptyRepo:
    """No recognizable manifests -> returns None (triggers LLM fallback)."""

    def test_returns_none(self):
        result = detect_by_rules(_fixture_path("empty-repo"))
        assert result is None
