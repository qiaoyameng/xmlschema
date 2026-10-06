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
Schema-aware rewriting of XML namespace prefixes.

A blind textual replacement of a namespace prefix is unsafe: lexical QName
values contained in attributes or element text (e.g. ``xsi:type``) keep
pointing at the old prefix binding, and ordinary strings that happen to
contain a colon would be corrupted. This module performs the rewriting using
the XSD schema to tell QName typed values apart from plain strings.

Element order, text and tail nodes and the expanded (namespace, local name)
identity of every element and attribute are preserved. The output declares
exactly the prefix bindings needed at each scope, so default namespaces,
local prefix redefinitions (shadowing) and no-namespace names are all
supported. Colliding preferred prefixes get a deterministic numeric suffix.
"""
import re
import io
from xml.etree import ElementTree
from typing import Any, Optional, Union, TYPE_CHECKING

from xmlschema.aliases import NsmapType
from xmlschema.exceptions import XMLSchemaTypeError, XMLSchemaValueError
from xmlschema.names import XML_NAMESPACE, XSI_NAMESPACE, XSI_TYPE
from xmlschema.utils.qnames import get_extended_qname

if TYPE_CHECKING:
    from xmlschema.validators import XsdElement, XsdType
    from xmlschema.validators.simple_types import XsdSimpleType

__all__ = ('rewrite_namespace_prefixes',)

_RESERVED_PREFIXES = frozenset({'xml', 'xmlns'})
XML_PREFIX = 'xml'

# Matches a lexical QName token used inside a QName typed value.
_QNAME_TOKEN = re.compile(
    r'^(?:(?P<prefix>[^\d\W][\w\-.\u00B7\u0300-\u036F\u0387\u06DD\u06DE\u203F\u2040]*):)?'
    r'(?P<local>[^\d\W][\w\-.\u00B7\u0300-\u036F\u0387\u06DD\u06DE\u203F\u2040]*)$'
)


def rewrite_namespace_prefixes(source: Any,
                               namespaces: Optional[NsmapType] = None,
                               schema: Any = None,
                               encoding: str = 'unicode',
                               xml_declaration: Optional[bool] = None) \
        -> Union[str, bytes]:
    """
    Rewrite the namespace prefixes of an XML document.

    :param source: the XML source: a filename, a file object, a bytes/str \
    containing XML or an ElementTree's :class:`Element`.
    :param namespaces: optional mapping of *preferred* prefixes to namespace \
    URIs. The empty prefix ``''`` is allowed and means that a default \
    namespace should keep (or get) an empty prefix.
    :param schema: an optional XSD schema used to detect QName typed element \
    and attribute values, including xs:list and xs:union members. Without a \
    schema only element/attribute names and the ``xsi:type`` attribute are \
    rewritten.
    :param encoding: for default ``'unicode'`` returns a ``str``; otherwise \
    the name of the encoding used for the returned ``bytes`` value.
    :param xml_declaration: if `True` prepends an XML declaration to output.
    :return: the serialized XML with rewritten prefixes.

    .. note:: When *source* is a plain ElementTree :class:`Element` (Clark
       notation) the original ``xmlns`` declarations are not available, so
       prefixes are synthesized and lexical QName values written with
       non-synthesized prefixes may not be resolvable. Passing the XML text
       or a file object gives exact scope information.
    """
    root, declarations, scopes = _parse_source(source, namespaces)
    preferred = _check_preferred(namespaces)

    analysis = _analyze(root, declarations, scopes, schema)
    used_uris = set(preferred.values()) | analysis['used_uris']
    uri_to_prefix = _allocate_prefixes(preferred, used_uris, analysis['doc_pref'])

    output = _serialize(root, scopes, analysis, uri_to_prefix)

    if xml_declaration:
        if encoding == 'unicode':
            output = '<?xml version="1.0" encoding="utf-8"?>\n' + output
        else:
            declaration = f'<?xml version="1.0" encoding="{encoding}"?>\n'
            output = (declaration + output).encode(encoding)
    elif encoding != 'unicode':
        output = output.encode(encoding)

    return output


# Parsing and scope discovery ----------------------------------------------

def _parse_source(source: Any,
                  namespaces: Optional[NsmapType] = None) \
        -> tuple[ElementTree.Element,
                 dict[int, list[tuple[str, str]]],
                 dict[int, dict[str, str]]]:
    if hasattr(source, 'tag') and hasattr(source, 'attrib'):
        root = source
        preferred = _check_preferred(namespaces)
        return _scan_tree(root, preferred)
    elif isinstance(source, (str, bytes, bytearray)):
        data: Any
        if isinstance(source, str):
            if source.lstrip()[:1] == '<':
                data = io.StringIO(source)
            else:
                # Not an inline document: treat as a filesystem path.
                data = open(source, 'rb')
        else:
            data = io.BytesIO(bytes(source))
    elif hasattr(source, 'read'):
        data = source
    else:
        raise XMLSchemaTypeError(f"invalid XML source type {type(source)!r}")

    return _stream_parse(data)


def _stream_parse(data: Any) \
        -> tuple[ElementTree.Element,
                 dict[int, list[tuple[str, str]]],
                 dict[int, dict[str, str]]]:
    parser = ElementTree.XMLParser()
    # Registering a target is not needed: start-ns/end-ns events are enough
    # to follow namespace scopes alongside the tree built by iterparse.
    events = ElementTree.iterparse(data, events=('start-ns', 'end-ns',
                                                 'start', 'end'),
                                   parser=parser)

    declarations: dict[int, list[tuple[str, str]]] = {}
    scopes: dict[int, dict[str, str]] = {}

    # A frame is [declared_list, parent_scope, started(Element|None), scope].
    # start-ns events of an element arrive *after* its parent start event and
    # *before* the element start event, so pending declarations require a
    # dedicated open frame even before the element itself is reported.
    frames: list[list] = [[[], {XML_PREFIX: XML_NAMESPACE}, None, None]]
    # Depth according to element start/end events; frames may be opened early
    # for declarations, so frame stack depth can exceed the element depth.
    root: Optional[ElementTree.Element] = None

    for event, value in events:
        if event == 'start-ns':
            if frames[-1][2] is not None:
                # Parent element already started: open the child frame that
                # these declarations belong to.
                frames.append([[], frames[-1][3], None, None])
            frames[-1][0].append(value)
        elif event == 'end-ns':
            # With expat end-ns always arrives after the matching element's
            # end event, and frames are already popped there.
            continue
        elif event == 'start':
            elem = value
            if frames[-1][2] is not None:
                # No start-ns events opened a frame for this element (it has
                # no new namespace declarations): push a plain child frame.
                frames.append([[], frames[-1][3], None, None])
            frame = frames[-1]
            declared = list(frame[0])
            scope = dict(frame[1])
            scope.update(declared)
            frame[2] = elem
            frame[3] = scope

            declarations[id(elem)] = declared
            scopes[id(elem)] = scope
            if root is None:
                root = elem
        else:  # 'end'
            frames.pop()

    if root is None:
        raise XMLSchemaValueError("the XML source does not contain any element")
    return root, declarations, scopes


def _scan_tree(root: ElementTree.Element,
               preferred: Optional[dict[str, str]] = None) \
        -> tuple[ElementTree.Element,
                 dict[int, list[tuple[str, str]]],
                 dict[int, dict[str, str]]]:
    """
    Fallback for a plain Element input: original xmlns declarations are not
    recoverable from a Clark-notation tree, so a single synthetic declaration
    set is attached to the root, with generated prefixes for each URI.
    """
    declarations: dict[int, list[tuple[str, str]]] = {id(root): []}
    scopes: dict[int, dict[str, str]] = {}
    in_scope = {XML_PREFIX: XML_NAMESPACE}
    for uri, prefix in _synthetic_declarations(root, preferred):
        declarations[id(root)].append((prefix, uri))
        in_scope[prefix] = uri

    for elem in root.iter():
        scopes[id(elem)] = in_scope

    return root, declarations, scopes


def _synthetic_declarations(root: ElementTree.Element,
                            preferred: Optional[dict[str, str]] = None):
    generated: dict[str, str] = {}
    counter = 0
    used_prefixes: set[str] = set()
    preferred = preferred or {}

    def add_uri(uri: str):
        nonlocal counter
        if not uri or uri in generated:
            return
        wanted = next((p for p, u in preferred.items() if u == uri and p),
                      None)
        if wanted and wanted not in used_prefixes:
            prefix = wanted
        elif uri == XSI_NAMESPACE and 'xsi' not in used_prefixes:
            prefix = 'xsi'
        else:
            prefix = f'ns{counter}'
            while prefix in used_prefixes:
                counter += 1
                prefix = f'ns{counter}'
            counter += 1
        generated[uri] = prefix
        used_prefixes.add(prefix)
        yield uri, prefix

    for elem in root.iter():
        tag = elem.tag
        if isinstance(tag, str) and tag[:1] == '{':
            yield from add_uri(tag[1:].split('}', 1)[0])
        for attr in elem.attrib:
            if isinstance(attr, str) and attr[:1] == '{':
                yield from add_uri(attr[1:].split('}', 1)[0])


# Prefix allocation ---------------------------------------------------------

def _check_preferred(namespaces: Optional[NsmapType]) -> dict[str, str]:
    if namespaces is None:
        return {}
    preferred: dict[str, str] = {}
    uris: set[str] = set()
    for prefix, uri in namespaces.items():
        if not isinstance(prefix, str) or not isinstance(uri, str):
            raise XMLSchemaTypeError(
                "namespaces must map prefix strings to URI strings")
        if prefix in _RESERVED_PREFIXES:
            raise XMLSchemaValueError(
                f"prefix {prefix!r} is reserved and cannot be rebound")
        if uri in uris:
            raise XMLSchemaValueError(
                f"namespace {uri!r} is mapped by more than one preferred prefix")
        uris.add(uri)
        preferred[prefix] = uri
    return preferred


def _next_free_prefix(prefix: str, taken: set[str]) -> str:
    candidate = prefix or 'default'
    if candidate not in taken:
        return candidate
    match = re.search(r'(\d+)$', candidate)
    if match:
        base = candidate[:match.start()]
        index = int(match.group(1)) + 1
    else:
        base = candidate
        index = 0
    while True:
        candidate = f'{base}{index}'
        if candidate not in taken:
            return candidate
        index += 1


def _allocate_prefixes(preferred: dict[str, str],
                       used_uris: set[str],
                       doc_pref: dict[str, tuple[str, int]]) -> dict[str, str]:
    """
    Assigns a global canonical prefix to every used URI. Caller preferred
    prefixes win; then the conventional 'xsi' prefix; then the prefixes used
    in the source document (in first occurrence order). Collisions get a
    deterministic numeric suffix (``name0``, ``name1`` ...).
    """
    uri_to_prefix: dict[str, str] = {XML_NAMESPACE: XML_PREFIX}
    taken: set[str] = set(_RESERVED_PREFIXES)

    default_uri = preferred.get('', '')
    preferred = {p: u for p, u in preferred.items() if p}

    # Caller-provided preferred prefixes first (insertion order preserved).
    for prefix, uri in preferred.items():
        if uri == XML_NAMESPACE or uri in uri_to_prefix:
            continue
        assigned = _next_free_prefix(prefix, taken)
        uri_to_prefix[uri] = assigned
        taken.add(assigned)

    if XSI_NAMESPACE in used_uris and XSI_NAMESPACE not in uri_to_prefix:
        assigned = _next_free_prefix('xsi', taken)
        uri_to_prefix[XSI_NAMESPACE] = assigned
        taken.add(assigned)

    # Source document prefixes, by first occurrence order of their binding.
    # An empty source prefix identifies a namespace actually declared as
    # default: preserve it, unless the caller explicitly requested another
    # URI for the empty prefix.
    for uri, (prefix, _order) in sorted(doc_pref.items(),
                                        key=lambda kv: kv[1][1]):
        if uri == XML_NAMESPACE or uri in uri_to_prefix:
            continue
        if not prefix and not default_uri:
            assigned = ''
        elif uri == default_uri and '' not in taken:
            assigned = ''
        else:
            assigned = _next_free_prefix(prefix or 'default', taken)
        uri_to_prefix[uri] = assigned
        taken.add(assigned)

    # A caller requested empty prefix, if not already consumed above.
    if default_uri and default_uri != XML_NAMESPACE \
            and '' not in taken and default_uri not in uri_to_prefix:
        uri_to_prefix[default_uri] = ''
        taken.add('')

    # Remaining URIs get generated prefixes (ns0, ns1, ... or next free).
    generated = 0
    for uri in sorted(used_uris):
        if uri == XML_NAMESPACE or uri in uri_to_prefix:
            continue
        assigned = _next_free_prefix(f'ns{generated}', taken)
        generated += 1
        uri_to_prefix[uri] = assigned
        taken.add(assigned)

    return uri_to_prefix


# Schema navigation ---------------------------------------------------------

def _resolve_root_element(schema: Any,
                          root: ElementTree.Element) -> Optional['XsdElement']:
    if schema is None or not isinstance(root.tag, str):
        return None
    try:
        element = schema.maps.elements.get(root.tag)
    except Exception:
        return None
    return element if element is not None and hasattr(element, 'type') else None


def _child_elements(xsd_element: 'XsdElement') \
        -> dict[str, 'XsdElement']:
    xsd_type = xsd_element.type
    children: dict[str, 'XsdElement'] = {}
    content = xsd_type.content if xsd_type.is_complex() else None
    if content is not None and hasattr(content, 'iter_elements'):
        for child in content.iter_elements():
            children.setdefault(child.name, child)
    return children


def _qname_member_types(xsd_type: 'XsdSimpleType') \
        -> list['XsdSimpleType']:
    """
    Returns the QName/NOTATION based atomic member types of a simple type:
    the type itself when atomic, the QName members of a union, or the QName
    item type (or its union members) of a list. An empty list means the
    type never carries a lexical QName.
    """
    try:
        if xsd_type.is_list():
            item_type = xsd_type.item_type
            if item_type.is_union():
                return [m for m in item_type.member_types if m.is_qname()]
            return [item_type] if item_type.is_qname() else []
        elif xsd_type.is_union():
            return [m for m in xsd_type.member_types if m.is_qname()]
        elif xsd_type.is_qname():
            return [xsd_type]
    except Exception:
        return []
    return []



# Analysis ------------------------------------------------------------------

# QName value descriptors:
#   ('atomic', [qname_member_types])  whole value is a single QName token
#   ('union',  [qname_member_types])  validate members in order per token
#   ('list',   [qname_member_types])  whitespace separated QName tokens


def _effective_type(elem: ElementTree.Element,
                    xsd_element: 'XsdElement',
                    scope: dict[str, str]) -> 'XsdType':
    xsd_type = xsd_element.type
    xsi_value = elem.get(XSI_TYPE)
    if xsi_value is not None:
        try:
            extended = get_extended_qname(xsi_value.strip(), scope)
            if extended[:1] == '{':
                resolved = xsd_element.maps.types.get(extended)
                if resolved is not None:
                    return resolved
        except Exception:
            pass
    return xsd_type


def _value_uris(value: str, scope: dict[str, str],
                attr_context: bool, list_value: bool = False) -> set[str]:
    """Collects the namespace URIs referenced by QName tokens of a value."""
    uris: set[str] = set()
    tokens = value.split() if list_value else [value.strip()]
    for token in tokens:
        if ':' not in token:
            if not attr_context:
                uri = scope.get('')
                if uri:
                    uris.add(uri)
            continue
        prefix = token.split(':', 1)[0]
        uri = scope.get(prefix)
        if uri:
            uris.add(uri)
    return uris


def _analyze(root: ElementTree.Element,
             declarations: dict[int, list[tuple[str, str]]],
             scopes: dict[int, dict[str, str]],
             schema: Any) -> dict[str, Any]:
    used_uris: set[str] = set()
    # First-occurrence preference: URI -> (source prefix, order index).
    doc_pref: dict[str, tuple[str, int]] = {}
    qname_attrs: dict[int, dict[str, tuple]] = {}
    qname_text: dict[int, tuple] = {}

    def add_tag_uris(elem: ElementTree.Element):
        tag = elem.tag
        if isinstance(tag, str) and tag[:1] == '{':
            used_uris.add(tag[1:].split('}', 1)[0])
        for name in elem.attrib:
            if isinstance(name, str) and name[:1] == '{':
                used_uris.add(name[1:].split('}', 1)[0])

    order = 0
    for elem in root.iter():
        scope = scopes[id(elem)]
        for prefix, uri in declarations.get(id(elem), ()):
            if uri and uri != XML_NAMESPACE and uri not in doc_pref:
                doc_pref[uri] = (prefix, order)
                order += 1
        add_tag_uris(elem)
        if XSI_TYPE in elem.attrib:
            xsi_value = elem.attrib[XSI_TYPE]
            used_uris.update(_value_uris(xsi_value, scope, True))

    association = _associate_schema(root, _resolve_root_element(schema, root))

    for elem in root.iter():
        xsd_element = association.get(id(elem))
        if xsd_element is None:
            continue

        scope = scopes[id(elem)]
        xsd_type = _effective_type(elem, xsd_element, scope)

        attrs_info: dict[str, tuple] = {}
        try:
            attribute_group = xsd_element.get_attributes(xsd_type)
        except Exception:
            attribute_group = {}
        for attr_name in elem.attrib:
            if not isinstance(attr_name, str):
                continue
            xsd_attribute = attribute_group.get(attr_name)
            if xsd_attribute is None or not hasattr(xsd_attribute, 'type'):
                continue
            members = _qname_member_types(xsd_attribute.type)
            if not members:
                continue
            if xsd_attribute.type.is_list():
                kind = 'list'
                used_uris.update(_value_uris(elem.attrib[attr_name], scope,
                                             True, True))
            elif xsd_attribute.type.is_union():
                kind = 'union'
                if _qname_members_match(elem.attrib[attr_name], members,
                                        scope, True):
                    used_uris.update(_value_uris(elem.attrib[attr_name],
                                                 scope, True))
            else:
                kind = 'atomic'
                used_uris.update(_value_uris(elem.attrib[attr_name], scope,
                                             True))
            attrs_info[attr_name] = (kind, members)
        if attrs_info:
            qname_attrs[id(elem)] = attrs_info

        if elem.text:
            simple_type = _simple_content_type(xsd_type)
            if simple_type is not None:
                members = _qname_member_types(simple_type)
                if members:
                    if simple_type.is_list():
                        qname_text[id(elem)] = ('list', members)
                        used_uris.update(_value_uris(elem.text, scope,
                                                     False, True))
                    elif simple_type.is_union():
                        qname_text[id(elem)] = ('union', members)
                        if _qname_members_match(elem.text, members,
                                                scope, False):
                            used_uris.update(_value_uris(elem.text, scope,
                                                         False))
                    else:
                        qname_text[id(elem)] = ('atomic', members)
                        used_uris.update(_value_uris(elem.text, scope, False))

    return {
        'used_uris': used_uris,
        'doc_pref': doc_pref,
        'qname_attrs': qname_attrs,
        'qname_text': qname_text,
        'local_undeclare_default': _local_default_undeclarations(
            root, declarations, scopes),
    }


def _local_default_undeclarations(
        root: ElementTree.Element,
        declarations: dict[int, list[tuple[str, str]]],
        scopes: dict[int, dict[str, str]]) -> set[int]:
    """Element ids that undeclare the default namespace with xmlns=\"\"."""
    return {
        id(elem)
        for elem in root.iter()
        if any(prefix == '' and not uri
               for prefix, uri in declarations.get(id(elem), ()))
    }


