import os
import time
from pathlib import Path

import pandas as pd
import numpy_financial as npf  # pip install numpy-financial
import requests
from fredapi import Fred

# Optional: load FRED_API_KEY from a local .env if python-dotenv is installed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
TREASURY_TTL_SEC = 24 * 60 * 60       # refresh DGS10 once a day
ZORI_TTL_SEC = 7 * 24 * 60 * 60       # refresh ZORI weekly

ZORI_METRO_CSV_URL = os.environ.get(
    "ZORI_METRO_CSV_URL",
    "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
)

# Our internal market tag -> Zillow MSA RegionName (exact string in the CSV)
MARKET_TO_ZORI_METRO = {
    "PHOENIX": "Phoenix, AZ",
    "TEMPE":   "Phoenix, AZ",
    "CHICAGO": "Chicago, IL",
    "DALLAS":  "Dallas, TX",
}


def _is_fresh(path, ttl_sec):
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_sec


def get_live_treasury_rate():
    """Live 10-Year Treasury (FRED DGS10) with daily disk cache.

    Reads the key from the FRED_API_KEY env var. Falls back to 4.5% if the
    key is unset or the fetch fails.
    """
    cache = CACHE_DIR / "dgs10.txt"
    if _is_fresh(cache, TREASURY_TTL_SEC):
        try:
            return float(cache.read_text().strip())
        except Exception:
            pass

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("ℹ️  FRED_API_KEY not set — defaulting to 4.50% 10-Yr Treasury.")
        return 0.045
    try:
        fred = Fred(api_key=api_key)
        series = fred.get_series("DGS10").dropna()
        rate = float(series.iloc[-1]) / 100
        cache.write_text(str(rate))
        return rate
    except Exception as e:
        print(f"⚠️  FRED fetch failed: {e}. Falling back to 4.50%.")
        return 0.045


_zori_df_cache = None


def _load_zori_metro_df():
    """Downloads (or reads cached) Zillow ZORI Metro CSV and memoizes it."""
    global _zori_df_cache
    if _zori_df_cache is not None:
        return _zori_df_cache

    cache = CACHE_DIR / "zori_metro.csv"
    if not _is_fresh(cache, ZORI_TTL_SEC):
        try:
            r = requests.get(ZORI_METRO_CSV_URL, timeout=30)
            r.raise_for_status()
            cache.write_bytes(r.content)
        except Exception as e:
            print(f"⚠️  ZORI download failed: {e}.")
            if not cache.exists():
                return None
    try:
        _zori_df_cache = pd.read_csv(cache)
        return _zori_df_cache
    except Exception as e:
        print(f"⚠️  Failed to parse ZORI CSV: {e}.")
        return None


def get_zori_rent_growth(market_tag):
    """Trailing-12-month YoY ZORI rent growth for the metro tied to market_tag.

    Returns None when the metro isn't mapped, the CSV can't be loaded, or
    the series is too short — the caller should fall back to MARKET_LIBRARY.
    """
    metro = MARKET_TO_ZORI_METRO.get(market_tag.upper())
    if not metro:
        return None
    df = _load_zori_metro_df()
    if df is None:
        return None
    row = df[df["RegionName"] == metro]
    if row.empty:
        return None

    date_cols = [c for c in df.columns if len(c) == 10 and c[4] == "-" and c[7] == "-"]
    if len(date_cols) < 13:
        return None
    series = row.iloc[0][date_cols].astype(float).dropna()
    if len(series) < 13:
        return None
    latest, year_ago = float(series.iloc[-1]), float(series.iloc[-13])
    if year_ago <= 0:
        return None
    return latest / year_ago - 1.0


MARKET_LIBRARY = {
    "PHOENIX": {"vacancy": 0.12, "rent_growth": 0.05},
    "CHICAGO": {"vacancy": 0.08, "rent_growth": 0.02},
    "DALLAS":  {"vacancy": 0.10, "rent_growth": 0.04},
    "TEMPE":   {"vacancy": 0.12, "rent_growth": 0.05},
    "DEFAULT": {"vacancy": 0.15, "rent_growth": 0.01}
}


