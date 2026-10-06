"""
Synthesis of non-empty lexical values for XSD simple types.

Only "common" facets are supported: enumerations, numeric bounds
(minInclusive/minExclusive/maxInclusive/maxExclusive), length/minLength/
maxLength, totalDigits/fractionDigits.

A simple type that carries a ``pattern`` or an XSD 1.1 ``assertion`` facet is
considered unsupported unless it also has an enumeration whose values are
already known to satisfy every other facet.
"""
from __future__ import annotations

import base64
import random
from decimal import Decimal
from typing import Any, Optional

import xmlschema.names as nm

from ..validators.facets import XsdFacet
from ..validators.simple_types import XsdSimpleType, XsdAtomic, XsdList, XsdUnion
from .exceptions import (
    NoInstancePossibleError,
    UnsupportedSchemaFeatureError,
)

XSD_NS = 'http://www.w3.org/2001/XMLSchema'

# Fixed lexical constants for date/time and binary types.
_DATE_TIME_CONSTANTS = {
    'dateTime': '2026-01-01T00:00:00',
    'dateTimeStamp': '2026-01-01T00:00:00Z',
    'date': '2026-01-01',
    'time': '00:00:00',
    'gYear': '2026',
    'gYearMonth': '2026-01',
    'gMonth': '--01',
    'gMonthDay': '--01-01',
    'gDay': '---01',
    'duration': 'P1D',
    'yearMonthDuration': 'P1M',
    'dayTimeDuration': 'P1D',
}

_INTEGER_TYPES = frozenset((
    'integer', 'long', 'int', 'short', 'byte',
    'nonPositiveInteger', 'negativeInteger',
    'nonNegativeInteger', 'positiveInteger',
    'unsignedLong', 'unsignedInt', 'unsignedShort', 'unsignedByte',
))

_STRING_TYPES = frozenset((
    'string', 'normalizedString', 'token', 'language', 'NMTOKEN',
    'Name', 'NCName', 'ID', 'IDREF', 'ENTITY',
))


def _is_builtin(st: XsdSimpleType) -> bool:
    from ..validators.simple_types import XsdAtomicBuiltin
    return isinstance(st, XsdAtomicBuiltin)


def has_unsupported_facet(st: XsdSimpleType) -> Optional[str]:
    """
    Returns a human readable reason if *st* uses a facet the value
    synthesizer cannot fulfill, ``None`` otherwise.
    """
    if isinstance(st, XsdList):
        return has_unsupported_facet(st.item_type)
    if isinstance(st, XsdUnion):
        reasons = []
        for member in st.member_types:
            reason = has_unsupported_facet(member)
            if reason is None:
                return None
            reasons.append(reason)
        return '; '.join(reasons)

    # Only facets declared *on this specific type definition* are relevant:
    # built-in types legitimately carry their own patterns (xs:double, xs:ID)
    # and those patterns are inherited through the base-type chain. A user
    # type that itself declares a pattern (without an enumeration) cannot be
    # satisfied by the value synthesizer.
    if _is_builtin(st):
        return None
    own_facets = getattr(st, 'facets', {}) or {}
    pattern_facet = own_facets.get(nm.XSD_PATTERN)
    if pattern_facet is not None and st.enumeration is None:
        patterns = [compiled.pattern for compiled in pattern_facet.patterns]
        return (
            f"simple type {st.sequence_type!r} declares a 'pattern' facet "
            f"({patterns!r}); pattern-based generation is not supported "
            "(provide an enumeration of valid values instead)"
        )
    if nm.XSD_ASSERTION in own_facets:
        return (
            f"simple type {st.sequence_type!r} declares an XSD 1.1 "
            "'assertion' facet, which is not supported by the bounded "
            "instance generator"
        )
    return None


def _facet_value(st: XsdSimpleType, tag: str) -> Any:
    facet = st.get_facet(tag)
    if isinstance(facet, XsdFacet):
        return facet.value
    return None


