"""Create the mock Facets schema in Unity Catalog and seed synthetic members.

    setup-facets --schema workspace.facets_qa --members 20000
"""

from __future__ import annotations

import argparse
from datetime import date

from facets_qa.facets.seed import generate_member_rows
from facets_qa.lakehouse.common import DEFAULT_SCHEMA, get_spark, insert_rows, run_sql_file


def setup(spark, schema: str, members: int, service_date: date, seed: int = 270) -> dict[str, int]:
    run_sql_file(spark, "databricks_facets_ddl.sql", schema)
    run_sql_file(spark, "dashboard_views.sql", schema)
    subsc, meme, elig = generate_member_rows(members, service_date, seed)
    counts = {
        "cmc_sbsb_subsc": insert_rows(spark, f"{schema}.cmc_sbsb_subsc", subsc, overwrite=True),
        "cmc_meme_member": insert_rows(spark, f"{schema}.cmc_meme_member", meme, overwrite=True),
        "cmc_mepe_prcs_elig": insert_rows(spark, f"{schema}.cmc_mepe_prcs_elig", elig, overwrite=True),
    }
    return counts


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--schema", default=DEFAULT_SCHEMA)
    p.add_argument("--members", type=int, default=20_000)
    p.add_argument("--service-date", type=date.fromisoformat, default=date.today())
    args = p.parse_args()
    counts = setup(get_spark(), args.schema, args.members, args.service_date)
    for table, n in counts.items():
        print(f"{args.schema}.{table}: {n} rows")


if __name__ == "__main__":
    main()