def _associate_schema(root: ElementTree.Element,
                      root_element: Optional['XsdElement']) \
        -> dict[int, 'XsdElement']:
    """Maps tree elements to their XSD element declaration."""
    association: dict[int, 'XsdElement'] = {}
    if root_element is None:
        return association

    def walk(elem: ElementTree.Element, xsd_element: 'XsdElement') -> None:
        association[id(elem)] = xsd_element
        child_map = _child_elements(xsd_element)
        for child in elem:
            if not isinstance(child.tag, str):
                continue
            child_decl = child_map.get(child.tag)
            if child_decl is None:
                child_decl = _match_substitute(child_map, child.tag)
            if child_decl is not None:
                walk(child, child_decl)

    walk(root, root_element)
    return association


def _simple_content_type(xsd_type: 'XsdType') -> Optional['XsdSimpleType']:
    try:
        if xsd_type.is_complex():
            content = xsd_type.content
            if content is not None and not content.is_complex() \
                    and content.is_simple():
                return content
            return None
        return xsd_type
    except Exception:
        return None


def _match_substitute(child_map: dict[str, 'XsdElement'],
                      tag: str) -> Optional['XsdElement']:
    for child in child_map.values():
        try:
            for substitute in child.iter_substitutes():
                if substitute.name == tag:
                    return substitute
        except Exception:
            continue
    return None


