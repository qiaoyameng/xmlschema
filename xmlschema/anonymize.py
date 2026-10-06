"""
Identity-constraint based anonymization of XML instance documents.

This module replaces business key values (the values collected by XSD
``xs:key``/``xs:unique`` identity constraints) while keeping the
``xs:keyref`` graph intact. For each identity constraint a bijective
mapping is established within the constraint's scope: equal key tuples get
equal replacements, distinct key tuples get distinct replacements, and each
referencing tuple is rewritten to the replacement of the tuple it points at.

Replacements are either provided by the caller or produced by a restricted
generator. Every candidate is validated against the field's XSD simple type,
so the output keeps satisfying the original field types and facets.

The operation is transactional: an input validation failure, a target value
collision, a generator that cannot produce a legal value, or a keyref that
cannot be resolved raises :class:`AnonymizationError` *before* the output
document is produced, so an instance is never anonymized halfway.
"""
import copy
import json
import logging
import random
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from elementpath import AttributeNode, ElementNode, XPathContext
from elementpath.xpath_nodes import EtreeElementNode

from xmlschema.resources import XMLResource
from xmlschema.validators.identities import \
    XsdIdentity, XsdKey, XsdKeyref, XsdUnique
from xmlschema.validators.simple_types import XsdSimpleType

__all__ = [
    'AnonymizationError', 'AnonymizationReceipt', 'FieldSpec',
    'IdentityReceipt', 'RestrictedValueGenerator', 'anonymize',
    'reference_graph_fingerprint',
]

logger = logging.getLogger('xmlschema.anonymize')

# A lexical value of one field of an identity tuple.
FieldValue = str
# The full tuple of field values (composite keys are handled as a whole).
KeyValueTuple = tuple[FieldValue, ...]

ReplacementTable = Mapping[
    Union[str, KeyValueTuple], Union[str, KeyValueTuple]]


class AnonymizationError(ValueError):
    """Raised when an anonymization run cannot be completed atomically."""


@dataclass(frozen=True)
class FieldSpec:
    """Type information for one field of an identity constraint."""

    xsd_type: XsdSimpleType
    path: str
    is_attribute: bool

    @property
    def type_name(self) -> str:
        root = self.xsd_type.root_type
        if root.name:
            return f'xs:{root.local_name}'
        return self.xsd_type.display_name

    def validate(self, lexical: str) -> None:
        """Validate a candidate lexical value against type and facets."""
        errors = list(self.xsd_type.iter_errors(lexical))
        if errors:
            reason = str(errors[0]).splitlines()[0]
            raise AnonymizationError(
                f"replacement {lexical!r} for field {self.path!r} is not valid "
                f"for {self.type_name}: {reason}"
            )


@dataclass
class IdentityReceipt:
    """
    Controlled receipt for one anonymized identity constraint.

    Mappings are kept per scope (indexed by scope ordinal) because the same
    textual key in two scopes is allowed to map to different replacements;
    the bijection is only required *within* a scope.

    Original key values are never written to the logs. They are only
    materialized when the receipt is serialized with ``reveal_original=True``
    on a secured channel; the default serialization reports counts and
    replacement values only.
    """

    identity: str
    kind: str
    fields: tuple[str, ...]
    scopes: dict[int, dict[KeyValueTuple, KeyValueTuple]]
    generated: bool

    @property
    def key_count(self) -> int:
        return sum(len(mapping) for mapping in self.scopes.values())

    @property
    def flat_mapping(self) -> dict[KeyValueTuple, KeyValueTuple]:
        """The mapping of the single-scope (common) case."""
        if len(self.scopes) != 1:
            raise AnonymizationError(
                f"{self.identity!r} has {len(self.scopes)} scopes; use "
                f"the per-scope 'scopes' attribute instead of flat_mapping"
            )
        return next(iter(self.scopes.values()))

    def to_dict(self, reveal_original: bool = False) -> dict[str, Any]:
        serialized_scopes = []
        for scope_index in sorted(self.scopes):
            mapping = self.scopes[scope_index]
            if reveal_original:
                pairs = [
                    {'original': list(old), 'replacement': list(new)}
                    for old, new in sorted(mapping.items())
                ]
            else:
                pairs = [
                    {'replacement': list(new)}
                    for new in sorted(mapping.values())
                ]
            serialized_scopes.append(
                {'scope': scope_index, 'mappings': pairs})
        return {
            'identity': self.identity,
            'kind': self.kind,
            'fields': list(self.fields),
            'generated': self.generated,
            'key_count': self.key_count,
            'scope_count': len(serialized_scopes),
            'scope_mappings': serialized_scopes,
        }


@dataclass
class AnonymizationReceipt:
    """The controlled mapping receipt of one anonymization run."""

    scopes: dict[str, IdentityReceipt] = field(default_factory=dict)

    def to_dict(self, reveal_original: bool = False) -> dict[str, Any]:
        return {
            'scope_count': len(self.scopes),
            'scopes': [r.to_dict(reveal_original) for r in self.scopes.values()],
        }

    def to_json(self, reveal_original: bool = False, indent: int = 2) -> str:
        return json.dumps(self.to_dict(reveal_original), indent=indent,
                          ensure_ascii=False)


