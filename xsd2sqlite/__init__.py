"""XSD-driven, scope-limited relational mapper for SQLite (stdlib only).

Typical flow::

    from xsd2sqlite import (
        create_plan, connect, create_schema, import_document,
        rebuild_document,
    )

    plan = create_plan("schema.xsd")
    assert plan.importable, [i.message for i in plan.errors]
    conn = connect("analysis.db")
    create_schema(conn, plan)
    doc_id = import_document(conn, plan, "doc.xml")
    xml_bytes = rebuild_document(conn, plan, doc_id)

Value states (value/empty/nil/default/fixed/missing) are kept distinct via
state columns and the ``_node.state`` column; they never collapse into SQL
NULL.  See ``plan.issues`` for every out-of-scope construct.
"""
from .importer import (
    ImportError_ as ImportError,
    connect,
    create_schema,
    import_document,
)
from .parser import parse_schema
from .plan import MappingPlan, Issue
from .planner import build_plan
from .rebuilder import rebuild_document

__all__ = [
    "create_plan",
    "parse_schema",
    "build_plan",
    "connect",
    "create_schema",
    "import_document",
    "rebuild_document",
    "MappingPlan",
    "Issue",
    "ImportError",
]

__version__ = "0.1.0"


def create_plan(xsd_source, root_element: str | None = None) -> MappingPlan:
    """Parse an XSD and derive its relational mapping plan.

    The returned plan is always populated; inspect ``plan.importable`` and
    ``plan.issues`` before creating the database schema.
    """
    parsed = parse_schema(xsd_source, root_element=root_element)
    return build_plan(parsed)
