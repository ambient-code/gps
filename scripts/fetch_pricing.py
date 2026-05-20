#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
# ]
# ///
"""Fetch cloud pricing data into data/pricing.db.

Builds a separate SQLite database with pricing for AWS services (EC2, S3, EBS,
ELB, NAT Gateway, Data Transfer) and Claude model token pricing.

AWS: fetches from public pricing bulk JSON files (no auth needed).
Claude: uses published Anthropic per-token pricing (hardcoded, no API needed).

A discount factor (e.g., 0.85 for 15% off) can be stored via --discount.
All list prices are stored at face value; discount is applied in views.

Usage:
    uv run scripts/fetch_pricing.py                      # fetch all
    uv run scripts/fetch_pricing.py --aws-only           # AWS pricing only
    uv run scripts/fetch_pricing.py --claude-only        # Claude model pricing only
    uv run scripts/fetch_pricing.py --regions us-east-1  # single region
    uv run scripts/fetch_pricing.py --discount 0.85      # set 15% discount factor
"""

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "pricing.db"

DEFAULT_REGIONS = ["us-east-1", "us-west-2"]

# Public AWS pricing bulk file URL template (no auth required)
AWS_PRICING_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{service}/current/{region}/index.json"

# AWS services to fetch. EBS is NOT a separate service — it's in the EC2 pricing file.
# NAT Gateway pricing is embedded in EC2 or not available per-region — omitted for now.
AWS_SERVICES = [
    {"service_code": "AmazonEC2", "label": "EC2", "edl_service": "ec2"},
    {"service_code": "AmazonS3", "label": "S3", "edl_service": "s3"},
    {"service_code": "AWSELB", "label": "ELB", "edl_service": "elb"},
    {
        "service_code": "AWSDataTransfer",
        "label": "Data Transfer",
        "edl_service": "data_transfer",
    },
]

# Published Anthropic Claude model pricing (per million tokens, USD)
# Source: https://www.anthropic.com/pricing — update when pricing changes
CLAUDE_PRICING = [
    {"model": "claude-opus-4", "input_per_mtok": 15.00, "output_per_mtok": 75.00},
    {"model": "claude-sonnet-4", "input_per_mtok": 3.00, "output_per_mtok": 15.00},
    {"model": "claude-haiku-4", "input_per_mtok": 0.80, "output_per_mtok": 4.00},
    {"model": "claude-3-5-sonnet", "input_per_mtok": 3.00, "output_per_mtok": 15.00},
    {"model": "claude-3-5-haiku", "input_per_mtok": 0.80, "output_per_mtok": 4.00},
    {"model": "claude-3-opus", "input_per_mtok": 15.00, "output_per_mtok": 75.00},
    {"model": "claude-3-sonnet", "input_per_mtok": 3.00, "output_per_mtok": 15.00},
    {"model": "claude-3-haiku", "input_per_mtok": 0.25, "output_per_mtok": 1.25},
]