# --- Restricted value generator ------------------------------------------

_DIGITS = '0123456789'
_UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_LOWER = 'abcdefghijklmnopqrstuvwxyz'
_ALNUM = _UPPER + _LOWER + _DIGITS

# Primitive XSD types the built-in restricted generator knows how to build
# legal values for. Anything outside this set requires a caller generator.
_GENERATABLE_TYPES = frozenset({
    'string', 'normalizedString', 'token', 'language', 'NMTOKEN', 'Name',
    'NCName', 'ID', 'IDREF', 'ENTITY',
    'integer', 'positiveInteger', 'nonNegativeInteger', 'negativeInteger',
    'nonPositiveInteger', 'long', 'int', 'short', 'byte',
    'unsignedLong', 'unsignedInt', 'unsignedShort', 'unsignedByte',
    'decimal',
})




def _effective_builtin_name(xsd_type: XsdSimpleType) -> str:
    """
    Walk the restriction chain to the first named built-in type, so an
    anonymous restriction of xs:positiveInteger is treated as an integer.
    """
    xsd_ns = 'http://www.w3.org/2001/XMLSchema'
    candidate = xsd_type
    seen = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        name = getattr(candidate, 'name', None)
        local_name = getattr(candidate, 'local_name', None)
        # Only built-in XSD types anchor the generation strategy; named
        # user types are skipped and their base type is inspected instead.
        if name and getattr(name, 'startswith', lambda _: False)(
                '{'+xsd_ns+'}') and local_name in _GENERATABLE_TYPES:
            return local_name
        candidate = getattr(candidate, 'base_type', None)
    root_name = getattr(getattr(xsd_type, 'root_type', None),
                        'local_name', 'string')
    return root_name if root_name in _GENERATABLE_TYPES else 'string' 



_XSD = 'http://www.w3.org/2001/XMLSchema'


def _iter_facet_chain(xsd_type: XsdSimpleType):
    """Yield (constraint-name, facet) pairs along the restriction chain."""
    candidate = xsd_type
    seen = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        for key, facet in getattr(candidate, 'facets', {}).items():
            if isinstance(key, str):
                yield key, facet
        candidate = getattr(candidate, 'base_type', None)


def _facet_value(xsd_type: XsdSimpleType, local_name: str,
                 default=None):
    key = '{%s}%s' % (_XSD, local_name)
    for facet_key, facet in _iter_facet_chain(xsd_type):
        if facet_key == key and hasattr(facet, 'value'):
            return facet.value
    return default


def _facet_object(xsd_type: XsdSimpleType, local_name: str):
    key = '{%s}%s' % (_XSD, local_name)
    for facet_key, facet in _iter_facet_chain(xsd_type):
        if facet_key == key:
            return facet
    return None


