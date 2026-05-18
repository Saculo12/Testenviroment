"""
CoStar CSV Export Ingester → Scoring Engine Bridge

Usage:
    python costar_ingester.py [path/to/export.csv]

If no path is given, scans costar_exports/ for the newest .csv file.

CoStar column names vary by export type and user-configured views.  This
script normalises the most common variants, derives NOI from cap rate × price
when NOI is absent, maps counties to internal market tags, then runs the
institutional scoring engine against each qualifying property (≥50 units).

Results are written to scored_deals.csv alongside the terminal output.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REPO_DIR = Path(__file__).parent
EXPORTS_DIR = REPO_DIR / "costar_exports"
OUTPUT_CSV = REPO_DIR / "scored_deals.csv"
MIN_UNITS = 50

# ---------------------------------------------------------------------------
# Column normalisation
# First match wins (dict is insertion-ordered in Python 3.7+).
# Keys are lowercased + stripped; values are our internal column names.
# ---------------------------------------------------------------------------
_COLUMN_MAP: dict[str, str] = {
    # Property name
    "property name": "Property Name",
    "building name": "Property Name",
    "property address": "Property Name",
    # Units
    "number of units": "# Units",
    "no. of units": "# Units",
    "no of units": "# Units",
    "# units": "# Units",
    "units": "# Units",
    "total units": "# Units",
    # NOI
    "net operating income": "NOI",
    "annual noi": "NOI",
    "noi": "NOI",
    # Asking price
    "for sale asking price": "Price",
    "asking price": "Price",
    "sale price": "Price",
    "listed price": "Price",
    "price": "Price",
    # Cap rate (used to derive NOI)
    "cap rate": "Cap Rate",
    "cap rate (%)": "Cap Rate",
    "current cap rate": "Cap Rate",
    "overall rate (oar)": "Cap Rate",
    # Geography
    "county name": "County",
    "county/parish": "County",
    "county": "County",
    "city": "City",
    "state": "State",
    "state/province": "State",
    "zip code": "Zip",
    "zip": "Zip",
    "market": "Market",
    "submarket": "Submarket",
    # Bonus fields (kept for the output CSV but not required)
    "year built": "Year Built",
    "building class": "Building Class",
    "rentable building area (sf)": "SF",
    "rentable building area": "SF",
}

# ---------------------------------------------------------------------------
# County → internal market tag
# MARKET_LIBRARY in the scoring engine is keyed by these tags.
# ---------------------------------------------------------------------------
_COUNTY_TO_MARKET: dict[str, str] = {
    # Arizona
    "MARICOPA": "PHOENIX",
    "PINAL": "PHOENIX",
    "PIMA": "TUCSON",
    "YAVAPAI": "PHOENIX",
    # Southern California
    "LOS ANGELES": "LOS_ANGELES",
    "ORANGE": "ORANGE_COUNTY",
    "VENTURA": "LOS_ANGELES",
    "SAN DIEGO": "SAN_DIEGO",
    "RIVERSIDE": "INLAND_EMPIRE",
    "SAN BERNARDINO": "INLAND_EMPIRE",
    # Texas (carry-over from scoring engine)
    "DALLAS": "DALLAS",
    "TARRANT": "DALLAS",
    "COLLIN": "DALLAS",
    "DENTON": "DALLAS",
    # Illinois
    "COOK": "CHICAGO",
    "DUPAGE": "CHICAGO",
}

_STATE_FALLBACK: dict[str, str] = {
    "AZ": "PHOENIX",
    "ARIZONA": "PHOENIX",
    "CA": "LOS_ANGELES",
    "CALIFORNIA": "LOS_ANGELES",
    "TX": "DALLAS",
    "TEXAS": "DALLAS",
    "IL": "CHICAGO",
    "ILLINOIS": "CHICAGO",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename: dict[str, str] = {}
    seen_targets: set[str] = set()
    for col in df.columns:
        key = col.strip().lower()
        target = _COLUMN_MAP.get(key)
        if target and target not in seen_targets:
            rename[col] = target
            seen_targets.add(target)
    return df.rename(columns=rename)


def _parse_numeric(series: pd.Series) -> pd.Series:
    """Strip currency symbols, commas, percent signs then coerce to float."""
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"[$,%\s]", "", regex=True)
        .str.replace(",", "", regex=False)
        .pipe(pd.to_numeric, errors="coerce")
    )


def _derive_noi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fill missing NOI via NOI = (cap_rate_pct / 100) × price.
    CoStar stores cap rates as whole-percent numbers (5.25 = 5.25 %).
    """
    if "NOI" not in df.columns:
        df["NOI"] = float("nan")

    mask = df["NOI"].isna()
    if mask.any() and {"Cap Rate", "Price"}.issubset(df.columns):
        cap = df.loc[mask, "Cap Rate"]
        price = df.loc[mask, "Price"]
        derived = (cap / 100.0) * price
        df.loc[mask, "NOI"] = derived
        filled = int(derived.notna().sum())
        if filled:
            print(f"  ↳ Derived NOI from cap rate × price for {filled} row(s).")
    return df


