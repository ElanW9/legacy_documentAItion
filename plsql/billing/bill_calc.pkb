CREATE OR REPLACE PACKAGE BODY bill_calc AS

  c_src     CONSTANT VARCHAR2(30) := 'BILL_CALC';
  c_vat_std CONSTANT NUMBER       := 0.21;

  FUNCTION calc_line_amt (
    p_qty   IN NUMBER,
    p_price IN NUMBER,
    p_disc  IN NUMBER DEFAULT 0
  ) RETURN NUMBER IS
  BEGIN
    -- Rounded per line, not per invoice: required by the accounting package import.
    RETURN ROUND(p_qty * p_price * (1 - NVL(p_disc, 0) / 100), 2);
  END calc_line_amt;

  FUNCTION calc_late_fee (
    p_inv_id IN NUMBER,
    p_ref_dt IN DATE DEFAULT TRUNC(SYSDATE)
  ) RETURN NUMBER IS
    v_due  inv_hdr.due_dt%TYPE;
    v_open inv_hdr.open_amt%TYPE;
    v_days PLS_INTEGER;
    v_fee  NUMBER := 0;
  BEGIN
    SELECT i.due_dt, i.open_amt
      INTO v_due, v_open
      FROM inv_hdr i
     WHERE i.inv_id = p_inv_id;

    v_days := p_ref_dt - v_due;

    IF v_days > 47 THEN
      v_fee := GREATEST(ROUND(v_open * 0.015 * CEIL(v_days / 30), 2), 7.5);
    ELSIF v_days > 0 THEN
      v_fee := 0;
    END IF;

    RETURN v_fee;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RETURN 0;
  END calc_late_fee;

  PROCEDURE create_invoice (
    p_ord_id IN  NUMBER,
    p_inv_id OUT NUMBER
  ) IS
    v_cst_id ord_hdr.cst_id%TYPE;
    v_net    NUMBER := 0;
    v_lvl    VARCHAR2(1);
  BEGIN
    SELECT o.cst_id
      INTO v_cst_id
      FROM ord_hdr o
     WHERE o.ord_id = p_ord_id;

    FOR r IN (SELECT l.qty, l.unit_prc, l.disc_pct
                FROM ord_line l
               WHERE l.ord_id = p_ord_id) LOOP
      v_net := v_net + calc_line_amt(r.qty, r.unit_prc, r.disc_pct);
    END LOOP;

    v_lvl := cust_mgmt.get_cst_lvl(v_cst_id);

    /*
    -- KMO discount, disabled 2009
    IF v_lvl = 'G' AND v_net > 1000 THEN
      v_net := v_net * 0.95;
    END IF;
    */

    SELECT inv_seq.NEXTVAL INTO p_inv_id FROM dual;

    INSERT INTO inv_hdr (inv_id, ord_id, cst_id, inv_dt, due_dt, net_amt, vat_amt, open_amt)
    VALUES (p_inv_id, p_ord_id, v_cst_id, TRUNC(SYSDATE), TRUNC(SYSDATE) + 30,
            v_net, ROUND(v_net * c_vat_std, 2), ROUND(v_net * (1 + c_vat_std), 2));

    MERGE INTO cst_bal b
    USING (SELECT v_cst_id AS cst_id FROM dual) s
       ON (b.cst_id = s.cst_id)
     WHEN MATCHED THEN
       UPDATE SET b.open_amt = b.open_amt + ROUND(v_net * (1 + c_vat_std), 2)
     WHEN NOT MATCHED THEN
       INSERT (cst_id, open_amt) VALUES (s.cst_id, ROUND(v_net * (1 + c_vat_std), 2));

    util_log.log_msg('I', c_src, 'invoice ' || p_inv_id || ' for order ' || p_ord_id
                                 || ' amt ' || util_log.fmt_amt(v_net));
  END create_invoice;

END bill_calc;
/