# QName token rewriting ------------------------------------------------------

def _resolve_token(token: str, scope: dict[str, str],
                   attr_context: bool) -> Optional[str]:
    """
    Resolves a lexical QName token against an in-scope namespace map.
    Returns the extended name (``{uri}local``) or a bare local name, or `None`
    if the token is not a valid QName or its prefix is unbound.
    """
    match = _QNAME_TOKEN.match(token)
    if match is None:
        return None
    prefix = match.group('prefix')
    local = match.group('local')
    if prefix is None:
        if attr_context:
            return local
        uri = scope.get('')
        return f'{{{uri}}}{local}' if uri else local
    uri = scope.get(prefix)
    if uri is None:
        return None
    return f'{{{uri}}}{local}' if uri else local


def _render_token(extended_name: str, uri_to_prefix: dict[str, str],
                  attr_context: bool,
                  auxiliary: Optional[dict[str, str]] = None) -> str:
    if extended_name[:1] != '{':
        # No-namespace token: stays a bare local name.
        return extended_name
    uri, local = extended_name[1:].split('}', 1)
    prefix = uri_to_prefix.get(uri)
    if not prefix:
        # No non-empty prefix is bound to this URI. Every referenced URI is
        # present in uri_to_prefix, so this only happens when the URI is the
        # default namespace; a QName lexical value cannot use the empty
        # prefix, so fall back to an auxiliary deterministic prefix.
        assert auxiliary is not None
        prefix = auxiliary.get(uri)
        if not prefix:
            prefix = _auxiliary_prefix(uri, uri_to_prefix, auxiliary)
    return f'{prefix}:{local}'


