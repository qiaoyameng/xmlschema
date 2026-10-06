#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import re
import logging
import pprint
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Optional, Union
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from xmlschema.aliases import BaseXsdType, ElementType, NsmapType, SchemaType, XMLSourceType
from xmlschema.exceptions import XMLSchemaTypeError, XMLSchemaValueError, XMLResourceOSError
from xmlschema.names import XSD_SCHEMA, XSD_IMPORT, XSD_INCLUDE, \
    XSD_REDEFINE, XSD_OVERRIDE, XSI_TYPE
from xmlschema.utils.qnames import get_namespace
from xmlschema.utils.logger import logged
from xmlschema.utils.paths import LocationPath
from xmlschema.utils.urls import is_remote_url, normalize_url, match_location
from xmlschema.translation import gettext as _
from xmlschema.resources import XMLResource

logger = logging.getLogger('xmlschema')

FIND_PATTERN = r'\bschemaLocation\s*=\s*[\'"]([^\'"]*)[\'"]'
REPLACE_PATTERN = r'\bschemaLocation\s*=\s*[\'"]\s*{0}\s*[\'"]'


class _PrefixAllocator:
    def __init__(self, namespaces: NsmapType, root_default: str = '') -> None:
        self._preferred_prefixes: dict[str, list[str]] = {}
        self._used_prefixes: dict[str, str] = {}
        self._nonempty_prefixes: dict[str, str] = {}
        explicit_default_namespace = namespaces.get('', '')
        has_default_alias = any(
            prefix and uri == explicit_default_namespace
            for prefix, uri in namespaces.items()
        )
        self.default_namespace = root_default if not explicit_default_namespace \
            or has_default_alias else explicit_default_namespace
        self.suppress_document_default = bool(has_default_alias)
        self.default_used = False

        for prefix, uri in namespaces.items():
            if prefix and uri:
                self._preferred_prefixes.setdefault(uri, []).append(prefix)
                if prefix not in self._used_prefixes and \
                        uri not in self._nonempty_prefixes:
                    self._used_prefixes[prefix] = uri
                    self._nonempty_prefixes[uri] = prefix

    def prefix_for(self, uri: str, element: bool = False) -> str:
        return self.prefix_for_uri(uri, {}, element)

    def prefix_for_uri(self, uri: str, active: NsmapType, element: bool = False) -> str:
        if not uri:
            return ''
        elif uri in self._nonempty_prefixes:
            return self._nonempty_prefixes[uri]
        elif element and uri == self.default_namespace:
            self.default_used = True
            return ''

        preferred_prefix = next(
            (p for p in self._preferred_prefixes.get(uri, [])
             if p not in self._used_prefixes or self._used_prefixes[p] == uri),
            ''
        )
        prefix = preferred_prefix
        if not prefix:
            original_prefix = self._original_prefix(uri, active)
            prefix = original_prefix if original_prefix not in self._used_prefixes \
                else self._unused_prefix(original_prefix)
        if prefix in self._used_prefixes and self._used_prefixes[prefix] != uri:
            prefix = self._unused_prefix(prefix or 'default')
        self._used_prefixes[prefix] = uri
        self._nonempty_prefixes[uri] = prefix
        return prefix

    def _original_prefix(self, uri: str, active: NsmapType) -> str:
        for prefix, mapped_uri in active.items():
            if prefix and mapped_uri == uri:
                return prefix
        return 'default'

    def has_prefix_for(self, uri: str) -> bool:
        return uri in self._nonempty_prefixes

    def root_declarations(self, default_namespace: str) -> list[tuple[str, str]]:
        declarations = []
        if self.default_used and default_namespace and not self.suppress_document_default:
            declarations.append(('', default_namespace))
        declarations.extend(
            (prefix, uri) for prefix, uri in self._used_prefixes.items()
            if prefix and
            uri != 'http://www.w3.org/XML/1998/namespace' and
            self._nonempty_prefixes.get(uri) == prefix
        )
        return declarations

    def _unused_prefix(self, candidate: str) -> str:
        while candidate in self._used_prefixes:
            match = re.search(r'(\d+)$', candidate)
            if match:
                candidate = candidate[:match.start()] + str(int(match.group()) + 1)
            else:
                candidate += '0'
        return candidate


