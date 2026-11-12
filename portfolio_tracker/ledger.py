"""Normalized financial ledger, deterministic FIFO lots, and tax exports."""
from __future__ import annotations
import csv, io, json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

EVENT_TYPES = {"buy","sell","dividend","interest","fee","tax","deposit","withdrawal","split","transfer","corporate_action"}
NAMESPACES = {"manual","broker","indmoney","paper"}
D = Decimal

def dec(value: Any, field: str = "value", default: str = "0") -> Decimal:
    try: v = D(default if value is None or value == "" else str(value))
    except (InvalidOperation, TypeError, ValueError): raise ValueError(f"{field} must be a valid decimal") from None
    if not v.is_finite(): raise ValueError(f"{field} must be finite")
    return v

def iso_date(value: Any) -> str:
    if value in (None, ""): return date.today().isoformat()
    text = str(value).replace("Z", "+00:00")
    try: return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        try: return date.fromisoformat(str(value)).isoformat()
        except ValueError: raise ValueError("event_date must be ISO date or timestamp") from None

def q(v: Decimal) -> str: return format(v, "f")
def fx_value(amount: Decimal, rate: Decimal | None) -> Decimal: return amount if rate is None else amount * rate

@dataclass(frozen=True)
class FinancialEvent:
    event_type: str
    namespace: str = "manual"
    portfolio: str = "Main"
    symbol: str = ""
    quantity: Decimal = D("0")
    price: Decimal = D("0")
    gross_amount: Decimal = D("0")
    fees: Decimal = D("0")
    taxes: Decimal = D("0")
    currency: str = "INR"
    event_date: str = ""
    source: str = "manual"
    external_id: str | None = None
    broker_id: str = ""
    account_id: str = ""
    portfolio_id: str = ""
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    fx_as_of: str | None = None
    source_timestamp: str | None = None
    metadata: dict[str, Any] | None = None

    def normalized(self) -> "FinancialEvent":
        if self.event_type not in EVENT_TYPES: raise ValueError("unsupported event_type")
        if self.namespace not in NAMESPACES: raise ValueError("unsupported namespace")
        currency = self.currency.upper()
        if currency not in {"INR", "USD"}: raise ValueError("currency must be INR or USD")
        qty, price, gross = dec(self.quantity,"quantity"), dec(self.price,"price"), dec(self.gross_amount,"gross_amount")
        fees, taxes = dec(self.fees,"fees"), dec(self.taxes,"taxes")
        if qty < 0 or fees < 0 or taxes < 0: raise ValueError("quantity, fees, and taxes must be nonnegative")
        if self.event_type in {"buy","sell","split"} and qty <= 0: raise ValueError("quantity must be positive")
        if self.event_type in {"buy","sell"} and price <= 0: raise ValueError("price must be positive")
        if gross == 0 and price and qty: gross = qty * price
        return FinancialEvent(self.event_type,self.namespace,self.portfolio.strip() or "Main",self.symbol.strip().upper(),qty,price,gross,fees,taxes,currency,iso_date(self.event_date),self.source.strip() or self.namespace,self.external_id,self.broker_id,self.account_id,self.portfolio_id,self.fx_rate,self.fx_source,self.fx_as_of,self.source_timestamp,self.metadata or {})

def _event_from_row(row: Any) -> FinancialEvent:
    return FinancialEvent(row["event_type"],row["namespace"],row["portfolio"],row["symbol"],D(row["quantity"]),D(row["price"]),D(row["gross_amount"]),D(row["fees"]),D(row["taxes"]),row["currency"],row["event_date"],row["source"],row["external_id"],row["broker_id"],row["account_id"],row["portfolio_id"],D(row["fx_rate"]) if row["fx_rate"] else None,row["fx_source"],row["fx_as_of"],row["source_timestamp"],json.loads(row["metadata_json"]))