def _effective_bounds(st: XsdSimpleType) -> tuple[Any, Any, bool, bool]:
    """Return (lower, upper, lower_exclusive, upper_exclusive) from facets."""
    lo = _facet_value(st, nm.XSD_MIN_INCLUSIVE)
    hi = _facet_value(st, nm.XSD_MAX_INCLUSIVE)
    lo_ex = _facet_value(st, nm.XSD_MIN_EXCLUSIVE)
    hi_ex = _facet_value(st, nm.XSD_MAX_EXCLUSIVE)

    lower_exclusive = False
    upper_exclusive = False
    if lo_ex is not None:
        if lo is None or lo_ex > lo:
            lo, lower_exclusive = lo_ex, True
    if hi_ex is not None:
        if hi is None or hi_ex < hi:
            hi, upper_exclusive = hi_ex, True
    return lo, hi, lower_exclusive, upper_exclusive


def _enumeration_value(st: XsdSimpleType, rng: random.Random) -> str:
    values = [v for v in (st.enumeration or []) if v != '']
    if not values:
        if st.enumeration:
            raise NoInstancePossibleError(
                f"enumeration of {st.sequence_type!r} contains only the empty "
                "string, but a non-empty value is required",
                component=st,
            )
        raise NoInstancePossibleError(
            f"empty enumeration for {st.sequence_type!r}", component=st,
        )
    shortest = min(len(str(v)) for v in values)
    candidates = [str(v) for v in values if len(str(v)) == shortest]
    return rng.choice(candidates)


def _length_target(st: XsdSimpleType, default: int = 1) -> int:
    length = _facet_value(st, nm.XSD_LENGTH)
    if length is not None:
        return max(int(length), 1)
    min_length = st.min_length or 0
    if min_length >= 1:
        return int(min_length)
    max_length = st.max_length
    if max_length is not None and max_length < default:
        return max(int(max_length), 1)
    return default


def _integer_value(st: XsdSimpleType, local_name: str,
                   rng: random.Random) -> str:
    lo, hi, lo_ex, hi_ex = _effective_bounds(st)

    builtin_lo = -2 ** 63
    builtin_hi = 2 ** 63 - 1
    if local_name in ('nonNegativeInteger', 'unsignedLong'):
        builtin_lo = 0
    elif local_name in ('positiveInteger',):
        builtin_lo = 1
    elif local_name in ('negativeInteger',):
        builtin_hi = -1
    elif local_name == 'nonPositiveInteger':
        builtin_hi = 0
    elif local_name == 'int' or local_name == 'unsignedInt':
        builtin_lo = -2 ** 31 if local_name == 'int' else 0
        builtin_hi = 2 ** 31 - 1 if local_name == 'int' else 2 ** 32 - 1
    elif local_name == 'short' or local_name == 'unsignedShort':
        builtin_lo = -2 ** 15 if local_name == 'short' else 0
        builtin_hi = 2 ** 15 - 1 if local_name == 'short' else 2 ** 16 - 1
    elif local_name == 'byte' or local_name == 'unsignedByte':
        builtin_lo = -2 ** 7 if local_name == 'byte' else 0
        builtin_hi = 2 ** 7 - 1 if local_name == 'byte' else 2 ** 8 - 1
    elif local_name == 'long' or local_name == 'unsignedLong':
        builtin_lo = -2 ** 63 if local_name == 'long' else 0
        builtin_hi = 2 ** 63 - 1 if local_name == 'long' else 2 ** 64 - 1

    lower = builtin_lo if lo is None else int(lo)
    upper = builtin_hi if hi is None else int(hi)
    if lo_ex:
        lower = max(lower, int(lo) + 1)
    if hi_ex:
        upper = min(upper, int(hi) - 1)

    if lower > upper:
        raise NoInstancePossibleError(
            f"no integer value satisfies bounds [{lower}, {upper}] for "
            f"{st.sequence_type!r}",
            component=st,
        )

    # Prefer the smallest magnitude value to keep the lexical form short.
    candidate = lower if lower >= 0 or upper < 0 else 0
    if not lower <= candidate <= upper:
        candidate = lower
    return str(candidate)