def _auxiliary_prefix(uri: str,
                      uri_to_prefix: dict[str, str],
                      auxiliary: dict[str, str]) -> str:
    existing = auxiliary.get(uri)
    if existing:
        return existing
    taken = set(uri_to_prefix.values())
    taken.update(auxiliary.values())
    prefix = _next_free_prefix('default', taken)
    auxiliary[uri] = prefix
    return prefix


def _rewrite_value(value: str, descriptor: tuple,
                   scope: dict[str, str],
                   uri_to_prefix: dict[str, str],
                   attr_context: bool,
                   xsi_type: bool = False,
                   auxiliary: Optional[dict[str, str]] = None) -> str:
    kind, members = descriptor[0], descriptor[1]

    def rewrite_token(token: str) -> Optional[str]:
        extended = _resolve_token(token, scope, attr_context)
        if extended is None:
            return None
        return _render_token(extended, uri_to_prefix, attr_context,
                             auxiliary)

    if kind == 'atomic':
        if xsi_type:
            extended = _resolve_token(value.strip(), scope, True)
            if extended is None:
                return value
            return _render_token(extended, uri_to_prefix, True, auxiliary)
        token = value.strip()
        rewritten = rewrite_token(token)
        if rewritten is None:
            return value
        return value.replace(token, rewritten, 1)

    if kind == 'union':
        token = value.strip()
        if not _is_qname_token(token, scope, attr_context):
            return value
        extended = _resolve_token(token, scope, attr_context)
        if extended is None:
            return value
        rewritten = _render_token(extended, uri_to_prefix, attr_context,
                                  auxiliary)
        return value.replace(token, rewritten, 1)

    # list: preserve the original separators
    def replace(match: 're.Match[str]') -> str:
        token = match.group(0)
        if not _is_qname_token(token, scope, attr_context):
            return token
        extended = _resolve_token(token, scope, attr_context)
        if extended is None:
            return token
        return _render_token(extended, uri_to_prefix, attr_context,
                             auxiliary)

    return re.sub(r'[^\s]+', replace, value)


