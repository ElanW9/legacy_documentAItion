from docgen.extract.lexer import mask_source, tokenize


def test_mask_preserves_length_and_newlines():
    src = "a := 1; -- comment\n/* multi\nline */ b := 'x\n y';\n"
    masked = mask_source(src)
    assert len(masked) == len(src)
    assert [i for i, c in enumerate(src) if c == "\n"] == [
        i for i, c in enumerate(masked) if c == "\n"
    ]


def test_mask_removes_comment_and_string_content():
    src = "SELECT x FROM t1; -- FROM t2\n/* FROM t3 */ v := 'FROM t4';"
    masked = mask_source(src)
    assert "t2" not in masked and "t3" not in masked and "t4" not in masked
    assert "FROM t1" in masked


def test_double_dash_inside_string_is_not_a_comment():
    masked = mask_source("log('a -- b'); x := 1;")
    assert "x := 1;" in masked


def test_doubled_quote_escape():
    masked = mask_source("v := 'it''s FROM t'; y := 2;")
    assert "FROM" not in masked and "y := 2;" in masked


def test_q_quote_with_bracket_delimiter():
    masked = mask_source("v := q'[it's FROM t]'; z := 3;")
    assert "FROM" not in masked and "z := 3;" in masked
    assert [t.kind for t in tokenize(masked)][:3] == ["ident", "op", "string"]


def test_unterminated_block_comment_is_masked_to_end():
    masked = mask_source("a := 1; /* never closed FROM t")
    assert "FROM" not in masked


def test_token_lines():
    toks = tokenize(mask_source("a\n-- c\nb /* x\n y */ c"))
    assert [(t.text, t.line) for t in toks] == [("a", 1), ("b", 3), ("c", 4)]
