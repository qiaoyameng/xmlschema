"""
Bounded, valid XML instance generation from an XML Schema.

The generator favours the instance with the *fewest element nodes*: optional
content is omitted, repeating content is generated exactly ``minOccurs``
times and choices resolve to their cheapest branch. Random choices are only
used to break ties and are fully controlled by an integer *seed*.

Three, and only three, failure kinds are distinguished:

* **no solution** -- the schema admits no finite instance (or none satisfying
  a mandatory identity constraint);
* **budget exhausted** -- a finite instance exists but not within
  *max_nodes* / *max_occurs*;
* **unsupported** -- a construct (patterns, assertions, wildcards, complex
  identity paths, ...) the generator deliberately does not support.

Every successful candidate is finally validated against the *same* schema
before it is returned.
"""
from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Optional
from xml.etree.ElementTree import Element


from ..validators.attributes import XsdAttribute, XsdAnyAttribute
from ..validators.complex_types import XsdComplexType
from ..validators.elements import XsdElement
from ..validators.groups import XsdGroup
from ..validators.identities import XsdIdentity, XsdKey, XsdKeyref, XsdUnique
from ..validators.simple_types import XsdSimpleType
from ..validators.wildcards import XsdAnyElement, XsdOpenContent
from .exceptions import (
    BudgetExhaustedError,
    GenerationStatus,
    GenerationValidationError,
    NoInstancePossibleError,
    UnsupportedSchemaFeatureError,
)
from .identities import (
    IdentityPlan,
    build_identity_plan,
    field_chain_particles,
    get_field_node,
    resolve_field_type,
    select_elements,
    set_field_value,
)
from .values import synthesize_value

INF: float = float('inf')
DEFAULT_MAX_NODES = 200
DEFAULT_MAX_OCCURS = 3

_XSI_NS = 'http://www.w3.org/2001/XMLSchema-instance'
_XSI_NIL = '{%s}nil' % _XSI_NS
MAX_RECURSION_DEPTH = 12
MAX_GENERATION_ATTEMPTS = 4

def _group_particles(group: XsdGroup) -> tuple[str, list]:
    referenced = group.ref
    if referenced is not None:
        return referenced.model, referenced.content
    return group.model, group.content


def _element_target(element: XsdElement) -> XsdElement:
    return element.ref if element.ref is not None else element


# ---------------------------------------------------------------------------
# Minimum node cost analysis
# ---------------------------------------------------------------------------