def _is_qname_token(token: str, scope: dict[str, str],
                    attr_context: bool) -> bool:
    """
    Whether *token* is a resolvable lexical QName in the given scope. The
    descriptor already guarantees the position is QName/NOTATION based, so
    lexical well-formedness and prefix resolution are sufficient.
    """
    if _resolve_token(token, scope, attr_context) is not None:
        return True
    # An unprefixed token only resolves through the default namespace in
    # element content.
    return not attr_context and _resolve_token(token, scope, False) is not None


def _qname_members_match(value: str, _members: list['XsdSimpleType'],
                         scope: dict[str, str], attr_context: bool) -> bool:
    """Whether a single-token value is lexically carried by a QName member."""
    token = value.strip()
    return _is_qname_token(token, scope, attr_context)


# Serialization --------------------------------------------------------------

_ESCAPE_TEXT = {'&': '&amp;', '<': '&lt;', '>': '&gt;'}
_ESCAPE_ATTR = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
                '\t': '&#9;', '\n': '&#10;', '\r': '&#13;'}


def _escape(text: str, table: dict[str, str]) -> str:
    return ''.join(table.get(ch, ch) for ch in text)


def _render_tag_name(name: str,
                     uri_to_prefix: dict[str, str],
                     auxiliary: Optional[dict[str, str]] = None,
                     attribute: bool = False) -> str:
    if name[:1] != '{':
        return name
    uri, local = name[1:].split('}', 1)
    prefix = uri_to_prefix.get(uri)
    if attribute and not prefix and auxiliary:
        prefix = auxiliary.get(uri) or prefix
    return f'{prefix}:{local}' if prefix else local


