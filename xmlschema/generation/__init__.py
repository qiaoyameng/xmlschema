"""
Bounded XML instance generation from XML Schema components.

This package provides a small, deterministic generator that produces valid
XML instances for API integration testing. Only a documented subset of XSD
constructs is supported; unsupported constructs and genuinely impossible or
over-budget schemas fail with a precise, distinct error.
"""
from __future__ import annotations

from typing import Optional

from ..aliases import SchemaType
from .exceptions import (
    BudgetExhaustedError,
    GenerationResult,
    GenerationStatus,
    GenerationValidationError,
    InstanceGenerationError,
    NoInstancePossibleError,
    UnsupportedSchemaFeatureError,
)
from .generator import (
    DEFAULT_MAX_NODES,
    DEFAULT_MAX_OCCURS,
    generate_instance,
)

__all__ = [
    'GenerationStatus', 'GenerationResult', 'InstanceGenerationError',
    'NoInstancePossibleError', 'BudgetExhaustedError',
    'UnsupportedSchemaFeatureError', 'GenerationValidationError',
    'generate_instance', 'generate_xml',
    'DEFAULT_MAX_NODES', 'DEFAULT_MAX_OCCURS',
]


def generate_xml(schema: SchemaType, *, name: Optional[str] = None,
                 seed: int = 0,
                 max_nodes: int = DEFAULT_MAX_NODES,
                 max_occurs: int = DEFAULT_MAX_OCCURS) -> GenerationResult:
    """
    Non-raising entry point for bounded instance generation.

    Returns a :class:`GenerationResult` whose ``status`` is one of
    ``success``, ``no_solution``, ``budget_exhausted`` or ``unsupported``.
    On success ``result.element`` is an ElementTree element that has been
    validated against *schema*; on failure ``result.reason`` explains why.
    """
    attempts = 0
    try:
        element = generate_instance(
            schema, name=name, seed=seed,
            max_nodes=max_nodes, max_occurs=max_occurs,
        )
    except BudgetExhaustedError as err:
        return GenerationResult(
            GenerationStatus.BUDGET_EXHAUSTED, schema=schema,
            reason=err.reason, nodes=err.nodes,
        )
    except NoInstancePossibleError as err:
        return GenerationResult(
            GenerationStatus.NO_SOLUTION, schema=schema, reason=err.reason,
        )
    except UnsupportedSchemaFeatureError as err:
        return GenerationResult(
            GenerationStatus.UNSUPPORTED, schema=schema, reason=err.reason,
        )

    nodes = sum(1 for _ in element.iter())
    return GenerationResult(
        GenerationStatus.SUCCESS, schema=schema, element=element, nodes=nodes,
    )
