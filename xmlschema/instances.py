from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional
from xml.etree import ElementTree

import xmlschema.names as nm
from xmlschema.utils.qnames import local_name

from .validators.attributes import XsdAttribute, XsdAnyAttribute
from .validators.complex_types import XsdComplexType
from .validators.elements import XsdElement
from .validators.groups import XsdGroup
from .validators.identities import XsdIdentity, XsdKey, XsdKeyref, XsdUnique
from .validators.simple_types import XsdList, XsdSimpleType, XsdUnion
from .validators.wildcards import XsdAnyElement

VALID = 'valid'
NO_SOLUTION = 'no_solution'
BUDGET_EXHAUSTED = 'budget_exhausted'
UNSUPPORTED = 'unsupported'
VALIDATION_FAILED = 'validation_failed'

INF = 10 ** 30
XSI_NIL = f'{{{nm.XSI_NAMESPACE}}}nil'


class InstanceGenerationError(ValueError):
    pass


class UnsupportedSchemaError(InstanceGenerationError):
    pass


class BudgetExhaustedError(InstanceGenerationError):
    pass


class NoSolutionError(InstanceGenerationError):
    pass


@dataclass(frozen=True)
class FieldTarget:
    identity: XsdIdentity
    field_index: int
    text: bool
    attr_name: Optional[str] = None


class ForceNode:
    __slots__ = ('pending', 'children', 'choice_branches',
                 'fields', 'attributes', 'text_fields')

    def __init__(self) -> None:
        self.pending: dict[str, list['ForceNode']] = {}
        self.children: dict[int, list['ForceNode']] = {}
        self.choice_branches: dict[int, list['ForceNode']] = {}
        self.fields: list[FieldTarget] = []
        self.attributes: dict[str, list[FieldTarget]] = {}
        self.text_fields: list[FieldTarget] = []

    def empty(self) -> bool:
        return not (self.pending or self.children or self.choice_branches or
                    self.fields or self.attributes or self.text_fields)

    def copy_shape(self) -> 'ForceNode':
        node = ForceNode()
        for name, children in self.pending.items():
            node.pending[name] = [copy_force_node(child) for child in children]
        for field in self.fields:
            node.fields.append(field)
        for name, fields in self.attributes.items():
            node.attributes[name] = list(fields)
        for field in self.text_fields:
            node.text_fields.append(field)
        return node

    def merge(self, other: 'ForceNode') -> None:
        for name, children in other.pending.items():
            self.pending.setdefault(name, []).extend(children)
        for key, children in other.children.items():
            self.children.setdefault(key, []).extend(children)
        for key, branches in other.choice_branches.items():
            self.choice_branches.setdefault(key, []).extend(branches)
        self.fields.extend(other.fields)
        for name, fields in other.attributes.items():
            self.attributes.setdefault(name, []).extend(fields)
        self.text_fields.extend(other.text_fields)


def copy_force_node(node: ForceNode) -> ForceNode:
    result = node.copy_shape()
    for key, children in node.children.items():
        result.children[key] = [copy_force_node(child) for child in children]
    for key, branches in node.choice_branches.items():
        result.choice_branches[key] = [copy_force_node(branch) for branch in branches]
    return result


def merge_nodes(nodes: list[ForceNode]) -> ForceNode:
    result = ForceNode()
    for node in nodes:
        result.merge(node)
    return result


