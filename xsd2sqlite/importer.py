"""Import XSD-validated XML documents into SQLite according to a MappingPlan.

Identity and order
------------------
* ``doc_id`` defaults to the SHA-256 of the raw bytes and may be overridden.
* ``node_id`` is a stable ordinal path ("1", "1/2", "1/2/3"): it is a
  position within the document, reusable when the same content is re-imported.
* every element occurrence carries ``ord`` (position among its element
  siblings in document order) and, for choices, ``branch`` (the local name
  of the branch element actually used).

Value states never collapse to a single SQL NULL: each value column has a
companion state column whose value is one of value/empty/nil/default/fixed
(absent rows/columns mean "missing").
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3
from typing import Optional
from xml.etree import ElementTree as ET

from .model import (
    BOOL,
    INTEGER,
    REAL,
    STATE_DEFAULT,
    STATE_EMPTY,
    STATE_FIXED,
    STATE_MISSING,
    STATE_NIL,
    STATE_VALUE,
    XSI_NS,
    split_tag,
)
from .plan import DOC_TABLE, NODE_TABLE, ElementNode, MappingPlan, TableSpec
from .parser import scan_xml_prefixes


class ImportError_(ValueError):
    """Raised when a document does not conform to the mapping plan."""


def connect(database) -> sqlite3.Connection:
    """Open a sqlite3 connection with foreign keys enforced."""
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection, plan: MappingPlan) -> None:
    """Create the tables described by the plan (fails if plan has errors)."""
    if not plan.importable:
        raise ValueError("mapping plan has errors; call create_plan first and "
                         "review plan.issues")
    for statement in plan.create_statements():
        conn.execute(statement)
    conn.commit()


def import_document(
    conn: sqlite3.Connection,
    plan: MappingPlan,
    source,
    *,
    doc_id: Optional[str] = None,
    source_uri: Optional[str] = None,
) -> str:
    """Import one document in a single transaction.

    On failure the transaction is rolled back, so no partial rows remain.
    Re-importing the same ``doc_id`` replaces the previous document atomically.
    """
    if not plan.importable:
        raise ValueError("mapping plan has errors; cannot import")
    raw, resolved_uri = _read_bytes(source, source_uri)
    chosen_id = doc_id or hashlib.sha256(raw).hexdigest()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ImportError_(f"XML parse failure: {exc}") from exc
    for prefix, uri in scan_xml_prefixes(raw.decode("utf-8", "replace")).items():
        ET.register_namespace(prefix, uri)

    try:
        with conn:  # commit on success, rollback on exception
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                f"DELETE FROM {DOC_TABLE} WHERE doc_id = ?", (chosen_id,))
            conn.execute(
                f"INSERT INTO {DOC_TABLE} "
                f"(doc_id, source_uri, root_element, content_sha256) "
                f"VALUES (?, ?, ?, ?)",
                (chosen_id, resolved_uri, _clark(root.tag), digest),
            )
            root_node_binding = plan.root_node
            _insert_element(
                conn=conn,
                plan=plan,
                node_binding=root_node_binding,
                table_spec=plan.table_for_node(root_node_binding),
                element=root,
                doc_id=chosen_id,
                node_id="1",
                parent_node_id=None,
                ord=1,
                ord_path="1",
            )
    except sqlite3.Error as exc:
        raise ImportError_(f"database error while importing: {exc}") from exc
    return chosen_id


# ---------------------------------------------------------------------------
# Insertion
# ---------------------------------------------------------------------------

def _insert_element(
    conn: sqlite3.Connection,
    plan: MappingPlan,
    node_binding: ElementNode,
    table_spec: Optional[TableSpec],
    element: ET.Element,
    doc_id: str,
    node_id: str,
    parent_node_id: Optional[str],
    ord: int,
    ord_path: str,
) -> None:
    expected = _clark(node_binding.element_qname)
    if element.tag != expected:
        raise ImportError_(
            f"{ord_path}: expected element {expected}, got {element.tag}")

    nil = _is_nil(element)
    if nil and not node_binding.nillable:
        raise ImportError_(
            f"{ord_path}: xsi:nil on non-nillable element "
            f"{node_binding.element_qname[1]!r}")

    branch = node_binding.choice_group and node_binding.element_qname[1] or None
    if table_spec is None:
        state, lexeme = _scalar_lexeme(
            node_binding.nillable, node_binding.default, node_binding.fixed,
            node_binding.simple_storage_tag, node_binding.simple_base_qname,
            _text(element) if not nil else None, ord_path, nil=nil)
        _insert_node_row(
            conn, doc_id, node_id, parent_node_id, element.tag, ord,
            ord_path, state, lexeme,
            node_binding.simple_storage_tag, branch)
        return

    if nil:
        _insert_node_row(
            conn, doc_id, node_id, parent_node_id, element.tag, ord,
            ord_path, STATE_NIL, None, None, branch)
        nil_columns = {
            b.state_column: STATE_NIL
            for b in (*table_spec.attributes, *table_spec.scalars)
        }
        _insert_table_row(
            conn, table_spec, doc_id, node_id, STATE_NIL, nil_columns)
        if list(element):
            raise ImportError_(
                f"{ord_path}: xsi:nil element must have no children")
        return

    # The node row is inserted first so child rows can reference it.
    _insert_node_row(
        conn, doc_id, node_id, parent_node_id, element.tag, ord,
        ord_path, STATE_VALUE, None, None, branch)

    columns: dict[str, object] = {}
    ct = table_spec.complex_type
    if ct.kind == "simple":
        _check_no_children(element, ord_path)
        scalar = table_spec.scalars[0]
        state, lexeme = _scalar_lexeme(
            scalar.nillable, scalar.default, scalar.fixed,
            scalar.storage_tag, scalar.base_qname, _text(element), ord_path)
        columns[scalar.column] = _coerce_lexeme(lexeme, scalar.storage_tag)
        columns[scalar.state_column] = state
        conn.execute(
            f"UPDATE {NODE_TABLE} SET state=?, val=?, val_tag=? "
            "WHERE doc_id=? AND node_id=?",
            (state, lexeme, scalar.storage_tag, doc_id, node_id))
    elif ct.kind == "empty":
        _check_no_children(element, ord_path)
        if (_text(element) or "").strip():
            raise ImportError_(
                f"{ord_path}: empty content type has character text")
    else:
        _check_no_character_text(element, ord_path)
        _insert_children(
            conn, plan, table_spec, element, doc_id, node_id, ord_path,
            columns)

    for attr_binding in table_spec.attributes:
        state, value = _attribute_state(attr_binding, element, ord_path)
        columns[attr_binding.column] = value
        columns[attr_binding.state_column] = state

    _insert_table_row(conn, table_spec, doc_id, node_id, STATE_VALUE, columns)


def _insert_children(
    conn: sqlite3.Connection,
    plan: MappingPlan,
    table_spec: TableSpec,
    element: ET.Element,
    doc_id: str,
    parent_node_id: str,
    parent_ord_path: str,
    columns: dict,
) -> None:
    element_children = [
        child for child in list(element) if isinstance(child.tag, str)
    ]
    if len(element_children) != len(list(element)):
        # Comments/processing instructions are fine; only elements map.
        pass

    # Consume children in document order, matching each against the table's
    # node bindings (unique by qname within one table).
    bindings_by_tag = {
        _clark(n.element_qname): n for n in table_spec.children
    }
    counters: dict[str, int] = {}
    seq = 0
    for child in element_children:
        ns, local = split_tag(child.tag)
        if ns == XSI_NS:
            raise ImportError_(
                f"{parent_ord_path}: xsi-namespaced element children are "
                "not allowed")
        binding = bindings_by_tag.get(child.tag)
        if binding is None:
            raise ImportError_(
                f"{parent_ord_path}: element {child.tag} is not declared in "
                f"the mapping of {table_spec.xpath}")
        seq += 1
        seen = counters.get(child.tag, 0) + 1
        counters[child.tag] = seen
        if binding.max_occurs != -1 and seen > binding.max_occurs:
            raise ImportError_(
                f"{parent_ord_path}/{local}: too many occurrences "
                f"(max {binding.max_occurs})")
        child_ord_path = f"{parent_ord_path}/{seq}"
        child_node_id = f"{parent_node_id}/{seq}"
        child_table = plan.table_for_node(binding)
        _insert_element(
            conn=conn,
            plan=plan,
            node_binding=binding,
            table_spec=child_table,
            element=child,
            doc_id=doc_id,
            node_id=child_node_id,
            parent_node_id=parent_node_id,
            ord=seq,
            ord_path=child_ord_path,
        )
        if _is_single_scalar(binding):
            scalar = next(s for s in table_spec.scalars
                          if _clark(s.element_qname) == child.tag)
            if seen != 1:
                raise ImportError_(
                    f"{child_ord_path}: repeated value for single-occurrence "
                    f"element {local!r}")
            state, value = _read_scalar_from_node(
                conn, doc_id, child_node_id, scalar)
            columns[scalar.column] = value
            columns[scalar.state_column] = state

    _check_cardinality(table_spec, counters, parent_ord_path)


def _check_cardinality(table_spec, counters, parent_ord_path) -> None:
    # Validate choice groups first: the minimum of one branch must be met.
    seen_groups: dict[str, int] = {}
    grouped: set[str] = set()
    for binding in table_spec.children:
        if binding.choice_group:
            grouped.add(binding.choice_group)
            seen_groups[binding.choice_group] = (
                seen_groups.get(binding.choice_group, 0)
                + counters.get(_clark(binding.element_qname), 0))
    for group in grouped:
        total = seen_groups[group]
        if total < 1:
            branches = [
                n.element_qname[1] for n in table_spec.children
                if n.choice_group == group
            ]
            raise ImportError_(
                f"{parent_ord_path}: choice group requires one of {branches}")
    for binding in table_spec.children:
        if binding.choice_group:
            continue
        seen = counters.get(_clark(binding.element_qname), 0)
        # An element with an XSD default/fixed is optional at instance level
        # even when declared with minOccurs=1: absence is legal and means the
        # default applies only at the PSVI level, not as stored content.
        if (seen < binding.min_occurs
                and binding.default is None
                and binding.fixed is None):
            raise ImportError_(
                f"{parent_ord_path}: missing required element "
                f"{binding.element_qname[1]!r} "
                f"({seen} < {binding.min_occurs})")


def _is_single_scalar(binding: ElementNode) -> bool:
    return (binding.table is None
            and not binding.is_repeated_simple
            and binding.choice_group is None
            and binding.simple_storage_tag is not None)


def _read_scalar_from_node(conn, doc_id, node_id, scalar):
    row = conn.execute(
        f"SELECT state, val, val_tag FROM {NODE_TABLE} "
        "WHERE doc_id=? AND node_id=?", (doc_id, node_id)).fetchone()
    state, lexeme, tag = row
    value = _coerce_lexeme(lexeme, tag)
    return state, value


def _insert_node_row(
    conn, doc_id, node_id, parent_node_id, tag, ord, ord_path,
    state, lexeme, storage_tag, branch,
) -> None:
    conn.execute(
        f"INSERT INTO {NODE_TABLE} "
        "(doc_id, node_id, parent_node_id, qname, ord, ord_path, state, "
        "val, val_tag, branch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, node_id, parent_node_id, tag, ord, ord_path, state,
         lexeme, storage_tag, branch),
    )


def _insert_table_row(conn, table, doc_id, node_id, state, columns) -> None:
    names = ["doc_id", "node_id", "state"]
    values = [doc_id, node_id, state]
    for key, value in columns.items():
        names.append(key)
        values.append(value)
    # Columns not supplied stay at their DEFAULT 'missing' (nil rows).
    placeholders = ", ".join("?" for _ in values)
    quoted = ", ".join(f'"{n}"' for n in names)
    conn.execute(
        f'INSERT INTO "{table.name}" ({quoted}) VALUES ({placeholders})',
        values)


# ---------------------------------------------------------------------------
# Value states and conversion
# ---------------------------------------------------------------------------

def _scalar_lexeme(
    nillable, default, fixed, storage_tag, base_qname, text, ord_path,
    *, nil=False,
):
    """Return ``(state, lexical_value)`` for a simple element occurrence."""
    if nil:
        return STATE_NIL, None
    lexeme = text if text is not None else ""
    # Whitespace collapse (XSD whitespace=preserve lexical info beyond
    # leading/trailing spacing is not tracked; documents are validated first).
    lexeme = lexeme.strip()
    if lexeme:
        _check_coercible(lexeme, storage_tag, base_qname, ord_path)
        return STATE_VALUE, lexeme
    if fixed is not None:
        return STATE_FIXED, fixed
    if default is not None:
        return STATE_DEFAULT, default
    if _is_string_family(base_qname):
        # An explicitly empty element of a string-like type carries an
        # explicit empty string; record it as 'value', distinct from absence.
        return STATE_VALUE, ""
    return STATE_EMPTY, None


def _attribute_state(binding, element, ord_path):
    raw = _attribute_by_qname(element, binding.attribute_qname)
    if raw is None:
        if binding.required:
            raise ImportError_(
                f"{ord_path}: missing required attribute "
                f"{binding.attribute_qname[1]!r}")
        if binding.fixed is not None:
            return STATE_FIXED, binding.fixed
        if binding.default is not None:
            return STATE_DEFAULT, binding.default
        return STATE_MISSING, None
    lexeme = raw.strip()
    _check_coercible(
        lexeme, binding.storage_tag, binding.base_qname, ord_path)
    if binding.fixed is not None and lexeme != binding.fixed:
        raise ImportError_(
            f"{ord_path}: attribute {binding.attribute_qname[1]!r} must "
            f"equal fixed value {binding.fixed!r}")
    return STATE_VALUE, lexeme


def _attribute_by_qname(element, qname):
    ns, local = qname
    if ns is None:
        # An unqualified attribute matches regardless of the parser's
        # namespace handling of no-namespace attributes.
        if local in element.attrib:
            return element.attrib[local]
        return element.attrib.get(f"{{}}{local}")
    return element.attrib.get(f"{{{ns}}}{local}")


def _coerce_lexeme(lexeme, storage_tag):
    if lexeme is None:
        return None
    if storage_tag == INTEGER:
        return int(lexeme)
    if storage_tag == REAL:
        return float(lexeme)
    if storage_tag == BOOL:
        if lexeme in ("true", "1"):
            return 1
        if lexeme in ("false", "0"):
            return 0
        raise ValueError(f"invalid boolean lexeme {lexeme!r}")
    return lexeme


def _check_coercible(lexeme, storage_tag, base_qname, ord_path):
    try:
        if storage_tag == INTEGER:
            int(lexeme)
        elif storage_tag == REAL:
            float(lexeme)
        elif storage_tag == BOOL and lexeme not in (
                "true", "false", "0", "1"):
            raise ValueError
    except ValueError:
        raise ImportError_(
            f"{ord_path}: value {lexeme!r} is not valid for type "
            f"{base_qname[1]}") from None


_STRING_FAMILY = {
    "string", "normalizedString", "token", "language", "NMTOKEN",
    "Name", "NCName", "ID", "IDREF", "ENTITY", "anySimpleType",
    "anyURI", "QName", "NOTATION",
}


def _is_string_family(base_qname) -> bool:
    ns, local = base_qname
    return ns == "http://www.w3.org/2001/XMLSchema" and local in _STRING_FAMILY


# ---------------------------------------------------------------------------
# Element helpers
# ---------------------------------------------------------------------------

def _is_nil(element: ET.Element) -> bool:
    raw = element.attrib.get(f"{{{XSI_NS}}}nil")
    return raw in ("true", "1")


def _text(element: ET.Element) -> Optional[str]:
    return element.text


def _check_no_children(element, ord_path) -> None:
    if list(element):
        raise ImportError_(
            f"{ord_path}: element has unexpected children")


def _check_no_character_text(element, ord_path) -> None:
    if element.text is not None and element.text.strip():
        raise ImportError_(
            f"{ord_path}: non-whitespace character text in element-only "
            "content (mixed content is not supported)")
    for child in list(element):
        if child.tail is not None and child.tail.strip():
            raise ImportError_(
                f"{ord_path}: non-whitespace character text between child "
                "elements (mixed content is not supported)")


def _clark(qname) -> str:
    if isinstance(qname, str):
        return qname
    ns, local = qname
    return f"{{{ns}}}{local}" if ns else local


def _read_bytes(source, source_uri):
    if isinstance(source, (bytes, bytearray)):
        return bytes(source), source_uri
    if isinstance(source, str) and source.lstrip().startswith(
            ("<", "\ufeff<", "<?xml")):
        data = source.encode("utf-8")
        return data, source_uri
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data, source_uri or "<stream>"
    path = source
    with open(path, "rb") as fh:
        return fh.read(), source_uri or str(path)
