#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import unittest
from textwrap import dedent
from xml.etree import ElementTree

from xmlschema import XMLResource, XMLSchema10, normalize_values

NS = 'http://example.com/normalization'
DSIG_NS = 'http://www.w3.org/2000/09/xmldsig#'

XSD = dedent(f'''\
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
               targetNamespace="{NS}" xmlns:t="{NS}"
               elementFormDefault="qualified">
      <xs:simpleType name="ambiguous">
        <xs:union memberTypes="xs:integer xs:NMTOKEN"/>
      </xs:simpleType>
      <xs:simpleType name="integerOnly">
        <xs:union>
          <xs:simpleType>
            <xs:restriction base="xs:integer"/>
          </xs:simpleType>
        </xs:union>
      </xs:simpleType>
      <xs:simpleType name="booleanType">
        <xs:restriction base="xs:boolean"/>
      </xs:simpleType>
      <xs:element name="root">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="boolean" type="xs:boolean"/>
            <xs:element name="typed" type="xs:boolean"/>
            <xs:element name="decimal" type="xs:decimal"/>
            <xs:element name="integer" type="xs:integer"/>
            <xs:element name="moment" type="xs:dateTime"/>
            <xs:element name="qname" type="xs:QName"/>
            <xs:element name="flags">
              <xs:simpleType>
                <xs:list itemType="xs:boolean"/>
              </xs:simpleType>
            </xs:element>
            <xs:element name="ambiguous" type="t:ambiguous"/>
            <xs:element name="integerOnly" type="t:integerOnly"/>
            <xs:element name="untyped" type="xs:anyType"/>
            <xs:element name="mixed">
              <xs:complexType mixed="true">
                <xs:sequence>
                  <xs:element name="b" type="xs:boolean" minOccurs="0"/>
                </xs:sequence>
              </xs:complexType>
            </xs:element>
            <xs:element name="signed">
              <xs:complexType>
                <xs:sequence>
                  <xs:element name="flag" type="xs:boolean"/>
                </xs:sequence>
                <xs:attribute name="id" type="xs:ID"/>
              </xs:complexType>
            </xs:element>
            <xs:any namespace="##other" processContents="skip" minOccurs="0"/>
          </xs:sequence>
          <xs:attribute name="decimalAttribute" type="xs:decimal"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>
''')


def document(qname_prefix, boolean, decimal, integer, moment, flags,
             integer_only, signed_flag='1'):
    return dedent(f'''\
        <t:root xmlns:t="{NS}" xmlns:xs="http://www.w3.org/2001/XMLSchema"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xmlns:ds="{DSIG_NS}" decimalAttribute="01.20">
          <t:boolean xsi:type="t:booleanType">{boolean}</t:boolean>
          <t:typed>{boolean}</t:typed>
          <t:decimal>{decimal}</t:decimal>
          <t:integer>{integer}</t:integer>
          <t:moment>{moment}</t:moment>
          <t:qname xmlns:{qname_prefix}="{NS}">{qname_prefix}:value</t:qname>
          <t:flags>{flags}</t:flags>
          <t:ambiguous>001</t:ambiguous>
          <t:integerOnly>{integer_only}</t:integerOnly>
          <t:untyped>1</t:untyped>
          <t:mixed>mixed 1 <t:b>{boolean}</t:b></t:mixed>
          <t:signed id="signed-data"><t:flag>{signed_flag}</t:flag></t:signed>
          <ds:Signature>
            <ds:SignedInfo>
              <ds:Reference URI="#signed-data"/>
            </ds:SignedInfo>
            <ds:SignatureValue>AAAA</ds:SignatureValue>
          </ds:Signature>
        </t:root>
    ''')


