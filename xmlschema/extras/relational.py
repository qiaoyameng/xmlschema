#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
A scoped relational mapper from XSD-validated XML documents to SQLite.

The mapper first builds an explicit *mapping plan* from an XSD schema and
then imports XML documents into a SQLite database (standard library
`sqlite3`) according to that plan. A reference entry point for rebuilding
an XML document from the mapped relational data is also provided.

Supported in this first version:

* ``xs:sequence``, ``xs:choice`` and ``xs:all`` model groups;
* attributes and simple content (text) of complex types;
* nested repeating elements, mapped to child tables up to a maximum depth;
* stable internal identities: every imported document gets a row in the
  ``xml_documents`` table and every node row gets an integer primary key,
  a ``seq`` column with the position between same-name siblings and a
  ``branch`` column recording the actual ``xs:choice`` branch taken;
* a five-way value state (``value``/``empty``/``nil``/``default``/``missing``)
  so that empty elements, absent values, ``xsi:nil`` and XSD defaults do
  not collapse into a single SQL ``NULL``.

Mixed content, wildcards (``xs:any``/``xs:anyAttribute``) and simple types
that cannot be mapped faithfully (e.g. ``xs:QName``) are reported as plan
notes at planning time instead of being silently dropped.

Each document is imported inside a single transaction: a failure rolls the
document back entirely, leaving no partial rows.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from xml.etree import ElementTree

import xmlschema
from xmlschema.names import XSI_NIL
from xmlschema.validators import XsdAnyAttribute, XsdAnyElement, XsdComplexType, \
    XsdElement, XsdGroup, XsdSimpleType

__all__ = ('MappingPlan', 'PlanNote', 'ValueColumn', 'TablePlan',
           'create_mapping_plan', 'VALUE_STATES')

#: The possible states of a mapped value. SQL NULL alone would conflate
#: empty elements, absent values, xsi:nil and schema defaults.
VALUE_STATES = ('value', 'empty', 'nil', 'default', 'missing')

DOCUMENTS_TABLE = 'xml_documents'

_NAME_PATTERN = re.compile(r'\W')
_STATES_CHECK = "IN ('value', 'empty', 'nil', 'default', 'missing')"

XmlSource = Union[str, bytes, Path, ElementTree.Element, ElementTree.ElementTree, Any]


def _local_name(qname: Optional[str]) -> str:
    if not qname:
        return ''
    return qname.split('}')[-1] if '}' in qname else qname


def _safe_name(name: str) -> str:
    return _NAME_PATTERN.sub('_', name) or 'unnamed'


def _to_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _parse_xml_source(source: XmlSource) -> ElementTree.Element:
    if isinstance(source, ElementTree.Element):
        return source
    if isinstance(source, ElementTree.ElementTree):
        return source.getroot()
    if hasattr(source, 'read'):
        return ElementTree.parse(source).getroot()
    if isinstance(source, bytes):
        return ElementTree.fromstring(source)
    if isinstance(source, Path):
        return ElementTree.parse(str(source)).getroot()
    if isinstance(source, str):
        if source.lstrip().startswith('<'):
            return ElementTree.fromstring(source)
        return ElementTree.parse(source).getroot()
    raise TypeError(f'unsupported XML source type: {type(source)!r}')


def _select_rows(conn: sqlite3.Connection, table: str, clause: str = '',
                 params: tuple = ()) -> List[Dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}" {clause}', params)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


@dataclass
class PlanNote:
    """A note about a schema construct that the mapping cannot fully cover."""
    context: str
    message: str

    def __str__(self) -> str:
        return f'{self.context}: {self.message}'


@dataclass
class ValueColumn:
    """
    Maps a simple value (an attribute, a simple-typed child element or the
    text content of an element) to a value/state column pair of a SQL table.
    """
    name: str
    state_name: str
    source: str             # 'attribute', 'element' or 'content'
    qname: str              # expanded QName to match in documents, '' for text content
    sql_type: str           # SQLite column affinity: TEXT, INTEGER or REAL
    xsd_type: str           # the XSD type name, for reference
    default: Optional[str] = None
    choice: Optional[str] = None  # id of the enclosing xs:choice group, if any


