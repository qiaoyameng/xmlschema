#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Lexical normalization of simple values in schema-valid XML instances.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union
from urllib.parse import unquote
from elementpath.datatypes import DateTime

import xmlschema.names as nm
from xmlschema.aliases import ElementType, NsmapType, SchemaType, XMLSourceType
from xmlschema.namespaces import NamespaceMapper
from xmlschema.utils.etree import etree_tostring, is_etree_document, is_lxml_element
from xmlschema.resources.xml_resource import XMLResource
from xmlschema.validators.elements import XsdElement
from xmlschema.validators.simple_types import XsdList, XsdSimpleType, XsdUnion
from xmlschema.validators.validation import ValidationContext

__all__ = ('NormalizedDocument', 'ValueRewrite', 'normalize_values')


@dataclass(frozen=True)
class ValueRewrite:
    """A single actual lexical rewrite of an element value or attribute."""

    path: str
    kind: str
    xsd_type: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class NormalizedDocument:
    """The copied XML element and the simple-value rewrites applied to it."""

    root: ElementType
    changes: tuple[ValueRewrite, ...]
    resource: XMLResource


def normalize_values(xml_document: Union[XMLSourceType, XMLResource],
                     schema=None,
                     cls=None,
                     namespaces: Optional[NsmapType] = None) -> NormalizedDocument:
    """
    Return a copy with schema-typed simple values in canonical lexical form.

    Boolean, integer, non-integer decimal, dateTime and dateTimeStamp values
    are canonicalized. List items are canonicalized separately and joined by
    single spaces. For union values, a rewrite is emitted only when exactly
    one member type accepts the original lexical value.

    QName values are not lexically rewritten, so local prefixes continue to
    denote the same expanded name. Mixed character content and values without
    a concrete supported type are left unchanged. Nodes inside or referenced
    by an enveloped XML Signature are also excluded.

    This is value-space lexical normalization, not XML canonicalization
    (C14N), and it does not claim that an existing digital signature remains
    valid.
    """
    from xmlschema.documents import get_context

    if isinstance(xml_document, XMLResource) and xml_document.is_lazy():
        xml_document.load()
        xml_document = XMLResource(xml_document.text, lazy=False)

    is_element_input = hasattr(xml_document, 'tag') and \
        hasattr(xml_document, 'attrib')
    if is_element_input or is_etree_document(xml_document) or \
            is_lxml_element(xml_document):
        if is_etree_document(xml_document):
            xml_document = xml_document.getroot()
        xml_document = XMLResource(etree_tostring(xml_document), lazy=False)
    elif not isinstance(xml_document, XMLResource):
        xml_document = XMLResource(xml_document, lazy=False)

    source, original_schema = get_context(
        xml_document, schema=schema, cls=cls, namespaces=namespaces,
        use_location_hints=False, lazy=False
    )
    source.load()
    working_schema = deepcopy(original_schema)
    working_schema.validate(source, use_location_hints=False)

    copy_resource = _copy_resource(source)
    root = copy_resource.root

    parent_map = {child: parent for parent in root.iter() for child in parent}
    excluded = _signed_subtrees(root)
    resolved: dict[ElementType, tuple[XsdElement, object, XsdSimpleType, bool]] = {}
    changes: list[ValueRewrite] = []

    def resolve_hook(elem: ElementType, xsd_element: XsdElement):
        matched, attribute_type, value_type, mixed = _resolve_type(
            elem, xsd_element, copy_resource, working_schema
        )
        resolved[elem] = (matched, attribute_type, value_type, mixed)
        return elem in excluded

    def rewrite(elem: ElementType, xsd_element: XsdElement):
        if elem not in resolved or elem in excluded:
            return

        matched, declared_type, xsd_type, mixed = resolved[elem]
        attribute_group = matched.get_attributes(declared_type)
        elem_nsmap = _in_scope_namespaces(copy_resource, elem, parent_map, namespaces)

        for attr_name, attr_value in list(elem.attrib.items()):
            value_type = None
            if attr_name.startswith('{http://www.w3.org/2000/xmlns/}') or \
                    attr_name == 'xmlns':
                continue

            if attr_name == nm.XSI_NIL:
                value_type = working_schema.maps.types[nm.XSD_BOOLEAN]
            elif attr_name in (nm.XSI_TYPE, nm.XSI_SCHEMA_LOCATION,
                               nm.XSI_NONS_SCHEMA_LOCATION):
                continue
            else:
                xsd_attribute = attribute_group.get(attr_name)
                if xsd_attribute is None or isinstance(xsd_attribute, tuple):
                    continue
                value_type = xsd_attribute.type

            if value_type is None:
                continue
            new_value = _canonical_value(
                value_type, attr_value, copy_resource, elem_nsmap
            )
            if new_value is not None and new_value != attr_value:
                old_value = attr_value
                elem.set(attr_name, new_value)
                attr_path = _element_path(elem, root, parent_map)
                changes.append(ValueRewrite(
                    path=f'{attr_path}@{_path_name(attr_name)}',
                    kind='attribute',
                    xsd_type=_type_name(value_type),
                    old_value=old_value,
                    new_value=new_value,
                ))

        if not mixed and isinstance(xsd_type, XsdSimpleType) and elem.text:
            old_value = elem.text
            new_value = _canonical_value(xsd_type, elem.text, copy_resource, elem_nsmap)
            if new_value is not None and new_value != elem.text:
                elem.text = new_value
                changes.append(ValueRewrite(
                    path=_element_path(elem, root, parent_map),
                    kind='text',
                    xsd_type=_type_name(xsd_type),
                    old_value=old_value,
                    new_value=new_value,
                ))

    working_schema.validate(
        copy_resource,
        namespaces=namespaces,
        validation_hook=resolve_hook,
        extra_validator=rewrite,
        use_location_hints=False,
    )

    return NormalizedDocument(
        root=root, changes=tuple(changes), resource=copy_resource
    )


