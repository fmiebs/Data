# Data Requirements: Stock-Level SVIX (Martin & Wagner 2019)

**Purpose**: Extend the existing SVIX pipeline from index-level (Martin 2017)
to stock-level (Martin & Wagner 2019) for all SPX constituents.
This document specifies all Eikon data fields to be fetched, the target
database tables, and the control table `pub_equity.overview_company_data`.

**Research project**: `C:\Users\Miebs\PycharmProjects\Research`
**Data project**: `C:\Users\Miebs\PycharmProjects\Data`
**Database**: `research_db` (PostgreSQL)

---

## 1. Control Table: `pub_equity.overview_company_data`

This is a **mapping/control table** that governs the automated data pull.
Each row describes one Eikon field, its storage target, and whether it is
currently active. The data pipeline reads this table to determine what to
fetch and where to store it.

### DDL

```sql
CREATE TABLE pub_equity.overview_company_data (
    field           VARCHAR(64)  NOT NULL,   -- Eikon field code, e.g. 'TR.SharesOutstanding'
    description     TEXT         NOT NULL,   -- Human-readable description
    active          BOOLEAN      NOT NULL DEFAULT TRUE,  -- Enable/disable field in pipeline
    frequency       VARCHAR(16)  NOT NULL,   -- 'daily', 'quarterly', 'static', 'event'
    target_table    VARCHAR(128) NOT NULL,   -- Fully qualified: schema.table
    target_column   VARCHAR(64)  NOT NULL,   -- Column in target_table
    notes           TEXT,                   -- Implementation notes, transformations
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (field)
);

COMMENT ON TABLE pub_equity.overview_company_data IS
    'Control table: maps Eikon field codes to target columns and pipeline settings.
     Populated once; pipeline reads active rows to determine what to pull.';
```

### Seed Data (INSERT)

```sql
-- ── Daily: Shares Outstanding ─────────────────────────────────────────────
INSERT INTO pub_equity.overview_company_data
    (field, description, active, frequency, target_table, target_column, notes)
VALUES
('TR.SharesOutstanding',
 'Shares outstanding (common equity, not adjusted)',
 TRUE, 'daily',
 'pub_equity.prices_daily', 'shares_outstanding',
 'Pulled alongside close price; used to compute market-cap weights for SVIX-bar.');

-- ── Quarterly: Book Equity ────────────────────────────────────────────────
INSERT INTO pub_equity.overview_company_data VALUES
('TR.BookValuePerShare',
 'Book value per share (common equity)',
 TRUE, 'quarterly',
 'pub_equity.fundamentals_quarterly', 'book_value_per_share',
 'Multiply by shares outstanding to get book equity. Period = fiscal quarter end.');

('TR.TotalEquity',
 'Total book equity (common equity, USD)',
 TRUE, 'quarterly',
 'pub_equity.fundamentals_quarterly', 'book_equity',
 'Direct book equity figure; cross-check with book_value_per_share × shares.');

-- ── Quarterly: GICS Classification ───────────────────────────────────────
INSERT INTO pub_equity.overview_company_data VALUES
('TR.GICSSector',
 'GICS Sector code (2-digit)',
 TRUE, 'quarterly',
 'pub_equity.fundamentals_quarterly', 'gics_sector',
 'Static in practice; stored quarterly to capture reclassifications.');

('TR.GICSIndustryGroup',
 'GICS Industry Group code (4-digit)',
 TRUE, 'quarterly',
 'pub_equity.fundamentals_quarterly', 'gics_industry_group',
 NULL);

('TR.GICSIndustry',
 'GICS Industry code (6-digit)',
 TRUE, 'quarterly',
 'pub_equity.fundamentals_quarterly', 'gics_industry',
 NULL);

('TR.GICSSubIndustry',
 'GICS Sub-Industry code (8-digit)',
 TRUE, 'quarterly',
 'pub_equity.fundamentals_quarterly', 'gics_sub_industry',
 NULL);

-- ── Event-based: Cash Dividends ───────────────────────────────────────────
INSERT INTO pub_equity.overview_company_data VALUES
('TR.DivExDate',
 'Ex-dividend date for regular cash dividends',
 TRUE, 'event',
 'pub_equity.dividends', 'ex_date',
 'Ex-date determines when to apply dividend in binomial tree for European-equivalent pricing.');

('TR.DivUnadjustedGross',
 'Gross dividend per share (unadjusted, local currency)',
 TRUE, 'event',
 'pub_equity.dividends', 'amount',
 'Unadjusted gross amount; do not use split-adjusted version for option pricing.');

('TR.DivPayDate',
 'Payment date of dividend',
 FALSE, 'event',
 'pub_equity.dividends', 'pay_date',
 'Optional; stored for completeness but ex_date is sufficient for binomial tree.');

-- ── Event-based: Special Dividends ────────────────────────────────────────
INSERT INTO pub_equity.overview_company_data VALUES
('TR.SpecialDivExDate',
 'Ex-dividend date for special (non-recurring) cash dividends',
 TRUE, 'event',
 'pub_equity.dividends', 'ex_date',
 'Special dividends can have a large impact on option prices; must be included.');

('TR.SpecialDivAmount',
 'Amount of special dividend per share (unadjusted)',
 TRUE, 'event',
 'pub_equity.dividends', 'amount',
 'Stored with div_type = ''special'' to distinguish from regular dividends.');
```

