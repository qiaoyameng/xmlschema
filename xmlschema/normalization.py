"""
Lexical normalization of simple values in XSD-validated XML instances.

This is value-space lexical normalization, not XML canonicalization (C14N).
Rewriting signed data can invalidate existing XML Signatures.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, NamedTuple, Optional
from xml.etree.ElementTree import Element

from elementpath.datatypes import AbstractDateTime, Duration

import xmlschema.names as nm
from xmlschema.aliases import BaseXsdType, ElementType, NsmapType, XMLSourceType
from xmlschema.validators import XsdElement, XsdSimpleType, XsdAtomic, \
    XsdList, XsdUnion, XsdAnyAttribute, XsdAnyElement, XMLSchemaBase, \
    XMLSchemaValidationError
from xmlschema.utils.qnames import local_name

__all__ = ('ValueRewrite', 'normalize_simple_values')

_ATOMIC_TYPES = {
    nm.XSD_DECIMAL,
    nm.XSD_INTEGER, nm.XSD_LONG, nm.XSD_INT, nm.XSD_SHORT, nm.XSD_BYTE,
    nm.XSD_NON_NEGATIVE_INTEGER, nm.XSD_POSITIVE_INTEGER,
    nm.XSD_NON_POSITIVE_INTEGER, nm.XSD_NEGATIVE_INTEGER,
    nm.XSD_UNSIGNED_LONG, nm.XSD_UNSIGNED_INT, nm.XSD_UNSIGNED_SHORT,
    nm.XSD_UNSIGNED_BYTE,
}
_INTEGER_TYPE = {
    nm.XSD_INTEGER, nm.XSD_LONG, nm.XSD_INT, nm.XSD_SHORT, nm.XSD_BYTE,
    nm.XSD_NON_NEGATIVE_INTEGER, nm.XSD_POSITIVE_INTEGER,
    nm.XSD_NON_POSITIVE_INTEGER, nm.XSD_NEGATIVE_INTEGER,
    nm.XSD_UNSIGNED_LONG, nm.XSD_UNSIGNED_INT, nm.XSD_UNSIGNED_SHORT,
    nm.XSD_UNSIGNED_BYTE,
}
_DATE_TIME_TYPES = {
    nm.XSD_DATETIME, nm.XSD_DATE, nm.XSD_TIME, nm.XSD_DURATION,
    nm.XSD_GYEAR, nm.XSD_GYEAR_MONTH, nm.XSD_GMONTH, nm.XSD_GMONTH_DAY,
    nm.XSD_GDAY,
}


class ValueRewrite(NamedTuple):
    """An actual lexical rewrite performed on an element text or attribute."""

    location: str
    xsd_type: str
    old_value: str
    new_value: str


def normalize_simple_values(xml_document: XMLSourceType,
                            schema: XMLSchemaBase,
                            namespaces: Optional[NsmapType] = None) \
        -> tuple[Element, list[ValueRewrite]]:
    """
    Return a deep copy of *xml_document* with simple lexical values canonicalized.

    The document is strictly validated against *schema* before rewriting. Only
    elements and attributes matched by concrete XSD simple types are considered.
    This performs value lexical normalization (for example ``1`` to ``true``,
    ``1.200`` to ``1.2``, or a UTC offset to ``Z``); it is not XML C14N and does
    not preserve validity of existing XML digital signatures.

    Mixed content, unresolved/unknown types, QName lexical forms and nodes
    referenced by XML Signatures are left untouched. When a union has more than
    one legal branch producing different canonical lexical forms, the value is
    also left untouched.

    Neither the source XML tree nor the schema are modified in place.
    """
    if not isinstance(schema, XMLSchemaBase):
        raise TypeError("the 'schema' argument must be an XMLSchema instance")

    resource = schema.maps.settings.get_xml_resource(xml_document)
    root = copy.deepcopy(resource.root)

    xsi_type_snapshots = {
        component: set(component.xsi_types)
        for component in schema.iter_components(XsdElement)
    }
    try:
        schema.validate(root, namespaces=namespaces, use_location_hints=False)

        target_ids = _signed_reference_ids(root)
        excluded = _build_excluded_nodes(root, target_ids)
        bindings: dict[Element, tuple[XsdElement, BaseXsdType]] = {}
        pending: list[tuple[XsdElement, BaseXsdType]] = []

        def element_hook(data: Any, xsd_element: Optional[XsdElement],
                         xsd_type: Optional[BaseXsdType]) -> Any:
            if xsd_element is not None and xsd_type is not None:
                pending.append((xsd_element, xsd_type))
            return data

        def extra_validator(elem: Element, xsd_element: XsdElement) -> None:
            if pending:
                bindings[elem] = pending.pop(0)

        schema.decode(
            root,
            validation='strict',
            namespaces=namespaces,
            use_location_hints=False,
            element_hook=element_hook,
            extra_validator=extra_validator,
        )
    finally:
        for component, values in xsi_type_snapshots.items():
            component.xsi_types = values

    rewrites: list[ValueRewrite] = []
    for elem in root.iter():
        if elem in excluded:
            continue

        binding = bindings.get(elem)
        if binding is not None:
            xsd_element, xsd_type = binding
            _normalize_element(elem, xsd_element, xsd_type, rewrites)

    return root, rewrites


def _normalize_element(elem: ElementType, xsd_element: XsdElement,
                       xsd_type: BaseXsdType, rewrites: list[ValueRewrite]) -> None:
    if isinstance(xsd_element, XsdAnyElement) and xsd_element.process_contents != 'strict':
        return

    attribute_group = xsd_element.get_attributes(xsd_type)
    for name, value in list(elem.attrib.items()):
        if name.startswith('xmlns') and (len(name) == 5 or name[5:6] == ':'):
            continue

        xsd_attribute = attribute_group.get(name)
        if xsd_attribute is None or isinstance(xsd_attribute, XsdAnyAttribute):
            continue

        new_value = canonical_simple_value(xsd_attribute.type, value)
        if new_value is not None and new_value != value:
            rewrites.append(ValueRewrite(
                _attribute_location(elem, name),
                xsd_type_name(xsd_attribute.type),
                value,
                new_value,
            ))
            elem.set(name, new_value)

    if not isinstance(xsd_type, XsdSimpleType) or len(elem) or elem.text is None:
        return
    if xsd_type.is_complex() or xsd_type.name == nm.XSD_ANY_TYPE:
        return

    new_value = canonical_simple_value(xsd_type, elem.text)
    if new_value is not None and new_value != elem.text:
        rewrites.append(ValueRewrite(
            _element_location(elem),
            xsd_type_name(xsd_type),
            elem.text,
            new_value,
        ))
        elem.text = new_value


def canonical_simple_value(xsd_type: XsdSimpleType, text: str) -> Optional[str]:
    """Return the canonical lexical value, or ``None`` when it must not change."""
    normalized = xsd_type.normalize(text)
    if xsd_type.is_list():
        item_type = xsd_type.item_type
        values = []
        for token in normalized.split():
            value = _canonical_atomic_or_union(item_type, token)
            if value is None:
                return None
            values.append(value)
        return ' '.join(values)

    if isinstance(xsd_type, XsdUnion):
        return _canonical_union(xsd_type, normalized)

    if xsd_type.is_atomic() and xsd_type.primitive_type.is_union():
        value = _canonical_union(xsd_type.primitive_type, normalized)
        if value is not None and xsd_type.text_is_valid(text):
            return value
        return None

    return _canonical_atomic(xsd_type, normalized)


def _canonical_atomic_or_union(xsd_type: XsdSimpleType, text: str) -> Optional[str]:
    if isinstance(xsd_type, XsdUnion):
        return _canonical_union(xsd_type, text)
    if xsd_type.is_atomic() and xsd_type.primitive_type.is_union():
        value = _canonical_union(xsd_type.primitive_type, text)
        if value is not None and xsd_type.text_is_valid(text):
            return value
        return None
    return _canonical_atomic(xsd_type, text)


def _canonical_union(xsd_type: XsdUnion, text: str) -> Optional[str]:
    candidates = []
    for member_type in xsd_type.member_types:
        candidate = canonical_simple_value(member_type, text)
        if candidate is not None and member_type.text_is_valid(text):
            candidates.append(candidate)
    if not candidates or any(candidate != candidates[0] for candidate in candidates[1:]):
        return None
    return candidates[0]


def _canonical_atomic(xsd_type: XsdSimpleType, text: str) -> Optional[str]:
    root_type = getattr(xsd_type, 'root_type', xsd_type)
    type_name = getattr(root_type, 'name', None)

    if type_name in (nm.XSD_QNAME, nm.XSD_NOTATION_TYPE):
        return None
    if type_name == nm.XSD_BOOLEAN:
        try:
            value = root_type.to_python(text)
        except (ValueError, TypeError):
            return None
        return 'true' if value else 'false'
    if type_name in _INTEGER_TYPE:
        try:
            return str(int(Decimal(text)))
        except Exception:
            return None
    if type_name == nm.XSD_DECIMAL or type_name in _ATOMIC_TYPES:
        try:
            return _canonical_decimal(Decimal(text))
        except Exception:
            return None
    if type_name in _DATE_TIME_TYPES:
        return _canonical_date_time(root_type, text)
    return None


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return '0'
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), 'f')


def _canonical_date_time(xsd_type: XsdSimpleType, text: str) -> Optional[str]:
    try:
        value = xsd_type.to_python(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, (AbstractDateTime, Duration)):
        return None
    canonical = str(value)
    return canonical if canonical != text else canonical


def _signed_reference_ids(root: ElementType) -> set[str]:
    ids = set()
    for reference in root.iter(f'{{{nm.DSIG_NAMESPACE}}}Reference'):
        uri = reference.get('URI')
        if uri and uri.startswith('#') and not uri.startswith('#xpointer('):
            ids.add(uri[1:])
    return ids


def _build_excluded_nodes(root: ElementType, target_ids: set[str]) \
        -> set[ElementType]:
    excluded: set[ElementType] = set()
    for element in root.iter():
        if element.tag == f'{{{nm.DSIG_NAMESPACE}}}Signature':
            _mark(element, excluded)

    if target_ids:
        for element in root.iter():
            if element.get('Id') in target_ids or \
                    element.get('{http://www.w3.org/XML/1998/namespace}id') in target_ids:
                _mark(element, excluded)
    return excluded


def _mark(element: ElementType, excluded: set[ElementType]) -> None:
    excluded.add(element)
    for child in element.iter():
        excluded.add(child)


def _element_location(elem: ElementType) -> str:
    return f"element({local_name(elem.tag)})"


def _attribute_location(elem: ElementType, name: str) -> str:
    return f"attribute({local_name(name)})"


def xsd_type_name(xsd_type: BaseXsdType) -> str:
    name = getattr(xsd_type, 'prefixed_name', None) or getattr(xsd_type, 'name', None)
    if name is not None:
        return str(name)
    if isinstance(xsd_type, XsdList):
        return f'list({xsd_type_name(xsd_type.item_type)})'
    if isinstance(xsd_type, XsdUnion):
        return 'union(' + ', '.join(xsd_type_name(t) for t in xsd_type.member_types) + ')'
    root_type = getattr(xsd_type, 'root_type', xsd_type)
    return str(getattr(root_type, 'prefixed_name', None) or getattr(root_type, 'name', None))
