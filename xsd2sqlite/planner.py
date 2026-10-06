"""Walk a parsed XSD and produce a :class:`~xsd2sqlite.plan.MappingPlan`.

Scope (v1):
  * xs:sequence / xs:choice (choice occurrences exactly once; its element
    branches may repeat according to their own maxOccurs);
  * attributes, element-only/empty/simple-content complex types;
  * repeated elements to any finite nesting depth (no recursion).

Out of scope and reported as plan errors (never silently dropped):
  mixed content, wildcards (xs:any / xs:anyAttribute), xs:all,
  substitution groups, abstract/recursive types, list/union simple types,
  xs:anyType document nodes, repeated choice/sequence particles.
"""
from __future__ import annotations

from typing import Optional, Union

from .model import (
    AnyType,
    AttributeDecl,
    Choice,
    ComplexType,
    ElementUse,
    ParsedSchema,
    SchemaFeatureError,
    Sequence,
    SimpleType,
    builtin_storage_tag,
)
from .plan import (
    AttrBinding,
    ElementNode,
    MappingPlan,
    ScalarBinding,
    TableSpec,
    _identifier,
)


def build_plan(parsed: ParsedSchema) -> MappingPlan:
    """Create the mapping plan, collecting every blocker as a plan issue."""
    plan = MappingPlan(parsed)
    try:
        root_decl = (
            parsed.root_element()
            if callable(parsed.root_element)
            else parsed.root_element
        )
        root_node = _register_element(
            plan=plan,
            use=ElementUse(root_decl, 1, 1),
            path=f"/{root_decl.qname[1]}",
            active_types=set(),
            root=True,
        )
    except SchemaFeatureError as exc:
        plan.error("/", str(exc))
        return plan
    plan.root_node = root_node
    return plan


def _register_element(
    plan: MappingPlan,
    use: ElementUse,
    path: str,
    active_types: set,
    root: bool = False,
) -> ElementNode:
    decl = use.decl
    node = ElementNode(
        element_qname=decl.qname,
        path=path,
        min_occurs=use.min_occurs,
        max_occurs=use.max_occurs,
        nillable=decl.nillable,
        default=decl.default,
        fixed=decl.fixed,
    )
    type_def = decl.type_def
    if isinstance(type_def, AnyType):
        raise SchemaFeatureError(
            f"element {decl.qname[1]!r} has type xs:anyType / no type; "
            "untyped elements are not supported")
    if isinstance(type_def, SimpleType):
        storage = builtin_storage_tag(type_def.base_qname)
        if storage is None:
            raise SchemaFeatureError(
                f"simple type {type_def.base_qname[1]!r} has no built-in "
                "storage mapping")
        if use.max_occurs != 1:
            node.is_repeated_simple = True
            node.simple_storage_tag = storage
            node.simple_base_qname = type_def.base_qname
        else:
            # A single simple child is added as a scalar column by the
            # owning table; still represented as a node binding so ordinal
            # metadata remains available in the plan.
            node.simple_storage_tag = storage
            node.simple_base_qname = type_def.base_qname
        if type_def.facets:
            plan.warning(path, "simple type facets are not re-validated "
                         "(documents are assumed XSD-validated): "
                         + ", ".join(type_def.facets))
        return node
    if isinstance(type_def, ComplexType):
        _register_complex_table(plan, node, type_def, path, active_types, root)
        return node
    raise SchemaFeatureError(f"unsupported type definition for {path}")


def _register_complex_table(
    plan: MappingPlan,
    node: ElementNode,
    ct: ComplexType,
    path: str,
    active_types: set,
    root: bool,
) -> None:
    type_key = ct.qname
    if type_key is not None:
        if type_key in active_types:
            raise SchemaFeatureError(
                f"recursive use of named type {type_key[1]!r} is not "
                "supported in v1")
        active_types = active_types | {type_key}

    base_name = node.element_qname[1]
    table = TableSpec(
        name=plan.unique_table_name(base_name),
        xpath=path,
        complex_type=ct,
        element_qname=node.element_qname,
        root=root,
    )
    node.table = table.name

    existing_columns: set[str] = {"doc_id", "node_id", "state"}
    for attr in ct.attributes:
        binding = _make_attr_binding(attr, existing_columns)
        table.attributes.append(binding)

    if ct.kind == "simple":
        storage = builtin_storage_tag(ct.simple_type.base_qname)
        if storage is None:
            raise SchemaFeatureError(
                f"simpleContent base {ct.simple_type.base_qname[1]!r} has no "
                "storage mapping")
        binding = ScalarBinding(
            element_qname=node.element_qname,
            nillable=node.nillable,
            default=node.default,
            fixed=node.fixed,
            storage_tag=storage,
            column=_free_column("content", existing_columns),
            state_column=_free_column("content_state", existing_columns),
            is_content=True,
            base_qname=ct.simple_type.base_qname,
        )
        table.scalars.append(binding)
        if ct.simple_type.facets:
            plan.warning(path, "simple type facets are not re-validated "
                         "(documents are assumed XSD-validated): "
                         + ", ".join(ct.simple_type.facets))
    elif ct.kind == "element-only" and ct.content is not None:
        _register_particle(plan, table, ct.content, path, active_types,
                           existing_columns, choice_group=None)
    # 'empty' contributes no content columns.
    plan.tables.append(table)