def _decimal_value(st: XsdSimpleType) -> str:
    lo, hi, lo_ex, hi_ex = _effective_bounds(st)
    total_digits = _facet_value(st, nm.XSD_TOTAL_DIGITS)
    fraction_digits = _facet_value(st, nm.XSD_FRACTION_DIGITS)

    lower = None if lo is None else Decimal(lo)
    upper = None if hi is None else Decimal(hi)
    if lo_ex and lower is not None:
        lower = lower + Decimal(1)
    if hi_ex and upper is not None:
        upper = upper - Decimal(1)

    candidate: Decimal
    if lower is not None and lower > 0:
        candidate = lower
    elif upper is not None and upper < 0:
        candidate = upper
    else:
        candidate = Decimal(0)

    if fraction_digits:
        step = Decimal(1).scaleb(-int(fraction_digits))
        if lower is not None and candidate < lower:
            candidate = (lower / step).to_integral_value(rounding='ROUND_CEILING') * step
        text = f"{candidate:.{int(fraction_digits)}f}"
    else:
        text = str(candidate)

    if total_digits is not None:
        digits = text.replace('-', '').replace('.', '').lstrip('0')
        if len(digits) > int(total_digits):
            raise NoInstancePossibleError(
                f"bounds require more than totalDigits={total_digits} for "
                f"{st.sequence_type!r}",
                component=st,
            )
    return text


def _float_value(st: XsdSimpleType, local_name: str) -> str:
    lo, hi, lo_ex, hi_ex = _effective_bounds(st)
    min_positive = 1e-38 if local_name == 'float' else 1e-308

    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    lower = _to_float(lo, -float('inf'))
    upper = _to_float(hi, float('inf'))
    if lo_ex:
        lower = max(lower, _to_float(lo, -float('inf')) + min_positive)
    if hi_ex:
        upper = min(upper, _to_float(hi, float('inf')) - min_positive)

    if lower > upper:
        raise NoInstancePossibleError(
            f"no {local_name} value satisfies the numeric bounds of "
            f"{st.sequence_type!r}",
            component=st,
        )
    if lower <= 0.0 <= upper:
        return '0'
    if lower > 0:
        return str(max(lower, 0.0) or min_positive)
    return str(min(upper, -0.0))


def _string_value(st: XsdSimpleType, local_name: str,
                  rng: random.Random) -> str:
    length = _length_target(st, default=1)

    if local_name in ('ID', 'IDREF', 'NCName', 'Name', 'ENTITY'):
        return _unique_name(local_name, rng, min_length=length)

    if local_name == 'NMTOKEN':
        token = f"tok_{rng.getrandbits(32):08x}"
    elif local_name == 'language':
        return 'en' if length <= 2 else ('en-US' if length <= 5 else 'en-US-x'[:length])
    elif local_name == 'normalizedString':
        token = 'x' * length
    else:
        token = 'x' * length

    if len(token) > length and st.max_length is not None:
        token = token[:max(int(st.max_length), 1)]
    return token


def _unique_name(local_name: str, rng: random.Random,
                  min_length: int = 1) -> str:
    prefix = {
        'ID': 'id_', 'IDREF': 'idref_', 'NCName': 'n_', 'Name': 'name_',
        'ENTITY': 'ent_',
    }.get(local_name, 'n_')
    token = f"{prefix}{rng.getrandbits(64):016x}"
    if min_length <= len(token):
        return token
    return token + ('x' * (min_length - len(token)))



def _family_names(st: XsdAtomic) -> list[str]:
    """Local names from the concrete built-in type up to its primitive."""
    names: list[str] = []
    current = st
    seen = 0
    while isinstance(current, XsdAtomic) and seen < 40:
        name = current.local_name
        if name:
            names.append(name)
        base = getattr(current, 'base_type', None)
        if not isinstance(base, XsdAtomic) or base is current:
            break
        current = base
        seen += 1
    return names


def family_of(st: XsdSimpleType) -> str:
    """Coarsest lexical family name of a (possibly derived) simple type."""
    if isinstance(st, (XsdList, XsdUnion)):
        return 'string'
    names = _family_names(st)  # type: ignore[arg-type]
    for name in names:
        if name in _STRING_TYPES or name in _INTEGER_TYPES or \
                name in ('decimal', 'float', 'double', 'boolean', 'anyURI',
                         'hexBinary', 'base64Binary', 'QName', 'NOTATION') \
                or name in _DATE_TIME_CONSTANTS:
            return name
    return getattr(st.primitive_type, 'local_name', 'string')