class MinCostAnalyzer:
    """
    Weighted reachability analysis of the schema component graph.

    Node weights: element particles cost 1 (the element tag itself), model
    groups cost 0. The minimum cost of a particle is the minimum total weight
    of any finite expansion tree. Costs are computed with Bellman-Ford style
    relaxation from infinite initial values; a component that can still be
    relaxed after ``N`` full sweeps lies on, or depends on, a zero-weight cycle
    with no emtiable exit -- i.e. an infinite mandatory recursion.
    """

    def __init__(self) -> None:
        self.element_costs: dict[XsdElement, int | float] = {}
        self.group_costs: dict[XsdGroup, int | float] = {}
        # Ids of elements whose *raw* content (without the xsi:nil shortcut)
        # has no finite completion.
        self.raw_infinite_elements: dict[int, bool] = {}
        # element -> its content group (if complex element-only)
        self._element_group: dict[XsdElement, XsdGroup] = {}
        # group -> (model, list of child particles)
        self._group_children: dict[XsdGroup, tuple[str, list]] = {}

    def analyze(self, root: XsdElement) -> None:
        self._collect_element(root, frozenset())
        elements = list(self.element_costs)
        groups = list(self.group_costs)
        n_nodes = len(elements) + len(groups)

        # Terminals (simple/empty typed elements and empty groups) converge
        # first; N sweeps are enough for any acyclic finite expansion path.
        for _ in range(n_nodes):
            self._relax()
        snapshot_elements = dict(self.element_costs)
        snapshot_groups = dict(self.group_costs)
        self._relax()

        divergent_elements = {
            e for e in elements
            if self.element_costs[e] < snapshot_elements[e]
        }
        divergent_groups = {
            g for g in groups
            if self.group_costs[g] < snapshot_groups[g]
        }
        # Propagate INF to every node depending on a divergent one.
        for _ in range(n_nodes):
            changed = False
            for group in groups:
                if group in divergent_groups:
                    continue
                if self._group_unit_cost(group) is INF:
                    divergent_groups.add(group)
                    changed = True
            for element in elements:
                if element in divergent_elements:
                    continue
                if self._element_content_cost(element) is INF:
                    divergent_elements.add(element)
                    changed = True
            if not changed:
                break
        for element in divergent_elements:
            self.element_costs[element] = INF
        for group in divergent_groups:
            self.group_costs[group] = INF

        self.raw_infinite_elements = self._compute_raw_infinite(
            list(elements) if (elements := list(self.element_costs)) else [],
            list(self.group_costs),
        )

    def _compute_raw_infinite(self, elements, groups) -> dict[int, bool]:
        # Same fixpoint as the main analysis but WITHOUT treating nillable
        # elements as zero-cost: this reveals which elements can ONLY be
        # completed through xsi:nil.
        raw_elements = {e: INF for e in elements}
        raw_groups = {g: INF for g in groups}

        def element_raw(element: XsdElement) -> int | float:
            group = self._element_group.get(element)
            return 0 if group is None else raw_groups.get(group, INF)

        def group_raw(group: XsdGroup) -> int | float:
            model, particles = self._group_children.get(
                group, _group_particles(group),
            )
            if not particles:
                return 0
            costs = []
            for particle in particles:
                if particle.max_occurs == 0 or particle.min_occurs == 0:
                    costs.append(0)
                    continue
                if isinstance(particle, XsdGroup):
                    unit = raw_groups.get(particle, INF)
                else:
                    target = _element_target(particle)
                    unit = raw_elements.get(target, INF)
                    if unit is not INF:
                        unit = 1 + unit
                costs.append(INF if unit is INF else unit * particle.min_occurs)
            if model == 'choice':
                return min(costs)
            return INF if any(c is INF for c in costs) else sum(costs)

        for _ in range(len(elements) + len(groups) + 2):
            changed = False
            for group in groups:
                value = group_raw(group)
                if value < raw_groups[group]:
                    raw_groups[group] = value
                    changed = True
            for element in elements:
                value = element_raw(element)
                if value < raw_elements[element]:
                    raw_elements[element] = value
                    changed = True
            if not changed:
                break
        return {id(e): raw_elements[e] is INF for e in elements}

    def _relax(self) -> None:
        for group in self.group_costs:
            value = self._group_unit_cost(group)
            if value < self.group_costs[group]:
                self.group_costs[group] = value
        for element in self.element_costs:
            value = self._element_content_cost(element)
            if value < self.element_costs[element]:
                self.element_costs[element] = value

    # -- collection ----------------------------------------------------------
    def _collect_element(self, element: XsdElement,
                         path: frozenset[int]) -> None:
        if element not in self.element_costs:
            self.element_costs[element] = INF
        if id(element) in path:
            return
        path = path | {id(element)}
        xsd_type = element.type
        if isinstance(xsd_type, XsdComplexType) and \
                isinstance(xsd_type.content, XsdGroup):
            self._element_group[element] = xsd_type.content
            self._collect_group(xsd_type.content, path)
        # A simpleContent complex type (or empty content) needs no child nodes.

    def _collect_group(self, group: XsdGroup,
                       path: frozenset[int]) -> None:
        if group not in self.group_costs:
            self.group_costs[group] = INF
        model, particles = _group_particles(group)
        signature = (model, particles)
        previous = self._group_children.get(group)
        if previous is None:
            self._group_children[group] = signature
        if id(group) in path:
            return
        path = path | {id(group)}
        for particle in particles:
            if isinstance(particle, XsdGroup):
                self._collect_group(particle, path)
            elif isinstance(particle, XsdElement):
                self._collect_element(_element_target(particle), path)

    # -- unit costs ----------------------------------------------------------
    def _element_content_cost(self, element: XsdElement) -> int | float:
        group = self._element_group.get(element)
        if group is None:
            return 0
        content_cost = self.group_costs.get(group, INF)
        if content_cost is INF and element.nillable:
            # xsi:nil="true" legally suppresses the whole content model.
            return 0
        return content_cost

    def _element_unit_cost(self, element: XsdElement) -> int | float:
        return self._element_content_cost(element)

    def _group_unit_cost(self, group: XsdGroup) -> int | float:
        model, particles = self._group_children.get(
            group, _group_particles(group),
        )
        if not particles:
            return 0
        costs = [self._particle_total_cost(p) for p in particles]
        if model == 'choice':
            return min(costs)
        if any(c is INF for c in costs):
            return INF
        return sum(costs)

    def _particle_unit_cost(self, particle) -> int | float:
        if isinstance(particle, XsdGroup):
            return self.group_costs.get(particle, INF)
        target = _element_target(particle)
        return self.element_costs.get(target, INF)

    def _particle_total_cost(self, particle) -> int | float:
        if particle.max_occurs == 0 or particle.min_occurs == 0:
            return 0
        unit = self._particle_unit_cost(particle)
        if unit is INF:
            return INF
        per_occurrence = 1 + unit if isinstance(particle, XsdElement) else unit
        return per_occurrence * particle.min_occurs

    def particle_total_cost(self, particle) -> int | float:
        return self._particle_total_cost(particle)


# ---------------------------------------------------------------------------
# Structure generation
# ---------------------------------------------------------------------------

class _BudgetExceeded(Exception):
    """Internal control-flow signal for a branch that exceeds node budget."""