class Property:
    def __init__(self, name, units, market_tag="DEFAULT", amort_years=30):
        self.name = name
        self.units = units
        self.market_tag = market_tag.upper()
        self.amort_years = amort_years

    def get_remaining_balance(self, loan_amount, year, rate):
        """Calculates the remaining loan balance at any given year for the balloon payoff"""
        r_monthly = rate / 12
        n_months = self.amort_years * 12
        payments_made = year * 12
        if payments_made >= n_months:
            return 0.0
        balance = loan_amount * (
            ((1 + r_monthly) ** n_months - (1 + r_monthly) ** payments_made)
            / ((1 + r_monthly) ** n_months - 1)
        )
        return max(0.0, balance)

    def calculate_irr_at_strike(self, strike_price, loan_amount, annual_debt_service, base_noi, rate, rent_growth):
        """Simulates a 10-year hold and handles a year-10 balloon payoff to find Levered IRR"""
        equity_down = strike_price - loan_amount
        levered_cash_flows = [-equity_down]

        current_noi = base_noi
        for year in range(1, 11):
            if year > 1:
                current_noi *= (1 + rent_growth)

            if year == 10:
                # Capitalize Year 11 NOI at an institutional 6.0% Exit Cap Rate to estimate Sale Price
                sale_price = (current_noi * (1 + rent_growth)) / 0.06
                remaining_debt = self.get_remaining_balance(loan_amount, 10, rate)
                net_sale_proceeds = sale_price - remaining_debt
                levered_cash_flows.append((current_noi - annual_debt_service) + net_sale_proceeds)
            else:
                levered_cash_flows.append(current_noi - annual_debt_service)

        try:
            return round(npf.irr(levered_cash_flows) * 100, 2)
        except Exception:
            return 0.0

    def process_institutional_underwriting(self, explicit_noi, pricing_guidance=None):
        """Forces a 1.25 DSCR and runs matrix scenarios categorized by LTV brackets and Agency spreads"""
        base_rate = get_live_treasury_rate()
        market_data = MARKET_LIBRARY.get(self.market_tag, MARKET_LIBRARY["DEFAULT"])

        live_rent_growth = get_zori_rent_growth(self.market_tag)
        if live_rent_growth is not None:
            rent_growth = live_rent_growth
            rent_growth_source = f"ZORI live ({self.market_tag})"
        else:
            rent_growth = market_data.get("rent_growth", 0.03)
            rent_growth_source = f"library default ({self.market_tag})"

        print(f"\n======================================================================================================")
        print(f"📊 INSTITUTIONAL BUY SCORE & STRIKE ENGINE: {self.name}")
        print(f"======================================================================================================")
        print(f"Underwriting Parameters: Forced 1.25 DSCR Target Floor | Base Operational NOI: ${explicit_noi:,.2f}")
        print(f"Macro Baseline: 10-Yr Treasury: {base_rate*100:.2f}% | Rent Growth: {rent_growth*100:.2f}% ({rent_growth_source}) | Amortization: {self.amort_years} Years")
        if pricing_guidance and pricing_guidance > 5000:
            print(f"Broker/OM Asking Price: ${pricing_guidance:,.2f}")
        print("-" * 102)
        print(f"{'LTV %':<6} | {'Agency Rate (Spread)':<22} | {'Max Supported Loan':<19} | {'🎯 TARGET STRIKE PRICE':<22} | {'🏆 BUY SCORE'}")
        print("-" * 102)

        # LTV brackets required to organize output metrics
        ltv_brackets = [0.55, 0.60, 0.65, 0.70]

        # Stress Agency debt cost options (150 bps and 175 bps)
        spread_variants = [(0.0150, "150 bps"), (0.0175, "175 bps")]

        for ltv in ltv_brackets:
            for spread, label in spread_variants:
                agency_rate = base_rate + spread
                r_monthly = agency_rate / 12
                n_months = self.amort_years * 12

                # Solve for annual loan constant factor
                loan_constant_factor = (r_monthly * (1 + r_monthly) ** n_months) / ((1 + r_monthly) ** n_months - 1)
                annual_loan_constant = loan_constant_factor * 12

                # Force DSCR to exactly 1.25
                max_allowable_debt_service = explicit_noi / 1.25
                max_supported_loan = max_allowable_debt_service / annual_loan_constant

                # Adjust purchase price instead of injecting cash gap parameters
                target_strike_price = max_supported_loan / ltv
                equity_down = target_strike_price - max_supported_loan

                y1_cash_flow = explicit_noi - max_allowable_debt_service
                y1_coc = (y1_cash_flow / equity_down) * 100 if equity_down > 0 else 0.0

                # Extract dynamic multiyear compounding IRR at this specific strike price structure
                levered_irr = self.calculate_irr_at_strike(
                    target_strike_price, max_supported_loan, max_allowable_debt_service, explicit_noi, agency_rate, rent_growth
                )

                # --- SCORING ENGINE (Evaluated at the Negotiated Strike Price Basis) ---
                score = 0

                # Cash-on-Cash Component (Max 40 Points, 6.0% Yield Target Hurdle)
                score += min(40.0, max(0.0, (y1_coc / 6.0) * 40))

                # Long-Term Compounding Return Component (Max 40 Points, 12.0% IRR Target Hurdle)
                score += min(40.0, max(0.0, (levered_irr / 12.0) * 40))

                # Leverage Efficiency Credit Allocation (Max 20 Points)
                if ltv == 0.55:
                    score += 20
                elif ltv == 0.60:
                    score += 15
                elif ltv == 0.65:
                    score += 10
                elif ltv == 0.70:
                    score += 5

                if score >= 85:
                    grade = "🟢 STRONG BUY"
                elif score >= 65:
                    grade = "🟡 HOLD/NEGOTIATE"
                else:
                    grade = "🔴 PASS"

                rate_str = f"{agency_rate*100:.2f}% ({label})"
                output_grade = f"{grade} ({score:.1f}/100) | CoC: {y1_coc:.2f}% | IRR: {levered_irr:.2f}%"

                print(f"{ltv*100:.0f}%   | {rate_str:<22} | {f'${max_supported_loan:,.2f}':<19} | {f'${target_strike_price:,.0f}':<22} | {output_grade}")
            print("-" * 102)


