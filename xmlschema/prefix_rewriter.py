#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Schema-aware rewriting of XML namespace prefixes.

Unlike a superficial renaming of element/attribute tags, this module rewrites
also the prefixes used inside *xs:QName* typed values (e.g. ``xsi:type``,
element text, attribute values, and list/union members), while every value
that is not proved to be a QName by the schema is left untouched (a plain
string containing a colon is not a QName unless its type says so).

The rewriter works on an ordered Element tree: element order, mixed text,
comments, processing instructions and the expanded names (namespace URI +
local name) of every node are preserved. A new document is produced, the
source document is never modified.
"""
import re
from collections.abc import Mapping
from copy import deepcopy
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

import xmlschema.names as nm
from xmlschema.aliases import ElementType, NsmapType
from xmlschema.exceptions import XMLSchemaTypeError, XMLSchemaValueError, \
    XMLResourceError
from xmlschema.namespaces import NamespaceMapper
from xmlschema.resources import XMLResource
from xmlschema.utils.etree import is_etree_document, is_etree_element
from xmlschema.utils.qnames import get_namespace, local_name
from xmlschema.validators import XMLSchemaBase, XMLSchemaValidationError, \
    XsdAnyAttribute, XsdAnyElement, XsdAttribute, XsdAttributeGroup, XsdComplexType, \
    XsdElement, XsdSimpleType
from xmlschema.validators.models import ModelVisitor
from xmlschema.validators.validation import ValidationContext

__all__ = ('NamespacePrefixRewriter', 'PrefixRewriteError',
           'rewrite_namespace_prefixes', 'rewrite_prefixes')

#: Policy for content whose type cannot be proved from the schema
#: (``xs:any``/``xs:anyAttribute`` with *processContents='skip'*, unbound
#: elements/attributes). ``'reject'`` raises :class:`PrefixRewriteError`,
#: ``'preserve'`` copies the subtree/value verbatim and does not rewrite any
#: prefix used inside it.
UNKNOWN_POLICIES = ('reject', 'preserve')

# XSI attributes that are plain values (not QNames) and are always admitted
# on an element when the schema allows instance attributes.
XSI_PLAIN_ATTRIBUTES = frozenset({
    nm.XSI_NIL, nm.XSI_SCHEMA_LOCATION, nm.XSI_NONS_SCHEMA_LOCATION,
})

class PrefixRewriteError(XMLSchemaValueError):
    """Raised when a namespace prefix rewriting cannot be completed safely."""


class _QNameRewriter:
    """
    Rewrites the prefix of a QName lexical value using a target namespace map.

    The *old_nsmap* is the in-scope namespace map of the source element where
    the value appears; the *new_nsmap* is the target in-scope namespace map
    that must contain a binding for the namespace of the QName.
    """

    __slots__ = ('old_nsmap', 'new_nsmap')

    def __init__(self, old_nsmap: NsmapType, new_nsmap: NsmapType) -> None:
        self.old_nsmap = old_nsmap
        self.new_nsmap = new_nsmap

    def resolve(self, value: str) -> str:
        """
        Resolves a QName lexical value to its expanded form using the source
        in-scope namespaces. Raises `PrefixRewriteError` if the prefix cannot
        be resolved (the value is not a valid QName in this context).
        """
        value = value.strip()
        if not value:
            raise PrefixRewriteError("empty value is not a valid xs:QName")

        if ':' in value:
            prefix, name = value.split(':', 1)
            if not prefix or not name or ':' in name:
                raise PrefixRewriteError(f"{value!r} is not a valid xs:QName")
            try:
                uri = self.old_nsmap[prefix]
            except KeyError:
                raise PrefixRewriteError(
                    f"prefix {prefix!r} of QName {value!r} is not bound "
                    f"in the source document"
                ) from None
        else:
            name = value
            uri = self.old_nsmap.get('', '')

        if not uri:
            return name  # QName with no namespace
        return f'{{{uri}}}{name}'

    def rewrite(self, value: str) -> str:
        """
        Rewrites a QName lexical value using the target namespace map.
        The expanded name of the QName is preserved.
        """
        expanded = self.resolve(value)
        if expanded[0] != '{':
            return expanded  # no namespace: keep the local name

        uri = get_namespace(expanded)
        name = local_name(expanded)

        if self.new_nsmap.get('') == uri:
            return name
        for prefix, ns_uri in self.new_nsmap.items():
            if prefix and ns_uri == uri:
                return f'{prefix}:{name}'

        raise PrefixRewriteError(
            f"no binding for namespace {uri!r} in the rewritten document, "
            f"cannot rewrite QName {value!r}"
        )


class NamespacePrefixRewriter:
    """
    Rewrites the namespace prefixes of an XML document using its XSD schema
    to locate every place where a prefix is semantically a QName.

    Only element/attribute tag prefixes are not enough: values typed as
    ``xs:QName`` (including ``xsi:type``, attributes, element text, list items
    and the QName members of unions) carry prefixes too and must be rewritten
    consistently. The declared XSD type is the only source of truth used to
    decide which values are QNames; ordinary strings that happen to contain a
    colon are preserved verbatim.

    :param schema: the :class:`~xmlschema.XMLSchema` (or a compatible schema \
    instance) describing the document.
    :param prefix_map: a mapping from namespace URIs to the prefix to use in \
    the output. Namespaces not listed keep a prefix chosen by the rewriter. \
    Explicit prefixes are required, because a default namespace would make \
    unprefixed QName values ambiguous.
    :param unknown_policy: how to treat content whose type cannot be proved \
    by the schema (``xs:any``/``xs:anyAttribute`` with \
    *processContents='skip'* or unbound components): ``'reject'`` (default) \
    raises :class:`PrefixRewriteError`, ``'preserve'`` keeps the source \
    subtree/value without rewriting any prefix used inside it.
    :raises: :class:`~xmlschema.exceptions.XMLSchemaTypeError` or \
    :class:`~xmlschema.exceptions.XMLSchemaValueError` for invalid arguments.
    """

    def __init__(self, schema: XMLSchemaBase,
                 prefix_map: Mapping[str, str] | None = None,
                 unknown_policy: str = 'reject') -> None:
        if not isinstance(schema, XMLSchemaBase):
            raise XMLSchemaTypeError(
                f"{schema!r} is not an XMLSchemaBase instance"
            )
        if unknown_policy not in UNKNOWN_POLICIES:
            raise XMLSchemaValueError(
                f"{unknown_policy!r} is not a valid unknown policy, "
                f"must be one of {UNKNOWN_POLICIES}"
            )
        self.schema = schema
        self.unknown_policy = unknown_policy

        # User-provided mapping URI -> output prefix, kept apart from the
        # mapping that the rewriter fills with auto-allocated prefixes.
        self._user_prefix_map: dict[str, str] = {}
        if prefix_map:
            for uri, prefix in prefix_map.items():
                if not isinstance(uri, str) or not isinstance(prefix, str):
                    raise XMLSchemaTypeError(
                        "prefix_map must be a mapping of str to str"
                    )
                if not prefix:
                    raise XMLSchemaValueError(
                        "the empty prefix cannot be used for the rewritten "
                        "document: QName values would become ambiguous, "
                        "please provide an explicit prefix"
                    )
                elif not _is_ncname(prefix):
                    raise XMLSchemaValueError(
                        f"{prefix!r} is not a valid XML namespace prefix"
                    )
                elif prefix == 'xml':
                    if uri != nm.XML_NAMESPACE:
                        raise XMLSchemaValueError(
                            f"the 'xml' prefix can be used only for "
                            f"{nm.XML_NAMESPACE!r}"
                        )
                elif prefix == 'xmlns':
                    raise XMLSchemaValueError(
                        "the 'xmlns' prefix is reserved and cannot be used"
                    )

            inverted: dict[str, str] = {}
            for uri, prefix in prefix_map.items():
                if prefix in inverted and inverted[prefix] != uri:
                    raise XMLSchemaValueError(
                        f"prefix {prefix!r} is assigned to more than one "
                        f"namespace ({inverted[prefix]!r} and {uri!r})"
                    )
                inverted[prefix] = uri

            self._user_prefix_map.update(prefix_map)

        # Mapping URI -> chosen output prefix; built deterministically.
        self._uri_to_prefix: dict[str, str] = dict(self._user_prefix_map)

        # Per-element effective XSD types found during the validation pass.
        self._element_types: dict[int, XsdElement] = {}
        # URI -> prefix actually needed, discovered while walking.
        self._used_uri_set: set[str] = set()
        # URI -> first source prefix, used only as a naming hint.
        self._source_prefix_hints: dict[str, str] = {}
        # Prefixes emitted for schema-proved QName values (verified against
        # the output declarations).
        self._qname_prefixes: set[str] = set()
        # Cache of validation contexts keyed by the in-scope nsmap identity.
        self._value_contexts: dict[int, ValidationContext] = {}
        self._current_resource: XMLResource | None = None

    # ---- Public API -------------------------------------------------------

    def rewrite(self, source: object, *, verify: bool = True) -> ElementType:
        """
        Returns a new Element tree whose namespace prefixes have been
        rewritten according to the schema and the prefix map.

        The *source* can be an :class:`~xmlschema.XMLResource`, an ElementTree
        element/document or any XML source accepted by
        :class:`~xmlschema.XMLResource`. Lazy resources are not supported.
        The source document is never modified.

        :param verify: when `True` (default) both documents are decoded with         the schema and compared semantically, and the output namespace         declarations are checked for QName resolution.
        :raises PrefixRewriteError: if the source contains schema-unproven \
        content in ``'reject'`` mode or a QName cannot be resolved.
        """
        resource = self._get_resource(source)
        self._pass_validation(resource)
        new_root, new_nsmap = self._build(resource)
        if verify:
            _verify_semantic_equivalence(self, resource, new_root, new_nsmap)
            self._check_qname_resolution(new_root, new_nsmap)
        return new_root

    def serialize(self, source: object, *, xml_declaration: bool = False,
                  encoding: str = 'unicode') -> object:
        """
        Rewrites *source* and serializes the new document to a string (or to
        bytes if a non-unicode *encoding* is given). Namespace declarations
        emitted in the output are verified to cover every QName value.
        """
        resource = self._get_resource(source)
        self._pass_validation(resource)
        new_root, new_nsmap = self._build(resource)
        _verify_semantic_equivalence(self, resource, new_root, new_nsmap)
        self._check_qname_resolution(new_root, new_nsmap)
        return _serialize(new_root, new_nsmap, xml_declaration, encoding)

    # ---- Source handling --------------------------------------------------

    def _get_resource(self, source: object) -> XMLResource:
        if isinstance(source, XMLResource):
            resource = source
        elif is_etree_element(source) or is_etree_document(source) or \
                isinstance(source, (str, bytes)) or hasattr(source, 'read'):
            resource = XMLResource(source)
        else:
            raise XMLSchemaTypeError(
                f"unexpected XML source type {type(source)!r}"
            )

        if resource.is_lazy():
            raise XMLResourceError(
                "lazy XML resources cannot be prefix-rewritten; "
                "load the resource first"
            )

        if self._has_nsmap_tracking(resource):
            return resource

        # An Element/ElementTree built outside XMLResource (or with a parser
        # that does not retain namespace maps) carries no in-scope namespace
        # maps, so QName lexical values could not be resolved. Re-parse a
        # serialized copy; the source tree itself is never modified.
        return XMLResource(resource.tostring(xml_declaration=True))

    @staticmethod
    def _has_nsmap_tracking(resource: XMLResource) -> bool:
        root = resource.root
        if resource.get_nsmap(root) is not None:
            return True
        return hasattr(root, 'nsmap') and root.nsmap is not None

    # ---- Validation pass: associate each element with its XSD element ----

    def _pass_validation(self, resource: XMLResource) -> None:
        """
        Runs a lax validation to reach every element that the schema can
        prove, recording the effective `XsdElement` declaration of each one
        (dummy declarations created for wildcards are detected separately).
        """
        self._current_resource = resource
        self._element_types.clear()

        for _error in self.schema.iter_errors(
                resource, validation='lax', validation_hook=self._hook):
            pass  # semantic validity is checked later; only locations matter


    def _hook(self, elem: ElementType,
              xsd_element: XsdElement) -> None:
        self._element_types[id(elem)] = xsd_element
        return None

    @staticmethod
    def _is_dummy_element(xsd_element: XsdElement) -> bool:
        """
        Returns `True` if the component is a synthesized element rather than
        a real schema declaration. Dummies are created by the validators for
        wildcard content (parent is an `XsdAnyElement`) and for roots that
        carry an *xsi:type* without a global declaration (parent is the
        schema and the component carries a generated ``form='unqualified'``
        marker that never appears in parsed schema sources).
        """
        if isinstance(xsd_element.parent, XsdAnyElement):
            return True
        return xsd_element.ref is None and xsd_element.parent is None \
            and xsd_element.elem.get('form') == 'unqualified'

    # ---- Prefix allocation ------------------------------------------------

    def _ensure_prefix(self, uri: str) -> str:
        """
        Allocates (or returns) the stable output prefix for a namespace URI.
        Allocation order follows the first use of the URI during the walk, so
        repeated runs on the same document always produce the same prefixes.
        """
        if not uri:
            return ''
        if uri == nm.XML_NAMESPACE:
            # 'xml' is reserved and always in scope; keep it in the map so
            # the serializer maps {XML}* names but never emits a declaration.
            self._uri_to_prefix[uri] = 'xml'
            return 'xml'
        if uri == nm.XMLNS_NAMESPACE:  # pragma: no cover
            raise PrefixRewriteError(
                "the xmlns namespace cannot be used for element/attribute names"
            )

        if uri in self._uri_to_prefix:
            return self._uri_to_prefix[uri]

        self._used_uri_set.add(uri)

        # The prefix may have been claimed by another URI earlier; resolve
        # conflicts deterministically with a numeric suffix.
        chosen = self._default_prefix_for(uri)
        taken = set(self._uri_to_prefix.values())
        if chosen in taken:
            base = chosen
            index = 1
            while f'{base}{index}' in taken:
                index += 1
            chosen = f'{base}{index}'
        self._uri_to_prefix[uri] = chosen
        return chosen

    def _default_prefix_for(self, uri: str) -> str:
        """
        Selects a non-empty default prefix for an unlisted URI, derived from
        the first source prefix bound to that URI or from a stable token.
        """
        candidate = self._source_prefix_hints.get(uri)
        if candidate and _is_ncname(candidate) and candidate not in ('xml', 'xmlns'):
            return candidate
        token = local_name(uri.rstrip('/#'))
        if token and _is_ncname(token):
            return token
        return 'ns'

    def _collect_source_prefix_hints(self, resource: XMLResource) -> None:
        """Records the first source prefix seen for every namespace URI."""
        self._source_prefix_hints.clear()
        for elem in resource.iter():
            if callable(elem.tag):
                continue
            nsmap = resource.get_nsmap(elem) or {}
            for prefix, uri in nsmap.items():
                if uri and uri not in self._source_prefix_hints and prefix:
                    self._source_prefix_hints[uri] = prefix

    def _reset_prefixes(self) -> None:
        """Resets prefix allocation to the initial user-provided mapping."""
        self._uri_to_prefix.clear()
        self._uri_to_prefix.update(self._user_prefix_map)
        self._used_uri_set.clear()

    # ---- Tree building ----------------------------------------------------

    def _build(self, resource: XMLResource) -> tuple[ElementType, NsmapType]:
        """
        Builds the rewritten tree in two sub-passes. The first pass allocates
        a stable prefix to every namespace URI actually needed (element and
        attribute names plus every schema-proved QName value); the second
        pass emits the new tree using those prefixes.
        """
        self._current_resource = resource
        self._value_contexts.clear()
        self._qname_prefixes.clear()
        self._reset_prefixes()
        self._collect_source_prefix_hints(resource)

        # Pass 1: discover all namespace URIs so that root declarations alone
        # suffice to resolve every QName, and fail early on unproven content.
        self._walk(resource, allocate_only=True)

        # Pass 2: create the new tree keeping order, text and tails.
        new_root = self._walk(resource, allocate_only=False)
        assert new_root is not None
        new_nsmap = dict(self._uri_to_prefix)
        self._declare_namespaces(new_root, new_nsmap)
        return new_root, new_nsmap

    def _walk(self, resource: XMLResource,
              allocate_only: bool) -> ElementType | None:
        root = resource.root
        root_xsd = self._element_types.get(id(root))

        if root_xsd is None or self._is_dummy_element(root_xsd):
            if self.unknown_policy == 'reject':
                raise PrefixRewriteError(
                    f"the root element {root.tag!r} has no provable type in "
                    f"the schema; cannot rewrite its QName prefixes safely "
                    f"(use unknown_policy='preserve' to keep it verbatim)"
                )
            if allocate_only:
                self._register_subtree_namespaces(root)
                return None
            return self._copy_unproven_subtree(resource, root)

        new_root = self._rewrite_element(resource, root, root_xsd,
                                         ancestor_nsmaps=[],
                                         allocate_only=allocate_only)
        return new_root

    def _register_tag_ns(self, resource: XMLResource, elem: ElementType) -> None:
        uri = get_namespace(elem.tag)
        if uri:
            self._ensure_prefix(uri)
        for name in elem.attrib:
            attr_uri = get_namespace(name)
            if attr_uri and attr_uri != nm.XMLNS_NAMESPACE:
                self._ensure_prefix(attr_uri)

    def _effective_type(self, elem: ElementType,
                        xsd_element: XsdElement,
                        nsmap: NsmapType):
        """
        Returns the effective XSD type of an instance element, taking
        *xsi:type* and XSD 1.1 type alternatives into account.
        """
        xsd_type = xsd_element.type
        if xsd_element.alternatives:
            xsd_type = xsd_element.get_alternative_type(elem)

        xsi_type = elem.attrib.get(nm.XSI_TYPE)
        if xsi_type is not None:
            xsi_type = xsi_type.strip()
            try:
                xsd_type = self.schema.maps.get_instance_type(
                    xsi_type, xsd_type, nsmap
                )
            except (KeyError, TypeError) as err:
                raise PrefixRewriteError(
                    f"cannot resolve xsi:type {xsi_type!r} of {elem.tag!r}: {err}"
                ) from None
        return xsd_type

    def _rewrite_element(self, resource: XMLResource, elem: ElementType,
                         xsd_element: XsdElement,
                         ancestor_nsmaps: list[NsmapType],
                         allocate_only: bool) -> ElementType | None:
        old_nsmap = self._element_nsmap(resource, elem, ancestor_nsmaps)
        new_nsmap = self._target_nsmap(old_nsmap)
        xsd_type = self._effective_type(elem, xsd_element, old_nsmap)

        if allocate_only:
            self._register_tag_ns(resource, elem)
            self._rewrite_element_value(resource, elem, xsd_type, old_nsmap,
                                        new_nsmap, allocate_only=True)
            self._rewrite_attributes(resource, elem, xsd_element, xsd_type,
                                     old_nsmap, new_nsmap, allocate_only=True)
            new_elem = None
        else:
            new_elem = ElementType(elem.tag)
            new_elem.text = self._rewrite_element_value(
                resource, elem, xsd_type, old_nsmap, new_nsmap,
                allocate_only=False)
            new_elem.tail = elem.tail
            self._rewrite_attributes(resource, elem, xsd_element, xsd_type,
                                     old_nsmap, new_nsmap, allocate_only=False,
                                     target=new_elem)

        model_group = xsd_type.model_group if isinstance(
            xsd_type, XsdComplexType) else None
        visitor = model_group.get_model_visitor() \
            if model_group is not None else None

        for child in elem:
            if callable(child.tag):
                if new_elem is not None:
                    new_elem.append(self._copy_document_node(child))
                continue

            xsd_child = self._resolve_child_declaration(visitor, child)
            if xsd_child is None:
                # Content not covered by the declared type (wildcard/unknown)
                if self.unknown_policy == 'reject':
                    raise PrefixRewriteError(
                        f"cannot prove the type of child element {child.tag!r} "
                        f"of {elem.tag!r} from the schema; refusing to guess "
                        f"(use unknown_policy='preserve' to keep it verbatim)"
                    )
                if new_elem is None:
                    self._register_subtree_namespaces(child)
                else:
                    new_elem.append(self._copy_unproven_subtree(resource, child))
                continue

            new_child = self._rewrite_element(
                resource, child, xsd_child, ancestor_nsmaps + [old_nsmap],
                allocate_only=allocate_only
            )
            if new_elem is not None and new_child is not None:
                new_elem.append(new_child)

        return new_elem

    def _resolve_child_declaration(self, visitor: ModelVisitor | None,
                                   child: ElementType) -> XsdElement | None:
        """
        Returns the real `XsdElement` declaration for a child, preferring the
        declaration collected during validation (which already accounts for
        *xsi:type* and substitution groups) and falling back to the content
        model for subtrees skipped by wildcards.
        """
        hook_child = self._element_types.get(id(child))
        # Always advance the content model, even when the validated
        # declaration is preferred, or later wildcard matches would use a
        # stale model state.
        matched = self._match_child(visitor, child)

        if hook_child is not None and not self._is_dummy_element(hook_child):
            return hook_child

        xsd_child = matched
        if xsd_child is None:
            return None
        elif isinstance(xsd_child, XsdAnyElement):
            if xsd_child.process_contents == 'skip':
                return None
            return self.schema.maps.elements.get(child.tag)
        elif isinstance(xsd_child, XsdElement):
            return xsd_child
        else:  # pragma: no cover
            return None

    def _match_child(self, visitor: ModelVisitor | None,
                     child: ElementType) -> XsdElement | None:
        """
        Matches a child element against the declared content model, advancing
        the model exactly like the validator does (so sequences, choices and
        occurrence bounds are honoured). Returns `None` when the schema does
        not declare the child at this position (wildcard/unknown content).
        """
        if visitor is None or visitor.element is None:
            return None

        while visitor.element is not None:
            xsd_child = visitor.match_element(child.tag)
            if xsd_child is not None:
                for _particle, _occurs, _expected in visitor.advance(True):
                    pass
                return xsd_child

            # Advance through particles that cannot match this child.
            for _particle, _occurs, _expected in visitor.advance(False):
                visitor.clear()
                break
        return None

    def _declare_namespaces(self, new_root: ElementType,
                            nsmap: NsmapType) -> None:
        """
        Adds explicit xmlns declarations to the new root. Prefixes are unique
        per URI, so root-level declarations cover the whole document, as
        required for every QName value to stay resolvable when serialized.
        """
        for uri, prefix in nsmap.items():
            if prefix in ('xml', 'xmlns'):
                continue  # the 'xml' prefix is implicitly declared
            if prefix:
                new_root.set(f'xmlns:{prefix}', uri)
            else:
                new_root.set('xmlns', uri)

    # ---- Namespace maps ---------------------------------------------------

    def _element_nsmap(self, resource: XMLResource, elem: ElementType,
                       ancestor_nsmaps: list[NsmapType]) -> NsmapType:
        """
        Returns the in-scope namespace map of a source element, so that
        prefixed QNames in its text/attributes resolve against the right
        local bindings (prefix shadowing is respected).
        """
        nsmap = resource.get_nsmap(elem)
        if nsmap is not None:
            return nsmap
        if hasattr(elem, 'nsmap'):
            return {prefix or '': uri for prefix, uri in elem.nsmap.items()}
        if ancestor_nsmaps:
            return ancestor_nsmaps[-1]
        return {}

    def _target_nsmap(self, old_nsmap: NsmapType) -> NsmapType:
        """
        Builds the target in-scope namespace map corresponding to a source
        in-scope map: each in-scope URI is bound to its allocated prefix.
        """
        new_nsmap: dict[str, str] = {}
        for prefix, uri in old_nsmap.items():
            if not uri:
                continue
            new_nsmap[self._ensure_prefix(uri)] = uri
        return new_nsmap

    # ---- Schema-typed value rewriting ------------------------------------

    def _rewrite_element_value(self, resource: XMLResource, elem: ElementType,
                               xsd_type: XsdSimpleType | XsdComplexType,
                               old_nsmap: NsmapType, new_nsmap: NsmapType,
                               allocate_only: bool):
        """
        Rewrites the text of an element according to its declared simple
        content type. Complex (element-only/mixed/empty) types carry no
        QName text at this level.
        """
        text = elem.text
        if text is None or isinstance(xsd_type, XsdComplexType) and \
                not isinstance(xsd_type.content, XsdSimpleType):
            return text
        if not isinstance(xsd_type, XsdSimpleType) and not (
                isinstance(xsd_type, XsdComplexType)
                and isinstance(xsd_type.content, XsdSimpleType)):
            return text

        simple_type = xsd_type if isinstance(xsd_type, XsdSimpleType) \
            else xsd_type.content
        return self._rewrite_typed_value(text, simple_type, old_nsmap,
                                         new_nsmap, allocate_only)

    def _rewrite_typed_value(self, text: str, xsd_type: XsdSimpleType,
                             old_nsmap: NsmapType, new_nsmap: NsmapType,
                             allocate_only: bool) -> str:
        """
        Rewrites a simple-typed lexical value. Only values whose declared
        type contains an xs:QName member are touched; list items and union
        members are processed individually, plain string types are returned
        unchanged even if they contain colons.
        """
        if not text or not self._type_contains_qname(xsd_type):
            return text
        self._check_qname_type(text, xsd_type, old_nsmap)
        if allocate_only:
            self._register_typed_value(text, xsd_type, old_nsmap)
            return text
        return self._rewrite_typed(text, xsd_type, old_nsmap, new_nsmap)

    def _type_contains_qname(self, xsd_type: XsdSimpleType) -> bool:
        """
        Returns `True` if a lexical value of this type can be a QName, i.e.
        the type is xs:QName/xs:NOTATION derived or is a list/union with such
        an atomic member.
        """
        if xsd_type.is_qname() or xsd_type.is_notation():
            return True
        if xsd_type.is_list():
            return self._type_contains_qname(xsd_type.item_type)
        if xsd_type.is_union():
            return any(self._type_contains_qname(mt)
                       for mt in xsd_type.member_types)
        return False

    def _is_qname_value(self, value: str, xsd_type: XsdSimpleType,
                        nsmap: NsmapType) -> bool:
        """
        Proves that *value* is actually a QName under *xsd_type* by decoding
        it strictly with the element's in-scope namespaces. A union value
        that the schema validates as xs:string (e.g. "literal:x") returns
        `False` and is therefore left untouched.
        """
        return self._decodes_as_qname(value.strip(), xsd_type, nsmap)

    def _decodes_as_qname(self, value: str, xsd_type: XsdSimpleType,
                          nsmap: NsmapType) -> bool:
        context = self._get_value_context(nsmap)
        context.errors.clear()
        try:
            decoded = xsd_type.raw_decode(value, 'strict', context)
        except XMLSchemaValidationError:
            return False

        return isinstance(decoded, str) and decoded[:1] == '{'

    def _get_value_context(self, nsmap: NsmapType) -> ValidationContext:
        key = frozenset(nsmap.items())
        context = self._value_contexts.get(hash(key))
        if context is None:
            source = self._current_resource or self.schema.source
            context = ValidationContext(
                source=source,
                converter=NamespaceMapper(dict(nsmap)),
                check_identities=False,
                use_defaults=False,
            )
            self._value_contexts[hash(key)] = context
        return context

    def _register_value_ns(self, value: str, old_nsmap: NsmapType) -> None:
        expanded = self._resolve_typed_qname(value, old_nsmap)
        uri = get_namespace(expanded)
        if uri:
            self._ensure_prefix(uri)

    def _rewrite_if_qname(self, value: str, xsd_type: XsdSimpleType,
                          old_nsmap: NsmapType, new_nsmap: NsmapType) -> str:
        if not self._is_qname_value(value, xsd_type, old_nsmap):
            return value

        core = value.strip()
        leading = value[:len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()):]

        expanded = self._resolve_typed_qname(core, old_nsmap)
        result = _emit_qname(expanded, new_nsmap)
        if ':' in result:
            self._qname_prefixes.add(result.split(':', 1)[0])
        return leading + result + trailing

    def _check_qname_type(self, value: str, xsd_type: XsdSimpleType,
                          nsmap: NsmapType) -> None:
        """
        Fails on a value that *must* be a QName by its declared type but
        cannot be parsed as one with the in-scope namespaces (e.g. an
        unbound prefix). For union types such values are allowed to fall
        back to a non-QName member, so no error is raised here.
        """
        if xsd_type.is_union():
            return
        if not (xsd_type.is_qname() or xsd_type.is_notation()) and \
                not (xsd_type.is_list() and
                     (xsd_type.item_type.is_qname()
                      or xsd_type.item_type.is_notation())):
            return
        candidate = value.strip()
        if not candidate:
            return
        if xsd_type.is_list():
            items = candidate.split()
        else:
            items = [candidate]
        for item in items:
            if ':' in item:
                prefix = item.split(':', 1)[0]
                if prefix not in nsmap:
                    raise PrefixRewriteError(
                        f"prefix {prefix!r} of QName {item!r} is not "
                        f"declared in the source document"
                    )

    def _resolve_typed_qname(self, value: str,
                             old_nsmap: NsmapType) -> str:
        """
        Resolves a value already proved to be a QName into its expanded form.
        An unbound prefix means the source document itself cannot resolve the
        QName, which is a hard error rather than something to guess about.
        """
        candidate = value.strip()
        if ':' in candidate:
            prefix, name = candidate.split(':', 1)
            if prefix not in old_nsmap:
                raise PrefixRewriteError(
                    f"prefix {prefix!r} of QName {value!r} is not declared "
                    f"in the source document"
                )
        return _QNameRewriter(old_nsmap, {}).resolve(candidate)

    def _register_typed_value(self, value: str, xsd_type: XsdSimpleType,
                              old_nsmap: NsmapType) -> None:
        """Registers namespaces of all QName members in a typed value."""
        if not value or not self._type_contains_qname(xsd_type):
            return
        if xsd_type.is_list():
            if self._type_contains_qname(xsd_type.item_type):
                for chunk in value.split():
                    if self._is_qname_value(chunk, xsd_type.item_type, old_nsmap):
                        self._register_value_ns(chunk, old_nsmap)
        elif self._is_qname_value(value, xsd_type, old_nsmap):
            self._register_value_ns(value, old_nsmap)

    def _rewrite_typed(self, value: str, xsd_type: XsdSimpleType,
                       old_nsmap: NsmapType, new_nsmap: NsmapType) -> str:
        """Rewrites all QName members of a typed value, item by item."""
        if not value or not self._type_contains_qname(xsd_type):
            return value
        if xsd_type.is_list():
            if not self._type_contains_qname(xsd_type.item_type):
                return value
            # Keep the original whitespace separators, only rewrite items.
            parts = re.split(r'(\s+)', value)
            return ''.join(
                part if not part or part.isspace() else
                self._rewrite_if_qname(part, xsd_type.item_type,
                                       old_nsmap, new_nsmap)
                for part in parts
            )
        return self._rewrite_if_qname(value, xsd_type, old_nsmap, new_nsmap)

    # ---- Attributes -------------------------------------------------------

    def _rewrite_attributes(self, resource: XMLResource, elem: ElementType,
                            xsd_element: XsdElement, xsd_type,
                            old_nsmap: NsmapType, new_nsmap: NsmapType,
                            allocate_only: bool,
                            target: ElementType | None = None) -> None:
        attribute_group = xsd_element.get_attributes(xsd_type)

        for expanded_name, value in elem.attrib.items():
            if expanded_name.startswith('xmlns') and (
                    expanded_name == 'xmlns' or expanded_name.startswith('xmlns:')):
                continue  # namespace declarations are regenerated at output

            if expanded_name == nm.XSI_TYPE:
                # xsi:type is always an xs:QName value (in XSI namespace).
                if allocate_only:
                    self._register_value_ns(value, old_nsmap)
                else:
                    assert target is not None
                    target.set(expanded_name,
                               self._rewrite_xsi_type(value, old_nsmap, new_nsmap))
                continue

            if expanded_name in XSI_PLAIN_ATTRIBUTES:
                # xsi:nil/schemaLocation/noNamespaceSchemaLocation are plain
                # XSD instance attributes, admitted without a declaration.
                if not allocate_only:
                    assert target is not None
                    target.set(expanded_name, value)
                continue

            xsd_attribute = self._lookup_attribute(attribute_group,
                                                   expanded_name, value)
            if xsd_attribute is None:
                self._handle_unknown_attribute(
                    expanded_name, value, elem, old_nsmap,
                    allocate_only, target)
                continue

            new_name = expanded_name
            if xsd_attribute.type.is_qname() or \
                    xsd_attribute.type.is_notation() or \
                    self._type_contains_qname(xsd_attribute.type):
                self._check_qname_type(value, xsd_attribute.type, old_nsmap)
                if allocate_only:
                    if self._is_qname_value(value, xsd_attribute.type, old_nsmap):
                        self._register_value_ns(value, old_nsmap)
                else:
                    assert target is not None
                    target.set(new_name, self._rewrite_if_qname(
                        value, xsd_attribute.type, old_nsmap, new_nsmap))
            elif not allocate_only:
                assert target is not None
                target.set(new_name, value)

    def _rewrite_xsi_type(self, value: str, old_nsmap: NsmapType,
                          new_nsmap: NsmapType) -> str:
        # xsi:type must always resolve; an unresolvable prefix is an error.
        rewriter = _QNameRewriter(old_nsmap, new_nsmap)
        result = rewriter.rewrite(value)
        if ':' in result:
            self._qname_prefixes.add(result.split(':', 1)[0])
        return result

    def _lookup_attribute(self, attribute_group: XsdAttributeGroup,
                          name: str, value: str):
        """
        Finds the declared attribute for an expanded attribute name. When the
        group has an `xs:anyAttribute`, the global attribute declaration is
        used if available (that's an actual declaration, not a guess).
        """
        try:
            xsd_attribute = attribute_group[name]
        except KeyError:
            wildcard = attribute_group.get(None)
            if isinstance(wildcard, XsdAnyAttribute) and \
                    wildcard.process_contents != 'skip':
                try:
                    return self.schema.maps.attributes[name]
                except KeyError:
                    return None
            return None
        return xsd_attribute if isinstance(xsd_attribute, XsdAttribute) else None

    def _handle_unknown_attribute(self, name: str, value: str,
                                  elem: ElementType, old_nsmap: NsmapType,
                                  allocate_only: bool,
                                  target: ElementType | None) -> None:
        if self.unknown_policy == 'reject':
            raise PrefixRewriteError(
                f"cannot prove the type of attribute {name!r} of {elem.tag!r} "
                f"from the schema; refusing to guess "
                f"(use unknown_policy='preserve' to keep it verbatim)"
            )
        if allocate_only:
            return
        assert target is not None
        target.set(name, value)

    # ---- Canonical semantics for verification ----------------------------

    def _canonical_element(self, resource: XMLResource,
                           elem: ElementType, xsd_element: XsdElement,
                           ancestor_nsmaps: list[NsmapType]) -> tuple:
        old_nsmap = self._element_nsmap(resource, elem, ancestor_nsmaps)
        xsd_type = self._effective_type(elem, xsd_element, old_nsmap)

        attributes: list[tuple[str, str]] = []
        attribute_group = xsd_element.get_attributes(xsd_type)
        for expanded_name, value in elem.attrib.items():
            if expanded_name == 'xmlns' or expanded_name.startswith('xmlns:'):
                continue
            if expanded_name == nm.XSI_TYPE:
                attributes.append(
                    (expanded_name, _QNameRewriter(old_nsmap, {}).resolve(value))
                )
                continue
            xsd_attribute = self._lookup_attribute(attribute_group,
                                                   expanded_name, value)
            if xsd_attribute is None:
                attributes.append((expanded_name, value))
            else:
                attributes.append((expanded_name, self._canonical_value(
                    value, xsd_attribute.type, old_nsmap)))

        text = elem.text
        if isinstance(xsd_type, XsdComplexType):
            if isinstance(xsd_type.content, XsdSimpleType):
                text = self._canonical_value(text, xsd_type.content, old_nsmap)
        elif isinstance(xsd_type, XsdSimpleType):
            text = self._canonical_value(text, xsd_type, old_nsmap)

        model_group = xsd_type.model_group if isinstance(
            xsd_type, XsdComplexType) else None
        visitor = model_group.get_model_visitor() \
            if model_group is not None else None

        children: list[tuple[str, tuple, str]] = []
        for child in elem:
            if callable(child.tag):
                children.append((f'#{child.tag.__name__}', (child.text or '',),
                                 child.tail or ''))
                continue

            xsd_child = self._resolve_child_declaration(visitor, child)
            if xsd_child is None:
                children.append(('?', _unproven_signature(child),
                                 child.tail or ''))
                continue
            children.append((child.tag,
                             self._canonical_element(
                                 resource, child, xsd_child,
                                 ancestor_nsmaps + [old_nsmap]),
                             child.tail or ''))

        return (elem.tag, text or '', tuple(attributes), tuple(children))

    def _canonical_value(self, value: str | None, xsd_type: XsdSimpleType,
                         nsmap: NsmapType) -> str:
        """
        Returns a prefix-independent representation of a lexical value:
        QName members become expanded names, list separators are normalized,
        and every other string is left exactly as it is.
        """
        if value is None or not value or not self._type_contains_qname(xsd_type):
            return value or ''

        if xsd_type.is_list():
            if not self._type_contains_qname(xsd_type.item_type):
                return value
            return ' '.join(
                _QNameRewriter(nsmap, {}).resolve(chunk)
                if self._is_qname_value(chunk, xsd_type.item_type, nsmap)
                else chunk
                for chunk in value.split()
            )

        if self._is_qname_value(value, xsd_type, nsmap):
            return _QNameRewriter(nsmap, {}).resolve(value)
        return value

    # ---- Unproven content and document nodes ------------------------------

    def _register_subtree_namespaces(self, elem: ElementType) -> None:
        """
        Registers every namespace used by element/attribute names inside a
        preserved (unproven) subtree, so the output still declares enough
        prefixes for its untouched prefixed content to resolve.
        """
        for node in elem.iter():
            if callable(node.tag):
                continue
            uri = get_namespace(node.tag)
            if uri:
                self._ensure_prefix(uri)
            for name in node.attrib:
                attr_uri = get_namespace(name)
                if attr_uri and attr_uri != nm.XMLNS_NAMESPACE:
                    self._ensure_prefix(attr_uri)

    def _copy_unproven_subtree(self, resource: XMLResource,
                               elem: ElementType) -> ElementType:
        """
        Copies an unproven subtree verbatim (preserve policy). The subtree
        keeps its own namespace declarations as explicit xmlns attributes so
        that any prefixed value inside it stays resolvable.
        """
        return deepcopy(elem)

    @staticmethod
    def _copy_document_node(node: ElementType) -> ElementType:
        # Comments and processing instructions are not typed content and do
        # not carry QNames, so they are copied unchanged.
        return deepcopy(node)

    def _check_qname_resolution(self, new_root: ElementType,
                                new_nsmap: NsmapType) -> None:
        """
        Verifies that the serialized output declares enough namespaces to
        resolve every QName lexical value present in the new tree. This is a
        structural check independent of the earlier per-value rewriting.
        """
        # The output serializer declares exactly the entries of new_nsmap at
        # the root; ensure no element/attribute namespace escaped the map.
        declared_uris = set(new_nsmap.keys())
        declared_prefixes = set(new_nsmap.values())
        for prefix in self._qname_prefixes:
            if prefix not in declared_prefixes:
                raise PrefixRewriteError(
                    f"prefix {prefix!r} used in a QName value is not "
                    f"declared in the rewritten document"
                )
        for elem in new_root.iter():
            if callable(elem.tag):
                continue
            uri = get_namespace(elem.tag)
            if uri and uri not in declared_uris:
                raise PrefixRewriteError(
                    f"namespace {uri!r} of element {elem.tag!r} is not "
                    f"declared in the rewritten document"
                )
            for name in elem.attrib:
                attr_uri = get_namespace(name)
                if attr_uri and attr_uri != nm.XMLNS_NAMESPACE \
                        and attr_uri not in declared_uris:
                    raise PrefixRewriteError(
                        f"namespace {attr_uri!r} of attribute {name!r} is "
                        f"not declared in the rewritten document"
                    )


# ---- Serialization -------------------------------------------------------

def _unproven_signature(elem: ElementType) -> tuple:
    """
    Structural signature of schema-unproven content. Expanded names, text
    and order are compared; prefixed values inside such content are not
    interpreted as QNames and are compared as raw strings.
    """
    attributes = tuple(sorted(
        (name, value) for name, value in elem.attrib.items()
        if not (name == 'xmlns' or name.startswith('xmlns:'))
    ))
    children = tuple(
        (f'#{node.tag.__name__}', (node.text or '',), node.tail or '')
        if callable(node.tag)
        else (node.tag, _unproven_signature(node), node.tail or '')
        for node in elem
    )
    return ('?', elem.tag, elem.text or '', attributes, children)


def _emit_qname(expanded: str, new_nsmap: NsmapType) -> str:
    """Emits an expanded QName using a prefix available in the target map."""
    if expanded[0] != '{':
        return expanded
    uri = get_namespace(expanded)
    name = local_name(expanded)
    for prefix, ns_uri in new_nsmap.items():
        if ns_uri == uri and prefix:
            return f'{prefix}:{name}'
    if new_nsmap.get('') == uri:
        return name
    raise PrefixRewriteError(
        f"no binding for namespace {uri!r} in the rewritten document, "
        f"cannot emit QName {expanded!r}"
    )


def _is_ncname(value: str) -> bool:
    """Checks that *value* can be used as an XML namespace prefix (NCName)."""
    if not value:
        return False
    first = value[0]
    if not (first.isalpha() or first == '_'):
        return False
    for char in value[1:]:
        if char in '-_.':
            continue
        if not (char.isalnum() or ord(char) > 127):
            return False
    return True


def _serialize(root: ElementType, nsmap: NsmapType,
               xml_declaration: bool, encoding: str):
    """
    Serializes the rewritten tree with all namespace declarations on the
    root element (the allocation guarantees that root declarations suffice
    to resolve every QName value in the document).
    """
    uri_to_prefix = {uri: prefix for uri, prefix in nsmap.items()}
    parts: list[str] = []
    if xml_declaration:
        parts.append("<?xml version='1.0' encoding='utf-8'?>\n")
    _serialize_element(root, uri_to_prefix, parts, is_root=True)
    text = ''.join(parts)
    if encoding.lower() == 'unicode':
        return text
    return text.encode(encoding)


def _serialize_element(elem: ElementType, uri_to_prefix: dict[str, str],
                       parts: list[str], is_root: bool = False) -> None:
    if callable(elem.tag):
        _serialize_node(elem, parts)
        return

    parts.append(f'<{_tag_name(elem.tag, uri_to_prefix)}')
    if is_root:
        for uri, prefix in sorted(uri_to_prefix.items(), key=lambda item: item[1]):
            if prefix in ('xml', 'xmlns'):
                continue  # the 'xml' prefix is implicitly in scope
            if prefix:
                parts.append(f' xmlns:{prefix}={quoteattr(uri)}')
            else:
                parts.append(f' xmlns={quoteattr(uri)}')

    for name, value in elem.attrib.items():
        if name == 'xmlns' or name.startswith('xmlns:'):
            continue
        parts.append(f' {_tag_name(name, uri_to_prefix)}={quoteattr(value)}')

    has_content = elem.text is not None or len(elem) > 0
    if not has_content:
        parts.append(' />')
        return

    parts.append('>')
    if elem.text is not None:
        parts.append(escape(elem.text))
    for child in elem:
        _serialize_element(child, uri_to_prefix, parts)
        if child.tail is not None and not callable(child.tag):
            parts.append(escape(child.tail))
    parts.append(f'</{_tag_name(elem.tag, uri_to_prefix)}>')


def _serialize_node(node: ElementType, parts: list[str]) -> None:
    tag = node.tag
    if tag is ElementTree.Comment:
        parts.append(f'<!--{node.text or ""}-->')
    elif tag is ElementTree.ProcessingInstruction:
        # stdlib stores the full "target data" string in .text.
        parts.append(f'<?{node.text or ""} ?>')
    if node.tail is not None:
        parts.append(escape(node.tail))


def _tag_name(tag: str, uri_to_prefix: dict[str, str]) -> str:
    if tag[0] != '{':
        return tag
    uri, name = tag[1:].split('}', 1)
    prefix = uri_to_prefix.get(uri)
    if prefix is None:
        raise PrefixRewriteError(
            f"missing output prefix for namespace {uri!r} (tag {tag!r})"
        )
    return f'{prefix}:{name}' if prefix else name


def rewrite_namespace_prefixes(source: object, schema: XMLSchemaBase,
                               prefix_map: Mapping[str, str] | None = None,
                               *, unknown_policy: str = 'reject',
                               verify: bool = True) -> ElementType:
    """
    Rewrites the namespace prefixes of an XML document in a schema-aware
    way, returning a new Element tree. The source document is not modified.

    :param source: the XML document (an :class:`~xmlschema.XMLResource`, an \
    Element/ElementTree, a string, bytes or file-like object).
    :param schema: the schema describing the document.
    :param prefix_map: optional mapping from namespace URIs to output prefixes.
    :param unknown_policy: ``'reject'`` (default) or ``'preserve'`` for \
    schema-unproven wildcard content.
    :param verify: when `True` (default) both the source and the rewritten \
    documents are decoded with *schema* and compared semantically, and the \
    output namespace declarations are checked for QName resolution.
    :raises PrefixRewriteError: when rewriting cannot be completed safely or \
    verification fails.
    """
    rewriter = NamespacePrefixRewriter(schema, prefix_map, unknown_policy)
    resource = rewriter._get_resource(source)
    rewriter._pass_validation(resource)
    new_root, new_nsmap = rewriter._build(resource)

    if verify:
        _verify_semantic_equivalence(rewriter, resource, new_root, new_nsmap)
        rewriter._check_qname_resolution(new_root, new_nsmap)
    return new_root


def _verify_semantic_equivalence(rewriter: 'NamespacePrefixRewriter',
                                 resource: XMLResource,
                                 new_root: ElementType,
                                 new_nsmap: NsmapType) -> None:
    """
    Decodes the source document and the rewritten document with the *same*
    schema and asserts that the decoded semantic values are equal. The
    comparison structures are built with the schema types as the only source
    of truth: every QName-typed value is normalized to its expanded name, so
    the comparison is independent of prefixes; element order, attributes and
    mixed text remain in document order. The rewritten document is parsed
    from its serialization, i.e. the exact bytes a partner would receive.
    """
    schema = rewriter.schema

    # The source must be valid first: otherwise equivalence cannot be claimed.
    source_errors = list(schema.iter_errors(resource))
    if source_errors:
        details = '; '.join(str(e.reason) for e in source_errors[:3])
        raise PrefixRewriteError(
            f"the source document is not valid against the schema: {details}"
        )

    serialized = _serialize(new_root, new_nsmap,
                            xml_declaration=False, encoding='unicode')
    new_resource = XMLResource(serialized)
    new_errors = list(schema.iter_errors(new_resource))
    if new_errors:
        details = '; '.join(str(e.reason) for e in new_errors[:3])
        raise PrefixRewriteError(
            f"the rewritten document is not valid against the schema: {details}"
        )

    source_semantics = _canonical_semantics(rewriter, resource)
    output_semantics = _canonical_semantics(rewriter, new_resource)
    if source_semantics != output_semantics:
        raise PrefixRewriteError(
            "semantic values changed after prefix rewriting"
        )


def _decode_pair(schema: XMLSchemaBase, resource: XMLResource):
    result = schema.decode(resource, validation='lax')
    if isinstance(result, tuple):
        return result
    return result, []


def _canonical_semantics(rewriter: 'NamespacePrefixRewriter',
                         resource: XMLResource) -> tuple:
    """
    Builds a prefix-independent semantic projection of a document, using the
    same schema-driven walk as the rewriter. QName values are represented in
    expanded form; non-QName strings (including colon-containing ones) and
    the order of elements, attributes and mixed text are preserved.
    """
    rewriter._pass_validation(resource)
    root = resource.root
    root_xsd = rewriter._element_types.get(id(root))
    if root_xsd is None or rewriter._is_dummy_element(root_xsd):
        # Preserved unproven content is represented structurally, expanded
        # names only: its prefixed values are intentionally not interpreted.
        return _unproven_signature(root)
    return rewriter._canonical_element(resource, root, root_xsd, [])


#: Convenience alias.
rewrite_prefixes = rewrite_namespace_prefixes
