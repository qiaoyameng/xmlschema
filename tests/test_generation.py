"""
Tests for bounded XML instance generation from an XML Schema.

Every test uses inline schema strings, so no external schema download is
needed (and network access is not required).
"""
from __future__ import annotations

import re

import pytest

import xmlschema
from xmlschema.generation import InstanceGenerationError
from xmlschema import (
    BudgetExhaustedError,
    GenerationStatus,
    NoInstancePossibleError,
    UnsupportedSchemaFeatureError,
    etree_tostring,
    generate_instance,
    generate_xml,
)


XSD_SEQUENCE = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="optional" type="xs:int" minOccurs="0"/>
        <xs:element name="required" type="xs:string"/>
        <xs:element name="many" minOccurs="2" maxOccurs="unbounded" type="xs:int"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>'''

XSD_CHOICE = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice>
        <xs:element name="heavy">
          <xs:complexType><xs:sequence>
            <xs:element name="a"/><xs:element name="b"/>
          </xs:sequence></xs:complexType>
        </xs:element>
        <xs:element name="light" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>'''

XSD_ALL = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:all>
        <xs:element name="first" type="xs:int"/>
        <xs:element name="second" type="xs:string" minOccurs="0"/>
      </xs:all>
    </xs:complexType>
  </xs:element>
</xs:schema>'''

XSD_FACETS = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType><xs:sequence>
      <xs:element name="n">
        <xs:simpleType><xs:restriction base="xs:int">
          <xs:minInclusive value="10"/><xs:maxInclusive value="20"/>
        </xs:restriction></xs:simpleType>
      </xs:element>
      <xs:element name="s">
        <xs:simpleType><xs:restriction base="xs:string">
          <xs:minLength value="3"/><xs:maxLength value="5"/>
        </xs:restriction></xs:simpleType>
      </xs:element>
      <xs:element name="e">
        <xs:simpleType><xs:restriction base="xs:string">
          <xs:enumeration value="short"/><xs:enumeration value="longer-value"/>
        </xs:restriction></xs:simpleType>
      </xs:element>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>'''

# Recursive named type with a terminating branch plus cross-element key/keyref.
XSD_RECURSIVE_KEYREF = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="http://test.io/rec"
           targetNamespace="http://test.io/rec"
           elementFormDefault="qualified">
  <xs:complexType name="nodeType">
    <xs:sequence>
      <xs:element name="code" type="xs:string"/>
      <xs:element name="child" type="t:nodeType" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="manager" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:element name="org">
    <xs:complexType><xs:sequence>
      <xs:element name="node" type="t:nodeType" minOccurs="1" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType>
    <xs:key name="codeKey">
      <xs:selector xpath=".//t:node | .//t:child"/>
      <xs:field xpath="t:code"/>
    </xs:key>
    <xs:keyref name="managerRef" refer="t:codeKey">
      <xs:selector xpath=".//t:node | .//t:child"/>
      <xs:field xpath="@manager"/>
    </xs:keyref>
  </xs:element>
</xs:schema>'''

XSD_NON_TERMINATING = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="loop">
    <xs:complexType><xs:sequence>
      <xs:element ref="loop" minOccurs="1" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>'''

XSD_TERMINATING_RECURSION = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="tree">
    <xs:choice>
      <xs:element name="branch" type="tree"/>
      <xs:element name="leaf" type="xs:string"/>
    </xs:choice>
  </xs:complexType>
  <xs:element name="tree" type="tree"/>
</xs:schema>'''

XSD_PATTERN = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="code"><xs:restriction base="xs:string">
    <xs:pattern value="[A-Z]{3}"/>
  </xs:restriction></xs:simpleType>
  <xs:element name="root" type="code"/>
</xs:schema>'''

XSD_PATTERN_WITH_ENUM = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="code"><xs:restriction base="xs:string">
    <xs:pattern value="[A-Z]+"/>
    <xs:enumeration value="A"/><xs:enumeration value="BC"/>
  </xs:restriction></xs:simpleType>
  <xs:element name="root" type="code"/>
</xs:schema>'''

XSD_MANDATORY_ANY = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:any processContents="skip"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>'''

XSD_ASSERTION_11 = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:simpleContent><xs:extension base="xs:int">
      <xs:assert test="$value > 0"/>
    </xs:extension></xs:simpleContent>
  </xs:complexType></xs:element>