class TestValueNormalization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema10(XSD)

    def test_equivalent_documents_have_same_normalized_values(self):
        first = document('o', '1', '01.00', '042',
                         '2026-01-02T03:04:05.100+00:00', '1  0', '042')
        second = document('t', 'true', '1.0', '42',
                          '2026-01-02T03:04:05.10Z', 'true false', '42')

        normalized_first = normalize_values(first, self.schema)
        normalized_second = normalize_values(second, self.schema)

        first_root = normalized_first.root
        second_root = normalized_second.root
        self.schema.validate(first)
        self.schema.validate(second)

        for name in ('boolean', 'typed', 'decimal', 'integer', 'moment',
                     'flags', 'integerOnly'):
            self.assertEqual(
                first_root.find(f'{{{NS}}}{name}').text,
                second_root.find(f'{{{NS}}}{name}').text,
                name,
            )

        self.assertEqual(first_root.find(f'{{{NS}}}boolean').text, 'true')
        self.assertEqual(first_root.find(f'{{{NS}}}decimal').text, '1.0')
        self.assertEqual(first_root.find(f'{{{NS}}}integer').text, '42')
        self.assertEqual(first_root.find(f'{{{NS}}}moment').text,
                         '2026-01-02T03:04:05.1Z')
        self.assertEqual(first_root.find(f'{{{NS}}}flags').text, 'true false')
        self.assertEqual(first_root.find(f'{{{NS}}}integerOnly').text, '42')
        self.assertEqual(first_root.attrib['decimalAttribute'], '1.2')

        decoded_first = self.schema.decode(first)
        decoded_second = self.schema.decode(second)
        decoded_qname_first = decoded_first['o:qname']
        decoded_qname_second = decoded_second['t:qname']
        self.assertEqual(
            '{' + decoded_qname_first['@xmlns:o'] + '}value',
            '{' + decoded_qname_second['@xmlns:t'] + '}value',
        )
        self.assertNotEqual(
            first_root.find(f'{{{NS}}}qname').text,
            second_root.find(f'{{{NS}}}qname').text,
        )
        self.assertTrue(
            first_root.find(f'{{{NS}}}qname').text.endswith(':value')
        )

    def test_ambiguous_mixed_unknown_and_signed_nodes_are_preserved(self):
        xml = document('p', '1', '01.00', '042',
                       '2026-01-02T03:04:05.100+00:00', '1  0', '042')
        result = normalize_values(xml, self.schema)
        root = result.root

        self.assertEqual(root.find(f'{{{NS}}}ambiguous').text, '001')
        self.assertEqual(root.find(f'{{{NS}}}untyped').text, '1')
        self.assertIn('mixed 1 ', root.find(f'{{{NS}}}mixed').text)
        self.assertEqual(root.find(f'{{{NS}}}mixed/{{{NS}}}b').text, 'true')
        self.assertEqual(root.find(f'{{{NS}}}signed/{{{NS}}}flag').text, '1')

        change_paths = {change.path for change in result.changes}
        self.assertNotIn(f'/{{}}root/{{{NS}}}signed/{{{NS}}}flag', change_paths)
        self.assertNotIn('/{http://www.w3.org/2000/09/xmldsig#}Signature',
                         change_paths)

    def test_idempotency(self):
        xml = document('p', '1', '01.00', '042',
                       '2026-01-02T03:04:05.100+00:00', '1  0', '042')
        first = normalize_values(xml, self.schema)
        second = normalize_values(first.resource, self.schema)

        self.assertEqual(
            first.resource.tostring(),
            second.resource.tostring(),
        )
        self.assertEqual(second.changes, ())

    def test_xsi_nil_uses_boolean_type(self):
        xsd = dedent('''\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
              <xs:element name="value" type="xs:boolean" nillable="true"/>
            </xs:schema>
        ''')
        xml = '<value xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ' \
              'xsi:nil="1"/>'
        schema = XMLSchema10(xsd)

        result = normalize_values(xml, schema)

        self.assertEqual(
            result.root.attrib[
                '{http://www.w3.org/2001/XMLSchema-instance}nil'
            ],
            'true',
        )
        self.assertEqual(result.changes[0].xsd_type, 'xs:boolean')

    def test_inputs_are_not_modified(self):
        xml = document(
            'p', '1', '01.00', '042',
            '2026-01-02T03:04:05.100+00:00', '1  0', '042'
        )
        source = XMLResource(xml)
        original_xml = source.tostring()
        schema = XMLSchema10(XSD)
        original_boolean = schema.elements['root'].type

        result = normalize_values(source, schema)

        self.assertIsNot(result.root, source.root)
        self.assertEqual(source.tostring(), original_xml)
        self.assertEqual(source.root.find(f'{{{NS}}}boolean').text, '1')
        self.assertIs(schema.elements['root'].type, original_boolean)


if __name__ == '__main__':
    unittest.main()
