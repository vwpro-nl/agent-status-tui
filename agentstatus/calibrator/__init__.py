"""Explicit, provider-neutral calibration subsystem.

Nothing in this package is imported by the normal status collection path.
"""

from .core import Calibrator, CalibratorError
from .model import ActivityResult, Adapter, Assessment, Observation

__all__ = ["ActivityResult", "Adapter", "Assessment", "Calibrator", "CalibratorError", "Observation"]
