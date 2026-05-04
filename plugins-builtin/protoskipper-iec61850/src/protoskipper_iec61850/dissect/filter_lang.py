# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850 PCAP filter language — P8.H.2.

Provides a simple expression language for filtering decoded PCAP records.

Syntax
------
::

    expr        ::= conjunction ( "or" conjunction )*
    conjunction ::= term ( "and" term )*
    term        ::= "not" term
                  | "(" expr ")"
                  | field op value

    field       ::= PROTO "." ATTR
    PROTO       ::= "goose" | "sv" | "mms"
    ATTR        ::= identifier

    op          ::= "==" | "!=" | "<" | "<=" | ">" | ">=" | "~="
    value       ::= quoted_string | integer

    quoted_string ::= '"' ... '"'
    integer       ::= ["-"] DIGIT+

Operator ``~=`` is a regex-match operator (case-sensitive, search not full
match).

Examples
--------
::

    goose.gocbref == "simpleIO/LLN0$GO$gcbAnalogValues"
    mms.invokeID > 10 and mms.service == "Read"
    sv.svid ~= "^MU01"
    not goose.test == true

Attribute names (case-insensitive)
------------------------------------
GOOSE fields:   gocbref, goid, dataset, stnum, sqnum, confrev, timeallowed,
                test, ndscomm, apid
SV fields:      svid, apid, noasdu, smprate
MMS fields:     pdutype, service, invokeid

The :func:`compile_filter` function returns a :class:`FilterExpression` that
can be evaluated against a :class:`~protoskipper_iec61850.dissect.pcap.PcapRecord`
by first passing the record's ``data`` through the appropriate dissectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FilterSyntaxError(ValueError):
    """Raised when a filter expression cannot be parsed."""


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------


@dataclass
class _Token:
    kind: str  # FIELD, OP, INT, STR, LPAREN, RPAREN, AND, OR, NOT, EOF
    value: str | int
    pos: int


