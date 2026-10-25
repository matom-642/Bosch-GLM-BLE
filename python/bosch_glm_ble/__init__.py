# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Matej Mician
"""Read and control Bosch GLM laser meters (e.g. GLM 50-27 CG) over Bluetooth LE."""

from . import protocol
from .client import (
    GLM,
    GLMBusy,
    GLMDisconnected,
    GLMError,
    GLMMeasurementError,
    GLMNotFound,
    GLMStatusError,
    GLMTimeout,
    Reading,
)

__version__ = "0.2.0"

__all__ = [
    "GLM",
    "Reading",
    "GLMError",
    "GLMNotFound",
    "GLMTimeout",
    "GLMStatusError",
    "GLMMeasurementError",
    "GLMBusy",
    "GLMDisconnected",
    "protocol",
]
