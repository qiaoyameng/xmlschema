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
Streaming record encoding to a binary sink.

This module implements an incremental encoder for exporting a sequence of
records (e.g. rows fetched one by one from a database) to an XML document
without building the whole ElementTree in memory. The first version supports
only schemas where the content model of the root element consists of a single
repeatable element particle (the *record* particle).

Each record is encoded with the standard encoder, so type conversion,
namespace handling, default values and content model validation are the same
of the non-streaming APIs. Cross-record identity constraints (xs:key,
xs:keyref, xs:unique) are checked when the source is exhausted, before
writing the root closing tag.
"""
import codecs
from collections.abc import Iterable
from typing import Any, BinaryIO, NoReturn, Optional
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from elementpath import build_node_tree

from xmlschema.aliases import NsmapType
from xmlschema.converters import ConverterType
from xmlschema.exceptions import XMLSchemaException, XMLSchemaTypeError, \
    XMLSchemaValueError
from xmlschema.translation import gettext as _
from xmlschema.arguments import check_validation_mode
from xmlschema.validators import XMLSchemaBase
from xmlschema.validators.exceptions import XMLSchemaValidationError
from xmlschema.validators.validation import EncodeContext
from xmlschema.validators.identities import XsdIdentity, XsdKeyref, \
    IdentityCounter, KeyrefCounter
from xmlschema.validators.groups import XsdGroup
from xmlschema.validators.elements import XsdElement
from xmlschema.validators.wildcards import XsdAnyElement

__all__ = ('XMLSchemaStreamError', 'RecordStreamResult', 'encode_records',
           'write_records')


class XMLSchemaStreamError(XMLSchemaException):
    """Raised when a streaming record encoding process cannot continue.

    The :attr:`result` attribute reports the range already written to the sink
    (a valid XML prefix) and :attr:`error` carries the original failure.
    """

    def __init__(self, message: str, result: 'RecordStreamResult',
                 error: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.result = result
        self.error = error


class RecordStreamResult:
    """
    The outcome of a streaming record encoding process.

    :ivar bytes_written: number of bytes written to the sink. In case of failure \
    this is the length of the XML prefix already emitted.
    :ivar records_written: number of records fully encoded and written.
    :ivar completed: `True` only if every record has been encoded, all the \
    cross-record checks passed and the closing root tag has been written. In \
    every other case the written data is only an XML prefix and must not be \
    considered a complete valid document.
    :ivar error: the error that stopped the process, or `None` on completion.
    """
    __slots__ = ('bytes_written', 'records_written', 'completed', 'error')

    def __init__(self) -> None:
        self.bytes_written = 0
        self.records_written = 0
        self.completed = False
        self.error: Optional[BaseException] = None

    @property
    def reason(self) -> Optional[str]:
        """The failure reason as a string, or `None` on completion."""
        return None if self.error is None else str(self.error) or repr(self.error)

    @property
    def written_range(self) -> tuple[int, int]:
        """The `(start, end)` byte range written to the sink."""
        return 0, self.bytes_written

    @property
    def valid_prefix(self) -> bool:
        """`True` if the bytes written so far form a legal XML prefix."""
        return self.bytes_written > 0

    def __bool__(self) -> bool:
        return self.completed

    def __repr__(self) -> str:
        return '{}({})'.format(
            self.__class__.__name__,
            ', '.join(f'{k}={getattr(self, k)!r}'
                      for k in self.__slots__)
        )


def _check_record_particle(root_element: XsdElement) -> XsdElement:
    """Validate the first-version schema shape and return the record particle."""
    model_group = root_element.type.model_group
    if model_group is None:
        reason = _("root element {!r} has no element content model")
        raise XMLSchemaValueError(reason.format(root_element.local_name))
    elif model_group.mixed:
        reason = _("mixed content is not supported by streaming record encoding")
        raise XMLSchemaValueError(reason)

    particles = [p for p in model_group if p.max_occurs != 0]
    if len(particles) != 1:
        reason = _("streaming record encoding needs a root content model with a "
                   "single repeatable record particle, found {0} particles")
        raise XMLSchemaValueError(reason.format(len(particles)))

    particle = particles[0]
    if isinstance(particle, XsdGroup):
        if particle.model != 'sequence' or len(particle) != 1:
            reason = _("the unique root particle must be a repeatable element")
            raise XMLSchemaValueError(reason)
        particle = particle[0]

    if not isinstance(particle, XsdElement) or not (
            particle.max_occurs is None or particle.max_occurs > 1):
        reason = _("the root record particle {!r} must be a repeatable element")
        raise XMLSchemaValueError(reason.format(getattr(particle, 'name', None)))
    return particle


def _tag_name(tag: str, namespaces: NsmapType) -> str:
    """Return the prefixed name of an expanded QName, if a prefix is known."""
    if tag[0] != '{':
        return tag

    namespace, local = tag[1:].split('}', 1)
    for prefix, uri in namespaces.items():
        if uri == namespace:
            return local if not prefix else f'{prefix}:{local}'
    return f'ns0:{local}'


def _element_open_tag(tag: str, namespaces: NsmapType,
                      attributes: Optional[list[tuple[str, str]]] = None,
                      encoding: str = 'utf-8') -> bytes:
    """Build the bytes of an element opening tag with namespace declarations."""
    declarations = dict(namespaces)
    name = _tag_name(tag, declarations)
    if name.startswith('ns0:') and 'ns0' not in declarations:
        declarations['ns0'] = tag[1:].split('}', 1)[0]

    chunks = [f'<{name}'.encode(encoding)]
    for prefix, uri in declarations.items():
        if not uri:
            continue
        declaration = 'xmlns' if not prefix else f'xmlns:{prefix}'
        chunks.append(f' {declaration}={quoteattr(uri)}'.encode(encoding))

    if attributes:
        for attr_name, value in attributes:
            chunks.append(f' {attr_name}={quoteattr(value)}'.encode(encoding))
    chunks.append(b'>')
    return b''.join(chunks)


def _attribute_name(name: str, namespaces: NsmapType) -> str:
    """Return the prefixed name of an expanded attribute QName."""
    if name[0] != '{':
        return name

    namespace, local = name[1:].split('}', 1)
    for prefix, uri in namespaces.items():
        if prefix and uri == namespace:
            return f'{prefix}:{local}'
    return f'ns0:{local}'


def _close_tag(tag: str, namespaces: NsmapType, encoding: str = 'utf-8') -> bytes:
    """Build the bytes of an element closing tag."""
    return f'</{_tag_name(tag, namespaces)}>'.encode(encoding)


def encode_records(schema: 'XMLSchemaBase',
                   records: Iterable[Any],
                   sink: BinaryIO,
                   path: Optional[str] = None,
                   validation: str = 'strict',
                   namespaces: Optional[NsmapType] = None,
                   use_defaults: bool = True,
                   converter: Optional[ConverterType] = None,
                   unordered: bool = False,
                   process_skipped: bool = False,
                   max_depth: Optional[int] = None,
                   untyped_data: bool = False,
                   etree_element_class: Optional[type] = None,
                   root_attributes: Optional[dict[str, Any]] = None,
                   xml_declaration: bool = True,
                   encoding: str = 'utf-8',
                   raise_on_error: bool = False,
                   **kwargs: Any) -> RecordStreamResult:
    """
    Encode an iterable of records to a binary sink, incrementally.

    The root element of the schema must have a content model made of a single
    repeatable element particle. Each item of *records* is encoded as one
    occurrence of that particle, reusing the standard encoding pipeline
    (type conversion, namespaces, default values and content model
    validation). Records are written in the given order without building the
    full XML tree in memory. Cross-record xs:key/xs:keyref/xs:unique
    constraints are checked after the source is exhausted and before writing
    the closing root tag.

    :param schema: an :class:`XMLSchemaBase` instance containing the root \
    element to encode.
    :param records: an iterable yielding the data of each record, in the same \
    format accepted by :meth:`XMLSchemaBase.encode`.
    :param sink: a binary file-like object exposing a ``write(bytes)`` method. \
    The sink is not closed by this function.
    :param path: optional XPath expression selecting the root element of the \
    schema; if omitted the unique root element is used.
    :param validation: the XSD validation mode, ``'strict'`` (default), \
    ``'lax'`` or ``'skip'``.
    :param namespaces: optional mapping from namespace prefix to URI.
    :param use_defaults: whether to apply schema default values.
    :param converter: converter class or instance to use for the encoding.
    :param unordered: activates the unordered encoding mode for content models.
    :param process_skipped: process data matching a wildcard with \
    ``processContents='skip'``.
    :param max_depth: optional maximum encoding depth.
    :param untyped_data: accept untyped atomic values for all built-in types.
    :param etree_element_class: optional Element class used for building the \
    per-record element fragments.
    :param root_attributes: optional mapping with the attributes of the root \
    element; schema default values are applied if *use_defaults* is true.
    :param xml_declaration: whether to emit the XML declaration.
    :param encoding: the encoding used for the binary output (also written in \
    the XML declaration).
    :param raise_on_error: if `True` a :class:`XMLSchemaStreamError` carrying \
    the result is raised on failure, otherwise the failed result is returned.
    :param kwargs: other options for building the converter instance.
    :return: a :class:`RecordStreamResult` describing the written range and \
    the completion/failure status. On failure the result has ``completed=False``, \
    carries the error/reason and the sink contains only a valid XML prefix; the \
    encoder must not be reused.
    """
    if not isinstance(schema, XMLSchemaBase) and isinstance(records, XMLSchemaBase):
        schema, records = records, schema

    check_validation_mode(validation)
    if not hasattr(sink, 'write'):
        raise XMLSchemaTypeError(_("the sink must have a write() method"))
    try:
        codecs.lookup(encoding)
    except LookupError as err:
        raise XMLSchemaValueError(_("unknown output encoding {!r}").format(encoding)) from err

    if path:
        root_element = schema.get_element(path)
    elif len(schema.root_elements) == 1:
        root_element = schema.root_elements[0]
    else:
        root_element = None
    if not isinstance(root_element, XsdElement):
        reason = _("a single root element is required for streaming record "
                   "encoding, provide a 'path' argument")
        raise XMLSchemaValueError(reason)

    record_element = _check_record_particle(root_element)
    result = RecordStreamResult()
    encoder = _RecordEncoder(
        schema=schema,
        root_element=root_element,
        record_element=record_element,
        sink=sink,
        result=result,
        validation=validation,
        namespaces=namespaces,
        use_defaults=use_defaults,
        converter=converter,
        unordered=unordered,
        process_skipped=process_skipped,
        max_depth=max_depth,
        untyped_data=untyped_data,
        etree_element_class=etree_element_class,
        root_attributes=root_attributes,
        xml_declaration=xml_declaration,
        encoding=encoding,
        raise_on_error=raise_on_error,
        kwargs=kwargs,
    )
    return encoder.run(records)


class _RecordEncoder:
    """Incremental binary encoder for single-record-particle root elements."""

    def __init__(self, schema: 'XMLSchemaBase',
                 root_element: XsdElement,
                 record_element: XsdElement,
                 sink: BinaryIO,
                 result: RecordStreamResult,
                 validation: str,
                 namespaces: Optional[NsmapType],
                 use_defaults: bool,
                 converter: Optional[ConverterType],
                 unordered: bool,
                 process_skipped: bool,
                 max_depth: Optional[int],
                 untyped_data: bool,
                 etree_element_class: Optional[type],
                 root_attributes: Optional[dict[str, Any]],
                 xml_declaration: bool,
                 encoding: str,
                 raise_on_error: bool,
                 kwargs: dict[str, Any]) -> None:

        self.schema = schema
        self.root_element = root_element
        self.record_element = record_element
        self.sink = sink
        self.result = result
        self.validation = validation
        self.encoding = encoding
        self.root_attributes = root_attributes
        self.raise_on_error = raise_on_error

        encode_kwargs = dict(kwargs)
        encode_kwargs.update(
            source=None,
            namespaces=namespaces,
            check_identities=True,
            use_defaults=use_defaults,
            converter=converter,
            unordered=unordered,
            process_skipped=process_skipped,
            max_depth=max_depth,
            untyped_data=untyped_data,
            etree_element_class=etree_element_class,
        )
        encode_kwargs['converter'] = schema.maps.settings.get_converter(**encode_kwargs)
        self.context = EncodeContext(**encode_kwargs)
        self.namespaces = self.context.namespaces

        for prefix, uri in self.schema.namespaces.items():
            if not prefix or prefix in ('xs', 'xml') or not uri:
                continue
            elif uri not in self.namespaces.values():
                self.namespaces[prefix] = uri

        self._register_namespaces()
        self.context.indent = 0

        self.counters: dict[XsdIdentity, IdentityCounter] = {}
        for identity in root_element.identities:
            if identity.ref is not None:
                identity = identity.ref
            if isinstance(identity, XsdKeyref):
                self.counters[identity] = KeyrefCounter(identity, root_element)
            else:
                self.counters[identity] = IdentityCounter(identity, root_element)

        self.xml_declaration = xml_declaration

    def _register_namespaces(self) -> None:
        for prefix, uri in self.namespaces.items():
            if not uri or prefix in ('xs', 'xml'):
                continue
            elif prefix and not prefix.startswith('ns'):
                ElementTree.register_namespace(prefix, uri)
        if self.namespaces.get(''):
            ElementTree.register_namespace('', self.namespaces[''])

    @staticmethod
    def _get_selectors(identity: XsdIdentity, xsd_element: XsdElement):
        selectors = identity.elements.get(xsd_element)
        if selectors is None:
            for element, field_selectors in identity.elements.items():
                if element.name == xsd_element.name:
                    return field_selectors
            return []
        return selectors

    def _iter_matching(self, element, xsd_element: XsdElement):
        """Pair the encoded elements of a record with their XSD declarations."""
        yield element, xsd_element

        xsd_type = xsd_element.type
        if not xsd_type.is_complex():
            return

        model_group = xsd_type.model_group
        if model_group is None:
            return

        for child in element:
            child_element = model_group.match_element(child.tag)
            if isinstance(child_element, XsdAnyElement) or child_element is None:
                continue
            yield from self._iter_matching(child, child_element)

    def run(self, records: Iterable[Any]) -> RecordStreamResult:
        try:
            self._write_header()
            self._write_records(records)
            self._finalize()
        except XMLSchemaStreamError as stream_error:
            if self.raise_on_error:
                raise
            return stream_error.result
        except BaseException as err:
            self.result.error = err
            if self.raise_on_error:
                message = str(err) or repr(err)
                raise XMLSchemaStreamError(message, self.result, err) from err
            return self.result

        return self.result

    def _fail(self, error: BaseException) -> NoReturn:
        if self.result.error is None:
            self.result.error = error
        message = str(error) or repr(error)
        stream_error = XMLSchemaStreamError(message, self.result, error)
        raise stream_error from error

    def _write(self, data: bytes) -> None:
        while data:
            try:
                n = self.sink.write(data)
            except BaseException as err:
                self._fail(err)

            if n is None:
                n = len(data)
            elif not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                error = OSError(_("sink refused to write data (returned {!r})").format(n))
                self._fail(error)
            elif n > len(data):
                error = OSError(_("sink reported an invalid write size {!r}").format(n))
                self._fail(error)

            self.result.bytes_written += n
            data = data[n:]

    def _write_header(self) -> None:
        if self.xml_declaration:
            self._write(
                f'<?xml version="1.0" encoding="{self.encoding}"?>\n'.encode(self.encoding)
            )

        attributes: Optional[list[tuple[str, str]]] = None
        if self.root_attributes is not None or self.root_element.attributes:
            attribute_group = self.root_element.get_attributes(self.root_element.type)
            attributes = attribute_group.raw_encode(
                self.root_attributes or {}, self.validation, self.context
            )
            if self.context.errors:
                self._fail(self.context.errors[0])
            attributes = [
                (_attribute_name(name, self.namespaces), value)
                for name, value in attributes
            ]

        self._write(_element_open_tag(
            self.root_element.name, self.namespaces, attributes, self.encoding
        ))

    def _write_records(self, records: Iterable[Any]) -> None:
        max_occurs = self.record_element.max_occurs
        for record in records:
            if max_occurs is not None and self.result.records_written >= max_occurs:
                reason = _("too many records for the root record particle {!r} "
                           "(maxOccurs={})").format(
                    self.record_element.local_name, max_occurs)
                self._fail(XMLSchemaValidationError(
                    self.schema, None, reason, namespaces=self.namespaces
                ))

            self.context.errors.clear()
            self.context.level = 0
            self.context.elem = None

            try:
                element = self.record_element.raw_encode(
                    record, self.validation, self.context
                )
            except XMLSchemaValidationError as err:
                self._fail(err)

            errors = self.context.errors
            if errors:
                # In lax mode errors are collected; stop on the first one so the
                # stream never emits a record that did not fully validate.
                self._fail(errors[0])
            if element is None:
                error = XMLSchemaValueError(
                    _("record encoding returned no element for {!r}").format(
                        self.record_element.local_name)
                )
                self._fail(error)

            self._check_record_identities(element)
            data = ElementTree.tostring(element, encoding='utf-8')
            if self.encoding != 'utf-8':
                data = data.decode('utf-8').encode(self.encoding)
            self._write(data)
            self.result.records_written += 1

    def _check_record_identities(self, element) -> None:
        node_root = build_node_tree(element)
        for record_part, xsd_element in self._iter_matching(element, self.record_element):
            if xsd_element.ref is not None:
                xsd_element = xsd_element.ref

            node = node_root.get_element_node(record_part)
            if node is None:
                continue

            for identity, counter in self.counters.items():
                selectors = self._get_selectors(identity, xsd_element)
                if not selectors:
                    continue

                try:
                    fields = tuple(
                        selector.get_value(node, self.namespaces) for selector in selectors
                    )
                except (XMLSchemaValueError, XMLSchemaTypeError) as err:
                    self._fail(err)
                    return

                if not any(value is not None for value in fields):
                    continue
                try:
                    counter.increase(fields)
                except ValueError as err:
                    self._fail(err)
                    return

    def _finalize(self) -> None:
        if self.result.records_written < self.record_element.min_occurs:
            reason = _("record {!r} occurs {} time(s), minOccurs={}").format(
                self.record_element.local_name, self.result.records_written,
                self.record_element.min_occurs)
            self._fail(XMLSchemaValidationError(
                self.schema, None, reason, namespaces=self.namespaces
            ))

        self._check_idref_references()
        self._check_key_references()

        self._write(_close_tag(self.root_element.name, self.namespaces, self.encoding))
        self.result.completed = True

    def _check_idref_references(self) -> None:
        for value, count in self.context.id_map.items():
            if count == 0:
                reason = _("IDREF %r not found in XML document") % value
                self._fail(XMLSchemaValidationError(
                    self.schema, value, reason, namespaces=self.namespaces
                ))

    def _check_key_references(self) -> None:
        for identity, counter in self.counters.items():
            if not isinstance(identity, XsdKeyref):
                continue
            for error in counter.iter_errors(self.counters):
                self._fail(XMLSchemaValidationError(
                    self.schema, None, str(error), namespaces=self.namespaces
                ))
                return


# Alias for the sink-oriented API.
write_records = encode_records
