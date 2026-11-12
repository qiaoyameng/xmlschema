#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""Tests for schema-aware namespace prefix rewriting."""
import unittest
from textwrap import dedent
from xml.etree import ElementTree

from xmlschema import XMLSchema, XMLResource
from xmlschema.names import XSD_NAMESPACE
from xmlschema.prefix_rewriter import (
    NamespacePrefixRewriter, PrefixRewriteError,
    rewrite_namespace_prefixes,
)

NS_TEST = 'http://example.test/test'
NS_OLD = 'http://example.test/old'
NS_EXT = 'http://example.test/ext'

SCHEMA = dedent('''\
    <xs:schema xmlns:xs="{xs}" targetNamespace="{t}"
               xmlns:t="{t}" elementFormDefault="qualified">
      <xs:simpleType name="qnameOrString">
        <xs:union memberTypes="xs:QName xs:string"/>
      </xs:simpleType>
      <xs:simpleType name="qnameList">
        <xs:list itemType="xs:QName"/>
      </xs:simpleType>
      <xs:complexType name="innerType">
        <xs:simpleContent>
          <xs:extension base="t:qnameOrString">
            <xs:attribute name="a" type="xs:QName"/>
          </xs:extension>
        </xs:simpleContent>
      </xs:complexType>
      <xs:complexType name="mixedType" mixed="true">
        <xs:sequence>
          <xs:element name="ref" type="xs:QName" minOccurs="0"
                      maxOccurs="unbounded"/>
          <xs:any namespace="##other" processContents="lax"
                  minOccurs="0" maxOccurs="unbounded"/>
        </xs:sequence>
      </xs:complexType>
      <xs:element name="root">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="inner" type="t:innerType" maxOccurs="unbounded"/>
            <xs:element name="list" type="t:qnameList" maxOccurs="unbounded"/>
            <xs:element name="note" type="xs:string" maxOccurs="unbounded"/>
            <xs:element name="mixed" type="t:mixedType"/>
          </xs:sequence>
          <xs:attribute name="x" type="xs:QName"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>
''').format(xs=XSD_NAMESPACE, t=NS_TEST)

XML_DATA = dedent('''\
    <t:root xmlns:t="{t}" xmlns:p="{old}" x="p:foo">
      <t:inner a="p:attr">p:foo</t:inner>
      <t:inner a="p:attr">literal:x</t:inner>
      <t:list>p:foo p:bar local</t:list>
      <t:note>this:is:string</t:note>
      <t:mixed>text<t:ref>p:foo</t:ref>tail
        <ext:e xmlns:ext="{ext}" ext:q="p:foo">ext:body</ext:e>
      </t:mixed>
    </t:root>
''').format(t=NS_TEST, old=NS_OLD, ext=NS_EXT)


class TestPrefixRewriting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(SCHEMA)
        cls.prefix_map = {NS_TEST: 't', NS_OLD: 'u'}

    def test_rewrites_qname_values_and_tags(self):
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map,
                                     unknown_policy='preserve')
        output = rw.serialize(XML_DATA)
        self.assertIn('xmlns:u="%s"' % NS_OLD, output)
        self.assertIn('xmlns:t="%s"' % NS_TEST, output)
        self.assertIn('x="u:foo"', output)
        self.assertIn('a="u:attr"', output)
        self.assertIn('>u:foo<', output)
        self.assertIn('<t:list>u:foo u:bar local</t:list>', output)

    def test_plain_string_with_colon_is_preserved(self):
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map,
                                     unknown_policy='preserve')
        output = rw.serialize(XML_DATA)
        # Union value validated as xs:string must not be touched.
        self.assertIn('>literal:x<', output)
        # Plain xs:string element keeps its colon content.
        self.assertIn('<t:note>this:is:string</t:note>', output)

    def test_original_document_is_unchanged(self):
        resource = XMLResource(XML_DATA)
        before = resource.tostring()
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map,
                                     unknown_policy='preserve')
        rw.rewrite(resource)
        self.assertEqual(before, resource.tostring())

    def test_returns_new_tree_with_expanded_names(self):
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map,
                                     unknown_policy='preserve')
        new_root = rw.rewrite(XML_DATA)
        self.assertEqual(new_root.tag, '{%s}root' % NS_TEST)
        children = [c for c in new_root if not callable(c.tag)]
        self.assertEqual(children[0].tag, '{%s}inner' % NS_TEST)
        # Original source object identity is not reused for elements.
        self.assertIsNot(new_root, XMLResource(XML_DATA).root)

    def test_stable_allocation_across_runs(self):
        rw1 = NamespacePrefixRewriter(self.schema,
                                      unknown_policy='preserve')
        rw2 = NamespacePrefixRewriter(self.schema,
                                      unknown_policy='preserve')
        self.assertEqual(rw1.serialize(XML_DATA), rw2.serialize(XML_DATA))

    def test_reject_on_unproven_wildcard_by_default(self):
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map)
        with self.assertRaises(PrefixRewriteError) as ctx:
            rw.serialize(XML_DATA)
        self.assertIn('cannot prove the type', str(ctx.exception))

    def test_preserve_keeps_wildcard_content(self):
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map,
                                     unknown_policy='preserve')
        output = rw.serialize(XML_DATA)
        self.assertIn('<ext:e ext:q="p:foo">ext:body</ext:e>', output)
        # The wildcard's own namespace still has to be declared.
        self.assertIn('xmlns:ext="%s"' % NS_EXT, output)

    def test_semantic_verification_roundtrip(self):
        rw = NamespacePrefixRewriter(self.schema, self.prefix_map,
                                     unknown_policy='preserve')
        output = rw.serialize(XML_DATA)
        # The rewritten document remains valid against the original schema.
        self.assertTrue(self.schema.is_valid(XMLResource(output)))

    def test_invalid_unknown_policy(self):
        with self.assertRaises(ValueError):
            NamespacePrefixRewriter(self.schema, unknown_policy='guess')

    def test_duplicate_prefix_for_two_namespaces_rejected(self):
        with self.assertRaises(ValueError):
            NamespacePrefixRewriter(self.schema,
                                    {NS_TEST: 'p', NS_OLD: 'p'})

    def test_reserved_prefixes_rejected(self):
        with self.assertRaises(ValueError):
            NamespacePrefixRewriter(self.schema, {NS_OLD: 'xmlns'})
        with self.assertRaises(ValueError):
            NamespacePrefixRewriter(
                self.schema, {NS_OLD: 'xml'},
            )