class RestrictedValueGenerator:
    """
    Generate candidate replacement values, checking them against the field
    type and facets afterwards.

    The generator is deliberately conservative and facet-aware: it handles
    enumerations, length bounds, numeric ranges and a useful subset of regex
    patterns; every candidate still has to pass the full XSD type
    validation. A fixed ``seed`` makes runs reproducible.
    """

    def __init__(self, seed: Optional[int] = None,
                 prefix: str = 'anon',
                 max_attempts: int = 10_000):
        self._rng = random.Random(seed)
        self._prefix = prefix
        self._max_attempts = max_attempts

    def generate(self, spec: FieldSpec, *, reserved: set[str],
                 immutable: set[str]) -> str:
        xsd_type = spec.xsd_type
        local_name = _effective_builtin_name(xsd_type)
        if local_name == 'anySimpleType':
            local_name = 'string'
        if local_name not in _GENERATABLE_TYPES:
            raise AnonymizationError(
                f"the restricted generator cannot produce values for "
                f"{spec.type_name} (field {spec.path!r}); provide a custom "
                f"generator or an explicit replacement table"
            )

        enumeration_facet = _facet_object(xsd_type, 'enumeration')
        enumeration = getattr(enumeration_facet, 'enumeration', None)
        if enumeration:
            choices = [str(value) for value in enumeration]
            self._rng.shuffle(choices)
            for candidate in choices:
                if candidate not in reserved and candidate not in immutable:
                    spec.validate(candidate)
                    return candidate
            raise AnonymizationError(
                f"every enumerated value of {spec.type_name} "
                f"(field {spec.path!r}) is already in use"
            )

        for _ in range(self._max_attempts):
            candidate = self._raw_candidate(local_name, xsd_type)
            if candidate in reserved or candidate in immutable:
                continue
            try:
                spec.validate(candidate)
            except AnonymizationError:
                continue
            return candidate

        raise AnonymizationError(
            f"unable to generate a value valid for {spec.type_name} "
            f"(field {spec.path!r}) after {self._max_attempts} attempts"
        )

    def _length_bounds(self, xsd_type: Any) -> tuple[Optional[int],
                                                      Optional[int]]:
        fixed = _facet_value(xsd_type, 'length')
        if fixed is not None:
            return int(fixed), int(fixed)
        minimum = _facet_value(xsd_type, 'minLength')
        maximum = _facet_value(xsd_type, 'maxLength')
        return (int(minimum) if minimum is not None else None,
                int(maximum) if maximum is not None else None)

    def _numeric_bounds(self, xsd_type: Any, local_name: str) \
            -> tuple[Optional[int], Optional[int]]:
        minimum: Optional[int] = None
        maximum: Optional[int] = None
        for facet_name, is_min, exclusive in (
                ('minInclusive', True, False),
                ('minExclusive', True, True),
                ('maxInclusive', False, False),
                ('maxExclusive', False, True)):
            value = _facet_value(xsd_type, facet_name)
            if value is None:
                continue
            value = int(value)
            if is_min:
                bounded = value + (1 if exclusive else 0)
                minimum = bounded if minimum is None else max(minimum, bounded)
            else:
                bounded = value - (1 if exclusive else 0)
                maximum = bounded if maximum is None else min(maximum, bounded)

        builtin = {
            'byte': (-128, 127), 'short': (-32768, 32767),
            'int': (-2147483648, 2147483647),
            'unsignedByte': (0, 255), 'unsignedShort': (0, 65535),
            'unsignedInt': (0, 4294967295),
            'positiveInteger': (1, None),
            'nonNegativeInteger': (0, None),
            'negativeInteger': (None, -1),
            'nonPositiveInteger': (None, 0),
        }
        if local_name in builtin:
            lo, hi = builtin[local_name]
            if lo is not None:
                minimum = lo if minimum is None else max(minimum, lo)
            if hi is not None:
                maximum = hi if maximum is None else min(maximum, hi)
        return minimum, maximum

    def _raw_candidate(self, local_name: str, xsd_type: Any) -> str:
        rng = self._rng
        integer_types = ('integer', 'long', 'int', 'short', 'byte',
                         'negativeInteger', 'nonPositiveInteger',
                         'positiveInteger', 'nonNegativeInteger',
                         'unsignedLong', 'unsignedInt', 'unsignedShort',
                         'unsignedByte')
        if local_name in integer_types:
            minimum, maximum = self._numeric_bounds(xsd_type, local_name)
            lo = minimum if minimum is not None else -1_000_000_000
            hi = maximum if maximum is not None else 1_000_000_000
            if lo > hi:
                raise AnonymizationError(
                    f"numeric bounds of {xsd_type.display_name} leave no "
                    f"integers available"
                )
            return str(rng.randint(lo, hi))
        if local_name == 'decimal':
            return f'{rng.randint(-1_000_000, 1_000_000)}.{rng.randint(0, 99):02d}'

        minimum, maximum = self._length_bounds(xsd_type)
        lo = minimum if minimum is not None else 8
        hi = maximum if maximum is not None else max(lo, 16)
        if lo > hi:
            raise AnonymizationError(
                f"length bounds of {xsd_type.display_name} are inconsistent"
            )

        if local_name in ('Name', 'NCName', 'ID', 'IDREF', 'ENTITY'):
            length = rng.randint(lo, hi)
            body = ''.join(rng.choice(_ALNUM) for _ in range(length - 1))
            return f'{rng.choice(_UPPER + "_")}{body}'
        if local_name == 'language':
            return rng.choice(('en', 'it', 'fr', 'de', 'es', 'zh'))

        patterns_facet = _facet_object(xsd_type, 'pattern')
        if patterns_facet is not None and getattr(patterns_facet,
                                                   'patterns', None):
            synthesized = _synthesize_from_pattern(
                patterns_facet.patterns, rng)
            if synthesized is not None:
                return synthesized

        length = rng.randint(lo, hi)
        return ''.join(rng.choice(_UPPER + _DIGITS) for _ in range(length))


def _expand_class(body: str) -> Optional[str]:
    ranges = {'A-Z': _UPPER, 'a-z': _LOWER, '0-9': _DIGITS}
    chars: list[str] = []
    index = 0
    while index < len(body):
        if index + 2 < len(body) and body[index + 1] == '-':
            expanded = ranges.get(body[index:index + 3])
            if expanded is None:
                return None
            chars.extend(expanded)
            index += 3
        else:
            chars.append(body[index])
            index += 1
    return ''.join(dict.fromkeys(chars))