class ForceTree:
    def __init__(self) -> None:
        self.root = ForceNode()
        self.element_attributes: dict[int, dict[int, list[FieldTarget]]] = {}

    def mark_identity(self, identity: XsdIdentity) -> None:
        force_fields = isinstance(identity, (XsdKey, XsdKeyref))
        selector = identity.selector.path.strip() if identity.selector and identity.selector.path else ''
        segments = parse_selector(selector)
        node = self.root
        for name in segments:
            next_node = ForceNode()
            node.pending.setdefault(name, []).append(next_node)
            node = next_node
        if not force_fields:
            return
        for index, field in enumerate(identity.fields):
            path = field.path.strip() if field.path else '.'
            target = FieldTarget(identity, index, True)
            mark_field_path(parse_field_path(path, identity), node, target)

    def compile(self, root_element: XsdElement) -> None:
        self._compile_element(root_element, self.root)
        unresolved = self._unresolved(self.root, set())
        if unresolved:
            raise UnsupportedSchemaError(
                f"identity selector/field path {unresolved!r} cannot be resolved in the schema"
            )

    def _compile_element(self, element: XsdElement, node: ForceNode) -> None:
        if isinstance(element.type, XsdSimpleType):
            if node.pending:
                raise UnsupportedSchemaError("identity path continues through a simple element")
        elif isinstance(element.type, XsdComplexType):
            mapped_attrs: dict[int, list[FieldTarget]] = {}
            for attr_name, fields in node.attributes.items():
                attr = _find_attribute(element, attr_name)
                if attr is None:
                    raise UnsupportedSchemaError(f"identity field attribute {attr_name!r} not found")
                mapped_attrs[id(attr)] = fields
                if attr.ref is not None and attr.ref is not attr:
                    mapped_attrs[id(attr.ref)] = fields
            if mapped_attrs:
                self.element_attributes.setdefault(id(element), {}).update(mapped_attrs)
                if element.ref is not None:
                    self.element_attributes.setdefault(id(element.ref), {}).update(mapped_attrs)
            content = element.type.content
            if isinstance(content, XsdGroup) and len(content):
                self._compile_group(content, node)
            elif node.pending or node.children or node.choice_branches:
                raise UnsupportedSchemaError("identity path continues through an empty element")

    def _compile_group(self, group: XsdGroup, node: ForceNode) -> None:
        if group.ref is not None and len(group) == 1:
            self._compile_group(group.ref, node)
            return
        for particle in group:
            if isinstance(particle, XsdAnyElement):
                if node.pending:
                    raise UnsupportedSchemaError("identity path crosses an xs:any wildcard")
                continue
            if isinstance(particle, XsdElement):
                name = local_name(particle.name or '')
                pending_nodes = node.pending.get(name, [])
                if not pending_nodes and particle.ref is not None:
                    pending_nodes = node.pending.get(local_name(particle.ref.name), [])
                if pending_nodes:
                    child_node = merge_nodes(pending_nodes)
                    self._compile_element(particle, child_node)
                    node.children.setdefault(id(particle), []).append(child_node)
            elif isinstance(particle, XsdGroup):
                if group.model == 'choice':
                    branches: list[ForceNode] = []
                    branch_node = node.copy_shape()
                    self._compile_group(particle, branch_node)
                    if not branch_node.empty():
                        branches.append(branch_node)
                    if branches:
                        node.choice_branches.setdefault(id(group), []).extend(branches)
                else:
                    child_node = node.copy_shape()
                    self._compile_group(particle, child_node)
                    if not child_node.empty():
                        node.children.setdefault(id(particle), []).append(child_node)
        if group.model == 'choice' and node.pending and id(group) not in node.choice_branches:
            raise UnsupportedSchemaError("identity path cannot be resolved in a choice")
        node.pending.clear()

    def _unresolved(self, node: ForceNode, seen: set[int]) -> list[str]:
        if id(node) in seen:
            return []
        seen.add(id(node))
        result = list(node.pending)
        for children in node.children.values():
            for child in children:
                result.extend(self._unresolved(child, seen))
        for branches in node.choice_branches.values():
            for branch in branches:
                result.extend(self._unresolved(branch, seen))
        return result


def parse_selector(path: str) -> list[str]:
    segments = _parse_simple_path(path, identity=None, allow_dot=True)
    if any(is_attr for _, is_attr in segments):
        raise UnsupportedSchemaError("attribute identity selectors are not supported")
    return [name for name, _ in segments]


def parse_field_path(path: str, identity: XsdIdentity) -> list[tuple[str, bool]]:
    return _parse_simple_path(path, identity, allow_dot=True)


def _parse_simple_path(path: str, identity: Optional[XsdIdentity],
                       allow_dot: bool) -> list[tuple[str, bool]]:
    if path in ('.', './'):
        if not allow_dot:
            raise UnsupportedSchemaError("unsupported identity path '.'")
        return []
    if path.startswith('./'):
        path = path[2:]
    if not path or path.startswith('/') or '//' in path or '|' in path or '[' in path:
        raise UnsupportedSchemaError(f"unsupported identity path {path!r}")
    result: list[tuple[str, bool]] = []
    for segment in path.split('/'):
        if not segment:
            raise UnsupportedSchemaError(f"unsupported identity path {path!r}")
        is_attr = segment.startswith('@')
        if is_attr:
            segment = segment[1:]
        if not segment or segment == '*' or ':' in segment or '(' in segment:
            raise UnsupportedSchemaError(f"unsupported identity path {path!r}")
        result.append((segment, is_attr))
    return result


def mark_field_path(segments: list[tuple[str, bool]], node: ForceNode,
                    target: FieldTarget) -> None:
    current = node
    for index, (name, is_attr) in enumerate(segments):
        final = index == len(segments) - 1
        if is_attr:
            if not final:
                raise UnsupportedSchemaError("an attribute field must be the final path segment")
            field = FieldTarget(target.identity, target.field_index, False, name)
            current.attributes.setdefault(name, []).append(field)
        elif final:
            current.fields.append(FieldTarget(target.identity, target.field_index, True))
        else:
            next_node = ForceNode()
            current.pending.setdefault(name, []).append(next_node)
            current = next_node
    if not segments:
        current.text_fields.append(FieldTarget(target.identity, target.field_index, True))


def _find_attribute(element: XsdElement, name: str) -> Optional[XsdAttribute]:
    for attr in element.attributes.values():
        if isinstance(attr, XsdAttribute) and local_name(attr.name or '') == name:
            return attr
    return None


