#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Tools for anonymizing XML documents at identity constraint level.

The main entry point is :func:`anonymize_document`, that replaces the values
selected by named *xs:key*/*xs:unique* identity constraints with anonymized
ones, keeping the referential integrity of the related *xs:keyref* constraints.

For every constraint a one-to-one mapping between original and replacement
keys is built within each scope (that is each instance of the element the
constraint is declared on), composite keys are handled as a whole and all the
references are updated accordingly. Replacement values can be provided by the
caller or produced by a restricted generator and must satisfy the XSD type
and the facets of the original fields.

The operation is atomic: on any failure (target value collision, no legal
value can be generated, an unresolvable reference) an exception is raised and
no partially anonymized document is produced. The original key values are
never logged and are reported in the mapping receipt only on explicit request.
"""
import argparse
import copy
import hashlib
import json
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
import xml.etree.ElementTree as ElementTree

from elementpath import XPathContext, build_node_tree
from elementpath.xpath_nodes import AttributeNode, ElementNode, EtreeElementNode

import xmlschema
from xmlschema.exceptions import XMLSchemaException
from xmlschema.validators.identities import XsdIdentity, XsdKey, XsdKeyref

logger = logging.getLogger(__name__)

__all__ = [
    'AnonymizationError', 'UnknownConstraintError', 'ReplacementCollisionError',
    'UnresolvableReferenceError', 'ValueGenerationError', 'MappingEntry',
    'MappingReceipt', 'AnonymizationResult', 'RestrictedValueGenerator',
    'anonymize_document', 'main',
]

ElementType = ElementTree.Element
GeneratorType = Callable[[tuple, tuple, str, set], tuple]


class AnonymizationError(Exception):
    """Base class for anonymization failures."""


class UnknownConstraintError(AnonymizationError):
    """A requested identity constraint does not exist in the schema."""


class ReplacementCollisionError(AnonymizationError):
    """Two distinct keys of a scope would be replaced with the same value."""


class UnresolvableReferenceError(AnonymizationError):
    """A keyref value cannot be located in the referred key scope."""


class ValueGenerationError(AnonymizationError):
    """A legal replacement value cannot be generated or is not legal."""


@dataclass
class MappingEntry:
    """
    A controlled receipt row for a single anonymized key. The original key
    is reported only when explicitly requested, otherwise only a fingerprint
    of it is provided.
    """
    constraint: str
    scope: str
    fingerprint: str
    replacement: tuple
    original: Optional[tuple] = None

    def to_dict(self) -> dict:
        obj = {
            'constraint': self.constraint,
            'scope': self.scope,
            'fingerprint': self.fingerprint,
            'replacement': list(self.replacement),
        }
        if self.original is not None:
            obj['original'] = list(self.original)
        return obj


@dataclass
class MappingReceipt:
    """The controlled mapping receipt of an anonymization run."""
    entries: list

    def to_dict(self) -> dict:
        return {'entries': [entry.to_dict() for entry in self.entries]}

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault('indent', 2)
        return json.dumps(self.to_dict(), **kwargs)

    def __iter__(self) -> Iterator[MappingEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class AnonymizationResult:
    """The outcome of a successful anonymization run."""
    document: str
    receipt: MappingReceipt


@dataclass
class _FieldSlot:
    """A writable reference to a field value in a document element."""
    element: ElementType
    attribute: Optional[str] = None

    def get(self) -> Optional[str]:
        if self.attribute is None:
            return self.element.text
        return self.element.get(self.attribute)

    def set(self, value: str) -> None:
        if self.attribute is None:
            self.element.text = value
        else:
            self.element.set(self.attribute, value)


@dataclass
class _KeyEntry:
    """The key selected by an identity constraint on a target element."""
    target: ElementType
    slots: list
    key: tuple
    lexical: tuple


def _fingerprint(lexical: tuple) -> str:
    digest = hashlib.sha256('\x1f'.join(lexical).encode('utf-8')).hexdigest()
    return digest[:16]


def _iter_identities(schema: xmlschema.XMLSchema) -> Iterator[XsdIdentity]:
    for component in schema.iter_components():
        if isinstance(component, XsdIdentity):
            yield component


def _iter_keyrefs(schema: xmlschema.XMLSchema) -> Iterator[XsdKeyref]:
    for component in schema.iter_components():
        if isinstance(component, XsdKeyref):
            yield component


def _find_constraints(schema: xmlschema.XMLSchema, names: list) -> list:
    available = {}
    for identity in _iter_identities(schema):
        if isinstance(identity, XsdKeyref):
            continue
        available[identity.name] = identity
        available.setdefault(identity.local_name, identity)

    identities = []
    for name in names:
        try:
            identity = available[name]
        except KeyError:
            msg = f'no xs:key/xs:unique identity constraint named {name!r}'
            raise UnknownConstraintError(msg) from None
        if identity not in identities:
            identities.append(identity)
    return identities


def _field_value_selectors(identity: XsdIdentity, tag: str) -> Optional[list]:
    for xsd_element, selectors in identity.elements.items():
        if xsd_element.name == tag:
            return selectors
    return None


def _field_types(identity: XsdIdentity, tag: str) -> tuple:
    selectors = _field_value_selectors(identity, tag)
    if selectors is None:
        return tuple(None for _ in identity.fields)

    types = []
    for selector in selectors:
        decoder = next((d for d in selector.decoders if hasattr(d, 'type')), None)
        types.append(decoder.type if decoder is not None else None)
    return tuple(types)


def _unwrap_element(node: Any) -> ElementType:
    if isinstance(node, ElementNode):
        return node.elem
    return node


def _field_slot(node: Any) -> _FieldSlot:
    if isinstance(node, AttributeNode):
        return _FieldSlot(_unwrap_element(node.parent), node.name)
    return _FieldSlot(_unwrap_element(node))


def _typed_key(identity: XsdIdentity, target: ElementType,
               lexical: tuple, namespaces: dict) -> tuple:
    selectors = _field_value_selectors(identity, target.tag)
    if selectors is None:
        return lexical

    node = EtreeElementNode(target)
    values = []
    try:
        for selector in selectors:
            values.append(selector.get_value(node, namespaces))
    except (XMLSchemaException, ValueError, TypeError):
        return lexical
    if any(value is None for value in values):
        return lexical
    return tuple(values)


def _collect_entries(identity: XsdIdentity, scope_elem: ElementType,
                     namespaces: dict) -> list:
    if identity.selector is None:
        msg = f'identity constraint {identity.name!r} has no selector'
        raise AnonymizationError(msg)

    entries = []
    context = XPathContext(build_node_tree(scope_elem))
    for target in identity.selector.token.select_results(context):
        target_elem = _unwrap_element(target)
        slots = []
        missing = False
        for field_selector in identity.fields:
            nodes = list(field_selector.token.select(XPathContext(target_elem)))
            if not nodes:
                missing = True
                break
            elif len(nodes) > 1:
                msg = (f'a field of {identity.name!r} selects multiple '
                       f'values in scope element {scope_elem.tag!r}')
                raise AnonymizationError(msg)
            slots.append(_field_slot(nodes[0]))

        if missing:
            if isinstance(identity, XsdKey):
                msg = f'missing field for xs:key {identity.name!r}'
                raise AnonymizationError(msg)
            continue  # xs:unique/xs:keyref entries with missing fields are not indexed

        lexical = tuple(slot.get() or '' for slot in slots)
        key = _typed_key(identity, target_elem, lexical, namespaces)
        entries.append(_KeyEntry(target_elem, slots, key, lexical))
    return entries


def _build_parent_map(root: ElementType) -> dict:
    return {child: elem for elem in root.iter() for child in elem}


def _element_path(elem: ElementType, parents: dict) -> str:
    parts = []
    node: Optional[ElementType] = elem
    while node is not None:
        parent = parents.get(node)
        if parent is None:
            parts.append(f'/{node.tag}')
        else:
            position = 1
            for sibling in parent:
                if sibling is node:
                    break
                elif sibling.tag == node.tag:
                    position += 1
            parts.append(f'/{node.tag}[{position}]')
        node = parent
    return ''.join(reversed(parts))


def _resolve_refer_path(refer_path: str, scope_elem: ElementType,
                        parents: dict) -> list:
    elements = [scope_elem]
    for step in refer_path.split('/'):
        if step in ('', '.'):
            continue
        elif step == '..':
            elements = [parents[e] for e in elements if e in parents]
        else:
            elements = [child for e in elements for child in e if child.tag == step]
        if not elements:
            break
    return elements


def _index_paths(root: ElementType) -> dict:
    paths = {}

    def walk(elem: ElementType, path: tuple) -> None:
        paths[id(elem)] = path
        for index, child in enumerate(elem):
            walk(child, path + (index,))

    walk(root, ())
    return paths


def _navigate(root: ElementType, path: tuple) -> ElementType:
    node = root
    for index in path:
        node = node[index]
    return node


def is_legal_value(xsd_type: Any, value: str) -> bool:
    """Checks a lexical replacement value against an XSD type and its facets."""
    try:
        decoded = xsd_type.text_decode(value)
        xsd_type.validate(decoded)
    except (XMLSchemaException, ValueError, TypeError):
        return False
    return True


class RestrictedValueGenerator:
    """
    Default restricted generator of replacement values.

    Generates deterministic pseudo-random candidates (when a *seed* is
    provided) and accepts only values that satisfy the XSD type and the
    facets of each key field and that do not collide with values already
    taken in the scope. Raises :class:`ValueGenerationError` when no legal
    value can be produced within *max_attempts* attempts.

    :param seed: optional seed for deterministic generation.
    :param max_attempts: maximum number of candidate values to try.
    :param prefix: prefix of the generated values.
    """
    def __init__(self, seed: Any = None, max_attempts: int = 1000,
                 prefix: str = 'anon') -> None:
        self.max_attempts = max_attempts
        self.prefix = prefix
        self._random = random.Random(seed)

    def __call__(self, field_types: tuple, key: tuple, scope: str,
                 taken: set) -> tuple:
        for _ in range(self.max_attempts):
            candidate = tuple(
                f'{self.prefix}-{self._random.getrandbits(64):016x}'
                for _ in field_types
            )
            if candidate in taken:
                continue
            if all(xsd_type is None or is_legal_value(xsd_type, value)
                   for xsd_type, value in zip(field_types, candidate)):
                return candidate

        msg = (f'cannot generate a legal replacement value in scope {scope!r} '
               f'after {self.max_attempts} attempts '
               f'(key fingerprint {_fingerprint(key)})')
        raise ValueGenerationError(msg)


def _normalize_provided(raw: Any) -> dict:
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = raw  # a list of [key, replacement] pairs

    provided = {}
    for key, value in items:
        if not isinstance(key, (list, tuple)):
            key = (key,)
        if not isinstance(value, (list, tuple)):
            value = (value,)
        provided[tuple(str(k) for k in key)] = tuple(str(v) for v in value)
    return provided


def _provided_replacements(replacements: dict, identity: XsdIdentity) -> dict:
    for name in (identity.name, identity.local_name):
        if name in replacements:
            return _normalize_provided(replacements[name])
    return {}


def _resolve_refer(keyref: XsdKeyref) -> Optional[XsdIdentity]:
    refer = keyref.refer
    if isinstance(refer, XsdIdentity) and refer.ref is not None:
        refer = refer.ref
    return refer if isinstance(refer, XsdIdentity) else None


def _reference_graph(schema: xmlschema.XMLSchema, root: ElementType,
                     parents: dict) -> list:
    """
    Builds the reference graph of a document as a list of edges. Each edge
    is a tuple (keyref name, keyref scope path, target path, referred key
    name, key scope path, lexical key).
    """
    edges = []
    for keyref in _iter_keyrefs(schema):
        refer = _resolve_refer(keyref)
        if refer is None:
            continue
        for scope_elem in root.iter(keyref.parent.name):
            scope_path = _element_path(scope_elem, parents)
            key_scopes = _resolve_refer_path(keyref.refer_path, scope_elem, parents)
            entries = _collect_entries(keyref, scope_elem, schema.namespaces)
            for entry in entries:
                target_path = _element_path(entry.target, parents)
                for key_scope in key_scopes:
                    edges.append((keyref.name, scope_path, target_path,
                                  refer.name, _element_path(key_scope, parents),
                                  entry.lexical))
    return edges


def _verify_connectivity(before: list, after: list, transforms: dict) -> None:
    """
    Verifies that the reference graph connectivity is unchanged, that is the
    *after* graph is the *before* graph with keys replaced by their mapping.
    """
    after_index: dict = {}
    for edge in after:
        after_index.setdefault(edge[:5], []).append(edge[5])

    for edge in before:
        prefix, lexical = edge[:5], edge[5]
        candidates = after_index.get(prefix)
        if not candidates:
            msg = f'reference graph edge lost after anonymization: {prefix!r}'
            raise AnonymizationError(msg)

        transform = transforms.get((edge[3], edge[4]))
        expected = transform.get(lexical, lexical) if transform else lexical
        if expected not in candidates:
            msg = f'reference graph edge changed after anonymization: {prefix!r}'
            raise AnonymizationError(msg)
        candidates.remove(expected)

    if any(after_index.values()):
        msg = 'reference graph has additional edges after anonymization'
        raise AnonymizationError(msg)


def anonymize_document(schema: Any, source: Any, constraints: Any, *,
                       replacements: Optional[dict] = None,
                       generator: Optional[GeneratorType] = None,
                       seed: Any = None,
                       max_attempts: int = 1000,
                       include_originals: bool = False) -> AnonymizationResult:
    """
    Anonymizes an XML document at identity constraint level.

    The values selected by the named *xs:key*/*xs:unique* identity constraints
    are replaced with anonymized ones and all the *xs:keyref* references to
    them are updated accordingly. A one-to-one mapping is built within each
    constraint scope, so equal keys in different scopes are anonymized
    independently. The resulting document is validated against the schema and
    its reference graph is checked to be isomorphic to the original one.

    The operation is atomic: on any failure an :class:`AnonymizationError`
    subclass is raised and no partially anonymized document is produced.

    :param schema: an :class:`xmlschema.XMLSchema` instance or the path of \
    an XSD schema file.
    :param source: the XML document to anonymize (anything accepted by \
    :class:`xmlschema.XMLResource`).
    :param constraints: a constraint name or a list of names of the \
    *xs:key*/*xs:unique* identity constraints to anonymize.
    :param replacements: optional caller-provided replacements, a mapping \
    of constraint names to mappings of original key tuples to replacement \
    tuples (a list of ``[key, replacement]`` pairs is also accepted).
    :param generator: optional callable \
    ``generator(field_types, key, scope, taken) -> tuple`` for producing \
    replacement values not provided by the caller. Defaults to a \
    :class:`RestrictedValueGenerator` instance.
    :param seed: optional seed for the default restricted generator.
    :param max_attempts: maximum attempts of the default restricted generator.
    :param include_originals: if set to `True` the original key values are \
    included in the mapping receipt (default: only fingerprints).
    :return: an :class:`AnonymizationResult` with the anonymized document \
    and the controlled mapping receipt.
    """
    if isinstance(schema, (str, Path)):
        schema = xmlschema.XMLSchema(schema)
    if isinstance(constraints, str):
        constraints = [constraints]

    identities = _find_constraints(schema, constraints)
    resource = xmlschema.XMLResource(source)
    root = resource.root
    parents = _build_parent_map(root)
    namespaces = schema.namespaces

    if generator is None:
        generator = RestrictedValueGenerator(seed=seed, max_attempts=max_attempts)
    replacements = replacements or {}

    # Phase 1: collect the key entries of each (constraint, scope element).
    scope_entries: dict = {}
    for identity in identities:
        for scope_elem in root.iter(identity.parent.name):
            entries = _collect_entries(identity, scope_elem, namespaces)
            scope_entries[(identity, scope_elem)] = entries

    # Phase 2: build the one-to-one key mapping of each scope.
    mappings: dict = {}
    receipt_entries = []
    for (identity, scope_elem), entries in scope_entries.items():
        scope_path = _element_path(scope_elem, parents)
        provided = _provided_replacements(replacements, identity)
        mapping: dict = {}
        lexical_map: dict = {}
        taken = {entry.lexical for entry in entries}

        for entry in entries:
            if entry.key in mapping:
                continue

            field_types = _field_types(identity, entry.target.tag)
            if entry.lexical in provided:
                candidate = provided[entry.lexical]
            else:
                candidate = generator(field_types, entry.lexical,
                                      scope_path, taken)
            candidate = tuple(str(value) for value in candidate)

            if len(candidate) != len(entry.slots):
                msg = (f'replacement arity mismatch for constraint '
                       f'{identity.name!r} in scope {scope_path!r} '
                       f'(key fingerprint {_fingerprint(entry.lexical)})')
                raise ValueGenerationError(msg)

            for xsd_type, value in zip(field_types, candidate):
                if xsd_type is not None and not is_legal_value(xsd_type, value):
                    msg = (f'replacement value does not satisfy the field '
                           f'type or facets of constraint {identity.name!r} '
                           f'in scope {scope_path!r} '
                           f'(key fingerprint {_fingerprint(entry.lexical)})')
                    raise ValueGenerationError(msg)

            if candidate in lexical_map.values():
                msg = (f'replacement value collision for constraint '
                       f'{identity.name!r} in scope {scope_path!r} '
                       f'(key fingerprint {_fingerprint(entry.lexical)})')
                raise ReplacementCollisionError(msg)

            mapping[entry.key] = candidate
            lexical_map[entry.lexical] = candidate
            taken.add(candidate)

        mappings[(identity, scope_elem)] = mapping
        for lexical, candidate in lexical_map.items():
            receipt_entries.append(MappingEntry(
                constraint=identity.name,
                scope=scope_path,
                fingerprint=_fingerprint(lexical),
                replacement=candidate,
                original=lexical if include_originals else None,
            ))
        logger.info(
            'anonymized %d key(s) of constraint %r in scope %s',
            len(lexical_map), identity.name, scope_path,
        )

    # Phase 3: schedule the rewrite of keys and of all the references.
    rewrites = []
    for (identity, scope_elem), entries in scope_entries.items():
        mapping = mappings[(identity, scope_elem)]
        for entry in entries:
            for slot, value in zip(entry.slots, mapping[entry.key]):
                rewrites.append((slot, value))

    anonymized = set(identities)
    for keyref in _iter_keyrefs(schema):
        refer = _resolve_refer(keyref)
        if refer not in anonymized:
            continue
        for scope_elem in root.iter(keyref.parent.name):
            scope_path = _element_path(scope_elem, parents)
            key_scopes = _resolve_refer_path(keyref.refer_path, scope_elem, parents)
            entries = _collect_entries(keyref, scope_elem, namespaces)
            for entry in entries:
                found = []
                for key_scope in key_scopes:
                    mapping = mappings.get((refer, key_scope))
                    if mapping and entry.key in mapping:
                        found.append(mapping[entry.key])

                if not found:
                    msg = (f'cannot resolve a reference of {keyref.name!r} '
                           f'in scope {scope_path!r} '
                           f'(key fingerprint {_fingerprint(entry.lexical)})')
                    raise UnresolvableReferenceError(msg)
                elif any(candidate != found[0] for candidate in found[1:]):
                    msg = (f'ambiguous reference of {keyref.name!r} '
                           f'in scope {scope_path!r} '
                           f'(key fingerprint {_fingerprint(entry.lexical)})')
                    raise AnonymizationError(msg)

                for slot, value in zip(entry.slots, found[0]):
                    rewrites.append((slot, value))

    # Phase 4: apply the rewrites to a copy of the document.
    new_root = copy.deepcopy(root)
    paths = _index_paths(root)
    for slot, value in rewrites:
        element = _navigate(new_root, paths[id(slot.element)])
        if slot.attribute is None:
            element.text = value
        else:
            element.set(slot.attribute, value)

    # Phase 5: validate the anonymized document against the schema.
    errors = list(schema.iter_errors(new_root))
    if errors:
        msg = (f'anonymized document does not validate against the schema: '
               f'{len(errors)} error(s), first: {errors[0]}')
        raise AnonymizationError(msg)

    # Phase 6: verify the reference graph connectivity is unchanged.
    transforms = {}
    for (identity, scope_elem), mapping in mappings.items():
        scope_path = _element_path(scope_elem, parents)
        transform = {}
        for entry in scope_entries[(identity, scope_elem)]:
            transform[entry.lexical] = mapping[entry.key]
        transforms[(identity.name, scope_path)] = transform

    before_edges = _reference_graph(schema, root, parents)
    new_parents = _build_parent_map(new_root)
    after_edges = _reference_graph(schema, new_root, new_parents)
    _verify_connectivity(before_edges, after_edges, transforms)

    document = xmlschema.etree_tostring(
        new_root, namespaces=resource.get_namespaces(),
    )
    logger.info('anonymization completed: %d key(s) replaced', len(receipt_entries))
    return AnonymizationResult(document=document,
                               receipt=MappingReceipt(receipt_entries))


def main(argv: Optional[list] = None) -> int:
    """Command line interface for :func:`anonymize_document`."""
    parser = argparse.ArgumentParser(
        prog='xmlschema-anonymize',
        description='Anonymize an XML document at identity constraint level, '
                    'keeping key/keyref referential integrity.',
    )
    parser.add_argument('schema', help='XSD schema file')
    parser.add_argument('source', help='XML document to anonymize')
    parser.add_argument('constraints', nargs='+',
                        help='names of the xs:key/xs:unique constraints to anonymize')
    parser.add_argument('-o', '--output', metavar='FILE',
                        help='output file (default: standard output)')
    parser.add_argument('-r', '--receipt', metavar='FILE',
                        help='write the mapping receipt as JSON to FILE')
    parser.add_argument('--replacements', metavar='FILE',
                        help='JSON file with caller-provided replacements: '
                             '{constraint: [[key, replacement], ...]}')
    parser.add_argument('--seed', type=int, default=None,
                        help='seed for the restricted value generator')
    parser.add_argument('--max-attempts', type=int, default=1000,
                        help='maximum attempts of the restricted value generator')
    parser.add_argument('--include-originals', action='store_true',
                        help='include the original key values in the receipt')
    args = parser.parse_args(argv)

    replacements = None
    if args.replacements:
        with open(args.replacements, encoding='utf-8') as fp:
            replacements = json.load(fp)

    try:
        result = anonymize_document(
            args.schema, args.source, args.constraints,
            replacements=replacements,
            seed=args.seed,
            max_attempts=args.max_attempts,
            include_originals=args.include_originals,
        )
    except AnonymizationError as err:
        print(f'error: {err}', file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(result.document, encoding='utf-8')
    else:
        print(result.document)
    if args.receipt:
        Path(args.receipt).write_text(result.receipt.to_json(), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
