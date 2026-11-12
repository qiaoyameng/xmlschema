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
"""
Tests for the incremental schema workspace
(:mod:`xmlschema.workspace`).
"""
import tempfile
import unittest
from pathlib import Path

from xmlschema import (
    SchemaWorkspace,
    SchemaSnapshot,
    MissingResourceError,
    DependencyCycleError,
    XMLSchemaParseError,
)


COMMON_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:common" xmlns:tns="urn:common"
    elementFormDefault="qualified">
  <xs:complexType name="SharedType">
    <xs:sequence>
      <xs:element name="name" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""

TYPES_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:types" xmlns:tns="urn:types"
    xmlns:c="urn:common" elementFormDefault="qualified">
  <xs:import namespace="urn:common" schemaLocation="common.xsd"/>
  <xs:complexType name="RecordType">
    <xs:sequence>
      <xs:element name="id" type="xs:int"/>
      <xs:element name="shared" type="c:SharedType"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="record" type="tns:RecordType"/>
</xs:schema>
"""

APP_B_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:appB" xmlns:tns="urn:appB"
    xmlns:t="urn:types" elementFormDefault="qualified">
  <xs:import namespace="urn:types" schemaLocation="types.xsd"/>
  <xs:element name="docB">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="t:record" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

APP_A_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:appA" xmlns:tns="urn:appA"
    xmlns:t="urn:types" xmlns:b="urn:appB"
    elementFormDefault="qualified">
  <xs:import namespace="urn:types" schemaLocation="types.xsd"/>
  <xs:import namespace="urn:appB" schemaLocation="appB.xsd"/>
  <xs:element name="docA">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="t:record"/>
        <xs:element ref="b:docB" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

VALID_DOC_A = (
    '<a:docA xmlns:a="urn:appA">'
    '<t:record xmlns:t="urn:types">'
    '<t:id>1</t:id>'
    '<t:shared><c:name xmlns:c="urn:common">x</c:name></t:shared>'
    '</t:record></a:docA>'
)
INVALID_DOC_A = VALID_DOC_A.replace('<t:id>1</t:id>', '<t:id>not-an-int</t:id>')
VALID_DOC_B_TEMPLATE = (
    '<b:docB xmlns:b="urn:appB">{records}</b:docB>'
)
RECORD_XML = (
    '<t:record xmlns:t="urn:types">'
    '<t:id>{idx}</t:id>'
    '<t:shared><c:name xmlns:c="urn:common">n</c:name></t:shared>'
    '</t:record>'
)


class WorkspaceTestBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / 'common.xsd').write_text(COMMON_XSD)
        (self.dir / 'types.xsd').write_text(TYPES_XSD)
        (self.dir / 'appB.xsd').write_text(APP_B_XSD)
        (self.dir / 'appA.xsd').write_text(APP_A_XSD)

    def tearDown(self):
        self._tmp.cleanup()

    def workspace(self, **kwargs):
        return SchemaWorkspace(**kwargs)

    def url(self, name):
        return self.dir.joinpath(name).as_uri()

class TestSchemaWorkspaceGraph(WorkspaceTestBase):
    """Graph scanning, summaries and dependency edges."""

    def test_first_publish_builds_the_whole_graph(self):
        workspace = self.workspace()
        snapshot = workspace.publish(str(self.dir / 'appA.xsd'))

        self.assertIsInstance(snapshot, SchemaSnapshot)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.stats.loads, 4)
        self.assertEqual(
            {url.split('/')[-1] for identity in snapshot.summaries
             for url in (identity[0],)},
            {'appA.xsd', 'appB.xsd', 'common.xsd', 'types.xsd'},
        )
        self.assertEqual(
            {(e.source[0].split('/')[-1], e.kind,
              e.target[0].split('/')[-1]) for e in snapshot.edges},
            {
                ('appA.xsd', 'import', 'types.xsd'),
                ('appA.xsd', 'import', 'appB.xsd'),
                ('appB.xsd', 'import', 'types.xsd'),
                ('types.xsd', 'import', 'common.xsd'),
            },
        )

    def test_identity_is_base_url_plus_effective_namespace(self):
        workspace = self.workspace()
        snapshot = workspace.publish(str(self.dir / 'appA.xsd'))
        identities = set(snapshot.summaries)
        self.assertIn((self.url('common.xsd'), 'urn:common'), identities)
        self.assertIn((self.url('types.xsd'), 'urn:types'), identities)

        summary = snapshot.summaries[(self.url('common.xsd'), 'urn:common')]
        self.assertEqual(summary.identity,
                         (self.url('common.xsd'), 'urn:common'))
        self.assertFalse(summary.is_chameleon)

    def test_dependents_and_dependencies(self):
        workspace = self.workspace()
        workspace.publish(str(self.dir / 'appA.xsd'))

        targets = {(e.target[0].split('/')[-1])
                   for e in workspace.dependencies(self.url('types.xsd'))}
        self.assertEqual(targets, {'common.xsd'})

        dependents = {(e.source[0].split('/')[-1])
                      for e in workspace.dependents(self.url('common.xsd'))}
        self.assertEqual(dependents, {'types.xsd'})


class TestSchemaWorkspaceIncremental(WorkspaceTestBase):
    """Incremental rebuilds and component reuse."""

    def test_no_change_rebuilds_only_the_root(self):
        workspace = self.workspace()
        first = workspace.publish(str(self.dir / 'appA.xsd'))
        second = workspace.apply({})

        self.assertEqual(second.generation, 2)
        self.assertEqual(second.stats.loads, 1)
        self.assertEqual(second.stats.schemas_rebuilt, {self.url('appA.xsd')})
        self.assertEqual(
            second.stats.namespaces_reused,
            {'urn:appB', 'urn:common', 'urn:types'},
        )

    def test_leaf_change_reuses_unaffected_branch(self):
        workspace = self.workspace()
        first = workspace.publish(str(self.dir / 'appA.xsd'))
        old_record = first.maps.elements['{urn:types}record']
        old_shared = first.maps.types['{urn:common}SharedType']

        new_app_b = APP_B_XSD.replace('maxOccurs="unbounded"', 'maxOccurs="1"')
        snapshot = workspace.apply({'appB.xsd': new_app_b.encode()})

        # Only the root and the changed appB branch are rebuilt: types/common
        # keep their already built global components.
        self.assertEqual(snapshot.stats.schemas_rebuilt,
                         {self.url('appA.xsd'), self.url('appB.xsd')})
        self.assertEqual(snapshot.stats.namespaces_reused,
                         {'urn:common', 'urn:types'})
        self.assertEqual(snapshot.stats.global_components_built, 2)
        self.assertIs(snapshot.maps.elements['{urn:types}record'], old_record)
        self.assertIs(snapshot.maps.types['{urn:common}SharedType'], old_shared)
        self.assertIsNot(snapshot.maps.elements['{urn:appB}docB'],
                         first.maps.elements['{urn:appB}docB'])

    def test_common_change_rebuilds_every_dependent(self):
        workspace = self.workspace()
        workspace.publish(str(self.dir / 'appA.xsd'))
        new_common = COMMON_XSD.replace('xs:string', 'xs:token')
        snapshot = workspace.apply({'common.xsd': new_common.encode()})

        self.assertEqual(
            snapshot.stats.schemas_rebuilt,
            {self.url(name) for name in
             ('appA.xsd', 'appB.xsd', 'common.xsd', 'types.xsd')},
        )
        self.assertEqual(snapshot.stats.namespaces_reused, set())


class TestSchemaWorkspaceReaders(WorkspaceTestBase):
    """Old readers keep the complete old version after a publish."""

    def test_old_and_new_readers_see_different_semantics(self):
        workspace = self.workspace()
        old_snapshot = workspace.publish(str(self.dir / 'appA.xsd'))

        new_app_b = APP_B_XSD.replace('maxOccurs="unbounded"', 'maxOccurs="1"')
        new_snapshot = workspace.apply({'appB.xsd': new_app_b.encode()})

        two_records = VALID_DOC_B_TEMPLATE.format(
            records=RECORD_XML.format(idx=1) + RECORD_XML.format(idx=2)
        )
        one_record = VALID_DOC_B_TEMPLATE.format(records=RECORD_XML.format(idx=1))

        # The old reader keeps validating against the old (unbounded) version.
        self.assertTrue(old_snapshot.is_valid(two_records))
        self.assertFalse(new_snapshot.is_valid(two_records))
        self.assertTrue(new_snapshot.is_valid(one_record))
        self.assertTrue(old_snapshot.is_valid(one_record))

    def test_old_snapshot_is_not_mutated_by_failed_candidates(self):
        workspace = self.workspace()
        old_snapshot = workspace.publish(str(self.dir / 'appA.xsd'))
        self.assertTrue(old_snapshot.is_valid(VALID_DOC_A))
        self.assertFalse(old_snapshot.is_valid(INVALID_DOC_A))

        broken_types = TYPES_XSD.replace('c:SharedType', 'c:MissingType')
        with self.assertRaises(XMLSchemaParseError):
            workspace.apply({'types.xsd': broken_types.encode()})

        # The failed candidate is not published and the old snapshot is intact.
        self.assertIs(workspace.snapshot, old_snapshot)
        self.assertTrue(old_snapshot.is_valid(VALID_DOC_A))
        self.assertFalse(old_snapshot.is_valid(INVALID_DOC_A))

        # The workspace still accepts valid changes afterwards.
        new_common = COMMON_XSD.replace('xs:string', 'xs:token')
        snapshot = workspace.apply({'common.xsd': new_common.encode()})
        self.assertEqual(snapshot.generation, 2)
        self.assertTrue(snapshot.is_valid(VALID_DOC_A))

    def test_valid_change_updates_new_readers(self):
        workspace = self.workspace()
        workspace.publish(str(self.dir / 'appA.xsd'))
        new_common = COMMON_XSD.replace(
            '<xs:element name="name" type="xs:string"/>',
            '<xs:element name="name" type="xs:int"/>',
        )
        snapshot = workspace.apply({'common.xsd': new_common.encode()})

        self.assertFalse(snapshot.is_valid(VALID_DOC_A))
        int_doc = VALID_DOC_A.replace(
            '<c:name xmlns:c="urn:common">x</c:name>',
            '<c:name xmlns:c="urn:common">7</c:name>',
        )
        self.assertTrue(snapshot.is_valid(int_doc))


CHAMELEON_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    elementFormDefault="qualified">
  <xs:complexType name="Payload">
    <xs:sequence><xs:element name="v" type="xs:int"/></xs:sequence>
  </xs:complexType>
</xs:schema>
"""