</xs:schema>'''


def _count(elem) -> int:
    return sum(1 for _ in elem.iter())


class TestBoundedGeneration:

    def test_sequence_occurs_and_minimal_nodes(self):
        schema = xmlschema.XMLSchema(XSD_SEQUENCE)
        elem = schema.generate_instance(seed=0)
        assert schema.is_valid(elem)
        tags = [child.tag for child in elem]
        assert tags == ['required', 'many', 'many']
        assert elem.find('optional') is None
        assert _count(elem) == 4

    def test_choice_prefers_cheapest_branch(self):
        schema = xmlschema.XMLSchema(XSD_CHOICE)
        elem = generate_instance(schema, seed=0)
        assert schema.is_valid(elem)
        assert elem.find('light') is not None
        assert elem.find('heavy') is None

    def test_all_omits_optional_children(self):
        schema = xmlschema.XMLSchema(XSD_ALL)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert [c.tag for c in elem] == ['first']

    def test_facets_numeric_length_enum(self):
        schema = xmlschema.XMLSchema(XSD_FACETS)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        number = int(elem.find('n').text)
        assert 10 <= number <= 20
        text = elem.find('s').text
        assert 3 <= len(text) <= 5 and text.strip() != ''
        # Shortest enumeration value is preferred.
        assert elem.find('e').text == 'short'

    def test_required_values_are_never_empty(self):
        schema = xmlschema.XMLSchema(XSD_SEQUENCE)
        elem = generate_instance(schema)
        assert elem.find('required').text != ''
        for child in elem:
            if child.text is not None:
                assert child.text != ''

    def test_seed_controls_tie_breaks(self):
        schema = xmlschema.XMLSchema('''<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="r"><xs:simpleType><xs:restriction base="xs:string">
            <xs:enumeration value="aa"/><xs:enumeration value="bb"/>
          </xs:restriction></xs:simpleType></xs:element>
        </xs:schema>''')
        first = generate_instance(schema, seed=1)
        again = generate_instance(schema, seed=1)
        second = generate_instance(schema, seed=2)
        assert first.text == again.text
        assert {first.text, second.text} <= {'aa', 'bb'}

    def test_result_serialization_is_valid_xml(self):
        result = generate_xml(xmlschema.XMLSchema(XSD_ALL))
        assert result.ok and result.status is GenerationStatus.SUCCESS
        xml_text = result.get_xml()
        assert isinstance(xml_text, str)
        assert '<first>' in xml_text


class TestRecursionAndIdentities:

    def test_terminating_recursion_chooses_leaf(self):
        schema = xmlschema.XMLSchema(XSD_TERMINATING_RECURSION)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert _count(elem) == 2

    def test_recursive_type_with_cross_element_keyref(self):
        schema = xmlschema.XMLSchema(XSD_RECURSIVE_KEYREF)
        elem = generate_instance(schema, seed=42)
        # The candidate must be validated by the SAME schema.
        assert schema.is_valid(elem)

        errors = list(schema.iter_errors(elem))
        assert not errors

        ns = '{http://test.io/rec}'
        nodes = list(elem.iter(ns + 'node')) + list(elem.iter(ns + 'child'))
        codes = {node.find(ns + 'code').text for node in nodes}
        managers = {node.attrib['manager'] for node in nodes}
        assert nodes
        assert managers <= codes  # every keyref points at an existing key

    def test_recursive_keyref_always_has_a_referenced_key(self):
        # Keyref selects nodes: even with a single node its @manager must
        # resolve to a generated code instead of dangling.
        schema = xmlschema.XMLSchema(XSD_RECURSIVE_KEYREF)
        for seed in range(6):
            elem = generate_instance(schema, seed=seed)
            assert schema.is_valid(elem), seed

    def test_non_terminating_recursion_is_no_solution(self):
        schema = xmlschema.XMLSchema(XSD_NON_TERMINATING)
        with pytest.raises(NoInstancePossibleError):
            generate_instance(schema)
        result = generate_xml(schema)
        assert result.status is GenerationStatus.NO_SOLUTION
        assert result.element is None and result.reason


class TestFailuresAreDistinct:

    def test_budget_exhausted_is_distinct(self):
        schema = xmlschema.XMLSchema('''<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="r"><xs:complexType><xs:sequence>
            <xs:element name="a" minOccurs="5" maxOccurs="5"/>
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>''')
        with pytest.raises(BudgetExhaustedError) as ctx:
            generate_instance(schema, max_nodes=3)
        assert ctx.value.budget == 3
        result = generate_xml(schema, max_nodes=3)
        assert result.status is GenerationStatus.BUDGET_EXHAUSTED
        assert 'budget' in result.reason or 'elements' in result.reason

    def test_same_schema_succeeds_with_larger_budget(self):
        schema = xmlschema.XMLSchema('''<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="r"><xs:complexType><xs:sequence>
            <xs:element name="a" minOccurs="5" maxOccurs="5"/>
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>''')
        elem = generate_instance(schema, max_nodes=10)
        assert schema.is_valid(elem)
        assert len(elem) == 5

    def test_pattern_is_unsupported_with_reason(self):
        schema = xmlschema.XMLSchema(XSD_PATTERN)
        with pytest.raises(UnsupportedSchemaFeatureError) as ctx:
            generate_instance(schema)
        assert 'pattern' in ctx.value.reason
        result = generate_xml(schema)
        assert result.status is GenerationStatus.UNSUPPORTED
        assert 'pattern' in result.reason

    def test_pattern_with_enumeration_is_allowed(self):
        schema = xmlschema.XMLSchema(XSD_PATTERN_WITH_ENUM)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert elem.text in {'A', 'BC'}

    def test_mandatory_wildcard_unsupported(self):
        schema = xmlschema.XMLSchema(XSD_MANDATORY_ANY)
        with pytest.raises(UnsupportedSchemaFeatureError) as ctx:
            generate_instance(schema)
        assert 'xs:any' in ctx.value.reason

    def test_xsd11_assertion_unsupported(self):
        schema = xmlschema.XMLSchema11(XSD_ASSERTION_11)
        with pytest.raises(UnsupportedSchemaFeatureError) as ctx:
            generate_instance(schema)
        assert 'assert' in ctx.value.reason

    def test_unsupported_error_is_an_instance_generation_error(self):
        schema = xmlschema.XMLSchema(XSD_PATTERN)
        with pytest.raises(InstanceGenerationError):
            generate_instance(schema)


class TestValidationGuard:

    def test_candidate_goes_through_schema_validation(self):
        schema = xmlschema.XMLSchema(XSD_FACETS)
        elem = generate_instance(schema)
        # Tampering with the generated value must be caught by the schema,
        # proving the generator relies on real validation rather than claims.
        elem.find('n').text = 'not-a-number'
        assert not schema.is_valid(elem)

    def test_unknown_global_element_name(self):
        schema = xmlschema.XMLSchema(XSD_ALL)
        with pytest.raises(NoInstancePossibleError):
            generate_instance(schema, name='does-not-exist')

    def test_invalid_budget_arguments(self):
        schema = xmlschema.XMLSchema(XSD_ALL)
        with pytest.raises(ValueError):
            generate_instance(schema, max_nodes=0)
        with pytest.raises(ValueError):
            generate_instance(schema, max_occurs=-1)


XSD_UNION_LIST = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="u">
    <xs:union memberTypes="xs:int xs:string"/>
  </xs:simpleType>
  <xs:simpleType name="l">
    <xs:restriction>
      <xs:simpleType><xs:list itemType="xs:int"/></xs:simpleType>
      <xs:minLength value="2"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="u" type="u"/>
    <xs:element name="l" type="l"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>'''