---

## 2. Schema Changes to Existing Tables

### 2a. `pub_equity.prices_daily` — add `shares_outstanding`

```sql
ALTER TABLE pub_equity.prices_daily
    ADD COLUMN IF NOT EXISTS shares_outstanding BIGINT;

COMMENT ON COLUMN pub_equity.prices_daily.shares_outstanding IS
    'Shares outstanding (common, not adjusted). Source: TR.SharesOutstanding via Eikon.
     Used to compute market-cap weight = shares_outstanding × close_price / sum(market_cap).
     Required for SVIX-bar in Martin & Wagner (2019) Phase 2.';
```

---

## 3. New Tables

### 3a. `pub_equity.fundamentals_quarterly`

```sql
CREATE TABLE pub_equity.fundamentals_quarterly (
    ric                 VARCHAR(32)  NOT NULL,
    period_end_date     DATE         NOT NULL,  -- fiscal quarter end
    fiscal_year         SMALLINT     NOT NULL,
    fiscal_quarter      SMALLINT     NOT NULL,  -- 1..4
    book_value_per_share NUMERIC(18,6),          -- TR.BookValuePerShare
    book_equity         NUMERIC(18,4),           -- TR.TotalEquity (USD)
    gics_sector         CHAR(2),                 -- TR.GICSSector
    gics_industry_group CHAR(4),                 -- TR.GICSIndustryGroup
    gics_industry       CHAR(6),                 -- TR.GICSIndustry
    gics_sub_industry   CHAR(8),                 -- TR.GICSSubIndustry
    reporting_ccy       CHAR(3),                 -- reporting currency
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (ric, period_end_date)
);

CREATE INDEX idx_fq_ric ON pub_equity.fundamentals_quarterly (ric);

COMMENT ON TABLE pub_equity.fundamentals_quarterly IS
    'Quarterly fundamentals for SPX constituents.
     Book equity used in Martin & Wagner (2019) cross-sectional regressions (Table IV).
     GICS used for sector-level analysis.
     Source: Refinitiv Eikon TR.BookValuePerShare, TR.TotalEquity, TR.GICS*.';
```

### 3b. `pub_equity.dividends`

```sql
CREATE TABLE pub_equity.dividends (
    ric         VARCHAR(32)  NOT NULL,
    ex_date     DATE         NOT NULL,
    div_type    VARCHAR(16)  NOT NULL,  -- 'regular', 'special'
    amount      NUMERIC(12,6) NOT NULL, -- gross per share, unadjusted, local ccy
    currency    CHAR(3),
    pay_date    DATE,                   -- optional payment date
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (ric, ex_date, div_type)
);

CREATE INDEX idx_div_ric_exdate ON pub_equity.dividends (ric, ex_date);

COMMENT ON TABLE pub_equity.dividends IS
    'Cash and special dividends for SPX constituents (unadjusted gross per share).
     Used in CRR/Leisen-Reimer binomial tree to convert American option prices
     to European-equivalent prices for SVIX computation (Martin & Wagner 2019, §II).
     Ex-date is the relevant date for the binomial tree — pay_date is informational.
     Source: Refinitiv Eikon TR.DivExDate + TR.DivUnadjustedGross (regular),
             TR.SpecialDivExDate + TR.SpecialDivAmount (special).';
```

---

## 4. Field Summary