CHAMELEON_INCLUDE_NS = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:{ns}" xmlns="urn:{ns}" elementFormDefault="qualified">
  <xs:include schemaLocation="chameleon.xsd"/>
  <xs:element name="{ns}Doc" type="Payload"/>
</xs:schema>
"""

CHAMELEON_ROOT = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:root" xmlns:r="urn:root"
    xmlns:b="urn:b" xmlns:d="urn:d" elementFormDefault="qualified">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:import namespace="urn:d" schemaLocation="d.xsd"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="b:bDoc" minOccurs="0"/>
        <xs:element ref="d:dDoc" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


class TestSchemaWorkspaceChameleon(WorkspaceTestBase):

    def setUp(self):
        super().setUp()
        (self.dir / 'chameleon.xsd').write_text(CHAMELEON_XSD)
        (self.dir / 'b.xsd').write_text(CHAMELEON_INCLUDE_NS.format(ns='b'))
        (self.dir / 'd.xsd').write_text(CHAMELEON_INCLUDE_NS.format(ns='d'))
        (self.dir / 'root.xsd').write_text(CHAMELEON_ROOT)

    def test_chameleon_has_one_identity_per_context(self):
        workspace = self.workspace()
        snapshot = workspace.publish(str(self.dir / 'root.xsd'))

        chameleon = [
            identity for identity in snapshot.summaries
            if identity[0] == self.url('chameleon.xsd')
        ]
        self.assertEqual(
            set(chameleon),
            {(self.url('chameleon.xsd'), 'urn:b'),
             (self.url('chameleon.xsd'), 'urn:d')},
        )
        for summary in snapshot.summaries.values():
            if summary.url == self.url('chameleon.xsd'):
                self.assertTrue(summary.is_chameleon)
                self.assertEqual(summary.digest,
                                 snapshot.summaries[chameleon[0]].digest)

    def test_chameleon_components_are_not_shared_between_contexts(self):
        workspace = self.workspace()
        snapshot = workspace.publish(str(self.dir / 'root.xsd'))

        payload_b = snapshot.maps.types['{urn:b}Payload']
        payload_d = snapshot.maps.types['{urn:d}Payload']
        self.assertIsNot(payload_b, payload_d)
        self.assertEqual(payload_b.schema.target_namespace, 'urn:b')
        self.assertEqual(payload_d.schema.target_namespace, 'urn:d')

        self.assertTrue(snapshot.is_valid(
            '<r:root xmlns:r="urn:root">'
            '<b:bDoc xmlns:b="urn:b"><b:v>1</b:v></b:bDoc>'
            '<d:dDoc xmlns:d="urn:d"><d:v>2</d:v></d:dDoc>'
            '</r:root>'
        ))

    def test_chameleon_change_rebuilds_both_contexts(self):
        workspace = self.workspace()
        workspace.publish(str(self.dir / 'root.xsd'))
        good = CHAMELEON_XSD.replace('xs:int', 'xs:positiveInteger')
        snapshot = workspace.apply({'chameleon.xsd': good.encode()})

        self.assertEqual(
            snapshot.stats.schemas_rebuilt,
            {self.url(name) for name in
             ('root.xsd', 'b.xsd', 'd.xsd', 'chameleon.xsd')},
        )

    def test_context_change_does_not_reuse_chameleon_for_other_context(self):
        workspace = self.workspace()
        first = workspace.publish(str(self.dir / 'root.xsd'))
        old_payload_d = first.maps.types['{urn:d}Payload']
        new_b = CHAMELEON_INCLUDE_NS.format(ns='b').replace(
            '<xs:element name="bDoc" type="Payload"/>',
            '<xs:element name="bDoc" type="Payload"/>'
            '<xs:element name="bExtra" type="xs:string"/>',
        )
        snapshot = workspace.apply({'b.xsd': new_b.encode()})

        # b.xsd changed but the chameleon document did not: only the root and
        # b.xsd are rebuilt, both chameleon contexts keep their components.
        self.assertEqual(
            snapshot.stats.schemas_rebuilt,
            {self.url('root.xsd'), self.url('b.xsd')},
        )
        self.assertEqual(
            snapshot.stats.resources_reused,
            {
                (self.url('d.xsd'), 'urn:d'),
                (self.url('chameleon.xsd'), 'urn:d'),
                (self.url('chameleon.xsd'), 'urn:b'),
            },
        )
        self.assertIs(snapshot.maps.types['{urn:d}Payload'], old_payload_d)
        self.assertIs(snapshot.maps.types['{urn:b}Payload'],
                      first.maps.types['{urn:b}Payload'])
        self.assertIn('{urn:b}bExtra', snapshot.maps.elements)


class TestSchemaWorkspaceErrors(WorkspaceTestBase):
    """Missing resources and dependency cycles report traceable chains."""

    def test_missing_resource_reports_the_reference_chain(self):
        (self.dir / 'root.xsd').write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:r">'
            '<xs:import namespace="urn:t" schemaLocation="types.xsd"/>'
            '<xs:element name="e" type="xs:string"/></xs:schema>'
        )
        (self.dir / 'types.xsd').write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:t">'
            '<xs:import namespace="urn:g" schemaLocation="ghost.xsd"/>'
            '<xs:element name="t" type="xs:string"/></xs:schema>'
        )
        workspace = self.workspace()
        with self.assertRaises(MissingResourceError) as ctx:
            workspace.publish(str(self.dir / 'root.xsd'))

        self.assertEqual(ctx.exception.location, self.url('ghost.xsd'))
        self.assertEqual(
            [url.split('/')[-1] for url in ctx.exception.chain],
            ['root.xsd', 'types.xsd', 'ghost.xsd'],
        )
        self.assertIsNone(workspace.snapshot)

    def test_remote_location_is_never_fetched(self):
        (self.dir / 'root.xsd').write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:r">'
            '<xs:import namespace="urn:ext" '
            'schemaLocation="http://example.invalid/ext.xsd"/>'
            '<xs:element name="e" type="xs:string"/></xs:schema>'
        )
        workspace = self.workspace()
        with self.assertRaises(MissingResourceError) as ctx:
            workspace.publish(str(self.dir / 'root.xsd'))
        self.assertTrue(ctx.exception.location.startswith('http://'))

    def test_dependency_cycle_reports_the_cycle_chain(self):
        (self.dir / 'a.xsd').write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:a">'
            '<xs:import namespace="urn:b" schemaLocation="b.xsd"/>'
            '<xs:element name="a" type="xs:string"/></xs:schema>'
        )
        (self.dir / 'b.xsd').write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:b">'
            '<xs:import namespace="urn:c" schemaLocation="c.xsd"/>'
            '<xs:element name="b" type="xs:string"/></xs:schema>'
        )
        (self.dir / 'c.xsd').write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:c">'
            '<xs:import namespace="urn:a" schemaLocation="a.xsd"/>'
            '<xs:element name="c" type="xs:string"/></xs:schema>'
        )
        workspace = self.workspace()
        with self.assertRaises(DependencyCycleError) as ctx:
            workspace.publish(str(self.dir / 'a.xsd'))

        names = [label.split('/')[-1].split(' ')[0]
                 for label in ctx.exception.chain]
        self.assertEqual(names, ['a.xsd', 'b.xsd', 'c.xsd', 'a.xsd'])

    def test_invalid_change_keeps_the_old_workspace(self):
        workspace = self.workspace()
        workspace.publish(str(self.dir / 'appA.xsd'))
        broken_app_b = APP_B_XSD.replace(
            'ref="t:record"', 'ref="t:notThere"',
        )
        with self.assertRaises(XMLSchemaParseError):
            workspace.apply({'appB.xsd': broken_app_b.encode()})
        self.assertEqual(workspace.snapshot.generation, 1)
        self.assertTrue(workspace.snapshot.is_valid(VALID_DOC_A))




XSD11_ROOT_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:r11" xmlns="urn:r11"
    elementFormDefault="qualified" defaultAttributes="defAttrs">
  <xs:attributeGroup name="defAttrs">
    <xs:attribute name="lang" type="xs:string" default="en"/>
  </xs:attributeGroup>
  <xs:element name="doc">
    <xs:complexType>
      <xs:sequence><xs:element name="item" type="xs:string"/></xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


class TestSchemaWorkspaceXsd11(unittest.TestCase):

    def test_xsd11_default_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root.xsd'
            root.write_text(XSD11_ROOT_XSD)
            workspace = SchemaWorkspace(xsd_version='1.1')
            snapshot = workspace.publish(str(root))

            self.assertIsNotNone(snapshot.schema.default_attributes)
            self.assertTrue(snapshot.is_valid(
                '<doc xmlns="urn:r11" lang="fr"><item>hello</item></doc>'
            ))
            snapshot2 = workspace.apply({})
            self.assertIsNotNone(snapshot2.schema.default_attributes)


class TestSchemaWorkspaceModes(WorkspaceTestBase):

    def test_strict_validation_mode(self):
        workspace = self.workspace(validation='strict')
        snapshot = workspace.publish(str(self.dir / 'appA.xsd'))
        self.assertTrue(snapshot.is_valid(VALID_DOC_A))
        self.assertFalse(snapshot.is_valid(INVALID_DOC_A))

    def test_first_publish_via_apply(self):
        workspace = self.workspace()
        snapshot = workspace.apply({}, root=str(self.dir / 'appA.xsd'))
        self.assertEqual(snapshot.generation, 1)
        self.assertTrue(snapshot.is_valid(VALID_DOC_A))

    def test_invalid_workspace_arguments(self):
        with self.assertRaises(ValueError):
            SchemaWorkspace(xsd_version='2.0')
        with self.assertRaises(ValueError):
            SchemaWorkspace(validation='unknown')
        workspace = self.workspace()
        with self.assertRaises(ValueError):
            workspace.apply({})



BASE_REDEFINE_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:base" xmlns:tns="urn:base"
    elementFormDefault="qualified">
  <xs:complexType name="T">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>
"""

