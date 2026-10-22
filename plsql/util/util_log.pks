CREATE OR REPLACE PACKAGE util_log AS
  /*
   * Generic logging helpers.
   * 2004-03-11  JDW  initial version
   */

  PROCEDURE log_msg (
    p_lvl IN VARCHAR2,
    p_src IN VARCHAR2,
    p_txt IN VARCHAR2
  );

  FUNCTION fmt_amt (p_amt IN NUMBER) RETURN VARCHAR2;

END util_log;
/
