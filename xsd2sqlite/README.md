# xsd2sqlite

A scope-limited, dependency-free (Python stdlib only, incl. `sqlite3`)
relational mapper for importing **XSD-validated** XML documents into SQLite
for analysis. It plans first, imports second, and can rebuild reference XML
from the stored rows.

## Pipeline

1. **Plan** — `create_plan(xsd)` walks the schema reachable from one document
   element and produces a serializable `MappingPlan` (tables, columns, child
   node bindings, choice groups) plus an explicit `issues` list. Nothing is
   silently dropped: unsupported constructs are reported as plan errors.
2. **Create** — `create_schema(conn, plan)` emits the DDL (only when the plan
   is importable).
3. **Import** — `import_document(conn, plan, xml)` loads one document in a
   single transaction; a failure rolls back completely.
4. **Rebuild** — `rebuild_document(conn, plan, doc_id)` serializes reference
   XML back out, driven by the same bindings.

## Supported scope (v1)

- `xs:sequence` and `xs:choice` (one occurrence of the choice; its element
  branches keep their own cardinality), including nested sequences/choices;
- attributes (required/optional, `default`, `fixed`);
- empty, element-only and simple-content (`xs:extension`) complex types;
- repeated elements with arbitrary finite nesting depth;
- named/anonymous simple types derived by `xs:restriction` from XSD built-ins
  (facets are recorded as warnings; documents are assumed already validated);
- named complex types, `xs:group`/`xs:attributeGroup` references.

## Explicitly rejected at plan time

Mixed content, `xs:any` / `xs:anyAttribute` wildcards, `xs:all`,
`xs:list`/`xs:union`, `xs:anyType`/untyped elements, substitution groups and
abstract elements, recursive schemas, repeating `sequence`/`choice` groups.
Each appears in `plan.issues` (`severity="error"`); `plan.importable` is then
false and no schema is created.

## Identity, order and value states

- `_document(doc_id, source_uri, root_element, content_sha256, imported_at)`:
  `doc_id` defaults to the SHA-256 of the raw bytes and may be overridden.
- `_node(doc_id, node_id, parent_node_id, qname, ord, ord_path, state, val,
  val_tag, branch)` gives every element occurrence a stable internal id
  (`node_id` is the document-relative ordinal path, e.g. `1/8/2`), its
  position among siblings (`ord`), and — for choice branches — the local name
  of the branch actually taken (`branch`).
- Each complex element additionally gets a companion table (`order`, `item`,
  …) with typed value columns. Every value column has a `*_state` column, so
  the following cases never collapse into one SQL `NULL`:

| state     | meaning                                                       |
|-----------|---------------------------------------------------------------|
| `missing` | particle/attribute absent (no node row; state column default) |
| `value`   | explicit value present (empty string for empty string types)  |
| `empty`   | empty element of a non-string-like type, no default           |
| `nil`     | `xsi:nil="true"`                                              |
| `default` | element/attribute present empty, value supplied by XSD default |
| `fixed`   | value supplied by an XSD `fixed` constraint                   |

Foreign keys (`_node` self-reference and companion table → `_node`) are
enforced with `ON DELETE CASCADE`; the importer opens connections with
`PRAGMA foreign_keys = ON`.

Rebuild only serializes explicit content (`value`, empty tags, `nil`), so
`default`/`fixed` values remain available for analysis without being injected
into the reference XML. Indentation whitespace in element-only content is
insignificant and is not stored.

## Usage

```python
from xsd2sqlite import (
    create_plan, connect, create_schema, import_document, rebuild_document,
)

plan = create_plan("schema.xsd")
if not plan.importable:
    for issue in plan.errors:
        print(issue.severity, issue.path, issue.message)
    raise SystemExit(1)

conn = connect("analysis.db")
create_schema(conn, plan)
doc_id = import_document(conn, plan, "doc.xml")
xml_bytes = rebuild_document(conn, plan, doc_id)
```

CLI reference:

```sh
python -m xsd2sqlite plan    schema.xsd --sql schema.sql
python -m xsd2sqlite init    schema.xsd analysis.db
python -m xsd2sqlite import  schema.xsd analysis.db doc.xml --id doc-1
python -m xsd2sqlite rebuild schema.xsd analysis.db doc-1
```

See `examples/orders.xsd` and the two sample documents for repeated items, a
choice, simple content, empty/nil/default distinctions and attributes.
