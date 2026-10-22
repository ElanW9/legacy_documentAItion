"""Targeted extractor tests on small inline snippets (edge cases)."""

from __future__ import annotations

import textwrap

import pytest

from docgen.extract.package import extract_from_sources, source_hash
from docgen.extract.parser import ParseError, source_from_text
from docgen.models import PackageFacts, Subprogram

SPEC = """\
CREATE OR REPLACE PACKAGE demo AS
  PROCEDURE p1 (a IN NUMBER, b IN OUT NOCOPY t_rec, c OUT VARCHAR2);
  FUNCTION f1 (x IN NUMBER(10,2) DEFAULT 1.5) RETURN tab.col%TYPE DETERMINISTIC;
  PROCEDURE ovl (a IN NUMBER);
  PROCEDURE ovl (a IN VARCHAR2);
END demo;
/
"""


def _body(code: str) -> str:
    return "CREATE OR REPLACE PACKAGE BODY demo AS\n" + textwrap.dedent(code) + "END demo;\n/\n"


def _facts(
    body: str | None, spec: str | None = SPEC, known: set[str] | None = None
) -> PackageFacts:
    return extract_from_sources(
        source_from_text(spec, "demo.pks") if spec else None,
        source_from_text(body, "demo.pkb") if body else None,
        known_packages=known or {"DEMO", "OTHER_PKG"},
    ).facts


def _sub(facts: PackageFacts, name: str) -> Subprogram:
    return next(s for s in facts.subprograms if s.name == name)


def _tables(sub: Subprogram) -> dict[str, list[str]]:
    return {t.table: list(t.operations) for t in sub.tables}


def test_parameters_modes_types_defaults():
    facts = _facts(None)
    p1 = _sub(facts, "P1")
    assert [(p.name, p.mode, p.type) for p in p1.parameters] == [
        ("A", "IN", "NUMBER"),
        ("B", "IN OUT", "T_REC"),
        ("C", "OUT", "VARCHAR2"),
    ]
    f1 = _sub(facts, "F1")
    assert f1.parameters[0].type == "NUMBER(10,2)"
    assert f1.parameters[0].default == "1.5"
    assert f1.return_type == "TAB.COL%TYPE"


def test_overloads_are_kept_separately():
    body = _body(
        """
        PROCEDURE ovl (a IN NUMBER) IS BEGIN NULL; END ovl;
        PROCEDURE ovl (a IN VARCHAR2) IS BEGIN NULL; END ovl;
        """
    )
    ovls = [s for s in _facts(body).subprograms if s.name == "OVL"]
    assert [s.parameters[0].type for s in ovls] == ["NUMBER", "VARCHAR2"]
    assert all(s.visibility == "public" and s.spec_line for s in ovls)


def test_block_matching_with_case_expression_if_loop_and_nested_proc():
    body = _body(
        """
        PROCEDURE p1 (a IN NUMBER, b IN OUT NOCOPY t_rec, c OUT VARCHAR2) IS
          v NUMBER;
          PROCEDURE local_helper IS
          BEGIN
            NULL;
          END local_helper;
        BEGIN
          v := CASE WHEN a > 1 THEN 1 ELSE 0 END;
          IF v = 1 THEN
            FOR r IN (SELECT id FROM t_loop) LOOP
              CASE r.id WHEN 1 THEN local_helper; ELSE NULL; END CASE;
            END LOOP;
          END IF;
          BEGIN
            NULL;
          EXCEPTION WHEN OTHERS THEN NULL;
          END;
        END p1;

        FUNCTION f1 (x IN NUMBER(10,2) DEFAULT 1.5) RETURN tab.col%TYPE DETERMINISTIC IS
        BEGIN
          RETURN x;
        END f1;
        """
    )
    facts = _facts(body)
    p1 = _sub(facts, "P1")
    f1 = _sub(facts, "F1")
    # line 1 is the package header, line 2 the blank line opening the snippet
    assert (p1.start_line, p1.end_line) == (3, 20)
    assert (f1.start_line, f1.end_line) == (22, 25)
    assert _tables(p1) == {"T_LOOP": ["SELECT"]}
    # nested local procedure is part of P1, not a package-level subprogram
    assert "LOCAL_HELPER" not in facts.subprogram_names()
    assert not any("could not find end" in w for w in facts.warnings)


def test_truncated_body_is_warned():
    body = "CREATE OR REPLACE PACKAGE BODY demo AS\n  PROCEDURE p1 IS\n  BEGIN\n    IF x THEN\n"
    facts = _facts(body)
    assert any("could not find end of P1" in w for w in facts.warnings)


