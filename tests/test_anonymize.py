#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""Tests for the xmlschema.extras.anonymize module."""
import json
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import xmlschema
from xmlschema.extras.anonymize import (
    AnonymizationError, UnknownConstraintError, ReplacementCollisionError,
    UnresolvableReferenceError, ValueGenerationError, anonymize_document, main,
)


MAIN_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="company">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="department" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="employee" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="name" type="xs:string"/>
                  </xs:sequence>
                  <xs:attribute name="empId" type="xs:string"/>
                </xs:complexType>
              </xs:element>
              <xs:element name="assignment" minOccurs="0" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:attribute name="empRef" type="xs:string"/>
                  <xs:attribute name="project" type="xs:string"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
            <xs:attribute name="deptId" type="xs:string"/>
          </xs:complexType>
          <xs:key name="empKey">
            <xs:selector xpath="employee"/>
            <xs:field xpath="@empId"/>
          </xs:key>
          <xs:keyref name="empRef" refer="empKey">
            <xs:selector xpath="assignment"/>
            <xs:field xpath="@empRef"/>
          </xs:keyref>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

# Two scopes (departments) containing the same textual ID "E100".
MAIN_DOC = """<company>
  <department deptId="D1">
    <employee empId="E100"><name>Alice</name></employee>
    <employee empId="E200"><name>Carol</name></employee>
    <assignment empRef="E100" project="P1"/>
    <assignment empRef="E200" project="P2"/>
  </department>
  <department deptId="D2">
    <employee empId="E100"><name>Bob</name></employee>
    <employee empId="E300"><name>Dan</name></employee>
    <assignment empRef="E100" project="P3"/>
  </department>
</company>
"""

FACET_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="company">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="employee" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="empId">
              <xs:simpleType>
                <xs:restriction base="xs:string">
                  <xs:pattern value="[A-Z][0-9]{2}"/>
                </xs:restriction>
              </xs:simpleType>
            </xs:attribute>
          </xs:complexType>
        </xs:element>
        <xs:element name="assignment" minOccurs="0" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="empRef" type="xs:string"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="empKey">
      <xs:selector xpath="employee"/>
      <xs:field xpath="@empId"/>
    </xs:key>
    <xs:keyref name="empRef" refer="empKey">
      <xs:selector xpath="assignment"/>
      <xs:field xpath="@empRef"/>
    </xs:keyref>
  </xs:element>
</xs:schema>
"""

FACET_DOC = """<company>
  <employee empId="A12"/>
  <employee empId="B34"/>
  <assignment empRef="A12"/>
</company>
"""

COMPOSITE_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="company">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="employee" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="empId" type="xs:string"/>
            <xs:attribute name="office" type="xs:string"/>
          </xs:complexType>
        </xs:element>
        <xs:element name="assignment" minOccurs="0" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="refId" type="xs:string"/>
            <xs:attribute name="refOffice" type="xs:string"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="empKey">
      <xs:selector xpath="employee"/>
      <xs:field xpath="@empId"/>
      <xs:field xpath="@office"/>
    </xs:key>
    <xs:keyref name="empRef" refer="empKey">
      <xs:selector xpath="assignment"/>
      <xs:field xpath="@refId"/>
      <xs:field xpath="@refOffice"/>
    </xs:keyref>
  </xs:element>
</xs:schema>
"""

COMPOSITE_DOC = """<company>
  <employee empId="E1" office="US"/>
  <employee empId="E1" office="EU"/>
  <assignment refId="E1" refOffice="US"/>
  <assignment refId="E1" refOffice="EU"/>
</company>
"""

NS_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns="http://example.com/company" targetNamespace="http://example.com/company"
    elementFormDefault="qualified">
  <xs:element name="company">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="employee" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="empId" type="xs:string"/>
          </xs:complexType>
        </xs:element>
        <xs:element name="assignment" minOccurs="0" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="empRef" type="xs:string"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="empKey">
      <xs:selector xpath="company:employee" xmlns:company="http://example.com/company"/>
      <xs:field xpath="@empId"/>
    </xs:key>
    <xs:keyref name="empRef" refer="empKey">
      <xs:selector xpath="company:assignment" xmlns:company="http://example.com/company"/>
      <xs:field xpath="@empRef"/>
    </xs:keyref>
  </xs:element>
</xs:schema>
"""

NS_DOC = """<company xmlns="http://example.com/company">
  <employee empId="E100"/>
  <assignment empRef="E100"/>