def _parse_simple_pattern(regex: str, rng: random.Random) \
        -> Optional[list[tuple[str, int]]]:
    """
    Parse a small pattern subset: literals and character classes, each with
    an optional ``{n}``, ``{n,m}``, ``*`` or ``+`` quantifier. Unsupported
    constructs return ``None`` so the caller can use plain retry filtering.
    """
    core = regex
    # elementpath compiles XSD patterns wrapped as ^(?:PATTERN)$(?!\n\Z)
    m = re.match(r'^\^\(\?:(.*)\)\$\(\?!.*\)\s*$', core, re.S)
    if m:
        core = m.group(1)
    else:
        core = re.sub(r'\(\?!.*\)\s*$', '', core)
        if core.startswith('^(?:'):
            core = core[4:]
        if core.endswith(')$'):
            core = core[:-2]

    tokens: list[tuple[str, int]] = []
    index = 0
    while index < len(core):
        ch = core[index]
        repeat = 1
        if ch == '[':
            close = core.find(']', index + 1)
            if close == -1:
                return None
            chars = _expand_class(core[index + 1:close])
            if chars is None:
                return None
            index = close + 1
        elif ch == '\\' and index + 1 < len(core):
            escaped = core[index:index + 2]
            mapping = {'\\d': _DIGITS, '\\w': _ALNUM + '_', '\\s': ' '}
            chars = mapping.get(escaped)
            if chars is None:
                return None
            index += 2
        elif ch in '.()|?^$':
            return None
        else:
            chars = ch
            index += 1

        if index < len(core) and core[index] == '{':
            close = core.find('}', index)
            if close == -1:
                return None
            spec = core[index + 1:close]
            try:
                if ',' in spec:
                    start, end = spec.split(',')
                    low = int(start) if start else 0
                    high = int(end) if end else max(low, 8)
                    repeat = rng.randint(low, high)
                else:
                    repeat = int(spec)
            except ValueError:
                return None
            index = close + 1
        elif index < len(core) and core[index] in '*+':
            repeat = rng.randint(1, 8)
            index += 1
        tokens.append((chars, repeat))
    return tokens


def _synthesize_from_pattern(patterns: Any, rng: random.Random) \
        -> Optional[str]:
    try:
        compiled = patterns[0]
        regex = compiled.pattern if hasattr(compiled, 'pattern')             else compiled
        tokens = _parse_simple_pattern(regex, rng)
    except (AttributeError, IndexError, TypeError):
        return None
    if tokens is None:
        return None
    parts = []
    for chars, repeat in tokens:
        parts.append(''.join(rng.choice(chars) for _ in range(repeat)))
    return ''.join(parts)


# --- Identity data extraction --------------------------------------------

@dataclass
class _FieldOccurrence:
    """One selected field node plus its field type information."""

    node: Any
    spec: FieldSpec

    @property
    def lexical(self) -> Optional[str]:
        if self.node is None:
            return None
        value = self.node.string_value
        return None if value is None else str(value)


@dataclass
class _Target:
    """An instance element selected by an identity selector."""

    element_node: Any
    occurrences: tuple[_FieldOccurrence, ...]

    @property
    def key_tuple(self) -> KeyValueTuple:
        values: list[str] = []
        for occurrence in self.occurrences:
            lexical = occurrence.lexical
            if lexical is None:
                # A missing field participates with the empty string in the
                # identity tuple only for keyref; keys must be complete and
                # this is checked separately.
                lexical = ''
            values.append(lexical)
        return tuple(values)


@dataclass
class _IdentityData:
    identity: XsdIdentity
    fields: tuple[FieldSpec, ...]
    # scope instance element -> selected targets inside that scope
    scopes: dict[Any, list[_Target]]

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def kind(self) -> str:
        return type(self.identity).__name__[3:].lower()

    def all_targets(self) -> Iterator[_Target]:
        for targets in self.scopes.values():
            yield from targets


def _iter_matching_instances(root: Any, xsd_element_name: str)         -> Iterator[Any]:
    """
    All instance elements (pre-order) whose tag matches the constraint's
    declaring XSD element.

    The document root itself is included: a constraint declared on the
    document element has the whole document as its single scope.
    """
    for elem in root.iter():
        if isinstance(getattr(elem, 'tag', None), str) \
                and elem.tag == xsd_element_name:
            yield elem


def _build_field_specs(identity: XsdIdentity) -> tuple[FieldSpec, ...]:
    identity.build()
    identity.update_elements(identity.parent)
    if not identity.elements:
        raise AnonymizationError(
            f"identity constraint {identity.name!r} does not resolve to any "
            f"schema element (wildcard selectors are not supported)"
        )

    selectors = next(iter(identity.elements.values()))
    specs: list[FieldSpec] = []
    for index, fvs in enumerate(selectors):
        decoder = fvs.decoders[0]
        specs.append(FieldSpec(
            xsd_type=decoder.type,
            path=identity.fields[index].path,
            is_attribute=type(decoder).__name__ == 'XsdAttribute',
        ))
    return tuple(specs)