INTEGER_TYPES = {
    nm.XSD_INTEGER, nm.XSD_LONG, nm.XSD_INT, nm.XSD_SHORT, nm.XSD_BYTE,
    nm.XSD_NON_NEGATIVE_INTEGER, nm.XSD_POSITIVE_INTEGER,
    nm.XSD_UNSIGNED_LONG, nm.XSD_UNSIGNED_INT, nm.XSD_UNSIGNED_SHORT,
    nm.XSD_UNSIGNED_BYTE, nm.XSD_NON_POSITIVE_INTEGER, nm.XSD_NEGATIVE_INTEGER,
}
DECIMAL_TYPES = {nm.XSD_DECIMAL, nm.XSD_DOUBLE, nm.XSD_FLOAT}
STRING_TYPES = {
    nm.XSD_STRING, nm.XSD_NORMALIZED_STRING, nm.XSD_TOKEN, nm.XSD_LANGUAGE,
    nm.XSD_NCNAME, nm.XSD_NAME, nm.XSD_NMTOKEN, nm.XSD_ID,
    nm.XSD_ANY_URI, nm.XSD_UNTYPED_ATOMIC,
}
DATE_TYPES = {
    nm.XSD_DATE, nm.XSD_DATETIME, nm.XSD_DATE_TIME_STAMP, nm.XSD_TIME, nm.XSD_GYEAR,
    nm.XSD_GYEAR_MONTH, nm.XSD_GMONTH, nm.XSD_GMONTH_DAY, nm.XSD_GDAY,
    nm.XSD_DURATION,
}
SUPPORTED_PRIMITIVES = (
    INTEGER_TYPES | DECIMAL_TYPES | STRING_TYPES | DATE_TYPES | {nm.XSD_BOOLEAN}
)
INTEGER_BOUNDS = {
    nm.XSD_LONG: (-9223372036854775808, 9223372036854775807),
    nm.XSD_INT: (-2147483648, 2147483647),
    nm.XSD_SHORT: (-32768, 32767),
    nm.XSD_BYTE: (-128, 127),
    nm.XSD_UNSIGNED_LONG: (0, 9223372036854775807),
    nm.XSD_UNSIGNED_INT: (0, 4294967295),
    nm.XSD_UNSIGNED_SHORT: (0, 65535),
    nm.XSD_UNSIGNED_BYTE: (0, 255),
    nm.XSD_NON_NEGATIVE_INTEGER: (0, INF),
    nm.XSD_POSITIVE_INTEGER: (1, INF),
    nm.XSD_NON_POSITIVE_INTEGER: (-INF, 0),
    nm.XSD_NEGATIVE_INTEGER: (-INF, -1),
}
DATE_SAMPLES = {
    nm.XSD_DATE: '2026-01-01',
    nm.XSD_DATETIME: '2026-01-01T00:00:00',
    nm.XSD_DATE_TIME_STAMP: '2026-01-01T00:00:00+00:00',
    nm.XSD_TIME: '00:00:00',
    nm.XSD_GYEAR: '2026',
    nm.XSD_GYEAR_MONTH: '2026-01',
    nm.XSD_GMONTH: '--01',
    nm.XSD_GMONTH_DAY: '--01-01',
    nm.XSD_GDAY: '---01',
    nm.XSD_DURATION: 'P1D',
}


@dataclass
class InstanceResult:
    """Result and explicit status of a bounded XML instance generation attempt."""

    status: str
    element: Optional[ElementTree.Element] = None
    reason: Optional[str] = None
    errors: Optional[list[str]] = None

    @property
    def valid(self) -> bool:
        return self.status == VALID and self.element is not None

    def xml(self, *, indent: Optional[str] = None) -> Optional[str]:
        """Serialize the generated ElementTree element, or return ``None``."""
        if self.element is None:
            return None
        if indent is not None:
            ElementTree.indent(self.element, space=indent)
        return ElementTree.tostring(self.element, encoding='unicode')


