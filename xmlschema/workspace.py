"""
Incremental schema workspace.

A :class:`SchemaWorkspace` watches a set of local XSD resources and maintains
their import/include/redefine dependency graph together with per-resource
summaries (content digest plus the effective target namespace). A refresh only
rebuilds the resources whose summary changed and the resources that depend on
them; resources in unaffected branches keep their already built components.

Resource identity is the pair *normalized base URL* + *effective target
namespace*. A chameleon schema document (no ``targetNamespace``) included from
two different contexts becomes two distinct resources, so components produced
for one effective namespace are never reused for another one.

Each successful refresh publishes an immutable :class:`SchemaSnapshot` holding
a complete built schema. Old readers keep referencing the previous snapshot,
so a failed candidate leaves the previous workspace fully usable. Missing
resources and dependency cycles are reported with the complete chain of
resources involved; no network access is attempted to complete the graph.
"""
from __future__ import annotations

import copy
import hashlib
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError as ElementTreeParseError

import xmlschema.names as nm
from xmlschema.aliases import SchemaType, XMLSourceType
from xmlschema.exceptions import XMLSchemaValueError, XMLResourceError
from xmlschema.loaders import SchemaLoader
from xmlschema.resources import XMLResource
from xmlschema.utils.etree import iter_schema_declarations, iter_schema_open_content
from xmlschema.utils.urls import is_remote_url, normalize_url
from xmlschema.validators import XsdGlobals, XMLSchema, XMLSchema11, \
    XsdDefaultOpenContent
from xmlschema.validators.helpers import parse_xpath_default_namespace

__all__ = [
    'SchemaWorkspaceError',
    'MissingResourceError',
    'DependencyCycleError',
    'ResourceSummary',
    'DependencyEdge',
    'BuildStats',
    'SchemaSnapshot',
    'SchemaWorkspace',
]


class SchemaWorkspaceError(Exception):
    """Base class for schema workspace errors."""

    def __init__(self, message: str, chain: Sequence[str] = ()) -> None:
        self.chain: list[str] = list(chain)
        if self.chain:
            message = f"{message} (chain: {' -> '.join(self.chain)})"
        super().__init__(message)


class MissingResourceError(SchemaWorkspaceError):
    """Raised when a resource referenced by the dependency graph is not available."""

    def __init__(self, location: str, chain: Sequence[str]) -> None:
        self.location = location
        super().__init__(f"missing resource {location!r}", chain)


class DependencyCycleError(SchemaWorkspaceError):
    """Raised when the dependency graph contains a cycle."""

    def __init__(self, chain: Sequence[str]) -> None:
        super().__init__('dependency cycle detected', chain)


@dataclass(frozen=True)
class ResourceSummary:
    """
    Summary of an XSD resource.

    :ivar url: normalized absolute URL of the resource (the parsing base address).
    :ivar target_namespace: the effective target namespace, i.e. the namespace
        declared by the document or the including context for chameleon schemas.
    :ivar digest: digest of the document bytes.
    :ivar declared_namespace: the ``targetNamespace`` as declared by the document
        (empty string for chameleon schemas).
    """
    url: str
    target_namespace: str
    digest: str
    declared_namespace: str

    @property
    def identity(self) -> tuple[str, str]:
        """The resource identity: parsing base URL plus effective namespace."""
        return self.url, self.target_namespace

    @property
    def is_chameleon(self) -> bool:
        return not self.declared_namespace


@dataclass(frozen=True)
class DependencyEdge:
    """
    A dependency edge between two resource identities.

    :ivar source: identity of the containing resource (url, effective namespace).
    :ivar target: identity of the referenced resource (url, effective namespace).
    :ivar kind: one of ``import``, ``include``, ``redefine``, ``override``.
    :ivar location: the literal ``schemaLocation`` / import hint as written.
    """
    source: tuple[str, str]
    target: tuple[str, str]
    kind: str
    location: str


@dataclass
class BuildStats:
    """Counters collected while building a workspace snapshot."""

    loads: int = 0
    """Number of XSD resources actually loaded from disk."""

    global_components_built: int = 0
    """Number of global XSD components instantiated and parsed."""

    schemas_rebuilt: set[str] = field(default_factory=set)
    """URLs of the schema documents that went through a full build."""

    resources_reused: set[tuple[str, str]] = field(default_factory=set)
    """Resource identities whose built components were reused unchanged."""

    namespaces_reused: set[str] = field(default_factory=set)
    """Namespaces of the reused resource identities."""

    def as_dict(self) -> dict[str, Any]:
        return {
            'loads': self.loads,
            'global_components_built': self.global_components_built,
            'schemas_rebuilt': sorted(self.schemas_rebuilt),
            'resources_reused': sorted(
                f'{url} [ns={ns}]' for url, ns in self.resources_reused
            ),
            'namespaces_reused': sorted(self.namespaces_reused),
        }