def _rewrite_typed_value(value: str, xsd_type: BaseXsdType, namespaces: NsmapType,
                        allocator: _PrefixAllocator,
                        context: Any = None) -> str:
    item_type: Optional[BaseXsdType] = None
    if xsd_type.is_list():
        item_type = xsd_type.item_type
    elif getattr(xsd_type.root_type, 'is_list', lambda: False)():
        item_type = xsd_type.root_type.item_type

    if item_type is not None:
        return _rewrite_qname_list(value, namespaces, allocator) \
            if _is_qname_type(item_type) else value

    if xsd_type.is_union():
        first_member: Optional[BaseXsdType] = None
        first_member_is_list = False
        for member_type in xsd_type.member_types:
            if member_type.is_list() and \
                    _is_qname_type(member_type.item_type) and \
                    _is_valid_lexical_value(member_type, value, context):
                first_member = member_type
                first_member_is_list = True
                break
            if member_type.is_union():
                rewritten = _rewrite_typed_value(
                    value, member_type, namespaces, allocator, context
                )
                if rewritten != value:
                    return rewritten
            elif member_type.text_is_valid(value, context):
                first_member = member_type
                break

        if first_member is None:
            return value
        elif first_member_is_list:
            return _rewrite_qname_list(value, namespaces, allocator)
        elif _is_qname_type(first_member):
            return _rewrite_qname_token(value, namespaces, allocator)
        return value

    return _rewrite_qname_token(value, namespaces, allocator) \
        if _is_qname_type(xsd_type) else value


def _rewrite_attribute_value(value: str, xsd_type: BaseXsdType | None,
                             namespaces: NsmapType,
                             allocator: _PrefixAllocator,
                             context: Any = None) -> str:
    if xsd_type is None:
        return value
    return _rewrite_typed_value(value, xsd_type, namespaces, allocator, context)


def _prepare_rewritten_tree(resource: XMLResource,
                            bindings: dict[ElementType, tuple[Any, BaseXsdType]],
                            allocator: _PrefixAllocator) -> dict[int, Any]:
    from xmlschema.validators import ValidationContext
    from xmlschema.namespaces import NamespaceMapper

    tag_rewrites: dict[ElementType, str] = {}
    attribute_rewrites: dict[tuple[ElementType, str], tuple[str, str]] = {}
    text_rewrites: dict[ElementType, str] = {}
    element_namespaces: dict[int, NsmapType] = {}

    for element in resource.root.iter():
        if not isinstance(element.tag, str):
            continue

        namespaces = resource.get_nsmap(element) or {}
        element_namespaces[id(element)] = namespaces
        mapper = NamespaceMapper(namespaces)
        context = ValidationContext(source=resource, converter=mapper)
        tag_rewrites[element] = _lexical_name(
            element.tag, namespaces, allocator, element=True,
            allow_default=namespaces.get('') == get_namespace(element.tag)
        )
        binding = bindings.get(element)
        xsd_element, xsd_type = binding if binding is not None else (None, None)
        attribute_group = xsd_element.get_attributes(xsd_type) \
            if xsd_element is not None and xsd_type is not None else {}

        for name, value in element.attrib.items():
            attribute_name = _lexical_name(
                name, namespaces, allocator, element=False
            )
            if name == XSI_TYPE:
                lexical = _rewrite_qname_token(value.strip(), namespaces, allocator)
            else:
                xsd_attribute = attribute_group.get(name)
                if xsd_attribute is None and xsd_type is not None:
                    xsd_attribute = xsd_type.schema.maps.attributes.get(name)
                attribute_type = xsd_attribute.type if xsd_attribute is not None else None
                lexical = _rewrite_attribute_value(
                    value, attribute_type, namespaces, allocator, context
                )
            attribute_rewrites[(element, name)] = attribute_name, lexical

        if element.text and not list(element) and xsd_type is not None:
            content_type = xsd_type.content \
                if xsd_type.is_complex() and xsd_type.has_simple_content() else xsd_type
            rewritten = _rewrite_typed_value(
                element.text, content_type, namespaces, allocator, context
            )
            if rewritten != element.text:
                text_rewrites[element] = rewritten

    return {
        'tags': tag_rewrites,
        'attributes': attribute_rewrites,
        'texts': text_rewrites,
        'namespaces': element_namespaces,
    }


