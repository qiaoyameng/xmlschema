from xml.etree import ElementTree as ET
from decimal import Decimal

from xmlschema import XMLSchema, XMLSchema11
from xmlschema.instances import (
    BUDGET_EXHAUSTED,
    NO_SOLUTION,
    UNSUPPORTED,
    VALID,
    generate_xml_instance,
)


RECURSIVE_XSD = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="node" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="node" minOccurs="0" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="id" type="xs:string" />
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
              <xs:element name="id" type="xs:string" />
              <xs:element name="stop" minOccurs="0">
                <xs:simpleType>
                  <xs:restriction base="xs:int">
                    <xs:minInclusive value="5" />
                    <xs:maxInclusive value="20" />
                  </xs:restriction>
                </xs:simpleType>
              </xs:element>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="nodeKey">
      <xs:selector xpath="node/node" />
      <xs:field xpath="id" />
    </xs:key>
    <xs:keyref name="nodeRef" refer="nodeKey">
      <xs:selector xpath="node" />
      <xs:field xpath="id" />
    </xs:keyref>
  </xs:element>
</xs:schema>
"""


FACET_XSD = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="doc">
    <xs:complexType>
      <xs:all>
        <xs:element name="count">
          <xs:simpleType>
            <xs:restriction base="xs:nonNegativeInteger">
              <xs:minInclusive value="1" />
              <xs:maxInclusive value="8" />
            </xs:restriction>
          </xs:simpleType>
        </xs:element>
        <xs:element name="code">
          <xs:simpleType>
            <xs:restriction base="xs:string">
              <xs:minLength value="3" />
              <xs:maxLength value="6" />
              <xs:enumeration value="abc" />
              <xs:enumeration value="def" />
            </xs:restriction>
          </xs:simpleType>
        </xs:element>
        <xs:element name="ratio">
          <xs:simpleType>
            <xs:restriction base="xs:decimal">
              <xs:totalDigits value="4" />
              <xs:fractionDigits value="1" />
            </xs:restriction>
          </xs:simpleType>
        </xs:element>
      </xs:all>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


CHOICE_XSD = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="doc">
    <xs:complexType>
      <xs:choice>
        <xs:element name="small" type="xs:string" />
        <xs:element name="large">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="a" type="xs:string" />
              <xs:element name="b" type="xs:string" />
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def test_generates_small_valid_recursive_instance_with_cross_element_keyref():
    schema = XMLSchema(RECURSIVE_XSD)
    result = generate_xml_instance(schema, seed=3, max_nodes=20)

    assert result.status == VALID
    assert result.element is not None
    assert schema.is_valid(result.element)
    nodes = result.element.findall("./node") + result.element.findall("./node/node")
    assert [node.findtext("id") for node in nodes]
    assert nodes[0].findtext("id") == nodes[1].findtext("id")
    assert result.element.find("./node/stop") is None


def test_sequence_choice_all_and_common_facets_are_supported():
    schema = XMLSchema(FACET_XSD)
    result = generate_xml_instance(schema, seed=1, max_nodes=10)

    assert result.status == VALID
    assert schema.is_valid(result.element)
    assert 1 <= int(result.element.findtext("count")) <= 8
    assert result.element.findtext("code") in {"abc", "def"}
    ratio = Decimal(result.element.findtext("ratio"))
    assert ratio.as_tuple().exponent == -1
    assert len(result.xml() or "") > 0


def test_choice_prefers_terminating_small_branch():
    schema = XMLSchema(CHOICE_XSD)
    result = generate_xml_instance(schema, seed=0, max_nodes=3)

    assert result.status == VALID
    assert result.element.find("small") is not None
    assert result.element.find("large") is None
    assert schema.is_valid(result.element)


def test_fixed_seed_is_deterministic():
    schema = XMLSchema(RECURSIVE_XSD)
    first = generate_xml_instance(schema, seed=7, max_nodes=20)
    second = generate_xml_instance(schema, seed=7, max_nodes=20)

    assert first.xml() == second.xml()


def test_budget_exhaustion_is_distinct_from_no_solution():
    schema = XMLSchema(RECURSIVE_XSD)
    result = generate_xml_instance(schema, seed=0, max_nodes=2)

    assert result.status == BUDGET_EXHAUSTED
    assert "budget" in (result.reason or "")


def test_impossible_required_choice_is_reported_as_no_solution():
    xsd = """
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="doc">
        <xs:complexType>
          <xs:choice>
            <xs:element name="empty">
              <xs:simpleType>
                <xs:restriction base="xs:string">
                  <xs:length value="0" />
                </xs:restriction>
              </xs:simpleType>
            </xs:element>
          </xs:choice>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
    result = generate_xml_instance(XMLSchema11(xsd), max_nodes=10)

    assert result.status == NO_SOLUTION
    assert result.element is None


def test_pattern_constraint_is_rejected_with_reason():
    xsd = """
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="doc">
        <xs:simpleType>
          <xs:restriction base="xs:string">
            <xs:pattern value="[0-9]+" />
          </xs:restriction>
        </xs:simpleType>
      </xs:element>
    </xs:schema>
    """
    result = generate_xml_instance(XMLSchema(xsd), max_nodes=10)

    assert result.status == UNSUPPORTED
    assert "pattern" in (result.reason or "")
    assert result.element is None


def test_xsd_11_assert_is_rejected_with_reason():
    xsd = """
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
               xmlns:vc="http://www.w3.org/2007/XMLSchema-versioning"
               vc:minVersion="1.1">
      <xs:element name="doc">
        <xs:complexType>
          <xs:simpleContent>
            <xs:extension base="xs:string">
              <xs:assert test="string-length(.) > 1" />
            </xs:extension>
          </xs:simpleContent>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
    result = generate_xml_instance(XMLSchema11(xsd), max_nodes=10)

    assert result.status == UNSUPPORTED
    assert "assert" in (result.reason or "")
    assert result.element is None


def test_required_values_are_not_blank_by_default():
    xsd = """
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="doc">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="code" type="xs:string" />
          </xs:sequence>
          <xs:attribute name="kind" type="xs:string" use="required" />
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
    schema = XMLSchema(xsd)
    result = generate_xml_instance(schema, seed=5, max_nodes=10)

    assert result.status == VALID
    assert result.element.findtext("code")
    assert result.element.attrib["kind"]