class SchemaSnapshot:
    """
    An immutable, fully built view of the workspace at a point in time.

    The snapshot owns a complete :class:`~xmlschema.XMLSchema` and can be used by
    any number of readers concurrently. Snapshot objects are never mutated: a
    refresh produces a new snapshot and leaves the previous one untouched.
    """

    def __init__(self, root: SchemaType, generation: int,
                 summaries: dict[tuple[str, str], ResourceSummary],
                 edges: list[DependencyEdge],
                 stats: BuildStats) -> None:
        self._root = root
        self.generation = generation
        self._summaries = dict(summaries)
        self._edges = list(edges)
        self.stats = stats

    @property
    def schema(self) -> SchemaType:
        """The built root schema of the snapshot."""
        return self._root

    @property
    def maps(self) -> XsdGlobals:
        """The global maps of the snapshot."""
        return self._root.maps

    @property
    def summaries(self) -> dict[tuple[str, str], ResourceSummary]:
        return dict(self._summaries)

    @property
    def edges(self) -> list[DependencyEdge]:
        return list(self._edges)

    def is_valid(self, source: XMLSourceType, **kwargs: Any) -> bool:
        """Validates an XML instance against the snapshot's root schema."""
        return self._root.is_valid(source, **kwargs)

    def validate(self, source: XMLSourceType, **kwargs: Any) -> None:
        """Like :meth:`is_valid` but raises on the first validation error."""
        self._root.validate(source, **kwargs)

    def iter_errors(self, source: XMLSourceType, **kwargs: Any) -> Iterator[Exception]:
        """Yields validation errors found in an XML instance."""
        return self._root.iter_errors(source, **kwargs)

    def resource_identities(self) -> set[tuple[str, str]]:
        return set(self._summaries)

    def __repr__(self) -> str:
        return f"SchemaSnapshot(generation={self.generation}, " \
               f"resources={len(self._summaries)})"


class _OfflineSchemaLoader(SchemaLoader):
    """
    Schema loader used by workspace candidate builds.

    Only the local resources already registered in the workspace are
    resolvable: any other location raises :class:`MissingResourceError`.
    No fallback locations and no remote fetching are used, so a broken
    reference can never be silently repaired from the network.

    A loader class is bound to a workspace through :func:`offline_loader_class`.
    """

    workspace: 'SchemaWorkspace'
    stats: BuildStats

    def load_schema(self, source: Any,
                    namespace: Optional[str] = None,
                    base_url: Optional[str] = None,
                    build: bool = False,
                    partial: bool = False) -> SchemaType:
        schema = self.maps.get_schema(namespace, source, base_url)
        if schema is not None:
            return schema

        url = self._resolve(str(source), base_url)
        data = self.workspace._read_resource(url)
        resource = XMLResource(
            data,
            base_url=self.workspace._dir_url(url),
            allow=self.workspace.allow,
            defuse='never',
        )
        resource.url = url
        resource._url_scheme = urlsplit(url).scheme
        self.stats.loads += 1

        return self.schema_class(
            source=resource,
            namespace=namespace,
            validation=self.maps.validation,
            global_maps=self.maps,
            base_url=base_url,
            build=build,
            partial=partial,
        )

    def _resolve(self, location: Optional[str], base_url: Optional[str]) -> str:
        if location is None:
            raise MissingResourceError('', [base_url or ''])
        url = normalize_url(location, base_url)
        if url not in self.workspace._local_files:
            chain: list[str] = []
            if base_url:
                importer = base_url.rstrip('/')
                if importer in self.workspace._location_index:
                    chain = self.workspace._location_chain(importer)
            raise MissingResourceError(url, chain + [url])
        return url

    def include_schema(self, target_schema: SchemaType,
                       location: str,
                       base_url: Optional[str] = None,
                       build: bool = False,
                       partial: bool = False) -> SchemaType:
        self._resolve(location, base_url)
        return super().include_schema(target_schema, location, base_url, build, partial)

    def import_schema(self, target_schema: SchemaType,
                      namespace: str,
                      location: str,
                      base_url: Optional[str] = None,
                      build: bool = False,
                      partial: bool = False) -> Optional[SchemaType]:
        self._resolve(location, base_url)
        return super().import_schema(
            target_schema, namespace, location, base_url, build, partial
        )