def _serialize_rewritten_element(elem: ElementType, resource: XMLResource,
                                 bindings: dict[ElementType, tuple[Any, BaseXsdType]],
                                 allocator: _PrefixAllocator,
                                 root_default: str) -> str:
    rewrites = _prepare_rewritten_tree(resource, bindings, allocator)
    root_namespace = get_namespace(elem.tag)
    root_output_default = '' if allocator.has_prefix_for(root_namespace) \
        else root_default

    def serialize(element: ElementType, active: dict[str, str],
                  parent_default: str, output_parent_default: str) -> str:
        if not isinstance(element.tag, str):
            return serialize_non_element(element)

        declared = resource.get_xmlns(element)
        previous: dict[str, Optional[str]] = {}
        local_default: Optional[str] = None
        if declared:
            for prefix, uri in declared:
                previous[prefix] = active.get(prefix)
                if prefix:
                    if uri:
                        active[prefix] = uri
                    else:
                        active.pop(prefix, None)
                else:
                    local_default = uri

        current_default = local_default if local_default is not None else parent_default
        namespaces = {prefix: uri for prefix, uri in active.items() if uri}
        if current_default:
            namespaces[''] = current_default

        tag = rewrites['tags'][element]
        attributes = serialize_attributes(element)
        root_declarations = ''
        local_declaration = ''
        if element is elem:
            parts = []
            output_default = root_output_default
            if output_default:
                parts.append(f' xmlns={quoteattr(output_default)}')
            for prefix, uri in allocator.root_declarations(output_default):
                if prefix:
                    parts.append(f' xmlns:{prefix}={quoteattr(uri)}')
            root_declarations = ''.join(parts)
        else:
            element_namespace, output_prefix, tag = _serialize_element_name(
                element, rewrites, allocator
            )
            output_namespace = element_namespace if output_prefix else ''

            output_default = current_default if not output_prefix and \
                current_default == element_namespace else ''
            if not element_namespace and output_parent_default:
                local_declaration = ' xmlns=""'
                output_default = ''
            elif not output_prefix and element_namespace and \
                    current_default and current_default != output_parent_default:
                local_declaration = f' xmlns={quoteattr(current_default)}'
                output_default = current_default
        output = f'<{tag}{root_declarations}{local_declaration}{attributes}'
        children = list(element)
        text = rewrites['texts'].get(element, element.text or '')

        if not children and not text:
            output += '/>'
        else:
            output += '>'
            if text:
                output += escape(text)
            for child in children:
                output += serialize(
                    child, active, current_default, output_default
                )
                if child.tail:
                    output += escape(child.tail)
            output += f'</{tag}>'

        if declared:
            for prefix, previous_uri in previous.items():
                if previous_uri is None:
                    active.pop(prefix, None)
                else:
                    active[prefix] = previous_uri

        return output

    def serialize_attributes(element: ElementType) -> str:
        output_parts = []

        for name, value in element.attrib.items():
            attribute_name, lexical = rewrites['attributes'][(element, name)]
            output_parts.append(f' {attribute_name}={quoteattr(lexical)}')

        return ''.join(output_parts)

    def serialize_non_element(node: ElementType) -> str:
        tag = node.tag
        if callable(tag):
            class_name = getattr(tag, '__name__', '')
            if class_name == 'Comment':
                return f'<!--{node.text or ""}-->'
            elif class_name == 'ProcessingInstruction':
                return f'<?{node.text or ""} ?>'
        return ''

    initial_nsmap = resource.get_nsmap(elem) or {}
    root_active = {prefix: uri for prefix, uri in initial_nsmap.items() if prefix and uri}
    return serialize(
        elem, root_active, initial_nsmap.get('', ''), root_output_default
    )