_OP_RE = re.compile(r"==|!=|<=|>=|<|>|~=")
_INT_RE = re.compile(r"-?\d+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def _tokenise(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] == "(":
            tokens.append(_Token("LPAREN", "(", i))
            i += 1
        elif text[i] == ")":
            tokens.append(_Token("RPAREN", ")", i))
            i += 1
        elif text[i] == '"':
            j = text.index('"', i + 1)
            tokens.append(_Token("STR", text[i + 1 : j], i))
            i = j + 1
        else:
            m = _OP_RE.match(text, i)
            if m:
                tokens.append(_Token("OP", m.group(), i))
                i = m.end()
                continue
            m = _IDENT_RE.match(text, i)
            if m:
                word = m.group()
                lower = word.lower()
                if lower == "and":
                    tokens.append(_Token("AND", "and", i))
                elif lower == "or":
                    tokens.append(_Token("OR", "or", i))
                elif lower == "not":
                    tokens.append(_Token("NOT", "not", i))
                elif "." in word:
                    tokens.append(_Token("FIELD", word, i))
                else:
                    # bare identifier / keyword used as value (e.g. true/false)
                    tokens.append(_Token("STR", word, i))
                i = m.end()
                continue
            m = _INT_RE.match(text, i)
            if m:
                tokens.append(_Token("INT", int(m.group()), i))
                i = m.end()
                continue
            msg = f"Unexpected character {text[i]!r} at position {i}"
            raise FilterSyntaxError(msg)
    tokens.append(_Token("EOF", "", n))
    return tokens


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


@dataclass
class _CompareNode:
    field_proto: str
    field_attr: str
    op: str
    value: str | int


@dataclass
class _AndNode:
    left: Any
    right: Any


@dataclass
class _OrNode:
    left: Any
    right: Any


@dataclass
class _NotNode:
    operand: Any


# ---------------------------------------------------------------------------
# Parser (recursive descent)
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _consume(self, kind: str | None = None) -> _Token:
        tok = self._tokens[self._pos]
        if kind and tok.kind != kind:
            msg = f"Expected {kind} but got {tok.kind!r} ({tok.value!r}) at pos {tok.pos}"
            raise FilterSyntaxError(msg)
        self._pos += 1
        return tok

    def parse(self) -> Any:
        node = self._expr()
        self._consume("EOF")
        return node

    def _expr(self) -> Any:
        node = self._conjunction()
        while self._peek().kind == "OR":
            self._consume("OR")
            right = self._conjunction()
            node = _OrNode(node, right)
        return node

    def _conjunction(self) -> Any:
        node = self._term()
        while self._peek().kind == "AND":
            self._consume("AND")
            right = self._term()
            node = _AndNode(node, right)
        return node

    def _term(self) -> Any:
        tok = self._peek()
        if tok.kind == "NOT":
            self._consume("NOT")
            operand = self._term()
            return _NotNode(operand)
        if tok.kind == "LPAREN":
            self._consume("LPAREN")
            node = self._expr()
            self._consume("RPAREN")
            return node
        # comparison: FIELD OP (STR | INT)
        field_tok = self._consume("FIELD")
        field_str = str(field_tok.value)
        if "." not in field_str:
            msg = f"Expected PROTO.ATTR, got {field_str!r}"
            raise FilterSyntaxError(msg)
        proto, attr = field_str.split(".", 1)
        op_tok = self._consume("OP")
        val_tok = self._peek()
        if val_tok.kind in ("STR", "INT"):
            self._consume()
            value: str | int = val_tok.value  # type: ignore[assignment]
        else:
            msg = f"Expected value, got {val_tok.kind!r}"
            raise FilterSyntaxError(msg)
        return _CompareNode(
            field_proto=proto.lower(),
            field_attr=attr.lower(),
            op=str(op_tok.value),
            value=value,
        )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _get_field(dissections: dict[str, Any], proto: str, attr: str) -> Any:
    """Extract a field value from dissections dict keyed by protocol name."""
    disc = dissections.get(proto)
    if disc is None:
        return None
    # Normalise attribute names
    attr_map: dict[str, str] = {
        # GOOSE
        "gocbref": "go_cb_ref",
        "goid": "go_id",
        "dataset": "dat_set",
        "stnum": "st_num",
        "sqnum": "sq_num",
        "confrev": "conf_rev",
        "timeallowed": "time_allowed_ms",
        "test": "test",
        "ndscomm": "nds_comm",
        "apid": "app_id",
        # SV
        "svid": "sv_id",
        "noasdu": "no_asdu",
        "smprate": "smp_rate",
        # MMS
        "pdutype": "pdu_type",
        "service": "service_name",
        "invokeid": "invoke_id",
    }
    resolved = attr_map.get(attr, attr)
    return getattr(disc, resolved, None)


def _compare(left: Any, op: str, right: str | int) -> bool:
    if left is None:
        return False
    if op == "~=":
        try:
            return bool(re.search(str(right), str(left)))
        except re.error:
            return False
    # Coerce types
    if isinstance(right, str):
        # try boolean coercion
        if right.lower() in ("true", "false"):
            right_val: Any = right.lower() == "true"
        else:
            right_val = right
    else:
        right_val = right
    try:
        if op == "==":
            return bool(left == right_val)
        if op == "!=":
            return bool(left != right_val)
        if op == "<":
            return bool(left < right_val)
        if op == "<=":
            return bool(left <= right_val)
        if op == ">":
            return bool(left > right_val)
        if op == ">=":
            return bool(left >= right_val)
    except TypeError:
        return False
    return False


def _eval_node(node: Any, dissections: dict[str, Any]) -> bool:
    if isinstance(node, _CompareNode):
        val = _get_field(dissections, node.field_proto, node.field_attr)
        return _compare(val, node.op, node.value)
    if isinstance(node, _AndNode):
        return _eval_node(node.left, dissections) and _eval_node(node.right, dissections)
    if isinstance(node, _OrNode):
        return _eval_node(node.left, dissections) or _eval_node(node.right, dissections)
    if isinstance(node, _NotNode):
        return not _eval_node(node.operand, dissections)
    msg = f"Unknown AST node: {type(node)}"
    raise TypeError(msg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class FilterExpression:
    """A compiled filter expression.

    Call :meth:`match` with a dissections dict to evaluate.

    The dissections dict maps protocol names to dissection objects::

        {
            "goose": GooseDissection | None,
            "sv": SvDissection | None,
            "mms": MmsDissection | None,
        }

    Attributes
    ----------
    source:
        The original filter text.
    """

    source: str
    _ast: Any = field(repr=False)

    def match(self, dissections: dict[str, Any]) -> bool:
        """Return ``True`` if *dissections* satisfies this filter."""
        return _eval_node(self._ast, dissections)


def compile_filter(expression: str) -> FilterExpression:
    """Parse and compile a filter expression string.

    Parameters
    ----------
    expression:
        Filter text (see module docstring for syntax).

    Returns
    -------
    :class:`FilterExpression`

    Raises
    ------
    :class:`FilterSyntaxError`
        If the expression cannot be parsed.
    """
    tokens = _tokenise(expression.strip())
    parser = _Parser(tokens)
    ast = parser.parse()
    return FilterExpression(source=expression, _ast=ast)
