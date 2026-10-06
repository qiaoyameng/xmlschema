"""Reference rebuild of XML documents from the relational storage.

The rebuild is driven by the same :class:`MappingPlan` bindings used for
import, so it never has to guess structure.  Only explicitly present content
is serialized: states ``default``/``fixed`` stay available for analysis in
their columns but are not injected, ``missing`` creates no nodes, ``empty``
creates an empty element and ``nil`` serializes ``xsi:nil="true"``.
"""
from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET

from .model import XSI_NS
from .plan import NODE_TABLE, MappingPlan


def rebuild_document(
    conn: sqlite3.Connection,
    plan: MappingPlan,
    doc_id: str,
    *,
    xml_declaration: bool = True,
) -> bytes:
    """Rebuild one stored document as canonical XML bytes."""
    _register_prefixes(plan)
    root_row = conn.execute(
        f"SELECT node_id, qname FROM {NODE_TABLE} "
        "WHERE doc_id=? AND parent_node_id IS NULL",
        (doc_id,),
    ).fetchone()
    if root_row is None:
        raise KeyError(f"document {doc_id!r} not found")
    root_node_id, _qname = root_row
    root_element = _rebuild_node(
        conn, plan, plan.root_node, doc_id, root_node_id)
    return ET.tostring(
        root_element, encoding="utf-8", xml_declaration=xml_declaration)


def _rebuild_node(conn, plan, node_binding, doc_id, node_id) -> ET.Element:
    row = conn.execute(
        f"SELECT qname, state, val FROM {NODE_TABLE} "
        "WHERE doc_id=? AND node_id=?",
        (doc_id, node_id),
    ).fetchone()
    qname_clark, state, val = row
    element = ET.Element(qname_clark)

    if state == "nil":
        element.set(f"{{{XSI_NS}}}nil", "true")
        return element

    table = plan.table_for_node(node_binding)
    if table is None:
        element.text = _text_for_state(state, val)
        return element

    table_row = _fetch_table_row(conn, table, doc_id, node_id)

    for attr in table.attributes:
        if table_row.get(attr.state_column) == "value":
            value = table_row.get(attr.column)
            if attr.storage_tag == "BOOL" and value is not None:
                value = "true" if value else "false"
            element.set(_attr_clark(attr.attribute_qname), str(value))

    ct = table.complex_type
    if ct.kind == "simple":
        scalar = table.scalars[0]
        scalar_state = table_row.get(scalar.state_column)
        element.text = _text_for_state(
            scalar_state, _node_val(conn, doc_id, node_id))
        return element

    if ct.kind == "empty":
        return element

    child_rows = conn.execute(
        f"SELECT node_id, qname FROM {NODE_TABLE} "
        "WHERE doc_id=? AND parent_node_id=? ORDER BY ord",
        (doc_id, node_id),
    ).fetchall()
    binding_by_tag = {
        _clark(n.element_qname): n for n in table.children
    }
    for child_id, child_qname in child_rows:
        child_binding = binding_by_tag[child_qname]
        element.append(_rebuild_node(
            conn, plan, child_binding, doc_id, child_id))
    return element


def _node_val(conn, doc_id, node_id):
    return conn.execute(
        f"SELECT val FROM {NODE_TABLE} WHERE doc_id=? AND node_id=?",
        (doc_id, node_id)).fetchone()[0]


def _text_for_state(state, val):
    """Map an import state back to the serialized element text.

    ``default``/``fixed`` values are kept in the relational columns for
    analysis but are not injected into the reference XML: an element that was
    written empty stays empty.
    """
    if state == "value":
        return val if val is not None else ""
    if state in ("empty", "default"):
        return None
    return None


def _fetch_table_row(conn, table, doc_id, node_id):
    columns = ["state"]
    for binding in (*table.attributes, *table.scalars):
        columns.extend([binding.column, binding.state_column])
    quoted = ", ".join(f'"{c}"' for c in columns)
    row = conn.execute(
        f'SELECT {quoted} FROM "{table.name}" '
        "WHERE doc_id=? AND node_id=?",
        (doc_id, node_id),
    ).fetchone()
    return dict(zip(columns, row))


def _clark(qname) -> str:
    ns, local = qname
    return f"{{{ns}}}{local}" if ns else local


def _attr_clark(qname) -> str:
    ns, local = qname
    return f"{{{ns}}}{local}" if ns else local


def _register_prefixes(plan: MappingPlan) -> None:
    prefixes = plan.schema_info.prefixes
    for prefix, uri in prefixes.items():
        ET.register_namespace(prefix, uri)
    ET.register_namespace("xsi", XSI_NS)
