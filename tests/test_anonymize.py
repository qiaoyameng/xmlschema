#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""Tests for identity-constraint based XML anonymization."""
import io
import json
import logging
import pathlib
import unittest
from contextlib import contextmanager

import xmlschema
from xmlschema.anonymize import (
    AnonymizationError, AnonymizationReceipt, RestrictedValueGenerator,
    anonymize, reference_graph_fingerprint,
)
from xmlschema.testing import run_xmlschema_tests
from xmlschema.utils.etree import etree_tostring

CASES_DIR = pathlib.Path(__file__).absolute().parent.joinpath(
    'test_cases', 'examples', 'anonymize')
NS = 'http://example.com/orders'
Q = lambda local: '{%s}%s' % (NS, local)
CUSTOMER_KEY = '{%s}customerKey' % NS
ORDER_LINE_KEY = '{%s}orderLineKey' % NS


@contextmanager
def capture_logs(logger_name, level=logging.INFO):
    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Handler()
    logger = logging.getLogger(logger_name)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


class AnonymizeTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = xmlschema.XMLSchema(CASES_DIR.joinpath('orders.xsd'))
        cls.doc_a = str(CASES_DIR.joinpath('orders-a.xml'))
        cls.doc_b = str(CASES_DIR.joinpath('orders-b.xml'))

    def iter_by_path(self, root, local):
        # Local elements are unqualified in this fixture (only the root is
        # namespaced), so search by local name ignoring namespace.
        return [elem for elem in root.iter()
                if isinstance(elem.tag, str) and elem.tag.endswith('}'+local)
                or elem.tag == local]

    # -- basic bijective mapping + keyref sync ----------------------------

    def test_caller_table_rewrites_key_and_keyref(self):
        table = {'customerKey': {
            'C100': 'C777', 'C200': 'C888',
        }}
        out, receipt = anonymize(self.doc_a, self.schema, table)
        self.assertEqual(
            [c.get('code') for c in self.iter_by_path(out, 'customer')],
            ['C777', 'C888'])
        # Every keyref customer value is rewritten consistently.
        self.assertEqual(
            [o.get('customer') for o in self.iter_by_path(out, 'order')],
            ['C777', 'C888'])
        self.assertTrue(self.schema.is_valid(out))
        mapping = receipt.scopes[CUSTOMER_KEY].flat_mapping
        self.assertEqual(mapping[('C100',)], ('C777',))
        self.assertFalse(receipt.scopes[CUSTOMER_KEY].generated)

    def test_equal_values_share_one_mapping_distinct_values_differ(self):
        table = {'customerKey': {
            'C100': 'C777', 'C200': 'C888',
        }}
        out, _ = anonymize(self.doc_a, self.schema, table)
        replacements = {c.get('code')
                        for c in self.iter_by_path(out, 'customer')}
        self.assertEqual(len(replacements), 2)
        # Both occurrences referencing C100 (one customer + its order) keep
        # the same replacement: mapping is a function.
        orders = {o.get('id'): o.get('customer')
                  for o in self.iter_by_path(out, 'order')}
        self.assertEqual(orders['O1000'], 'C777')

    # -- composite keys handled as a whole ---------------------------------

    def test_composite_key_is_handled_as_a_whole(self):
        table = {'orderLineKey': {
            ('1', 'AAA-1001'): ('7', 'ZZZ-0001'),
            ('2', 'BBB-2002'): ('8', 'ZZZ-0002'),
            ('1', 'CCC-3003'): ('9', 'ZZZ-0003'),
        }}
        out, receipt = anonymize(self.doc_a, self.schema, table)
        lines = [(l.find('seq').text, l.find('item').text)
                 for l in self.iter_by_path(out, 'line')]
        self.assertIn(('7', 'ZZZ-0001'), lines)
        self.assertIn(('8', 'ZZZ-0002'), lines)
        self.assertIn(('9', 'ZZZ-0003'), lines)
        # Whole-tuple mapping, not a per-column product: the two distinct
        # tuples sharing seq '1' get distinct replacements.
        scope_maps = receipt.scopes[ORDER_LINE_KEY].scopes
        # Two orders -> two independent scopes.
        self.assertEqual(len(scope_maps), 2)
        merged = {old: new for mapping in scope_maps.values()
                  for old, new in mapping.items()}
        self.assertEqual(merged[('1', 'AAA-1001')], ('7', 'ZZZ-0001'))
        self.assertEqual(merged[('1', 'CCC-3003')], ('9', 'ZZZ-0003'))
        self.assertTrue(self.schema.is_valid(out))

    def test_composite_table_with_wrong_arity_is_rejected(self):
        bad_tables = (
            {'orderLineKey': {('1', 'AAA-1001'): ('7',)}},
            {'orderLineKey': {'X': ('7', 'ZZZ-0001')}},
        )
        for table in bad_tables:
            with self.assertRaises(AnonymizationError):
                anonymize(self.doc_a, self.schema, table)

    # -- types and facets ---------------------------------------------------

    def test_generator_output_respects_patterns_enumeration_and_ranges(self):
        out, _ = anonymize(
            self.doc_a, self.schema,
            generator=RestrictedValueGenerator(seed=3))
        self.assertTrue(self.schema.is_valid(out))
        import re
        code_pat = re.compile(r'^C\d{3}$')
        order_pat = re.compile(r'^O\d{4}$')
        sku_pat = re.compile(r'^[A-Z]{3}-\d{4}$')
        for customer in self.iter_by_path(out, 'customer'):
            self.assertRegex(customer.get('code'), code_pat)
            self.assertIn(customer.get('region'),
                          ('EMEA', 'APAC', 'AMER'))
        for order in self.iter_by_path(out, 'order'):
            self.assertRegex(order.get('id'), order_pat)
        for line in self.iter_by_path(out, 'line'):
            self.assertRegex(line.find('item').text, sku_pat)
            seq = int(line.find('seq').text)
            self.assertTrue(1 <= seq <= 9999)

    def test_invalid_caller_value_violating_facets_fails_atomically(self):
        original = pathlib.Path(self.doc_a).read_text()
        with self.assertRaises(AnonymizationError):
            anonymize(self.doc_a, self.schema,
                     {'customerKey': {'C100': 'not-a-code',
                                      'C200': 'C888'}})
        # Source document untouched on failure.
        self.assertEqual(pathlib.Path(self.doc_a).read_text(), original)

    # -- collisions ----------------------------------------------------------

    def test_target_collision_between_two_originals_fails(self):
        with self.assertRaises(AnonymizationError) as ctx:
            anonymize(self.doc_a, self.schema,
                     {'customerKey': {'C100': 'C777', 'C200': 'C777'}})
        self.assertIn('collision', str(ctx.exception).lower())

    def test_target_clash_with_existing_original_fails(self):
        # C100 -> C200 would give C200 two preimages.
        with self.assertRaises(AnonymizationError):
            anonymize(self.doc_a, self.schema,
                     {'customerKey': {'C100': 'C200'}})

    # -- unresolvable keyref -------------------------------------------------

    def test_unresolvable_keyref_fails_entire_run(self):
        broken = io.BytesIO(
            b'<ledger xmlns:o="http://example.com/orders">'
            b'<customer code="C100" region="EMEA"><name>a</name></customer>'
            b'<order id="O9999" customer="C100">'
            b'<line><seq>1</seq><item>AAA-1001</item></line></order>'
            b'<order id="O1000" customer="C000">'
            b'<line><seq>1</seq><item>BBB-2002</item></line></order>'
            b'</ledger>')
        with self.assertRaises(AnonymizationError):
            anonymize(broken, self.schema,
                     {'customerKey': {'C100': 'C777'}})

    def test_missing_replacement_in_strict_mode_fails(self):
        with self.assertRaises(AnonymizationError):
            anonymize(self.doc_a, self.schema,
                     {'customerKey': {'C100': 'C777'}}, strict=True)
        # In non-strict mode a default restricted generator fills the gap.
        out, _ = anonymize(self.doc_a, self.schema,
                           {'customerKey': {'C100': 'C777'}})
        self.assertTrue(self.schema.is_valid(out))

    # -- scope isolation -----------------------------------------------------

    def test_same_text_id_in_two_documents_uses_independent_mappings(self):
        # Provide deliberately different, valid tables. A replacement from
        # document A must never leak into document B.
        out_a, _ = anonymize(self.doc_a, self.schema, {
            'customerKey': {'C100': 'C701', 'C200': 'C702'}})
        out_b, _ = anonymize(self.doc_b, self.schema, {
            'customerKey': {'C100': 'C901', 'C300': 'C903'}})
        codes_a = [c.get('code') for c in self.iter_by_path(out_a, 'customer')]
        codes_b = [c.get('code') for c in self.iter_by_path(out_b, 'customer')]
        self.assertEqual(codes_a, ['C701', 'C702'])
        self.assertEqual(codes_b, ['C901', 'C903'])
        # Same textual id C100 resolved differently per document.
        orders_b = {o.get('id'): o.get('customer')
                    for o in self.iter_by_path(out_b, 'order')}
        self.assertEqual(orders_b['O1000'], 'C901')
        self.assertNotIn('C701', codes_b)

    def test_separate_generator_runs_do_not_share_state(self):
        out_a, r_a = anonymize(
            self.doc_a, self.schema,
            generator=RestrictedValueGenerator(seed=5))
        out_b, r_b = anonymize(
            self.doc_b, self.schema,
            generator=RestrictedValueGenerator(seed=5))
        # Each run is its own bijection and validates independently.
        self.assertTrue(self.schema.is_valid(out_a))
        self.assertTrue(self.schema.is_valid(out_b))
        map_a = r_a.scopes[CUSTOMER_KEY].flat_mapping
        map_b = r_b.scopes[CUSTOMER_KEY].flat_mapping
        self.assertEqual({len(set(v)) for v in (
            set(map_a.values()), set(map_b.values()))},
            {len(map_a), len(map_b)})

    # -- controlled receipt and safe logging --------------------------------

    def test_receipt_hides_originals_by_default(self):
        _, receipt = anonymize(
            self.doc_a, self.schema,
            {'customerKey': {'C100': 'C777', 'C200': 'C888'}})
        default_json = receipt.to_json()
        self.assertNotIn('C100', default_json)
        self.assertNotIn('C200', default_json)
        self.assertIn('C777', default_json)
        revealed = receipt.to_json(reveal_original=True)
        self.assertIn('C100', revealed)
        payload = json.loads(default_json)
        self.assertEqual(payload['scopes'][0]['key_count'], 2)

    def test_default_logging_does_not_emit_original_keys(self):
        table = {'customerKey': {'C100': 'C777', 'C200': 'C888'}}
        with capture_logs('xmlschema.anonymize') as records:
            anonymize(self.doc_a, self.schema, table)
        text = ' '.join(r.getMessage() for r in records)
        for secret in ('C100', 'C200', 'AAA-1001', 'O1000'):
            self.assertNotIn(secret, text)

    # -- reference graph invariance ------------------------------------------

    def test_reference_graph_unchanged_after_anonymization(self):
        out, _ = anonymize(
            self.doc_a, self.schema,
            generator=RestrictedValueGenerator(seed=11))
        before = reference_graph_fingerprint(self.schema, self.doc_a)
        after = reference_graph_fingerprint(self.schema, out)
        self.assertEqual(before, after)
        self.assertTrue(before.edges)  # the document really has references

    def test_graph_fingerprint_detects_a_changed_reference(self):
        # Tamper with the anonymized output so one keyref points elsewhere;
        # the fingerprint must reveal the broken connection.
        out, _ = anonymize(
            self.doc_a, self.schema,
            {'customerKey': {'C100': 'C777', 'C200': 'C888'}})
        orders = self.iter_by_path(out, 'order')
        orders[0].set('customer', 'C888')  # reroute O1000
        before = reference_graph_fingerprint(self.schema, self.doc_a)
        after = reference_graph_fingerprint(self.schema, out)
        self.assertNotEqual(before, after)


    def test_generator_failure_when_no_legal_value_remains(self):
        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="doc"><xs:complexType><xs:sequence>'
            '<xs:element name="part" maxOccurs="unbounded">'
            '<xs:complexType><xs:sequence>'
            '<xs:element name="code"><xs:simpleType>'
            '<xs:restriction base="xs:string">'
            '<xs:enumeration value="X"/>'
            '<xs:enumeration value="Y"/>'
            '</xs:restriction></xs:simpleType></xs:element>'
            '</xs:sequence></xs:complexType></xs:element>'
            '</xs:sequence></xs:complexType>'
            '<xs:unique name="partUnique">'
            '<xs:selector xpath="part"/><xs:field xpath="code"/>'
            '</xs:unique></xs:element></xs:schema>'
        )
        instance = io.StringIO(
            '<doc><part><code>X</code></part>'
            '<part><code>Y</code></part></doc>')
        schema = xmlschema.XMLSchema(io.StringIO(xsd))
        # Both enumerated values are already used: nothing legal to emit.
        with self.assertRaises(AnonymizationError) as ctx:
            anonymize(instance, schema,
                      generator=RestrictedValueGenerator(seed=1))
        self.assertIn('enumerated', str(ctx.exception).lower())

    def test_unique_constraint_is_supported_like_key(self):
        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="doc"><xs:complexType><xs:sequence>'
            '<xs:element name="thing" maxOccurs="unbounded">'
            '<xs:complexType><xs:attribute name="uid" use="required">'
            '<xs:simpleType><xs:restriction base="xs:string">'
            '<xs:pattern value="[A-Z][0-9]"/></xs:restriction>'
            '</xs:simpleType></xs:attribute></xs:complexType>'
            '</xs:element></xs:sequence></xs:complexType>'
            '<xs:unique name="thingUnique">'
            '<xs:selector xpath="thing"/><xs:field xpath="@uid"/>'
            '</xs:unique></xs:element></xs:schema>'
        )
        instance = io.StringIO(
            '<doc><thing uid="A1"/><thing uid="B2"/></doc>')
        schema = xmlschema.XMLSchema(io.StringIO(xsd))
        out, receipt = anonymize(
            instance, schema,
            generator=RestrictedValueGenerator(seed=4))
        self.assertTrue(schema.is_valid(out))
        uids = [t.get('uid') for t in out.iter() if t.tag == 'thing']
        self.assertEqual(len(set(uids)), 2)
        for uid in uids:
            self.assertRegex(uid, r'^[A-Z][0-9]$')



    NESTED_SCOPE_XSD = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:c="http://c.example" '
        'targetNamespace="http://c.example" elementFormDefault="unqualified">'
        '<xs:element name="company"><xs:complexType><xs:sequence>'
        '<xs:element name="dept" maxOccurs="unbounded">'
        '<xs:complexType><xs:sequence>'
        '<xs:element name="emp" maxOccurs="unbounded"><xs:complexType>'
        '<xs:attribute name="id" use="required"><xs:simpleType>'
        '<xs:restriction base="xs:string">'
        '<xs:pattern value="[A-Z][0-9]{2}"/>'
        '</xs:restriction></xs:simpleType></xs:attribute>'
        '<xs:attribute name="peer" type="xs:string"/>'
        '</xs:complexType></xs:element>'
        '</xs:sequence></xs:complexType>'
        '<xs:key name="empKey"><xs:selector xpath="emp"/>'
        '<xs:field xpath="@id"/></xs:key>'
        '<xs:keyref name="peerRef" refer="c:empKey">'
        '<xs:selector xpath="emp"/><xs:field xpath="@peer"/></xs:keyref>'
        '</xs:element>'
        '</xs:sequence></xs:complexType></xs:element></xs:schema>'
    )

    def test_same_text_id_in_two_scopes_uses_separate_bijections(self):
        instance = io.StringIO(
            '<c:company xmlns:c="http://c.example">'
            '<dept><emp id="A01" peer="A01"/><emp id="A02"/></dept>'
            '<dept><emp id="A01" peer="A01"/><emp id="A09"/></dept>'
            '</c:company>')
        schema = xmlschema.XMLSchema(io.StringIO(self.NESTED_SCOPE_XSD))
        before = reference_graph_fingerprint(schema, instance)
        out, receipt = anonymize(
            instance, schema,
            generator=RestrictedValueGenerator(seed=2))
        self.assertTrue(schema.is_valid(out))
        depts = [d for d in out.iter() if d.tag == 'dept']
        scope_ids = [[e.get('id') for e in d] for d in depts]
        # The same original text A01 gets a *different* replacement per
        # scope because the bijection is scoped.
        self.assertNotEqual(scope_ids[0][0], scope_ids[1][0])
        # Each keyref still resolves inside its own scope only.
        for dept in depts:
            ids = {e.get('id') for e in dept}
            for emp in dept:
                peer = emp.get('peer')
                if peer:
                    self.assertIn(peer, ids)
        scope_maps = receipt.scopes[
            '{http://c.example}empKey'].scopes
        self.assertEqual(len(scope_maps), 2)
        self.assertNotEqual(
            scope_maps[0][('A01',)], scope_maps[1][('A01',)])
        after = reference_graph_fingerprint(schema, out)
        self.assertEqual(before, after)


    # -- determinism ----------------------------------------------------------

    def test_seeded_generation_is_deterministic(self):
        out1, r1 = anonymize(
            self.doc_a, self.schema,
            generator=RestrictedValueGenerator(seed=99))
        out2, r2 = anonymize(
            self.doc_a, self.schema,
            generator=RestrictedValueGenerator(seed=99))
        self.assertEqual(etree_tostring(out1), etree_tostring(out2))
        self.assertEqual(
            r1.scopes[CUSTOMER_KEY].flat_mapping,
            r2.scopes[CUSTOMER_KEY].flat_mapping)


if __name__ == '__main__':
    run_xmlschema_tests('anonymize.py module')