class _StructureGenerator:
    """
    Grows an ElementTree following minimum-node preferences.

    :ivar forced_particles: XsdElement particle ids that must occur at least
        once in the subtree currently being generated (used to materialize
        key/keyref selected elements on a second attempt).
    """

    def __init__(self, costs: MinCostAnalyzer, rng: random.Random,
                 *, max_nodes: int, max_occurs: int,
                 forced_particles: Optional[set[int]] = None) -> None:
        self.costs = costs
        self.rng = rng
        self.max_nodes = max_nodes
        self.max_occurs = max_occurs
        self.forced_particles = forced_particles if forced_particles is not None else set()
        self.nodes = 0
        self.node_meta: dict[Element, XsdElement] = {}
        # (particle id, target element id) pairs on the current generation
        # path: used to stop identity-driven forcing from following a
        # recursive binding forever.
        self.particle_path: set[tuple[int, int]] = set()

    # -- public entry --------------------------------------------------------
    def generate(self, element: XsdElement) -> Element:
        return self._make_element(element)

    # -- element node --------------------------------------------------------
    def _make_element(self, particle: XsdElement) -> Element:
        target = _element_target(particle)

        if target.abstract:
            substitutes = sorted(
                (e for e in self._substitutions(target) if not e.abstract),
                key=lambda e: self.costs.element_costs.get(e, INF),
            )
            if not substitutes:
                raise NoInstancePossibleError(
                    f"element {target.prefixed_name!r} is abstract and has no "
                    "concrete substitution-group member",
                    component=target,
                )
            target = substitutes[0]

        self._reserve(1, target)
        etree_elem = Element(particle.name)
        self.node_meta[etree_elem] = target
        self._generate_attributes(etree_elem, target)
        self._generate_content(etree_elem, target)
        return etree_elem

    def _substitutions(self, target: XsdElement) -> set[XsdElement]:
        groups = target.maps.substitution_groups
        return groups.get(target.name, set()) if hasattr(groups, 'get') else set()

    def _reserve(self, count: int, component: object) -> None:
        if self.nodes + count > self.max_nodes:
            raise _BudgetExceeded(component)
        self.nodes += count

    # -- attributes ----------------------------------------------------------
    def _generate_attributes(self, etree_elem: Element, target: XsdElement) -> None:
        if not hasattr(target.type, 'attributes'):
            return
        attribute_group = target.type.attributes
        for name, attribute in attribute_group.items():
            if isinstance(attribute, XsdAnyAttribute):
                # anyAttribute never has to be instantiated.
                continue
            if attribute.use != 'required' or name is None:
                continue
            value = self._attribute_value(attribute)
            etree_elem.set(attribute.name, value)

    def _attribute_value(self, attribute: XsdAttribute) -> str:
        if attribute.fixed is not None:
            return attribute.fixed
        if attribute.default is not None:
            return attribute.default
        return synthesize_value(
            attribute.type, self.rng,
            purpose=f"required attribute {attribute.prefixed_name!r}",
        )

    # -- content -------------------------------------------------------------
    def _generate_content(self, etree_elem: Element, target: XsdElement) -> None:
        xsd_type = target.type
        if isinstance(xsd_type, XsdSimpleType):
            etree_elem.text = synthesize_value(
                xsd_type, self.rng,
                purpose=f"element {target.prefixed_name!r}",
            )
            return

        if not isinstance(xsd_type, XsdComplexType):
            return

        self._check_unsupported_complex_type(xsd_type)

        content = xsd_type.content
        if isinstance(content, XsdSimpleType):
            etree_elem.text = synthesize_value(
                content, self.rng,
                purpose=f"element {target.prefixed_name!r}",
            )
            return
        if not isinstance(content, XsdGroup):
            return

        # xsi:nil is the *last-resort* terminating branch for a nillable
        # element whose raw content model has no finite minimum completion.
        # It is checked against the RAW (nil-free) analysis so ordinary
        # finite content is always preferred.
        if target.nillable and \
                self.costs.raw_infinite_elements.get(id(target), False):
            etree_elem.set(_XSI_NIL, 'true')
            return

        # Empty element-only content: nothing to emit.
        children = self._generate_group(content)
        etree_elem.extend(children)

    def _check_unsupported_complex_type(self, xsd_type: XsdComplexType) -> None:
        assertions = getattr(xsd_type, 'assertions', ())
        if assertions:
            raise UnsupportedSchemaFeatureError(
                f"complexType {xsd_type.prefixed_name if xsd_type.name else '<anonymous>'!r} "
                "uses an XSD 1.1 xs:assert, which is not supported by the bounded "
                "instance generator",
                component=xsd_type,
            )
        open_content = getattr(xsd_type, 'open_content', None)
        if isinstance(open_content, XsdOpenContent):
            raise UnsupportedSchemaFeatureError(
                "XSD 1.1 openContent is not supported by the bounded instance "
                "generator",
                component=xsd_type,
            )

    # -- model groups --------------------------------------------------------
    def _generate_group(self, group: XsdGroup) -> list[Element]:
        model, particles = _group_particles(group)
        if not particles:
            return []

        min_occurs, max_occurs = self._effective_occurs(group)
        if max_occurs == 0:
            return []

        result: list[Element] = []
        for occurrence in range(min_occurs):
            result.extend(self._generate_group_once(model, particles, group))
        return result

    def _effective_occurs(self, particle) -> tuple[int, Optional[int]]:
        min_occurs = particle.min_occurs
        max_occurs = particle.max_occurs
        if max_occurs is not None and max_occurs > self.max_occurs and \
                min_occurs <= self.max_occurs:
            # Unbounded/large repetitions are clamped; the minimum is kept.
            max_occurs = self.max_occurs
        return min_occurs, max_occurs

    def _generate_group_once(self, model: str, particles: list,
                             wrapper: XsdGroup) -> list[Element]:
        if model == 'all':
            return self._generate_all(particles)
        if model == 'choice':
            return self._generate_choice(particles, wrapper)
        return self._generate_sequence(particles)

    def _generate_sequence(self, particles: list) -> list[Element]:
        result: list[Element] = []
        for particle in particles:
            result.extend(self._generate_particle(particle))
        return result

    def _generate_all(self, particles: list) -> list[Element]:
        # xs:all children are each an element with 0/1 occurrence (plus
        # emtiable groups in XSD 1.1). Document order is irrelevant for xs:all.
        ordered = sorted(
            particles,
            key=lambda p: (
                p.min_occurs == 0 and id(p) not in self.forced_particles,
                self.costs.particle_total_cost(p),
            ),
        )
        result: list[Element] = []
        for particle in ordered:
            result.extend(self._generate_particle(particle))
        return result

    def _generate_choice(self, particles: list, wrapper: XsdGroup) -> list[Element]:
        feasible: list = []
        for particle in particles:
            if isinstance(particle, XsdAnyElement):
                continue
            cost = self.costs.particle_total_cost(particle)
            if cost is not INF:
                feasible.append((particle, cost))

        forced = [p for p, _ in feasible if id(p) in self.forced_particles]
        if forced:
            chosen = self._cheapest_random(forced)
            return self._generate_particle(chosen)

        if not feasible:
            if wrapper.min_occurs == 0:
                return []
            if any(isinstance(p, XsdAnyElement) for p in particles):
                raise UnsupportedSchemaFeatureError(
                    "a mandatory xs:choice can only expand into an xs:any "
                    "wildcard, which cannot be instantiated by the bounded "
                    "instance generator",
                    component=wrapper,
                )
            raise NoInstancePossibleError(
                "a mandatory xs:choice has no branch that can terminate",
                component=wrapper,
            )

        minimum = min(cost for _, cost in feasible)
        candidates = [p for p, cost in feasible if cost == minimum]
        chosen = self.rng.choice(candidates)
        return self._generate_particle(chosen)

    def _cheapest_random(self, particles: list):
        return self.rng.choice(particles)

    # -- particles -----------------------------------------------------------
    def _generate_particle(self, particle) -> list[Element]:
        if isinstance(particle, XsdAnyElement):
            return self._generate_wildcard(particle)
        if isinstance(particle, XsdGroup):
            return self._generate_group_particle(particle)
        return self._generate_element_particle(particle)

    def _generate_group_particle(self, group: XsdGroup) -> list[Element]:
        min_occurs, _ = self._effective_occurs(group)
        if min_occurs == 0 and id(group) not in self.forced_particles:
            return []

        unit_cost = self.costs.group_costs.get(group, INF)
        result: list[Element] = []
        repetitions = max(1, min_occurs) if id(group) in self.forced_particles \
            else min_occurs
        for _ in range(repetitions):
            result.extend(self._generate_group(group))
        return result

    def _generate_element_particle(self, particle: XsdElement) -> list[Element]:
        target = _element_target(particle)
        min_occurs, _max_occurs = self._effective_occurs(particle)
        forced = id(particle) in self.forced_particles
        path_key = (id(particle), id(target))

        # Identity forcing may re-enter a recursive particle on every nesting
        # level. The first re-entry on a given path is enough for the key/
        # keyref to have at least one node at each mandatory level; further
        # identical re-entries would descend forever.
        if forced and min_occurs == 0 and path_key in self.particle_path:
            forced = False
        if min_occurs == 0 and not forced:
            return []


        if isinstance(target.type, XsdComplexType) and \
                isinstance(target.type.content, XsdGroup):
            self._check_unsupported_complex_type(target.type)

        result: list[Element] = []
        repetitions = max(1, min_occurs)
        if forced and repetitions == 0:
            repetitions = 1

        self.particle_path.add(path_key)
        try:
            for _ in range(repetitions):
                result.append(self._make_element(particle))
        finally:
            self.particle_path.discard(path_key)
        return result

    def _generate_wildcard(self, wildcard: XsdAnyElement) -> list[Element]:
        if wildcard.min_occurs == 0:
            return []
        raise UnsupportedSchemaFeatureError(
            f"a mandatory xs:any wildcard (occurs={wildcard.min_occurs}) cannot "
            "be instantiated by the bounded instance generator",
            component=wildcard,
        )