def offline_loader_class(workspace: 'SchemaWorkspace') -> type[SchemaLoader]:
    """Builds an offline loader class bound to a specific workspace."""
    class _BoundOfflineSchemaLoader(_OfflineSchemaLoader):
        def __init__(self, maps: XsdGlobals, *args: Any, **kwargs: Any) -> None:
            super().__init__(maps, *args, **kwargs)
            self.workspace = workspace
            self.stats = workspace._build_stats

    _BoundOfflineSchemaLoader.__name__ = 'BoundOfflineSchemaLoader'
    return _BoundOfflineSchemaLoader


@dataclass(frozen=True)
class _SummaryEntry:
    """Internal record for one resource identity."""
    summary: ResourceSummary
    url: str
    dependencies: tuple[tuple[tuple[str, str], str, str], ...] = ()
    # dependencies: (target identity, kind, literal location)


class SchemaWorkspace:
    """
    Incremental workspace for a graph of local XSD resources.

    :param base_url: the base URL used to normalize relative change locations.
    :param xsd_version: the XSD version used for the build, ``'1.0'`` or ``'1.1'``.
    :param validation: the validation mode used while parsing/building schemas.

    The workspace keeps per-resource summaries (``url``, effective namespace,
    digest) and the include/import/redefine edges between them. Calling
    :meth:`apply` with a set of document changes only reloads and rebuilds the
    resources reachable from the changed documents; branches that do not depend
    on them reuse their already built global components.

    The root document is instantiated fresh for every candidate because it owns
    the candidate global maps; its unchanged dependencies are reused. This
    keeps the per-refresh cost proportional to the changed branch.

    A refresh is transactional: the candidate is fully built and checked before
    it replaces :attr:`snapshot`. When the candidate fails, the previous
    snapshot (and its schemas) remain untouched and the error is raised to the
    caller.
    """

    def __init__(self, base_url: Optional[str] = None,
                 *,
                 xsd_version: str = '1.0',
                 validation: str = 'lax',
                 allow: str = 'local') -> None:

        if xsd_version not in ('1.0', '1.1'):
            raise XMLSchemaValueError(f"unsupported XSD version {xsd_version!r}")
        if validation not in ('strict', 'lax', 'skip'):
            raise XMLSchemaValueError(f"unsupported validation mode {validation!r}")

        self.base_url = base_url
        self.xsd_version = xsd_version
        self.validation = validation
        self.allow = allow

        self._entries: dict[tuple[str, str], _SummaryEntry] = {}
        self._location_index: dict[str, set[tuple[str, str]]] = {}
        self._overlay: dict[str, bytes] = {}
        self._local_files: set[str] = set()
        self._chain: dict[str, str] = {}  # url -> URL of the importer for diagnostics
        self._snapshot: Optional[SchemaSnapshot] = None
        self._generation = 0
        self._schema_class: type[SchemaType] = \
            XMLSchema if xsd_version == '1.0' else XMLSchema11

        self.stats = BuildStats()
        self._build_stats = self.stats
        self._lock = threading.RLock()

    @property
    def snapshot(self) -> Optional[SchemaSnapshot]:
        """The currently published snapshot, ``None`` before the first publish."""
        return self._snapshot

    def __contains__(self, url_or_identity: Any) -> bool:
        try:
            return self._identity_of(url_or_identity) in self._entries
        except KeyError:
            return False

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(resources={len(self._entries)}, " \
               f"generation={self._generation})"

    def _identity_of(self, value: Any) -> tuple[str, str]:
        if isinstance(value, tuple):
            if len(value) != 2:
                raise XMLSchemaValueError(f"invalid resource identity {value!r}")
            return str(value[0]), str(value[1])
        url = normalize_url(str(value), self.base_url)
        identities = self._location_index.get(url, set())
        if not identities:
            raise KeyError(url)
        if len(identities) == 1:
            return next(iter(identities))
        # For a chameleon document with several effective namespaces, prefer
        # the context in which the document declares its own namespace.
        for identity in sorted(identities, key=lambda x: x[1]):
            if self._entries[identity].summary.declared_namespace:
                return identity
        return next(iter(sorted(identities)))

    def summaries(self) -> dict[tuple[str, str], ResourceSummary]:
        """Returns the resource summaries of the current candidate state."""
        return {k: v.summary for k, v in self._entries.items()}

    def dependencies(self, url_or_identity: Any) -> list[DependencyEdge]:
        """Returns the outgoing dependency edges of a resource identity."""
        identity = self._identity_of(url_or_identity)
        entry = self._entries[identity]
        return [
            DependencyEdge(identity, target, kind, location)
            for target, kind, location in entry.dependencies
        ]

    def dependents(self, url_or_identity: Any) -> list[DependencyEdge]:
        """Returns the incoming dependency edges of a resource identity."""
        identity = self._identity_of(url_or_identity)
        result = []
        for source, entry in self._entries.items():
            for target, kind, location in entry.dependencies:
                if target == identity:
                    result.append(DependencyEdge(source, target, kind, location))
        return result

    def _location_chain(self, url: Optional[str]) -> list[str]:
        """Builds the import chain that leads to a location, for diagnostics."""
        if not url:
            return []
        chain = [url]
        seen = {url}
        while (parent := self._chain.get(chain[-1])) is not None:
            if parent in seen:
                break
            chain.append(parent)
            seen.add(parent)
        chain.reverse()
        return chain

    # ------------------------------------------------------------------ #
    # Resource loading and summary scanning
    # ------------------------------------------------------------------ #

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _read_resource(self, url: str) -> bytes:
        """Reads a local resource. Overlay content takes precedence."""
        if url in self._overlay:
            return self._overlay[url]
        if is_remote_url(url):
            raise MissingResourceError(url, self._location_chain(url))
        path = unquote(urlsplit(url).path)
        try:
            with open(path, 'rb') as fp:
                return fp.read()
        except OSError as err:
            raise MissingResourceError(url, self._location_chain(url)) from err

    def _parse_resource(self, url: str,
                        data: bytes) -> tuple[Element, str]:
        """Parses resource bytes and extracts the declared target namespace."""
        try:
            resource = XMLResource(
                data, base_url=self._dir_url(url), allow='none', defuse='never'
            )
        except (XMLResourceError, ElementTreeParseError) as err:
            raise XMLSchemaValueError(
                f"resource {url!r} is not a well-formed XML document: {err}"
            ) from err
        root = resource.root
        if root.tag != nm.XSD_SCHEMA:
            raise XMLSchemaValueError(
                f"resource {url!r} is not an XSD schema document "
                f"(root element is {root.tag!r})"
            )
        declared = root.attrib.get('targetNamespace', '').strip()
        return root, declared

    def _scan(self, root_url: str) -> dict[tuple[str, str], _SummaryEntry]:
        """
        Walks the dependency graph starting from *root_url* and builds a fresh
        set of summary entries. The traversal is done without building any
        component: only the documents are parsed and their declarations read.
        """
        entries: dict[tuple[str, str], _SummaryEntry] = {}
        location_index: dict[str, set[tuple[str, str]]] = {}
        local_files: set[str] = set()
        # Live reference used by _read_resource() to report the importer chain
        # when a resource turns out to be missing during the traversal.
        chain_parents: dict[str, str] = {}
        self._chain = chain_parents

        # Stack items: (url, context_namespace, parent_url)
        stack: list[tuple[str, str, Optional[str]]] = [(root_url, '', None)]
        parsed: dict[str, tuple[Element, str, str]] = {}

        while stack:
            url, effective_ns, parent_url = stack.pop()
            if parent_url is not None:
                chain_parents.setdefault(url, parent_url)

            if url in parsed:
                root, declared_ns, digest = parsed[url]
            else:
                data = self._read_resource(url)
                local_files.add(url)
                root, declared_ns = self._parse_resource(url, data)
                digest = self._digest(data)
                parsed[url] = root, declared_ns, digest

            target_ns = declared_ns or effective_ns
            identity = (url, target_ns)
            if identity in entries:
                continue

            edges: list[tuple[tuple[str, str], str, str]] = []
            for elem in iter_schema_declarations(root):
                kind = elem.tag.split('}')[-1]
                location = elem.get('schemaLocation')

                if kind == 'import':
                    imported_ns = elem.get('namespace', '').strip()
                    if location is None:
                        # An import without schemaLocation can only be resolved
                        # from local mappings; the workspace has none.
                        continue
                    ref_url = normalize_url(location, self._dir_url(url))
                    ref_identity = (ref_url, imported_ns)
                elif location is None:
                    continue
                else:
                    # include/redefine/override share the target namespace,
                    # including the inherited one for chameleon schemas.
                    ref_url = normalize_url(location, self._dir_url(url))
                    ref_identity = (ref_url, target_ns)

                edges.append((ref_identity, kind, location))
                stack.append((ref_url, ref_identity[1], url))

            entries[identity] = _SummaryEntry(
                summary=ResourceSummary(
                    url=url,
                    target_namespace=target_ns,
                    digest=digest,
                    declared_namespace=declared_ns,
                ),
                url=url,
                dependencies=tuple(edges),
            )
            location_index.setdefault(url, set()).add(identity)

        self._entries = entries
        self._location_index = location_index
        self._local_files = local_files
        self._chain = chain_parents
        return entries

    # ------------------------------------------------------------------ #
    # Dependency analysis
    # ------------------------------------------------------------------ #

    def _check_cycles(self, entries: dict[tuple[str, str], _SummaryEntry]) -> None:
        """Detects cycles with a DFS and reports the cycle chain."""
        WHITE, GRAY, BLACK = 0, 1, 2
        colors: dict[tuple[str, str], int] = {k: WHITE for k in entries}
        stack: list[tuple[tuple[str, str], Iterator[tuple[str, str]]]] = []

        for start in entries:
            if colors[start] != WHITE:
                continue
            colors[start] = GRAY
            stack = [(start, self._iter_targets(start, entries))]
            path = [start]
            while stack:
                identity, iterator = stack[-1]
                try:
                    target = next(iterator)
                except StopIteration:
                    colors[identity] = BLACK
                    stack.pop()
                    path.pop()
                    continue

                if target not in entries:
                    # Missing targets are reported separately as resources.
                    continue
                if colors[target] == GRAY:
                    index = path.index(target)
                    cycle = path[index:] + [target]
                    raise DependencyCycleError(
                        [self._chain_label(x) for x in cycle]
                    )
                if colors[target] == WHITE:
                    colors[target] = GRAY
                    path.append(target)
                    stack.append((target, self._iter_targets(target, entries)))

    @staticmethod
    def _iter_targets(identity: tuple[str, str],
                      entries: dict[tuple[str, str], _SummaryEntry]
                      ) -> Iterator[tuple[str, str]]:
        for target, _kind, _location in entries[identity].dependencies:
            yield target

    @staticmethod
    def _chain_label(identity: tuple[str, str]) -> str:
        url, namespace = identity
        return f"{url} [ns={namespace}]" if namespace else url

    def _missing_targets(self, entries: dict[tuple[str, str], _SummaryEntry]
                         ) -> list[tuple[tuple[str, str], str]]:
        missing: list[tuple[tuple[str, str], str]] = []
        for identity, entry in entries.items():
            for target, _kind, location in entry.dependencies:
                if target not in entries:
                    missing.append((identity, location))
        return missing

    def _check_complete(self, entries: dict[tuple[str, str], _SummaryEntry]) -> None:
        """Verifies that every edge target is available locally."""
        for identity, location in self._missing_targets(entries):
            url, _ns = identity
            missing_url = normalize_url(location, self._dir_url(url))
            raise MissingResourceError(
                missing_url,
                self._location_chain(url) + [missing_url],
            )

    @staticmethod
    def _dir_url(url: str) -> str:
        """The URL of the directory containing *url*."""
        return url.rsplit('/', 1)[0] if '/' in url else url

    def _affected_identities(self,
                             changed_urls: set[str],
                             entries: dict[tuple[str, str], _SummaryEntry]
                             ) -> set[tuple[str, str]]:
        """
        Returns the identities of resources that must be rebuilt: the changed
        documents themselves plus every resource that (transitively) depends on
        one of them. An unaffected branch never enters the set.
        """
        changed_identities: set[tuple[str, str]] = set()
        for identity in entries:
            if identity[0] in changed_urls:
                changed_identities.add(identity)

        reverse: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for identity, entry in entries.items():
            for target, _kind, _location in entry.dependencies:
                reverse.setdefault(target, set()).add(identity)

        affected = set(changed_identities)
        queue = list(changed_identities)
        while queue:
            current = queue.pop()
            for dependent in reverse.get(current, ()):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)
        return affected

    # ------------------------------------------------------------------ #
    # Candidate build
    # ------------------------------------------------------------------ #

    def _reuse_components(self,
                          new_maps: XsdGlobals,
                          old_maps: XsdGlobals,
                          reused_identities: set[tuple[str, str]]
                          ) -> set[tuple[str, str]]:
        """
        Reuses the built global components of unchanged resources.

        Every reused schema is shallow copied and re-owned by *new_maps*; the
        components themselves are shared with the previous snapshot. The global
        maps of *new_maps* are populated only with components whose schema
        belongs to a reused resource, so rebuilt resources stay empty until
        their fresh components are built. Returns the resource identities that
        were effectively reused.
        """
        effective_reuse: set[tuple[str, str]] = set()
        reused_original_schemas: set[SchemaType] = set()

        # At this point the candidate maps only own the partial root, whose
        # resource is never part of the reused identities.
        for namespace, schemas in old_maps.namespaces.items():
            for schema in schemas:
                if schema.meta_schema is None or schema.source.url is None:
                    continue
                identity = (schema.source.url, schema.target_namespace)
                if identity not in reused_identities:
                    continue
                schema_copy = copy.copy(schema)
                schema_copy.clear()
                # Register directly: the schema's source object is shared with
                # the previous snapshot, so regular registration would treat it
                # as a duplicate of an otherwise different schema object.
                new_maps._schemas.add(schema_copy)
                new_maps.namespaces[namespace] = schema_copy
                object.__setattr__(schema_copy, 'maps', new_maps)
                reused_original_schemas.add(schema)
                effective_reuse.add(identity)

        # Resolve the schema object each component belongs to by its resource
        # identity. A shared component still points to the previous snapshot's
        # schema object, so identity matching is required to decide whether it
        # belongs to a reused resource (kept) or to a rebuilt one (skipped).
        def is_reused_component(component_schema: SchemaType) -> bool:
            if component_schema.meta_schema is None \
                    or component_schema.source.url is None:
                return True  # built-in/meta component, always inherited
            identity = (
                component_schema.source.url,
                component_schema.target_namespace,
            )
            return identity in effective_reuse

        # Copy built globals. Components owned by rebuilt resources are skipped
        # so that the new maps cannot resolve to a stale component; meta-schema
        # components and components of reused resources are kept.
        for old_map, new_map in zip(old_maps.global_maps, new_maps.global_maps):
            for qname, component in old_map._store.items():
                if not is_reused_component(component.schema):
                    continue
                new_map._store[qname] = component

        # Reused identity constraints stay valid as long as their referents
        # are reused too; rebuilt referents are re-resolved at candidate build.
        for name, identity in old_maps.identities.items():
            if getattr(identity, 'schema', None) in reused_original_schemas:
                new_maps.identities[name] = identity

        return effective_reuse

    def _build_candidate(self,
                         root_url: str,
                         entries: dict[tuple[str, str], _SummaryEntry],
                         rebuilt_identities: set[tuple[str, str]],
                         old_snapshot: Optional[SchemaSnapshot],
                         stats: BuildStats) -> SchemaType:
        """
        Builds the candidate schema using the previous snapshot as a source of
        reusable components. The changed branch is loaded fresh; the other
        namespaces are attached to the candidate's global maps without parsing.
        """
        root_data = self._read_resource(root_url)
        root_resource = XMLResource(
            root_data,
            base_url=self._dir_url(root_url),
            allow=self.allow,
            defuse='never',
        )
        # Keep the base address on the resource: identity and the partial
        # build both rely on source.url.
        root_resource.url = root_url
        root_resource._url_scheme = urlsplit(root_url).scheme
        stats.loads += 1

        # Partial initialization parses and validates the root document but
        # does not follow its import/include statements: the workspace is in
        # charge of which documents get reloaded versus reused.
        candidate = self._schema_class(
            source=root_resource,
            base_url=self.base_url,
            validation=self.validation,
            allow=self.allow,
            defuse='never',
            use_fallback=False,
            loader_class=offline_loader_class(self),
            build=False,
            partial=True,
        )
        candidate.partial = False
        candidate_maps = candidate.maps

        reused_identities_effective: set[tuple[str, str]] = set()
        if old_snapshot is not None:
            old_maps = old_snapshot.maps
            reused_identities_effective = self._reuse_components(
                new_maps=candidate_maps,
                old_maps=old_maps,
                reused_identities={k for k in entries if k not in rebuilt_identities},
            )
        stats.resources_reused = reused_identities_effective
        stats.namespaces_reused = {ns for _url, ns in reused_identities_effective}

        # Load the fresh schema objects of the affected branch. Reused
        # resources are already registered in the candidate maps, so the
        # standard loader does not re-open their documents.
        self._load_affected(
            candidate=candidate,
            rebuilt_identities=rebuilt_identities,
            stats=stats,
        )

        # Process the root's import/include/redefine statements: the partial
        # initialization only recorded them, so link the freshly loaded and
        # reused dependencies to the root schema (imports/includes registries
        # and imported_namespaces). Reused resources are not re-read.
        candidate_maps.loader.load_declared_schemas(candidate)
        self._link_reused_imports(candidate, candidate_maps, root_url)

        self._parse_xsd11_defaults(candidate)
        self._partial_build(candidate_maps, rebuilt_identities, stats)
        return candidate

    def _link_reused_imports(self, candidate: SchemaType,
                             maps: XsdGlobals, root_url: str) -> None:
        """
        Links the reused dependencies of the root schema to its import/include
        registries, because the partial root initialization skips the standard
        declaration processing and the offline loader only follows statements
        of freshly loaded schemas.
        """
        base_url = self._dir_url(root_url)
        for elem in iter_schema_declarations(candidate.source.root):
            location = elem.get('schemaLocation')
            if location is None:
                continue
            url = normalize_url(location, base_url)
            if elem.tag == nm.XSD_IMPORT:
                namespace = elem.get('namespace', '').strip()
                if url not in candidate.imports:
                    schema = maps.get_schema(namespace, url)
                    if schema is not None and schema is not candidate:
                        candidate.imports[url] = schema
            elif url not in candidate.includes:
                namespace = candidate.target_namespace
                schema = maps.get_schema(namespace, url)
                if schema is not None and schema is not candidate:
                    candidate.includes[url] = schema

    def _parse_xsd11_defaults(self, candidate: SchemaType) -> None:
        """
        Parses the XSD 1.1 default declarations that the partial root
        initialization deliberately skipped (it returns before the standard
        post-import block).
        """
        if candidate.XSD_VERSION <= '1.0':
            return
        root = candidate.source.root
        candidate.xpath_default_namespace = parse_xpath_default_namespace(candidate)
        if 'defaultAttributes' in root.attrib:
            try:
                candidate.default_attributes = \
                    candidate.resolve_qname(root.attrib['defaultAttributes'])
            except (ValueError, KeyError, RuntimeError) as err:
                candidate.parse_error(err, root)
        for child in iter_schema_open_content(root):
            candidate.default_open_content = XsdDefaultOpenContent(child, candidate)
            break

    def _load_affected(self,
                      candidate: SchemaType,
                      rebuilt_identities: set[tuple[str, str]],
                      stats: BuildStats) -> None:
        """
        Loads fresh schema objects for every rebuilt resource. Reused resources
        already registered in the candidate maps are skipped, so their documents
        are never re-opened.
        """
        for url, namespace in sorted(rebuilt_identities):
            if url == candidate.url:
                continue
            if candidate.maps.get_schema(namespace, url) is not None:
                continue
            # The offline loader reads overlays/local files and increases
            # stats.loads itself.
            candidate.add_schema(url, namespace=namespace, build=False)

    def _partial_build(self,
                       maps: XsdGlobals,
                       rebuilt_identities: set[tuple[str, str]],
                       stats: BuildStats) -> None:
        """
        Builds only the components of the rebuilt branch and checks the
        candidate. Reused globals stay untouched in the maps, so the build
        cost is proportional to the affected branch only.
        """
        rebuilt_schemas = [
            schema for schema in maps.schemas
            if schema.maps is maps
            and schema.meta_schema is not None
            and schema.source.url is not None
            and (schema.source.url, schema.target_namespace) in rebuilt_identities
        ]
        stats.schemas_rebuilt = {schema.source.url for schema in rebuilt_schemas}

        before = self._built_globals(maps)

        maps.global_maps.load(rebuilt_schemas)
        maps.types.build_builtins(maps.validator)
        maps.global_maps.build(rebuilt_schemas)
        maps.check(rebuilt_schemas)

        self._refresh_substitution_groups(maps, rebuilt_schemas)
        self._check_candidate(maps)

        after = self._built_globals(maps)
        stats.global_components_built = max(0, after - before)
        maps._built = True
        maps.check_validator()

    @staticmethod
    def _built_globals(maps: XsdGlobals) -> int:
        return sum(1 for component in maps.iter_globals() if component.built)

    @staticmethod
    def _check_candidate(maps: XsdGlobals) -> None:
        """
        Raises the first candidate error regardless of the parsing validation
        mode: publication requires every component to build successfully.
        """
        # No staged component may remain: every global must have been built.
        staged = sum(m.total_staged for m in maps.global_maps)
        if staged:
            raise XMLSchemaValueError(
                f"candidate build left {staged} unbuilt global component(s)"
            )

        errors: list[Exception] = []
        for schema in maps.schemas:
            if schema.maps is maps:
                errors.extend(schema.errors)
        errors.extend(maps.all_errors)
        if errors:
            raise errors[0]

    @staticmethod
    def _refresh_substitution_groups(maps: XsdGlobals,
                                     rebuilt_schemas: list[SchemaType]) -> None:
        """
        Recomputes the per-version substitutes sets.

        Only rebuilt head elements are assigned a fresh set, because they
        belong to the candidate and are shared with no other version. Reused
        head components are never mutated; the ``substitution_groups`` map of
        the candidate stays authoritative for their members.
        """
        rebuilt_urls = {s.source.url for s in rebuilt_schemas}
        for head_name, members in list(maps.substitution_groups.items()):
            head = maps.elements.get(head_name)
            if head is None or head.schema.source.url not in rebuilt_urls:
                continue
            head.substitutes = {e.name for e in members}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def publish(self, root: str) -> SchemaSnapshot:
        """
        Scans the graph rooted at *root*, builds the candidate and publishes
        it. The root location is normalized against the workspace base URL.
        """
        root_url = normalize_url(root, self.base_url)
        return self._refresh(root_url, set())

    def apply(self, changes: dict[str, Optional[bytes]],
              root: Optional[str] = None) -> SchemaSnapshot:
        """
        Applies document changes and republishes the workspace.

        :param changes: a mapping of resource locations to new content. A
            ``None`` value drops the in-memory overlay of that location: the
            document is then read from disk again, and becomes a missing
            resource if the file is no longer there. Locations are normalized
            against the workspace base URL.
        :param root: the root location of the graph; required for the first
            publication, defaults to the previous root afterwards.
        :return: the newly published snapshot.
        :raises MissingResourceError: if a referenced resource is unavailable.
        :raises DependencyCycleError: if the dependency graph contains a cycle.
        :raises XMLSchemaParseError: if the candidate fails to build or check.
        """
        with self._lock:
            if root is not None:
                root_url = normalize_url(root, self.base_url)
            elif self._snapshot is not None:
                root_url = self._snapshot.maps.validator.url
            else:
                raise XMLSchemaValueError(
                    "a root location is required for the first publication"
                )

            # Relative change locations are resolved against the directory of
            # the root resource when no workspace base URL is set.
            base_url = self.base_url or self._dir_url(root_url)

            old_overlay = dict(self._overlay)
            overlay: dict[str, bytes] = {}
            removed: set[str] = set()
            for location, content in changes.items():
                url = normalize_url(location, base_url)
                if content is None:
                    self._overlay.pop(url, None)
                    removed.add(url)
                else:
                    self._overlay[url] = content
                    overlay[url] = content

            changed_urls = set(overlay) | removed
            try:
                return self._refresh(root_url, changed_urls)
            except Exception:
                # A failed candidate restores the previous document set.
                self._overlay = old_overlay
                raise

    def _refresh(self, root_url: str, changed_urls: set[str]) -> SchemaSnapshot:
        """
        Builds a candidate from the current document set and publishes it.
        On any failure the previous snapshot stays published and the scanned
        graph indexes are restored.
        """
        old_snapshot = self._snapshot
        old_entries = self._entries
        old_location_index = self._location_index
        old_local_files = self._local_files
        old_chain = self._chain

        entries = self._scan(root_url)
        self._check_cycles(entries)
        self._check_complete(entries)

        stats = BuildStats()
        self._build_stats = stats
        if old_snapshot is None:
            rebuilt_identities = set(entries)
        else:
            rebuilt_identities = self._affected_identities(changed_urls, entries)
            # Resources whose summary changed for other reasons (e.g. a
            # different effective namespace) are always rebuilt.
            for identity, entry in entries.items():
                old_entry = old_entries.get(identity)
                if old_entry is None or old_entry.summary != entry.summary:
                    rebuilt_identities.add(identity)

        # The root document is instantiated fresh for every candidate, so its
        # globals must be built even when nothing in the graph changed.
        rebuilt_identities.add(
            next(identity for identity in entries if identity[0] == root_url)
        )

        try:
            candidate = self._build_candidate(
                root_url=root_url,
                entries=entries,
                rebuilt_identities=rebuilt_identities,
                old_snapshot=old_snapshot,
                stats=stats,
            )
        except Exception:
            # The candidate is discarded; the old snapshot and its indexes
            # stay untouched.
            self._entries = old_entries
            self._location_index = old_location_index
            self._local_files = old_local_files
            self._chain = old_chain
            raise

        self._generation += 1
        snapshot = SchemaSnapshot(
            root=candidate,
            generation=self._generation,
            summaries={k: v.summary for k, v in entries.items()},
            edges=[
                DependencyEdge(identity, target, kind, location)
                for identity, entry in entries.items()
                for target, kind, location in entry.dependencies
            ],
            stats=stats,
        )
        self._snapshot = snapshot
        self.stats = stats
        return snapshot