@dataclass
class TablePlan:
    """Maps an XSD element occurrence to a SQL table."""
    name: str
    path: str
    element: XsdElement
    parent: Optional['TablePlan'] = None
    choice: Optional[str] = None  # id of the enclosing xs:choice group, if any
    columns: List[ValueColumn] = field(default_factory=list)
    content: List[Any] = field(default_factory=list)  # ValueColumn | TablePlan, in order
    choices: Dict[str, str] = field(default_factory=dict)  # choice id -> branch column
    _names: Dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def local_name(self) -> str:
        return _local_name(self.element.name)


class MappingPlan:
    """
    An explicit mapping plan between an XSD schema and a SQLite database.

    :param schema: an XMLSchema instance or a schema source accepted by
      `xmlschema.XMLSchema`.
    :param root: the name of the global element to map as document root.
      Required when the schema has more than one global element.
    :param max_depth: maximum nesting depth of element tables. Recursive or
      deeper content is truncated and reported with a plan note.
    """

    documents_table = DOCUMENTS_TABLE

    def __init__(self, schema: Any, root: Union[str, XsdElement, None] = None,
                 max_depth: int = 6) -> None:
        if not isinstance(schema, xmlschema.XMLSchemaBase):
            schema = xmlschema.XMLSchema(schema)
        self.schema = schema
        self.max_depth = max_depth
        self.notes: List[PlanNote] = []
        self.tables: List[TablePlan] = []
        self._table_names: Dict[str, int] = {}
        self._choice_counter = 0

        if root is None:
            if len(schema.elements) != 1:
                raise ValueError(
                    f'schema has {len(schema.elements)} global elements: '
                    f'select the root element passing the *root* argument'
                )
            root_element = next(iter(schema.elements.values()))
        elif isinstance(root, XsdElement):
            root_element = root
        else:
            try:
                root_element = schema.elements[root]
            except KeyError:
                raise ValueError(f'no global element {root!r} in schema') from None

        self.root = self._build_table(root_element, parent=None,
                                      path=_local_name(root_element.name),
                                      depth=0, choice=None)

    def __repr__(self) -> str:
        return (f'{self.__class__.__name__}(root={self.root.name!r}, '
                f'tables={len(self.tables)}, notes={len(self.notes)})')

    def _note(self, context: str, message: str) -> None:
        self.notes.append(PlanNote(context, message))

    def _unique_table_name(self, base: str) -> str:
        base = _safe_name(base)
        count = self._table_names.get(base, 0) + 1
        self._table_names[base] = count
        return base if count == 1 else f'{base}_{count}'

    @staticmethod
    def _unique_column_name(table: TablePlan, base: str) -> str:
        base = _safe_name(base)
        count = table._names.get(base, 0) + 1
        table._names[base] = count
        return base if count == 1 else f'{base}_{count}'

    def _column_affinity(self, xsd_type: Any, path: str) -> str:
        if not isinstance(xsd_type, XsdSimpleType):
            return 'TEXT'

        names = []
        tp: Any = xsd_type
        while isinstance(tp, XsdSimpleType):
            if tp.name:
                names.append(_local_name(tp.name))
            tp = tp.base_type if isinstance(tp.base_type, XsdSimpleType) else None

        if xsd_type.is_list() or xsd_type.is_union():
            kind = 'list' if xsd_type.is_list() else 'union'
            self._note(path, f'{kind} simple type is stored as TEXT '
                             f'(whitespace-separated lexical form)')
            return 'TEXT'
        if names and names[0] in ('anyType', 'anySimpleType'):
            self._note(path, f'unsupported type xs:{names[0]}: '
                             f'the value is stored as TEXT')
            return 'TEXT'
        if 'QName' in names or 'NOTATION' in names:
            self._note(path, f'type xs:{names[0]} cannot be mapped faithfully: '
                             f'stored as TEXT with literal namespace prefixes')
            return 'TEXT'
        if 'integer' in names:
            return 'INTEGER'
        if 'float' in names or 'double' in names:
            return 'REAL'
        return 'TEXT'

    def _build_table(self, xsd_element: XsdElement, parent: Optional[TablePlan],
                     path: str, depth: int, choice: Optional[str]) -> Optional[TablePlan]:
        if depth > self.max_depth:
            self._note(path, f'nesting truncated: content deeper than '
                             f'max_depth={self.max_depth} is not mapped')
            return None

        if parent is None:
            base_name = _local_name(xsd_element.name)
        else:
            base_name = f'{parent.name}__{_local_name(xsd_element.name)}'
        table = TablePlan(
            name=self._unique_table_name(base_name),
            path=path,
            element=xsd_element,
            parent=parent,
            choice=choice,
        )
        self.tables.append(table)

        xsd_type = xsd_element.type
        if isinstance(xsd_type, XsdComplexType):
            if xsd_type.mixed:
                self._note(path, 'mixed content is not supported: text nodes '
                                 'interleaved with child elements are not imported')
            for name, attribute in xsd_type.attributes.items():
                if name is None or isinstance(attribute, XsdAnyAttribute):
                    self._note(path, 'attribute wildcard (xs:anyAttribute) is not '
                                     'mapped to any column')
                    continue
                self._add_attribute_column(table, path, attribute)
            if xsd_type.has_simple_content():
                self._add_content_column(table, path, xsd_element, xsd_type.content)
            else:
                self._build_content(table, xsd_type.content, path, depth,
                                    repeating=False, choice=None)
        else:
            self._add_content_column(table, path, xsd_element, xsd_type)
        return table

    def _build_content(self, table: TablePlan, group: Any, path: str, depth: int,
                       repeating: bool, choice: Optional[str]) -> None:
        if not isinstance(group, XsdGroup) or group.max_occurs == 0:
            return
        if group.model == 'choice':
            self._choice_counter += 1
            choice = f'choice{self._choice_counter}'
            if not repeating and group.max_occurs == 1:
                table.choices[choice] = \
                    self._unique_column_name(table, f'{choice}__branch')
        repeating = repeating or group.max_occurs != 1

        for particle in group:
            if isinstance(particle, XsdAnyElement):
                self._note(path, 'element wildcard (xs:any) is not mapped '
                                 'to any table or column')
            elif isinstance(particle, XsdGroup):
                self._build_content(table, particle, path, depth, repeating, choice)
            elif isinstance(particle, XsdElement):
                self._build_element_particle(table, particle, path, depth,
                                             repeating, choice)

    def _build_element_particle(self, table: TablePlan, particle: XsdElement,
                                path: str, depth: int, repeating: bool,
                                choice: Optional[str]) -> None:
        if particle.abstract or particle.substitution_group:
            self._note(path, f'element {particle.name!r} is abstract or heads a '
                             f'substitution group: only its own occurrences '
                             f'are mapped')

        child_path = f'{path}/{_local_name(particle.name)}'
        if isinstance(particle.type, XsdComplexType) or \
                repeating or particle.max_occurs != 1:
            child = self._build_table(particle, parent=table, path=child_path,
                                      depth=depth + 1, choice=choice)
            if child is not None:
                table.content.append(child)
            return

        name = self._unique_column_name(table, f'e_{_local_name(particle.name)}')
        column = ValueColumn(
            name=name,
            state_name=self._unique_column_name(table, f'{name}__state'),
            source='element',
            qname=particle.name,
            sql_type=self._column_affinity(particle.type, child_path),
            xsd_type=particle.type.name or 'anonymous',
            default=particle.default,
            choice=choice,
        )
        table.columns.append(column)
        table.content.append(column)

    def _add_attribute_column(self, table: TablePlan, path: str,
                              attribute: Any) -> None:
        name = self._unique_column_name(table, f'a_{_local_name(attribute.name)}')
        column = ValueColumn(
            name=name,
            state_name=self._unique_column_name(table, f'{name}__state'),
            source='attribute',
            qname=attribute.name,
            sql_type=self._column_affinity(attribute.type, path),
            xsd_type=attribute.type.name or 'anonymous',
            default=attribute.default,
        )
        table.columns.append(column)

    def _add_content_column(self, table: TablePlan, path: str,
                            xsd_element: XsdElement, xsd_type: Any) -> None:
        name = self._unique_column_name(table, 'value')
        column = ValueColumn(
            name=name,
            state_name=self._unique_column_name(table, f'{name}__state'),
            source='content',
            qname='',
            sql_type=self._column_affinity(xsd_type, path),
            xsd_type=xsd_type.name or 'anonymous',
            default=xsd_element.default,
        )
        table.columns.append(column)
        table.content.append(column)

    def ddl(self) -> str:
        """Returns the SQL statements that create the mapped database schema."""
        statements = [
            f'CREATE TABLE "{self.documents_table}" (\n'
            f'  "doc_id" INTEGER PRIMARY KEY,\n'
            f'  "source" TEXT,\n'
            f'  "imported_at" TEXT NOT NULL\n'
            f')'
        ]
        for table in self.tables:
            columns = [
                '"id" INTEGER PRIMARY KEY',
                f'"doc_id" INTEGER NOT NULL '
                f'REFERENCES "{self.documents_table}" ("doc_id") ON DELETE CASCADE',
            ]
            if table.parent is not None:
                columns.append(
                    f'"parent_id" INTEGER NOT NULL '
                    f'REFERENCES "{table.parent.name}" ("id") ON DELETE CASCADE'
                )
            columns.append('"seq" INTEGER NOT NULL DEFAULT 0')
            columns.append('"branch" TEXT')
            columns.append('"xsi_nil" INTEGER NOT NULL DEFAULT 0 '
                           'CHECK ("xsi_nil" IN (0, 1))')
            for column in table.columns:
                columns.append(f'"{column.name}" {column.sql_type}')
                columns.append(
                    f'"{column.state_name}" TEXT NOT NULL DEFAULT \'missing\' '
                    f'CHECK ("{column.state_name}" {_STATES_CHECK})'
                )
            for branch_column in table.choices.values():
                columns.append(f'"{branch_column}" TEXT')
            statements.append(
                f'CREATE TABLE "{table.name}" (\n  ' + ',\n  '.join(columns) + '\n)'
            )
        return ';\n\n'.join(statements) + ';\n'

    def create_tables(self, conn: sqlite3.Connection, drop: bool = False) -> None:
        """
        Creates the mapped tables on a SQLite connection and enables the
        enforcement of foreign key constraints on it.

        :param conn: a `sqlite3` connection.
        :param drop: if `True` drop pre-existing mapped tables first.
        """
        conn.execute('PRAGMA foreign_keys = ON')
        if drop:
            for table in reversed(self.tables):
                conn.execute(f'DROP TABLE IF EXISTS "{table.name}"')
            conn.execute(f'DROP TABLE IF EXISTS "{self.documents_table}"')
        conn.executescript(self.ddl())

    def import_document(self, conn: sqlite3.Connection, source: XmlSource,
                        source_name: Optional[str] = None,
                        validate: bool = False) -> int:
        """
        Imports an XML document into the mapped tables of a SQLite database.
        The whole document is imported inside a single transaction: if
        anything fails the transaction is rolled back and no partial rows
        are left behind.

        :param conn: a `sqlite3` connection with the mapped tables created.
        :param source: the XML document: a path, a file-like object, a string
          with XML data, or an ElementTree Element/ElementTree.
        :param source_name: an optional name recorded in the documents table.
        :param validate: if `True` the document is validated against the
          schema before importing.
        :return: the internal identity (`doc_id`) of the imported document.
        """
        root_element = _parse_xml_source(source)
        if validate:
            self.schema.validate(root_element)
        if root_element.tag != self.root.element.name:
            raise ValueError(
                f'root element {root_element.tag!r} does not match '
                f'the planned root {self.root.element.name!r}'
            )
        if source_name is None and isinstance(source, (str, Path)) and \
                not str(source).lstrip().startswith('<'):
            source_name = str(source)

        imported_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with conn:
            cursor = conn.execute(
                f'INSERT INTO "{self.documents_table}" ("source", "imported_at") '
                f'VALUES (?, ?)',
                (source_name, imported_at),
            )
            doc_id = cursor.lastrowid
            self._insert_row(conn, self.root, root_element, doc_id,
                             parent_id=None, seq=0)
        return doc_id

    def _insert_row(self, conn: sqlite3.Connection, table: TablePlan,
                    elem: ElementTree.Element, doc_id: int,
                    parent_id: Optional[int], seq: int) -> int:
        nil = elem.get(XSI_NIL) in ('true', '1')
        names = ['doc_id', 'seq', 'branch', 'xsi_nil']
        values: List[Any] = [doc_id, seq,
                             table.local_name if table.choice else None, int(nil)]
        if table.parent is not None:
            names.append('parent_id')
            values.append(parent_id)
        for column in table.columns:
            value, state = self._extract_value(column, elem, nil)
            names.extend((column.name, column.state_name))
            values.extend((value, state))
        for choice_id, branch_column in table.choices.items():
            names.append(branch_column)
            values.append(self._choice_branch(table, choice_id, elem))

        columns = ', '.join(f'"{name}"' for name in names)
        placeholders = ', '.join('?' for _ in names)
        cursor = conn.execute(
            f'INSERT INTO "{table.name}" ({columns}) VALUES ({placeholders})',
            values,
        )
        row_id = cursor.lastrowid

        if not nil:
            for item in table.content:
                if isinstance(item, TablePlan):
                    child_seq = 0
                    for child in elem:
                        if child.tag == item.element.name:
                            self._insert_row(conn, item, child, doc_id,
                                             parent_id=row_id, seq=child_seq)
                            child_seq += 1
        return row_id

    @staticmethod
    def _extract_value(column: ValueColumn, elem: ElementTree.Element,
                       nil: bool) -> Tuple[Any, str]:
        if column.source == 'attribute':
            value = elem.get(column.qname)
            if value is None:
                if column.default is not None:
                    return column.default, 'default'
                return None, 'missing'
            return value, 'value'

        if column.source == 'element':
            child = None
            for item in elem:
                if item.tag == column.qname:
                    child = item
                    break
            if child is None:
                if column.default is not None:
                    return column.default, 'default'
                return None, 'missing'
            if child.get(XSI_NIL) in ('true', '1'):
                return None, 'nil'
            text = child.text
        else:
            if nil:
                return None, 'nil'
            text = elem.text

        if text is None or text == '':
            if column.default is not None:
                return column.default, 'default'
            return None, 'empty'
        return text, 'value'

    @staticmethod
    def _choice_branch(table: TablePlan, choice_id: str,
                       elem: ElementTree.Element) -> Optional[str]:
        members = {column.qname for column in table.columns
                   if column.choice == choice_id}
        members.update(item.element.name for item in table.content
                       if isinstance(item, TablePlan) and item.choice == choice_id)
        for child in elem:
            if child.tag in members:
                return _local_name(child.tag)
        return None

    def rebuild_document(self, conn: sqlite3.Connection,
                         doc_id: int) -> ElementTree.Element:
        """
        Rebuilds the XML document with the given identity from the mapped
        relational data. This is the reference entry point for the reverse
        mapping: values recorded with state 'default' or 'missing' are
        omitted (defaults are applied again at the next import), values
        recorded as 'empty' or 'nil' are rebuilt as empty or
        ``xsi:nil`` elements.

        :param conn: a `sqlite3` connection with imported documents.
        :param doc_id: the internal identity of the document to rebuild.
        :return: the root element of the rebuilt document.
        """
        rows = _select_rows(conn, self.root.name, 'WHERE "doc_id" = ?', (doc_id,))
        if not rows:
            raise ValueError(f'no imported document with doc_id={doc_id!r}')
        if len(rows) > 1:
            raise ValueError(f'multiple root rows for doc_id={doc_id!r}')
        return self._build_element(conn, self.root, rows[0])

    def _build_element(self, conn: sqlite3.Connection, table: TablePlan,
                       row: Dict[str, Any]) -> ElementTree.Element:
        elem = ElementTree.Element(table.element.name)
        if row['xsi_nil']:
            elem.set(XSI_NIL, 'true')
            return elem

        for column in table.columns:
            if column.source == 'attribute' and \
                    row[column.state_name] == 'value':
                elem.set(column.qname, _to_text(row[column.name]))

        for item in table.content:
            if isinstance(item, ValueColumn):
                state = row[item.state_name]
                if item.source == 'content':
                    if state == 'value':
                        elem.text = _to_text(row[item.name])
                elif state == 'value':
                    child = ElementTree.SubElement(elem, item.qname)
                    child.text = _to_text(row[item.name])
                elif state == 'empty':
                    ElementTree.SubElement(elem, item.qname)
                elif state == 'nil':
                    child = ElementTree.SubElement(elem, item.qname)
                    child.set(XSI_NIL, 'true')
            else:
                child_rows = _select_rows(conn, item.name,
                                          'WHERE "parent_id" = ? ORDER BY "seq"',
                                          (row['id'],))
                for child_row in child_rows:
                    elem.append(self._build_element(conn, item, child_row))
        return elem


def create_mapping_plan(schema: Any, root: Union[str, XsdElement, None] = None,
                        max_depth: int = 6) -> MappingPlan:
    """
    Creates a mapping plan between an XSD schema and a SQLite database.
    Convenience factory equivalent to ``MappingPlan(schema, root, max_depth)``.
    """
    return MappingPlan(schema, root=root, max_depth=max_depth)
