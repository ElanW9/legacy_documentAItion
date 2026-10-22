CREATE OR REPLACE PACKAGE bill_calc AS
  /*
   * Invoice calculation and creation.
   * 2001-09-20  PVR  initial version
   * 2009-02-03  LDB  removed KMO discount
   * 2013-11-28  LDB  late fees
   */

  FUNCTION calc_line_amt (
    p_qty   IN NUMBER,
    p_price IN NUMBER,
    p_disc  IN NUMBER DEFAULT 0
  ) RETURN NUMBER;

  FUNCTION calc_late_fee (
    p_inv_id IN NUMBER,
    p_ref_dt IN DATE DEFAULT TRUNC(SYSDATE)
  ) RETURN NUMBER;

  PROCEDURE create_invoice (
    p_ord_id IN  NUMBER,
    p_inv_id OUT NUMBER
  );

END bill_calc;
/
