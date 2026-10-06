"""Round-trip, identity, state-distinction and transaction tests."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

from xsd2sqlite import (
    connect,
    create_plan,
    create_schema,
    import_document,
    rebuild_document,
)
from xsd2sqlite.importer import ImportError_ as XsdImportError

EXAMPLES = os.path.join(os.path.dirname(__file__), "..",
                        "xsd2sqlite", "examples")
XSD = os.path.join(EXAMPLES, "orders.xsd")
DOC_FULL = os.path.join(EXAMPLES, "order_full.xml")
DOC_MIN = os.path.join(EXAMPLES, "order_minimal.xml")


def _normalize(xml_bytes):
    """Parse and dump XML without inter-element whitespace for comparison."""
    root = ET.fromstring(xml_bytes)

    def strip(elem):
        if elem.text is not None and not elem.text.strip():
            elem.text = None
        if elem.tail is not None and not elem.tail.strip():
            elem.tail = None
        for child in elem:
            strip(child)

    strip(root)
    return ET.canonicalize(ET.tostring(root, encoding="unicode"),
                           with_comments=False)


class PlanTests(unittest.TestCase):
    def test_plan_is_importable_and_lists_tables(self):
        plan = create_plan(XSD)
        self.assertTrue(plan.importable, [i.message for i in plan.errors])
        table_names = {t.name for t in plan.tables}
        self.assertIn("order", table_names)
        self.assertIn("item", table_names)
        root = next(t for t in plan.tables if t.root)
        self.assertEqual(root.name, "order")
        scalar_names = {s.element_qname[1] for s in root.scalars}
        self.assertEqual(
            scalar_names,
            {"customer", "note", "currency", "gift", "coupon"})
        # repeated item lives in its own table, not as scalar columns
        item_node = next(n for n in root.children
                         if n.element_qname[1] == "item")
        self.assertEqual(item_node.max_occurs, -1)
        self.assertEqual(item_node.table, "item")
        # actual choice branches are registered as children and grouped
        branches = {n.element_qname[1]
                    for n in root.children if n.choice_group}
        self.assertEqual(branches, {"invoiceNo", "collectOnDelivery"})

    def test_plan_reports_wildcard(self):
        xsd = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root">
            <xs:complexType>
              <xs:sequence>
                <xs:any maxOccurs="unbounded"/>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        plan = create_plan(xsd)
        self.assertFalse(plan.importable)
        self.assertTrue(any("wildcard" in i.message for i in plan.errors))

    def test_plan_reports_mixed_content(self):
        xsd = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="p">
            <xs:complexType mixed="true">
              <xs:sequence>
                <xs:element name="b" type="xs:string"/>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        plan = create_plan(xsd)
        self.assertFalse(plan.importable)
        self.assertTrue(any("mixed" in i.message for i in plan.errors))

    def test_plan_reports_anytype_and_repeating_choice(self):
        xsd_any = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root"/>
        </xs:schema>"""
        self.assertFalse(create_plan(xsd_any).importable)
        xsd_rep = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root">
            <xs:complexType>
              <xs:sequence>
                <xs:choice maxOccurs="unbounded">
                  <xs:element name="a" type="xs:string"/>
                  <xs:element name="b" type="xs:string"/>
                </xs:choice>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        errors = [i.message for i in create_plan(xsd_rep).errors]
        self.assertTrue(any("repeating xs:choice" in m for m in errors))

    def test_plan_reports_any_attribute_and_simple_union(self):
        xsd = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="v">
                  <xs:complexType>
                    <xs:simpleContent>
                      <xs:extension base="xs:string">
                        <xs:anyAttribute/>
                      </xs:extension>
                    </xs:simpleContent>
                  </xs:complexType>
                </xs:element>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        errors = [i.message for i in create_plan(xsd).errors]
        self.assertTrue(
            any("anyAttribute" in m or "wildcard" in m for m in errors),
            errors)

        union = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root">
            <xs:simpleType>
              <xs:union memberTypes="xs:int xs:string"/>
            </xs:simpleType>
          </xs:element>
        </xs:schema>"""
        errors = [i.message for i in create_plan(union).errors]
        self.assertTrue(any("xs:list/xs:union" in m for m in errors), errors)

    def test_plan_json_is_serializable(self):
        import json
        payload = json.dumps(create_plan(XSD).to_dict())
        self.assertIn('"importable": true', payload)

    def test_ddl_contains_foreign_keys(self):
        plan = create_plan(XSD)
        statements = "\n".join(plan.create_statements())
        self.assertIn("FOREIGN KEY", statements)
        self.assertIn("REFERENCES \"_node\"", statements)


class ImportAndRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.plan = create_plan(XSD)
        self.conn = connect(self.db_path)
        create_schema(self.conn, self.plan)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_full_document_round_trip(self):
        doc_id = import_document(self.conn, self.plan, DOC_FULL,
                                 doc_id="full")
        rebuilt = rebuild_document(self.conn, self.plan, doc_id)
        with open(DOC_FULL, "rb") as fh:
            original = fh.read()
        self.assertEqual(_normalize(original), _normalize(rebuilt))

    def test_minimal_document_round_trip_and_defaults(self):
        doc_id = import_document(self.conn, self.plan, DOC_MIN, doc_id="min")
        rebuilt = rebuild_document(self.conn, self.plan, doc_id)
        with open(DOC_MIN, "rb") as fh:
            self.assertEqual(_normalize(fh.read()), _normalize(rebuilt))
        # defaults recorded but not serialized back into XML
        order_row = self.conn.execute(
            'SELECT "attr_channel_state", "currency_state" FROM "order" '
            "WHERE doc_id=?", (doc_id,)).fetchone()
        self.assertEqual(order_row[0], "default")
        self.assertEqual(order_row[1], "missing")
        discount = self.conn.execute(
            'SELECT "discount_state" FROM "item" WHERE doc_id=?',
            (doc_id,)).fetchone()[0]
        self.assertEqual(discount, "missing")

    def test_node_identity_order_and_choice_branch(self):
        doc_id = import_document(self.conn, self.plan, DOC_FULL,
                                 doc_id="full")
        nodes = self.conn.execute(
            "SELECT node_id, qname, ord, branch FROM _node "
            "WHERE doc_id=? ORDER BY ord_path", (doc_id,)).fetchall()
        ids = {n[0]: (n[1], n[2], n[3]) for n in nodes}
        self.assertIn("1", ids)
        # three item occurrences keep document order
        item_ids = [nid for nid, v in ids.items()
                    if v[0].endswith("}item") or v[0] == "item"]
        self.assertEqual(len(item_ids), 3)
        paths = sorted(item_ids, key=lambda nid: [
            int(part) for part in nid.split("/")])
        self.assertEqual([ids[nid][1] for nid in paths], [8, 9, 10])
        # choice branch that was taken is stored
        branch_nodes = {v[2] for v in ids.values() if v[2] is not None}
        self.assertEqual(branch_nodes, {"invoiceNo"})

    def test_empty_nil_missing_default_are_distinct(self):
        doc_id = import_document(self.conn, self.plan, DOC_FULL,
                                 doc_id="full")
        states = self.conn.execute(
            "SELECT qname, state FROM _node WHERE doc_id=?",
            (doc_id,)).fetchall()
        state_by_local = {}
        for qname, state in states:
            local = qname.rsplit("}", 1)[-1]
            state_by_local.setdefault(local, []).append(state)
        self.assertIn("nil", state_by_local["coupon"])
        self.assertIn("nil", state_by_local["remark"])
        # note is an explicit empty element carrying empty string
        note = self.conn.execute(
            'SELECT "note", "note_state" FROM "order" WHERE doc_id=?',
            (doc_id,)).fetchone()
        self.assertEqual(note, ("", "value"))
        # currency has an explicit value
        currency = self.conn.execute(
            'SELECT "currency", "currency_state" FROM "order" '
            "WHERE doc_id=?", (doc_id,)).fetchone()
        self.assertEqual(currency, ("USD", "value"))

    def test_minimal_states(self):
        doc_id = import_document(self.conn, self.plan, DOC_MIN, doc_id="min")
        row = self.conn.execute(
            'SELECT "note_state", "currency_state", "gift_state", '
            '"coupon_state" FROM "order" WHERE doc_id=?',
            (doc_id,)).fetchone()
        self.assertEqual(row, ("missing", "missing", "missing", "missing"))
        # the missing optional particles produced no node rows
        count = self.conn.execute(
            "SELECT COUNT(*) FROM _node WHERE doc_id=? AND qname LIKE '%note'",
            (doc_id,)).fetchone()[0]
        self.assertEqual(count, 0)
        # fixed attribute is reflected as fixed state
        fixed = self.conn.execute(
            'SELECT "attr_fixed_tag_state" FROM "shipping" WHERE doc_id=?',
            (doc_id,)).fetchone()[0]
        self.assertEqual(fixed, "fixed")

    def test_foreign_keys_enforced(self):
        import_document(self.conn, self.plan, DOC_MIN, doc_id="x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                'INSERT INTO "order" (doc_id, node_id, state) '
                'VALUES ("x", "99/99", "value")')

    def test_failed_import_leaves_no_rows(self):
        bad = b"""<?xml version="1.0"?>
        <order orderNo="O-BAD">
          <customer>x</customer>
          <shipping mode="sea">s</shipping>
          <invoiceNo>I</invoiceNo>
          <item id="i1">
            <sku>A</sku>
            <quantity>2</quantity>
            <unitPrice>1.00</unitPrice>
          </item>
          <unexpected/>
        </order>"""
        with self.assertRaises(XsdImportError):
            import_document(self.conn, self.plan, bad, doc_id="bad")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM _document WHERE doc_id='bad'")
            .fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM _node WHERE doc_id='bad'")
            .fetchone()[0], 0)

    def test_reimport_is_atomic_replace(self):
        id1 = import_document(self.conn, self.plan, DOC_MIN, doc_id="same")
        id2 = import_document(self.conn, self.plan, DOC_FULL, doc_id="same")
        self.assertEqual(id1, id2)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM _node WHERE doc_id='same'").fetchone()[0]
        expected = self.conn.execute(
            "SELECT COUNT(*) FROM _node WHERE doc_id='full'").fetchone()
        import_document(self.conn, self.plan, DOC_FULL, doc_id="full")
        expected = self.conn.execute(
            "SELECT COUNT(*) FROM _node WHERE doc_id='full'").fetchone()[0]
        self.assertEqual(count, expected)

    def test_invalid_value_rolls_back(self):
        bad = b"""<?xml version="1.0"?>
        <order orderNo="O-BAD2">
          <customer>x</customer>
          <shipping mode="sea">s</shipping>
          <invoiceNo>I</invoiceNo>
          <item id="i1">
            <sku>A</sku>
            <quantity>not-a-number</quantity>
            <unitPrice>1.00</unitPrice>
          </item>
        </order>"""
        with self.assertRaises(XsdImportError):
            import_document(self.conn, self.plan, bad, doc_id="bad2")
        for table in ("_document", "_node", "order", "item", "shipping"):
            count = self.conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE doc_id=?',
                ("bad2",)).fetchone()[0]
            self.assertEqual(count, 0, table)

    def test_namespaced_nested_repeats_round_trip(self):
        xsd = b"""<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
                   targetNamespace="urn:shop"
                   elementFormDefault="qualified">
          <xs:element name="catalog">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="section" maxOccurs="unbounded">
                  <xs:complexType>
                    <xs:sequence>
                      <xs:element name="product" maxOccurs="unbounded">
                        <xs:complexType>
                          <xs:sequence>
                            <xs:element name="name" type="xs:string"/>
                            <xs:element name="warranty" type="xs:string"
                                        default="none"/>
                          </xs:sequence>
                        </xs:complexType>
                      </xs:element>
                    </xs:sequence>
                  </xs:complexType>
                </xs:element>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
        doc = b"""<?xml version="1.0"?>
        <c:catalog xmlns:c="urn:shop">
          <c:section>
            <c:product><c:name>A</c:name><c:warranty/></c:product>
            <c:product><c:name>B</c:name></c:product>
          </c:section>
          <c:section>
            <c:product><c:name>C</c:name><c:warranty>x</c:warranty>
            </c:product>
          </c:section>
        </c:catalog>"""
        plan = create_plan(xsd)
        self.assertTrue(plan.importable, [i.message for i in plan.errors])
        ns_conn = connect(":memory:")
        create_schema(ns_conn, plan)
        doc_id = import_document(ns_conn, plan, doc, doc_id="ns")
        rebuilt = rebuild_document(ns_conn, plan, doc_id)
        self.assertEqual(_normalize(doc), _normalize(rebuilt))
        rows = ns_conn.execute(
            'SELECT "warranty_state" FROM "product" ORDER BY node_id'
        ).fetchall()
        self.assertEqual([r[0] for r in rows],
                         ["default", "missing", "value"])


if __name__ == "__main__":
    unittest.main()
