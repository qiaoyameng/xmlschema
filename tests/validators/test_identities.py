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
import pathlib
import pickle
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from xmlschema import XMLSchemaParseError, XMLSchemaValidationError
from xmlschema.validators import XMLSchema11
from xmlschema.validators.identities import IdentityCounter, KeyrefCounter, \
    FieldValueSelector, SQLiteIdentityIndex
from xmlschema.testing import XsdValidatorTestCase


class TestXsdIdentities(XsdValidatorTestCase):

    cases_dir = pathlib.Path(__file__).parent.joinpath('../test_cases')

    def test_key_definition(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""")

        selector = schema.identities['key1'].selector
        self.assertTrue(selector.built)
        self.assertEqual(repr(selector), "XsdSelector(path='.')")

    def test_duplicated_key_name(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>
            <xs:element name="secondary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 2)
        self.assertIn("duplicated value ('key1',)", schema.all_errors[0].message)

    def test_missing_key_name(self):
        schema = self.check_schema("""
                <xs:element name="primary_key" type="xs:string">
                    <xs:key>
                        <xs:selector xpath="." />
                        <xs:field xpath="."/>
                    </xs:key>
                </xs:element>""", validation='lax')

        errors = schema.all_errors
        if schema.XSD_VERSION == '1.0':
            self.assertEqual(len(errors), 3)
            self.assertEqual(errors[0].message, "missing required attribute 'name'")
        else:
            self.assertEqual(len(errors), 3)
            self.assertIn("missing key field '@name'", errors[0].message)

    def test_missing_selector(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""", validation='lax')

        errors = schema.all_errors
        self.assertEqual(len(errors), 2)
        self.assertIn("Unexpected child with tag 'xs:field' at position 1", errors[0].message)

        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                  <xs:annotation/>
              </xs:key>
            </xs:element>""", validation='lax')

        errors = schema.all_errors
        self.assertEqual(len(errors), 2 if schema.XSD_VERSION == '1.0' else 1)
        self.assertEqual(errors[-1].message, "missing 'selector' declaration")

    def test_missing_selector_path(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector />  <!-- missing 'xpath' attribute -->
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 1)
        self.assertEqual(schema.identities['key1'].selector.path, '*')

    def test_invalid_selector_path(self):
        with self.assertRaises(XMLSchemaParseError) as ctx:
            self.check_schema("""
                <xs:element name="primary_key" type="xs:string">
                    <xs:key name="key1">
                        <xs:selector xpath="/" />  <!-- Invalid XPath selector expression -->
                        <xs:field xpath="."/>
                    </xs:key>
                </xs:element>""")

        self.assertEqual(ctx.exception.message, "invalid XPath expression for an XsdSelector")

        with self.assertRaises(XMLSchemaParseError) as ctx:
            self.check_schema("""
                <xs:element name="primary_key" type="xs:string">
                    <xs:key name="key1">
                        <xs:selector xpath="xs : *" />  <!-- Invalid XPath expression -->
                        <xs:field xpath="."/>
                    </xs:key>
                </xs:element>""")

        self.assertIn("XPST0003", ctx.exception.message)

    def test_invalid_selector_node(self):
        with self.assertRaises(XMLSchemaParseError) as ctx:
            self.check_schema("""
                <xs:element name="root">
                    <xs:complexType>
                        <xs:sequence>
                            <xs:element ref="a" maxOccurs="unbounded"/>
                        </xs:sequence>
                        <xs:attribute name="foo" type="xs:string"/>
                    </xs:complexType>
                    <xs:key name="node_key">
                        <xs:selector xpath="attribute::*"/>
                        <xs:field xpath="."/>
                    </xs:key>
                </xs:element>
                <xs:element name="a" type="xs:string"/>""")

        self.assertEqual(ctx.exception.message,
                         'selector xpath expression can only select elements')

    def test_key_validation(self):
        schema = self.check_schema("""
            <xs:element name="root">
                <xs:complexType>
                    <xs:sequence>
                        <xs:element ref="a" maxOccurs="unbounded"/>
                    </xs:sequence>
                </xs:complexType>
                <xs:key name="node_key">
                    <xs:selector xpath="./a"/>
                    <xs:field xpath="."/>
                </xs:key>
            </xs:element>
            <xs:element name="a" type="xs:string"/>""")

        self.assertTrue(schema.is_valid('<root><a>alpha</a><a>beta</a></root>'))
        self.assertFalse(schema.is_valid('<root><a>alpha</a><a>alpha</a></root>'))

    def test_missing_key_field(self):
        schema = self.check_schema("""
            <xs:element name="root">
                <xs:complexType>
                    <xs:sequence>
                        <xs:element ref="a" maxOccurs="unbounded"/>
                    </xs:sequence>
                </xs:complexType>
                <xs:key name="node_key">
                    <xs:selector xpath="./a"/>
                    <xs:field xpath="b"/>
                </xs:key>
            </xs:element>
            <xs:element name="a" type="xs:string"/>""")

        with self.assertRaises(XMLSchemaValidationError) as ctx:
            schema.validate('<root><a>alpha</a><a>beta</a></root>')
        self.assertIn("missing key field 'b'", ctx.exception.reason)

    def test_keyref(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""")

        self.assertTrue(schema.identities['keyref1'].built)
        refer = schema.identities['keyref1'].refer
        schema.identities['keyref1'].build()
        self.assertTrue(schema.identities['keyref1'].built)
        self.assertIs(schema.identities['keyref1'].refer, refer)

    def test_missing_keyref_refer(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="unknown_key">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 1)
        self.assertEqual(schema.all_errors[0].message,
                         "key/unique identity constraint 'unknown_key' is missing")

        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="x:key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 1)
        self.assertEqual(schema.all_errors[0].message,
                         "prefix 'x' not found in namespace map")

        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 2 if schema.XSD_VERSION == '1.0' else 1)
        self.assertEqual(schema.all_errors[-1].message, "missing required attribute 'refer'")

    def test_wrong_kind_keyref_refer(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="keyref2">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
              <xs:keyref name="keyref2" refer="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 1)
        self.assertIn("reference to a non key/unique identity constraint",
                      schema.all_errors[0].message)

    def test_cardinality_mismatch_keyref_refer(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""", validation='lax')

        self.assertEqual(len(schema.all_errors), 1)
        self.assertIn("field cardinality mismatch", schema.all_errors[0].message)

    def test_build(self):
        schema = self.check_schema("""
            <xs:element name="root">
                <xs:complexType>
                    <xs:sequence>
                        <xs:element ref="a" maxOccurs="unbounded"/>
                    </xs:sequence>
                    <xs:attribute name="b" type="xs:string"/>
                </xs:complexType>
                <xs:key name="key1">
                    <xs:selector xpath=".//root/a"/>
                    <xs:field xpath="@b"/>
                </xs:key>
            </xs:element>
            <xs:element name="a">
                <xs:complexType>
                    <xs:attribute name="b" type="xs:string"/>
                </xs:complexType>
            </xs:element>""")

        self.assertIn(schema.elements['a'], schema.identities['key1'].elements)
        self.assertEqual(len(schema.identities['key1'].elements), 1)
        self.assertTrue(all(
            isinstance(x, FieldValueSelector)
            for x in schema.identities['key1'].elements[schema.elements['a']]
        ))

    def test_identity_counter(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""")

        elem = ElementTree.XML('<primary_key>3</primary_key>')
        counter = IdentityCounter(schema.identities['key1'], elem)
        self.assertEqual(repr(counter), 'IdentityCounter()')
        self.assertIsNone(counter.increase(('1',)))
        self.assertIsNone(counter.increase(('2',)))
        self.assertEqual(repr(counter), "IdentityCounter({('1',): 1, ('2',): 1})")

        with self.assertRaises(ValueError) as ctx:
            counter.increase(('1',))
        self.assertIn("duplicated value ('1',)", str(ctx.exception))

        self.assertIsNone(counter.increase(('1',)))
        self.assertEqual(counter.counter[('1',)], 3)

    def test_keyref_counter(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>""")

        elem = ElementTree.XML('<primary_key>3</primary_key>')
        counter = KeyrefCounter(schema.identities['keyref1'], elem)
        self.assertIsNone(counter.increase(('1',)))
        self.assertIsNone(counter.increase(('2',)))
        self.assertIsNone(counter.increase(('1',)))
        self.assertIsNone(counter.increase(('3',)))
        self.assertIsNone(counter.increase(('3',)))
        self.assertIsNone(counter.increase(('4',)))

        with self.assertRaises(KeyError):
            list(counter.iter_errors(identities={}))

        key_counter = IdentityCounter(schema.identities['key1'], elem)
        self.assertIsNone(key_counter.increase(('1',)))
        self.assertIsNone(key_counter.increase('4'))

        identities = {schema.identities['key1']: key_counter}
        errors = list(counter.iter_errors(identities))
        self.assertEqual(len(errors), 2)
        self.assertIn("value ('2',) not found", str(errors[0]))
        self.assertIn("value ('3',) not found", str(errors[1]))
        self.assertIn("(2 times)", str(errors[1]))

    def test_key_multiple_values__issue_418(self):
        xsd_file = self.casepath('issues/issue_418/issue_418.xsd')
        schema = self.schema_class(xsd_file)

        xml_file = self.casepath('issues/issue_418/issue_418.xml')
        self.assertIsNone(schema.validate(xml_file))

        xml_file = self.casepath('issues/issue_418/issue_418-invalid.xml')
        with self.assertRaises(XMLSchemaValidationError) as ctx:
            schema.validate(xml_file)
        self.assertIn("field selects multiple values", str(ctx.exception))

    def test_sqlite_duplicate_key_with_low_threshold(self):
        schema = self.check_schema("""
            <xs:element name="root">
              <xs:complexType>
                <xs:sequence>
                  <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
                </xs:sequence>
              </xs:complexType>
              <xs:key name="a_key">
                <xs:selector xpath="a"/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""")

        valid_xml = '<root>' + ''.join(f'<a>v{i}</a>' for i in range(100)) + '</root>'
        invalid_xml = '<root><a>v0</a>' + \
            ''.join(f'<a>v{i}</a>' for i in range(1, 100)) + '<a>v0</a></root>'

        self.assertTrue(schema.is_valid(valid_xml, sqlite_identity_index_threshold=0))
        self.assertEqual(schema.is_valid(invalid_xml),
                         schema.is_valid(invalid_xml, sqlite_identity_index_threshold=0))
        errors = list(schema.iter_errors(invalid_xml,
                                         sqlite_identity_index_threshold=0))
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicated value ('v0',)", errors[0].reason)

    def test_sqlite_forward_keyref(self):
        schema = self.check_schema("""
            <xs:element name="root">
              <xs:complexType>
                <xs:sequence>
                  <xs:element name="ref" type="xs:string"
                              minOccurs="0" maxOccurs="unbounded"/>
                  <xs:element name="key" type="xs:string"
                              minOccurs="0" maxOccurs="unbounded"/>
                </xs:sequence>
              </xs:complexType>
              <xs:keyref name="ref_key" refer="primary_key">
                <xs:selector xpath="ref"/>
                <xs:field xpath="."/>
              </xs:keyref>
              <xs:key name="primary_key">
                <xs:selector xpath="key"/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>""")

        valid_xml = '<root><ref>x</ref><key>x</key></root>'
        invalid_xml = '<root><ref>x</ref><key>y</key></root>'

        self.assertTrue(schema.is_valid(valid_xml, sqlite_identity_index_threshold=0))
        self.assertFalse(schema.is_valid(invalid_xml, sqlite_identity_index_threshold=0))
        self.assertEqual(
            [str(e.reason) for e in schema.iter_errors(invalid_xml)],
            [str(e.reason) for e in
             schema.iter_errors(invalid_xml, sqlite_identity_index_threshold=0)]
        )

    def test_sqlite_nested_scopes_with_same_name(self):
        schema = self.check_schema("""
            <xs:element name="root">
              <xs:complexType>
                <xs:sequence>
                  <xs:element name="scope" minOccurs="0" maxOccurs="unbounded">
                    <xs:complexType>
                      <xs:sequence>
                        <xs:element name="b" type="xs:string" maxOccurs="unbounded"/>
                      </xs:sequence>
                    </xs:complexType>
                    <xs:unique name="same">
                      <xs:selector xpath="b"/>
                      <xs:field xpath="."/>
                    </xs:unique>
                  </xs:element>
                </xs:sequence>
              </xs:complexType>
            </xs:element>""")

        valid_xml = '<root><scope><b>x</b></scope><scope><b>x</b></scope></root>'
        invalid_xml = '<root><scope><b>x</b><b>x</b></scope></root>'

        self.assertTrue(schema.is_valid(valid_xml, sqlite_identity_index_threshold=0))
        self.assertFalse(schema.is_valid(invalid_xml, sqlite_identity_index_threshold=0))

    def test_sqlite_typed_key_equivalence(self):
        schema = self.check_schema("""
            <xs:element name="root" xmlns:p="urn:test">
              <xs:complexType>
                <xs:sequence>
                  <xs:element name="n" type="xs:decimal" maxOccurs="unbounded"/>
                  <xs:element name="q" type="xs:QName" maxOccurs="unbounded"/>
                  <xs:element name="t" type="xs:token" maxOccurs="unbounded"/>
                </xs:sequence>
              </xs:complexType>
              <xs:key name="numeric_key">
                <xs:selector xpath="n"/><xs:field xpath="."/>
              </xs:key>
              <xs:key name="qname_key">
                <xs:selector xpath="q"/><xs:field xpath="."/>
              </xs:key>
              <xs:key name="token_key">
                <xs:selector xpath="t"/><xs:field xpath="."/>
              </xs:key>
            </xs:element>""")

        xml_data = '<root xmlns:p="urn:test" xmlns:z="urn:test">' \
                   '<n>1.0</n><n>1.00</n>' \
                   '<q>p:a</q><q>z:a</q>' \
                   '<t> a   b </t><t>a b</t>' \
                   '</root>'
        self.assertFalse(schema.is_valid(xml_data))
        self.assertFalse(schema.is_valid(xml_data, sqlite_identity_index_threshold=0))

    def test_sqlite_temporary_database_cleanup(self):
        import os
        original_tmpdir = os.environ.get('TMPDIR')
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ['TMPDIR'] = tmpdir
            try:
                before = set(Path(tmpdir).iterdir())
                schema = self.check_schema("""
                    <xs:element name="root">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
                        </xs:sequence>
                      </xs:complexType>
                      <xs:key name="a_key">
                        <xs:selector xpath="a"/><xs:field xpath="."/>
                      </xs:key>
                    </xs:element>""")

                with self.assertRaises(XMLSchemaValidationError):
                    schema.validate('<root><a>x</a><a>x</a></root>',
                                    sqlite_identity_index_threshold=0)
                with self.assertRaises(ElementTree.ParseError):
                    schema.validate('<root><a>x</a>',
                                    sqlite_identity_index_threshold=0)
                self.assertTrue(schema.is_valid('<root><a>x</a></root>',
                                                sqlite_identity_index_threshold=0))
                self.assertEqual(set(Path(tmpdir).iterdir()), before)
            finally:
                if original_tmpdir is None:
                    os.environ.pop('TMPDIR', None)
                else:
                    os.environ['TMPDIR'] = original_tmpdir

    def test_sqlite_index_explicit_close(self):
        index = SQLiteIdentityIndex()
        path = Path(index.path)
        self.assertTrue(path.exists())
        index.close()
        self.assertFalse(path.exists())

    def test_sqlite_counter_stores_composite_key_and_location(self):
        schema = self.check_schema("""
            <xs:element name="root">
              <xs:complexType>
                <xs:sequence>
                  <xs:element name="a" maxOccurs="unbounded">
                    <xs:complexType>
                      <xs:attribute name="x" type="xs:string"/>
                      <xs:attribute name="y" type="xs:integer"/>
                    </xs:complexType>
                  </xs:element>
                </xs:sequence>
              </xs:complexType>
              <xs:key name="composite_key">
                <xs:selector xpath="a"/>
                <xs:field xpath="@x"/>
                <xs:field xpath="@y"/>
              </xs:key>
            </xs:element>""")

        with self.assertRaises(XMLSchemaValidationError) as ctx:
            schema.validate(
                '<root>\n'
                '<a x="p" y="1"/>\n'
                '<a x="q" y="2"/>\n'
                '<a x="p" y="1"/>\n'
                '</root>',
                sqlite_identity_index_threshold=0
            )
        self.assertIn("duplicated value ('p', 1)", ctx.exception.reason)

        index = SQLiteIdentityIndex()
        try:
            identity = schema.identities['composite_key']
            elem = ElementTree.Element('root')
            counter = identity.get_counter(elem, index, 0)
            counter.increase(('p', 1), ('sample.xml', 2))
            key = index.connection.execute(
                'SELECT tuple_key FROM identity_values').fetchone()[0]
            row = index.connection.execute(
                '''SELECT fields, source_url, source_line
                   FROM identity_values''').fetchone()
            self.assertTrue(key.startswith(b'K2:'))
            self.assertEqual(pickle.loads(row[0]), ('p', 1))
            self.assertEqual(row[1], 'sample.xml')
            self.assertEqual(row[2], 2)
        finally:
            index.close()


class TestXsd11Identities(TestXsdIdentities):

    schema_class = XMLSchema11

    def test_key_reference_definition(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>
            <xs:element name="secondary_key" type="xs:string">
              <xs:key ref="key1"/>
            </xs:element>""")

        key1 = schema.identities['key1']
        self.assertIsNot(schema.elements['secondary_key'].identities[0], key1)
        self.assertIs(schema.elements['secondary_key'].identities[0].ref, key1)

    def test_missing_key_reference_definition(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>
            <xs:element name="secondary_key" type="xs:string">
              <xs:key ref="unknown_key"/>
            </xs:element>""", validation='lax')

        errors = schema.all_errors
        self.assertEqual(len(errors), 1)
        self.assertEqual("unknown identity constraint 'unknown_key'", errors[0].message)

    def test_different_kind_key_reference_definition(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
            </xs:element>
            <xs:element name="secondary_key" type="xs:string">
              <xs:unique ref="key1"/>
            </xs:element>""", validation='lax')

        errors = schema.all_errors
        self.assertEqual(len(errors), 1)
        self.assertEqual("attribute 'ref' points to a different kind constraint",
                         errors[0].message)

    def test_keyref_reference_definition(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:key>
              <xs:keyref name="keyref1" refer="key1">
                <xs:selector xpath="."/>
                <xs:field xpath="."/>
              </xs:keyref>
            </xs:element>
            <xs:element name="secondary_key" type="xs:string">
              <xs:keyref ref="keyref1"/>
            </xs:element>""")

        self.assertIsNot(schema.elements['secondary_key'].identities[0],
                         schema.elements['primary_key'].identities[-1])
        self.assertIs(schema.elements['secondary_key'].identities[0].ref,
                      schema.elements['primary_key'].identities[-1])

    def test_xsi_type_substitution__issue_446(self):
        xsd_file = self.casepath('features/substitutions/words.xsd')
        schema = self.schema_class(xsd_file)

        xml_data = '<words><word>foo</word></words>'
        self.assertTrue(schema.is_valid(xml_data))
        xml_data = '<words><word>foo</word><word>foo</word></words>'
        self.assertFalse(schema.is_valid(xml_data))

        xml_file = self.casepath('features/substitutions/words1.xml')
        self.assertTrue(schema.is_valid(xml_file))
        xml_file = self.casepath('features/substitutions/words2.xml')
        self.assertTrue(schema.is_valid(xml_file))
        xml_file = self.casepath('features/substitutions/words3.xml')
        self.assertFalse(schema.is_valid(xml_file))
        xml_file = self.casepath('features/substitutions/words4.xml')
        self.assertFalse(schema.is_valid(xml_file))

    def test_selector_default_namespace(self):
        schema = self.check_schema("""
            <xs:element name="primary_key" type="xs:string">
              <xs:key name="key1">
                <xs:selector xpath="." xpathDefaultNamespace="https://xmlschema.test/ns"/>
                <xs:field xpath="." xpathDefaultNamespace="##targetNamespace"/>
              </xs:key>
            </xs:element>""")

        self.assertEqual(schema.identities['key1'].selector.xpath_default_namespace,
                         'https://xmlschema.test/ns')
        self.assertEqual(schema.identities['key1'].fields[0].xpath_default_namespace, '')


if __name__ == '__main__':
    from xmlschema.testing import run_xmlschema_tests
    run_xmlschema_tests('identities')