REDEFINE_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:base" xmlns:tns="urn:base"
    elementFormDefault="qualified">
  <xs:redefine schemaLocation="base.xsd">
    <xs:complexType name="T">
      <xs:complexContent>
        <xs:extension base="tns:T">
          <xs:sequence><xs:element name="b" type="xs:int"/></xs:sequence>
        </xs:extension>
      </xs:complexContent>
    </xs:complexType>
  </xs:redefine>
</xs:schema>
"""

REDEFINE_MAIN_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:main" xmlns:b="urn:base"
    elementFormDefault="qualified">
  <xs:import namespace="urn:base" schemaLocation="redefine.xsd"/>
  <xs:element name="main" type="b:T"/>
</xs:schema>
"""


class TestSchemaWorkspaceRedefine(WorkspaceTestBase):

    def setUp(self):
        super().setUp()
        (self.dir / 'base.xsd').write_text(BASE_REDEFINE_XSD)
        (self.dir / 'redefine.xsd').write_text(REDEFINE_XSD)
        (self.dir / 'main.xsd').write_text(REDEFINE_MAIN_XSD)

    def test_redefine_edge_and_rebuild_propagation(self):
        workspace = self.workspace()
        snapshot = workspace.publish(str(self.dir / 'main.xsd'))

        edges = {(e.source[0].split('/')[-1], e.kind,
                 e.target[0].split('/')[-1]) for e in snapshot.edges}
        self.assertIn(('redefine.xsd', 'redefine', 'base.xsd'), edges)
        self.assertTrue(snapshot.is_valid(
            '<m:main xmlns:m="urn:main" xmlns:b="urn:base">'
            '<b:a>x</b:a><b:b>1</b:b></m:main>'
        ))

        # A change to the redefined document invalidates the redefine and
        # every schema that imports the redefined namespace.
        same = workspace.apply({})
        self.assertEqual(same.stats.resources_reused,
                         {(self.url('base.xsd'), 'urn:base'),
                          (self.url('redefine.xsd'), 'urn:base')})

        new_base = BASE_REDEFINE_XSD.replace('xs:string', 'xs:token')
        changed = workspace.apply({'base.xsd': new_base.encode()})
        self.assertEqual(
            changed.stats.schemas_rebuilt,
            {self.url(name) for name in ('main.xsd', 'redefine.xsd', 'base.xsd')},
        )



