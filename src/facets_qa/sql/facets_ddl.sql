-- Local mock of the Facets tables used by the test framework.
-- Column names follow Facets conventions; this is a representative subset of
-- each table, not the full vendor schema. Runs on SQLite for local execution.

-- ---------------------------------------------------------------- Membership
CREATE TABLE IF NOT EXISTS CMC_SBSB_SUBSC (
    SBSB_CK          INTEGER PRIMARY KEY,
    GRGR_CK          INTEGER NOT NULL,
    SBSB_ID          VARCHAR(9)  NOT NULL UNIQUE,
    SBSB_LAST_NAME   VARCHAR(35) NOT NULL,
    SBSB_FIRST_NAME  VARCHAR(15) NOT NULL,
    SBSB_ORIG_EFF_DT DATE        NOT NULL
);

CREATE TABLE IF NOT EXISTS CMC_MEME_MEMBER (
    MEME_CK          INTEGER PRIMARY KEY,
    SBSB_CK          INTEGER NOT NULL REFERENCES CMC_SBSB_SUBSC (SBSB_CK),
    MEME_SFX         INTEGER NOT NULL,           -- 0 = subscriber, 1+ = dependents
    MEME_REL         CHAR(1) NOT NULL,           -- M = self, S = spouse, D = dependent
    MEME_LAST_NAME   VARCHAR(35) NOT NULL,
    MEME_FIRST_NAME  VARCHAR(15) NOT NULL,
    MEME_BIRTH_DT    DATE    NOT NULL,
    MEME_SEX         CHAR(1) NOT NULL
);

CREATE TABLE IF NOT EXISTS CMC_MEPE_PRCS_ELIG (
    MEME_CK          INTEGER NOT NULL REFERENCES CMC_MEME_MEMBER (MEME_CK),
    CSPD_CAT         CHAR(1) NOT NULL,           -- M = medical
    MEPE_EFF_DT      DATE    NOT NULL,
    MEPE_TERM_DT     DATE    NOT NULL,           -- 9999-12-31 = open ended
    MEPE_ELIG_IND    CHAR(1) NOT NULL,           -- Y / N
    CSPI_ID          VARCHAR(8)  NOT NULL,
    PDPD_ID          VARCHAR(8)  NOT NULL,
    PRIMARY KEY (MEME_CK, CSPD_CAT, MEPE_EFF_DT)
);

-- ---------------------------------------------------------------- Provider
CREATE TABLE IF NOT EXISTS CMC_PRPR_PROV (
    PRPR_ID          VARCHAR(12) PRIMARY KEY,
    PRPR_NPI         VARCHAR(10) NOT NULL UNIQUE,
    PRPR_NAME        VARCHAR(55) NOT NULL,
    MCTN_ID          VARCHAR(9)  NOT NULL        -- tax ID
);

-- ---------------------------------------------------------------- Claims
CREATE TABLE IF NOT EXISTS CMC_CLCL_CLAIM (
    CLCL_ID          VARCHAR(12) PRIMARY KEY,
    CLCL_PA_ACCT_NO  VARCHAR(20) NOT NULL,       -- patient control number (CLM01)
    CLCL_CL_TYPE     CHAR(1)     NOT NULL,       -- M = medical
    CLCL_CL_SUB_TYPE CHAR(1)     NOT NULL,       -- M = professional, H = hospital
    CLCL_CUR_STS     VARCHAR(2)  NOT NULL,       -- 01 = entered / pended
    MEME_CK          INTEGER     NOT NULL,
    PRPR_ID          VARCHAR(12) NOT NULL,
    CLCL_TOT_CHG     DECIMAL(11,2) NOT NULL,
    CLCL_LOW_SVC_DT  DATE        NOT NULL,
    CLCL_HIGH_SVC_DT DATE        NOT NULL,
    PSCD_ID          VARCHAR(2),                 -- place of service (professional)
    CLCL_INPUT_DT    DATE        NOT NULL
);

CREATE TABLE IF NOT EXISTS CMC_CDML_CL_LINE (
    CLCL_ID          VARCHAR(12) NOT NULL REFERENCES CMC_CLCL_CLAIM (CLCL_ID),
    CDML_SEQ_NO      INTEGER     NOT NULL,
    IPCD_ID          VARCHAR(7),                 -- procedure code
    CDML_MOD1        VARCHAR(2),
    CDML_MOD2        VARCHAR(2),
    CDML_MOD3        VARCHAR(2),
    CDML_MOD4        VARCHAR(2),
    RCRC_ID          VARCHAR(4),                 -- revenue code (institutional)
    CDML_CHG_AMT     DECIMAL(11,2) NOT NULL,
    CDML_UNITS       DECIMAL(9,3)  NOT NULL,
    CDML_FROM_DT     DATE        NOT NULL,
    CDML_TO_DT       DATE        NOT NULL,
    PRIMARY KEY (CLCL_ID, CDML_SEQ_NO)
);

CREATE TABLE IF NOT EXISTS CMC_CLHP_HOSP (
    CLCL_ID          VARCHAR(12) PRIMARY KEY REFERENCES CMC_CLCL_CLAIM (CLCL_ID),
    CLHP_FAC_TYPE    CHAR(1)     NOT NULL,       -- bill type digit 2 (type of facility)
    CLHP_BILL_CLASS  CHAR(1)     NOT NULL,       -- bill type digit 3 (classification)
    CLHP_FREQUENCY   CHAR(1)     NOT NULL,       -- bill type digit 4
    CLHP_ADM_DT      DATE,
    CLHP_DC_DT       DATE,
    CLHP_ADM_TYP     CHAR(1),
    CLHP_DC_STAT     CHAR(2)
);

-- ---------------------------------------------------------------- QA results (loaded back)
CREATE TABLE IF NOT EXISTS ELIG_TEST_RESULTS (
    RUN_ID           VARCHAR(40) NOT NULL,
    RUN_TS           TIMESTAMP   NOT NULL,
    MEME_CK          INTEGER,
    MEMBER_ID        VARCHAR(80) NOT NULL,
    SCENARIO         VARCHAR(30) NOT NULL,
    SERVICE_DATE     DATE        NOT NULL,
    TRACE_NUMBER     VARCHAR(50) NOT NULL,
    EXPECTED_STATUS  VARCHAR(10) NOT NULL,
    ACTUAL_STATUS    VARCHAR(10),
    EXPECTED_AAA     VARCHAR(2),
    ACTUAL_AAA       VARCHAR(2),
    RESULT           VARCHAR(5)  NOT NULL,       -- PASS / FAIL / ERROR
    FAILURE_REASON   VARCHAR(400),
    ATTEMPTS         INTEGER     NOT NULL,
    LATENCY_MS       INTEGER     NOT NULL,
    WORKER_ID        INTEGER     NOT NULL
);

CREATE TABLE IF NOT EXISTS CLAIM_TEST_RESULTS (
    RUN_ID           VARCHAR(40) NOT NULL,
    RUN_TS           TIMESTAMP   NOT NULL,
    SCENARIO         VARCHAR(60) NOT NULL,
    CLAIM_TYPE       VARCHAR(4)  NOT NULL,
    POLARITY         VARCHAR(8)  NOT NULL,       -- positive / negative
    CLCL_ID          VARCHAR(12),
    RESULT           VARCHAR(5)  NOT NULL,
    DETAIL           VARCHAR(2000),
    X12              TEXT
);
