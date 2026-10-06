"""
Support for a deliberately small subset of XSD identity constraints
(``xs:key``, ``xs:unique`` and ``xs:keyref``).

Supported selector/field syntax is the common subset used for cross-element
references:

* selectors: ``child`` and ``descendant-or-self`` (``.//``) steps with plain
  element names or ``*``; multiple branches joined with ``|``;
* fields: a single ``.``, ``@attribute``, or a chain of child element steps
  optionally ending in an attribute.

Anything more complex (predicates, other axes, free-standing ``//`` inside a
path, ...) makes the generator fail with a precise :class:`UnsupportedSchemaFeatureError`
instead of producing an instance that might violate the constraint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from ..validators.attributes import XsdAttribute
from ..validators.elements import XsdElement
from ..validators.groups import XsdGroup
from ..validators.identities import XsdIdentity
from ..validators.simple_types import XsdSimpleType
from ..validators.wildcards import XsdWildcard
from .exceptions import (
    NoInstancePossibleError,
    UnsupportedSchemaFeatureError,
)


@dataclass(frozen=True)
class ElementStep:
    axis: str          # 'child' or 'descendant'
    name: str | None   # qualified name or None for '*'


@dataclass(frozen=True)
class FieldTarget:
    kind: str          # 'text', 'attribute' or 'element-text'
    attribute: str | None = None
    chain: tuple[str, ...] = ()


@dataclass
class IdentityPlan:
    identity: XsdIdentity
    branches: list[list[ElementStep]] = field(default_factory=list)
    targets: list[FieldTarget] = field(default_factory=list)


def _split_prefix(token: str) -> tuple[str | None, str]:
    if ':' in token:
        prefix, local = token.split(':', 1)
        return prefix, local
    return None, token


def _resolve_qname(prefix: str | None, local: str,
                   identity: XsdIdentity) -> str:
    if prefix is None:
        # Unprefixed names in identity XPath expressions resolve against the
        # target namespace of the schema (xpathDefaultNamespace for 1.1).
        namespace = identity.selector.xpath_default_namespace
        if namespace:
            return '{%s}%s' % (namespace, local)
        return local
    try:
        namespace = identity.schema.namespaces[prefix]
    except KeyError:
        raise UnsupportedSchemaFeatureError(
            f"identity {identity.name!r} uses undeclared namespace prefix "
            f"{prefix!r}",
            component=identity,
        )
    return '{%s}%s' % (namespace, local)


def parse_element_path(path: str, identity: XsdIdentity,
                       *, allow_descendant: bool) -> list[ElementStep]:
    steps: list[ElementStep] = []
    raw_steps = path.strip().split('/')
    if raw_steps and raw_steps[0] == '.':
        raw_steps = raw_steps[1:]
    index = 0
    while index < len(raw_steps):
        token = raw_steps[index]
        if token == '':
            if index != 0 or not allow_descendant:
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} path {path!r} uses an axis "
                    "that is not supported by the bounded instance generator",
                    component=identity,
                )
            try:
                next_token = raw_steps[index + 1]
            except IndexError:
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} path {path!r} is incomplete",
                    component=identity,
                )
            if next_token in ('.', '') or ':' in next_token and \
                    next_token.startswith('@'):
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} path {path!r} is not supported",
                    component=identity,
                )
            prefix, local = _split_prefix(next_token)
            steps.append(ElementStep(
                'descendant',
                None if local == '*' else _resolve_qname(prefix, local, identity),
            ))
            index += 2
            continue

        if token == '*':
            steps.append(ElementStep('child', None))
        elif token and not token.startswith('@') and '.' not in token:
            prefix, local = _split_prefix(token)
            if prefix == '@':
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} path {path!r} puts an attribute "
                    "in selector position, which is not supported",
                    component=identity,
                )
            steps.append(ElementStep(
                'child',
                None if local == '*' else _resolve_qname(prefix, local, identity),
            ))
        else:
            raise UnsupportedSchemaFeatureError(
                f"identity {identity.name!r} path {path!r} is not supported by "
                "the bounded instance generator (only plain child/.//name paths "
                "are supported)",
                component=identity,
            )
        index += 1
    return steps


def parse_field_path(path: str, identity: XsdIdentity) -> FieldTarget:
    path = path.strip()
    if path == '.':
        return FieldTarget('text')

    attribute: str | None = None
    chain: list[str] = []
    raw_steps = [s for s in path.split('/') if s]
    if raw_steps[0] == '.':
        raw_steps = raw_steps[1:]

    for position, token in enumerate(raw_steps):
        if token.startswith('@'):
            if position != len(raw_steps) - 1:
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} field {path!r} has an attribute "
                    "step that is not in final position",
                    component=identity,
                )
            prefix, local = _split_prefix(token[1:])
            if local == '*':
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} field {path!r} uses an "
                    "attribute wildcard (@*), which is not supported by the "
                    "bounded instance generator",
                    component=identity,
                )
            attribute = _resolve_qname(prefix, local, identity)
        else:
            if attribute is not None:
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} field {path!r} is not supported",
                    component=identity,
                )
            prefix, local = _split_prefix(token)
            if local == '*':
                raise UnsupportedSchemaFeatureError(
                    f"identity {identity.name!r} field {path!r} uses an "
                    "element wildcard, which is not supported by the "
                    "bounded instance generator",
                    component=identity,
                )
            chain.append(_resolve_qname(prefix, local, identity))

    if not chain:
        return FieldTarget('attribute', attribute=attribute)
    return FieldTarget(
        'element-text', attribute=attribute, chain=tuple(chain),
    )


def build_identity_plan(identity: XsdIdentity) -> IdentityPlan:
    if identity.selector is None:
        raise UnsupportedSchemaFeatureError(
            f"identity {identity.name!r} has no selector", component=identity,
        )
    plan = IdentityPlan(identity=identity)
    for branch in identity.selector.path.split('|'):
        plan.branches.append(
            parse_element_path(branch, identity, allow_descendant=True)
        )
    for field in identity.fields:
        plan.targets.append(parse_field_path(field.path, identity))
    return plan


def _matches(tag: str, step: ElementStep) -> bool:
    return step.name is None or tag == step.name


def select_elements(scope: Element, plan: IdentityPlan,
                    node_meta: dict[Element, object]) -> list[Element]:
    """Materialize the selector of *plan* starting from the scope element."""
    selected: list[Element] = []

    def _walk(element: Element, steps: list[ElementStep]) -> None:
        if not steps:
            selected.append(element)
            return
        step = steps[0]
        remaining = steps[1:]
        if step.axis == 'descendant':
            for child in element.iter():
                if child is element:
                    continue
                if child in node_meta and _matches(child.tag, step):
                    _walk(child, remaining)
        else:
            for child in element:
                if child in node_meta and _matches(child.tag, step):
                    _walk(child, remaining)

    for branch in plan.branches:
        _walk(scope, list(branch))

    # De-duplicate while preserving order (multiple selector branches).
    seen: set[int] = set()
    unique: list[Element] = []
    for element in selected:
        if id(element) not in seen:
            seen.add(id(element))
            unique.append(element)
    return unique


def resolve_field_type(element_comp: XsdElement,
                       target: FieldTarget) -> XsdSimpleType:
    """Resolve the simple type of a field on a bound XsdElement."""
    if target.kind == 'text':
        st = element_comp.type
        if not isinstance(st, XsdSimpleType):
            st = getattr(st, 'content', None)
        if not isinstance(st, XsdSimpleType):
            raise UnsupportedSchemaFeatureError(
                f"field '.' of element {element_comp.prefixed_name!r} does not "
                "have a simple type"
            )
        return st

    if target.kind == 'attribute':
        attribute = element_comp.type.attributes.get(target.attribute)
        if isinstance(attribute, XsdAttribute):
            return attribute.type
        raise UnsupportedSchemaFeatureError(
            f"field attribute of {element_comp.prefixed_name!r} is not "
            "declared in the schema"
        )

    comp: XsdElement = element_comp
    group: XsdGroup | None = getattr(comp.type, 'content', None)
    if not isinstance(group, XsdGroup):
        raise UnsupportedSchemaFeatureError(
            f"field chain of {element_comp.prefixed_name!r} crosses a simple "
            "type"
        )
    for name in target.chain:
        found = _find_particle(group, name)
        if isinstance(found, XsdWildcard):
            raise UnsupportedSchemaFeatureError(
                "an identity field selects a wildcard element, which is not "
                "supported"
            )
        if not isinstance(found, XsdElement):
            raise UnsupportedSchemaFeatureError(
                f"field step {name!r} is not a single element declaration"
            )
        comp = found
        group = getattr(comp.type, 'content', None)

    st = comp.type
    if not isinstance(st, XsdSimpleType):
        st = getattr(st, 'content', None)
    if not isinstance(st, XsdSimpleType):
        raise UnsupportedSchemaFeatureError(
            f"field element {comp.prefixed_name!r} does not have a simple type"
        )
    return st


def field_chain_particles(element_comp: XsdElement,
                          target: FieldTarget) -> list[XsdElement]:
    """Return the element particles a field chain needs to be materialized."""
    if target.kind != 'element-text':
        return []
    particles: list[XsdElement] = []
    group: XsdGroup | None = getattr(element_comp.type, 'content', None)
    if not isinstance(group, XsdGroup):
        return []
    for name in target.chain:
        found = _find_particle(group, name)
        if not isinstance(found, XsdElement):
            break
        particles.append(found)
        group = getattr(found.type, 'content', None)
        if not isinstance(group, XsdGroup):
            break
    return particles


def _find_particle(group: XsdGroup, name: str | None):
    if name is None:
        return None
    referenced = group.ref
    particles = referenced.content if referenced is not None else group.content
    for particle in particles:
        if isinstance(particle, XsdElement) and particle.name == name:
            return particle.ref if particle.ref is not None else particle
        if isinstance(particle, XsdGroup):
            found = _find_particle(particle, name)
            if found is not None:
                return found
    return None


def get_field_node(element: Element, target: FieldTarget):
    """Return a (kind, holder, key) descriptor of the field value node."""
    if target.kind == 'text':
        return 'text', element, None
    if target.kind == 'attribute':
        if target.attribute in element.attrib:
            return 'attribute', element, target.attribute
        return None

    current = element
    for name in target.chain:
        child = None
        for candidate in current:
            if candidate.tag == name:
                child = candidate
                break
        if child is None:
            return None
        current = child
    return 'text', current, None


def set_field_value(element: Element, target: FieldTarget, value: str) -> None:
    node = get_field_node(element, target)
    if node is None:
        raise NoInstancePossibleError(
            "a required identity field node is missing from the generated "
            "structure"
        )
    kind, holder, key = node
    if kind == 'attribute':
        holder.attrib[key] = value
    else:
        holder.text = value