# ROSA managed service fees (on top of EC2 infrastructure costs)
# Source: https://aws.amazon.com/rosa/pricing/
ROSA_SERVICE_FEES = [
    {
        "sku": "rosa-worker-fee",
        "description": "ROSA managed service fee per worker node per hour",
        "usage_type": "ROSA-Worker",
        "unit": "Hrs",
        "price_per_unit": 0.171,
    },
    {
        "sku": "rosa-hcp-cluster-fee",
        "description": "ROSA HCP cluster management fee per cluster per hour",
        "usage_type": "ROSA-HCP-Cluster",
        "unit": "Hrs",
        "price_per_unit": 0.25,
    },
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS cloud_pricing (
    pricing_id  INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    service     TEXT NOT NULL,
    region      TEXT NOT NULL,
    sku         TEXT,
    description TEXT,
    instance_type  TEXT,
    instance_family TEXT,
    vcpu        INTEGER,
    memory_gb   REAL,
    storage_type TEXT,
    usage_type  TEXT,
    unit        TEXT NOT NULL,
    price_per_unit REAL NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    effective_date TEXT,
    model_name  TEXT,
    tier_start  REAL,
    tier_end    REAL,
    fetched_at  TEXT NOT NULL,
    UNIQUE(provider, service, region, sku, usage_type, unit, tier_start)
);

CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_pricing_provider_service ON cloud_pricing(provider, service);
CREATE INDEX IF NOT EXISTS idx_pricing_instance_type ON cloud_pricing(instance_type);
CREATE INDEX IF NOT EXISTS idx_pricing_region ON cloud_pricing(region);
CREATE INDEX IF NOT EXISTS idx_pricing_model ON cloud_pricing(model_name);
-- Views (discount_factor from _meta, defaults to 1.0)
CREATE VIEW IF NOT EXISTS v_ec2_ondemand AS
SELECT instance_type, instance_family, vcpu, memory_gb, region,
       price_per_unit AS list_hourly,
       ROUND(price_per_unit * COALESCE(
           (SELECT CAST(value AS REAL) FROM _meta WHERE key = 'discount_factor'), 1.0
       ), 6) AS hourly_price,
       ROUND(price_per_unit * COALESCE(
           (SELECT CAST(value AS REAL) FROM _meta WHERE key = 'discount_factor'), 1.0
       ) * 730, 2) AS monthly_price
FROM cloud_pricing
WHERE provider = 'aws' AND service = 'ec2' AND usage_type = 'OnDemand'
ORDER BY instance_family, vcpu;

CREATE VIEW IF NOT EXISTS v_vertex_ai_pricing AS
SELECT model_name, description, usage_type, unit, price_per_unit,
       tier_start, tier_end
FROM cloud_pricing
WHERE provider = 'anthropic' AND service = 'vertex_ai'
ORDER BY model_name, usage_type;
"""


def init_db() -> sqlite3.Connection:
    """Create pricing.db and apply schema."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    return conn


# ---------------------------------------------------------------------------
# AWS Pricing — public bulk JSON files (no auth)
# ---------------------------------------------------------------------------


def _parse_ec2_product(product: dict) -> dict:
    """Extract EC2-specific attributes from a product."""
    attrs = product.get("attributes", {})
    instance_type = attrs.get("instanceType", "")
    family = instance_type.split(".")[0] if "." in instance_type else ""
    vcpu = None
    memory_gb = None
    try:
        vcpu = int(attrs.get("vcpu", "0"))
    except (ValueError, TypeError):
        pass
    mem_str = attrs.get("memory", "")
    if mem_str and "GiB" in mem_str:
        try:
            memory_gb = float(mem_str.replace(" GiB", "").replace(",", ""))
        except (ValueError, TypeError):
            pass
    return {
        "instance_type": instance_type or None,
        "instance_family": family or None,
        "vcpu": vcpu,
        "memory_gb": memory_gb,
        "storage_type": None,
    }


def _parse_ebs_product(product: dict) -> dict:
    attrs = product.get("attributes", {})
    return {
        "instance_type": None,
        "instance_family": None,
        "vcpu": None,
        "memory_gb": None,
        "storage_type": attrs.get("volumeApiName") or attrs.get("volumeType"),
    }


def _parse_generic_product(_product: dict) -> dict:
    return {
        "instance_type": None,
        "instance_family": None,
        "vcpu": None,
        "memory_gb": None,
        "storage_type": None,
    }


def _should_include_ec2(attrs: dict) -> bool:
    """Filter EC2 products to Linux/Shared/OnDemand only."""
    return (
        attrs.get("operatingSystem") == "Linux"
        and attrs.get("tenancy") == "Shared"
        and attrs.get("preInstalledSw") == "NA"
        and attrs.get("capacitystatus") == "Used"
    )


def _extract_ondemand_prices(terms: dict, sku: str) -> list[dict]:
    """Extract OnDemand price dimensions for a given SKU."""
    results = []
    on_demand = terms.get("OnDemand", {}).get(sku, {})
    for _term_key, term_details in (
        on_demand.items() if isinstance(on_demand, dict) else []
    ):
        for _dim_key, dim in term_details.get("priceDimensions", {}).items():
            price_str = dim.get("pricePerUnit", {}).get("USD", "0")
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            if price == 0.0:
                continue
            results.append(
                {
                    "usage_type": "OnDemand",
                    "unit": dim.get("unit", ""),
                    "price_per_unit": price,
                    "description": dim.get("description", ""),
                    "effective_date": term_details.get("effectiveDate"),
                }
            )
    return results


def fetch_aws_pricing(regions: list[str], now: str) -> list[dict]:
    """Fetch AWS pricing from public bulk JSON files (no auth required)."""
    all_rows: list[dict] = []

    for svc in AWS_SERVICES:
        service_code = svc["service_code"]
        edl_service = svc["edl_service"]
        label = svc["label"]

        if edl_service == "ec2":
            parse_product = _parse_ec2_product
        elif edl_service == "ebs":
            parse_product = _parse_ebs_product
        else:
            parse_product = _parse_generic_product

        for region in regions:
            url = AWS_PRICING_URL.format(service=service_code, region=region)
            print(f"  Fetching {label} pricing for {region}...")
            print(f"    URL: {url}")

            try:
                # Download to temp file to handle large files (EC2 ~150MB/region)
                with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tmp:
                    with httpx.stream(
                        "GET", url, timeout=300, follow_redirects=True
                    ) as resp:
                        resp.raise_for_status()
                        total = 0
                        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                            tmp.write(chunk)
                            total += len(chunk)
                        print(f"    Downloaded {total / 1024 / 1024:.1f} MB")

                    tmp.seek(0)
                    data = json.load(tmp)

            except httpx.HTTPStatusError as e:
                print(f"    ERROR: HTTP {e.response.status_code}", file=sys.stderr)
                continue
            except httpx.RequestError as e:
                print(f"    ERROR: {e}", file=sys.stderr)
                continue
            except json.JSONDecodeError:
                print("    ERROR: invalid JSON response", file=sys.stderr)
                continue

            products = data.get("products", {})
            terms = data.get("terms", {})
            product_count = 0

            for sku, product in products.items():
                attrs = product.get("attributes", {})

                # For EC2, filter aggressively
                if edl_service == "ec2" and not _should_include_ec2(attrs):
                    continue

                product_fields = parse_product(product)
                product_desc = (
                    attrs.get("usagetype", "") + " " + attrs.get("operation", "")
                ).strip()

                for dim in _extract_ondemand_prices(terms, sku):
                    row = {
                        "provider": "aws",
                        "service": edl_service,
                        "region": region,
                        "sku": sku,
                        "description": dim["description"] or product_desc,
                        "usage_type": dim["usage_type"],
                        "unit": dim["unit"],
                        "price_per_unit": dim["price_per_unit"],
                        "currency": "USD",
                        "effective_date": dim.get("effective_date"),
                        "model_name": None,
                        "tier_start": None,
                        "tier_end": None,
                        "fetched_at": now,
                        **product_fields,
                    }
                    all_rows.append(row)
                    product_count += 1

            # Free memory before next service/region
            del data, products, terms
            print(f"    {product_count} price points")

    return all_rows


# ---------------------------------------------------------------------------
# Claude model pricing — hardcoded from public Anthropic pricing
# ---------------------------------------------------------------------------


def fetch_claude_pricing(now: str) -> list[dict]:
    """Generate Claude model pricing rows from published Anthropic rates."""
    rows: list[dict] = []
    for model in CLAUDE_PRICING:
        name = model["model"]
        # Input tokens
        rows.append(
            {
                "provider": "anthropic",
                "service": "vertex_ai",
                "region": "global",
                "sku": f"{name}-input",
                "description": f"{name} input tokens (via Vertex AI)",
                "instance_type": None,
                "instance_family": None,
                "vcpu": None,
                "memory_gb": None,
                "storage_type": None,
                "usage_type": "per-token-input",
                "unit": "1M tokens",
                "price_per_unit": model["input_per_mtok"],
                "currency": "USD",
                "effective_date": now[:10],
                "model_name": name,
                "tier_start": None,
                "tier_end": None,
                "fetched_at": now,
            }
        )
        # Output tokens
        rows.append(
            {
                "provider": "anthropic",
                "service": "vertex_ai",
                "region": "global",
                "sku": f"{name}-output",
                "description": f"{name} output tokens (via Vertex AI)",
                "instance_type": None,
                "instance_family": None,
                "vcpu": None,
                "memory_gb": None,
                "storage_type": None,
                "usage_type": "per-token-output",
                "unit": "1M tokens",
                "price_per_unit": model["output_per_mtok"],
                "currency": "USD",
                "effective_date": now[:10],
                "model_name": name,
                "tier_start": None,
                "tier_end": None,
                "fetched_at": now,
            }
        )
    print(f"    {len(rows)} Claude model price points")
    return rows


# ---------------------------------------------------------------------------
# ROSA service fees — static rates from aws.amazon.com/rosa/pricing/
# ---------------------------------------------------------------------------


def fetch_rosa_service_fees(now: str) -> list[dict]:
    """Generate ROSA service fee rows from published AWS rates."""
    rows: list[dict] = []
    for fee in ROSA_SERVICE_FEES:
        rows.append(
            {
                "provider": "aws",
                "service": "rosa",
                "region": "global",
                "sku": fee["sku"],
                "description": fee["description"],
                "instance_type": None,
                "instance_family": None,
                "vcpu": None,
                "memory_gb": None,
                "storage_type": None,
                "usage_type": fee["usage_type"],
                "unit": fee["unit"],
                "price_per_unit": fee["price_per_unit"],
                "currency": "USD",
                "effective_date": now[:10],
                "model_name": None,
                "tier_start": None,
                "tier_end": None,
                "fetched_at": now,
            }
        )
    print(f"    {len(rows)} ROSA service fee entries")
    return rows


# ---------------------------------------------------------------------------
# Database upserts
# ---------------------------------------------------------------------------


def upsert_pricing(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert cloud pricing rows."""
    count = 0
    for row in rows:
        conn.execute(
            """INSERT INTO cloud_pricing (
                   provider, service, region, sku, description,
                   instance_type, instance_family, vcpu, memory_gb, storage_type,
                   usage_type, unit, price_per_unit, currency, effective_date,
                   model_name, tier_start, tier_end, fetched_at
               ) VALUES (
                   :provider, :service, :region, :sku, :description,
                   :instance_type, :instance_family, :vcpu, :memory_gb, :storage_type,
                   :usage_type, :unit, :price_per_unit, :currency, :effective_date,
                   :model_name, :tier_start, :tier_end, :fetched_at
               )
               ON CONFLICT(provider, service, region, sku, usage_type, unit, tier_start)
               DO UPDATE SET
                   description=excluded.description,
                   instance_type=excluded.instance_type,
                   instance_family=excluded.instance_family,
                   vcpu=excluded.vcpu,
                   memory_gb=excluded.memory_gb,
                   storage_type=excluded.storage_type,
                   price_per_unit=excluded.price_per_unit,
                   currency=excluded.currency,
                   effective_date=excluded.effective_date,
                   model_name=excluded.model_name,
                   tier_end=excluded.tier_end,
                   fetched_at=excluded.fetched_at""",
            row,
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch cloud pricing into pricing.db")
    parser.add_argument(
        "--aws-only", action="store_true", help="Fetch AWS pricing only"
    )
    parser.add_argument(
        "--claude-only", action="store_true", help="Fetch Claude pricing only"
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=DEFAULT_REGIONS,
        help=f"AWS regions (default: {' '.join(DEFAULT_REGIONS)})",
    )
    parser.add_argument(
        "--discount",
        type=float,
        default=None,
        help="Discount factor multiplier (e.g., 0.85 = 15%% off). Stored in _meta.",
    )
    args = parser.parse_args()

    # Determine what to fetch
    fetch_aws = not args.claude_only
    fetch_claude = not args.aws_only

    conn = init_db()
    now = datetime.now(timezone.utc).isoformat()
    errors: list[str] = []

    # Store discount factor if provided
    if args.discount is not None:
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('discount_factor', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(args.discount),),
        )
        conn.commit()
        print(f"Discount factor set to {args.discount}")

    try:
        # --- AWS ---
        if fetch_aws:
            print("\n=== AWS Pricing (public, no auth) ===")
            rows = fetch_aws_pricing(args.regions, now)
            if rows:
                count = upsert_pricing(conn, rows)
                conn.commit()
                print(f"  Total: {count} AWS price points upserted")
                conn.execute(
                    "INSERT INTO _meta (key, value) VALUES ('last_aws_fetch', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now,),
                )
            else:
                errors.append("AWS: no data fetched")

        # --- Claude ---
        if fetch_claude:
            print("\n=== Claude Model Pricing (Anthropic published rates) ===")
            rows = fetch_claude_pricing(now)
            if rows:
                count = upsert_pricing(conn, rows)
                conn.commit()
                print(f"  Total: {count} Claude price points upserted")
                conn.execute(
                    "INSERT INTO _meta (key, value) VALUES ('last_claude_fetch', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now,),
                )

        # --- ROSA service fees ---
        if fetch_aws or (not args.claude_only):
            print("\n=== ROSA Service Fees (aws.amazon.com/rosa/pricing) ===")
            rows = fetch_rosa_service_fees(now)
            if rows:
                count = upsert_pricing(conn, rows)
                conn.commit()
                print(f"  Total: {count} ROSA fee entries upserted")

        conn.commit()

    except KeyboardInterrupt:
        print("\nInterrupted — saving progress...")
        conn.commit()
    finally:
        # Post-build
        print("\n--- Post-build ---")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"  integrity_check: {result}")
        conn.execute("ANALYZE")
        print("  ANALYZE: done")

        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('last_build', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()

    # Summary
    print(f"\nPricing DB written to {DB_PATH}")
    if errors:
        print(f"\nWarnings ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
    else:
        print("All sources fetched successfully.")


if __name__ == "__main__":
    main()
