"""Limited in-memory model of the XSD constructs supported by the mapper.

Only what v1 needs is represented: global/local elements, named and anonymous
complex types (element-only, empty and simple-content), simple types derived by
restriction from built-ins, attributes, xs:sequence and xs:choice particles.
Anything else is reported by the builder/planner instead of being approximated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union
import re

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# Storage tags. BOOL is stored as INTEGER in SQLite but keeps its own tag so the
# reference XML rebuild can render true/false instead of 1/0.
TEXT = "TEXT"
INTEGER = "INTEGER"
REAL = "REAL"
BOOL = "BOOL"

DDL_TYPE = {
    TEXT: "TEXT",
    INTEGER: "INTEGER",
    REAL: "REAL",
    BOOL: "INTEGER",
}

_INTEGER_FAMILY = {
    "integer", "long", "int", "short", "byte",
    "nonNegativeInteger", "positiveInteger", "nonPositiveInteger",
    "negativeInteger", "unsignedLong", "unsignedInt", "unsignedShort",
    "unsignedByte", "wholeInteger",
}
_REAL_FAMILY = {"float", "double"}

_QNAME_SPLIT = re.compile(r"\{(?P<ns>[^}]*)\}(?P<local>.+)")


def split_tag(tag: str) -> tuple[str, str]:
    """Split a Clark-notation ElementTree tag into ``(namespace, local)``."""
    match = _QNAME_SPLIT.fullmatch(tag)
    if match is None:
        return "", tag
    return match.group("ns"), match.group("local")

# Explicit value states. They deliberately keep apart cases that a bare SQL
# NULL would conflate (missing particle, empty element, xsi:nil, defaults).
STATE_MISSING = "missing"    # particle/attribute absent from the document
STATE_VALUE = "value"        # explicit value present
STATE_EMPTY = "empty"        # element present with empty content, no default
STATE_NIL = "nil"            # xsi:nil="true"
STATE_DEFAULT = "default"    # value supplied by an XSD default
STATE_FIXED = "fixed"        # value supplied by an XSD fixed constraint

PRESENT_STATES = {STATE_VALUE, STATE_EMPTY, STATE_NIL, STATE_DEFAULT, STATE_FIXED}


def builtin_storage_tag(qname: tuple[Optional[str], str]) -> Optional[str]:
    """Return the SQLite storage tag for a built-in XSD datatype qname."""
    ns, local = qname
    if ns != XSD_NS:
        return None
    if local in _INTEGER_FAMILY:
        return INTEGER
    if local in _REAL_FAMILY:
        return REAL
    if local == "boolean":
        return BOOL
    # decimal and every date/time/string/ID/... type stay textual to preserve
    # lexical precision (decimals especially must not pass through float).
    return TEXT


class SchemaFeatureError(Exception):
    """Raised while resolving an XSD construct that v1 does not support."""


@dataclass(frozen=True)
class AnyType:
    """Marker for xs:anyType / untyped declarations (unsupported in v1)."""


ANY_TYPE = AnyType()


@dataclass
class SimpleType:
    qname: Optional[tuple[Optional[str], str]]
    base_qname: tuple[Optional[str], str]
    facets: tuple[str, ...] = ()


@dataclass
class AttributeDecl:
    qname: tuple[Optional[str], str]
    type_def: SimpleType
    form: Optional[bool] = None    # None: follow attributeFormDefault
    global_: bool = False
    use: str = "optional"          # optional | required | prohibited
    default: Optional[str] = None
    fixed: Optional[str] = None


@dataclass
class ElementDecl:
    qname: tuple[Optional[str], str]
    type_def: Union[SimpleType, "ComplexType", AnyType]
    form: Optional[bool] = None    # None: follow elementFormDefault
    global_: bool = False
    nillable: bool = False
    default: Optional[str] = None
    fixed: Optional[str] = None
    abstract: bool = False
    substitution_group: Optional[tuple[Optional[str], str]] = None


@dataclass
class ElementUse:
    decl: ElementDecl
    min_occurs: int = 1
    max_occurs: int = 1            # -1 means unbounded


@dataclass
class Sequence:
    children: list = field(default_factory=list)
    min_occurs: int = 1
    max_occurs: int = 1


@dataclass
class Choice:
    children: list = field(default_factory=list)
    min_occurs: int = 1
    max_occurs: int = 1


@dataclass
class ComplexType:
    qname: Optional[tuple[Optional[str], str]]
    kind: str                      # 'element-only' | 'empty' | 'simple'
    attributes: list[AttributeDecl] = field(default_factory=list)
    content: Union[Sequence, Choice, None] = None
    simple_type: Optional[SimpleType] = None
    mixed: bool = False


@dataclass
class Schema:
    """Resolved schema document plus the prefixes chosen by its author."""

    target_namespace: Optional[str]
    element_form_default: str = "unqualified"
    attribute_form_default: str = "unqualified"
    prefixes: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedSchema:
    schema_info: Schema
    root_element: object  # ElementDecl, or a zero-arg callable resolving one