class ValueFactory:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.atomic_counters: dict[str, int] = {}
        self.id_counter = 0
        self.key_tuples: dict[object, list[tuple[str, ...]]] = {}
        self.used_key_tuples: dict[object, set[tuple[str, ...]]] = {}

    def make_value(self, xsd_type: XsdSimpleType, *, required: bool = False,
                   fixed: Optional[str] = None) -> str:
        if fixed is not None:
            return fixed
        return self._make_candidate(xsd_type, required=required, identity=False)

    def make_key_tuple(self, identity: XsdIdentity,
                       xsd_types: list[XsdSimpleType]) -> tuple[str, ...]:
        owner = identity.refer if isinstance(identity, XsdKeyref) else identity
        if isinstance(identity, XsdKeyref):
            available = self.key_tuples.get(id(owner), ())
            if not available:
                raise NoSolutionError("keyref has no matching key value")
            return self.rng.choice(tuple(available))
        used = self.used_key_tuples.setdefault(id(identity), set())
        if all(getattr(xsd_type, 'enumeration', None) is not None
               for xsd_type in xsd_types):
            options = [
                [self._to_text(value) for value in xsd_type.enumeration
                 if xsd_type.text_is_valid(self._to_text(value))]
                for xsd_type in xsd_types
            ]
            candidate_space = self._cartesian(options)
            available = [item for item in candidate_space if item not in used]
            if not available:
                raise NoSolutionError("enumeration cannot provide unique identity values")
            candidate = self.rng.choice(available)
        else:
            candidate = tuple(
                self._make_candidate(xsd_type, required=True, identity=True)
                for xsd_type in xsd_types
            )
            for _ in range(99):
                if candidate not in used:
                    break
                candidate = tuple(
                    self._make_candidate(xsd_type, required=True, identity=True)
                    for xsd_type in xsd_types
                )
            if candidate in used:
                raise NoSolutionError("cannot generate unique identity values")
        used.add(candidate)
        self.key_tuples.setdefault(id(identity), []).append(candidate)
        return candidate

    @staticmethod
    def _cartesian(options: list[list[str]]) -> list[tuple[str, ...]]:
        result: list[tuple[str, ...]] = [()]
        for values in options:
            result = [prefix + (value,) for prefix in result for value in values]
        return result

    def _make_candidate(self, xsd_type: XsdSimpleType, *, required: bool,
                        identity: bool) -> str:
        primitive_name = getattr(xsd_type.primitive_type, 'name', None)
        if primitive_name not in SUPPORTED_PRIMITIVES:
            raise UnsupportedSchemaError(f"simple type {primitive_name!r} is not supported")
        enumeration = xsd_type.enumeration
        if enumeration is not None:
            values = [self._to_text(value) for value in enumeration]
            valid = [value for value in values if xsd_type.text_is_valid(value)]
            if not valid:
                raise NoSolutionError("enumeration has no usable value")
            if identity:
                return self.rng.choice(valid)
            return self.rng.choice(valid)
        length = getattr(xsd_type.get_facet(nm.XSD_LENGTH), 'value', None)
        min_length = xsd_type.min_length or 0
        max_length = xsd_type.max_length
        if primitive_name in INTEGER_TYPES:
            return self._integer_value(xsd_type, primitive_name, identity)
        if primitive_name in DECIMAL_TYPES:
            return self._decimal_value(xsd_type, primitive_name, identity)
        if primitive_name == nm.XSD_BOOLEAN:
            return self.rng.choice(('true', 'false'))
        if primitive_name in DATE_TYPES:
            value = DATE_SAMPLES[primitive_name]
            if xsd_type.text_is_valid(value):
                return value
            raise UnsupportedSchemaError(f"date value constraint on {primitive_name!r} is unsupported")
        target_length = self._string_length(length, min_length, max_length, identity)
        if primitive_name == nm.XSD_ID:
            self.id_counter += 1
            value = f"id{self.id_counter}"
            if len(value) < target_length:
                value += 'a' * (target_length - len(value))
            value = value[:target_length] if target_length else value
        elif primitive_name in {nm.XSD_NCNAME, nm.XSD_NAME}:
            base = chr(ord('a') + self.rng.randrange(26))
            alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
            value = base + ''.join(self.rng.choice(alphabet) for _ in range(max(0, target_length - 1)))
        else:
            alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
            value = ''.join(self.rng.choice(alphabet) for _ in range(target_length))
            if not value and not required and not identity and max_length in (None, 0):
                value = ''
        if not xsd_type.text_is_valid(value):
            raise NoSolutionError(f"cannot construct a value for {xsd_type}")
        return value

    def _string_length(self, length: Optional[int], min_length: int,
                       max_length: Optional[int], identity: bool) -> int:
        if length is not None:
            return int(length)
        if identity:
            low = max(min_length, 1)
            high = max(low, min(max_length if max_length is not None else low + 8, low + 8))
            return self.rng.randint(low, high)
        if min_length:
            high = max(min_length, min(max_length if max_length is not None else min_length + 4,
                                       min_length + 4))
            return self.rng.randint(min_length, high)
        return self.rng.randint(1, min(max_length if max_length is not None else 4, 4) or 1)

    def _integer_value(self, xsd_type: XsdSimpleType, primitive: str,
                       identity: bool) -> str:
        lower, upper = INTEGER_BOUNDS.get(primitive, (-INF, INF))
        min_facet = xsd_type.min_value
        max_facet = xsd_type.max_value
        if min_facet is not None:
            lower = max(lower, int(min_facet))
        if max_facet is not None:
            upper = min(upper, int(max_facet))
        if primitive == nm.XSD_POSITIVE_INTEGER:
            lower = max(lower, 1)
        if primitive == nm.XSD_NON_NEGATIVE_INTEGER or primitive in {
            nm.XSD_UNSIGNED_LONG, nm.XSD_UNSIGNED_INT, nm.XSD_UNSIGNED_SHORT,
            nm.XSD_UNSIGNED_BYTE,
        }:
            lower = max(lower, 0)
        if lower > upper:
            raise NoSolutionError("numeric facet range is empty")
        if identity:
            key = primitive
            sequence = self.atomic_counters
            if lower < 0 and upper > 0:
                start = 1
            else:
                start = lower
            if start > upper:
                start = lower
            value = sequence.get(key, start)
            if value < lower:
                value = lower
            elif value > upper:
                value = self.rng.randint(lower, upper)
            sequence[key] = value + 1
            return str(value)
        span = min(max(upper - lower, 0), 10)
        return str(self.rng.randint(lower, lower + span) if upper > lower else lower)

    def _decimal_value(self, xsd_type: XsdSimpleType, primitive: str,
                       identity: bool) -> str:
        total_digits = getattr(xsd_type.get_facet(nm.XSD_TOTAL_DIGITS), 'value', None)
        fraction_digits = getattr(xsd_type.get_facet(nm.XSD_FRACTION_DIGITS), 'value', 0)
        if xsd_type.min_value is not None or xsd_type.max_value is not None:
            lower = Decimal(xsd_type.min_value) \
                if xsd_type.min_value is not None else Decimal('0')
            upper = Decimal(xsd_type.max_value) \
                if xsd_type.max_value is not None else lower + 10
            if lower > upper:
                raise NoSolutionError("numeric facet range is empty")
            value = lower
            if total_digits is not None and not xsd_type.text_is_valid(format(value, 'f')):
                magnitude = Decimal(10) ** max(int(total_digits) - 1, 0)
                rounded = (lower / magnitude).to_integral_value() * magnitude
                value = rounded if rounded >= lower else rounded + magnitude
                if value > upper:
                    raise NoSolutionError("cannot satisfy numeric digits and range")
        elif identity:
            value = Decimal(self.atomic_counters.get(nm.XSD_DECIMAL, 1))
            self.atomic_counters[nm.XSD_DECIMAL] = int(value) + 1
        elif fraction_digits:
            value = Decimal(self.rng.randint(1, 9)) / (Decimal(10) ** int(fraction_digits))
        else:
            value = Decimal(self.rng.randint(1, 5))
        text = format(value, 'f')
        if not xsd_type.text_is_valid(text):
            raise NoSolutionError("cannot construct a decimal value")
        return text

    @staticmethod
    def _to_text(value: Any) -> str:
        if value is True:
            return 'true'
        if value is False:
            return 'false'
        return str(value)