def record_event(db: Any, event: FinancialEvent) -> int:
    e = event.normalized(); now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        if e.external_id is not None:
            old = conn.execute("SELECT id FROM financial_events WHERE namespace=? AND source=? AND external_id=?", (e.namespace,e.source,e.external_id)).fetchone()
            if old: return int(old[0])
        cur = conn.execute("INSERT INTO financial_events(namespace,broker_id,account_id,portfolio_id,portfolio,symbol,event_type,quantity,price,gross_amount,fees,taxes,currency,fx_rate,fx_source,fx_as_of,event_date,source,source_timestamp,external_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (e.namespace,e.broker_id,e.account_id,e.portfolio_id,e.portfolio,e.symbol,e.event_type,q(e.quantity),q(e.price),q(e.gross_amount),q(e.fees),q(e.taxes),e.currency,q(e.fx_rate) if e.fx_rate is not None else None,e.fx_source,e.fx_as_of,e.event_date,e.source,e.source_timestamp,e.external_id,json.dumps(e.metadata or {},sort_keys=True,separators=(",",":")),now))
        event_id = int(cur.lastrowid)
    return event_id

def list_events(db: Any, **scope: Any) -> list[Any]:
    clauses, params = [], []
    for key, value in scope.items():
        if value is not None: clauses.append(f"{key}=?"); params.append(value)
    with db.connect() as conn: return conn.execute("SELECT * FROM financial_events" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY event_date,id", params).fetchall()

def calculate(db: Any, *, namespace: str = "manual", portfolio: str | None = None, symbol: str | None = None, as_of: str | None = None, long_term_days: int = 365, market_prices: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = list_events(db, namespace=namespace, portfolio=portfolio, symbol=symbol)
    rows = [r for r in rows if as_of is None or r["event_date"] <= iso_date(as_of)]
    lots: list[dict[str,Any]] = []; matches=[]; realized=D("0"); income=D("0"); fees=D("0"); taxes=D("0")
    for row in rows:
        e=_event_from_row(row); rate=e.fx_rate
        fees += fx_value(e.fees,rate); taxes += fx_value(e.taxes,rate)
        if e.event_type in {"dividend","interest"}: income += fx_value(e.gross_amount,rate); continue
        if e.event_type == "buy":
            lots.append({"event_id":row["id"],"symbol":e.symbol,"date":e.event_date,"qty":e.quantity,"unit":(e.gross_amount+e.fees+e.taxes)/e.quantity,"currency":e.currency,"rate":rate}); continue
        if e.event_type == "split":
            for lot in lots:
                if lot["symbol"] == e.symbol: lot["qty"] *= e.price; lot["unit"] /= e.price
            continue
        if e.event_type != "sell": continue
        remaining=e.quantity; proceeds=fx_value(e.gross_amount-e.fees-e.taxes,rate)
        for lot in lots:
            if remaining <= 0 or lot["symbol"] != e.symbol or lot["qty"] <= 0: continue
            used=min(remaining,lot["qty"]); cost=fx_value(used*lot["unit"],lot["rate"]); pd=(date.fromisoformat(e.event_date)-date.fromisoformat(lot["date"])).days; period="long" if pd >= long_term_days else "short"
            matches.append({"sell_event_id":row["id"],"lot_event_id":lot["event_id"],"quantity":q(used),"cost_basis":q(cost),"proceeds":q(proceeds*used/e.quantity),"realized_pl":q(proceeds*used/e.quantity-cost),"holding_days":pd,"holding_period":period}); realized += proceeds*used/e.quantity-cost; lot["qty"]-=used; remaining-=used
        if remaining > 0: raise ValueError(f"sell exceeds available FIFO lots for {e.symbol}")
    unrealized=D("0")
    for lot in lots:
        if lot["qty"] > 0 and market_prices and lot["symbol"] in market_prices:
            unrealized += fx_value(lot["qty"] * (dec(market_prices[lot["symbol"]], "market_price") - lot["unit"]), lot["rate"])
    return {"realized_pl":q(realized),"unrealized_pl":q(unrealized),"income":q(income),"fees":q(fees),"taxes":q(taxes),"lots":[{"event_id":lot["event_id"],"symbol":lot["symbol"],"date":lot["date"],"remaining_quantity":q(lot["qty"]),"unit_cost":q(lot["unit"]),"currency":lot["currency"],"fx_rate":q(lot["rate"]) if lot["rate"] is not None else None} for lot in lots if lot["qty"]>0],"matches":matches}

def materialize_lots(db: Any, **filters: Any) -> dict[str,Any]:
    result=calculate(db,**filters)
    with db.connect() as conn:
        for lot in result["lots"]:
            existing=conn.execute("SELECT id FROM tax_lots WHERE event_id=? AND namespace=? AND portfolio=? AND symbol=?",(lot["event_id"],filters.get("namespace","manual"),filters.get("portfolio") or "Main",lot["symbol"])).fetchone()
            if not existing:
                row=conn.execute("SELECT * FROM financial_events WHERE id=?",(lot["event_id"],)).fetchone(); e=_event_from_row(row)
                conn.execute("INSERT INTO tax_lots(event_id,namespace,broker_id,account_id,portfolio,symbol,acquired_date,quantity,remaining_quantity,unit_cost,currency,fx_rate,fx_source,fx_as_of,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(lot["event_id"],e.namespace,e.broker_id,e.account_id,e.portfolio,e.symbol,e.event_date,lot["remaining_quantity"],lot["remaining_quantity"],lot["unit_cost"],e.currency,q(e.fx_rate) if e.fx_rate else None,e.fx_source,e.fx_as_of,datetime.now(timezone.utc).isoformat()))
        for m in result["matches"]:
            if not conn.execute("SELECT 1 FROM lot_matches WHERE sell_event_id=? AND lot_id IN (SELECT id FROM tax_lots WHERE event_id=?)",(m["sell_event_id"],m["lot_event_id"])).fetchone():
                lid=conn.execute("SELECT id FROM tax_lots WHERE event_id=? AND namespace=? ORDER BY id LIMIT 1",(m["lot_event_id"],filters.get("namespace","manual"))).fetchone()[0]; conn.execute("INSERT INTO lot_matches(sell_event_id,lot_id,quantity,cost_basis,proceeds,realized_pl,holding_days,holding_period) VALUES(?,?,?,?,?,?,?,?)",(m["sell_event_id"],lid,m["quantity"],m["cost_basis"],m["proceeds"],m["realized_pl"],m["holding_days"],m["holding_period"]))
    return result

def import_rows(db: Any, rows: Iterable[dict[str,Any]], *, namespace: str = "manual", source: str = "import", preview: bool = False) -> dict[str,Any]:
    accepted=0; errors=[]; ids=[]; rows=list(rows)
    for i, raw in enumerate(rows,1):
        try:
            payload=dict(raw); payload["namespace"]=namespace; payload.setdefault("source",source); e=FinancialEvent(**payload).normalized(); ids.append(e)
            if not preview: record_event(db,e)
            accepted+=1
        except (TypeError,ValueError,KeyError) as exc: errors.append({"row":i,"error":str(exc)})
    return {"row_count":len(rows),"accepted":accepted,"rejected":len(errors),"errors":errors,"preview":preview}

def export_data(db: Any, *, fmt: str="json", **filters: Any) -> str:
    rows=[dict(r) for r in list_events(db,**filters)]
    if fmt.lower()=="json": return json.dumps(rows,sort_keys=True,default=str)
    if fmt.lower()!="csv": raise ValueError("format must be csv or json")
    out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=list(rows[0]) if rows else ["id"]); writer.writeheader(); writer.writerows(rows); return out.getvalue()
