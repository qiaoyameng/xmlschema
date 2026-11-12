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
"""Tests for streaming record encoding to a binary sink."""
import gc
import io
import tracemalloc
import unittest
from xml.etree import ElementTree

from xmlschema import XMLSchema
from xmlschema.exceptions import XMLSchemaValueError
from xmlschema.streaming import encode_records, XMLSchemaStreamError, \
    RecordStreamResult

SIMPLE_RECORD_SCHEMA = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="rec" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="id" type="xs:int"/>
              <xs:element name="name" type="xs:string"/>
            </xs:sequence>
            <xs:attribute name="code" type="xs:string" default="D"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
      <xs:attribute name="version" type="xs:string" default="1.0"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

IDENTITY_SCHEMA = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="rec" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="id" type="xs:string"/>
              <xs:element name="ref" type="xs:string" minOccurs="0"/>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="k">
      <xs:selector xpath="rec"/>
      <xs:field xpath="id"/>
    </xs:key>
    <xs:keyref name="kr" refer="k">
      <xs:selector xpath="rec"/>
      <xs:field xpath="ref"/>
    </xs:keyref>
  </xs:element>
</xs:schema>"""

NAMESPACED_SCHEMA = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="http://example.com/ns/streaming"
    elementFormDefault="qualified">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="rec" type="xs:string" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


class LimitedWriteSink:
    """A binary sink that refuses every write bigger than *max_chunk* bytes."""

    def __init__(self, max_chunk=8):
        self.buffer = bytearray()
        self.max_chunk = max_chunk
        self.closed = False

    def write(self, data):
        n = min(len(data), self.max_chunk)
        self.buffer += bytes(data[:n])
        return n

    def close(self):
        self.closed = True


class RefusingSink:
    """A binary sink that always refuses to write any data."""

    def __init__(self):
        self.buffer = bytearray()

    def write(self, data):
        return 0


class RaisingSink:
    """A binary sink that raises an OSError while writing."""

    def __init__(self, raise_after=0):
        self.buffer = bytearray()
        self.raise_after = raise_after
        self.closed = False

    def write(self, data):
        if len(self.buffer) >= self.raise_after:
            raise OSError("device not ready")
        n = len(data)
        self.buffer += bytes(data)
        return n

    def close(self):
        self.closed = True


class OneShotSource:
    """A record source that can be iterated only once."""

    def __init__(self, values):
        self.values = list(values)
        self.iterations = 0

    def __iter__(self):
        if self.iterations:
            raise RuntimeError("source can be iterated only once")
        self.iterations += 1
        for value in self.values:
            yield value


class FailingSource:
    def __init__(self, values, failing_at, error):
        self.values = list(values)
        self.failing_at = failing_at
        self.error = error

    def __iter__(self):
        for index, value in enumerate(self.values):
            if index == self.failing_at:
                raise self.error
            yield value


class TestStreamingEncoding(unittest.TestCase):

    def setUp(self):
        self.schema = XMLSchema(SIMPLE_RECORD_SCHEMA)
        self.identity_schema = XMLSchema(IDENTITY_SCHEMA)

    def test_happy_path_with_chunked_sink_and_one_shot_source(self):
        sink = LimitedWriteSink(max_chunk=7)
        source = OneShotSource([
            {'id': 1, 'name': 'first'},
            {'id': 2, 'name': 'second'},
        ])

        result = encode_records(self.schema, source, sink)

        self.assertIsInstance(result, RecordStreamResult)
        self.assertTrue(result.completed)
        self.assertIsNone(result.error)
        self.assertEqual(result.records_written, 2)
        self.assertEqual(result.bytes_written, len(sink.buffer))
        self.assertEqual(source.iterations, 1)

        # The written document must parse and validate against the schema.
        root = ElementTree.fromstring(bytes(sink.buffer))
        self.assertEqual(root.tag, 'root')
        self.assertEqual([child.tag for child in root], ['rec', 'rec'])
        self.assertEqual([child.findtext('id') for child in root], ['1', '2'])
        self.schema.validate(root)

    def test_records_keep_order_and_apply_defaults_and_conversions(self):
        sink = io.BytesIO()
        records = [{'id': 10, 'name': 'a'}, {'id': 20, 'name': 'b'}]
        result = encode_records(self.schema, records, sink)
        self.assertTrue(result.completed)

        root = ElementTree.fromstring(sink.getvalue())
        self.assertEqual([child.findtext('id') for child in root], ['10', '20'])
        self.assertEqual({child.get('code') for child in root}, {'D'})
        self.assertEqual(root.get('version'), '1.0')

    def test_success_read_back_with_full_document_validation(self):
        sink = io.BytesIO()
        result = encode_records(self.schema, [
            {'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}
        ], sink)
        self.assertTrue(result.completed)
        self.assertTrue(self.schema.is_valid(ElementTree.fromstring(sink.getvalue())))
        self.assertEqual(
            result.bytes_written, len(sink.getvalue())
        )

    def test_schema_method_entry_point(self):
        sink = io.BytesIO()
        result = self.schema.encode_records([{'id': 1, 'name': 'a'}], sink)
        self.assertTrue(result.completed)
        self.schema.validate(ElementTree.fromstring(sink.getvalue()))

    def test_namespaced_output(self):
        schema = XMLSchema(NAMESPACED_SCHEMA)
        sink = LimitedWriteSink(max_chunk=5)
        result = encode_records(
            schema, ['a', 'b'], sink,
            namespaces={'t': 'http://example.com/ns/streaming'}
        )
        self.assertTrue(result.completed)
        root = ElementTree.fromstring(bytes(sink.buffer))
        self.assertEqual(root.tag, '{http://example.com/ns/streaming}root')
        schema.validate(root)

    def test_schema_shape_rejection(self):
        multi_particle = XMLSchema("""
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root"><xs:complexType><xs:sequence>
            <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
            <xs:element name="b" type="xs:string"/>
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>""")
        with self.assertRaises(XMLSchemaValueError):
            encode_records(multi_particle, [], io.BytesIO())

        single = XMLSchema("""
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root"><xs:complexType><xs:sequence>
            <xs:element name="rec" type="xs:string"/>
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>""")
        with self.assertRaises(XMLSchemaValueError):
            encode_records(single, [], io.BytesIO())

    def test_occurrence_bounds(self):
        schema = XMLSchema("""
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="root"><xs:complexType><xs:sequence>
            <xs:element name="rec" type="xs:string" maxOccurs="2"/>
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>""")

        sink = io.BytesIO()
        result = encode_records(schema, ['a', 'b', 'c'], sink)
        self.assertEqual(result.records_written, 2)
        self.assertFalse(result.completed)

        sink = io.BytesIO()
        result = encode_records(schema, [], sink)
        self.assertFalse(result.completed)

        sink = io.BytesIO()
        result = encode_records(schema, ['a', 'b'], sink)
        self.assertTrue(result.completed)

    def test_validation_failure_leaves_only_valid_prefix(self):
        sink = io.BytesIO()
        result = encode_records(self.schema, [
            {'id': 1, 'name': 'ok'},
            {'id': 'not-an-integer', 'name': 'bad'},
        ], sink)

        self.assertFalse(result.completed)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.records_written, 1)
        self.assertEqual(result.bytes_written, len(sink.getvalue()))
        self.assertEqual(result.written_range, (0, len(sink.getvalue())))
        self.assertNotIn(b'</root>', sink.getvalue())

        # The written bytes are a valid prefix: closing the root makes it parseable
        # and the completed part validates against the schema.
        completed = sink.getvalue() + b'</root>'
        root = ElementTree.fromstring(completed)
        self.schema.validate(root)

        # But it must not be confused with a fully validated document.
        with self.assertRaises(ElementTree.ParseError):
            ElementTree.fromstring(sink.getvalue())

        # The same failure is raised as an exception when requested.
        with self.assertRaises(XMLSchemaStreamError) as ctx:
            encode_records(self.schema, [
                {'id': 'not-an-integer', 'name': 'bad'}
            ], io.BytesIO(), raise_on_error=True)
        self.assertEqual(ctx.exception.result.records_written, 0)

    def test_duplicated_key_failure_distinguishes_prefix_from_document(self):
        sink = io.BytesIO()
        result = encode_records(self.identity_schema, [
            {'id': 'a', 'ref': 'a'},
            {'id': 'a', 'ref': 'a'},
        ], sink)

        self.assertFalse(result.completed)
        self.assertEqual(result.records_written, 1)
        self.assertIn('duplicated value', result.reason)

        # The prefix is a legal fragment when manually closed.
        root = ElementTree.fromstring(sink.getvalue() + b'</root>')
        self.assertEqual(len(root), 1)

        # A complete document built from the same data is invalid.
        bad_document = (
            b'<?xml version="1.0" encoding="utf-8"?><root>'
            b'<rec><id>a</id><ref>a</ref></rec>'
            b'<rec><id>a</id><ref>a</ref></rec></root>'
        )
        self.assertFalse(self.identity_schema.is_valid(
            ElementTree.fromstring(bad_document)
        ))

    def test_unresolved_keyref_fails_before_closing_tag(self):
        sink = io.BytesIO()
        result = encode_records(self.identity_schema, [
            {'id': 'a'}, {'id': 'b', 'ref': 'missing'}
        ], sink)

        self.assertFalse(result.completed)
        self.assertEqual(result.records_written, 2)
        self.assertNotIn(b'</root>', sink.getvalue())
        # The valid prefix still parses when closed.
        root = ElementTree.fromstring(sink.getvalue() + b'</root>')
        self.assertEqual(len(root), 2)

    def test_valid_keyref_completes(self):
        sink = io.BytesIO()
        result = encode_records(self.identity_schema, [
            {'id': 'a'}, {'id': 'b', 'ref': 'a'}
        ], sink)
        self.assertTrue(result.completed)
        self.identity_schema.validate(ElementTree.fromstring(sink.getvalue()))

    def test_short_write_sink_is_handled(self):
        sink = LimitedWriteSink(max_chunk=3)
        result = encode_records(self.schema, [
            {'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}
        ], sink)
        self.assertTrue(result.completed)
        self.assertEqual(result.bytes_written, len(sink.buffer))
        self.schema.validate(ElementTree.fromstring(bytes(sink.buffer)))

    def test_refusing_sink_enter_non_continuable_state(self):
        sink = RefusingSink()
        result = encode_records(self.schema, [{'id': 1, 'name': 'a'}], sink)
        self.assertEqual(result.bytes_written, 0)
        self.assertFalse(result.completed)
        self.assertIsNotNone(result.error)

    def test_sink_exception_is_reported_and_sink_not_closed(self):
        sink = RaisingSink(raise_after=10)
        result = encode_records(self.schema, [
            {'id': 1, 'name': 'x' * 50}
        ], sink)
        self.assertIsInstance(result.error, OSError)
        self.assertFalse(result.completed)
        self.assertFalse(sink.closed)

    def test_source_exception_keeps_written_range(self):
        sink = io.BytesIO()
        source = FailingSource(
            [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}],
            failing_at=1, error=RuntimeError("database connection lost"),
        )
        result = encode_records(self.schema, source, sink)

        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(result.records_written, 1)
        self.assertEqual(result.bytes_written, len(sink.getvalue()))
        self.assertNotIn(b'</root>', sink.getvalue())
        # No closing tag appended after the failure.
        root = ElementTree.fromstring(sink.getvalue() + b'</root>')
        self.schema.validate(root)

    def test_one_shot_source_is_consumed_once(self):
        sink = LimitedWriteSink(max_chunk=4)
        source = OneShotSource([{'id': 1, 'name': 'a'}])
        result = encode_records(self.schema, source, sink)
        self.assertTrue(result.completed)
        self.assertEqual(source.iterations, 1)

    def test_memory_does_not_grow_with_record_count(self):
        class NullSink:
            def write(self, data):
                return len(data)

        def records(count):
            for index in range(count):
                yield {'id': index, 'name': f'name-{index}'}

        tracemalloc.start()
        try:
            gc.collect()
            encode_records(self.schema, records(2000), NullSink())
            gc.collect()
            first_current, first_peak = tracemalloc.get_traced_memory()

            tracemalloc.reset_peak()
            encode_records(self.schema, records(20000), NullSink())
            gc.collect()
            second_current, second_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # Retained memory must not grow with the number of processed records.
        self.assertLess(abs(second_current - first_current), 256 * 1024)
        self.assertLess(second_peak - first_peak, 1024 * 1024)

    def test_non_utf8_encoding(self):
        schema = XMLSchema(NAMESPACED_SCHEMA)
        sink = io.BytesIO()
        result = encode_records(schema, ['café'], sink, encoding='iso-8859-1')
        self.assertTrue(result.completed)
        self.assertIn(b'encoding="iso-8859-1"', sink.getvalue())
        schema.validate(ElementTree.fromstring(sink.getvalue()))

    def test_unknown_encoding_rejected(self):
        sink = io.BytesIO()
        with self.assertRaises(XMLSchemaValueError):
            encode_records(self.schema, [], sink, encoding='no-such-codec')

    def test_sink_without_write_rejected(self):
        with self.assertRaises(TypeError):
            encode_records(self.schema, [], object())

    def test_sink_is_never_closed(self):
        sink = LimitedWriteSink(max_chunk=4)
        encode_records(self.schema, [{'id': 1, 'name': 'a'}], sink)
        self.assertFalse(sink.closed)

    def test_result_reported_on_failure_is_not_success(self):
        result = encode_records(self.schema, [{'id': 'bad', 'name': 'x'}], io.BytesIO())
        self.assertFalse(bool(result))
        self.assertFalse(result.completed)
        self.assertIsNotNone(result.error)
        self.assertIsNotNone(result.reason)
        self.assertEqual(result.written_range, (0, result.bytes_written))


if __name__ == '__main__':
    unittest.main()