class XMLInstanceGenerator:
    def __init__(self, schema: Any, *, seed: int = 0, max_nodes: int = 32):
        self.schema = schema
        self.rng = random.Random(seed)
        self.max_nodes = max_nodes
        self.budget = max_nodes
        self.values = ValueFactory(self.rng)
        self.force_tree = ForceTree()
        self.root_element: Optional[XsdElement] = None
        self.element_nodes: list[tuple[ElementTree.Element, XsdElement]] = []
        self.visited_simple: set[int] = set()

    def generate(self, path: Optional[str] = None) -> InstanceResult:
        root = self._select_root(path)
        self.root_element = root
        try:
            self._scan_element(root, set())
            for identity in root.identities:
                self.force_tree.mark_identity(identity)
            self.force_tree.compile(root)
            minimum = self._element_min_cost(root, self.force_tree.root, set(), 0)
            if minimum == INF:
                return InstanceResult(NO_SOLUTION, reason="the schema has no finite valid branch")
            if minimum > self.max_nodes:
                return InstanceResult(
                    BUDGET_EXHAUSTED,
                    reason=f"node budget of {self.max_nodes} is below required {minimum} nodes"
                )
            self.budget = self.max_nodes - 1
            element = self._generate_element(root, self.force_tree.root, set(), 0, selected=True)
            self._fill_identities(element)
            errors = [str(error) for error in schema_iter_errors(self.schema, element)]
            if errors:
                return InstanceResult(VALIDATION_FAILED, element=element,
                                      reason="generated candidate failed schema validation",
                                      errors=errors)
            return InstanceResult(VALID, element=element)
        except UnsupportedSchemaError as exc:
            return InstanceResult(UNSUPPORTED, reason=str(exc))
        except NoSolutionError as exc:
            return InstanceResult(NO_SOLUTION, reason=str(exc))
        except BudgetExhaustedError:
            return InstanceResult(BUDGET_EXHAUSTED,
                                  reason=f"node budget of {self.max_nodes} was exhausted")

    def _select_root(self, path: Optional[str]) -> XsdElement:
        if path is not None:
            selected = self.schema.find(path)
            if not isinstance(selected, XsdElement):
                raise NoSolutionError(f"path {path!r} does not select a global element")
            return selected
        roots = self.schema.root_elements
        if len(roots) != 1:
            raise NoSolutionError("a root element path is required when the schema has "
                                  "more than one possible document element")
        return roots[0]

    def _scan_element(self, element: XsdElement, seen: set[int]) -> None:
        actual = element.ref if element.ref is not None else element
        if actual.abstract:
            raise UnsupportedSchemaError("abstract elements are not supported")
        if getattr(actual, 'substitutes', ()) or getattr(actual, 'alternatives', ()):
            raise UnsupportedSchemaError("substitution groups and type alternatives are not supported")
        if id(actual) in seen:
            return
        seen = seen | {id(actual)}
        if element.fixed is not None and not element.type.text_is_valid(element.fixed):
            raise NoSolutionError(f"fixed value of {element.local_name!r} is not valid")
        self._scan_type(actual.type, seen)

    def _scan_type(self, xsd_type: Any, seen: set[int]) -> None:
        if isinstance(xsd_type, XsdSimpleType):
            self._scan_simple_type(xsd_type, seen)
        elif isinstance(xsd_type, XsdComplexType):
            if xsd_type.abstract:
                raise UnsupportedSchemaError("abstract complex types are not supported")
            if xsd_type.assertions:
                raise UnsupportedSchemaError("xs:assert assertions are not supported")
            if xsd_type.mixed:
                raise UnsupportedSchemaError("mixed content is not supported")
            if xsd_type.open_content is not None:
                raise UnsupportedSchemaError("XSD 1.1 open content is not supported")
            for attr in xsd_type.attributes.values():
                if isinstance(attr, XsdAnyAttribute):
                    raise UnsupportedSchemaError("xs:anyAttribute is not supported")
                self._scan_simple_type(attr.type, seen)
                if attr.fixed is not None and not attr.type.text_is_valid(attr.fixed):
                    raise NoSolutionError(f"fixed value of attribute {attr.local_name!r} is invalid")
            if isinstance(xsd_type.content, XsdSimpleType):
                self._scan_simple_type(xsd_type.content, seen)
            elif isinstance(xsd_type.content, XsdGroup):
                self._scan_group(xsd_type.content, seen)
            if xsd_type.base_type is not None and getattr(xsd_type.base_type, 'parent', None) is not None:
                self._scan_type(xsd_type.base_type, seen)
        else:
            raise UnsupportedSchemaError(f"unsupported XSD type {type(xsd_type).__name__}")

    def _scan_group(self, group: XsdGroup, seen: set[int]) -> None:
        if group.ref is not None and len(group) == 1:
            self._scan_group(group.ref, seen)
            return
        for particle in group:
            if isinstance(particle, XsdAnyElement):
                raise UnsupportedSchemaError("xs:any content wildcards are not supported")
            if isinstance(particle, XsdElement):
                self._scan_element(particle, seen)
            elif isinstance(particle, XsdGroup):
                self._scan_group(particle, seen)

    def _scan_simple_type(self, xsd_type: XsdSimpleType, seen: set[int]) -> None:
        if id(xsd_type) in self.visited_simple:
            return
        self.visited_simple.add(id(xsd_type))
        if isinstance(xsd_type, XsdList):
            raise UnsupportedSchemaError("xs:list simple types are not supported")
        if isinstance(xsd_type, XsdUnion):
            raise UnsupportedSchemaError("xs:union simple types are not supported")
        primitive = getattr(xsd_type, 'primitive_type', None)
        if primitive is None or primitive.name not in SUPPORTED_PRIMITIVES:
            raise UnsupportedSchemaError(
                f"simple type {getattr(primitive, 'local_name', None)!r} is not supported"
            )
        if xsd_type.get_facet(nm.XSD_PATTERN) is not None:
            raise UnsupportedSchemaError("xs:pattern constraints are not supported")
        if xsd_type.get_facet(nm.XSD_ASSERTION) is not None:
            raise UnsupportedSchemaError("simple type xs:assertion constraints are not supported")
        if xsd_type.get_facet(nm.XSD_EXPLICIT_TIMEZONE) is not None:
            raise UnsupportedSchemaError("xs:explicitTimezone constraints are not supported")
        if primitive.name in DATE_TYPES and (xsd_type.min_value is not None or
                                             xsd_type.max_value is not None):
            raise UnsupportedSchemaError("numeric range facets on date/time types are unsupported")
        if xsd_type.is_empty() and not xsd_type.enumeration:
            raise NoSolutionError("a required simple type only permits the empty string")
        base_type = getattr(xsd_type, 'base_type', None)
        if base_type is not None and getattr(base_type, 'parent', None) is not None:
            self._scan_simple_type(base_type, seen)

    def _element_min_cost(self, element: XsdElement, node: ForceNode,
                          active: set[int], force_count: int) -> int:
        actual = element.ref if element.ref is not None else element
        active_id = id(actual.type)
        forced = bool(node.children or node.fields or node.attributes or
                      node.text_fields or node.choice_branches)
        if active_id in active and forced:
            if force_count >= 1:
                return INF
            force_count += 1
            next_active = active
        else:
            next_active = active | {active_id}
        cost = 1
        attr_fields = self.force_tree.element_attributes.get(id(element), {})
        if not attr_fields and element.ref is not None:
            attr_fields = self.force_tree.element_attributes.get(id(element.ref), {})
        if isinstance(actual.type, XsdSimpleType):
            required_text = actual.value_constraint is None and (
                actual.min_occurs > 0 or forced or node.fields or node.text_fields)
            if actual.type.is_empty() and required_text:
                return INF
            return cost
        if isinstance(actual.type, XsdComplexType):
            for attr in actual.type.attributes.values():
                if isinstance(attr, XsdAttribute) and (
                        attr.is_required() or id(attr) in attr_fields):
                    if attr.type.is_empty() and attr.value_constraint is None:
                        return INF
            if isinstance(actual.type.content, XsdSimpleType):
                if actual.type.content.is_empty() and actual.value_constraint is None and forced:
                    return INF
                return cost
            content = actual.type.content
            if not isinstance(content, XsdGroup):
                return cost
            return cost + self._group_min_cost(content, node, next_active, force_count)
        return INF

    def _group_min_cost(self, group: XsdGroup, node: ForceNode,
                        active: set[int], force_count: int) -> int:
        if group.max_occurs == 0:
            return 0
        if group.ref is not None and len(group) == 1:
            return self._group_min_cost(group.ref, node, active, force_count)
        if group.model == 'choice':
            forced = bool(node.choice_branches.get(id(group)))
            if forced:
                branch_nodes = node.choice_branches[id(group)]
                values = [self._choice_branch_cost(group, branch, active, force_count)
                          for branch in branch_nodes]
                return min(values) if values else INF
            if group.min_occurs == 0:
                return 0
            values = [self._particle_cost(particle, ForceNode(), active, force_count, 1)
                      for particle in group]
            return min(values) if values else INF
        return self._sequence_cost(group, node, active, force_count)

    def _choice_branch_cost(self, group: XsdGroup, node: ForceNode,
                            active: set[int], force_count: int) -> int:
        total = 0
        for particle in group:
            particle_node = self._particle_node(particle, node)
            forced = bool(particle_node.children or particle_node.fields or
                          particle_node.attributes or particle_node.text_fields or
                          particle_node.choice_branches)
            if forced:
                total += self._particle_cost(
                    particle, particle_node, active, force_count, 1
                )
        return total

    def _sequence_cost(self, group: XsdGroup, node: ForceNode,
                       active: set[int], force_count: int) -> int:
        return sum(self._particle_cost(particle, self._particle_node(particle, node),
                                       active, force_count, particle.min_occurs)
                   for particle in group)

    def _particle_node(self, particle: Any, parent: ForceNode) -> ForceNode:
        nodes = parent.children.get(id(particle), [])
        if not nodes and isinstance(particle, XsdElement) and particle.ref is not None:
            nodes = parent.children.get(id(particle.ref), [])
        return merge_nodes(nodes) if nodes else ForceNode()

    def _particle_cost(self, particle: Any, node: ForceNode,
                       active: set[int], force_count: int, min_occurrences: int) -> int:
        if isinstance(particle, XsdAnyElement):
            return INF
        required = min_occurrences > 0
        forced = bool(node.children or node.fields or node.attributes or
                      node.text_fields or node.choice_branches)
        if isinstance(particle, XsdGroup):
            required = required or forced
            if not required:
                return 0
            return min_occurrences * self._group_min_cost(
                particle, node, active, force_count
            )
        if isinstance(particle, XsdElement):
            actual = particle.ref if particle.ref is not None else particle
            if not required and not forced:
                return 0
            if active:
                pass
            return min_occurrences * self._element_min_cost(
                particle, node, active, force_count
            )
        return INF

    def _generate_element(self, element: XsdElement, node: ForceNode,
                          active: set[int], force_count: int, *,
                          selected: bool = False) -> ElementTree.Element:
        actual = element.ref if element.ref is not None else element
        forced = bool(node.children or node.fields or node.attributes or
                      node.text_fields or node.choice_branches)
        self._spend(1)
        xml_element = ElementTree.Element(element.name if element.ref is None else actual.name)
        if selected:
            self.element_nodes.append((xml_element, actual))
        attr_fields = self.force_tree.element_attributes.get(id(actual), {})
        if isinstance(actual.type, XsdComplexType):
            self._generate_attributes(actual, xml_element, attr_fields)
            type_id = id(actual.type)
            recursive = type_id in active and forced
            if recursive and force_count >= 1 and actual.nillable:
                xml_element.set(XSI_NIL, 'true')
                return xml_element
            next_count = force_count + 1 if type_id in active and forced else force_count
            next_active = active | {type_id}
            content = actual.type.content
            if isinstance(content, XsdGroup) and len(content):
                self._generate_group(content, node, xml_element, next_active, next_count)
            elif isinstance(content, XsdSimpleType):
                self._generate_text(actual, xml_element, node)
        else:
            self._generate_text(actual, xml_element, node)
        return xml_element

    def _spend(self, amount: int) -> None:
        if amount > self.budget:
            raise BudgetExhaustedError()
        self.budget -= amount

    def _generate_attributes(self, element: XsdElement, xml_element: ElementTree.Element,
                             field_map: dict[int, list[FieldTarget]]) -> None:
        if not isinstance(element.type, XsdComplexType):
            return
        for attr in element.type.attributes.values():
            if not isinstance(attr, XsdAttribute):
                continue
            targets = field_map.get(id(attr), [])
            if not targets and attr.ref is not None:
                targets = field_map.get(id(attr.ref), [])
            if attr.use == 'prohibited':
                continue
            if attr.fixed is not None:
                xml_element.set(attr.name, attr.fixed)
            elif targets:
                continue
            elif attr.default is not None and not attr.is_required() and not targets:
                continue
            elif attr.is_required():
                value = self.values.make_value(attr.type, required=True)
                xml_element.set(attr.name, value)

    def _generate_text(self, element: XsdElement, xml_element: ElementTree.Element,
                       node: ForceNode) -> None:
        if element.fixed is not None:
            xml_element.text = element.fixed
            return
        if element.default is not None and not node.fields and not node.text_fields:
            return
        required = element.min_occurs > 0 or bool(node.fields or node.text_fields)
        if not required and self.rng.choice((False, True)):
            return
        if node.fields or node.text_fields:
            return
        else:
            xml_element.text = self.values.make_value(element.type, required=True)

    def _generate_group(self, group: XsdGroup, parent_node: ForceNode,
                        parent_xml: ElementTree.Element, active: set[int],
                        force_count: int) -> None:
        if group.max_occurs == 0:
            return
        if group.ref is not None and len(group) == 1:
            self._generate_group(group.ref, parent_node, parent_xml, active, force_count)
            return
        if group.model == 'choice':
            branch_nodes = parent_node.choice_branches.get(id(group), [])
            if branch_nodes:
                costs = [self._choice_branch_cost(group, node, active, force_count)
                         for node in branch_nodes]
                selected_index = self._choose_index(costs, self.budget)
                self._generate_choice_branch(
                    group, branch_nodes[selected_index], parent_xml,
                    active, force_count
                )
                return
            if group.min_occurs == 0:
                return
            costs = [self._particle_cost(particle, ForceNode(), active, force_count, 1)
                     for particle in group]
            selected_index = self._choose_index(costs, self.budget)
            occurrences = max(group.min_occurs, 1)
            self._generate_particle(group[selected_index], ForceNode(), parent_xml,
                                    active, force_count, occurrences)
            return
        self._generate_sequence(group, parent_node, parent_xml, active, force_count)

    def _generate_choice_branch(self, group: XsdGroup, node: ForceNode,
                           parent_xml: ElementTree.Element, active: set[int],
                           force_count: int) -> None:
        for particle in group:
            particle_node = self._particle_node(particle, node)
            forced = bool(particle_node.children or particle_node.fields or
                          particle_node.attributes or particle_node.text_fields or
                          particle_node.choice_branches)
            if forced:
                self._generate_particle(particle, particle_node, parent_xml,
                                        active, force_count, 1)

    def _generate_sequence(self, group: XsdGroup, node: ForceNode,
                           parent_xml: ElementTree.Element, active: set[int],
                           force_count: int) -> None:
        for particle in group:
            particle_node = self._particle_node(particle, node)
            forced = bool(particle_node.children or particle_node.fields or
                          particle_node.attributes or particle_node.text_fields or
                          particle_node.choice_branches)
            occurrences = particle.min_occurs
            if isinstance(particle, XsdElement) and particle.max_occurs is None and forced:
                occurrences = max(occurrences, 1)
            if isinstance(particle, XsdGroup) and forced and occurrences == 0:
                occurrences = 1
            if occurrences:
                self._generate_particle(particle, particle_node, parent_xml,
                                        active, force_count, occurrences)

    def _generate_particle(self, particle: Any, node: ForceNode,
                           parent_xml: ElementTree.Element, active: set[int],
                           force_count: int, occurrences: int) -> None:
        if isinstance(particle, XsdElement):
            for _ in range(occurrences):
                child = self._generate_element(particle, node, active, force_count)
                parent_xml.append(child)
                actual = particle.ref if particle.ref is not None else particle
                self.element_nodes.append((child, actual))
        elif isinstance(particle, XsdGroup):
            for _ in range(occurrences):
                self._generate_group(particle, node, parent_xml, active, force_count)

    def _choose_index(self, costs: list[int], budget: int) -> int:
        affordable = [index for index, cost in enumerate(costs) if cost <= budget]
        if not affordable:
            raise BudgetExhaustedError()
        minimum = min(costs[index] for index in affordable)
        candidates = [index for index in affordable if costs[index] == minimum]
        return self.rng.choice(candidates)

    def _fill_identities(self, root_xml: ElementTree.Element) -> None:
        if not self.root_element:
            return
        ordered_identities = sorted(
            self.root_element.identities,
            key=lambda item: 1 if isinstance(item, XsdKeyref) else 0
        )
        for identity in ordered_identities:
            selected_map = identity.elements
            for schema_element, field_selectors in selected_map.items():
                for xml_element, bound_schema in self.element_nodes:
                    if bound_schema is not schema_element and \
                            bound_schema is not schema_element.ref:
                        continue
                    xsd_types: list[XsdSimpleType] = []
                    for field_selector in field_selectors:
                        decoder = field_selector.decoders[0]
                        xsd_type = getattr(decoder, 'type', None)
                        if not isinstance(xsd_type, XsdSimpleType):
                            raise UnsupportedSchemaError(
                                "identity field must select a simple typed node"
                            )
                        xsd_types.append(xsd_type)
                    values = self.values.make_key_tuple(identity, xsd_types)
                    for index, field_selector in enumerate(field_selectors):
                        target = FieldTarget(identity, index, True)
                        self._assign_field(
                            xml_element, field_selector.field.path,
                            field_selector.decoders, target, values[index]
                        )

    def _assign_field(self, xml_element: ElementTree.Element, path: str,
                      decoders: list[Any], target: FieldTarget,
                      value: str) -> None:
        segments = parse_field_path(path, target.identity)
        current_xml = xml_element
        decoder_index = 0
        for index, (name, is_attr) in enumerate(segments):
            final = index == len(segments) - 1
            if is_attr:
                decoder = decoders[decoder_index] if decoder_index < len(decoders) else None
                if not isinstance(decoder, XsdAttribute):
                    raise UnsupportedSchemaError(f"identity attribute {name!r} was not resolved")
                current_xml.set(decoder.name, value)
                return
            child_xml = self._find_xml_child(current_xml, name)
            decoder = decoders[decoder_index] if decoder_index < len(decoders) else None
            if not isinstance(decoder, XsdElement):
                raise UnsupportedSchemaError(f"identity field {name!r} was not resolved")
            decoder_index += 1
            if final:
                child_xml.text = value
                return
            current_xml = child_xml
        if not segments:
            decoder = decoders[0] if decoders else None
            xsd_type = getattr(decoder, 'type', None)
            if not isinstance(xsd_type, XsdSimpleType):
                raise UnsupportedSchemaError("identity self field was not resolved")
            current_xml.text = value

    @staticmethod
    def _find_xml_child(xml_parent: ElementTree.Element,
                        name: str) -> ElementTree.Element:
        for child_xml in list(xml_parent):
            if local_name(child_xml.tag) == name:
                return child_xml
        raise NoSolutionError(f"identity field path child {name!r} was not generated")


def schema_iter_errors(schema: Any, element: ElementTree.Element):
    return schema.iter_errors(element)


def generate_xml_instance(schema: Any, *, path: Optional[str] = None,
                          seed: int = 0, max_nodes: int = 32) -> InstanceResult:
    """Generate and validate a small XML instance from an XSD schema."""
    return XMLInstanceGenerator(schema, seed=seed, max_nodes=max_nodes).generate(path)
