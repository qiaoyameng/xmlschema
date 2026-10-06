#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""Tests for the XSD -> SQLite relational mapper (xmlschema.extras.relational)."""

import sqlite3
import unittest
from xml.etree import ElementTree

import xmlschema
from xmlschema.extras.relational import create_mapping_plan

SCHEMA_SOURCE = '''<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="order">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="customer">
          <xs:complexType>
            <xs:simpleContent>
              <xs:extension base="xs:string">
                <xs:attribute name="name" type="xs:string" use="required"/>
              </xs:extension>
            </xs:simpleContent>
          </xs:complexType>
        </xs:element>
        <xs:element name="item" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="qty" type="xs:int"/>
              <xs:element name="note" type="xs:string" minOccurs="0" nillable="true"/>
              <xs:choice>
                <xs:element name="price" type="xs:decimal"/>
                <xs:element name="free" type="xs:boolean"/>
              </xs:choice>
            </xs:sequence>
            <xs:attribute name="sku" type="xs:string" use="required"/>
            <xs:attribute name="kind" type="xs:string" default="std"/>
          </xs:complexType>
        </xs:element>
        <xs:element name="comment" type="xs:string" minOccurs="0" default="none"/>
        <xs:element name="tag" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:int" use="required"/>
      <xs:attribute name="code" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>'''

XML_DOCUMENT = (
    '<order id="7" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<customer name="Acme">Alice</customer>'
    '<item sku="A1"><qty>2</qty><note xsi:nil="true"/><price>9.99</price></item>'
    '<item sku="B2" kind="gift"><qty>1</qty><note></note><free>true</free></item>'
    '<tag>x</tag><tag>y</tag>'
    '</order>'
)

UNSUPPORTED_SCHEMA = '''<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="nodeType" mixed="true">
    <xs:sequence>
      <xs:element name="label" type="xs:QName"/>
      <xs:element name="child" type="nodeType" minOccurs="0" maxOccurs="unbounded"/>
      <xs:any namespace="##other" minOccurs="0"/>
    </xs:sequence>
    <xs:anyAttribute/>
  </xs:complexType>
  <xs:element name="node" type="nodeType"/>
</xs:schema>'''

NAMESPACED_SCHEMA = '''<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  targetNamespace="http://example.com/rel" xmlns="http://example.com/rel"
  elementFormDefault="qualified">
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="entry" type="xs:int" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="version" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>'''

NAMESPACED_DOCUMENT = (
    '<doc xmlns="http://example.com/rel" version="1">'
    '<entry>1</entry><entry>2</entry></doc>'
)


def normalize(elem):
    """Canonical nested-tuple form for comparing documents regardless of prefixes."""
    return (
        elem.tag,
        tuple(sorted(elem.attrib.items())),
        elem.text or '',
        tuple(normalize(child) for child in elem),
    )


