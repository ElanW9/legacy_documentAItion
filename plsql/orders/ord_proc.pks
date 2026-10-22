CREATE OR REPLACE PACKAGE ord_proc AS
  /*
   * Order processing.
   * 2000-02-14  PVR  initial version
   * 2011-05-09  KDM  discount pct per order
   * 2015-10-01  LDB  archiving
   */

  g_max_lines CONSTANT PLS_INTEGER := 250;

  FUNCTION calc_disc_pct (p_ord_id IN NUMBER) RETURN NUMBER;

  PROCEDURE submit_order (p_ord_id IN NUMBER);

  PROCEDURE cancel_order (
    p_ord_id IN NUMBER,
    p_rsn_cd IN VARCHAR2
  );

  PROCEDURE archive_orders (
    p_keep_days IN  PLS_INTEGER DEFAULT 730,
    p_cnt       OUT PLS_INTEGER
  );

END ord_proc;
/