def _lexical_name(extended_name: str, namespaces: NsmapType,
                  allocator: _PrefixAllocator, element: bool = False,
                  allow_default: bool = False) -> str:
    if extended_name.startswith('{'):
        uri, local_name = extended_name[1:].split('}', 1)
        prefix = allocator.prefix_for_uri(uri, namespaces, element and allow_default)
        return f'{prefix}:{local_name}' if prefix else local_name
    return extended_name


def _rewrite_qname_token(token: str, namespaces: NsmapType,
                         allocator: _PrefixAllocator) -> str:
    if not token or token[0] == '{' or not re.fullmatch(
            r'[A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?', token):
        return token

    if ':' in token:
        prefix, local_name = token.split(':', 1)
        try:
            uri = namespaces[prefix]
        except KeyError:
            return token
        return f'{allocator.prefix_for_uri(uri, namespaces)}:{local_name}' \
            if uri else local_name

    uri = namespaces.get('', '')
    return f'{allocator.prefix_for_uri(uri, namespaces)}:{token}' if uri else token


def _rewrite_qname_list(value: str, namespaces: NsmapType,
                        allocator: _PrefixAllocator) -> str:
    parts = re.split(r'(\s+)', value)
    return ''.join(_rewrite_qname_token(part, namespaces, allocator) if part.strip()
                   else part for part in parts)


def _serialize_element_name(element: ElementType, rewrites: dict[str, Any],
                            allocator: _PrefixAllocator) -> tuple[str, str, str]:
    element_namespace = get_namespace(element.tag)
    local_name = element.tag[1:].split('}', 1)[1] if element_namespace else element.tag
    target_prefix = ''
    if element_namespace:
        active_namespaces = rewrites['namespaces'].get(id(element), {})
        if element_namespace == allocator.default_namespace and \
                active_namespaces.get('') == element_namespace and \
                not allocator.has_prefix_for(element_namespace):
            target_prefix = ''
        else:
            target_prefix = allocator.prefix_for_uri(element_namespace, active_namespaces)
    tag = f'{target_prefix}:{local_name}' if target_prefix else local_name
    return element_namespace, target_prefix, tag


def _is_qname_type(xsd_type: BaseXsdType) -> bool:
    return xsd_type.is_qname() or xsd_type.is_notation()


def _is_valid_lexical_value(xsd_type: BaseXsdType, value: str, context: Any) -> bool:
    context.errors.clear()
    xsd_type.text_is_valid(value, context)
    return not context.errors


def rewrite_namespace_prefixes(source: Union[XMLResource, XMLSourceType],
                               schema: SchemaType,
                               namespaces: Optional[NsmapType] = None) -> str:
    """
    Rewrite namespace prefixes in an XML instance without changing expanded names.

    QName-valued element and attribute text is rewritten only when the associated
    schema component derives from ``xs:QName`` or ``xs:NOTATION``. List, union and
    list/union members are handled by their effective member type.

    :param source: the XML instance, preferably an :class:`XMLResource` parsed from
        text so local namespace declarations can be tracked.
    :param schema: the schema used to identify QName-valued nodes and
        ``xsi:type`` content.
    :param namespaces: maps the requested non-empty prefixes (and optionally the
        empty prefix for a default namespace) to namespace URIs.
    :return: the rewritten XML document as text. Element order, character data and
        expanded element/attribute/QName names are preserved.
    """
    if not isinstance(source, XMLResource):
        source = XMLResource(source)

    root_nsmap = source.get_nsmap(source.root)
    if root_nsmap is None:
        msg = _("XMLResource must be parsed from XML text to preserve namespace scopes")
        raise XMLSchemaTypeError(msg)

    bindings: dict[ElementType, tuple[Any, BaseXsdType]] = {}
    pending_elements: list[ElementType] = []

    def validation_hook(elem: ElementType, xsd_element: Any) -> None:
        pending_elements.append(elem)

    def element_hook(element_data: Any, xsd_element: Any,
                     xsd_type: BaseXsdType) -> Any:
        if pending_elements:
            bindings[pending_elements.pop()] = xsd_element, xsd_type
        return element_data

    allocator = _PrefixAllocator(namespaces or {}, root_nsmap.get('', ''))

    list(schema.iter_decode(
        source,
        validation='skip',
        process_namespaces=True,
        converter=None,
        validation_hook=validation_hook,
        element_hook=element_hook,
    ))

    return _serialize_rewritten_element(
        source.root, source, bindings, allocator, allocator.default_namespace
    )


