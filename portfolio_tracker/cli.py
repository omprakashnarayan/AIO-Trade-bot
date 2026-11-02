from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings, save_settings
from .db import Database
from .reporting import generate_report
from .imports.indmoney import INDMONEY_US_PORTFOLIO, import_indmoney_csv, import_indmoney_records
from .web import main as run_web


def cmd_init(_args):
    db = Database()
    db.init_db()
    load_settings()
    print(f"Initialized {db.path}")


def cmd_set(args):
    settings = load_settings()
    settings[args.key] = args.value
    save_settings(settings)
    print(json.dumps(settings, indent=2))


def cmd_holdings(args):
    db = Database()
    db.init_db()
    db.upsert_holding(
        args.symbol,
        args.label or "",
        float(args.shares),
        float(args.avg_cost),
        args.notes or "",
        portfolio=args.portfolio,
        currency=args.currency,
    )
    print(f"Saved holding {args.symbol.upper()} in {args.portfolio}")


def cmd_tx(args):
    db = Database()
    db.init_db()
    db.adjust_holding(
        args.symbol,
        float(args.shares),
        float(args.price),
        args.side,
        fees=float(args.fees or 0),
        trade_date=args.trade_date,
        notes=args.notes or "",
        portfolio=args.portfolio,
        currency=args.currency,
    )
    print(f"Saved transaction for {args.symbol.upper()} in {args.portfolio}")


def cmd_refresh(_args):
    _, markdown, path = generate_report(Database(), save=True)
    print(markdown)
    if path:
        print(f"\nSaved report to {path}")


def cmd_report(_args):
    _, markdown, _ = generate_report(Database(), save=True)
    print(markdown)


def cmd_list(_args):
    db = Database()
    db.init_db()
    rows = db.list_holdings()
    if not rows:
        print("No holdings yet")
        return
    for row in rows:
        print(
            f"{row['portfolio']}: {row['symbol']} shares={row['shares']} avg_cost={row['avg_cost']} currency={row['currency']} label={row['label']}"
        )


def cmd_import_indmoney(args):
    db = Database()
    if args.csv:
        result = import_indmoney_csv(db, args.csv, source=args.source, portfolio=args.portfolio)
    else:
        raw = Path(args.records).read_text(encoding="utf-8") if Path(args.records).is_file() else args.records
        records = json.loads(raw)
        if not isinstance(records, list):
            raise ValueError("--records must contain a JSON array")
        result = import_indmoney_records(db, records, source=args.source, portfolio=args.portfolio)
    print(json.dumps({"portfolio": result.portfolio, "imported": result.imported, "skipped": result.skipped}, sort_keys=True))


def cmd_serve(args):
    run_web(host=args.host, port=args.port, debug=args.debug)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio-tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialize local db/settings").set_defaults(func=cmd_init)

    p = sub.add_parser("set", help="set a config value")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("holding", help="create/update a holding")
    p.add_argument("symbol")
    p.add_argument("shares")
    p.add_argument("avg_cost")
    p.add_argument("--portfolio", default="Main")
    p.add_argument("--currency", default="INR")
    p.add_argument("--label")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_holdings)

    p = sub.add_parser("tx", help="add a transaction")
    p.add_argument("symbol")
    p.add_argument("side", choices=["buy", "sell"])
    p.add_argument("shares")
    p.add_argument("price")
    p.add_argument("--portfolio", default="Main")
    p.add_argument("--currency", default="INR")
    p.add_argument("--fees", default=0)
    p.add_argument("--trade-date", dest="trade_date")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_tx)

    p = sub.add_parser("import-indmoney", help="import local INDmoney US CSV or JSON records")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="CSV path or CSV text")
    source.add_argument("--records", help="JSON array or JSON file of manual records")
    p.add_argument("--source", default="indmoney-manual", help="provenance source label")
    p.add_argument("--portfolio", choices=[INDMONEY_US_PORTFOLIO], default=INDMONEY_US_PORTFOLIO, help="dedicated INDmoney US portfolio")
    p.set_defaults(func=cmd_import_indmoney)

    sub.add_parser("list", help="list holdings").set_defaults(func=cmd_list)
    sub.add_parser("refresh", help="fetch prices and save report").set_defaults(func=cmd_refresh)
    sub.add_parser("report", help="print a markdown report").set_defaults(func=cmd_report)

    p = sub.add_parser("serve", help="run the local web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5055)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
