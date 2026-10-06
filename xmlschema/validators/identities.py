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
This module contains classes for other XML Schema identity constraints.
"""
import copy
import datetime
import os
import pickle
import re
import math
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, cast, Any, Optional, Union

from elementpath import ElementPathError, XPathContext, \
    ElementNode, translate_pattern, AttributeNode
from elementpath.datatypes import UntypedAtomic, QName, Duration, \
    AbstractDateTime, AbstractBinary
from elementpath.xpath_nodes import EtreeElementNode

import xmlschema.names as nm
from xmlschema.exceptions import XMLSchemaTypeError, XMLSchemaValueError
from xmlschema.translation import gettext as _
from xmlschema.utils.qnames import get_qname, get_extended_qname
from xmlschema.aliases import ElementType, SchemaType, NsmapType, AtomicValueType, \
    BaseXsdType, SchemaElementType, SchemaAttributeType
from .helpers import parse_xpath_default_namespace
from ..xpath import IdentityXPathParser, XPathElement, XMLSchemaProxy

from .exceptions import XMLSchemaNotBuiltError
from .xsdbase import XsdComponent
from .attributes import XsdAttribute
from .wildcards import XsdAnyElement, XsdWildcard
from . import elements as elements_module

if TYPE_CHECKING:
    from .elements import XsdElement

IdentityFieldItemType = Union[AtomicValueType, XsdAttribute, tuple[Any, ...], None]
IdentityCounterType = tuple[IdentityFieldItemType, ...]
IdentitySourceType = Optional[tuple[Optional[str], Optional[int]]]


# XSD identities use a restricted XPath 2.0 parser. The XMLSchemaProxy is
# not used for the specific selection of fields and elements and the XSD
# fields are collected at first validation run.

IdentityMapType = dict[Union['XsdKey', 'XsdKeyref', str, None],
                       Union['IdentityCounter', 'KeyrefCounter']]
IdentityNodeType = Union[ElementNode, AttributeNode]
FieldDecoderType = Union[SchemaElementType, SchemaAttributeType]


def _encoded_part(tag: bytes, value: bytes = b'') -> bytes:
    return tag + str(len(value)).encode('ascii') + b':' + value


def encode_identity_value(value: IdentityFieldItemType) -> bytes:
    """Encode an identity value preserving the equality semantics used in memory."""
    if value is None:
        return b'N'
    elif isinstance(value, bool):
        return b'b' + (b'1' if value else b'0')
    elif isinstance(value, tuple):
        parts = [b'L' + str(len(value)).encode('ascii') + b':']
        parts.extend(encode_identity_value(item) for item in value)
        return b''.join(parts)
    elif isinstance(value, str):
        return _encoded_part(b's', value.encode('utf-8'))
    elif isinstance(value, bytes):
        return _encoded_part(b'x', value)
    elif isinstance(value, (Decimal, int)):
        canonical = format(Decimal(value).normalize(), 'f')
        return _encoded_part(b'n', canonical.encode('ascii'))
    elif isinstance(value, float):
        if math.isnan(value):
            return b'Fnan'
        return _encoded_part(b'f', repr(value).encode('ascii'))
    elif isinstance(value, QName):
        uri = value.uri.encode('utf-8')
        local_name = value.local_name.encode('utf-8')
        return _encoded_part(b'q', uri + b'|' + local_name)
    elif isinstance(value, Duration):
        seconds = format(value.seconds, 'f')
        serialized = f'{value.__class__.__name__}|{value.months}|{seconds}'
        return _encoded_part(b'D', serialized.encode('utf-8'))
    elif isinstance(value, AbstractDateTime):
        if value.tzinfo is None:
            serialized = f'{value.__class__.__name__}|{value._year}|' \
                         f'{value._dt.replace(tzinfo=None).isoformat()}'
        else:
            dt = value._dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            serialized = f'{value.__class__.__name__}|{value._year}|{dt.isoformat()}'
        return _encoded_part(b'T', serialized.encode('utf-8'))
    elif isinstance(value, AbstractBinary):
        return _encoded_part(b'B', value.decode())

    try:
        return _encoded_part(b'P', pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    except (TypeError, pickle.PicklingError):
        return _encoded_part(b'r', repr(value).encode('utf-8'))


def encode_identity_key(fields: IdentityCounterType) -> bytes:
    parts = [b'K' + str(len(fields)).encode('ascii') + b':']
    parts.extend(encode_identity_value(field) for field in fields)
    return b''.join(parts)


def identity_fields_tuple(fields: Any) -> IdentityCounterType:
    return fields if isinstance(fields, tuple) else (fields,)


class XsdSelector(XsdComponent):
    """Class for defining an XPath selector for an XSD identity constraint."""
    _ADMITTED_TAGS = nm.XSD_SELECTOR,
    _REGEXP = (
        r"(\.//)?(((child::)?((\i\c*:)?(\i\c*|\*)))|\.)(/(((child::)?"
        r"((\i\c*:)?(\i\c*|\*)))|\.))*(\|(\.//)?(((child::)?((\i\c*:)?"
        r"(\i\c*|\*)))|\.)(/(((child::)?((\i\c*:)?(\i\c*|\*)))|\.))*)*"
    )
    pattern: Optional[re.Pattern[str]] = None
    xpath_default_namespace = ''

    def __init__(self, elem: ElementType, schema: SchemaType,
                 parent: Optional['XsdIdentity']) -> None:
        super().__init__(elem, schema, parent)

    def _parse(self) -> None:
        try:
            self.path = self.elem.attrib['xpath']
        except KeyError:
            self.parse_error(_("'xpath' attribute required"))
            self.path = '*'
        else:
            path = self.path.replace(' ', '')
            if self.pattern is None:
                regexp = translate_pattern(
                    self._REGEXP,
                    back_references=False,
                    lazy_quantifiers=False,
                    anchors=False
                )
                self.__class__.pattern = re.compile(regexp)
                assert self.pattern is not None

            if not self.pattern.match(path):
                msg = _("invalid XPath expression for an {}")
                self.parse_error(msg.format(self.__class__.__name__))

        # XSD 1.1 xpathDefaultNamespace attribute
        if self.schema.XSD_VERSION > '1.0':
            if 'xpathDefaultNamespace' in self.elem.attrib:
                self.xpath_default_namespace = parse_xpath_default_namespace(self)
            else:
                self.xpath_default_namespace = self.schema.xpath_default_namespace

        self.parser = IdentityXPathParser(
            namespaces=self.schema.namespaces,
            strict=False,
            compatibility_mode=True,
            default_namespace=self.xpath_default_namespace,
        )

        try:
            self.token = self.parser.parse(self.path)
        except ElementPathError as err:
            self.token = self.parser.parse('*')
            self.parse_error(err)

    def __repr__(self) -> str:
        return '%s(path=%r)' % (self.__class__.__name__, self.path)


class XsdFieldSelector(XsdSelector):
    """Class for defining an XPath field selector for an XSD identity constraint."""
    _ADMITTED_TAGS = nm.XSD_FIELD,
    _REGEXP = (
        r"(\.//)?((((child::)?((\i\c*:)?(\i\c*|\*)))|\.)/)*((((child::)?"
        r"((\i\c*:)?(\i\c*|\*)))|\.)|((attribute::|@)((\i\c*:)?(\i\c*|\*))))"
        r"(\|(\.//)?((((child::)?((\i\c*:)?(\i\c*|\*)))|\.)/)*"
        r"((((child::)?((\i\c*:)?(\i\c*|\*)))|\.)|"
        r"((attribute::|@)((\i\c*:)?(\i\c*|\*)))))*"
    )
    pattern = None


class XsdIdentity(XsdComponent):
    """
    Common class for XSD identity constraints.

    :ivar selector: the XPath selector of the identity constraint.
    :ivar fields: a list containing the XPath field selectors of the identity constraint.
    """
    name: str
    local_name: str
    prefixed_name: str
    parent: 'XsdElement'
    ref: Optional['XsdIdentity']

    selector: Optional[XsdSelector]
    fields: list[XsdFieldSelector]

    # XSD elements bound by selector (for speed-up and for lazy mode)
    elements: dict['XsdElement', list['FieldValueSelector']]

    __slots__ = ('selector', 'fields', 'elements')

    def __init__(self, elem: ElementType, schema: SchemaType,
                 parent: Optional['XsdElement']) -> None:
        super().__init__(elem, schema, parent)

    def _parse(self) -> None:
        try:
            self.name = get_qname(self.target_namespace, self.elem.attrib['name'])
        except KeyError:
            self.parse_error(_("missing required attribute 'name'"))
            self.name = ''

        for child in self.elem:
            if child.tag == nm.XSD_SELECTOR:
                self.selector = XsdSelector(child, self.schema, self)
                break
        else:
            self.parse_error(_("missing 'selector' declaration"))
            self.selector = None

        self.fields = []
        for child in self.elem:
            if child.tag == nm.XSD_FIELD:
                self.fields.append(XsdFieldSelector(child, self.schema, self))

        self.elements = {}

    def build(self) -> None:
        if self._built is not False:
            return
        self._built = None

        try:
            if self.ref is self:
                try:
                    ref = self.maps.identities[self.name]
                except KeyError:
                    self.fields = []
                    self.elements = {}
                    msg = _("unknown identity constraint {!r}")
                    self.parse_error(msg.format(self.name))
                    self.ref = None
                    return
                else:
                    if not isinstance(ref, self.__class__):
                        msg = _("attribute 'ref' points to a different kind constraint")
                        self.parse_error(msg)
                    self.selector = ref.selector
                    self.fields = ref.fields
                    self.elements = {}
                    self.ref = ref

            try:
                self.update_elements(base_element=self.parent)
            except TypeError as err:
                self.parse_error(err)

            self._built = True
        finally:
            if self._built is None:
                self._built = False

    def update_elements(self, base_element: Union['XsdElement', XPathElement]) -> None:
        if self.selector is None:
            return

        context = XPathContext(self.schema.xpath_node, item=base_element.xpath_node)
        e: Any
        for e in self.selector.token.select_results(context):
            if isinstance(e, elements_module.XsdElement):
                if e.name is not None:
                    if e.ref is not None:
                        e = e.ref
                    if e not in self.elements:
                        self.elements[e] = [FieldValueSelector(f, e) for f in self.fields]
                        e.selected_by.add(self)

            elif not isinstance(e, (XsdAnyElement, XPathElement)):
                msg = _("selector xpath expression can only select elements")
                raise XMLSchemaTypeError(msg)

        # Try to detect other target XSD elements extracting QNames of
        # the leaf elements from the XPath expression and use them to
        # match from global elements. Anyway identity counters created
        # by identity are not enabled if the data is outside the scope.
        qname: Any
        for qname in self.selector.token.iter_leaf_elements():
            e = self.maps.elements.get(
                get_extended_qname(qname, self.schema.namespaces)
            )
            if isinstance(e, elements_module.XsdElement):
                if e.ref is not None:
                    e = e.ref
                if e not in self.elements:
                    self.elements[e] = [FieldValueSelector(f, e) for f in self.fields]
                    e.selected_by.add(self)

    def get_counter(self, elem: ElementType,
                    index: Optional['SQLiteIdentityIndex'] = None,
                    index_threshold: Optional[int] = None) -> 'IdentityCounter':
        return IdentityCounter(self, elem, index, index_threshold)


class XsdUnique(XsdIdentity):
    _ADMITTED_TAGS = nm.XSD_UNIQUE,


class XsdKey(XsdIdentity):
    _ADMITTED_TAGS = nm.XSD_KEY,


class XsdKeyref(XsdIdentity):
    """
    Implementation of xs:keyref.

    :ivar refer: reference to a *xs:key* declaration that must be in the same element \
    or in a descendant element.
    """
    _ADMITTED_TAGS = nm.XSD_KEYREF,
    refer: str | XsdKey | None = None
    refer_path = '.'

    def _parse(self) -> None:
        super()._parse()
        try:
            self.refer = self.schema.resolve_qname(self.elem.attrib['refer'])
        except (KeyError, ValueError, RuntimeError) as err:
            if 'refer' not in self.elem.attrib:
                self.parse_error(_("missing required attribute 'refer'"))
            else:
                self.parse_error(err)

    def build(self) -> None:
        if self._built:
            return
        super().build()

        if isinstance(self.refer, (XsdKey, XsdUnique)):
            return  # referenced key/unique identity constraint already set
        elif isinstance(self.ref, XsdKeyref):
            self.refer = self.ref.refer

        if self.refer is None:
            return  # attribute or key/unique identity constraint missing
        elif isinstance(self.refer, str):
            refer: Optional[XsdIdentity]
            for refer in self.parent.identities:
                if refer.name == self.refer:
                    break
            else:
                refer = None

            if refer is not None and refer.ref is None:
                self.refer = refer  # type: ignore[assignment]
            else:
                try:
                    self.refer = self.maps.identities[self.refer]  # type: ignore[assignment]
                except KeyError:
                    msg = _("key/unique identity constraint %r is missing")
                    self.parse_error(msg % self.refer)
                    return

        if not isinstance(self.refer, (XsdKey, XsdUnique)):
            msg = _("reference to a non key/unique identity constraint %r")
            self.parse_error(msg % self.refer)
        elif len(self.refer.fields) != len(self.fields):
            msg = _("field cardinality mismatch between {0!r} and {1!r}")
            self.parse_error(msg.format(self, self.refer))
        elif self.parent is not self.refer.parent:
            refer_path = self.refer.parent.get_path(ancestor=self.parent)
            if refer_path is None:
                # From a note in par. 3.11.5 Part 1 of XSD 1.0 spec: "keyref
                # identity-constraints may be defined on domains distinct from
                # the embedded domain of the identity-constraint they reference,
                # or the domains may be the same but self-embedding at some depth.
                # In either case the node table for the referenced identity-constraint
                # needs to propagate upwards, with conflict resolution."
                refer_path = self.parent.get_path(ancestor=self.refer.parent, reverse=True)
                if refer_path is None:
                    path1 = self.parent.get_path(reverse=True)
                    path2 = self.refer.parent.get_path()
                    assert path1 is not None
                    assert path2 is not None
                    refer_path = f'{path1}/{path2}'

            self.refer_path = refer_path

    def get_counter(self, elem: ElementType,
                    index: Optional['SQLiteIdentityIndex'] = None,
                    index_threshold: Optional[int] = None) -> 'KeyrefCounter':
        return KeyrefCounter(self, elem, index, index_threshold)


class Xsd11Unique(XsdUnique):
    def _parse(self) -> None:
        if self._parse_reference():
            self.ref = self
        else:
            super()._parse()


class Xsd11Key(XsdKey):
    def _parse(self) -> None:
        if self._parse_reference():
            self.ref = self
        else:
            super()._parse()


class Xsd11Keyref(XsdKeyref):
    def _parse(self) -> None:
        if self._parse_reference():
            self.ref = self
        else:
            super()._parse()


class SQLiteIdentityIndex:
    """A temporary SQLite storage for identity constraint counters."""

    __slots__ = ('path', '_temporary_dir', '_connection', '_counter_ids')

    def __init__(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory(prefix='xmlschema-identity-')
        self.path = os.path.join(self._temporary_dir.name, 'identities.sqlite')
        self._connection = sqlite3.connect(self.path)
        self._counter_ids: dict[int, int] = {}

        self._connection.execute('PRAGMA journal_mode=MEMORY')
        self._connection.execute('PRAGMA synchronous=OFF')
        self._connection.execute(
            '''CREATE TABLE identity_values (
                counter_id INTEGER NOT NULL,
                tuple_key BLOB NOT NULL,
                fields BLOB NOT NULL,
                occurrences INTEGER NOT NULL,
                source_url TEXT,
                source_line INTEGER,
                PRIMARY KEY (counter_id, tuple_key)
            )'''
        )

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def counter_id(self, counter: 'IdentityCounter') -> int:
        key = id(counter)
        try:
            return self._counter_ids[key]
        except KeyError:
            counter_id = len(self._counter_ids) + 1
            self._counter_ids[key] = counter_id
            return counter_id

    def increase(self, counter: 'IdentityCounter', fields: IdentityCounterType,
                 source: IdentitySourceType = None) -> int:
        fields = identity_fields_tuple(fields)
        tuple_key = encode_identity_key(fields)
        payload = pickle.dumps(fields, protocol=pickle.HIGHEST_PROTOCOL)
        counter_id = self.counter_id(counter)
        source_url, source_line = source or (None, None)

        cursor = self._connection.execute(
            '''INSERT OR IGNORE INTO identity_values
               (counter_id, tuple_key, fields, occurrences, source_url, source_line)
               VALUES (?, ?, ?, 1, ?, ?)''',
            (counter_id, sqlite3.Binary(tuple_key), sqlite3.Binary(payload),
             source_url, source_line)
        )
        if cursor.rowcount:
            return 1

        self._connection.execute(
            '''UPDATE identity_values SET occurrences = occurrences + 1
               WHERE counter_id = ? AND tuple_key = ?''',
            (counter_id, sqlite3.Binary(tuple_key))
        )
        row = self._connection.execute(
            'SELECT occurrences FROM identity_values WHERE counter_id = ? AND tuple_key = ?',
            (counter_id, sqlite3.Binary(tuple_key))
        ).fetchone()
        return int(row[0])

    def migrate(self, counter: 'IdentityCounter',
                values: Counter[IdentityCounterType],
                exclude: Optional[IdentityCounterType] = None) -> None:
        counter_id = self.counter_id(counter)
        rows = []
        for fields, occurrences in values.items():
            fields = identity_fields_tuple(fields)
            if exclude is not None and fields == identity_fields_tuple(exclude):
                continue
            rows.append((
                counter_id,
                sqlite3.Binary(encode_identity_key(fields)),
                sqlite3.Binary(pickle.dumps(fields, protocol=pickle.HIGHEST_PROTOCOL)),
                occurrences,
                None,
                None,
            ))
        self._connection.executemany(
            '''INSERT OR IGNORE INTO identity_values
               (counter_id, tuple_key, fields, occurrences, source_url, source_line)
               VALUES (?, ?, ?, ?, ?, ?)''',
            rows
        )

    def merge(self, target: 'IdentityCounter', source: 'IdentityCounter') -> int:
        if source.on_disk:
            source_id = self.counter_id(source)
        else:
            source_id = self.counter_id(source)
            rows = []
            for fields, occurrences in source._counter.items():
                fields = identity_fields_tuple(fields)
                rows.append((
                    source_id,
                    sqlite3.Binary(encode_identity_key(fields)),
                    sqlite3.Binary(pickle.dumps(fields, protocol=pickle.HIGHEST_PROTOCOL)),
                    occurrences,
                    None,
                    None,
                ))
            self._connection.executemany(
                '''INSERT INTO identity_values
                   (counter_id, tuple_key, fields, occurrences, source_url, source_line)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                rows
            )

        if target.on_disk:
            target_id = self.counter_id(target)
        else:
            target_id = self.counter_id(target)
            rows = []
            for fields, occurrences in target._counter.items():
                fields = identity_fields_tuple(fields)
                rows.append((
                    target_id,
                    sqlite3.Binary(encode_identity_key(fields)),
                    sqlite3.Binary(pickle.dumps(fields, protocol=pickle.HIGHEST_PROTOCOL)),
                    occurrences,
                    None,
                    None,
                ))
            self._connection.executemany(
                '''INSERT INTO identity_values
                   (counter_id, tuple_key, fields, occurrences, source_url, source_line)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                rows
            )

        rows = self._connection.execute(
            '''SELECT tuple_key, fields, occurrences, source_url, source_line
               FROM identity_values WHERE counter_id = ?''',
            (source_id,)
        ).fetchall()
        for tuple_key, fields, occurrences, source_url, source_line in rows:
            self._connection.execute(
                '''INSERT INTO identity_values
                   (counter_id, tuple_key, fields, occurrences, source_url, source_line)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(counter_id, tuple_key) DO UPDATE SET
                       occurrences = identity_values.occurrences + excluded.occurrences''',
                (target_id, tuple_key, fields, occurrences, source_url, source_line)
            )
        self._connection.execute(
            'DELETE FROM identity_values WHERE counter_id = ?', (source_id,)
        )
        self._counter_ids.pop(id(source), None)
        self._connection.commit()

        return target_id

    def clear(self, counter: 'IdentityCounter') -> None:
        key = id(counter)
        counter_id = self._counter_ids.pop(key, None)
        if counter_id is not None:
            self._connection.execute(
                'DELETE FROM identity_values WHERE counter_id = ?', (counter_id,)
            )

    def items(self, counter: 'IdentityCounter') \
            -> list[tuple[IdentityCounterType, int]]:
        counter_id = self.counter_id(counter)
        rows = self._connection.execute(
            '''SELECT fields, occurrences FROM identity_values
               WHERE counter_id = ? ORDER BY rowid''',
            (counter_id,)
        ).fetchall()
        return [(pickle.loads(fields), int(occurrences))
                for fields, occurrences in rows]

    def missing(self, keyref: 'KeyrefCounter', key: 'IdentityCounter') \
            -> Iterator[tuple[IdentityCounterType, int]]:
        keyref_id = self.counter_id(keyref)
        key_id = self.counter_id(key)
        rows = self._connection.execute(
            '''SELECT r.fields, r.occurrences FROM identity_values AS r
               WHERE r.counter_id = ? AND NOT EXISTS (
                   SELECT 1 FROM identity_values AS k
                   WHERE k.counter_id = ? AND k.tuple_key = r.tuple_key
               ) ORDER BY r.rowid''',
            (keyref_id, key_id)
        )
        for fields, occurrences in rows:
            yield pickle.loads(fields), int(occurrences)

    def missing_values(self, keyref: 'KeyrefCounter',
                       values: Counter[IdentityCounterType]) \
            -> Iterator[tuple[IdentityCounterType, int]]:
        keyref_id = self.counter_id(keyref)
        key_id = 0
        rows = []
        for fields, occurrences in values.items():
            fields = identity_fields_tuple(fields)
            rows.append((
                key_id,
                sqlite3.Binary(encode_identity_key(fields)),
                sqlite3.Binary(pickle.dumps(fields, protocol=pickle.HIGHEST_PROTOCOL)),
                occurrences,
                None,
                None,
            ))
        self._connection.executemany(
            '''INSERT INTO identity_values
               (counter_id, tuple_key, fields, occurrences, source_url, source_line)
               VALUES (?, ?, ?, ?, ?, ?)''',
            rows
        )
        try:
            result = self._connection.execute(
                '''SELECT r.fields, r.occurrences FROM identity_values AS r
                   WHERE r.counter_id = ? AND NOT EXISTS (
                       SELECT 1 FROM identity_values AS k
                       WHERE k.counter_id = ? AND k.tuple_key = r.tuple_key
                   ) ORDER BY r.rowid''',
                (keyref_id, key_id)
            ).fetchall()
        finally:
            self._connection.execute(
                'DELETE FROM identity_values WHERE counter_id = ?', (key_id,)
            )
            self._connection.commit()
        for fields, occurrences in result:
            yield pickle.loads(fields), int(occurrences)

    def contains(self, counter: 'IdentityCounter', fields: IdentityCounterType) -> bool:
        fields = identity_fields_tuple(fields)
        counter_id = self.counter_id(counter)
        for candidate in (fields, fields[0]) if len(fields) == 1 else (fields,):
            candidate = identity_fields_tuple(candidate)
            row = self._connection.execute(
                '''SELECT 1 FROM identity_values WHERE counter_id = ? AND tuple_key = ?''',
                (counter_id, sqlite3.Binary(encode_identity_key(candidate)))
            ).fetchone()
            if row is not None:
                return True
        return False

    def close(self) -> None:
        connection = self._connection
        self._connection = None  # type: ignore[assignment]
        if connection is not None:
            connection.close()
        temporary_dir = self._temporary_dir
        self._temporary_dir = None  # type: ignore[assignment]
        if temporary_dir is not None:
            temporary_dir.cleanup()

    def __enter__(self) -> 'SQLiteIdentityIndex':
        return self

    def __exit__(self, exc_type: Optional[type[BaseException]],
                 exc_value: Optional[BaseException],
                 traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class IdentityCounter:
    elements: Optional[set[Any]]  # don't need to check, should be only etree elements anyway

    __slots__ = ('elements', '_counter', 'identity', 'elem', 'enabled',
                 '_index', '_index_id', '_index_threshold')

    def __init__(self, identity: XsdIdentity, elem: ElementType,
                 index: Optional[SQLiteIdentityIndex] = None,
                 index_threshold: Optional[int] = None) -> None:
        self.counter: Counter[IdentityCounterType] = Counter[IdentityCounterType]()
        self.identity = identity
        self.elem = elem
        self.enabled = True
        self.elements = None
        self._index = index
        self._index_id: Optional[int] = None
        self._index_threshold = index_threshold

    @property
    def counter(self) -> Counter[IdentityCounterType]:
        if self._index is None or self._index_id is None:
            return self._counter
        return Counter(dict(self._index.items(self)))

    @counter.setter
    def counter(self, value: Counter[IdentityCounterType]) -> None:
        self._counter = value

    @property
    def on_disk(self) -> bool:
        return self._index is not None and self._index_id is not None

    def _switch_to_disk(self, fields: IdentityCounterType) -> None:
        assert self._index is not None
        values = self._counter
        self._index_id = self._index.counter_id(self)
        self._index.migrate(self, values, exclude=fields)
        self._counter.clear()

    def merge(self, other: 'IdentityCounter') -> None:
        if self.on_disk or other.on_disk:
            index = self._index or other._index
            assert index is not None
            self._index = other._index = index
            self._index_threshold = other._index_threshold
            target_id = index.merge(self, other)
            self._index_id = target_id
            other._index_id = None
            self._counter.clear()
            other._counter.clear()
        else:
            self._counter.update(other._counter)

    def _counter_or_items(self):
        if self.on_disk:
            assert self._index is not None
            return self._index.items(self)
        return self._counter.items()

    def __repr__(self) -> str:
        return "%s%r" % (self.__class__.__name__[:-7], self.counter)

    def reset(self, elem: ElementType) -> None:
        if self.on_disk:
            assert self._index is not None
            self._index.clear(self)
            self._index_id = None
            if self._index_threshold is None:
                self._index = None
        self._counter.clear()
        self.elem = elem
        self.enabled = True
        self.elements = None

    def _increment(self, fields: IdentityCounterType,
                   source: IdentitySourceType) -> int:
        fields = identity_fields_tuple(fields)

        if self.on_disk:
            assert self._index is not None
            return self._index.increase(self, fields, source)

        self._counter[fields] += 1
        occurrences = self._counter[fields]
        if self._index is None or self._index_threshold is None:
            return occurrences

        if len(self._counter) <= self._index_threshold:
            return occurrences

        self._switch_to_disk(fields)
        return self._index.increase(self, fields, source)

    def increase(self, fields: IdentityCounterType,
                 source: IdentitySourceType = None) -> None:
        occurrences = self._increment(fields, source)
        if occurrences == 2:
            msg = _("duplicated value {0!r} for {1!r}")
            raise XMLSchemaValueError(msg.format(fields, self.identity))


class KeyrefCounter(IdentityCounter):
    identity: XsdKeyref

    def __init__(self, identity: XsdIdentity, elem: ElementType,
                 index: Optional[SQLiteIdentityIndex] = None,
                 index_threshold: Optional[int] = None) -> None:
        super().__init__(identity, elem, index, index_threshold)
        if isinstance(self.identity.refer, (XsdKey, XsdUnique)):
            self.refer = self.identity.refer

    def increase(self, fields: IdentityCounterType,
                 source: IdentitySourceType = None) -> None:
        self._increment(fields, source)

    def iter_errors(self, identities: dict[XsdIdentity, IdentityCounter]) \
            -> Iterator[XMLSchemaValueError]:
        if self.refer is None:
            return  # don't validate with an unbuilt keyref

        refer_counter = identities[self.refer]

        if self.on_disk and refer_counter.on_disk:
            assert self._index is not None and self._index is refer_counter._index
            missing = self._index.missing(self, refer_counter)
        elif self.on_disk:
            assert self._index is not None
            missing = self._index.missing_values(self, refer_counter._counter)
        elif refer_counter.on_disk:
            assert refer_counter._index is not None
            missing = (
                (value, occurrences)
                for value, occurrences in self._counter.items()
                if not refer_counter._index.contains(refer_counter, value)
                and not (len(value) == 1
                         and refer_counter._index.contains(refer_counter, value[0]))
            )
        else:
            refer_values = refer_counter._counter
            missing = (
                (value, occurrences)
                for value, occurrences in self._counter_or_items()
                if value not in refer_values
                and not (len(value) == 1 and value[0] in refer_values)
            )

        for value, occurrences in missing:
            if occurrences > 1:
                msg = "value {} not found for {!r} ({} times)"
                yield XMLSchemaValueError(msg.format(value, self.refer, occurrences))
            else:
                msg = "value {} not found for {!r}"
                yield XMLSchemaValueError(msg.format(value, self.identity.refer))


class FieldValueSelector:

    __slots__ = ('field', 'xsd_element', 'xpath_proxy', 'value_constraints',
                 'token', 'decoders', 'skip_wildcard')

    def __init__(self, field: XsdFieldSelector, xsd_element: 'XsdElement') -> None:
        if field.token is None:
            msg = f"identity field {field} is not built"
            raise XMLSchemaNotBuiltError(field, msg)

        self.skip_wildcard = False
        self.field = field
        self.xsd_element = xsd_element
        self.value_constraints = {}

        self.xpath_proxy = XMLSchemaProxy(xsd_element.schema, xsd_element)
        self.token = copy.deepcopy(field.token)
        self.decoders = []

        for node in self.token.select(self.xpath_proxy.get_context()):
            if not isinstance(node, (AttributeNode, ElementNode)):
                raise XMLSchemaTypeError(
                    "xs:field path must select only attributes and elements"
                )

            comp = cast(FieldDecoderType, node.obj)
            self.decoders.append(comp)
            if isinstance(comp, XsdWildcard):
                if comp.process_contents == 'skip':
                    self.skip_wildcard = True
            else:
                value_constraint = comp.value_constraint
                if value_constraint is not None:
                    self.value_constraints[node.name] = comp.type.text_decode(value_constraint)
                    if isinstance(comp, XsdAttribute):
                        self.value_constraints[None] = self.value_constraints[node.name]

        if len(self.decoders) > 1 and None in self.value_constraints:
            self.value_constraints.pop(None)

    def get_value(self, element_node: EtreeElementNode,
                  namespaces: Optional[NsmapType] = None) -> IdentityFieldItemType:
        """
        Get field value from an element node for a schema or instance context element.

        :param element_node: a no Element
        :param namespaces: is an optional mapping from namespace prefix to URI.
        """
        value: Union[AtomicValueType, list[Optional[AtomicValueType]], None] = None
        element_node.schema = None  # type: ignore[assignment]
        context = XPathContext(
            element_node,
            namespaces=namespaces,
            schema=self.xpath_proxy,
        )

        empty = True
        for node in cast(Iterator[IdentityNodeType], self.token.select(context)):
            if empty:
                empty = False
            else:
                msg = _("%r field selects multiple values!")
                raise XMLSchemaValueError(msg % self.field)

            try:
                xsd_type = cast(Optional[BaseXsdType], node.xsd_type)
            except AttributeError:
                msg = _("%r field selects a %r!")
                raise XMLSchemaTypeError(msg % (self.field, type(node)))

            if xsd_type is None:
                if self.skip_wildcard:
                    value = None
                else:
                    value = node.string_value
            elif xsd_type.content_type_label not in ('simple', 'mixed'):
                msg = _("%r field doesn't have a simple type!")
                raise XMLSchemaTypeError(msg % self.field)
            elif xsd_type.is_qname():
                value = get_extended_qname(node.string_value.strip(), namespaces)
            elif xsd_type.is_boolean():
                # Workarounds for discovered issues with XPath processors
                value = xsd_type.text_decode(node.string_value.strip())
            else:
                try:
                    value = node.typed_value  # type: ignore[assignment,unused-ignore]
                except (KeyError, ValueError):
                    for decoder in self.decoders:
                        if not isinstance(decoder, XsdWildcard):
                            if decoder.is_matching(node.name):
                                value = decoder.type.text_decode(node.string_value)
                                break
                    else:
                        value = node.string_value

            if value is None:
                value = self.value_constraints.get(node.name)
        else:
            if empty:
                value = self.value_constraints.get(None)

        match value:
            case None:
                if not isinstance(self.field.parent, XsdKey) or \
                        'ref' in element_node.obj.attrib and \
                        self.field.schema.meta_schema is None and \
                        self.field.schema.XSD_VERSION != '1.0':
                    return None
                else:
                    msg = _("missing key field {0!r} for {1!r}")
                    raise XMLSchemaValueError(msg.format(self.field.path, self))
            case list():
                return tuple(value)
            case UntypedAtomic():
                return str(value)
            case bool():
                return value, bool
            case float():
                if math.isnan(value):
                    return 'nan', float
                else:
                    return value, float
            case _:
                return value
