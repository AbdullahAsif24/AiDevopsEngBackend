"""Pydantic schemas for deployment-type detection.

These are thin re-exports of the canonical contracts defined in
``app.contracts``. The pipeline, routes, and WebSocket payloads all import from
here so the detection surface stays self-contained and versioned, while the
underlying model definitions live next to the other shared contracts.

Keeping this module as a re-export (rather than a duplicate definition) avoids
the risk of two Pydantic models drifting apart across the codebase.
"""
from __future__ import annotations

from ..contracts import DetectionResult, DeploymentType

__all__ = ["DeploymentType", "DetectionResult"]