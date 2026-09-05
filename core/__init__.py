"""Core module - fundamental components

This module contains core models, exceptions, and parameter validation.
    - models: data models
    - exceptions: custom exceptions
    - parameter_validator: unified parameter validation
"""

from .models import Schedule, ScheduleItem, ScheduleType
from .exceptions import (
    AutonomousPlanningError,
    LLMError,
    LLMQuotaExceededError,
    LLMTimeoutError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    DatabaseError,
    ValidationError,
    InvalidParametersError,
    InvalidTimeWindowError,
    ScheduleError,
    ScheduleGenerationError,
)
from .parameter_validator import ParameterValidator

__all__ = [
    "Schedule",
    "ScheduleItem",
    "ScheduleType",
    "AutonomousPlanningError",
    "LLMError",
    "LLMQuotaExceededError",
    "LLMTimeoutError",
    "LLMInvalidResponseError",
    "LLMRateLimitError",
    "DatabaseError",
    "ValidationError",
    "InvalidParametersError",
    "InvalidTimeWindowError",
    "ScheduleError",
    "ScheduleGenerationError",
    "ParameterValidator",
]
