#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""Tests for schema-aware namespace prefix rewriting."""
import io
import unittest
from xml.etree import ElementTree

from xmlschema import XMLSchema, rewrite_namespace_prefixes
from xmlschema.names import XSI_NAMESPACE
from xmlschema.utils.qnames import get_extended_qname


# A schema exercising every QName carrying construct:
# atomic xs:QName (element + attribute), xs:list of QName, union of
# QName/string, xsi:type and a derived QName enumeration.
XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:a" xmlns="urn:a"
           elementFormDefault="qualified"
           attributeFormDefault="qualified">
  <xs:complexType name="RT">
    <xs:sequence>
      <xs:element name="q" type="xs:QName"/>
      <xs:element name="ql">
        <xs:simpleType><xs:list itemType="xs:QName"/></xs:simpleType>
      </xs:element>
      <xs:element name="qu">
        <xs:simpleType>
          <xs:union memberTypes="xs:QName xs:string"/>
        </xs:simpleType>
      </xs:element>
      <xs:element name="plain" type="xs:string"/>
      <xs:element name="notn" type="xs:NOTATION" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="qa" type="xs:QName"/>
    <xs:attribute name="qal">
      <xs:simpleType><xs:list itemType="xs:QName"/></xs:simpleType>
    </xs:attribute>
    <xs:attribute name="qau">
      <xs:simpleType>
        <xs:union memberTypes="xs:QName xs:string"/>
      </xs:simpleType>
    </xs:attribute>
  </xs:complexType>
  <xs:element name="root" type="RT"/>