def _register_particle(
    plan: MappingPlan,
    table: TableSpec,
    particle: Union[Sequence, Choice],
    path: str,
    active_types: set,
    existing_columns: set,
    choice_group: Optional[str],
) -> None:
    if particle.max_occurs != 1 or particle.min_occurs not in (0, 1):
        kind = "choice" if isinstance(particle, Choice) else "sequence"
        raise SchemaFeatureError(
            f"repeating xs:{kind} groups are not supported in v1 ({path}); "
            "repeat individual element branches instead")
    if isinstance(particle, Choice):
        group = plan.next_choice_group()
        table.choice_groups.append(group)
        for index, child in enumerate(particle.children):
            if isinstance(child, ElementUse):
                _register_element_use(
                    plan, table, child,
                    f"{path}/{child.decl.qname[1]}",
                    active_types, existing_columns, group)
            else:
                _register_particle(
                    plan, table, child,
                    f"{path}/choice[{index + 1}]",
                    active_types, existing_columns, group)
        return
    for index, child in enumerate(particle.children):
        if isinstance(child, ElementUse):
            _register_element_use(
                plan, table, child,
                f"{path}/{child.decl.qname[1]}",
                active_types, existing_columns, choice_group)
        else:
            # Only one level of direct nesting is emitted by the parser for
            # sequences; choices are handled above.
            _register_particle(
                plan, table, child,
                f"{path}/group[{index + 1}]",
                active_types, existing_columns, choice_group)


def _register_element_use(
    plan: MappingPlan,
    table: TableSpec,
    use: ElementUse,
    path: str,
    active_types: set,
    existing_columns: set,
    choice_group: Optional[str],
) -> None:
    decl = use.decl
    if _duplicate_qname(table, decl.qname):
        raise SchemaFeatureError(
            f"element {decl.qname[1]!r} appears twice in the content of "
            f"{table.xpath}; ambiguous relational mapping")
    child_node = _register_element(plan, use, path, active_types)
    child_node.choice_group = choice_group
    table.children.append(child_node)

    type_def = decl.type_def
    if isinstance(type_def, SimpleType) and use.max_occurs == 1:
        if choice_group is not None:
            # Choice branches are represented by their _node rows and the
            # recorded branch; the branch taken must not collapse into a
            # nullable table column.
            return
        storage = builtin_storage_tag(type_def.base_qname)
        binding = ScalarBinding(
            element_qname=decl.qname,
            nillable=decl.nillable,
            default=decl.default,
            fixed=decl.fixed,
            storage_tag=storage,
            column=_free_column(decl.qname[1], existing_columns),
            state_column=_free_column(
                f"{decl.qname[1]}_state", existing_columns),
            min_occurs=use.min_occurs,
            choice_group=choice_group,
            base_qname=type_def.base_qname,
        )
        table.scalars.append(binding)


def _make_attr_binding(
        attr: AttributeDecl, existing_columns: set) -> AttrBinding:
    storage = builtin_storage_tag(attr.type_def.base_qname)
    if storage is None:
        raise SchemaFeatureError(
            f"attribute {attr.qname[1]!r} type "
            f"{attr.type_def.base_qname[1]!r} has no storage mapping")
    return AttrBinding(
        attribute_qname=attr.qname,
        storage_tag=storage,
        column=_free_column(f"attr_{attr.qname[1]}", existing_columns),
        state_column=_free_column(
            f"attr_{attr.qname[1]}_state", existing_columns),
        required=attr.use == "required",
        default=attr.default,
        fixed=attr.fixed,
        base_qname=attr.type_def.base_qname,
    )


def _duplicate_qname(table: TableSpec,
                     qname: tuple[Optional[str], str]) -> bool:
    return any(n.element_qname == qname for n in table.children)


def _free_column(base: str, used: set) -> str:
    candidate = _identifier(base)
    candidate = candidate[0].lower() + candidate[1:]
    if candidate not in used:
        used.add(candidate)
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in used:
        suffix += 1
    candidate = f"{candidate}_{suffix}"
    used.add(candidate)
    return candidate