def run_batch_analysis(file_path):
    # Enforce directory path alignment trap protection
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print(f"Error: file '{file_path}' not found in current project folder directory!")
        return

    df.columns = df.columns.str.strip()
    print(f"📂 Found {len(df)} assets in data sheet. Activating Dynamic Strike Matrices...\n")

    for index, row in df.iterrows():
        units = row['# Units']
        if units < 50:
            print(f"⏩ BYPASSED: {row['Property Name']} ({units} units falls below your 50-unit minimum portfolio constraint.)")
            print("-" * 102)
            continue

        # Determine macro tags using geographic constraints
        market_label = "DEFAULT"
        if "County" in row and pd.notna(row["County"]):
            cleaned_county = str(row["County"]).strip().upper()
            if "TEMPE" in cleaned_county or "MARICOPA" in cleaned_county or "PHOENIX" in cleaned_county:
                market_label = "TEMPE"
            elif "RIVERSIDE" in cleaned_county:
                market_label = "DEFAULT"
            else:
                market_label = "DEFAULT"

        deal = Property(
            name=row['Property Name'].strip(),
            units=units,
            market_tag=market_label,
            amort_years=30  # Standard Agency multi-family amortization schedule baseline
        )

        # Check for explicit pricing or trailing NOI properties inside source sheet documents
        explicit_noi = row.get('NOI', 0)
        pricing_guidance = row.get('Price', 0)

        deal.process_institutional_underwriting(explicit_noi=explicit_noi, pricing_guidance=pricing_guidance)


if __name__ == "__main__":
    # Point this to whatever CSV file you are currently evaluating (e.g., "Untitled.csv" or "deals.csv")
    if os.path.exists("Untitled.csv"):
        run_batch_analysis("Untitled.csv")
    else:
        print("⚠️ 'Untitled.csv' not found. Please verify your filename parameters.")