def _serialize(root: ElementTree.Element,
               scopes: dict[int, dict[str, str]],
               analysis: dict[str, Any],
               uri_to_prefix: dict[str, str]) -> str:
    qname_attrs = analysis['qname_attrs']
    qname_text = analysis['qname_text']
    local_undeclare = analysis['local_undeclare_default']

    auxiliary: dict[str, str] = {}

    # A default-namespace URI bound to the empty prefix still needs a
    # non-empty alias for the names of its (namespace-qualified) attributes
    # and for QName lexical values that resolve into it.
    default_uris = {uri for uri, prefix in uri_to_prefix.items()
                    if uri and not prefix}
    if default_uris:
        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            for attr_name in elem.attrib:
                if isinstance(attr_name, str) and attr_name[:1] == '{':
                    uri = attr_name[1:].split('}', 1)[0]
                    if uri in default_uris and uri not in auxiliary:
                        _auxiliary_prefix(uri, uri_to_prefix, auxiliary)

    # Precompute rewritten QName values; this can add an auxiliary prefix
    # when a default namespace URI is referenced inside a QName value.
    rewritten_attrs: dict[int, dict[str, str]] = {}
    rewritten_text: dict[int, str] = {}
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        scope = scopes[id(elem)]
        attr_values: dict[str, str] = {}
        for attr_name, attr_value in elem.attrib.items():
            if not isinstance(attr_name, str):
                continue
            if attr_name == XSI_TYPE:
                attr_values[attr_name] = _rewrite_value(
                    attr_value, ('atomic', []), scope, uri_to_prefix,
                    True, xsi_type=True, auxiliary=auxiliary)
            elif id(elem) in qname_attrs \
                    and attr_name in qname_attrs[id(elem)]:
                attr_values[attr_name] = _rewrite_value(
                    attr_value, qname_attrs[id(elem)][attr_name], scope,
                    uri_to_prefix, True, auxiliary=auxiliary)
        if attr_values:
            rewritten_attrs[id(elem)] = attr_values
        if id(elem) in qname_text and elem.text:
            rewritten_text[id(elem)] = _rewrite_value(
                elem.text, qname_text[id(elem)], scope, uri_to_prefix,
                False, auxiliary=auxiliary)

    needed: dict[str, str] = {}
    for uri, prefix in uri_to_prefix.items():
        if uri == XML_NAMESPACE:
            continue  # the xml prefix is implicitly always in scope
        if uri and prefix:
            needed[prefix] = uri
        elif uri:
            needed[''] = uri  # default namespace binding
    for uri, prefix in auxiliary.items():
        needed.setdefault(prefix, uri)

    parts: list[str] = []

    def serialize(elem: ElementTree.Element) -> None:
        tag = elem.tag
        if not isinstance(tag, str):
            return  # comments/PIs are not part of the data model output

        parts.append('<')
        parts.append(_render_tag_name(tag, uri_to_prefix))

        if id(elem) in local_undeclare and '' in needed \
                and elem is not root:
            parts.append(' xmlns=""')

        # Root gets every binding needed in the document, so local prefix
        # redefinitions of the source become a single global canonical map.
        if elem is root and needed:
            if '' in needed:
                parts.append(f' xmlns="{_escape(needed[""], _ESCAPE_ATTR)}"')
            for prefix in sorted(p for p in needed if p):
                uri = needed[prefix]
                parts.append(
                    f' xmlns:{prefix}="{_escape(uri, _ESCAPE_ATTR)}"')

        for attr_name, attr_value in elem.attrib.items():
            if not isinstance(attr_name, str):
                continue
            if attr_name[:1] == '{':
                rendered_name = _render_tag_name(
                    attr_name, uri_to_prefix, auxiliary, attribute=True)
            else:
                rendered_name = attr_name
            value = rewritten_attrs.get(id(elem), {}).get(attr_name,
                                                          attr_value)
            parts.append(f' {rendered_name}="'
                         f'{_escape(value, _ESCAPE_ATTR)}"')

        text = elem.text
        if id(elem) in rewritten_text and text is not None:
            text = rewritten_text[id(elem)]

        if not len(elem) and not text:
            parts.append(' />')
            return

        parts.append('>')
        if text:
            parts.append(_escape(text, _ESCAPE_TEXT))
        for child in elem:
            serialize(child)
            if child.tail:
                parts.append(_escape(child.tail, _ESCAPE_TEXT))
        parts.append('</')
        parts.append(_render_tag_name(tag, uri_to_prefix))
        parts.append('>')

    serialize(root)
    return ''.join(parts)
