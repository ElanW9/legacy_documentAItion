CREATE OR REPLACE PACKAGE BODY util_log AS

  PROCEDURE log_msg (
    p_lvl IN VARCHAR2,
    p_src IN VARCHAR2,
    p_txt IN VARCHAR2
  ) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
  BEGIN
    INSERT INTO app_log (log_id, log_ts, lvl, src, txt)
    VALUES (app_log_seq.NEXTVAL, SYSTIMESTAMP, p_lvl, p_src, SUBSTR(p_txt, 1, 4000));
    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      NULL;
  END log_msg;

  FUNCTION fmt_amt (p_amt IN NUMBER) RETURN VARCHAR2 IS
  BEGIN
    RETURN TO_CHAR(NVL(p_amt, 0), 'FM999G999G990D00');
  END fmt_amt;

END util_log;
/