def _collect_identity(identity: XsdIdentity, resource: XMLResource,
                      namespaces: Optional[dict[str, str]]) -> _IdentityData:
    root = resource.root
    """
    Collect, for every scope of the identity constraint, the selected
    targets and their field XPath nodes.

    XSD scopes are the instance elements matching the element that declares
    the identity; the identity selector is evaluated relative to each of
    them, which keeps nested same-named scopes independent.
    """
    field_specs = _build_field_specs(identity)
    declarer_name = identity.parent.name

    scopes: dict[Any, list[_Target]] = {}
    for scope_elem in _iter_matching_instances(root, declarer_name):
        scope_node = resource.get_xpath_node(scope_elem)
        targets: list[_Target] = []
        for target_node in identity.selector.token.select(
                XPathContext(scope_node, namespaces=namespaces)):
            if not isinstance(target_node, (ElementNode, EtreeElementNode)):
                raise AnonymizationError(
                    f"selector {identity.selector.path!r} of {identity.name!r} "
                    f"selected a non-element node"
                )
            occurrences: list[_FieldOccurrence] = []
            for index, field_selector in enumerate(identity.fields):
                nodes = list(field_selector.token.select(
                    XPathContext(target_node, namespaces=namespaces)))
                if len(nodes) > 1:
                    raise AnonymizationError(
                        f"field {field_selector.path!r} of {identity.name!r} "
                        f"selects multiple nodes for one target"
                    )
                occurrences.append(_FieldOccurrence(
                    nodes[0] if nodes else None, field_specs[index]))
            targets.append(_Target(target_node, tuple(occurrences)))
        scopes[scope_elem] = targets

    return _IdentityData(identity, field_specs, scopes)


def _write_occurrence(occurrence: _FieldOccurrence, value: str) -> None:
    node = occurrence.node
    if node is None:
        raise AnonymizationError(
            "cannot write a replacement to a missing field node"
        )
    if isinstance(node, AttributeNode):
        host = node.parent.obj
        # elementpath AttributeNode.obj carries the attribute string value;
        # the owning element is the parent node.
        host.set(node.name, value)
    else:
        node.obj.text = value


# --- Tree helpers ---------------------------------------------------------

def _build_parent_map(root: Any) -> dict[Any, Any]:
    return {child: parent for parent in root.iter() for child in parent
            if isinstance(getattr(child, 'tag', None), str)}


def _nearest_scope(elem: Any, parent_map: Mapping[Any, Any],
                   scope_elements: set[Any]) -> Optional[Any]:
    """The nearest element on the ancestor axis (including *elem*) that is
    one of *scope_elements*."""
    candidate: Any = elem
    while candidate is not None:
        if candidate in scope_elements:
            return candidate
        candidate = parent_map.get(candidate)
    if len(scope_elements) == 1:
        # A global (document-root level) constraint: a keyref declared on a
        # nested element references the single top-level scope.
        return next(iter(scope_elements))
    return None


# --- Mapping planning -----------------------------------------------------

@dataclass
class _ScopePlan:
    scope_elem: Any
    mapping: dict[KeyValueTuple, KeyValueTuple]


@dataclass
class _IdentityPlan:
    data: _IdentityData
    generated: bool
    scope_plans: dict[Any, _ScopePlan]

    @property
    def identity(self) -> XsdIdentity:
        return self.data.identity


def _normalize_provided(replacements: Optional[ReplacementTable],
                        field_count: int, identity_name: str) \
        -> dict[KeyValueTuple, KeyValueTuple]:
    if not replacements:
        return {}
    normalized: dict[KeyValueTuple, KeyValueTuple] = {}
    for old, new in replacements.items():
        old_tuple = (old,) if isinstance(old, str) else tuple(old)
        new_tuple = (new,) if isinstance(new, str) else tuple(new)
        if len(old_tuple) != field_count or len(new_tuple) != field_count:
            raise AnonymizationError(
                f"replacement entry {list(old_tuple)!r}->{list(new_tuple)!r} "
                f"for {identity_name!r} does not match its {field_count} "
                f"field(s)"
            )
        if old_tuple in normalized and normalized[old_tuple] != new_tuple:
            raise AnonymizationError(
                f"conflicting replacements provided for {list(old_tuple)!r} "
                f"in {identity_name!r}"
            )
        normalized[old_tuple] = new_tuple
    return normalized


def _plan_key_identity(data: _IdentityData,
                       provided: dict[KeyValueTuple, KeyValueTuple],
                       generator: Optional[RestrictedValueGenerator]) \
        -> _IdentityPlan:
    """
    Build a bijective mapping for every scope of a key/unique constraint.

    Each distinct tuple maps to a distinct replacement. A replacement that
    duplicates another replacement or reuses an original value belonging to
    another tuple (which would create a second preimage) aborts the run.
    """
    generated_any = False
    scope_plans: dict[Any, _ScopePlan] = {}

    for scope_elem, targets in data.scopes.items():
        ordered: list[KeyValueTuple] = []
        for target in targets:
            if target.key_tuple not in ordered:
                ordered.append(target.key_tuple)

        originals = set(ordered)
        mapping: dict[KeyValueTuple, KeyValueTuple] = {}
        for old_tuple in ordered:
            if old_tuple in provided:
                new_tuple = provided[old_tuple]
            elif generator is not None:
                new_tuple = _generate_tuple(data.fields, generator,
                                            set(mapping.values()), originals)
                generated_any = True
            else:
                raise AnonymizationError(
                    f"no replacement provided for key {list(old_tuple)!r} "
                    f"of {data.name!r} and no generator was configured"
                )

            _validate_tuple(data.fields, new_tuple, data.name)

            if new_tuple in mapping.values():
                clash = next(old for old, new in mapping.items()
                             if new == new_tuple)
                raise AnonymizationError(
                    f"target value collision in {data.name!r}: "
                    f"{list(old_tuple)!r} and {list(clash)!r} both map to "
                    f"{list(new_tuple)!r}"
                )
            if new_tuple != old_tuple and new_tuple in originals:
                raise AnonymizationError(
                    f"target value collision in {data.name!r}: replacement "
                    f"{list(new_tuple)!r} is also an original value of a "
                    f"different key in the same scope"
                )
            mapping[old_tuple] = new_tuple

        scope_plans[scope_elem] = _ScopePlan(scope_elem, mapping)

    return _IdentityPlan(data, generated_any, scope_plans)


