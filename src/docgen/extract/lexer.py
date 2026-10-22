"""Comment/string masking and tokenisation for PL/SQL.

The masker replaces the *content* of comments and string literals with spaces
while keeping every newline, so offsets and line numbers in the masked text are
identical to the original. All structural matching runs on masked text; the
original text is only sliced (by offset) when we need a verbatim fragment such
as a parameter type or an error message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


def _blank(text: str) -> str:
    """Replace every character except newlines by a space."""
    return re.sub(r"[^\n]", " ", text)


def mask_source(src: str) -> str:
    """Return `src` with comments and string-literal contents blanked out.

    Handles `-- line` comments, `/* block */` comments, `'...'` literals with
    doubled-quote escapes, and Oracle alternative quoting `q'[...]'` (also with
    N/n prefix). Quote delimiters are kept so the result still tokenises as a
    string literal. Unterminated constructs are blanked to end of input.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            end = src.find("\n", i)
            end = n if end == -1 else end
            out.append(_blank(src[i:end]))
            i = end
            continue

        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(_blank(src[i:end]))
            i = end
            continue

        # q'<delim>...<delim>' alternative quoting, optionally N-prefixed.
        m = re.match(r"[nN]?[qQ]'(.)", src[i : i + 4])
        if m and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] in "_$#")):
            opener = m.group(1)
            closer = _Q_CLOSERS.get(opener, opener)
            body_start = i + m.end()
            end = src.find(closer + "'", body_start)
            end = n if end == -1 else end + 2
            prefix = src[i:body_start]
            out.append(prefix + _blank(src[body_start : end - 2]) + src[end - 2 : end])
            i = end
            continue

        if ch == "'":
            j = i + 1
            while j < n:
                if src[j] == "'":
                    if j + 1 < n and src[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            out.append("'" + _blank(src[i + 1 : end - 1]) + ("'" if end - 1 > i else ""))
            i = end
            continue

        out.append(ch)
        i += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # "ident" | "number" | "string" | "op"
    text: str  # original spelling
    upper: str  # upper-cased spelling, for keyword comparisons
    start: int  # offset into the source
    end: int
    line: int  # 1-based


_TOKEN_RE = re.compile(
    r"""
    (?P<string>[nN]?[qQ]?'(?:[^']|'')*')
  | (?P<ident>[A-Za-z][A-Za-z0-9_$\#]*)
  | (?P<qident>"[^"\n]+")
  | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<op>:=|=>|\|\||<>|!=|\^=|<=|>=|\.\.|[%(),;.=<>+\-*/:@])
    """,
    re.VERBOSE,
)


def tokenize(masked: str) -> list[Token]:
    """Tokenise masked source. Unknown characters are skipped."""
    tokens: list[Token] = []
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(masked):
        line += masked.count("\n", pos, m.start())
        pos = m.start()
        kind = m.lastgroup or "op"
        text = m.group()
        if kind == "qident":
            kind = "ident"
            upper = text.strip('"')
        else:
            upper = text.upper()
        tokens.append(Token(kind, text, upper, m.start(), m.end(), line))
    return tokens