</xs:schema>"""


def decode(schema, text):
    """Decode and normalize to expanded QName identities."""
    return schema.to_dict(text)


class TestPrefixRewriting(unittest.TestCase):

    def setUp(self):
        self.schema = XMLSchema(XSD)

    def test_tags_and_xmlns_renamed(self):
        xml = ('<a:root xmlns:a="urn:a" xmlns:b="urn:b">'
               '<c:child xmlns:c="urn:c"/></a:root>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'aa': 'urn:a', 'bb': 'urn:b', 'cc': 'urn:c'})
        self.assertIn('xmlns:aa="urn:a"', out)
        self.assertIn('xmlns:bb="urn:b"', out)
        self.assertIn('xmlns:cc="urn:c"', out)
        self.assertIn('<aa:root', out)
        self.assertIn('<cc:child', out)
        self.assertNotIn('xmlns:a=', out)
        # Expanded names are unchanged.
        root = ElementTree.fromstring(out)
        self.assertEqual(root.tag, '{urn:a}root')
        self.assertEqual(root[0].tag, '{urn:c}child')

    def test_plain_string_with_colon_untouched(self):
        xml = ('<a:root xmlns:a="urn:a" xmlns:b="urn:b" '
               'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
               '<a:plain>protocol:like value</a:plain>'
               '<a:qu>not:a/qname</a:qu>'
               '</a:root>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'aa': 'urn:a', 'bb': 'urn:b'},
            schema=self.schema)
        self.assertIn('<aa:plain>protocol:like value</aa:plain>', out)
        # 'not' is not a declared prefix: value must be left untouched.
        self.assertIn('<aa:qu>not:a/qname</aa:qu>', out)

    def test_qname_attribute_list_union_and_xsi_type(self):
        xml = (
            '<a:root xmlns:a="urn:a" xmlns:b="urn:b" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'a:qa="b:Thing" a:qal="b:P1 b:P2" a:qau="b:U" '
            'xsi:type="a:RT">'
            '<a:q>b:Thing</a:q>'
            '<a:ql>b:X  b:Y</a:ql>'
            '<a:qu>b:Ref</a:qu>'
            '<a:plain>str:ing</a:plain>'
            '</a:root>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'aa': 'urn:a', 'bb': 'urn:b'},
            schema=self.schema)
        self.assertIn('aa:qa="bb:Thing"', out)
        self.assertIn('aa:qal="bb:P1 bb:P2"', out)
        self.assertIn('aa:qau="bb:U"', out)
        self.assertIn('xsi:type="aa:RT"', out)
        self.assertIn('<aa:q>bb:Thing</aa:q>', out)
        # Original double-space separator is preserved.
        self.assertIn('<aa:ql>bb:X  bb:Y</aa:ql>', out)
        self.assertIn('<aa:qu>bb:Ref</aa:qu>', out)
        self.assertIn('<aa:plain>str:ing</aa:plain>', out)
        self.assertNotIn('="b:', out)
        self.assertNotIn('>b:', out)

    def test_decode_identity_preserved(self):
        xml = (
            '<a:root xmlns:a="urn:a" xmlns:b="urn:b" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'a:qa="b:Thing" a:qal="b:P1 b:P2" '
            'xsi:type="a:RT">'
            '<a:q>b:Thing</a:q>'
            '<a:ql>b:X b:Y</a:ql>'
            '<a:qu>b:Ref</a:qu>'
            '<a:plain>v:w</a:plain>'
            '</a:root>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'aa': 'urn:a', 'bb': 'urn:b'},
            schema=self.schema)
        before = decode(self.schema, xml)
        after = decode(self.schema, out)
        # QName identities (expanded names), not lexicals, must match.
        self.assertEqual(before['a:q'].replace('b:', '{urn:b}'),
                         after['aa:q'].replace('bb:', '{urn:b}'))
        self.assertEqual(after['aa:qu'], '{urn:b}Ref')
        self.assertEqual(before['a:qu'], after['aa:qu'])
        self.assertEqual(before['a:ql'], ['b:X', 'b:Y'])
        self.assertEqual(after['aa:ql'], ['bb:X', 'bb:Y'])
        # Namespace identities of the list members.
        nsmap = {'b': 'urn:b', 'bb': 'urn:b'}
        self.assertEqual(
            [get_extended_qname(x, nsmap) for x in before['a:ql']],
            [get_extended_qname(x, nsmap) for x in after['aa:ql']])
        self.assertEqual(before['a:plain'], after['aa:plain'])

    def test_default_namespace_and_no_namespace(self):
        xml = (
            '<root xmlns="urn:a" xmlns:a="urn:a" xmlns:b="urn:b" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'a:qa="b:Thing" xsi:type="a:RT">'
            '<q>b:Thing</q><ql>b:X b:Y</ql><qu>b:Ref</qu>'
            '<plain>x:y</plain></root>')
        out = rewrite_namespace_prefixes(xml, schema=self.schema)
        # Default namespace preserved as default.
        self.assertIn('xmlns="urn:a"', out)
        root = ElementTree.fromstring(out)
        self.assertEqual(root.tag, '{urn:a}root')
        # Decode identities agree.
        before = decode(self.schema, xml)
        after = decode(self.schema, out)
        self.assertEqual(before['qu'], after['qu'])
        self.assertEqual(after['qu'], '{urn:b}Ref')

    def test_default_namespace_qname_value_uses_auxiliary_prefix(self):
        # A QName value resolving into the default namespace needs a
        # non-empty auxiliary prefix in the output (the empty prefix cannot
        # be used inside a QName lexical), while element names keep using
        # the default namespace. Identities must survive the rewrite.
        xml = (
            '<root xmlns="urn:a" xmlns:a="urn:a" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:type="a:RT">'
            '<q>root</q><ql/> <qu>plain</qu><plain>z</plain></root>')
        out = rewrite_namespace_prefixes(xml, schema=self.schema)
        out_root = ElementTree.fromstring(out)
        self.assertEqual(out_root.tag, '{urn:a}root')
        self.assertIn('xmlns="urn:a"', out)
        # The auxiliary alias is declared and used by xsi:type.
        self.assertIn('xsi:type="', out)
        self.assertIn(':RT"', out)
        # Decoding the value with its in-scope prefix yields the same
        # expanded identity as resolving 'root' under the default namespace.
        q_text = out_root[0].text
        ns_map = {}
        for _ev, decl in ElementTree.iterparse(
                io.StringIO(out), events=('start-ns',)):
            ns_map[decl[0]] = decl[1]
        self.assertEqual(get_extended_qname(q_text, ns_map),
                         '{urn:a}root')

    def test_local_prefix_redefinition_scope(self):
        # Same prefix 'p' bound to urn:A at root, rebound to urn:B on a
        # branch; a sibling after the branch sees urn:A again.
        xsd = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="branch">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="inner">
                <xs:complexType>
                  <xs:attribute name="ref" type="xs:QName"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
        <xs:element name="sibling">
          <xs:complexType>
            <xs:attribute name="ref" type="xs:QName"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
        schema = XMLSchema(xsd)
        xml = (
            '<root xmlns:p="urn:A">'
            '<branch xmlns:p="urn:B"><inner ref="p:X"/></branch>'
            '<sibling ref="p:Y"/></root>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'a': 'urn:A', 'b': 'urn:B'}, schema=schema)
        before = decode(schema, xml)
        after = decode(schema, out)
        # Compare expanded identities; lexical attribute QNames keep their
        # prefix in the default converter, so resolve against the scope.
        inner_ns = {'b': 'urn:B'}
        sibling_ns = {'a': 'urn:A'}
        self.assertEqual(
            get_extended_qname(after['branch']['inner']['@ref'], inner_ns),
            '{urn:B}X')
        self.assertEqual(
            get_extended_qname(after['sibling']['@ref'], sibling_ns),
            '{urn:A}Y')
        self.assertEqual(
            get_extended_qname(before['branch']['inner']['@ref'],
                               {'p': 'urn:B'}),
            get_extended_qname(after['branch']['inner']['@ref'], inner_ns))
        self.assertEqual(
            get_extended_qname(before['sibling']['@ref'], {'p': 'urn:A'}),
            get_extended_qname(after['sibling']['@ref'], sibling_ns))

    def test_collision_fixed_allocation(self):
        xml = ('<p:root xmlns:p="urn:one" xmlns:q="urn:two"/>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'p': 'urn:one', 'p2': 'urn:two'})
        # 'p' requested for both urn:one (kept) ... force a real collision:
        self.assertIn('xmlns:p="urn:one"', out)

        colliding = rewrite_namespace_prefixes(
            '<p:root xmlns:p="urn:one" xmlns:r="urn:two"/>',
            namespaces={'x': 'urn:zero', 'x0': 'urn:one'})
        # numeric suffix rule for a taken preferred prefix
        self.assertIn('xmlns:x="urn:zero"', colliding)
        self.assertIn('xmlns:x0="urn:one"', colliding)

    def test_order_text_and_structure_preserved(self):
        xml = (
            '<a:root xmlns:a="urn:a">\n  text-start\n'
            '  <a:first>1</a:first>\n  <a:second>2</a:second>\n'
            '  tail\n</a:root>')
        out = rewrite_namespace_prefixes(xml, namespaces={'z': 'urn:a'})
        self.assertIn('\n  text-start\n', out)
        self.assertIn('  tail\n', out)
        tags = [c.tag for c in ElementTree.fromstring(out)]
        self.assertEqual(tags, ['{urn:a}first', '{urn:a}second'])

    def test_bytes_and_file_object_input(self):
        xml = '<a:root xmlns:a="urn:a"/>'
        out = rewrite_namespace_prefixes(
            xml.encode('utf-8'), namespaces={'z': 'urn:a'})
        self.assertIsInstance(out, str)
        out_bytes = rewrite_namespace_prefixes(
            io.BytesIO(xml.encode()), namespaces={'z': 'urn:a'},
            encoding='utf-8')
        self.assertIsInstance(out_bytes, bytes)
        self.assertIn(b'xmlns:z="urn:a"', out_bytes)

    def test_without_schema_still_rewrites_xsi_type(self):
        xml = (
            '<a:root xmlns:a="urn:a" xmlns:b="urn:b" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:type="b:Thing">'
            '<a:q>b:Thing</a:q></a:root>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'aa': 'urn:a', 'bb': 'urn:b'})
        self.assertIn('xsi:type="bb:Thing"', out)
        # Without a schema the QName-like element text is NOT rewritten.
        self.assertIn('<aa:q>b:Thing</aa:q>', out)

    def test_xsi_type_namespace_declared_without_tag_use(self):
        # The namespace referenced only by xsi:type must still be declared.
        xml = ('<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
               'xmlns:g="urn:ghost" xsi:type="g:Thing"/>')
        out = rewrite_namespace_prefixes(xml)
        self.assertIn('xmlns:g="urn:ghost"', out)
        self.assertIn('xsi:type="g:Thing"', out)
        ElementTree.fromstring(out)

    def test_unbound_prefix_left_untouched(self):
        xml = ('<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
               'xsi:type="missing:T">'
               '<c xmlns:a="urn:a">zz:weird</c></r>')
        out = rewrite_namespace_prefixes(
            xml, namespaces={'aa': 'urn:a'})
        # Unresolvable xsi:type lexical and plain string are preserved.
        self.assertIn('xsi:type="missing:T"', out)
        self.assertIn('<c>zz:weird</c>', out)  # no-namespace, unprefixed
        ElementTree.fromstring(out)

    def test_local_default_undeclaration_preserved(self):
        xml = ('<root xmlns="urn:a" xmlns:z="urn:z">'
               '<child xmlns="">local</child><z:x>1</z:x></root>')
        out = rewrite_namespace_prefixes(xml)
        self.assertIn('<child xmlns="">local</child>', out)
        parsed = ElementTree.fromstring(out)
        self.assertEqual(parsed.tag, '{urn:a}root')
        self.assertEqual(parsed[0].tag, 'child')  # stays no-namespace

        # With an explicit prefix for the default ns, the child still
        # stays unqualified and no xmlns="" is necessary.
        out2 = rewrite_namespace_prefixes(xml, namespaces={'nsa': 'urn:a'})
        parsed2 = ElementTree.fromstring(out2)
        self.assertEqual(parsed2[0].tag, 'child')
        self.assertEqual(parsed2[1].tag, '{urn:z}x')


if __name__ == '__main__':
    unittest.main()