def _validate_tuple(fields_: tuple[FieldSpec, ...],
                    new_tuple: KeyValueTuple, identity_name: str) -> None:
    if len(new_tuple) != len(fields_):
        raise AnonymizationError(
            f"replacement {list(new_tuple)!r} for {identity_name!r} must "
            f"have {len(fields_)} field(s)"
        )
    for spec, value in zip(fields_, new_tuple):
        if not isinstance(value, str):
            raise AnonymizationError(
                f"replacement field {spec.path!r} must be a string, got "
                f"{type(value).__name__}"
            )
        spec.validate(value)


def _generate_tuple(fields_: tuple[FieldSpec, ...],
                    generator: RestrictedValueGenerator,
                    used_tuples: set[KeyValueTuple],
                    originals: set[KeyValueTuple]) -> KeyValueTuple:
    values: list[str] = []
    for index, spec in enumerate(fields_):
        reserved = {tuple_[index] for tuple_ in used_tuples}
        immutable = {tuple_[index] for tuple_ in originals}
        values.append(generator.generate(
            spec, reserved=reserved, immutable=immutable))
    return tuple(values)


def _resolve_keyrefs(plans: Mapping[Any, _IdentityPlan],
                     collected: Mapping[XsdIdentity, _IdentityData],
                     parent_map: Mapping[Any, Any]) -> None:
    """
    Resolve every keyref tuple against its referenced key mapping in the
    enclosing key scope. Unresolvable references abort the whole run.
    """
    for identity, data in collected.items():
        if not isinstance(identity, XsdKeyref):
            continue
        refer = identity.refer
        if not isinstance(refer, (XsdKey, XsdUnique)):
            raise AnonymizationError(
                f"keyref {data.name!r} does not reference a key/unique "
                f"constraint"
            )
        if refer not in plans:
            raise AnonymizationError(
                f"keyref {data.name!r} references {refer.name!r} which is "
                f"not being anonymized in this run"
            )
        refer_plan = plans[refer]
        key_scopes = set(refer_plan.scope_plans)

        for scope_elem, targets in data.scopes.items():
            key_scope = _nearest_scope(scope_elem, parent_map, key_scopes)
            if key_scope is None:
                raise AnonymizationError(
                    f"cannot locate the scope of {refer.name!r} enclosing "
                    f"keyref {data.name!r}"
                )
            mapping = refer_plan.scope_plans[key_scope].mapping
            for target in targets:
                ref_tuple = target.key_tuple
                if all(value == '' for value in ref_tuple):
                    continue
                if ref_tuple not in mapping:
                    raise AnonymizationError(
                        f"keyref {data.name!r} points to {list(ref_tuple)!r} "
                        f"which is not defined by {refer.name!r} in the "
                        f"enclosing scope"
                    )


# --- Reference graph fingerprint -----------------------------------------

