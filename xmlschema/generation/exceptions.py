"""
Exception and result classes for bounded XML instance generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional
from xml.etree.ElementTree import Element

if TYPE_CHECKING:
    from ..validators import XMLSchemaBase

__all__ = [
    'GenerationStatus', 'InstanceGenerationError', 'NoInstancePossibleError',
    'BudgetExhaustedError', 'UnsupportedSchemaFeatureError',
    'GenerationValidationError', 'GenerationResult',
]


class GenerationStatus(str, Enum):
    """Outcome status of a bounded instance generation attempt."""

    SUCCESS = 'success'
    NO_SOLUTION = 'no_solution'
    BUDGET_EXHAUSTED = 'budget_exhausted'
    UNSUPPORTED = 'unsupported'


class InstanceGenerationError(ValueError):
    """Base class for errors raised during XML instance generation."""

    def __init__(self, reason: str, *, component: object = None) -> None:
        self.reason = reason
        self.component = component
        if component is not None:
            msg = f"{reason} [component: {component!r}]"
        else:
            msg = reason
        super().__init__(msg)


class NoInstancePossibleError(InstanceGenerationError):
    """
    Raised when the schema constraints admit no finite instance at all
    (e.g. a recursive element that cannot terminate, or an empty
    enumeration for a required value).
    """


class BudgetExhaustedError(InstanceGenerationError):
    """
    Raised when every candidate instance would exceed the configured
    node/occurrence budget, although a larger budget could succeed.
    """

    def __init__(self, reason: str, *, budget: int, nodes: int,
                 component: object = None) -> None:
        self.budget = budget
        self.nodes = nodes
        super().__init__(reason, component=component)


class UnsupportedSchemaFeatureError(InstanceGenerationError):
    """
    Raised when generation is requested for a schema construct that the
    bounded generator does not support (patterns/assertions that cannot be
    satisfied by enumeration, wildcards, XSD 1.1 open content, ...).

    Nothing is generated when this error is raised: the *reason* always
    explains precisely which construct is not supported.
    """


class GenerationValidationError(InstanceGenerationError):
    """
    Raised when a generated candidate fails validation against the same
    schema. This signals a generator bug rather than an invalid schema.
    """


@dataclass
class GenerationResult:
    """
    Result of :func:`xmlschema.generation.generate_xml`.

    :ivar status: one of the values of :class:`GenerationStatus`.
    :ivar element: the generated root element (``None`` unless success).
    :ivar reason: a human readable explanation for non-success outcomes.
    :ivar nodes: number of generated elements.
    :ivar attempts: number of generation attempts performed.
    :ivar schema: the schema the instance was generated and validated against.
    """

    status: GenerationStatus
    schema: 'XMLSchemaBase'
    element: Optional[Element] = None
    reason: Optional[str] = None
    nodes: int = 0
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is GenerationStatus.SUCCESS

    def get_xml(self, *, indent: str = '  ',
                xml_declaration: bool = True,
                encoding: str = 'unicode'):
        """Serialize the generated element using :func:`xmlschema.etree_tostring`."""
        if self.element is None:
            raise ValueError("no element has been generated")
        from xml.etree.ElementTree import register_namespace
        from ..utils.etree import etree_tostring
        namespaces = getattr(self.schema, 'namespaces', None) or {}
        for prefix, uri in namespaces.items():
            if prefix and uri:
                register_namespace(prefix, uri)
        return etree_tostring(
            self.element, indent=indent,
            xml_declaration=xml_declaration, encoding=encoding,
        )