# ---------------------------------------------------------------------------
# Identity constraint filling
# ---------------------------------------------------------------------------

class IdentityFiller:
    """
    Assigns values to key/unique/keyref fields of a generated tree and reports
    which selected element particles had to be present to satisfy a keyref.
    """

    def __init__(self, schema, rng: random.Random) -> None:
        self.schema = schema
        self.rng = rng
        self.plans: dict[XsdIdentity, IdentityPlan] = {}
        # Per owner element: identity -> set of key tuples
        self.tables: dict[Element, dict[XsdIdentity, set[tuple]]] = {}
        # Monotonic counters guaranteeing distinct synthesized key values.
        self._key_counters: dict[int, int] = {}
        self._numeric_counters: dict[int, int] = {}

    def plan(self, identity: XsdIdentity) -> IdentityPlan:
        if identity not in self.plans:
            self.plans[identity] = build_identity_plan(identity)
        return self.plans[identity]

    def fill_scope(self, scope: Element, identities: list[XsdIdentity],
                   node_meta: dict[Element, XsdElement]) -> None:
        tables = self.tables.setdefault(scope, {})
        for identity in identities:
            if not isinstance(identity, (XsdKey, XsdUnique)):
                continue
            plan = self.plan(identity)
            selected = select_elements(scope, plan, node_meta)
            tables[identity] = set()
            self._assign_keys(identity, plan, selected, tables[identity],
                              node_meta)

        for identity in identities:
            if not isinstance(identity, XsdKeyref):
                continue
            plan = self.plan(identity)
            refer = identity.refer
            if not isinstance(refer, (XsdKey, XsdUnique)):
                raise UnsupportedSchemaFeatureError(
                    f"keyref {identity.name!r} does not reference a built "
                    "key/unique constraint",
                    component=identity,
                )
            if refer.parent is not identity.parent:
                raise UnsupportedSchemaFeatureError(
                    f"keyref {identity.name!r} references key {refer.name!r} "
                    "declared on a different element (cross-scope keyref is not "
                    "supported by the bounded instance generator)",
                    component=identity,
                )
            selected = select_elements(scope, plan, node_meta)
            refer_values = tables.get(refer, set())
            self._assign_keyrefs(identity, plan, selected, refer_values,
                                 node_meta, scope)

    # -- key/unique ----------------------------------------------------------
    def _assign_keys(self, identity: XsdIdentity, plan: IdentityPlan,
                     selected: list[Element], table: set[tuple],
                     node_meta: dict[Element, XsdElement]) -> None:
        is_key = isinstance(identity, XsdKey)
        for element in selected:
            values: list[str] = []
            complete = True
            for target in plan.targets:
                node = get_field_node(element, target)
                if node is None:
                    complete = False
                    break
                _, holder, key = node
                existing = holder.attrib[key] if key is not None else \
                    (holder.text or '')
                if existing:
                    values.append(existing)
                else:
                    values.append('')
            if not complete:
                if is_key:
                    raise NoInstancePossibleError(
                        f"key {identity.name!r} selects an element whose field "
                        "node is missing"
                    )
                continue

            tuple_value = tuple(values)
            needs_new_value = is_key and (
                any(v == '' for v in values) or tuple_value in table
            )
            if needs_new_value:
                tuple_value = self._synthesize_key_tuple(
                    identity, plan, element, values, node_meta, table,
                )
            elif tuple_value in table and all(v != '' for v in values):
                # xs:unique: two non-null duplicates are invalid, while a
                # missing field participates in no uniqueness check.
                tuple_value = self._synthesize_key_tuple(
                    identity, plan, element, values, node_meta, table,
                )
            table.add(tuple_value)
            for target, value in zip(plan.targets, tuple_value):
                set_field_value(element, target, value)

    def _synthesize_key_tuple(self, identity: XsdIdentity, plan: IdentityPlan,
                              element: Element, current: list[str],
                              node_meta: dict[Element, XsdElement],
                              table: set[tuple]) -> tuple:
        component = node_meta[element]
        base_values = list(current)
        missing = [i for i, value in enumerate(base_values) if value == '']
        for index in missing:
            target = plan.targets[index]
            stype = resolve_field_type(component, target)
            base_values[index] = synthesize_value(
                stype, self.rng, purpose=f"key {identity.name!r} field",
            )

        candidate = tuple(base_values)
        if candidate not in table:
            return candidate

        # Deterministically make the tuple unique by suffixing one mutable
        # string-like field. Prefer a missing field; otherwise suffix the
        # first field whose type accepts arbitrary strings.
        mutable_index = self._mutable_key_field(
            plan, component, base_values, missing,
        )
        counter_key = id(identity)
        while True:
            sequence = self._key_counters.get(counter_key, 0) + 1
            self._key_counters[counter_key] = sequence
            target = plan.targets[mutable_index]
            stype = resolve_field_type(component, target)
            suffix = f"k{sequence}"
            candidate_values = list(base_values)
            candidate_values[mutable_index] = self._append_suffix(
                stype, base_values[mutable_index], suffix,
            )
            candidate = tuple(candidate_values)
            if candidate not in table:
                return candidate
            if sequence > 4096:
                raise NoInstancePossibleError(
                    f"cannot synthesize enough distinct values for key "
                    f"{identity.name!r}"
                )

    def _mutable_key_field(self, plan: IdentityPlan, component: XsdElement,
                           base_values: list[str],
                           missing: list[int]) -> int:
        if missing:
            return missing[0]
        from .values import family_of
        numeric = set((
            'integer', 'long', 'int', 'short', 'byte', 'nonNegativeInteger',
            'nonPositiveInteger', 'positiveInteger', 'negativeInteger',
            'unsignedLong', 'unsignedInt', 'unsignedShort', 'unsignedByte',
        ))
        for index, target in enumerate(plan.targets):
            family = family_of(resolve_field_type(component, target))
            if family in {'string', 'token', 'ID', 'IDREF', 'NCName',
                          'NMTOKEN', 'Name'}:
                return index
        for index, target in enumerate(plan.targets):
            if family_of(resolve_field_type(component, target)) in numeric:
                return index
        return 0

    def _append_suffix(self, stype, base: str, suffix: str) -> str:
        from .values import family_of
        family = family_of(stype)
        numeric = set((
            'integer', 'long', 'int', 'short', 'byte', 'nonNegativeInteger',
            'nonPositiveInteger', 'positiveInteger', 'negativeInteger',
            'unsignedLong', 'unsignedInt', 'unsignedShort', 'unsignedByte',
            'decimal',
        ))
        if family in numeric:
            return self._next_numeric_value(stype)
        max_length = getattr(stype, 'max_length', None)
        value = f"{base}{suffix}"
        if max_length is not None and len(value) > int(max_length):
            value = (base[:max(0, int(max_length) - len(suffix))] + suffix)
            value = value[-int(max_length):]
        return value

    def _next_numeric_value(self, stype) -> str:
        counter = self._numeric_counters.get(id(stype), 0) + 1
        self._numeric_counters[id(stype)] = counter
        lo = getattr(stype, 'min_value', None)
        hi = getattr(stype, 'max_value', None)
        try:
            lower = int(lo) if lo is not None else 0
            upper = int(hi) if hi is not None else None
        except (TypeError, ValueError):
            return str(counter)
        if lower > 0:
            value = lower + counter - 1
        elif upper is not None and upper < 0:
            value = upper - counter + 1
        else:
            value = counter
        if upper is not None and value > upper:
            raise NoInstancePossibleError(
                "numeric key facet bounds do not admit enough distinct values"
            )
        return str(value)

    # -- keyref --------------------------------------------------------------
    def _assign_keyrefs(self, identity: XsdKeyref, plan: IdentityPlan,
                        selected: list[Element], refer_values: set[tuple],
                        node_meta: dict[Element, XsdElement],
                        scope: Element) -> None:
        for element in selected:
            nodes = []
            missing = False
            for target in plan.targets:
                node = get_field_node(element, target)
                if node is None:
                    missing = True
                    break
                nodes.append(node)
            if missing:
                continue

            current = tuple(
                holder.attrib[key] if key is not None else (holder.text or '')
                for _, holder, key in nodes
            )
            if all(v != '' for v in current) and current in refer_values:
                continue

            if not refer_values:
                raise NoInstancePossibleError(
                    f"keyref {identity.name!r} selects {len(selected)} element(s) "
                    f"but referenced key {identity.refer.name!r} has no value "
                    "to point to"
                )
            chosen = self.rng.choice(sorted(refer_values))
            for target, value in zip(plan.targets, chosen):
                set_field_value(element, target, value)

    # -- forcing analysis ----------------------------------------------------
    def required_forced_particles(
        self,
        scopes: list[tuple[Element, list[XsdIdentity]]],
        node_meta: dict[Element, XsdElement],
    ) -> set[int]:
        """
        Particle ids of key/keyref selected elements that must be present for
        identity constraints to be satisfiable in the generated tree.
        """
        forced: set[int] = set()
        for scope, identities in scopes:
            for identity in identities:
                plan = self.plan(identity)
                needs_presence = isinstance(identity, XsdKey)
                if isinstance(identity, XsdKeyref):
                    refer = identity.refer
                    if not isinstance(refer, (XsdKey, XsdUnique)):
                        continue
                    refer_plan = self.plan(refer)
                    refer_selected = select_elements(
                        scope, refer_plan, node_meta,
                    )
                    needs_presence = len(refer_selected) == 0
                if not needs_presence:
                    continue
                for element_comp in identity.elements:
                    if isinstance(element_comp, XsdElement):
                        forced.add(id(element_comp))
                        for target in plan.targets:
                            for particle in field_chain_particles(
                                element_comp, target,
                            ):
                                forced.add(id(particle))
        return forced


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def _initial_forced_particles(root: XsdElement) -> set[int]:
    """
    Particle ids that must be materialized so that every key/unique identity
    declared on an element reachable from *root* has its selected elements
    and field chains available. Recursive bindings create distinct local
    particle objects for each nesting level, so traversal tracks the *element
    type* path rather than visited particle ids.
    """
    forced: set[int] = set()

    def _mark_identities(element: XsdElement) -> None:
        for identity in element.identities:
            if not isinstance(identity, (XsdKey, XsdUnique)):
                continue
            plan = build_identity_plan(identity)
            for bound in identity.elements:
                if isinstance(bound, XsdElement):
                    forced.add(id(bound))
                    for target in plan.targets:
                        for particle in field_chain_particles(bound, target):
                            forced.add(id(particle))

    def _walk_group(group: XsdGroup, depth: int) -> None:
        # Local anonymous groups of recursive types are distinct objects at
        # each level; a depth guard only protects against pathological cycles
        # through shared named groups/types.
        if depth > MAX_RECURSION_DEPTH:
            return
        _, particles = _group_particles(group)
        for particle in particles:
            if isinstance(particle, XsdGroup):
                _walk_group(particle, depth)
            elif isinstance(particle, XsdElement):
                _walk_particle(particle, depth + 1)

    def _mark_from_element(element: XsdElement) -> None:
        for identity in element.identities:
            if not isinstance(identity, (XsdKey, XsdUnique)):
                continue
            plan = build_identity_plan(identity)
            for bound in identity.elements:
                if isinstance(bound, XsdElement):
                    forced.add(id(bound))
                    for field_target in plan.targets:
                        for leaf in field_chain_particles(bound, field_target):
                            forced.add(id(leaf))

    def _walk_particle(particle: XsdElement, depth: int) -> None:
        if depth > MAX_RECURSION_DEPTH:
            return
        target = _element_target(particle)
        _mark_from_element(target)
        xsd_type = target.type
        if isinstance(xsd_type, XsdComplexType) and \
                isinstance(xsd_type.content, XsdGroup):
            _walk_group(xsd_type.content, depth)

    _mark_from_element(root)
    xsd_type = root.type
    if isinstance(xsd_type, XsdComplexType) and \
            isinstance(xsd_type.content, XsdGroup):
        _walk_group(xsd_type.content, 0)
    return forced


