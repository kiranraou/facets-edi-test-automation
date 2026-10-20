-- Eligibility test candidates: one row per subscriber per medical eligibility span.
-- The pipeline groups spans per member and derives the expected 271 outcome from them.
-- Same query runs against the ADF-landed bronze tables in Databricks (see jobs/).
SELECT
    m.MEME_CK,
    s.SBSB_ID,
    m.MEME_SFX,
    m.MEME_FIRST_NAME,
    m.MEME_LAST_NAME,
    m.MEME_BIRTH_DT,
    m.MEME_SEX,
    e.MEPE_EFF_DT,
    e.MEPE_TERM_DT,
    e.MEPE_ELIG_IND,
    e.CSPD_CAT,
    e.PDPD_ID
FROM CMC_MEME_MEMBER m
JOIN CMC_SBSB_SUBSC s
  ON s.SBSB_CK = m.SBSB_CK
LEFT JOIN CMC_MEPE_PRCS_ELIG e
  ON e.MEME_CK = m.MEME_CK
 AND e.CSPD_CAT = 'M'
WHERE m.MEME_SFX = 0
ORDER BY m.MEME_CK, e.MEPE_EFF_DT
