CREATE OR REPLACE PACKAGE cust_mgmt AS
  /*
   * Customer master maintenance.
   * 1999-06-02  PVR  initial version
   * 2006-01-17  KDM  added xfl check
   */

  FUNCTION get_cst_lvl (p_cst_id IN NUMBER) RETURN VARCHAR2;

  PROCEDURE upd_cst_stat (
    p_cst_id IN NUMBER,
    p_new_cd IN VARCHAR2,
    p_usr    IN VARCHAR2 DEFAULT USER
  );

  PROCEDURE chk_cst_xfl (
    p_cst_id IN  NUMBER,
    p_xfl    OUT VARCHAR2
  );

END cust_mgmt;
/