def _collect_scopes(root: Element, node_meta: dict[Element, XsdElement]
                    ) -> list[tuple[Element, list[XsdIdentity]]]:
    scopes: list[tuple[Element, list[XsdIdentity]]] = []
    for element in root.iter():
        component = node_meta.get(element)
        if component is None:
            continue
        if component.identities:
            scopes.append((element, list(component.identities)))
    return scopes


def _preflight_simple_type(st: XsdSimpleType,
                           seen: set[int]) -> Optional[str]:
    if id(st) in seen:
        return None
    seen.add(id(st))
    if st.enumeration is not None:
        return None
    from .values import has_unsupported_facet
    return has_unsupported_facet(st)


def _iter_simple_types(element: XsdElement, seen_components: set[int]
                       ) -> Iterator[XsdSimpleType]:
    if id(element) in seen_components:
        return
    seen_components.add(id(element))
    target = _element_target(element)

    xsd_type = target.type
    if isinstance(xsd_type, XsdSimpleType):
        yield xsd_type
    elif isinstance(xsd_type, XsdComplexType):
        content = xsd_type.content
        if isinstance(content, XsdSimpleType):
            yield content
        elif isinstance(content, XsdGroup):
            for attribute in xsd_type.attributes.values():
                if isinstance(attribute, XsdAttribute):
                    yield attribute.type
            yield from _iter_simple_types_group(content, seen_components)