def synthesize_value(st: XsdSimpleType, rng: random.Random,
                     *, purpose: str = '') -> str:
    """
    Produce a non-empty lexical value valid for the given simple type.

    :param st: a built-in or derived XSD simple type.
    :param rng: seeded random source used for tie breaking.
    :param purpose: optional description of what the value is used for
        (included in error messages).
    :raises UnsupportedSchemaFeatureError: for patterns/assertions without
        enumeration, xs:QName/xs:NOTATION without enumeration.
    :raises NoInstancePossibleError: when bounds/enumerations admit no value.
    """
    suffix = f" for {purpose}" if purpose else ''

    if st.enumeration is not None:
        return _enumeration_value(st, rng)

    reason = has_unsupported_facet(st)
    if reason is not None:
        raise UnsupportedSchemaFeatureError(reason, component=st)

    if isinstance(st, XsdUnion):
        for member in st.member_types:
            try:
                return synthesize_value(member, rng, purpose=purpose)
            except (NoInstancePossibleError, UnsupportedSchemaFeatureError):
                continue
        raise NoInstancePossibleError(
            f"none of the union member types admits a value{suffix}",
            component=st,
        )

    if isinstance(st, XsdList):
        count = _length_target(st, default=1)
        items = [
            synthesize_value(st.item_type, rng, purpose=f"list item{suffix}")
            for _ in range(count)
        ]
        return ' '.join(items)

    # A restriction of a list (length facets on a list) is represented as
    # XsdAtomicRestriction whose primitive_type is the XsdList itself.
    list_primitive = getattr(st, 'primitive_type', None)
    if isinstance(list_primitive, XsdList):
        count = _length_target(st, default=1)
        items = [
            synthesize_value(list_primitive.item_type, rng,
                             purpose=f"list item{suffix}")
            for _ in range(count)
        ]
        return ' '.join(items)

    if not isinstance(st, XsdAtomic):
        raise UnsupportedSchemaFeatureError(
            f"unsupported simple type variety {st.variety!r}{suffix}",
            component=st,
        )

    # Walk the type chain so restrictions of xs:ID/xs:int/... keep the
    # lexical family of their built-in ancestor.
    primitive = st.primitive_type
    primitive_name = primitive.local_name
    family = _family_names(st)
    local_name = primitive_name
    for candidate in family:
        local_name = candidate
        break
    family_set = set(family)

    integer_family = next((n for n in family if n in _INTEGER_TYPES), None)
    if integer_family is not None:
        return _integer_value(st, integer_family, rng)
    if primitive_name == 'decimal' or 'decimal' in family_set:
        return _decimal_value(st)
    if primitive_name in ('float', 'double'):
        return _float_value(st, primitive_name)
    date_family = next((n for n in family if n in _DATE_TIME_CONSTANTS), None)
    if date_family is not None:
        return _DATE_TIME_CONSTANTS[date_family]
    if 'hexBinary' in family_set:
        return '0' * (2 * _length_target(st, default=1))
    if 'base64Binary' in family_set:
        length = _length_target(st, default=1)
        return base64.b64encode(b'\x00' * length).decode('ascii')
    if 'anyURI' in family_set:
        return 'http://example.org/'
    if family_set & {'QName', 'NOTATION'}:
        raise UnsupportedSchemaFeatureError(
            "values of xs:QName/xs:NOTATION cannot be synthesized without an "
            f"enumeration{suffix}; declare one or provide a fixed value",
            component=st,
        )
    if primitive_name == 'boolean':
        return 'true'
    string_family = next((n for n in family if n in _STRING_TYPES), None)
    if string_family is not None or primitive_name in _STRING_TYPES:
        return _string_value(st, string_family or primitive_name, rng)
    if primitive_name in ('anyAtomicType', 'anySimpleType'):
        return 'x' * _length_target(st, default=1)

    raise UnsupportedSchemaFeatureError(
        f"value synthesis is not supported for xs:{primitive_name}{suffix}",
        component=st,
    )