class TestDefaultNamespaceAndShadowing(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(dedent('''\
            <xs:schema xmlns:xs="{xs}" targetNamespace="{t}"
                       elementFormDefault="qualified">
              <xs:element name="root">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="q" type="xs:QName"
                                maxOccurs="unbounded"/>
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
            </xs:schema>
        ''').format(xs=XSD_NAMESPACE, t=NS_TEST))

    def test_default_namespace_qname_without_prefix(self):
        xml_data = dedent('''\
            <root xmlns="{t}" xmlns:p="{old}">
              <q>p:x</q>
              <q>local</q>
            </root>
        ''').format(t=NS_TEST, old=NS_OLD)
        rw = NamespacePrefixRewriter(
            self.schema, {NS_TEST: 'f', NS_OLD: 'p'})
        output = rw.serialize(xml_data)
        # Unprefixed QName in the default namespace becomes 'f:local',
        # keeping the same expanded name {NS_TEST}local.
        self.assertIn('<f:q>f:local</f:q>', output)
        self.assertIn('<f:q>p:x</f:q>', output)
        self.assertTrue(self.schema.is_valid(XMLResource(output)))

    def test_local_prefix_shadowing(self):
        xml_data = dedent('''\
            <root xmlns="{t}" xmlns:p="{old}">
              <q>p:first</q>
              <q xmlns:p="{ext}">p:second</q>
            </root>
        ''').format(t=NS_TEST, old=NS_OLD, ext=NS_EXT)
        rw = NamespacePrefixRewriter(
            self.schema, {NS_TEST: 'f', NS_OLD: 'oa', NS_EXT: 'ob'})
        output = rw.serialize(xml_data)
        self.assertIn('<f:q>oa:first</f:q>', output)
        self.assertIn('<f:q>ob:second</f:q>', output)
        self.assertTrue(self.schema.is_valid(XMLResource(output)))


class TestXsiTypeAndSkipWildcard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(dedent('''\
            <xs:schema xmlns:xs="{xs}" targetNamespace="{t}"
                       xmlns:t="{t}" elementFormDefault="qualified">
              <xs:complexType name="base">
                <xs:sequence>
                  <xs:element name="q" type="xs:QName" minOccurs="0"/>
                </xs:sequence>
              </xs:complexType>
              <xs:complexType name="derived">
                <xs:complexContent>
                  <xs:extension base="t:base">
                    <xs:attribute name="extra" type="xs:QName"/>
                  </xs:extension>
                </xs:complexContent>
              </xs:complexType>
              <xs:element name="root">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="item" type="t:base" maxOccurs="unbounded"/>
                    <xs:element name="skipped" minOccurs="0">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:any namespace="##any" processContents="skip"
                                 maxOccurs="unbounded"/>
                        </xs:sequence>
                      </xs:complexType>
                    </xs:element>
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
            </xs:schema>
        ''').format(xs=XSD_NAMESPACE, t=NS_TEST))

    def test_xsi_type_prefix_is_rewritten(self):
        xml_data = dedent('''\
            <t:root xmlns:t="{t}" xmlns:p="{old}"
                    xmlns:xsi="{xsi}">
              <t:item xsi:type="t:derived" extra="p:foo">
                <t:q>p:foo</t:q>
              </t:item>
            </t:root>
        ''').format(t=NS_TEST, old=NS_OLD,
                    xsi='http://www.w3.org/2001/XMLSchema-instance')
        rw = NamespacePrefixRewriter(
            self.schema, {NS_TEST: 'ex', NS_OLD: 'old'})
        output = rw.serialize(xml_data)
        self.assertIn('xsi:type="ex:derived"', output)
        self.assertIn('extra="old:foo"', output)
        self.assertTrue(self.schema.is_valid(XMLResource(output)))

    def test_skip_wildcard_rejected_then_preserved(self):
        xml_data = dedent('''\
            <t:root xmlns:t="{t}">
              <t:item/>
              <t:skipped>
                <x:w xmlns:x="{ext}" x:a="p:v">x:text</x:w>
              </t:skipped>
            </t:root>
        ''').format(t=NS_TEST, ext=NS_EXT)
        rw = NamespacePrefixRewriter(self.schema)
        with self.assertRaises(PrefixRewriteError):
            rw.serialize(xml_data)

        rw2 = NamespacePrefixRewriter(self.schema,
                                      unknown_policy='preserve')
        output = rw2.serialize(xml_data)
        self.assertIn('x:a="p:v"', output)
        self.assertIn('xmlns:x="%s"' % NS_EXT, output)


class TestOrderAndMixedContent(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(dedent('''\
            <xs:schema xmlns:xs="{xs}" targetNamespace="{t}"
                       xmlns:t="{t}" elementFormDefault="qualified">
              <xs:complexType name="mixedType" mixed="true">
                <xs:sequence>
                  <xs:element name="a" type="xs:string"/>
                  <xs:element name="b" type="xs:string"/>
                  <xs:element name="q" type="xs:QName"/>
                </xs:sequence>
              </xs:complexType>
              <xs:element name="root" type="t:mixedType"/>
            </xs:schema>
        ''').format(xs=XSD_NAMESPACE, t=NS_TEST))

    def test_element_order_and_mixed_text_preserved(self):
        xml_data = dedent('''\
            <t:root xmlns:t="{t}" xmlns:p="{old}">first
              <t:a>1</t:a> between
              <t:b>2</t:b> more
              <t:q>p:name</t:q> end
            </t:root>
        ''').format(t=NS_TEST, old=NS_OLD)
        rw = NamespacePrefixRewriter(
            self.schema, {NS_TEST: 't', NS_OLD: 'u'})
        output = rw.serialize(xml_data)
        self.assertIn('first', output)
        self.assertIn('between', output)
        self.assertIn('more', output)
        self.assertIn('end', output)
        self.assertIn('<t:a>1</t:a> between', output)
        self.assertIn('<t:b>2</t:b> more', output)
        self.assertIn('<t:q>u:name</t:q>', output)
        # Element order a, b, q preserved.
        self.assertLess(output.index('<t:a>'), output.index('<t:b>'))
        self.assertLess(output.index('<t:b>'), output.index('<t:q>'))

    def test_expanded_names_are_preserved(self):
        xml_data = dedent('''\
            <t:root xmlns:t="{t}" xmlns:p="{old}">
              <t:a>1</t:a><t:b>2</t:b><t:q>p:name</t:q>
            </t:root>
        ''').format(t=NS_TEST, old=NS_OLD)
        rw = NamespacePrefixRewriter(
            self.schema, {NS_TEST: 't', NS_OLD: 'u'})
        new_root = rw.rewrite(xml_data)
        self.assertEqual(new_root.tag, '{%s}root' % NS_TEST)
        child_tags = [c.tag for c in new_root if not callable(c.tag)]
        self.assertEqual(child_tags, [
            '{%s}a' % NS_TEST,
            '{%s}b' % NS_TEST,
            '{%s}q' % NS_TEST,
        ])


class TestListAndUnionMembers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(dedent('''\
            <xs:schema xmlns:xs="{xs}" targetNamespace="{t}"
                       xmlns:t="{t}" elementFormDefault="qualified">
              <xs:simpleType name="unionType">
                <xs:union memberTypes="xs:QName xs:string"/>
              </xs:simpleType>
              <xs:simpleType name="listType">
                <xs:list itemType="xs:QName"/>
              </xs:simpleType>
              <xs:element name="root">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="u" type="t:unionType"
                                maxOccurs="unbounded"/>
                    <xs:element name="l" type="t:listType"/>
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
            </xs:schema>
        ''').format(xs=XSD_NAMESPACE, t=NS_TEST))

    def test_union_member_distinction(self):
        xml_data = dedent('''\
            <t:root xmlns:t="{t}" xmlns:p="{old}">
              <t:u>p:qname</t:u>
              <t:u>not:a:qname:value</t:u>
              <t:l>p:one p:two plain</t:l>
            </t:root>
        ''').format(t=NS_TEST, old=NS_OLD)
        rw = NamespacePrefixRewriter(
            self.schema, {NS_TEST: 't', NS_OLD: 'u'})
        output = rw.serialize(xml_data)
        self.assertIn('<t:u>u:qname</t:u>', output)
        # String union member (more than one colon) stays verbatim.
        self.assertIn('<t:u>not:a:qname:value</t:u>', output)
        self.assertIn('<t:l>u:one u:two plain</t:l>', output)


if __name__ == '__main__':
    from xmlschema.testing import run_xmlschema_tests
    run_xmlschema_tests('schema-aware prefix rewriting')


class TestErrorHandling(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(dedent('''\
            <xs:schema xmlns:xs="{xs}" targetNamespace="{t}"
                       elementFormDefault="qualified">
              <xs:element name="root">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="q" type="xs:QName"/>
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
            </xs:schema>
        ''').format(xs=XSD_NAMESPACE, t=NS_TEST))

    def test_unbound_qname_prefix_is_rejected(self):
        xml_data = '<root xmlns="{t}"><q>p:x</q></root>'.format(t=NS_TEST)
        rw = NamespacePrefixRewriter(self.schema)
        with self.assertRaises(PrefixRewriteError) as ctx:
            rw.serialize(xml_data)
        self.assertIn("not declared", str(ctx.exception))

    def test_lazy_resource_is_rejected(self):
        resource = XMLResource(
            '<root xmlns="{t}"><q>p:x</q></root>'.format(t=NS_TEST), lazy=True)
        rw = NamespacePrefixRewriter(self.schema)
        from xmlschema.exceptions import XMLResourceError
        with self.assertRaises(XMLResourceError):
            rw.rewrite(resource)

    def test_invalid_source_type(self):
        rw = NamespacePrefixRewriter(self.schema)
        with self.assertRaises(TypeError):
            rw.rewrite(12345)

    def test_schema_argument_type_checked(self):
        from xmlschema.exceptions import XMLSchemaTypeError
        with self.assertRaises(XMLSchemaTypeError):
            NamespacePrefixRewriter('not-a-schema')

    def test_xsi_nil_is_admitted_without_declaration(self):
        schema = XMLSchema(dedent(
            '<xs:schema xmlns:xs="{xs}" targetNamespace="{t}" '
            'elementFormDefault="qualified">'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="a" type="xs:string" nillable="true"/>'
            '</xs:sequence></xs:complexType></xs:element></xs:schema>'
        ).format(xs=XSD_NAMESPACE, t=NS_TEST))
        xml_data = (
            '<root xmlns="{t}" xmlns:xsi="{xsi}">'
            '<a xsi:nil="true"/></root>'
        ).format(t=NS_TEST,
                 xsi='http://www.w3.org/2001/XMLSchema-instance')
        rw = NamespacePrefixRewriter(schema, {NS_TEST: 't'})
        output = rw.serialize(xml_data)
        self.assertIn('xsi:nil="true"', output)

    def test_element_input_without_nsmap_raises_clearly(self):
        # A stdlib Element built outside XMLResource has already lost the
        # xmlns declarations needed to resolve QName values.
        root = ElementTree.fromstring(
            '<root xmlns="{t}"><q>p:x</q></root>'.format(t=NS_TEST))
        rw = NamespacePrefixRewriter(self.schema)
        with self.assertRaises(PrefixRewriteError):
            rw.serialize(root)


class TestPublicAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = XMLSchema(SCHEMA)
        cls.prefix_map = {NS_TEST: 't', NS_OLD: 'u'}

    def test_module_alias(self):
        from xmlschema import rewrite_namespace_prefixes, rewrite_prefixes
        self.assertIs(rewrite_prefixes, rewrite_namespace_prefixes)

    def test_schema_method(self):
        new_root = self.schema.rewrite_prefixes(
            XML_DATA, self.prefix_map, unknown_policy='preserve')
        self.assertEqual(new_root.tag, '{%s}root' % NS_TEST)

    def test_module_function_returns_tree(self):
        new_root = rewrite_namespace_prefixes(
            XML_DATA, self.schema, self.prefix_map,
            unknown_policy='preserve')
        self.assertEqual(new_root.tag, '{%s}root' % NS_TEST)
