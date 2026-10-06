#!/usr/bin/env python
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

from xmlschema import XMLSchema, XMLResource, rewrite_namespace_prefixes


class TestNamespacePrefixRewrite(unittest.TestCase):

    def setUp(self):
        self.schema = XMLSchema(dedent('''\
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                       xmlns:t="http://example.test/tns"
                       targetNamespace="http://example.test/tns"
                       elementFormDefault="qualified"
                       attributeFormDefault="unqualified">
              <xs:simpleType name="qname-list">
                <xs:list itemType="xs:QName"/>
              </xs:simpleType>
              <xs:simpleType name="qname-or-string">
                <xs:union memberTypes="xs:QName xs:string"/>
              </xs:simpleType>
              <xs:simpleType name="string-or-qname">
                <xs:union memberTypes="xs:string xs:QName"/>
              </xs:simpleType>
              <xs:complexType name="derived-type">
                <xs:complexContent>
                  <xs:restriction base="xs:anyType"/>
                </xs:complexContent>
              </xs:complexType>
              <xs:simpleType name="derived-qname">
                <xs:restriction base="xs:QName"/>
              </xs:simpleType>
              <xs:element name="root">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="string-value" type="xs:string"/>
                    <xs:element name="qname-value" type="xs:QName"/>
                    <xs:element name="qname-list" type="t:qname-list"/>
                    <xs:element name="union-string" type="t:string-or-qname"/>
                    <xs:element name="union-qname" type="t:qname-or-string"/>
                    <xs:element name="shadowed-qname" type="xs:QName"/>
                    <xs:element name="unqualified" form="unqualified"/>
                    <xs:element name="typed-qname" type="xs:QName"/>
                  </xs:sequence>
                  <xs:attribute name="attr-qname" type="xs:QName"/>
                </xs:complexType>
              </xs:element>
            </xs:schema>
        '''))

        self.xml = dedent('''\
            <root xmlns="http://example.test/tns"
                  xmlns:old="http://example.test/tns"
                  xmlns:alpha="http://example.test/alpha"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  attr-qname="alpha:attribute">
              <string-value>alpha:ordinary string</string-value>
              <qname-value>alpha:element</qname-value>
              <qname-list>  alpha:first   alpha:second  </qname-list>
              <union-string>alpha:ordinary</union-string>
              <union-qname>alpha:union</union-qname>
              <shadowed-qname xmlns:alpha="http://example.test/beta">alpha:shadow</shadowed-qname>
              <unqualified xmlns=""/>
              <typed-qname xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                           xsi:type="old:derived-qname">alpha:typed</typed-qname>
            </root>
        ''')

    def qname_identities(self, xml_text):
        result = self.schema.decode(XMLResource(xml_text), keep_empty=True)
        resource = XMLResource(xml_text)
        identities = {}
        def decode_value(value, nsmap):
            if isinstance(value, dict):
                value = value['$']
            if ':' in value:
                prefix, local_name = value.split(':', 1)
                return f'{{{nsmap[prefix]}}}{local_name}'
            return value

        for element in resource.root.iter():
            nsmap = resource.get_nsmap(element) or {}
            name = element.tag.split('}', 1)[1] if '}' in element.tag else element.tag

            if name in {'qname-value', 'union-qname', 'shadowed-qname',
                        'typed-qname'}:
                identities[name] = decode_value(element.text, nsmap)
            elif name == 'qname-list':
                identities[name] = tuple(
                    decode_value(token, nsmap) for token in element.text.split()
                )

            xsi_type = element.get(
                '{http://www.w3.org/2001/XMLSchema-instance}type'
            )
            if xsi_type is not None:
                identities[f'{name}@xsi:type'] = decode_value(xsi_type, nsmap)

        root_nsmap = resource.get_nsmap(resource.root) or {}
        identities['attr-qname'] = decode_value(
            resource.root.attrib['attr-qname'], root_nsmap
        )
        return identities

    def test_rewrite_qname_values_and_local_prefix_shadowing(self):
        rewritten = rewrite_namespace_prefixes(
            self.xml,
            self.schema,
            {
                '': 'http://example.test/tns',
                't': 'http://example.test/tns',
                'a': 'http://example.test/alpha',
                'b': 'http://example.test/beta',
                'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
            }
        )

        self.assertIn('<t:root', rewritten)
        self.assertIn('xmlns:t="http://example.test/tns"', rewritten)
        self.assertNotIn('xmlns="http://example.test/tns"', rewritten)
        self.assertIn('xsi:type="t:derived-qname"', rewritten)
        self.assertIn('attr-qname="a:attribute"', rewritten)
        self.assertIn('<t:string-value>alpha:ordinary string</t:string-value>', rewritten)
        self.assertIn('<t:qname-value>a:element</t:qname-value>', rewritten)
        self.assertIn('<t:qname-list>  a:first   a:second  </t:qname-list>', rewritten)
        self.assertIn('<t:union-string>alpha:ordinary</t:union-string>', rewritten)
        self.assertIn('<t:union-qname>a:union</t:union-qname>', rewritten)
        self.assertIn('<t:shadowed-qname>b:shadow</t:shadowed-qname>', rewritten)
        self.assertIn('<unqualified/>', rewritten)
        self.assertIn('xsi:type="t:derived-qname"', rewritten)
        self.assertIn('<t:typed-qname', rewritten)
        self.assertIn('>a:typed</t:typed-qname>', rewritten)

        ElementTree.fromstring(rewritten)
        self.assertEqual(
            self.qname_identities(self.xml),
            self.qname_identities(rewritten)
        )

    def test_conflicting_prefixes_get_fixed_allocations(self):
        xml = dedent('''\
            <root xmlns="http://example.test/tns"
                  xmlns:p="http://example.test/alpha">
              <qname-value xmlns:p="http://example.test/beta">p:value</qname-value>
            </root>
        ''')

        rewritten = rewrite_namespace_prefixes(
            xml,
            self.schema,
            {'t': 'http://example.test/tns',
             'p': 'http://example.test/alpha'}
        )

        self.assertIn('<t:root', rewritten)
        self.assertIn('xmlns:p="http://example.test/alpha"', rewritten)
        self.assertIn('xmlns:p0="http://example.test/beta"', rewritten)
        self.assertIn('<t:qname-value>p0:value</t:qname-value>', rewritten)


if __name__ == '__main__':
    unittest.main()
