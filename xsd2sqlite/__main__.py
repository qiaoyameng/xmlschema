"""Command line reference entry point.

Usage:
    python -m xsd2sqlite plan    schema.xsd [--root qname] [--sql out.sql]
    python -m xsd2sqlite init    schema.xsd database.db [--root qname]
    python -m xsd2sqlite import  schema.xsd database.db document.xml [--id X]
    python -m xsd2sqlite rebuild schema.xsd database.db DOC_ID
"""
from __future__ import annotations

import argparse
import json
import sys

from .importer import connect, create_schema, import_document
from .planner import build_plan
from .parser import parse_schema
from .rebuilder import rebuild_document


def _plan(args):
    parsed = parse_schema(args.xsd, root_element=args.root)
    plan = build_plan(parsed)
    if args.sql:
        if not plan.importable:
            print("plan has errors; refusing to write SQL", file=sys.stderr)
            return 2
        with open(args.sql, "w", encoding="utf-8") as fh:
            for statement in plan.create_statements():
                fh.write(statement + ";\n\n")
    print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
    return 0 if plan.importable else 1


def _init(args):
    plan = build_plan(parse_schema(args.xsd, root_element=args.root))
    if not plan.importable:
        for issue in plan.errors:
            print(f"ERROR {issue.path}: {issue.message}", file=sys.stderr)
        return 1
    conn = connect(args.database)
    create_schema(conn, plan)
    conn.close()
    print(f"created {len(plan.tables)} element tables in {args.database}")
    return 0


def _import(args):
    plan = build_plan(parse_schema(args.xsd, root_element=args.root))
    if not plan.importable:
        print("plan has errors", file=sys.stderr)
        return 2
    conn = connect(args.database)
    doc_id = import_document(
        conn, plan, args.document, doc_id=args.id,
        source_uri=str(args.document))
    conn.close()
    print(doc_id)
    return 0


def _rebuild(args):
    plan = build_plan(parse_schema(args.xsd, root_element=args.root))
    conn = connect(args.database)
    sys.stdout.buffer.write(rebuild_document(conn, plan, args.doc_id))
    conn.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="xsd2sqlite")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_xsd(p):
        p.add_argument("xsd")
        p.add_argument("--root")

    p_plan = sub.add_parser("plan")
    add_xsd(p_plan)
    p_plan.add_argument("--sql")
    p_plan.set_defaults(func=_plan)

    p_init = sub.add_parser("init")
    add_xsd(p_init)
    p_init.add_argument("database")
    p_init.set_defaults(func=_init)

    p_import = sub.add_parser("import")
    add_xsd(p_import)
    p_import.add_argument("database")
    p_import.add_argument("document")
    p_import.add_argument("--id")
    p_import.set_defaults(func=_import)

    p_rebuild = sub.add_parser("rebuild")
    add_xsd(p_rebuild)
    p_rebuild.add_argument("database")
    p_rebuild.add_argument("doc_id")
    p_rebuild.set_defaults(func=_rebuild)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