</company>
"""


def employees_by_dept(document):
    root = ElementTree.fromstring(document)
    return [
        (
            [emp.get('empId') for emp in dept.iter('employee')],
            [ass.get('empRef') for ass in dept.iter('assignment')],
        )
        for dept in root.iter('department')
    ]


class TestAnonymizeDocument(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = xmlschema.XMLSchema(MAIN_SCHEMA)

    def anonymize(self, doc=MAIN_DOC, **kwargs):
        kwargs.setdefault('seed', 42)
        return anonymize_document(self.schema, doc, ['empKey'], **kwargs)

    def test_scopes_do_not_share_mappings(self):
        result = self.anonymize()
        self.assertTrue(self.schema.is_valid(
            ElementTree.fromstring(result.document)))
        self.assertNotIn('E100', result.document)
        self.assertNotIn('E200', result.document)

        # Same textual ID in two scopes: same fingerprint, different replacements.
        self.assertEqual(len(result.receipt), 4)
        scopes = {entry.scope for entry in result.receipt}
        self.assertEqual(len(scopes), 2)
        fingerprints = {}
        replacements = set()
        for entry in result.receipt:
            fingerprints.setdefault(entry.fingerprint, set()).add(
                entry.replacement)
            replacements.add(entry.replacement)
        shared = [fp for fp, repls in fingerprints.items() if len(repls) > 1]
        self.assertTrue(shared)  # E100 mapped differently in the two scopes
        self.assertEqual(len(replacements), 4)  # no cross-scope reuse

    def test_references_updated_and_graph_preserved(self):
        before = employees_by_dept(MAIN_DOC)
        result = self.anonymize(include_originals=True)
        after = employees_by_dept(result.document)

        # Same number of departments, keys and references.
        self.assertEqual(len(before), len(after))
        mapping = {}
        for entry in result.receipt:
            mapping.setdefault(entry.scope, {})[entry.original] = entry.replacement

        scopes = sorted(mapping)
        self.assertEqual(len(scopes), 2)
        for (keys, refs), (new_keys, new_refs), scope in zip(
                before, after, scopes):
            # Each reference still points to the same employee (connectivity).
            self.assertEqual(len(keys), len(new_keys))
            self.assertEqual(len(refs), len(new_refs))
            for old_key, new_key in zip(keys, new_keys):
                self.assertEqual(mapping[scope][(old_key,)], (new_key,))
            for old_ref, new_ref in zip(refs, new_refs):
                self.assertEqual(mapping[scope][(old_ref,)], (new_ref,))

    def test_original_document_is_not_modified(self):
        source = ElementTree.fromstring(MAIN_DOC)
        self.anonymize(doc=source)
        self.assertIn('E100', ElementTree.tostring(source, encoding='unicode'))

    def test_caller_provided_replacements(self):
        result = self.anonymize(replacements={'empKey': {
            ('E100',): ('X1',),
            ('E200',): ('X2',),
            ('E300',): ('X3',),
        }})
        self.assertIn('X1', result.document)
        self.assertIn('X2', result.document)
        self.assertIn('X3', result.document)
        self.assertTrue(self.schema.is_valid(
            ElementTree.fromstring(result.document)))

    def test_caller_provided_illegal_replacement(self):
        schema = xmlschema.XMLSchema(FACET_SCHEMA)
        with self.assertRaises(ValueGenerationError):
            anonymize_document(schema, FACET_DOC, ['empKey'],
                               replacements={'empKey': {('A12',): ('nope',),
                                                        ('B34',): ('C00',)}})

    def test_replacement_collision_fails(self):
        with self.assertRaises(ReplacementCollisionError):
            self.anonymize(replacements={'empKey': {
                ('E100',): ('DUP',),
                ('E200',): ('DUP',),
            }})

    def test_unresolvable_reference_fails(self):
        doc = MAIN_DOC.replace('empRef="E200"', 'empRef="NOPE"')
        with self.assertRaises(UnresolvableReferenceError):
            self.anonymize(doc=doc)

    def test_unknown_constraint_fails(self):
        with self.assertRaises(UnknownConstraintError):
            anonymize_document(self.schema, MAIN_DOC, ['noSuchKey'])

    def test_keyref_constraint_is_not_anonymizable(self):
        with self.assertRaises(UnknownConstraintError):
            anonymize_document(self.schema, MAIN_DOC, ['empRef'])

    def test_generator_cannot_produce_legal_value(self):
        schema = xmlschema.XMLSchema(FACET_SCHEMA)
        with self.assertRaises(ValueGenerationError):
            anonymize_document(schema, FACET_DOC, ['empKey'], seed=1,
                               max_attempts=50)

    def test_facets_satisfied_by_custom_generator(self):
        schema = xmlschema.XMLSchema(FACET_SCHEMA)

        def generator(field_types, key, scope, taken):
            for n in range(100):
                candidate = (f'K{n:02d}',)
                if candidate not in taken:
                    return candidate
            raise ValueGenerationError('exhausted')

        result = anonymize_document(schema, FACET_DOC, ['empKey'],
                                    generator=generator)
        self.assertIn('K00', result.document)
        self.assertIn('K01', result.document)
        self.assertNotIn('A12', result.document)
        self.assertTrue(schema.is_valid(
            ElementTree.fromstring(result.document)))

    def test_composite_key_treated_as_whole(self):
        schema = xmlschema.XMLSchema(COMPOSITE_SCHEMA)
        result = anonymize_document(schema, COMPOSITE_DOC, ['empKey'], seed=3)
        self.assertTrue(schema.is_valid(
            ElementTree.fromstring(result.document)))
        self.assertEqual(len(result.receipt), 2)
        for entry in result.receipt:
            self.assertEqual(len(entry.replacement), 2)
        # Same empId in different composite keys: distinct whole-key mappings.
        replacements = {entry.replacement for entry in result.receipt}
        self.assertEqual(len(replacements), 2)

        root = ElementTree.fromstring(result.document)
        keys = {(emp.get('empId'), emp.get('office'))
                for emp in root.iter('employee')}
        for ass in root.iter('assignment'):
            self.assertIn((ass.get('refId'), ass.get('refOffice')), keys)

    def test_composite_key_partial_reference_unresolvable(self):
        schema = xmlschema.XMLSchema(COMPOSITE_SCHEMA)
        doc = COMPOSITE_DOC.replace('refOffice="EU"', 'refOffice="UK"')
        with self.assertRaises(UnresolvableReferenceError):
            anonymize_document(schema, doc, ['empKey'], seed=3)

    def test_receipt_defaults_to_fingerprints(self):
        result = self.anonymize()
        data = result.receipt.to_dict()
        for entry in data['entries']:
            self.assertNotIn('original', entry)
            self.assertTrue(entry['fingerprint'])
        self.assertNotIn('E100', result.receipt.to_json())

    def test_receipt_with_originals(self):
        result = self.anonymize(include_originals=True)
        originals = {entry.original for entry in result.receipt}
        self.assertIn(('E100',), originals)
        self.assertIn(('E200',), originals)

    def test_logs_do_not_contain_original_keys(self):
        logger = 'xmlschema.extras.anonymize'
        with self.assertLogs(logger, level='INFO') as cm:
            self.anonymize()
        output = '\n'.join(cm.output)
        self.assertNotIn('E100', output)
        self.assertNotIn('E200', output)
        self.assertNotIn('E300', output)

    def test_namespaced_document(self):
        schema = xmlschema.XMLSchema(NS_SCHEMA)
        result = anonymize_document(schema, NS_DOC, ['empKey'], seed=5)
        self.assertNotIn('E100', result.document)
        self.assertTrue(schema.is_valid(
            ElementTree.fromstring(result.document)))
        self.assertIn('http://example.com/company', result.document)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_file = Path(tmpdir) / 'schema.xsd'
            doc_file = Path(tmpdir) / 'doc.xml'
            out_file = Path(tmpdir) / 'out.xml'
            receipt_file = Path(tmpdir) / 'receipt.json'
            schema_file.write_text(MAIN_SCHEMA, encoding='utf-8')
            doc_file.write_text(MAIN_DOC, encoding='utf-8')

            rc = main([str(schema_file), str(doc_file), 'empKey',
                       '-o', str(out_file), '-r', str(receipt_file),
                       '--seed', '11'])
            self.assertEqual(rc, 0)
            self.assertTrue(self.schema.is_valid(str(out_file)))
            receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
            self.assertEqual(len(receipt['entries']), 4)
            self.assertNotIn('E100', out_file.read_text(encoding='utf-8'))

    def test_cli_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_file = Path(tmpdir) / 'schema.xsd'
            doc_file = Path(tmpdir) / 'doc.xml'
            out_file = Path(tmpdir) / 'out.xml'
            schema_file.write_text(MAIN_SCHEMA, encoding='utf-8')
            doc_file.write_text(
                MAIN_DOC.replace('empRef="E200"', 'empRef="NOPE"'),
                encoding='utf-8')
            rc = main([str(schema_file), str(doc_file), 'empKey',
                       '-o', str(out_file)])
            self.assertEqual(rc, 1)
            self.assertFalse(out_file.exists())


if __name__ == '__main__':
    unittest.main()