INCLUDE_TARGET_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:x" xmlns="urn:x">
  <xs:simpleType name="S">
    <xs:restriction base="xs:string"/>
  </xs:simpleType>
</xs:schema>
"""

INCLUDE_MAIN_XSD = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:x" xmlns="urn:x">
  <xs:include schemaLocation="inc.xsd"/>
  <xs:element name="e" type="S"/>
</xs:schema>
"""


class TestSchemaWorkspaceInclude(unittest.TestCase):

    def test_include_edge_and_incremental_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'inc.xsd').write_text(INCLUDE_TARGET_XSD)
            (directory / 'main.xsd').write_text(INCLUDE_MAIN_XSD)
            workspace = SchemaWorkspace()
            snapshot = workspace.publish(str(directory / 'main.xsd'))

            self.assertEqual(
                [(e.kind, e.target[0].split('/')[-1])
                 for e in snapshot.edges],
                [('include', 'inc.xsd')],
            )
            self.assertTrue(snapshot.is_valid('<e xmlns="urn:x">hello</e>'))

            no_change = workspace.apply({})
            self.assertEqual(
                no_change.stats.resources_reused,
                {((directory / 'inc.xsd').as_uri(), 'urn:x')},
            )

            changed = workspace.apply({
                'inc.xsd': INCLUDE_TARGET_XSD.replace('xs:string', 'xs:token').encode(),
            })
            self.assertEqual(
                changed.stats.schemas_rebuilt,
                {(directory / 'main.xsd').as_uri(),
                 (directory / 'inc.xsd').as_uri()},
            )
            self.assertTrue(changed.is_valid('<e xmlns="urn:x">hello</e>'))


if __name__ == '__main__':
    unittest.main()
