# Data Requirements: USD Risk-Free Rate Curve

**Purpose**: Provide a daily USD risk-free zero-coupon yield curve for use in
the stock-level SVIX pipeline (Martin & Wagner 2019). The curve is used to
discount cash flows in the CRR/Leisen-Reimer binomial tree for European-
equivalent option pricing, and to interpolate the risk-free rate at arbitrary
option maturities τ.

**Research project**: `C:\Users\Miebs\PycharmProjects\Research`
**Data project**: `C:\Users\Miebs\PycharmProjects\Data`
**Database**: `research_db` (PostgreSQL)

---

## 1. Schema and Table

```sql
CREATE SCHEMA pub_rates;

CREATE TABLE pub_rates.USD_rates (
    trade_date   DATE         NOT NULL,
    tenor        VARCHAR(8)   NOT NULL,
    tenor_days   SMALLINT     NOT NULL,
    rate_type    VARCHAR(8)   NOT NULL,
    rate_pct     NUMERIC(8,4),
    compounding  VARCHAR(16)  NOT NULL,
    source       VARCHAR(32)  NOT NULL,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, tenor, rate_type)
);

CREATE INDEX idx_usd_rates_date ON pub_rates.USD_rates (trade_date);

COMMENT ON TABLE pub_rates.USD_rates IS
    'Daily USD risk-free rate curve. Stores both raw par yields (FRED CMT)
     and bootstrapped zero rates (discrete compounding). Conversion to
     continuous compounding is performed in application code as needed.';
```

---

## 2. Field Descriptions

| Field | Type | Description |
|---|---|---|
| `trade_date` | DATE | Reference date of the rate observation |
| `tenor` | VARCHAR(8) | Readable tenor label: `'1m'`, `'3m'`, `'6m'`, `'1y'`, `'2y'`, `'3y'`, `'5y'`, `'7y'`, `'10y'`, `'20y'`, `'30y'` |
| `tenor_days` | SMALLINT | Tenor in calendar days: 30, 91, 182, 365, 730, 1095, 1825, 2555, 3650, 7300, 10950 |
| `rate_type` | VARCHAR(8) | `'par'` = raw FRED par yield; `'zero'` = bootstrapped zero-coupon rate |
| `rate_pct` | NUMERIC(8,4) | Rate in percent, e.g. `4.25` for 4.25 % p.a. |
| `compounding` | VARCHAR(16) | Compounding convention — see §3 |
| `source` | VARCHAR(32) | `'FRED'` for par yields; `'bootstrapped'` for derived zeros |
| `updated_at` | TIMESTAMPTZ | Timestamp of last INSERT/UPDATE |

---

## 3. Rate Conventions

### 3a. Par Yields (`rate_type = 'par'`)

FRED Constant Maturity Treasury (CMT) rates are published as-is:

| Maturity | FRED convention | `compounding` value |
|---|---|---|
| 1m, 3m, 6m | Bank discount basis (annualised, 360-day year) | `'discount'` |
| 1y, 2y, 3y, 5y, 7y, 10y, 20y, 30y | Semi-annual bond equivalent yield | `'semiannual'` |

These are **not** zero-coupon rates and are stored verbatim from FRED without
any transformation.

### 3b. Zero Rates (`rate_type = 'zero'`)

Bootstrapped from par yields using the standard sequential bootstrap:

1. Short end (≤6m): convert discount-basis T-bill rates directly to zero rates
   on an annual discrete basis.
2. Long end (≥1y): sequential bootstrap, stripping each maturity using
   previously derived zeros for intermediate coupon cash flows. Coupon
   frequency = semi-annual (matching Treasury bond convention).
3. Result: continuously compounded zero rates are **not** stored.
   The database holds **annually compounded discrete zero rates**:
   `r_zero_pct` such that `disc(τ) = (1 + r_zero/100)^(−τ/365)`.

`compounding` value = `'annual'` for all zero-rate rows.

**Conversion in application code** (e.g. for the binomial tree or
Black-Scholes formula) from discrete annual to continuous:

```python
r_cont = np.log(1 + r_disc / 100)   # r_disc in percent → r_cont as decimal
```

Storing discrete rates in the database avoids rounding artefacts from the
double conversion (par → continuous → back to discrete) and keeps the stored
values directly interpretable.

---

## 4. Source: FRED CMT Series

| Tenor | FRED Series ID | Description |
|---|---|---|
| 1m | `DGS1MO` | 1-Month Treasury Constant Maturity Rate |
| 3m | `DGS3MO` | 3-Month Treasury Constant Maturity Rate |
| 6m | `DGS6MO` | 6-Month Treasury Constant Maturity Rate |
| 1y | `DGS1` | 1-Year Treasury Constant Maturity Rate |
| 2y | `DGS2` | 2-Year Treasury Constant Maturity Rate |
| 3y | `DGS3` | 3-Year Treasury Constant Maturity Rate |
| 5y | `DGS5` | 5-Year Treasury Constant Maturity Rate |
| 7y | `DGS7` | 7-Year Treasury Constant Maturity Rate |
| 10y | `DGS10` | 10-Year Treasury Constant Maturity Rate |
| 20y | `DGS20` | 20-Year Treasury Constant Maturity Rate |
| 30y | `DGS30` | 30-Year Treasury Constant Maturity Rate |

FRED provides these series via the FRED API (`fredapi` Python package) or
direct CSV download. API key required (free registration at fred.stlouisfed.org).

---

## 5. Tenors Stored

Eleven standard CMT tenors are stored. For the binomial tree and SVIX
interpolation, the required maturity τ (typically 30–360 days) is obtained
by **log-linear interpolation** between the two adjacent tenors:

```python
# Example: τ = 45 days → interpolate between 1m (30d) and 3m (91d)
w = (np.log(tau) - np.log(t1)) / (np.log(t2) - np.log(t1))
r_interp = (1 - w) * r1 + w * r2   # r in percent
```

---

## 6. Historical Depth and Universe

- **Start date**: 10 years back from current date (≈ 2016-01-01)
- **End date**: most recent available trading day
- **Frequency**: daily (FRED publishes on US business days; weekends and
  holidays have no observation — do not forward-fill, leave gaps)
- **Both rate types** per date: each trading day has 11 × 2 = 22 rows
  (11 tenors × par + zero)

---

## 7. Fetch and Bootstrap Script

To be implemented in the Data project as:
`C:\Users\Miebs\PycharmProjects\Data\modules\fetch_usd_rates.py`

Responsibilities:
1. Read FRED API key from `Config/.env` (variable `FRED_API_KEY`)
2. Pull par yields for all 11 CMT series for the required date range
3. Bootstrap zero rates from par yields (annual discrete, as specified in §3b)
4. Write both par and zero rows to `pub_rates.USD_rates`
5. Incremental by default: only pull dates not yet present in the table
6. Callable from `run_monthly.py` (Data project job orchestrator)

---

## 8. Usage in SVIX Pipeline

In `SVIX/compute_svix_stock.py`, the discount factor for maturity τ is
obtained as:

```python
# 1. Query zero rate for trade_date, interpolated at tau days
#    (log-linear between adjacent tenors)
r_zero_pct = interpolate_zero_rate(trade_date, tau, conn)

# 2. Convert discrete annual → continuous
r_cont = np.log(1 + r_zero_pct / 100)

# 3. Discount factor
disc = np.exp(-r_cont * tau / 365)
```

This replaces the interim approach of using the SPX PCP-derived discount
factor as a risk-free rate proxy.