@dataclass
class XsdSource:
    """Class for keeping track of an XSD schema source."""
    path: LocationPath
    resource: XMLResource

    def __init__(self, path: LocationPath, resource: XMLResource) -> None:
        self.path = path
        self.resource = resource
        self.text = resource.get_text()
        self.processed = False
        self.modified = False
        self.substitutions: Optional[list[tuple[str, str]]] = None

    @property
    def schema_locations(self) -> set[str]:
        """Extract schema locations from XSD resource tree."""
        locations = set()
        for child in self.resource.root:
            if child.tag in (XSD_IMPORT, XSD_INCLUDE, XSD_REDEFINE, XSD_OVERRIDE):
                schema_location = child.get('schemaLocation', '').strip()
                if schema_location:
                    locations.add(schema_location)

        return locations

    def replace_location(self, location: str, repl_location: str) -> None:
        if location == repl_location:
            return

        logger.debug("Replace location %r with %r", location, repl_location)
        repl = f'schemaLocation="{repl_location}"'
        pattern = REPLACE_PATTERN.format(re.escape(location))
        self.text = re.sub(pattern, repl, self.text)
        self.modified = True

    def get_location_path(self, location: str,
                          ref: Union[SchemaType, XMLResource],
                          modify: bool = True) -> LocationPath:
        """
        Return a relative location path for the referred XSD schema, replacing
        the original location in the schema source, if necessary.
        """
        parts: Any

        if is_remote_url(location):
            parts = urlsplit(unquote(location))
            path = LocationPath(parts.scheme). \
                joinpath(parts.netloc). \
                joinpath(parts.path.lstrip('/'))
        else:
            if location.startswith('file:/'):
                path = LocationPath(unquote(urlsplit(location).path))
            else:
                path = LocationPath(unquote(location))

            if not path.is_absolute():
                path = self.path.parent.joinpath(path).normalize()
                if not str(path).startswith('..'):
                    # A relative path that doesn't exceed the loading schema dir
                    return path

                # Use the absolute resource path
                path = LocationPath(ref.filepath)  # type: ignore[arg-type]

            if path.drive:
                drive = path.drive.split(':')[0]
                path = LocationPath(drive).joinpath('/'.join(path.parts[1:]))

            path = LocationPath('file').joinpath(path.as_posix().lstrip('/'))

        if path.is_absolute():
            raise XMLSchemaValueError(f'Replacing path {path} is not relative!')

        # Obtain the replacement location
        parts = path.parent.parts
        dir_parts = self.path.parent.parts

        k = 0
        for item1, item2 in zip(parts, dir_parts):
            if item1 != item2:
                break
            k += 1

        if not k:
            prefix = '/'.join(['..'] * len(dir_parts))
            repl_path = LocationPath(prefix).joinpath(path)
        else:
            repl_path = LocationPath('/'.join(parts[k:])).joinpath(path.name)
            if k < len(dir_parts):
                prefix = '/'.join(['..'] * (len(dir_parts) - k))
                repl_path = LocationPath(prefix).joinpath(repl_path)

        repl_location = repl_path.as_posix()
        if location != repl_location:
            if self.substitutions is None:
                self.substitutions = []
            self.substitutions.append((location, repl_location))

            if modify:
                self.replace_location(location, repl_location)

        return path