def _copy_resource(resource: XMLResource) -> XMLResource:
    copied = XMLResource(deepcopy(resource.root))

    if hasattr(copied.root, 'nsmap'):
        return copied

    original_elements = list(resource.root.iter())
    copied_elements = list(copied.root.iter())
    copied._nsmaps = {
        copied_element: dict(resource._nsmaps[original_element])
        for original_element, copied_element in zip(original_elements, copied_elements)
        if original_element in resource._nsmaps
    }
    copied._xmlns = {
        copied_element: resource._xmlns[original_element]
        for original_element, copied_element in zip(original_elements, copied_elements)
        if original_element in resource._xmlns
    }
    return copied


def _in_scope_namespaces(resource: XMLResource,
                         elem: ElementType,
                         parent_map: dict[ElementType, Optional[ElementType]],
                         extra: Optional[NsmapType]) -> dict[str, str]:
    result: dict[str, str] = {}
    chain: list[ElementType] = []
    current: Optional[ElementType] = elem

    while current is not None:
        chain.append(current)
        current = parent_map.get(current)

    for current in reversed(chain):
        nsmap = resource.get_nsmap(current)
        if nsmap:
            result.update(nsmap)

    if extra:
        result.update(extra)
    result.setdefault('xml', nm.XML_NAMESPACE)
    return result


def _resolve_type(elem: ElementType,
                  xsd_element: XsdElement,
                  resource: XMLResource,
                  schema: SchemaType):
    matched = xsd_element
    if matched.abstract:
        for substitute in matched.iter_substitutes():
            if substitute.name == elem.tag:
                matched = substitute
                break

    attribute_type = matched.get_alternative_type(elem)
    type_name = elem.attrib.get(nm.XSI_TYPE)
    if type_name is not None and schema.meta_schema is not None:
        nsmap = resource.get_nsmap(elem)
        if nsmap is None:
            nsmap = resource.get_namespaces(root_only=False)
        xsd_type = schema.maps.get_instance_type(
            type_name.strip(), attribute_type, nsmap
        )
        attribute_type = xsd_type
    else:
        xsd_type = attribute_type

    mixed = xsd_type.is_complex() and xsd_type.has_mixed_content()
    value_type = xsd_type.content if xsd_type.is_complex() and \
        xsd_type.has_simple_content() else xsd_type
    return matched, attribute_type, value_type, mixed


def _canonical_value(xsd_type: XsdSimpleType,
                     text: str,
                     resource: XMLResource,
                     nsmap: NsmapType) -> Optional[str]:
    union = _union_type(xsd_type)
    if union is not None:
        valid = [member_type for member_type in union.member_types
                 if _is_valid(member_type, text, resource, nsmap)]
        if len(valid) != 1:
            return None
        candidate = _canonical_value(valid[0], text, resource, nsmap)
        if candidate is None:
            return None
        return candidate if _is_valid(xsd_type, candidate, resource, nsmap) else None

    if isinstance(xsd_type, XsdList):
        item_type = xsd_type.item_type
        parts: list[str] = []
        for item in xsd_type.normalize(text).split():
            parts.append(_canonical_list_item(item_type, item, resource, nsmap))
        candidate = ' '.join(parts)
        return candidate if _is_valid(xsd_type, candidate, resource, nsmap) else None

    return _canonical_atomic(xsd_type, text, resource, nsmap)


def _canonical_list_item(xsd_type: XsdSimpleType,
                         text: str,
                         resource: XMLResource,
                         nsmap: NsmapType) -> str:
    union = _union_type(xsd_type)
    if union is not None:
        valid = [member_type for member_type in union.member_types
                 if _is_valid(member_type, text, resource, nsmap)]
        if len(valid) != 1:
            return text
        candidate = _canonical_list_item(valid[0], text, resource, nsmap)
        return candidate if _is_valid(xsd_type, candidate, resource, nsmap) else text

    if isinstance(xsd_type, XsdList):
        items = xsd_type.normalize(text).split()
        candidate = ' '.join(
            _canonical_list_item(xsd_type.item_type, item, resource, nsmap)
            for item in items
        )
        return candidate if _is_valid(xsd_type, candidate, resource, nsmap) else text

    candidate = _canonical_atomic(xsd_type, text, resource, nsmap)
    return text if candidate is None else candidate


