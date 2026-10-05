import copy
from pathlib import Path
from xml.etree import ElementTree

from xmlschema import XMLSchema, normalize_simple_values
from xmlschema.utils.etree import etree_tostring


SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns="http://example.test/ns" targetNamespace="http://example.test/ns"
    elementFormDefault="qualified">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="flag" type="xs:boolean"/>
        <xs:element name="amount" type="xs:decimal"/>
        <xs:element name="count" type="xs:integer"/>
        <xs:element name="when" type="xs:dateTime"/>
        <xs:element name="codes">
          <xs:simpleType>
            <xs:list itemType="xs:integer"/>
          </xs:simpleType>
        </xs:element>
        <xs:element name="choice" type="ambiguous"/>
        <xs:element name="ref" type="xs:QName"/>
        <xs:element name="mixed">
          <xs:complexType mixed="true">
            <xs:sequence>
              <xs:element name="b" type="xs:boolean" minOccurs="0"/>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
      <xs:attribute name="mark" type="xs:boolean"/>
    </xs:complexType>
  </xs:element>
  <xs:simpleType name="ambiguous">
    <xs:union memberTypes="xs:integer xs:boolean"/>
  </xs:simpleType>
</xs:schema>"""

XML = """<n:root xmlns:n="http://example.test/ns"
         xmlns:alt="http://example.test/alt" mark="1">
  <n:flag>1</n:flag>
  <n:amount>012.3400</n:amount>
  <n:count>+0042</n:count>
  <n:when>2026-10-06T09:30:00.000+00:00</n:when>
  <n:codes> 01  020 </n:codes>
  <n:choice>1</n:choice>
  <n:ref>alt:thing</n:ref>
  <n:mixed>before <n:b>1</n:b> after</n:mixed>
</n:root>"""


def test_simple_value_normalization_and_idempotence():
    schema = XMLSchema(SCHEMA)
    source = ElementTree.fromstring(XML)
    before = copy.deepcopy(source)
    namespaces = {'alt': 'http://example.test/alt'}

    normalized, rewrites = normalize_simple_values(source, schema, namespaces)

    assert etree_tostring(source) == etree_tostring(before)
    assert normalized is not source
    assert normalized.find('{http://example.test/ns}flag').text == 'true'
    assert normalized.find('{http://example.test/ns}amount').text == '12.34'
    assert normalized.find('{http://example.test/ns}count').text == '42'
    assert normalized.find('{http://example.test/ns}when').text == \
        '2026-10-06T09:30:00Z'
    assert normalized.find('{http://example.test/ns}codes').text == '1 20'
    assert normalized.attrib['mark'] == 'true'

    assert normalized.find('{http://example.test/ns}choice').text == '1'
    assert normalized.find('{http://example.test/ns}ref').text == 'alt:thing'
    mixed = normalized.find('{http://example.test/ns}mixed')
    assert mixed.text == 'before '
    assert mixed.find('{http://example.test/ns}b').text == 'true'

    normalized_again, rewrites_again = normalize_simple_values(
        normalized, schema, namespaces
    )
    assert rewrites_again == []
    assert etree_tostring(normalized_again) == etree_tostring(normalized)
    assert len(rewrites) == 7
    assert {item.old_value for item in rewrites} == {
        '1', '012.3400', '+0042',
        '2026-10-06T09:30:00.000+00:00', ' 01  020 ',
    }
    assert any(item.xsd_type == 'list(xs:integer)' for item in rewrites)


def test_dynamic_type_and_schema_are_not_modified():
    schema_text = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="value" type="xs:anySimpleType"/>
      <xs:simpleType name="flag">
        <xs:restriction base="xs:boolean"/>
      </xs:simpleType>
    </xs:schema>"""
    xml = '<value xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ' \
          'xsi:type="flag">1</value>'
    schema = XMLSchema(schema_text)
    source = ElementTree.fromstring(xml)
    xsd_element = schema.elements['value']
    before = set(xsd_element.xsi_types)

    normalized, rewrites = normalize_simple_values(source, schema)

    assert normalized.text == 'true'
    assert rewrites[0].xsd_type == 'flag'
    assert source.text == '1'
    assert xsd_element.xsi_types == before


def test_signed_reference_target_is_excluded():
    dsig_schema_path = str(Path('xmlschema/schemas/DSIG/xmldsig-core-schema.xsd'))
    schema_text = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
        xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
      <xs:import namespace="http://www.w3.org/2000/09/xmldsig#"/>
      <xs:element name="root">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="data">
              <xs:complexType>
                <xs:simpleContent>
                  <xs:extension base="xs:string">
                    <xs:attribute name="Id" type="xs:ID"/>
                    <xs:attribute name="mark" type="xs:boolean"/>
                  </xs:extension>
                </xs:simpleContent>
              </xs:complexType>
            </xs:element>
            <xs:element ref="ds:Signature"/>
          </xs:sequence>
          <xs:attribute name="rootMark" type="xs:boolean"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>"""
    xml = """<root xmlns:ds="http://www.w3.org/2000/09/xmldsig#" rootMark="1">
      <data Id="signed" mark="1">1</data>
      <ds:Signature>
        <ds:SignedInfo>
          <ds:CanonicalizationMethod Algorithm="x"/>
          <ds:SignatureMethod Algorithm="x"/>
          <ds:Reference URI="#signed">
            <ds:DigestMethod Algorithm="x"/>
            <ds:DigestValue>YWE=</ds:DigestValue>
          </ds:Reference>
        </ds:SignedInfo>
        <ds:SignatureValue>YWE=</ds:SignatureValue>
      </ds:Signature>
    </root>"""
    schema = XMLSchema(schema_text, locations={
        'http://www.w3.org/2000/09/xmldsig#': dsig_schema_path,
    }, use_location_hints=False)

    normalized, rewrites = normalize_simple_values(ElementTree.fromstring(xml), schema)
    data = normalized.find('data')

    assert normalized.attrib['rootMark'] == 'true'
    assert data.attrib['mark'] == '1'
    assert data.text == '1'
    assert [(item.location, item.new_value) for item in rewrites] == [
        ('attribute(rootMark)', 'true'),
    ]
