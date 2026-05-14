import pandas as pd
import numpy_financial as npf  # Make sure to run 'pip install numpy-financial'
from fredapi import Fred
import os


def get_live_treasury_rate(api_key=None):
    """Fetches the live 10-Year Treasury rate from FRED, or defaults to 4.5%"""
    if not api_key:
        return 0.045  # Default 4.5% if no API key is provided
    try:
        fred = Fred(api_key=api_key)
        data = fred.get_series('DGS10')
        latest_rate = data.iloc[-1] / 100  # Convert e.g., 4.5 to 0.045
        return latest_rate
    except Exception as e:
        print(f"⚠️ FRED API failed to fetch live treasury rate: {e}. Falling back to 4.5%")
        return 0.045


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
        rent_growth = market_data.get("rent_growth", 0.03)

        print(f"\n======================================================================================================")
        print(f"📊 INSTITUTIONAL BUY SCORE & STRIKE ENGINE: {self.name}")
        print(f"======================================================================================================")
        print(f"Underwriting Parameters: Forced 1.25 DSCR Target Floor | Base Operational NOI: ${explicit_noi:,.2f}")
        print(f"Macro Baseline: 10-Yr Treasury: {base_rate*100:.2f}% | Amortization Schedule: {self.amort_years} Years")
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