def _union_type(xsd_type: XsdSimpleType) -> Optional[XsdUnion]:
    if isinstance(xsd_type, XsdUnion):
        return xsd_type

    primitive_type = getattr(xsd_type, 'primitive_type', None)
    if isinstance(primitive_type, XsdUnion):
        return primitive_type
    return None


def _canonical_atomic(xsd_type: XsdSimpleType,
                      text: str,
                      resource: XMLResource,
                      nsmap: NsmapType) -> Optional[str]:
    normalized = xsd_type.normalize(text)
    root_type = getattr(xsd_type, 'root_type', None)
    root_name = getattr(root_type, 'name', None)

    try:
        if xsd_type.is_qname():
            return None
        elif xsd_type.is_boolean():
            value = xsd_type.raw_decode(normalized, 'strict', _context(resource, nsmap))
            if not isinstance(value, bool):
                return None
            candidate = 'true' if bool(value) else 'false'
        elif xsd_type.is_derived(xsd_type.maps.types[nm.XSD_INTEGER]):
            value = xsd_type.raw_decode(normalized, 'strict', _context(resource, nsmap))
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            candidate = str(int(value))
        elif xsd_type.is_decimal():
            value = xsd_type.raw_decode(normalized, 'strict', _context(resource, nsmap))
            if not isinstance(value, Decimal) or isinstance(value, bool):
                return None
            candidate = _canonical_decimal(value)
        elif root_name in (nm.XSD_DATETIME, nm.XSD_DATE_TIME_STAMP):
            value = xsd_type.raw_decode(normalized, 'strict', _context(resource, nsmap))
            if not isinstance(value, DateTime):
                return None
            candidate = str(value)
        else:
            return None
    except (TypeError, ValueError, ArithmeticError):
        return None

    return candidate if _is_valid(xsd_type, candidate, resource, nsmap) else None


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return '0.0'
    if value == value.to_integral_value():
        value = value.quantize(Decimal(1))
    else:
        value = value.normalize()
    text = format(value, 'f')
    return text if '.' in text else text + '.0'


def _is_valid(xsd_type: XsdSimpleType,
              text: str,
              resource: XMLResource,
              nsmap: NsmapType) -> bool:
    context = _context(resource, nsmap)
    try:
        xsd_type.text_decode(text, 'strict', context)
    except Exception:
        return False
    return not context.errors


def _context(resource: XMLResource, nsmap: NsmapType) -> ValidationContext:
    mapper = NamespaceMapper(dict(nsmap), source=resource)
    context = ValidationContext(resource, converter=mapper)
    context.namespaces = mapper.namespaces
    return context


def _type_name(xsd_type: XsdSimpleType) -> str:
    return getattr(xsd_type, 'prefixed_name', None) or str(xsd_type)


def _element_path(elem: ElementType,
                  root: ElementType,
                  parent_map: dict[ElementType, Optional[ElementType]]) -> str:
    parts: list[str] = []
    current: Optional[ElementType] = elem

    while current is not None:
        if current is root:
            parts.append(_path_name(current.tag))
            break

        parent = parent_map.get(current)
        if parent is None:
            break

        position = 1
        for index, sibling in enumerate(parent, start=1):
            if sibling is current:
                position = index
                break

        parts.append(f'{_path_name(current.tag)}[{position}]')
        current = parent

    return '/' + '/'.join(reversed(parts))


def _path_name(tag: str) -> str:
    if tag.startswith('{'):
        namespace, local = tag[1:].split('}', 1)
        return f'{{{namespace}}}{local}'
    return tag


def _signed_subtrees(root: ElementType) -> set[ElementType]:
    excluded: set[ElementType] = set()
    id_index = _index_ids(root)

    for signature in root.iter(f'{{{nm.DSIG_NAMESPACE}}}Signature'):
        excluded.add(signature)
        for reference in signature.iter(f'{{{nm.DSIG_NAMESPACE}}}Reference'):
            uri = reference.attrib.get('URI', '').strip()
            if not uri:
                excluded.update(root.iter())
                continue
            if not uri.startswith('#'):
                continue

            fragment = unquote(uri[1:])
            target = _resolve_reference(fragment, root, id_index)
            if target is None:
                return set(root.iter())
            excluded.add(target)
            excluded.update(target.iter())

    return excluded


def _index_ids(root: ElementType) -> dict[str, ElementType]:
    result: dict[str, ElementType] = {}
    for elem in root.iter():
        for attr_name, value in elem.attrib.items():
            local = attr_name.rsplit('}', 1)[-1]
            if local.lower() == 'id':
                result.setdefault(value, elem)
    return result


def _resolve_reference(fragment: str,
                       root: ElementType,
                       id_index: dict[str, ElementType]):
    if '(' not in fragment:
        return id_index.get(fragment)

    if fragment in ('xpointer(/)', 'xpointer(/%s)' % root.tag):
        return root

    if fragment.startswith('element(') and fragment.endswith(')'):
        token = fragment[len('element('):-1].strip()
        if token and token[0] in '"\'' and token[-1] == token[0]:
            return id_index.get(token[1:-1])

    return None