def save_sources(target: Union[str, Path],
                 sources: Iterable[XsdSource],
                 save_locations: bool = False) -> dict[str, str]:
    """Save XSD sources to a target directory."""
    target_path = Path(target) if isinstance(target, str) else target
    if target_path.is_dir():
        if list(target_path.iterdir()):
            msg = _("target directory {} is not empty")
            raise XMLSchemaValueError(msg.format(target))
    elif target_path.exists():
        msg = _("target {} is not a directory")
        raise XMLSchemaValueError(msg.format(target_path.parent))
    elif not target_path.parent.exists():
        msg = _("target parent directory {} does not exist")
        raise XMLSchemaValueError(msg.format(target_path.parent))
    elif not target_path.parent.is_dir():
        msg = _("target parent {} is not a directory")
        raise XMLSchemaValueError(msg.format(target_path.parent))

    location_map = {}

    for src in sources:
        assert src.processed

        filepath = target_path.joinpath(src.path)

        # Safety check: raise error if filepath is not inside the target path
        try:
            filepath.resolve(strict=False).relative_to(target_path.resolve(strict=False))
        except ValueError:
            msg = _("target directory {} violation for exported path {}, {}")
            raise XMLSchemaValueError(msg.format(target, str(src.path), str(filepath)))

        if not filepath.parent.exists():
            filepath.parent.mkdir(parents=True)

        encoding = 'utf-8'  # default encoding for XML 1.0

        if src.text.startswith('<?'):
            # Get the encoding from XML declaration
            xml_declaration = src.text.split('\n', maxsplit=1)[0]
            re_match = re.search('(?<=encoding=["\'])[^"\']+', xml_declaration)
            if re_match is not None:
                encoding = re_match.group(0).lower()

        if src.modified:
            logger.info("Write modified XSD source to %s", filepath)
        else:
            logger.info("Write unchanged XSD source to %s", filepath)

        if src.substitutions:
            for location, repl_location in src.substitutions:
                if location not in location_map:
                    location_map[location] = repl_location
                elif repl_location != location_map[location]:
                    logger.warning("Substitution collision for location %r: %r != %r",
                                   location, repl_location, location_map[location])

        with filepath.open(mode='w', encoding=encoding) as fp:
            fp.write(src.text)

    if save_locations:
        with target_path.joinpath('__init__.py').open('w') as fp:
            logger.info("Write LOCATION_MAP to %s", fp.name)
            fp.write(f'LOCATION_MAP = {pprint.pformat(location_map)}')

    return location_map


@logged
def export_schema(schema: SchemaType,
                  target: Union[str, Path],
                  save_remote: bool = False,
                  remove_residuals: bool = True,
                  exclude_locations: Optional[list[str]] = None,
                  loglevel: Optional[Union[str, int]] = None) -> dict[str, str]:
    """
    Export XSD sources used by a schema instance to a target directory.
    Don't use this function directly, use XMLSchema.export() method instead.
    """
    def residuals_filter(x: str) -> bool:
        return is_remote_url(x) and x not in schema.includes and \
            (exclude_locations is None or x not in exclude_locations)

    if loglevel is not None:
        logger.info("Export schema using loglevel %r", loglevel)

    name = schema.name or 'schema.xsd'
    exports = {schema: XsdSource(LocationPath(name), schema.source)}
    path: Any

    if exclude_locations is None:
        exclude_locations = []

    logger.debug("Start export of schema %r", name)

    while True:
        current_length = len(exports)

        for schema in list(exports):
            schema_source = exports[schema]
            if schema_source.processed:
                continue  # Skip already processed schemas

            schema_source.processed = True
            logger.debug("Process schema instance %r", schema)

            schema_locations = schema_source.schema_locations

            imports_items = [(x.url, x) for x in schema.imports.values()
                             if x is not None and x.meta_schema is not None]

            for location, ref_schema in chain(schema.includes.items(), imports_items):
                if not location:
                    continue
                elif location in exclude_locations or not save_remote and is_remote_url(location):
                    logger.debug("Location %r is excluded by argument", location)
                    continue

                # Find matching schema location
                location_match = match_location(location, schema_locations)
                if location_match is None:
                    logger.debug("Unmatched location %r, skip ...", location)
                    continue

                location = location_match
                logger.debug("Matched location %r", location)
                schema_locations.remove(location)

                path = schema_source.get_location_path(location, ref_schema)
                if ref_schema not in exports:
                    exports[ref_schema] = XsdSource(path, ref_schema.source)

            if remove_residuals:
                # Deactivate residual redundant imports from remote URLs
                for location in filter(residuals_filter, schema_locations):
                    logger.debug("Clear residual remote location %r", location)
                    schema_source.replace_location(location, '')

        if current_length == len(exports):
            break

    return save_sources(target, exports.values())


