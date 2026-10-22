CREATE OR REPLACE PACKAGE BODY cust_mgmt AS

  c_src CONSTANT VARCHAR2(30) := 'CUST_MGMT';

  FUNCTION get_cst_lvl (p_cst_id IN NUMBER) RETURN VARCHAR2 IS
    v_tot NUMBER;
    v_yrs NUMBER;
  BEGIN
    SELECT NVL(SUM(h.inv_amt), 0),
           TRUNC(MONTHS_BETWEEN(SYSDATE, MIN(c.crt_dt)) / 12)
      INTO v_tot, v_yrs
      FROM cst_mstr c
      JOIN cst_hist h ON h.cst_id = c.cst_id
     WHERE c.cst_id = p_cst_id
       AND h.inv_dt >= ADD_MONTHS(SYSDATE, -12);

    IF v_tot >= 25000 OR (v_tot >= 10000 AND v_yrs >= 5) THEN
      RETURN 'G';
    ELSIF v_tot >= 5000 THEN
      RETURN 'S';
    END IF;
    RETURN 'B';
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RETURN 'B';
  END get_cst_lvl;

  PROCEDURE upd_cst_stat (
    p_cst_id IN NUMBER,
    p_new_cd IN VARCHAR2,
    p_usr    IN VARCHAR2 DEFAULT USER
  ) IS
    v_old_cd cst_mstr.stat_cd%TYPE;
  BEGIN
    SELECT stat_cd
      INTO v_old_cd
      FROM cst_mstr
     WHERE cst_id = p_cst_id
       FOR UPDATE;

    IF v_old_cd = 'X' THEN
      RAISE_APPLICATION_ERROR(-20031, 'Customer ' || p_cst_id || ' is closed');
    END IF;

    UPDATE cst_mstr m
       SET m.stat_cd = p_new_cd,
           m.upd_dt  = SYSDATE,
           m.upd_usr = p_usr
     WHERE m.cst_id = p_cst_id;

    INSERT INTO cst_stat_hist (cst_id, old_cd, new_cd, chg_dt, chg_usr)
    VALUES (p_cst_id, v_old_cd, p_new_cd, SYSDATE, p_usr);

    util_log.log_msg('I', c_src, 'stat ' || v_old_cd || '->' || p_new_cd || ' cst ' || p_cst_id);
  END upd_cst_stat;

  PROCEDURE chk_cst_xfl (
    p_cst_id IN  NUMBER,
    p_xfl    OUT VARCHAR2
  ) IS
    v_typ  cst_mstr.typ_cd%TYPE;
    v_ctry cst_mstr.ctry_cd%TYPE;
    v_vno  cst_mstr.vat_no%TYPE;
  BEGIN
    SELECT typ_cd, ctry_cd, vat_no
      INTO v_typ, v_ctry, v_vno
      FROM cst_mstr
     WHERE cst_id = p_cst_id;

    IF v_typ = 'K' AND v_ctry <> 'BE' AND v_vno IS NOT NULL THEN
      p_xfl := 'Y';
    ELSIF v_typ = 'K' AND v_ctry <> 'BE' THEN
      p_xfl := 'P';
    ELSE
      p_xfl := 'N';
    END IF;
  END chk_cst_xfl;

  PROCEDURE rebuild_cst_idx IS
  BEGIN
    DELETE FROM cst_idx;
    INSERT INTO cst_idx (cst_id, srch_key)
      SELECT cst_id, UPPER(TRIM(nm)) || '#' || zip_cd
        FROM cst_mstr
       WHERE stat_cd <> 'X';
    COMMIT;
  END rebuild_cst_idx;

END cust_mgmt;
/
