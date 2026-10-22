CREATE OR REPLACE PACKAGE BODY ord_proc AS

  c_src           CONSTANT VARCHAR2(30) := 'ORD_PROC';
  c_allow_partial CONSTANT BOOLEAN      := FALSE;

  FUNCTION calc_disc_pct (p_ord_id IN NUMBER) RETURN NUMBER IS
    v_cst_id ord_hdr.cst_id%TYPE;
    v_chan   ord_hdr.chan_cd%TYPE;
    v_tot    NUMBER;
    v_lvl    VARCHAR2(1);
    v_pct    NUMBER;
  BEGIN
    SELECT h.cst_id, h.chan_cd, SUM(l.qty * l.unit_prc)
      INTO v_cst_id, v_chan, v_tot
      FROM ord_hdr h, ord_line l
     WHERE l.ord_id = h.ord_id
       AND h.ord_id = p_ord_id
     GROUP BY h.cst_id, h.chan_cd;

    v_lvl := cust_mgmt.get_cst_lvl(v_cst_id);

    v_pct :=
      CASE
        WHEN v_lvl = 'G' AND v_tot >= 5000                       THEN 12
        WHEN v_lvl = 'G'                                         THEN 8
        WHEN v_lvl = 'S' AND v_chan = 'W'
             AND TO_CHAR(SYSDATE, 'MM') = '12'                   THEN 5
        WHEN v_lvl = 'S'                                         THEN 3
        WHEN v_chan = 'W' AND v_tot >= 2500                      THEN 2
        ELSE 0
      END;

    RETURN v_pct;
  END calc_disc_pct;

  PROCEDURE validate_order (p_ord_id IN NUMBER) IS
    v_cnt  PLS_INTEGER;
    v_stat ord_hdr.stat_cd%TYPE;
    v_xfl  VARCHAR2(1);
    v_cst  ord_hdr.cst_id%TYPE;
  BEGIN
    SELECT stat_cd, cst_id
      INTO v_stat, v_cst
      FROM ord_hdr
     WHERE ord_id = p_ord_id;

    IF v_stat <> 'N' THEN
      RAISE_APPLICATION_ERROR(-20101, 'Order ' || p_ord_id || ' not in status N');
    END IF;

    SELECT COUNT(*)
      INTO v_cnt
      FROM ord_line
     WHERE ord_id = p_ord_id;

    IF v_cnt = 0 THEN
      RAISE_APPLICATION_ERROR(-20102, 'Order ' || p_ord_id || ' has no lines');
    ELSIF v_cnt > g_max_lines THEN
      RAISE_APPLICATION_ERROR(-20103, 'Order ' || p_ord_id || ' has too many lines');
    END IF;

    cust_mgmt.chk_cst_xfl(v_cst, v_xfl);
    IF v_xfl = 'P' THEN
      RAISE_APPLICATION_ERROR(-20104, 'Customer ' || v_cst || ' xfl=P');
    END IF;
  END validate_order;

  PROCEDURE submit_order (p_ord_id IN NUMBER) IS
    v_pct    NUMBER;
    v_inv_id NUMBER;
  BEGIN
    validate_order(p_ord_id);

    v_pct := calc_disc_pct(p_ord_id);

    UPDATE ord_line
       SET disc_pct = v_pct
     WHERE ord_id = p_ord_id;

    UPDATE ord_hdr
       SET stat_cd = 'S',
           sub_dt  = SYSDATE
     WHERE ord_id = p_ord_id;

    BEGIN
      bill_calc.create_invoice(p_ord_id, v_inv_id);
      UPDATE ord_hdr
         SET inv_id = v_inv_id,
             stat_cd = 'I'
       WHERE ord_id = p_ord_id;
    EXCEPTION
      WHEN OTHERS THEN
        NULL;
    END;

    util_log.log_msg('I', c_src, 'submitted ' || p_ord_id || ' -- disc ' || v_pct);
  END submit_order;

  PROCEDURE cancel_order (
    p_ord_id IN NUMBER,
    p_rsn_cd IN VARCHAR2
  ) IS
    v_stat ord_hdr.stat_cd%TYPE;
  BEGIN
    SELECT stat_cd
      INTO v_stat
      FROM ord_hdr
     WHERE ord_id = p_ord_id
       FOR UPDATE;

    IF v_stat IN ('N', 'S') THEN
      UPDATE ord_hdr
         SET stat_cd = 'C',
             rsn_cd  = p_rsn_cd
       WHERE ord_id = p_ord_id;
    ELSIF v_stat = 'I' AND c_allow_partial THEN
      UPDATE ord_line
         SET qty = 0
       WHERE ord_id = p_ord_id
         AND shp_qty = 0;
    ELSE
      RAISE_APPLICATION_ERROR(-20111, 'Order ' || p_ord_id || ' cannot be cancelled in status ' || v_stat);
    END IF;

    util_log.log_msg('I', c_src, 'cancelled ' || p_ord_id || ' rsn ' || p_rsn_cd);
  END cancel_order;

  PROCEDURE archive_orders (
    p_keep_days IN  PLS_INTEGER DEFAULT 730,
    p_cnt       OUT PLS_INTEGER
  ) IS
    v_tbl VARCHAR2(30) := 'ORD_HDR_ARCH_' || TO_CHAR(SYSDATE, 'YYYY');
  BEGIN
    EXECUTE IMMEDIATE
      'INSERT INTO ' || v_tbl || q'[ SELECT * FROM ord_hdr WHERE stat_cd IN ('I','C') AND sub_dt < :1]'
      USING SYSDATE - p_keep_days;
    p_cnt := SQL%ROWCOUNT;

    DELETE FROM ord_line ol
     WHERE EXISTS (SELECT 1
                     FROM ord_hdr oh
                    WHERE oh.ord_id = ol.ord_id
                      AND oh.stat_cd IN ('I', 'C')
                      AND oh.sub_dt < SYSDATE - p_keep_days);

    DELETE ord_hdr
     WHERE stat_cd IN ('I', 'C')
       AND sub_dt < SYSDATE - p_keep_days;

    util_log.log_msg('I', c_src, 'archived ' || p_cnt || ' orders into ' || v_tbl);
  END archive_orders;

END ord_proc;
/
