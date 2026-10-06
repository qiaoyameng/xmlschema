"""Relational mapping plan: derived from a parsed XSD before any row is read.

The plan is deliberately explicit and serializable.  Every element binding
gets an identity table row (``_node``), so even repeated simple elements keep
stable internal ids, document-relative ordinal paths and, for choice content,
the branch actually taken.  Complex types additionally receive one table for
their attributes, scalar single children and simple content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import (
    DDL_TYPE,
    ComplexType,
    ParsedSchema,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

DOC_TABLE = "_document"
NODE_TABLE = "_node"


@dataclass
class Issue:
    severity: str
    path: str
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "path": self.path,
                "message": self.message}


@dataclass
class ScalarBinding:
    """A single-valued simple particle or the simpleContent text itself."""

    element_qname: tuple[Optional[str], str]
    nillable: bool
    default: Optional[str]
    fixed: Optional[str]
    storage_tag: str
    column: str
    state_column: str
    min_occurs: int = 1
    choice_group: Optional[str] = None
    is_content: bool = False
    base_qname: tuple = ((None), "")


@dataclass
class AttrBinding:
    attribute_qname: tuple[Optional[str], str]
    storage_tag: str
    column: str
    state_column: str
    required: bool
    default: Optional[str]
    fixed: Optional[str]
    base_qname: tuple = (None, "")


@dataclass
class ElementNode:
    """One element occurrence site that maps to ``_node`` rows.

    Every element that can actually appear (including repeated simple ones)
    has a node binding.  ``table`` is set when the element is complex (its
    companion row lives there).  The importer/rebuilder never needs to
    rediscover structure: they walk this binding graph.
    """

    element_qname: tuple[Optional[str], str]
    path: str
    min_occurs: int
    max_occurs: int
    nillable: bool
    default: Optional[str]
    fixed: Optional[str]
    is_repeated_simple: bool = False
    simple_storage_tag: Optional[str] = None
    simple_base_qname: Optional[tuple] = None
    table: Optional[str] = None
    choice_group: Optional[str] = None


@dataclass
class TableSpec:
    name: str
    xpath: str
    complex_type: ComplexType
    element_qname: tuple[Optional[str], str]
    root: bool = False
    attributes: list[AttrBinding] = field(default_factory=list)
    scalars: list[ScalarBinding] = field(default_factory=list)
    children: list[ElementNode] = field(default_factory=list)
    choice_groups: list[str] = field(default_factory=list)


class MappingPlan:
    """The full mapping plan for one root element of one XSD."""

    def __init__(self, parsed: ParsedSchema) -> None:
        self.schema_info = parsed.schema_info
        self._root_provider = (
            parsed.root_element if callable(parsed.root_element)
            else (lambda: parsed.root_element)
        )
        self.tables: list[TableSpec] = []
        self.issues: list[Issue] = []
        self.root_node: Optional[ElementNode] = None
        self._used_names: set[str] = {DOC_TABLE, NODE_TABLE}
        self._choice_seq = 0

    @property
    def root_decl(self):
        return self._root_provider()

    # -- plan properties --------------------------------------------------

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def importable(self) -> bool:
        return not self.errors

    def error(self, path: str, message: str) -> None:
        self.issues.append(Issue(SEVERITY_ERROR, path, message))

    def warning(self, path: str, message: str) -> None:
        self.issues.append(Issue(SEVERITY_WARNING, path, message))

    def unique_table_name(self, base: str) -> str:
        candidate = _identifier(base)
        if candidate not in self._used_names:
            self._used_names.add(candidate)
            return candidate
        suffix = 2
        while f"{candidate}_{suffix}" in self._used_names:
            suffix += 1
        candidate = f"{candidate}_{suffix}"
        self._used_names.add(candidate)
        return candidate

    def next_choice_group(self) -> str:
        self._choice_seq += 1
        return f"_choice_{self._choice_seq}"

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "target_namespace": self.schema_info.target_namespace,
            "root_element": _qname(self.root_decl.qname),
            "importable": self.importable,
            "issues": [i.to_dict() for i in self.issues],
            "tables": [
                {
                    "name": t.name,
                    "xpath": t.xpath,
                    "element": _qname(t.element_qname),
                    "root": t.root,
                    "attributes": [
                        {"name": a.attribute_qname[1],
                         "column": a.column,
                         "state_column": a.state_column,
                         "type": DDL_TYPE[a.storage_tag],
                         "required": a.required,
                         "default": a.default,
                         "fixed": a.fixed}
                        for a in t.attributes
                    ],
                    "scalars": [
                        {"element": s.element_qname[1],
                         "column": s.column,
                         "state_column": s.state_column,
                         "type": DDL_TYPE[s.storage_tag],
                         "min_occurs": s.min_occurs,
                         "nillable": s.nillable,
                         "default": s.default,
                         "fixed": s.fixed,
                         "choice_group": s.choice_group,
                         "is_content": s.is_content}
                        for s in t.scalars
                    ],
                    "child_nodes": [
                        {"element": n.element_qname[1],
                         "path": n.path,
                         "table": n.table,
                         "min_occurs": n.min_occurs,
                         "max_occurs": n.max_occurs,
                         "repeated_simple": n.is_repeated_simple,
                         "choice_group": n.choice_group}
                        for n in t.children
                    ],
                    "choice_groups": t.choice_groups,
                }
                for t in self.tables
            ],
        }

    # -- DDL --------------------------------------------------------------

    def create_statements(self) -> list[str]:
        """Return the CREATE TABLE / CREATE INDEX statements of the plan."""
        statements = [
            f'CREATE TABLE {_q(DOC_TABLE)} (\n'
            f'    doc_id TEXT PRIMARY KEY,\n'
            f'    source_uri TEXT,\n'
            f'    root_element TEXT,\n'
            f'    content_sha256 TEXT,\n'
            f'    imported_at TEXT NOT NULL DEFAULT (datetime(\'now\'))\n'
            f')',
            f'CREATE TABLE {_q(NODE_TABLE)} (\n'
            f'    doc_id TEXT NOT NULL\n'
            f'        REFERENCES {_q(DOC_TABLE)}(doc_id)\n'
            f'        ON DELETE CASCADE ON UPDATE CASCADE,\n'
            f'    node_id TEXT NOT NULL,\n'
            f'    parent_node_id TEXT,\n'
            f'    qname TEXT NOT NULL,\n'
            f'    ord INTEGER,\n'
            f'    ord_path TEXT NOT NULL,\n'
            f'    state TEXT NOT NULL,\n'
            f'    val BLOB,\n'
            f'    val_tag TEXT,\n'
            f'    branch TEXT,\n'
            f'    PRIMARY KEY (doc_id, node_id),\n'
            f'    FOREIGN KEY (doc_id, parent_node_id)\n'
            f'        REFERENCES {_q(NODE_TABLE)}(doc_id, node_id)\n'
            f'        ON DELETE CASCADE ON UPDATE CASCADE\n'
            f')',
            "CREATE INDEX _node_parent_idx ON _node(doc_id, parent_node_id)",
        ]
        for table in self.tables:
            lines = [
                f'CREATE TABLE {_q(table.name)} (',
                "    doc_id TEXT NOT NULL,",
                "    node_id TEXT NOT NULL,",
                "    state TEXT NOT NULL",
            ]
            for binding in table.attributes:
                sql_type = DDL_TYPE[binding.storage_tag]
                lines[-1] += ","
                lines.append(f'    {_q(binding.column)} {sql_type}')
                lines[-2] = lines[-2]  # no-op keep style
                lines[-1] += ","
                lines.append(f'    {_q(binding.state_column)} TEXT NOT NULL'
                             f" DEFAULT 'missing'")
            for scalar in table.scalars:
                sql_type = DDL_TYPE[scalar.storage_tag]
                lines[-1] += ","
                lines.append(f'    {_q(scalar.column)} {sql_type}')
                lines[-1] += ","
                lines.append(
                    f'    {_q(scalar.state_column)} TEXT NOT NULL'
                    f" DEFAULT 'missing'")
            lines[-1] += ","
            lines.append("    PRIMARY KEY (doc_id, node_id),")
            lines.append(
                "    FOREIGN KEY (doc_id, node_id)\n"
                f'        REFERENCES {_q(NODE_TABLE)}(doc_id, node_id)\n'
                "        ON DELETE CASCADE ON UPDATE CASCADE")
            lines.append(")")
            statements.append("\n".join(lines))
            statements.append(
                f"CREATE INDEX {_q(table.name + '_doc_idx')} "
                f"ON {_q(table.name)}(doc_id)")
        return statements

    def table_for_node(self, node: ElementNode) -> Optional[TableSpec]:
        if node.table is None:
            return None
        return next(t for t in self.tables if t.name == node.table)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _identifier(name: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                      for ch in name)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"_{cleaned}"
    return cleaned[:50]


def _qname(qname: tuple[Optional[str], str]) -> str:
    ns, local = qname
    return f"{{{ns}}}{local}" if ns else local