@dataclass(frozen=True)
class GraphFingerprint:
    """
    Value-independent fingerprint of the key/keyref reference graph.

    It is computed with *canonical relabeling*: within every scope each
    distinct key tuple gets a canonical id from its first appearance and
    every keyref occurrence an edge to the id it points at. The actual key
    text never enters the fingerprint, but the graph shape, which reference
    points where, how often a key occurs, and how it is shared are all
    captured. Anonymization only renames the labels, so the fingerprint is
    unchanged; any structural or association change alters it.
    """

    node_count: int
    edges: tuple[tuple[str, str, int, int], ...]
    key_multiplicity: tuple[tuple[str, str, int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            'node_count': self.node_count,
            'edges': [list(edge) for edge in self.edges],
            'key_multiplicity': [list(item)
                                 for item in self.key_multiplicity],
        }


def reference_graph_fingerprint(schema: Any, source: Any,
                                namespaces: Optional[dict[str, str]] = None
                                ) -> GraphFingerprint:
    """
    Compute a value-independent fingerprint of the key/keyref graph of
    *source*.
    """
    resource = XMLResource(source)
    root = resource.root
    if namespaces is None:
        namespaces = dict(schema.namespaces)
    parent_map = _build_parent_map(root)
    identities = _anonymizable_identities(schema)

    collected: dict[XsdIdentity, _IdentityData] = {
        identity: _collect_identity(identity, resource, namespaces)
        for identity in identities
    }

    # Canonical ids for key tuples: (refer name, scope index, tuple) -> id.
    key_id: dict[tuple[str, int, KeyValueTuple], int] = {}
    multiplicity: list[tuple[str, str, int, int]] = []
    node_count = 0

    for identity, data in collected.items():
        if isinstance(identity, XsdKeyref):
            continue
        for scope_index, targets in enumerate(data.scopes.values()):
            first_seen: dict[KeyValueTuple, int] = {}
            counts: dict[KeyValueTuple, int] = {}
            for target in targets:
                key_tuple = target.key_tuple
                counts[key_tuple] = counts.get(key_tuple, 0) + 1
                if key_tuple not in first_seen:
                    first_seen[key_tuple] = node_count
                    key_id[(identity.name, scope_index, key_tuple)] = \
                        node_count
                    node_count += 1
            for key_tuple, count in counts.items():
                multiplicity.append(
                    (identity.name, identity.name, scope_index, count))

    edges: set[tuple[str, str, int, int]] = set()
    for identity, data in collected.items():
        if not isinstance(identity, XsdKeyref):
            continue
        refer = identity.refer
        if not isinstance(refer, (XsdKey, XsdUnique)):
            continue
        refer_data = collected[refer]
        refer_scope_order = list(refer_data.scopes)
        ref_node_index = 0
        for scope_index, targets in enumerate(data.scopes.values()):
            scope_elem = list(data.scopes)[scope_index]
            key_scope = _nearest_scope(
                scope_elem, parent_map, set(refer_data.scopes))
            if key_scope is None:
                raise AnonymizationError(
                    f"cannot locate the scope of {refer.name!r} for "
                    f"keyref {identity.name!r}"
                )
            key_scope_index = refer_scope_order.index(key_scope)
            for target in targets:
                ref_tuple = target.key_tuple
                if all(value == '' for value in ref_tuple):
                    continue
                try:
                    target_id = key_id[(refer.name, key_scope_index,
                                       ref_tuple)]
                except KeyError:
                    raise AnonymizationError(
                        f"keyref {identity.name!r} -> {list(ref_tuple)!r} "
                        f"cannot be resolved in {refer.name!r}"
                    )
                ref_id = node_count + ref_node_index
                edges.add((identity.name, refer.name, ref_id, target_id))
                ref_node_index += 1
        node_count += ref_node_index

    return GraphFingerprint(
        node_count=node_count,
        edges=tuple(sorted(edges)),
        key_multiplicity=tuple(sorted(multiplicity)),
    )


# --- Public API -----------------------------------------------------------

def _anonymizable_identities(schema: Any) -> list[XsdIdentity]:
    return [identity for identity in schema.identities.values()
            if isinstance(identity, (XsdKey, XsdUnique, XsdKeyref))]


def _rewrite_output(output_root: Any,
                    collected: Mapping[XsdIdentity, _IdentityData],
                    plans: Mapping[Any, _IdentityPlan],
                    namespaces: Optional[dict[str, str]],
                    source_parent_map: Mapping[Any, Any]) -> None:
    """
    Recompute targets on the output copy (same structural positions as the
    original) and apply the per-scope mappings, writing key targets first
    and keyref targets immediately afterwards per scope.
    """
    out_resource = XMLResource(output_root)
    for identity, original_data in collected.items():
        if isinstance(identity, XsdKeyref):
            continue
        out_data = _collect_identity(identity, out_resource, namespaces)
        plan = plans[identity]
        original_scopes = list(original_data.scopes)
        out_scopes = list(out_data.scopes)
        if len(original_scopes) != len(out_scopes):
            raise AnonymizationError(
                f"internal error: scope count changed while copying "
                f"{identity.name!r}"
            )

        for original_scope, out_scope in zip(original_scopes, out_scopes):
            mapping = plan.scope_plans[original_scope].mapping
            original_targets = original_data.scopes[original_scope]
            out_targets = out_data.scopes[out_scope]
            if len(original_targets) != len(out_targets):
                raise AnonymizationError(
                    f"internal error: target count changed while copying "
                    f"{identity.name!r}"
                )
            for new_target in out_targets:
                new_tuple = mapping[new_target.key_tuple]
                for occurrence, value in zip(new_target.occurrences,
                                             new_tuple):
                    _write_occurrence(occurrence, value)

    for identity, ref_data in collected.items():
        if not isinstance(identity, XsdKeyref):
            continue
        refer = identity.refer
        refer_plan = plans[refer]
        refer_original = collected[refer]
        out_data = _collect_identity(identity, out_resource, namespaces)
        original_scopes = list(ref_data.scopes)
        out_scopes = list(out_data.scopes)
        for original_scope, out_scope in zip(original_scopes, out_scopes):
            # Resolve against the original tree; the copied tree has the
            # same shape so scope indices line up.
            key_scope = _nearest_scope(
                original_scope, source_parent_map,
                set(refer_plan.scope_plans))
            if key_scope is None:
                raise AnonymizationError(
                    f"cannot locate key scope for keyref {identity.name!r}"
                )
            mapping = refer_plan.scope_plans[key_scope].mapping
            out_targets = out_data.scopes[out_scope]
            for new_target in out_targets:
                ref_tuple = new_target.key_tuple
                if all(value == '' for value in ref_tuple):
                    continue
                new_tuple = mapping[ref_tuple]
                for occurrence, value in zip(new_target.occurrences,
                                             new_tuple):
                    _write_occurrence(occurrence, value)


def anonymize(source: Any,
              schema: Any,
              replacements: Optional[Mapping[str, ReplacementTable]] = None,
              *,
              generator: Optional[RestrictedValueGenerator] = None,
              namespaces: Optional[dict[str, str]] = None,
              validate: bool = True,
              include: Optional[set[str]] = None,
              strict: bool = False,
              ) -> tuple[Any, AnonymizationReceipt]:
    """
    Anonymize the business keys of an XML document while preserving every
    key/keyref association.

    :param source: an XML document (path, file, URL, ElementTree element or
        :class:`xmlschema.XMLResource`).
    :param schema: the :class:`xmlschema.XMLSchema` defining the identity
        constraints.
    :param replacements: optional ``identity_name -> table`` mapping where
        each table maps an old scalar/tuple to a new scalar/tuple. Composite
        keys are provided as tuples and handled as a whole.
    :param generator: a :class:`RestrictedValueGenerator` used for keys
        missing from *replacements*. When omitted a default restricted
        generator fills the gaps; pass ``strict=True`` to require that
        every key is explicitly present in *replacements* instead.
    :param namespaces: prefix-to-URI map for identity selectors; defaults to
        the schema namespaces.
    :param validate: validate the source before planning and the output
        after rewriting; any failure aborts the whole run.
    :param include: optional set of constraint names (QName or local name)
        to process; defaults to every key/unique/keyref in the schema.
    :param strict: when true every anonymized key must be supplied in
        *replacements* and no implicit generation happens.
    :returns: ``(output_element, AnonymizationReceipt)``. The original
        document is left untouched on failure.
    :raises AnonymizationError: collision, unresolvable reference, invalid
        replacement or generator failure.
    """
    resource = XMLResource(source)
    root = resource.root
    if namespaces is None:
        namespaces = dict(schema.namespaces)

    all_identities = _anonymizable_identities(schema)
    if include is not None:
        identities = [identity for identity in all_identities
                      if identity.name in include
                      or identity.local_name in include]
        missing = include - {i.name for i in all_identities} \
            - {i.local_name for i in all_identities}
        if missing:
            raise AnonymizationError(
                f"identity constraints not found in schema: {sorted(missing)!r}"
            )
    else:
        identities = all_identities

    if validate:
        validation_errors = list(schema.iter_errors(resource))
        if validation_errors:
            raise AnonymizationError(
                "source document is not schema-valid, aborting: "
                + str(validation_errors[0]).splitlines()[0]
            )

    tables = replacements or {}
    effective_generator = generator
    if effective_generator is None and not strict:
        effective_generator = RestrictedValueGenerator()

    # Phase 1: read all constraints from the unmodified source.
    collected: dict[XsdIdentity, _IdentityData] = {
        identity: _collect_identity(identity, resource, namespaces)
        for identity in identities
    }
    for identity, data in collected.items():
        logger.info("constraint %s (%s): %d target row(s) in %d scope(s)",
                    identity.local_name, data.kind,
                    sum(len(t) for t in data.scopes.values()),
                    len(data.scopes))

    # Phase 2: build bijective per-scope mappings for keys and uniques.
    plans: dict[Any, _IdentityPlan] = {}
    for identity, data in collected.items():
        if isinstance(identity, XsdKeyref):
            continue
        table = tables.get(identity.name, tables.get(identity.local_name, {}))
        provided = _normalize_provided(table, len(data.fields), identity.name)
        plans[identity] = _plan_key_identity(
            data, provided, effective_generator)

    # Phase 3: resolve every keyref; failures abort before any write.
    _resolve_keyrefs(plans, collected, _build_parent_map(root))

    # Phase 4: apply onto a deep copy.
    output_root = copy.deepcopy(root)
    _rewrite_output(output_root, collected, plans, namespaces,
                    _build_parent_map(root))

    if validate:
        output_errors = list(schema.iter_errors(output_root))
        if output_errors:
            raise AnonymizationError(
                "anonymized document failed schema validation; the whole "
                "run is rejected: " + str(output_errors[0]).splitlines()[0]
            )

    receipt = _build_receipt(plans)
    logger.info("anonymization complete for %d constraint(s); original key "
                "values are not logged", len(receipt.scopes))
    return output_root, receipt


def _build_receipt(plans: Mapping[Any, _IdentityPlan]) -> AnonymizationReceipt:
    receipt = AnonymizationReceipt()
    for plan in plans.values():
        scope_mappings: dict[int, dict[KeyValueTuple, KeyValueTuple]] = {}
        for scope_index, scope_plan in enumerate(plan.scope_plans.values()):
            scope_mappings[scope_index] = dict(scope_plan.mapping)
        receipt.scopes[plan.data.name] = IdentityReceipt(
            identity=plan.data.name,
            kind=plan.data.kind,
            fields=tuple(spec.path for spec in plan.data.fields),
            scopes=scope_mappings,
            generated=plan.generated,
        )
    return receipt