| Eikon Field | Description | Frequency | Target Table | Target Column |
|---|---|---|---|---|
| `TR.SharesOutstanding` | Shares outstanding | daily | `pub_equity.prices_daily` | `shares_outstanding` |
| `TR.BookValuePerShare` | Book value per share | quarterly | `pub_equity.fundamentals_quarterly` | `book_value_per_share` |
| `TR.TotalEquity` | Total book equity | quarterly | `pub_equity.fundamentals_quarterly` | `book_equity` |
| `TR.GICSSector` | GICS Sector (2-digit) | quarterly | `pub_equity.fundamentals_quarterly` | `gics_sector` |
| `TR.GICSIndustryGroup` | GICS Industry Group (4-digit) | quarterly | `pub_equity.fundamentals_quarterly` | `gics_industry_group` |
| `TR.GICSIndustry` | GICS Industry (6-digit) | quarterly | `pub_equity.fundamentals_quarterly` | `gics_industry` |
| `TR.GICSSubIndustry` | GICS Sub-Industry (8-digit) | quarterly | `pub_equity.fundamentals_quarterly` | `gics_sub_industry` |
| `TR.DivExDate` | Ex-date, regular dividend | event | `pub_equity.dividends` | `ex_date` |
| `TR.DivUnadjustedGross` | Amount, regular dividend | event | `pub_equity.dividends` | `amount` |
| `TR.DivPayDate` | Payment date, regular | event | `pub_equity.dividends` | `pay_date` |
| `TR.SpecialDivExDate` | Ex-date, special dividend | event | `pub_equity.dividends` | `ex_date` |
| `TR.SpecialDivAmount` | Amount, special dividend | event | `pub_equity.dividends` | `amount` |

---

## 5. Universe and Historical Depth

- **Universe**: All `constituent_ric` in `pub_equity.index_constituents`
  where `index_ric = '.SPX'`. Point-in-time snapshots (first of month)
  are available from this table — use the union of all historical snapshots
  to build the full pull universe.

  ```sql
  SELECT DISTINCT constituent_ric
  FROM pub_equity.index_constituents
  WHERE index_ric = '.SPX';
  ```

- **Historical depth**: 10 years back from the current date
  (i.e., from approximately 2016-01-01).

- **Daily data** (`prices_daily`, `shares_outstanding`):
  Pull for each trading day over the full 10-year window.

- **Quarterly data** (`fundamentals_quarterly`):
  Pull all available quarterly observations in the 10-year window.
  Store one row per (ric, period_end_date).

- **Event data** (`dividends`):
  Pull all dividend events with ex_date in the 10-year window.

---

## 6. Why These Fields?

### Shares Outstanding (daily)
Market-cap weight: $w_{i,t} = \frac{N_{i,t} \cdot S_{i,t}}{\sum_j N_{j,t} \cdot S_{j,t}}$

Used in Phase 2 of the SVIX computation to form the value-weighted average
$\overline{\text{SVIX}}^2_t = \sum_i w_{i,t} \cdot \text{SVIX}^2_{i,t}$ [MW Eq. 13].

### Book Equity (quarterly)
Used in cross-sectional return predictability regressions [MW Table IV]:
book-to-market ratio $B/M_{i,t}$.

### GICS Classification (quarterly)
Enables sector-level aggregation of SVIX and lower bounds.
Stored quarterly to capture any reclassifications over time.

### Dividends with Ex-Dates (event)
Required for the binomial tree (CRR or Leisen-Reimer) to convert
American option prices to European-equivalent prices. The tree needs
discrete dividend amounts and their ex-dates within the option's remaining
life. OptionMetrics uses the most recently paid historical dividend repeated
for future ex-dates — this pipeline follows the same convention.

The forward price (needed for the ATM crossing to find $F_{i,t}$) is:
$$F_{i,t} = \frac{S_{i,t} - \text{PV}(\text{Div})}{d(t,T)}$$
where $\text{PV}(\text{Div}) = \sum_k D_k \cdot e^{-r \cdot t_k}$ over
all ex-dates $t_k \in (t, T]$ with amount $D_k$.

---

## 7. Implementation Notes

1. **Unadjusted prices**: Use **unadjusted** dividend amounts (`TR.DivUnadjustedGross`,
   `TR.SpecialDivAmount`) — split/merger adjustments must not be applied,
   as option strikes are quoted against the unadjusted stock price.

2. **Currency**: Dividend amounts are in local currency (USD for US stocks).
   Store `currency` for non-USD constituents (ADRs, foreign listings).

3. **Special dividends**: Must be included — large special dividends
   (e.g., Microsoft 2004) cause discontinuities in option prices that
   would invalidate the forward calculation if ignored.

4. **Shares outstanding timing**: Eikon `TR.SharesOutstanding` is typically
   reported with a lag (filing date). Use the most recently available figure
   as of `trade_date` when computing weights; do not forward-fill across
   quarter boundaries without verification.

5. **GICS stability**: GICS codes change infrequently. Storing quarterly
   is sufficient; a static table would also work. The quarterly approach
   ensures any reclassification is captured.

6. **`overview_company_data` as pipeline control**: The data pull script
   should `SELECT field, target_table, target_column FROM pub_equity.overview_company_data WHERE active = TRUE`
   and dynamically dispatch fetches. This avoids hardcoding fields in the
   pipeline and allows adding/removing fields without code changes.