def test_table_operations_aliases_and_false_positives():
    body = _body(
        """
        PROCEDURE p1 (a IN NUMBER, b IN OUT NOCOPY t_rec, c OUT VARCHAR2) IS
        BEGIN
          SELECT o.id INTO c FROM app.orders o, (SELECT 1 x FROM dual) d, lines@remote l
           WHERE EXTRACT(YEAR FROM o.dt) = 2020 FOR UPDATE;
          SELECT 1 INTO c FROM hdr h JOIN dtl d ON d.id = h.id LEFT JOIN extra e ON 1 = 1;
          UPDATE orders u SET u.x = 1;
          DELETE FROM old_rows;
          DELETE gone WHERE 1 = 0;
          INSERT INTO audit_t (a) SELECT a FROM src_t;
          MERGE INTO bal b USING delta s ON (b.id = s.id)
            WHEN MATCHED THEN UPDATE SET b.v = s.v DELETE WHERE b.v = 0
            WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id);
          v := 'SELECT * FROM not_a_table'; -- FROM also_not
        END p1;
        """
    )
    tables = _tables(_sub(_facts(body), "P1"))
    assert tables == {
        "APP.ORDERS": ["SELECT"],
        "AUDIT_T": ["INSERT"],
        "BAL": ["MERGE"],
        "DELTA": ["SELECT"],
        "DTL": ["SELECT"],
        "EXTRA": ["SELECT"],
        "GONE": ["DELETE"],
        "HDR": ["SELECT"],
        "LINES": ["SELECT"],
        "OLD_ROWS": ["DELETE"],
        "ORDERS": ["UPDATE"],
        "SRC_T": ["SELECT"],
    }
    aliases = {t.table: t.aliases for t in _sub(_facts(body), "P1").tables}
    assert aliases["APP.ORDERS"] == ["O"]
    assert aliases["ORDERS"] == ["U"]


def test_calls_internal_refs_sequences_raises_dynamic_sql():
    body = _body(
        """
        PROCEDURE helper IS BEGIN NULL; END helper;

        PROCEDURE dead_one IS BEGIN NULL; END dead_one;

        PROCEDURE p1 (a IN NUMBER, b IN OUT NOCOPY t_rec, c OUT VARCHAR2) IS
          v rec_t;
        BEGIN
          helper;
          demo.f1(1);
          other_pkg.do_it(a);
          unknown_pkg.x(1);
          v.field := s_seq.NEXTVAL;
          dbms_output.put_line('helper');
          EXECUTE IMMEDIATE 'DELETE FROM x';
          RAISE_APPLICATION_ERROR(-20001, 'bad ' || a, TRUE);
        END p1;

        FUNCTION f1 (x IN NUMBER(10,2) DEFAULT 1.5) RETURN tab.col%TYPE DETERMINISTIC IS
        BEGIN RETURN x; END f1;
        """
    )
    facts = _facts(body)
    p1 = _sub(facts, "P1")
    assert p1.internal_calls == ["F1", "HELPER"]
    assert [(c.package, c.member, c.kind) for c in p1.calls] == [
        ("DBMS_OUTPUT", "PUT_LINE", "builtin"),
        ("OTHER_PKG", "DO_IT", "repo"),
    ]
    assert p1.sequences == ["S_SEQ"]
    assert p1.dynamic_sql
    assert [(r.code, r.message) for r in p1.raises] == [(-20001, "'bad ' || a")]
    assert _sub(facts, "HELPER").referenced_by == ["P1"]
    assert facts.dependencies == ["DBMS_OUTPUT", "OTHER_PKG"]
    assert any("DEAD_ONE is private and never called" in w for w in facts.warnings)
    assert not any("HELPER is private" in w for w in facts.warnings)
    assert any("P1 uses dynamic SQL" in w for w in facts.warnings)


def test_spec_only_and_missing_body_implementation():
    facts = _facts(None)
    assert any("no package body" in w for w in facts.warnings)
    assert all(s.start_line is None for s in facts.subprograms)

    body = _body("PROCEDURE p1 (a IN NUMBER) IS BEGIN NULL; END;\n")
    facts = _facts(body)
    assert any("F1 is declared in the spec but has no body" in w for w in facts.warnings)


def test_package_globals():
    body = _body(
        """
        c_rate CONSTANT NUMBER := 0.21;
        g_cnt  PLS_INTEGER;
        e_bad  EXCEPTION;
        TYPE t_tab IS TABLE OF NUMBER;
        """
    )
    globals_ = {g.name: g for g in _facts(body).globals}
    assert set(globals_) == {"C_RATE", "G_CNT"}
    assert globals_["C_RATE"].constant and globals_["C_RATE"].value == "0.21"


def test_hash_is_line_ending_independent():
    lf = "CREATE PACKAGE x AS\nEND x;\n"
    a = _facts(None, spec=lf)
    b = _facts(None, spec=lf.replace("\n", "\r\n"))
    assert a.source_hash == b.source_hash
    assert a.source_hash != source_hash(lf + " ", "")


def test_not_a_package_raises():
    with pytest.raises(ParseError):
        _facts(None, spec="CREATE TABLE t (id NUMBER);")