def _iter_simple_types_group(group: XsdGroup,
                             seen_components: set[int]
                             ) -> Iterator[XsdSimpleType]:
    if id(group) in seen_components:
        return
    seen_components.add(id(group))
    _, particles = _group_particles(group)
    for particle in particles:
        if isinstance(particle, XsdGroup):
            yield from _iter_simple_types_group(particle, seen_components)
        elif isinstance(particle, XsdElement):
            yield from _iter_simple_types(particle, seen_components)
        elif isinstance(particle, XsdAnyElement) and particle.min_occurs:
            raise UnsupportedSchemaFeatureError(
                "a mandatory xs:any wildcard cannot be instantiated by the "
                "bounded instance generator",
                component=particle,
            )


def _preflight(root: XsdElement) -> None:
    """Fail early with a precise reason for known unsupported constructs."""
    seen_components: set[int] = set()

    def _check_element(element: XsdElement) -> None:
        if id(element) in seen_components:
            return
        seen_components.add(id(element))
        target = _element_target(element)

        for stype in _iter_simple_types(target, set()):
            seen_types: set[int] = set()
            reason = _preflight_simple_type(stype, seen_types)
            if reason is not None:
                raise UnsupportedSchemaFeatureError(reason, component=stype)

        xsd_type = target.type
        if isinstance(xsd_type, XsdComplexType):
            assertions = getattr(xsd_type, 'assertions', ())
            if assertions:
                raise UnsupportedSchemaFeatureError(
                    "an XSD 1.1 xs:assert on a complex type is not supported "
                    "by the bounded instance generator",
                    component=xsd_type,
                )
            if isinstance(getattr(xsd_type, 'open_content', None), XsdOpenContent):
                raise UnsupportedSchemaFeatureError(
                    "XSD 1.1 openContent is not supported by the bounded "
                    "instance generator",
                    component=xsd_type,
                )
            content = xsd_type.content
            if isinstance(content, XsdGroup):
                _check_group(content)

        for identity in target.identities:
            build_identity_plan(identity)
            if isinstance(identity, XsdKeyref) and \
                    isinstance(identity.refer, (XsdKey, XsdUnique)) and \
                    identity.refer.parent is not identity.parent:
                raise UnsupportedSchemaFeatureError(
                    f"keyref {identity.name!r} references a key declared on a "
                    "different element (cross-scope keyref is not supported)",
                    component=identity,
                )

    def _check_group(group: XsdGroup) -> None:
        if id(group) in seen_components:
            return
        seen_components.add(id(group))
        _, particles = _group_particles(group)
        for particle in particles:
            if isinstance(particle, XsdGroup):
                _check_group(particle)
            elif isinstance(particle, XsdAnyElement):
                if particle.min_occurs:
                    raise UnsupportedSchemaFeatureError(
                        "a mandatory xs:any wildcard cannot be instantiated by "
                        "the bounded instance generator",
                        component=particle,
                    )
            elif isinstance(particle, XsdElement):
                _check_element(particle)

    _check_element(root)