class TestMappingPlan(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = xmlschema.XMLSchema(SCHEMA_SOURCE)

    def setUp(self):
        self.plan = create_mapping_plan(self.schema)
        self.conn = sqlite3.connect(':memory:')
        self.plan.create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def query(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def import_doc(self):
        return self.plan.import_document(self.conn, XML_DOCUMENT)

    def test_document_is_valid(self):
        self.assertTrue(self.schema.is_valid(XML_DOCUMENT))

    def test_plan_tables_and_columns(self):
        self.assertEqual(self.plan.notes, [])
        self.assertEqual([t.name for t in self.plan.tables],
                         ['order', 'order__customer',
                          'order__item', 'order__tag'])
        item = self.plan.tables[2]
        self.assertEqual([c.name for c in item.columns],
                         ['a_sku', 'a_kind', 'e_qty',
                          'e_note', 'e_price', 'e_free'])
        self.assertEqual(item.choices, {'choice1': 'choice1__branch'})
        tag = self.plan.tables[3]
        self.assertEqual([c.name for c in tag.columns], ['value'])

    def test_ddl_constraints(self):
        ddl = self.plan.ddl()
        self.assertIn('REFERENCES "xml_documents" ("doc_id") ON DELETE CASCADE', ddl)
        self.assertIn('REFERENCES "order" ("id") ON DELETE CASCADE', ddl)
        self.assertIn('CHECK ("xsi_nil" IN (0, 1))', ddl)
        self.assertIn('INTEGER', ddl)

    def test_create_tables_drop(self):
        self.import_doc()
        self.plan.create_tables(self.conn, drop=True)
        self.assertEqual(self.query('SELECT * FROM "xml_documents"'), [])

    def test_import_value_states(self):
        doc_id = self.import_doc()
        docs = self.query('SELECT * FROM "xml_documents"')
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['doc_id'], doc_id)

        order = self.query('SELECT * FROM "order"')[0]
        self.assertEqual(order['a_id'], 7)
        self.assertIsInstance(order['a_id'], int)
        self.assertEqual(order['a_id__state'], 'value')
        self.assertIsNone(order['a_code'])
        self.assertEqual(order['a_code__state'], 'missing')
        self.assertEqual(order['e_comment'], 'none')
        self.assertEqual(order['e_comment__state'], 'default')

        customer = self.query('SELECT * FROM "order__customer"')[0]
        self.assertEqual(customer['parent_id'], order['id'])
        self.assertEqual(customer['doc_id'], doc_id)
        self.assertEqual(customer['value'], 'Alice')
        self.assertEqual(customer['a_name'], 'Acme')

        items = self.query('SELECT * FROM "order__item" ORDER BY "seq"')
        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first['seq'], 0)
        self.assertEqual(first['e_qty'], 2)
        self.assertIsInstance(first['e_qty'], int)
        self.assertIsNone(first['e_note'])
        self.assertEqual(first['e_note__state'], 'nil')
        self.assertEqual(first['a_kind'], 'std')
        self.assertEqual(first['a_kind__state'], 'default')
        self.assertEqual(second['seq'], 1)
        self.assertIsNone(second['e_note'])
        self.assertEqual(second['e_note__state'], 'empty')
        self.assertEqual(second['a_kind'], 'gift')
        self.assertEqual(second['a_kind__state'], 'value')

        # the four non-value states are all distinct in the database
        states = {row['e_note__state'] for row in items}
        self.assertEqual(states, {'nil', 'empty'})
        self.assertNotEqual(order['a_code__state'],
                            order['e_comment__state'])

    def test_choice_branch_and_sibling_order(self):
        self.import_doc()
        items = self.query('SELECT * FROM "order__item" ORDER BY "seq"')
        self.assertEqual([row['choice1__branch'] for row in items],
                         ['price', 'free'])
        self.assertEqual(items[0]['e_price'], '9.99')
        self.assertEqual(items[1]['e_free'], 'true')
        tags = self.query('SELECT * FROM "order__tag" ORDER BY "seq"')
        self.assertEqual([row['value'] for row in tags], ['x', 'y'])
        self.assertEqual([row['seq'] for row in tags], [0, 1])

    def test_foreign_keys(self):
        self.import_doc()
        self.assertEqual(
            self.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.conn.execute('DELETE FROM "xml_documents"')
        for table in self.plan.tables:
            self.assertEqual(self.query(f'SELECT * FROM "{table.name}"'), [])

    def test_round_trip(self):
        doc_id = self.import_doc()
        rebuilt = self.plan.rebuild_document(self.conn, doc_id)
        original = ElementTree.fromstring(XML_DOCUMENT)
        self.assertEqual(normalize(rebuilt), normalize(original))

        # fixed point: re-importing the rebuilt document rebuilds identically
        doc_id2 = self.plan.import_document(
            self.conn, ElementTree.tostring(rebuilt))
        self.assertNotEqual(doc_id, doc_id2)
        rebuilt2 = self.plan.rebuild_document(self.conn, doc_id2)
        self.assertEqual(normalize(rebuilt2), normalize(rebuilt))

    def test_transaction_rollback(self):
        with self.assertRaises(ElementTree.ParseError):
            self.plan.import_document(self.conn, '<order id="1"><customer>')
        with self.assertRaises(ValueError):
            self.plan.import_document(self.conn, '<unexpected/>')
        with self.assertRaises(xmlschema.XMLSchemaException):
            self.plan.import_document(
                self.conn, '<order><customer name="A">x</customer></order>',
                validate=True)
        self.assertEqual(self.query('SELECT * FROM "xml_documents"'), [])
        self.assertEqual(self.query('SELECT * FROM "order"'), [])

    def test_rebuild_unknown_document(self):
        with self.assertRaises(ValueError):
            self.plan.rebuild_document(self.conn, 999)

    def test_plan_notes_for_unsupported_constructs(self):
        schema = xmlschema.XMLSchema(UNSUPPORTED_SCHEMA)
        plan = create_mapping_plan(schema, max_depth=3)
        notes = '\n'.join(str(note) for note in plan.notes)
        self.assertIn('mixed content', notes)
        self.assertIn('xs:any)', notes)
        self.assertIn('xs:anyAttribute', notes)
        self.assertIn('QName', notes)
        self.assertIn('truncated', notes)

        # fields are still mapped, not silently dropped
        names = [c.name for table in plan.tables for c in table.columns]
        self.assertIn('e_label', names)

    def test_namespaced_round_trip(self):
        plan = create_mapping_plan(xmlschema.XMLSchema(NAMESPACED_SCHEMA))
        conn = sqlite3.connect(':memory:')
        try:
            plan.create_tables(conn)
            doc_id = plan.import_document(conn, NAMESPACED_DOCUMENT)
            rebuilt = plan.rebuild_document(conn, doc_id)
            original = ElementTree.fromstring(NAMESPACED_DOCUMENT)
            self.assertEqual(normalize(rebuilt), normalize(original))
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