@logged
def download_schemas(url: str,
                     target: Union[str, Path],
                     save_remote: bool = True,
                     save_locations: bool = True,
                     modify: bool = False,
                     defuse: str = 'remote',
                     timeout: int = 300,
                     exclude_locations: Optional[list[str]] = None,
                     loglevel: Optional[Union[str, int]] = None) -> dict[str, str]:
    """
    Download one or more schemas from a URL and save them in a target directory. All the
    referred locations in schema sources are downloaded and stored in the target directory.

    :param url: The URL of the schema to download, usually a remote one.
    :param target: the target directory to save the schema.
    :param save_remote: if to save remote schemas, defaults to `True`.
    :param save_locations: for default save a LOCATION_MAP dictionary to a `__init__.py`, \
    that can be imported in your code to provide a *uri_mapper* argument for build the \
    schema instance. Provide `False` to skip the package file creation in the target \
    directory.
    :param modify: provide `True` to modify original schemas, defaults to `False`.
    :param defuse: when to defuse XML data before loading, defaults to `'remote'`.
    :param timeout: the timeout in seconds for the connection attempt in case of remote data.
    :param exclude_locations: provide a list of locations to skip.
    :param loglevel: for setting a different logging level for schema downloads call.
    :return: a dictionary containing the map of modified locations.
    """
    if loglevel is not None:
        logger.info("Download schemas using loglevel %r", loglevel)

    resource = XMLResource(url, defuse=defuse, timeout=timeout)
    logger.info("Downloaded XML resource from %s", url)
    if resource.root.tag != XSD_SCHEMA:
        raise XMLSchemaValueError(f'Resource referred by {url} is not a XSD schema')

    name = resource.name
    downloads = {
        resource: XsdSource(LocationPath(name), resource)  # type: ignore[arg-type]
    }
    path: Any

    if exclude_locations is None:
        exclude_locations = []

    logger.debug("Start download of schema resource %r", name)

    while True:
        current_length = len(downloads)

        for resource in list(downloads):
            schema_source = downloads[resource]
            if schema_source.processed:
                continue  # Skip already processed schemas

            schema_source.processed = True
            logger.debug("Process schema resource %r", resource)
            schema_locations = schema_source.schema_locations

            for location in schema_locations:
                if location in exclude_locations or not save_remote and is_remote_url(location):
                    logger.debug("Location %r is excluded by argument", location)
                    continue

                url = normalize_url(location, resource.base_url)
                if any(x.url == url for x in downloads):
                    continue

                try:
                    ref_resource = XMLResource(url, defuse=defuse, timeout=timeout)
                except (OSError, XMLResourceOSError) as err:
                    logger.error('Error accessing resource at URL %s: %s', url, err)
                    continue
                except ElementTree.ParseError as err:
                    logger.error('Error parsing XML resource at URL %s: %s', url, err)
                    continue
                else:
                    logger.info("Downloaded XML resource from %s", url)

                if ref_resource.root.tag != XSD_SCHEMA:
                    logger.error('XML resource at URL %s is not an XSD schema', url)
                    continue

                path = schema_source.get_location_path(location, ref_resource, modify)
                downloads[ref_resource] = XsdSource(path, ref_resource)

        if current_length == len(downloads):
            break

    return save_sources(target, downloads.values(), save_locations)