def _pick_root(schema, name: Optional[str]) -> XsdElement:
    if name is not None:
        root = schema.elements.get(name) if name.startswith('{') else \
            next((e for e in schema.elements.values()
                  if e.local_name == name or e.prefixed_name == name), None)
        if root is None:
            raise NoInstancePossibleError(
                f"schema has no global element {name!r}"
            )
        return root

    candidates = [e for e in schema.elements.values() if not e.abstract]
    if not candidates:
        raise NoInstancePossibleError("schema declares no concrete global element")

    analyzer = MinCostAnalyzer()
    candidate_list = list(candidates)
    for candidate in candidate_list:
        analyzer.analyze(candidate)
    def _total(element: XsdElement) -> int | float:
        content = analyzer.element_costs.get(element, INF)
        return INF if content is INF else 1 + content

    feasible = [e for e in candidate_list if _total(e) is not INF]
    pool = feasible or candidate_list
    minimum = min(_total(e) for e in pool)
    cheapest = [e for e in pool if _total(e) == minimum]
    return sorted(cheapest, key=lambda e: e.name)[0]


def generate_instance(schema, *, name: Optional[str] = None,
                      seed: int = 0,
                      max_nodes: int = DEFAULT_MAX_NODES,
                      max_occurs: int = DEFAULT_MAX_OCCURS,
                      validate_result: bool = True
                      ) -> Element:
    """
    Generate a small, schema-valid XML element instance.

    :param schema: an :class:`xmlschema.XMLSchema` (or XSD 1.1) instance.
    :param name: optional global element name (local, prefixed or Clark
        notation). When omitted the cheapest concrete global element is used.
    :param seed: integer seed controlling every random choice; the same
        schema and seed always produce the same instance.
    :param max_nodes: hard budget on the number of generated elements.
    :param max_occurs: cap used when a particle allows many/unbounded
        repetitions.
    :param validate_result: if `True` (default) the generated tree is
        validated against *schema* before being returned.
    :returns: an :class:`xml.etree.ElementTree.Element`.
    :raises NoInstancePossibleError: no finite instance exists.
    :raises BudgetExhaustedError: an instance exists but not within budget.
    :raises UnsupportedSchemaFeatureError: an unsupported construct is used.
    :raises GenerationValidationError: validation of the generated candidate
        failed (defensive guard).
    """
    if max_nodes < 1:
        raise ValueError("max_nodes must be >= 1")
    if max_occurs < 0:
        raise ValueError("max_occurs must be >= 0")

    root_component = _pick_root(schema, name)
    _preflight(root_component)

    analyzer = MinCostAnalyzer()
    analyzer.analyze(root_component)
    if analyzer.element_costs.get(root_component, INF) is INF:
        raise NoInstancePossibleError(
            "the element has no finite content: every expansion path is "
            "infinitely recursive with no emtiable branch",
            component=root_component,
        )

    forced_particles = _initial_forced_particles(root_component)
    last_budget_error: Optional[BudgetExhaustedError] = None
    generated: Optional[Element] = None
    node_meta: dict[Element, XsdElement] = {}
    attempts = 0

    for attempts in range(1, MAX_GENERATION_ATTEMPTS + 1):
        rng = random.Random(f"{seed}:{attempts}")
        builder = _StructureGenerator(
            analyzer, rng,
            max_nodes=max_nodes,
            max_occurs=max_occurs,
            forced_particles=forced_particles,
        )
        try:
            generated = builder.generate(root_component)
        except _BudgetExceeded as exc:
            last_budget_error = BudgetExhaustedError(
                f"the minimum valid instance needs more than {max_nodes} "
                "elements",
                budget=max_nodes,
                nodes=builder.nodes,
                component=exc.args[0] if exc.args else None,
            )
            # Forced elements made the structure too big; a different forcing
            # cannot make a mandatory minimum smaller.
            raise last_budget_error

        node_meta = builder.node_meta
        filler = IdentityFiller(schema, rng)
        try:
            scopes = _collect_scopes(generated, node_meta)
            for scope_elem, identities in scopes:
                filler.fill_scope(scope_elem, identities, node_meta)
        except NoInstancePossibleError:
            if forced_particles:
                raise
            # First attempt had no key target: retry forcing them structurally.
            forced_particles = filler.required_forced_particles(scopes, node_meta)
            if not forced_particles:
                raise
            continue

        if validate_result:
            errors = list(schema.iter_errors(generated))
            if errors:
                # Generation is deterministic; a different seed can only
                # change identity tie breaks. Retry with a shifted seed so
                # rare key/keyref value collisions self-heal.
                if attempts < MAX_GENERATION_ATTEMPTS and \
                        any(scope_ids for _, scope_ids in scopes):
                    seed += 1_000
                    continue
                raise GenerationValidationError(
                    "generated instance failed schema validation: "
                    + '; '.join(str(e).splitlines()[0] for e in errors[:5]),
                )
        return generated

    raise GenerationValidationError(
        f"could not produce a valid instance after {attempts} attempts"
    )
