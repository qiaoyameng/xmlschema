"""Parse a small, well-defined subset of XSD 1.0 into :mod:`xsd2sqlite.model`.

Global declarations are indexed first and resolved lazily, so a schema is not
rejected just because it also contains unreferenced exotic definitions: only
constructs reached from the chosen root element have to be supported.  When an
unsupported construct is reached, :class:`~xsd2sqlite.model.SchemaFeatureError`
is raised carrying the XSD component path, and the planner records it.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from xml.etree import ElementTree as ET

from .model import (
    ANY_TYPE,
    XSD_NS,
    AttributeDecl,
    Choice,
    ComplexType,
    ElementDecl,
    ElementUse,
    ParsedSchema,
    Schema,
    SchemaFeatureError,
    Sequence,
    SimpleType,
    builtin_storage_tag,
    split_tag,
)

_XMLNS_RE = re.compile(
    r"""xmlns:(?P<prefix>[A-Za-z_][\w.-]*)\s*=\s*
        (?P<q>['"])(?P<uri>[^'"]*)(?P=q)""",
    re.VERBOSE,
)
_XMLNS_DEFAULT_RE = re.compile(
    r"""xmlns\s*=\s*(?P<q>['"])(?P<uri>[^'"]*)(?P=q)"""
)


def scan_xml_prefixes(source: str) -> dict[str, str]:
    """Collect explicit ``xmlns:prefix`` bindings of an XML instance."""
    return {m.group("prefix"): m.group("uri")
            for m in _XMLNS_RE.finditer(source)}


def scan_namespace_prefixes(source: str) -> dict[str, str]:
    """Collect ``xmlns:prefix="uri"`` declarations from the schema text.

    ElementTree does not retain prefixes, but the reference serializer wants to
    reuse the ones the schema author chose.
    """
    prefixes = {m.group("prefix"): m.group("uri")
                for m in _XMLNS_RE.finditer(source)}
    for match in _XMLNS_DEFAULT_RE.finditer(source):
        prefixes.setdefault("", match.group("uri"))
    return prefixes


def _occurs(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    return -1 if value == "unbounded" else int(value)


def _xs(local: str) -> str:
    return f"{{{XSD_NS}}}{local}"


def _children(node: ET.Element, local: Optional[str] = None) -> list[ET.Element]:
    if local is None:
        return [c for c in list(node) if split_tag(c.tag)[1] not in
                ("annotation",)]
    return [c for c in list(node)
            if c.tag == _xs(local)]


class _Builder:
    """Lazy resolver over the indexed global declarations of a schema."""

    def __init__(self, root: ET.Element, schema_info: Schema) -> None:
        self.tns = schema_info.target_namespace
        self.element_form_default = schema_info.element_form_default
        self.attribute_form_default = schema_info.attribute_form_default
        self.globals: dict[str, dict[tuple, ET.Element]] = {
            kind: {} for kind in
            ("element", "complexType", "simpleType", "group", "attributeGroup",
             "attribute")
        }
        for child in list(root):
            ns, local = split_tag(child.tag)
            if ns != XSD_NS or local in ("annotation", "import", "include"):
                continue
            if local not in self.globals:
                continue
            name = child.get("name")
            if name is not None:
                self.globals[local][self._resolve_qname(name)] = child
        self._building: set[tuple[str, tuple]] = set()

    # -- qname helpers ----------------------------------------------------

    def _resolve_qname(self, raw: str) -> tuple[Optional[str], str]:
        if ":" in raw:
            prefix, local = raw.split(":", 1)
            ns = getattr(self, "_prefix_ns", {}).get(prefix)
            if ns is None:
                raise SchemaFeatureError(
                    f"QName {raw!r} uses an undeclared namespace prefix")
            return ns, local
        return self.tns, raw

    def set_prefix_map(self, prefix_ns: dict[str, str]) -> None:
        self._prefix_ns = prefix_ns

    def _effective_element(self, decl: ElementDecl) -> tuple[Optional[str], str]:
        ns, local = decl.qname
        qualified = decl.form if decl.form is not None else (
            self.element_form_default == "qualified")
        return (ns if qualified else None), local

    def _effective_attribute(
            self, decl: AttributeDecl) -> tuple[Optional[str], str]:
        ns, local = decl.qname
        qualified = decl.form if decl.form is not None else (
            self.attribute_form_default == "qualified")
        return (ns, local) if (ns is not None and qualified) else (None, local)

    @staticmethod
    def _feature(node: ET.Element, detail: str) -> SchemaFeatureError:
        ns, local = split_tag(node.tag)
        name = node.get("name") or node.get("ref") or ""
        return SchemaFeatureError(f"xs:{local} {name}: {detail}".strip())

    # -- built-in / simple types -----------------------------------------

    def builtin_simple_type(self, qname: tuple) -> SimpleType:
        return SimpleType(qname=qname, base_qname=qname)

    def _builtin_from_ref(self, raw: str) -> SimpleType:
        if ":" in raw:
            prefix, local = raw.split(":", 1)
            ns = self._prefix_ns.get(prefix, "")
            if ns != XSD_NS:
                raise SchemaFeatureError(
                    f"unknown simple type namespace prefix {prefix!r}")
            return self.builtin_simple_type((XSD_NS, local))
        if raw in ("string", "boolean", "int", "integer", "decimal",
                    "date", "dateTime", "anySimpleType"):
            return self.builtin_simple_type((XSD_NS, raw))
        raise SchemaFeatureError(
            f"unprefixed type {raw!r} is not a recognized built-in type")

    def build_simple_type_node(self, node: ET.Element) -> SimpleType:
        children = _children(node, "restriction")
        if not children:
            raise self._feature(node, "only xs:restriction simple types are "
                                "supported (xs:list/xs:union are not)")
        restriction = children[0]
        base_raw = restriction.get("base")
        if base_raw is None:
            raise self._feature(restriction, "restriction needs a 'base'")
        base_qname = self._resolve_builtin_or_named_qname(base_raw)
        facets = tuple(sorted({
            split_tag(f.tag)[1]
            for f in list(restriction)
            if split_tag(f.tag)[0] == XSD_NS
            and split_tag(f.tag)[1] != "annotation"
        }))
        name_raw = node.get("name")
        qname = self._resolve_qname(name_raw) if name_raw else None
        return SimpleType(qname=qname, base_qname=base_qname, facets=facets)

    def _resolve_builtin_or_named_qname(self, raw: str) -> tuple:
        if ":" in raw:
            prefix, local = raw.split(":", 1)
            ns = self._prefix_ns.get(prefix)
            if ns == XSD_NS:
                return XSD_NS, local
            if ns is None:
                raise SchemaFeatureError(
                    f"undeclared namespace prefix {prefix!r}")
            qname = ns, local
            if qname not in self.globals["simpleType"]:
                raise SchemaFeatureError(f"unknown simple type {raw!r}")
            return qname
        # unprefixed: XSD built-in or a chameleon/namespace-less named type
        builtin = (XSD_NS, raw)
        if builtin_storage_tag(builtin) is not None or raw == "anySimpleType":
            return builtin
        qname = self.tns, raw
        if qname in self.globals["simpleType"]:
            return qname
        no_ns = (None, raw)
        if no_ns in self.globals["simpleType"]:
            return no_ns
        raise SchemaFeatureError(f"unknown simple type {raw!r}")

    def resolve_simple_type(self, qname: tuple) -> SimpleType:
        if builtin_storage_tag(qname) is not None or qname == (
                XSD_NS, "anySimpleType"):
            return self.builtin_simple_type(qname)
        node = self.globals["simpleType"].get(qname)
        if node is None:
            raise SchemaFeatureError(f"unknown simpleType {qname!r}")
        key = ("simpleType", qname)
        if key in self._building:
            raise SchemaFeatureError("recursive named simpleType")
        self._building.add(key)
        try:
            st = self.build_simple_type_node(node)
        finally:
            self._building.discard(key)
        return st

    def resolve_type_of_element_node(
            self, node: ET.Element):
        type_raw = node.get("type")
        if type_raw is not None:
            qname = self._resolve_builtin_or_named_qname(type_raw)
            if builtin_storage_tag(qname) is not None or qname == (
                    XSD_NS, "anySimpleType"):
                return self.builtin_simple_type(qname)
            if qname in self.globals["simpleType"]:
                return self.resolve_simple_type(qname)
            if qname in self.globals["complexType"]:
                return self.resolve_complex_type(qname)
            raise SchemaFeatureError(f"unknown type {type_raw!r}")
        local_defs = [c for c in list(node) if split_tag(c.tag)[0] == XSD_NS]
        for c in local_defs:
            local = split_tag(c.tag)[1]
            if local == "simpleType":
                return self.build_simple_type_node(c)
            if local == "complexType":
                return self.build_complex_type_node(c, qname=None)
        return ANY_TYPE

    # -- elements ---------------------------------------------------------

    def build_element_node(self, node: ET.Element, global_: bool) -> ElementDecl:
        ref = node.get("ref")
        if ref is not None:
            qname = self._resolve_qname(ref)
            target = self.globals["element"].get(qname)
            if target is None:
                raise SchemaFeatureError(f"unknown element reference {ref!r}")
            decl = self.resolve_element(qname)
            decl = ElementDecl(
                qname=decl.qname,
                type_def=decl.type_def,
                nillable=decl.nillable,
                default=decl.default,
                fixed=decl.fixed,
                abstract=decl.abstract,
                substitution_group=decl.substitution_group,
                global_=False,
            )
        else:
            name = node.get("name")
            if name is None:
                raise self._feature(node, "element needs 'name' or 'ref'")
            if ":" in name:
                raise self._feature(node, "element name must not be prefixed")
            qname = (self.tns, name) if self.tns else (None, name)
            type_def = self.resolve_type_of_element_node(node)
            form_raw = node.get("form")
            form = None if form_raw is None else form_raw == "qualified"
            substitution = node.get("substitutionGroup")
            decl = ElementDecl(
                qname=qname,
                type_def=type_def,
                nillable=node.get("nillable") == "true",
                default=node.get("default"),
                fixed=node.get("fixed"),
                abstract=node.get("abstract") == "true",
                substitution_group=(self._resolve_qname(substitution)
                                    if substitution else None),
                form=form,
                global_=global_,
            )
        decl.qname = self._effective_element(decl)
        if decl.abstract:
            raise SchemaFeatureError(
                f"abstract elements are not supported ({decl.qname[1]})")
        if decl.substitution_group is not None:
            raise SchemaFeatureError(
                f"substitutionGroup is not supported ({decl.qname[1]})")
        return decl

    def resolve_element(self, qname: tuple) -> ElementDecl:
        node = self.globals["element"].get(qname)
        if node is None:
            raise SchemaFeatureError(f"unknown element {qname!r}")
        key = ("element", qname)
        if key in self._building:
            raise SchemaFeatureError(
                f"recursive element reference not supported ({qname[1]!r})")
        self._building.add(key)
        try:
            decl = self.build_element_node(node, global_=True)
        finally:
            self._building.discard(key)
        return decl

    # -- attributes -------------------------------------------------------

    def build_attribute_node(self, node: ET.Element) -> AttributeDecl:
        if node.get("ref"):
            qname = self._resolve_qname(node.get("ref"))
            target = self.globals["attribute"].get(qname)
            if target is None:
                raise SchemaFeatureError(
                    f"unknown attribute reference {node.get('ref')!r}")
            attr = self.resolve_attribute(qname)
        else:
            name = node.get("name")
            if name is None:
                raise self._feature(node, "attribute needs 'name' or 'ref'")
            qname = (self.tns, name) if self.tns else (None, name)
            type_raw = node.get("type")
            if type_raw is not None:
                type_qname = self._resolve_builtin_or_named_qname(type_raw)
                if builtin_storage_tag(type_qname) is not None or (
                        type_qname == (XSD_NS, "anySimpleType")):
                    type_def = self.builtin_simple_type(type_qname)
                else:
                    type_def = self.resolve_simple_type(type_qname)
            else:
                st_nodes = _children(node, "simpleType")
                if st_nodes:
                    type_def = self.build_simple_type_node(st_nodes[0])
                else:
                    type_def = self.builtin_simple_type((XSD_NS, "anySimpleType"))
            form_raw = node.get("form")
            attr = AttributeDecl(
                qname=qname,
                type_def=type_def,
                form=(None if form_raw is None else form_raw == "qualified"),
                use=node.get("use", "optional"),
                default=node.get("default"),
                fixed=node.get("fixed"),
                global_=False,
            )
        attr.qname = self._effective_attribute(attr)
        if attr.use == "prohibited":
            raise self._feature(node, "prohibited attributes are not needed "
                                "for mapped documents")
        return attr

    def resolve_attribute(self, qname: tuple) -> AttributeDecl:
        node = self.globals["attribute"].get(qname)
        if node is None:
            raise SchemaFeatureError(f"unknown attribute {qname!r}")
        return self.build_attribute_node(node)

    def collect_attributes(
            self, owner: ET.Element) -> list[AttributeDecl]:
        attrs: list[AttributeDecl] = []
        for node in list(owner):
            ns, local = split_tag(node.tag)
            if ns != XSD_NS:
                continue
            if local == "attribute":
                attrs.append(self.build_attribute_node(node))
            elif local == "attributeGroup":
                ref = node.get("ref")
                if ref is None:
                    raise self._feature(node, "nested attributeGroup needs ref")
                qname = self._resolve_qname(ref)
                group_node = self.globals["attributeGroup"].get(qname)
                if group_node is None:
                    raise SchemaFeatureError(
                        f"unknown attributeGroup {ref!r}")
                cycle_key = ("attributeGroup", qname)
                if cycle_key in self._building:
                    raise SchemaFeatureError(
                        f"cyclic attributeGroup {ref!r}")
                self._building.add(cycle_key)
                try:
                    attrs.extend(self.collect_attributes(group_node))
                finally:
                    self._building.discard(cycle_key)
            elif local == "anyAttribute":
                raise self._feature(
                    node, "wildcard attributes (xs:anyAttribute) are not "
                    "supported; fields must be explicit")
        return attrs

    # -- content particles ------------------------------------------------

    def build_particle(self, node: ET.Element):
        ns, local = split_tag(node.tag)
        if ns != XSD_NS:
            raise self._feature(node, "unexpected non-XSD content node")
        if local == "element":
            min_occurs = _occurs(node.get("minOccurs"), 1)
            max_occurs = _occurs(node.get("maxOccurs"), 1)
            return ElementUse(self.build_element_node(node, global_=False),
                              min_occurs, max_occurs)
        if local in ("sequence", "choice"):
            cls = Sequence if local == "sequence" else Choice
            kids: list = []
            for child in list(node):
                cns, clocal = split_tag(child.tag)
                if cns != XSD_NS or clocal == "annotation":
                    continue
                if clocal == "any":
                    raise self._feature(
                        child, "wildcard elements (xs:any) are not supported; "
                        "fields must be explicit")
                if clocal not in ("element", "sequence", "choice", "group"):
                    raise self._feature(
                        child, f"xs:{clocal} inside xs:{local} is not "
                        "supported in v1")
                if clocal == "group":
                    particle = self._expand_group(child)
                    if isinstance(particle, Sequence):
                        kids.extend(particle.children)
                    else:
                        kids.append(particle)
                else:
                    kids.append(self.build_particle(child))
            return cls(
                children=kids,
                min_occurs=_occurs(node.get("minOccurs"), 1),
                max_occurs=_occurs(node.get("maxOccurs"), 1),
            )
        if local == "all":
            raise self._feature(node, "xs:all is not supported in v1; use "
                                "xs:sequence")
        raise self._feature(node, f"xs:{local} is not a supported particle")

    def _expand_group(self, node: ET.Element):
        ref = node.get("ref")
        if ref is None:
            raise self._feature(node, "xs:group needs a ref")
        if (node.get("minOccurs") is not None
                or node.get("maxOccurs") is not None):
            raise self._feature(node, "repeating xs:group references are not "
                                "supported; repeat the inner particle instead")
        qname = self._resolve_qname(ref)
        group_node = self.globals["group"].get(qname)
        if group_node is None:
            raise SchemaFeatureError(f"unknown group {ref!r}")
        key = ("group", qname)
        if key in self._building:
            raise SchemaFeatureError(f"cyclic group reference {ref!r}")
        self._building.add(key)
        try:
            content = [c for c in list(group_node)
                       if split_tag(c.tag)[0] == XSD_NS
                       and split_tag(c.tag)[1] != "annotation"]
            if len(content) != 1:
                raise self._feature(
                    group_node, "xs:group must contain one sequence/choice")
            particle = self.build_particle(content[0])
        finally:
            self._building.discard(key)
        return particle

    # -- complex types ----------------------------------------------------

    def build_complex_type_node(
            self, node: ET.Element,
            qname: Optional[tuple]) -> ComplexType:
        mixed = node.get("mixed") == "true"
        attributes = self.collect_attributes(node)
        content_nodes = [c for c in list(node)
                         if split_tag(c.tag)[0] == XSD_NS
                         and split_tag(c.tag)[1] not in (
                             "annotation", "attribute", "attributeGroup",
                             "anyAttribute")]
        if not content_nodes:
            if mixed:
                raise self._feature(node, "mixed content is not supported")
            return ComplexType(qname=qname, kind="empty",
                               attributes=attributes)
        first = content_nodes[0]
        local = split_tag(first.tag)[1]
        if local == "simpleContent":
            return self._build_simple_content(first, qname, attributes)
        if local == "complexContent":
            return self._build_complex_content(first, qname, attributes, mixed)
        if local in ("sequence", "choice"):
            if mixed:
                raise self._feature(node, "mixed content is not supported")
            return ComplexType(qname=qname, kind="element-only",
                               attributes=attributes,
                               content=self.build_particle(first))
        raise self._feature(node, f"xs:{local} is not supported on complex "
                            "type content")

    def _build_simple_content(
            self, node: ET.Element, qname,
            attributes: list[AttributeDecl]) -> ComplexType:
        kids = [c for c in list(node)
                if split_tag(c.tag)[0] == XSD_NS
                and split_tag(c.tag)[1] != "annotation"]
        if len(kids) != 1 or split_tag(kids[0].tag)[1] != "extension":
            raise self._feature(
                node, "only simpleContent/xs:extension is supported")
        extension = kids[0]
        base_raw = extension.get("base")
        if base_raw is None:
            raise self._feature(extension, "simpleContent extension needs base")
        base_qname = self._resolve_builtin_or_named_qname(base_raw)
        if builtin_storage_tag(base_qname) is not None or base_qname == (
                XSD_NS, "anySimpleType"):
            simple_type = self.builtin_simple_type(base_qname)
        elif base_qname in self.globals["simpleType"]:
            simple_type = self.resolve_simple_type(base_qname)
        else:
            raise self._feature(
                extension, f"simpleContent base {base_raw!r} must be a "
                "simple type")
        attributes.extend(self.collect_attributes(extension))
        return ComplexType(qname=qname, kind="simple",
                           attributes=attributes,
                           simple_type=simple_type)

    def _build_complex_content(
            self, node: ET.Element, qname,
            attributes: list[AttributeDecl], mixed: bool) -> ComplexType:
        kids = [c for c in list(node)
                if split_tag(c.tag)[0] == XSD_NS
                and split_tag(c.tag)[1] != "annotation"]
        if len(kids) != 1:
            raise self._feature(node, "complexContent needs one derivation")
        derivation = kids[0]
        dlocal = split_tag(derivation.tag)[1]
        if dlocal != "extension":
            raise self._feature(
                derivation, f"complexContent/xs:{dlocal} is not supported; "
                "only extension of a named complex type")
        base_raw = derivation.get("base")
        if base_raw is None:
            raise self._feature(derivation, "complexContent extension "
                                "needs base")
        base_qname = self._resolve_qname(base_raw)
        base_node = self.globals["complexType"].get(base_qname)
        if base_node is None:
            raise self._feature(
                derivation, f"unknown complex type base {base_raw!r}")
        key = ("complexType", base_qname)
        if key in self._building:
            raise SchemaFeatureError(
                f"recursive complex type {base_raw!r}")
        self._building.add(key)
        try:
            base_ct = self.build_complex_type_node(base_node, base_qname)
        finally:
            self._building.discard(key)
        if base_ct.mixed or mixed:
            raise self._feature(node, "mixed content is not supported")
        attributes = list(base_ct.attributes) + attributes
        attributes.extend(self.collect_attributes(derivation))
        particles = [c for c in list(derivation)
                     if split_tag(c.tag)[0] == XSD_NS
                     and split_tag(c.tag)[1] not in (
                         "annotation", "attribute", "attributeGroup",
                         "anyAttribute")]
        if base_ct.kind == "empty" and not particles:
            return ComplexType(qname=qname, kind="empty",
                               attributes=attributes)
        if base_ct.kind == "simple":
            raise self._feature(
                derivation, "extending simpleContent with elements is not "
                "supported")
        seq_children = list(base_ct.content.children) if base_ct.content else []
        new_particle = None
        if particles:
            if len(particles) != 1 or split_tag(particles[0].tag)[1] not in (
                    "sequence", "choice"):
                raise self._feature(
                    derivation, "complexContent extension may add one "
                    "sequence/choice")
            new_particle = self.build_particle(particles[0])
            if not isinstance(base_ct.content, (Sequence, type(None))):
                raise self._feature(
                    derivation, "cannot append to a choice base type")
            seq_children.extend(new_particle.children)
        return ComplexType(qname=qname, kind="element-only",
                           attributes=attributes,
                           content=Sequence(children=seq_children))

    def resolve_complex_type(self, qname: tuple) -> ComplexType:
        node = self.globals["complexType"].get(qname)
        if node is None:
            raise SchemaFeatureError(f"unknown complexType {qname!r}")
        return self.build_complex_type_node(node, qname)


def parse_schema(source, root_element: Optional[str] = None) -> ParsedSchema:
    """Parse an XSD document and resolve the requested root element.

    ``source`` is a path or readable text.  ``root_element`` is a prefixed or
    unprefixed QName; when omitted the only global element is used.
    """
    text, source_name = _read_text(source)
    tree_root = ET.fromstring(text)
    ns, local = split_tag(tree_root.tag)
    if ns != XSD_NS or local != "schema":
        raise SchemaFeatureError(
            f"{source_name}: root is not an xs:schema element")

    prefixes = scan_namespace_prefixes(text)
    target_namespace = tree_root.get("targetNamespace")
    schema_info = Schema(
        target_namespace=target_namespace or None,
        element_form_default=tree_root.get("elementFormDefault",
                                           "unqualified"),
        attribute_form_default=tree_root.get("attributeFormDefault",
                                             "unqualified"),
        prefixes=prefixes,
    )
    builder = _Builder(tree_root, schema_info)
    # Prefix mapping also needs the standard xs prefix aliases (xsd, XSD...).
    prefix_ns = dict(prefixes)
    prefix_ns.setdefault("xs", XSD_NS)
    prefix_ns.setdefault("xsd", XSD_NS)
    builder.set_prefix_map(prefix_ns)
    for prefix, uri in prefix_ns.items():
        if prefix:
            ET.register_namespace(prefix, uri)

    global_elements = list(builder.globals["element"])
    if not global_elements:
        raise SchemaFeatureError("schema declares no global elements")
    if root_element is None:
        if len(global_elements) > 1:
            names = ", ".join(q[1] for q in global_elements)
            raise SchemaFeatureError(
                f"schema has multiple global elements ({names}); pass "
                "root_element explicitly")
        root_qname = global_elements[0]
    else:
        root_qname = builder._resolve_qname(root_element)
        if root_qname not in builder.globals["element"]:
            raise SchemaFeatureError(
                f"root element {root_element!r} not found in schema")
    return ParsedSchema(
        schema_info=schema_info,
        root_element=lambda: builder.resolve_element(root_qname),
    )


def _read_text(source) -> tuple[str, str]:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source).decode("utf-8"), "<bytes>"
    if isinstance(source, str) and source.lstrip()[:1] in ("<",):
        return source, "<string>"
    if hasattr(source, "read"):
        data = source.read()
        return (data.decode("utf-8") if isinstance(data, bytes) else data), \
            "<stream>"
    path = os.fspath(source)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read(), path