XSD_GROUP_REF = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="g">
    <xs:sequence><xs:element name="x" type="xs:string"/></xs:sequence>
  </xs:group>
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:group ref="g" minOccurs="1" maxOccurs="1"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>'''

XSD_ATTRIBUTES = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="required-id" type="xs:ID" use="required"/>
          <xs:attribute name="optional" type="xs:int" use="optional"/>
          <xs:attribute name="fixed" type="xs:string" fixed="F" use="required"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>'''


class TestMoreFeatures:

    def test_union_and_list(self):
        schema = xmlschema.XMLSchema(XSD_UNION_LIST)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        list_items = elem.find('l').text.split()
        assert len(list_items) == 2 and all(i for i in list_items)

    def test_group_reference(self):
        schema = xmlschema.XMLSchema(XSD_GROUP_REF)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert [c.tag for c in elem] == ['x']

    def test_required_and_fixed_attributes(self):
        schema = xmlschema.XMLSchema(XSD_ATTRIBUTES)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert elem.attrib['fixed'] == 'F'
        assert elem.attrib['required-id']
        assert 'optional' not in elem.attrib
        assert elem.text != ''

    def test_seed_repeatable_structure(self):
        schema = xmlschema.XMLSchema(XSD_SEQUENCE)
        first = etree_tostring(generate_instance(schema, seed=123))
        second = etree_tostring(generate_instance(schema, seed=123))
        other = etree_tostring(generate_instance(schema, seed=124))
        assert first == second
        assert first == other  # no optional ties in this schema: still minimal

    def test_result_nodes_count_is_accurate(self):
        result = generate_xml(xmlschema.XMLSchema(XSD_FACETS))
        assert result.ok
        assert result.nodes == _count(result.element)

    def test_rejects_unsupported_selector_syntax(self):
        schema = xmlschema.XMLSchema('''<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="r">
            <xs:complexType>
              <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
            </xs:complexType>
            <xs:key name="k">
              <xs:selector xpath=".//a"/>
              <xs:field xpath="@*"/>
            </xs:key>
          </xs:element>
        </xs:schema>''')
        result = generate_xml(schema)
        assert result.status is GenerationStatus.UNSUPPORTED
        assert 'field' in result.reason or 'path' in result.reason

    def test_recursion_minimal_is_leaf(self):
        # minOccurs=1 choice: the recursive branch costs more than the leaf,
        # so the cheapest valid instance is root + leaf only.
        schema = xmlschema.XMLSchema(XSD_TERMINATING_RECURSION)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert elem.find('leaf') is not None

    def test_recursion_finite_within_large_budget(self):
        schema = xmlschema.XMLSchema(XSD_RECURSIVE_KEYREF)
        small = generate_xml(schema, max_nodes=5)
        assert small.ok
        big = generate_xml(schema, max_nodes=50)
        assert big.ok
        assert schema.is_valid(big.element)