def _assign_market_tag(row: pd.Series) -> str:
    county = str(row.get("County", "")).strip().upper()
    state = str(row.get("State", "")).strip().upper()

    for pattern, tag in _COUNTY_TO_MARKET.items():
        if pattern in county:
            return tag

    return _STATE_FALLBACK.get(state, "DEFAULT")


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------

def load_costar_export(path: Path) -> pd.DataFrame:
    """
    Read a CoStar CSV export and return a normalised DataFrame ready for
    the scoring engine.  Raises ValueError if required columns are absent.
    """
    print(f"📂 Loading: {path.name}")

    # utf-8-sig strips the BOM that Excel/CoStar sometimes prepends.
    df = pd.read_csv(path, thousands=",", encoding="utf-8-sig")
    df.columns = df.columns.str.strip()
    df = _normalize_columns(df)

    required = {"Property Name", "# Units"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Required columns not found after normalisation: {missing}\n"
            f"  Columns seen: {list(df.columns)}\n"
            f"  Tip: check that your CoStar export includes a property-name and"
            f" unit-count column."
        )

    for col in ("# Units", "NOI", "Price", "Cap Rate"):
        if col in df.columns:
            df[col] = _parse_numeric(df[col])

    df = _derive_noi(df)
    df["_market_tag"] = df.apply(_assign_market_tag, axis=1)

    print(f"  ↳ {len(df)} rows, {len(df.columns)} columns.")
    return df


def find_newest_export() -> Path | None:
    EXPORTS_DIR.mkdir(exist_ok=True)
    csvs = sorted(
        EXPORTS_DIR.glob("*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return csvs[0] if csvs else None


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_costar_pipeline(export_path: Path | None = None) -> None:
    import real_estate_scoring_engine as engine

    if export_path is None:
        export_path = find_newest_export()
    if export_path is None:
        print(
            "❌ No CoStar export found.\n"
            "   Drop a CSV into costar_exports/ or pass the file path as an argument."
        )
        sys.exit(1)

    df = load_costar_export(export_path)

    # Pre-fetch macro data once so every property uses the same snapshot.
    treasury_rate = engine.get_live_treasury_rate()

    before = len(df)
    df = df[df["# Units"].notna() & (df["# Units"] >= MIN_UNITS)].copy()
    skipped_size = before - len(df)
    if skipped_size:
        print(f"  ↳ Skipped {skipped_size} properties below {MIN_UNITS}-unit minimum.")

    if df.empty:
        print(f"⚠️  No properties with ≥{MIN_UNITS} units after filtering.")
        return

    result_rows: list[dict] = []

    for _, row in df.iterrows():
        market_tag: str = row["_market_tag"]
        noi = row.get("NOI")
        price = row.get("Price")

        if pd.isna(noi) or float(noi) <= 0:
            print(f"  ⏩ SKIPPED {row['Property Name']!r}: NOI unavailable (no price or cap rate).")
            continue

        market_data = engine.MARKET_LIBRARY.get(market_tag, engine.MARKET_LIBRARY["DEFAULT"])
        live_rg = engine.get_zori_rent_growth(market_tag)
        rent_growth = live_rg if live_rg is not None else market_data.get("rent_growth", 0.03)
        rg_source = "ZORI" if live_rg is not None else "library"

        prop = engine.Property(
            name=str(row["Property Name"]).strip(),
            units=int(row["# Units"]),
            market_tag=market_tag,
        )
        prop.process_institutional_underwriting(
            explicit_noi=float(noi),
            pricing_guidance=float(price) if pd.notna(price) and float(price) > 0 else None,
        )

        result_rows.append({
            "Source File": export_path.name,
            "Property Name": row["Property Name"],
            "# Units": int(row["# Units"]),
            "NOI": noi,
            "Asking Price": price,
            "Market Tag": market_tag,
            "Rent Growth Source": rg_source,
            "Rent Growth %": round(rent_growth * 100, 2),
            "10Y Treasury %": round(treasury_rate * 100, 2),
        })

    if not result_rows:
        print("⚠️  No scoreable deals found (all skipped due to missing NOI).")
        return

    out = pd.DataFrame(result_rows)
    # Append to existing scored_deals.csv if present, otherwise create.
    if OUTPUT_CSV.exists():
        existing = pd.read_csv(OUTPUT_CSV)
        out = pd.concat([existing, out], ignore_index=True).drop_duplicates(
            subset=["Property Name", "Source File"]
        )
    out.to_csv(OUTPUT_CSV, index=False)
    print(f"\n✅ Scored {len(result_rows)} deal(s) → {OUTPUT_CSV.name}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    run_costar_pipeline(path)