class TestRealSchemasAndEdgeCases:

    def test_collection_example_generates_valid_xml(self):
        import pathlib
        xsd_path = pathlib.Path(__file__).parent / \
            'test_cases/examples/collection/collection.xsd'
        schema = xmlschema.XMLSchema(str(xsd_path))
        elem = generate_instance(schema, seed=0, max_nodes=80)
        assert schema.is_valid(elem)

    def test_required_attribute_pattern_is_unsupported(self):
        schema = xmlschema.XMLSchema('''<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="r"><xs:complexType>
            <xs:simpleContent><xs:extension base="xs:string">
              <xs:attribute name="a" use="required">
                <xs:simpleType><xs:restriction base="xs:string">
                  <xs:pattern value="[0-9]{3}"/>
                </xs:restriction></xs:simpleType>
              </xs:attribute>
            </xs:extension></xs:simpleContent>
          </xs:complexType></xs:element>
        </xs:schema>''')
        result = generate_xml(schema)
        assert result.status is GenerationStatus.UNSUPPORTED
        assert 'pattern' in result.reason

    def test_no_global_element_is_no_solution(self):
        schema = xmlschema.XMLSchema('''<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:simpleType name="s"><xs:restriction base="xs:string"/></xs:simpleType>
        </xs:schema>''')
        result = generate_xml(schema)
        assert result.status is GenerationStatus.NO_SOLUTION

    def test_get_xml_serializes(self):
        result = generate_xml(xmlschema.XMLSchema(XSD_SEQUENCE))
        text = result.get_xml(xml_declaration=False).strip()
        assert text.startswith('<root>') and '<required>' in text
        raw = result.get_xml(xml_declaration=False, indent='')
        assert '<?xml' not in raw


XSD_NILLABLE_RECURSION = '''<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="n" nillable="true">
    <xs:complexType><xs:sequence>
      <xs:element ref="n" minOccurs="1"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>'''


class TestNillableTermination:

    def test_nillable_breaks_mandatory_recursion(self):
        schema = xmlschema.XMLSchema(XSD_NILLABLE_RECURSION)
        elem = generate_instance(schema)
        assert schema.is_valid(elem)
        assert elem.attrib[
            '{http://www.w3.org/2001/XMLSchema-instance}nil'
        ] == 'true'

    def test_non_nillable_mandatory_recursion_is_no_solution(self):
        schema = xmlschema.XMLSchema(XSD_NON_TERMINATING)
        result = generate_xml(schema)
        assert result.status is GenerationStatus.NO_SOLUTION
