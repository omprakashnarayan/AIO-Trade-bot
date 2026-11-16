"""Contract fixtures for authenticated read results (no broker credentials or network)."""
import json

import pytest

from portfolio_tracker.brokers.base import BrokerAdapterError
from portfolio_tracker.db import Database
from portfolio_tracker.provider_data import ProviderSyncService, normalize_provider_snapshot
from portfolio_tracker.provider_mcp import MCPClient, MCPProviderAdapter


class Response:
    headers = {}
    def __init__(self, result): self.result = result
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.result).encode()


class Opener:
    def __init__(self, tools, results):
        self.tools, self.results, self.calls = tools, iter(results), []
    def open(self, req, timeout):
        payload = json.loads(req.data)
        self.calls.append(payload)
        method = payload['method']
        result = {'initialize': {}, 'tools/list': {'tools': self.tools}}.get(method)
        if method == 'tools/call': result = next(self.results)
        return Response({'jsonrpc': '2.0', 'id': payload.get('id'), 'result': result})


def text(data):
    return {'content': [{'type': 'text', 'text': json.dumps(data)}]}


def adapter(provider, tools, results):
    opener = Opener(tools, results)
    client = MCPClient('https://example.test/mcp', provider=provider, opener=opener)
    return MCPProviderAdapter(provider=provider, broker_id=provider, account_id='acct', client=client), opener


def tool(name, properties=None):
    return {'name': name, 'inputSchema': {'type': 'object', 'properties': properties or {}, 'additionalProperties': False}}


@pytest.mark.parametrize('provider,names,payloads', [
    ('kite', ('get_holdings', 'get_positions'), ([{'tradingsymbol':'INFY','quantity':2,'average_price':100}], {'net':[{'tradingsymbol':'TCS','quantity':-1,'average_price':300}], 'day':[]})),
    ('upstox', ('get-holdings', 'get-positions'), ({'status':'success','data':[{'trading_symbol':'INFY','quantity':2,'average_price':100}]}, {'status':'success','data':[{'trading_symbol':'TCS','quantity':-1,'average_price':300}]})),
])
def test_published_read_pairs_preserve_distinct_rows_and_scope(tmp_path, provider, names, payloads):
    a, opener = adapter(provider, [tool(n) for n in names], [text(v) for v in payloads])
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    result = ProviderSyncService(db).sync(a)
    assert result['status'] == 'succeeded', result
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id=provider, account_id='acct')] == ['INFY']
    with db.connect() as conn:
        rows = conn.execute('SELECT symbol,quantity,source FROM provider_positions WHERE broker_id=? AND account_id=?', (provider,'acct')).fetchall()
    assert [(r['symbol'], r['quantity'], r['source']) for r in rows] == [('TCS','-1',provider)]
    assert [c['params']['name'] for c in opener.calls if c['method']=='tools/call'] == list(names)


@pytest.mark.parametrize('payload', [{}, {'status':'error','data':[{'symbol':'INFY','quantity':2}]}, {'status':'success','data':[]}, {'status':'success','data':[{'symbol':'INFY'}]}, {'data':{'holdings':[]}}, {'holdings':[{'symbol':'INFY','quantity':1}]}])
def test_malformed_empty_and_partial_never_persist_or_succeed(tmp_path, payload):
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    a, _ = adapter('upstox', [tool('get-holdings'),tool('get-positions')], [text(payload), text({'status':'success','data':[{'trading_symbol':'TCS','quantity':1}]})])
    result = ProviderSyncService(db).sync(a)
    assert result['status'] == 'error'
    assert db.list_provider_holdings(broker_id='upstox', account_id='acct') == []
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM provider_reconciliations').fetchone()[0] == 0


def test_missing_read_tool_and_write_tool_are_not_executed(tmp_path):
    a, opener = adapter('upstox', [tool('get-holdings'),tool('place_order'), tool('login')], [text({'status':'success','data':[{'symbol':'INFY','quantity':2}]})])
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    assert ProviderSyncService(db).sync(a)['status'] == 'error'
    assert not [c for c in opener.calls if c['method']=='tools/call']
    assert not a.client.tools.keys() & {'place_order','login'}


def test_kite_numeric_pagination_uses_discovered_schema_and_collects_pages(tmp_path):
    args = {'from': {'type':'number'}, 'limit': {'type':'number'}}
    a, opener = adapter('kite', [tool('get_holdings', args),tool('get_positions')], [
        text([{'tradingsymbol':'INFY','quantity':2}]),text([]),text({'net':[{'tradingsymbol':'TCS','quantity':1}],'day':[]})])
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    assert ProviderSyncService(db).sync(a)['status'] == 'succeeded'
    calls = [c['params'] for c in opener.calls if c['method']=='tools/call']
    assert calls[0]['arguments'] == {'from':0,'limit':100}
    assert calls[1]['arguments'] == {'from':1,'limit':100}


def test_indmoney_discovery_does_not_infer_undocumented_response_shape(tmp_path):
    a, opener = adapter('indmoney',[tool('networth_snapshot'),tool('networth_holdings')], [text({'total_networth':100}),text({'holdings':[{'symbol':'INFY','quantity':1}]})])
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    assert ProviderSyncService(db).sync(a)['status'] == 'error'
    assert db.list_provider_holdings(broker_id='indmoney', account_id='acct') == []
    assert not [c for c in opener.calls if c.get('params', {}).get('name') in {'login','place_order'}]


def test_reconcile_removes_stale_rows_only_in_complete_snapshot_scope(tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    first = normalize_provider_snapshot('kite','kite','acct',{'holdings':[{'symbol':'OLD','quantity':1}],'positions':[{'symbol':'POS','quantity':1}]})
    db.upsert_provider_snapshot(first)
    second = normalize_provider_snapshot('kite','kite','acct',{'holdings':[{'symbol':'NEW','quantity':1}],'positions':[{'symbol':'POS','quantity':2}]})
    db.upsert_provider_snapshot(second)
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id='kite',account_id='acct')] == ['NEW']
    with db.connect() as conn:
        assert conn.execute('SELECT quantity FROM provider_positions WHERE broker_id=? AND account_id=?', ('kite','acct')).fetchone()[0] == '2'


def test_incomplete_snapshot_does_not_delete_prior_rows(tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    complete = normalize_provider_snapshot('kite', 'kite', 'acct', {'holdings':[{'symbol':'OLD','quantity':1}], 'positions':[{'symbol':'POS','quantity':1}]})
    db.upsert_provider_snapshot(complete)
    other = normalize_provider_snapshot('kite', 'kite', 'other', {'holdings':[{'symbol':'OTHER','quantity':1}], 'positions':[{'symbol':'OTHERPOS','quantity':1}]})
    db.upsert_provider_snapshot(other)
    incomplete = normalize_provider_snapshot('kite', 'kite', 'acct', {'holdings':[{'symbol':'NEW','quantity':1}]})
    db.upsert_provider_snapshot(incomplete)
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id='kite', account_id='acct')] == ['NEW', 'OLD']
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id='kite', account_id='other')] == ['OTHER']
    with db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT symbol FROM provider_positions WHERE broker_id='kite' AND account_id='acct'")] == ['POS']


@pytest.mark.parametrize('provider,names,payloads', [
    ('kite', ('get_holdings','get_positions'), ([{'tradingsymbol':'INFY','quantity':2}, {'tradingsymbol':'BAD'}], {'net':[{'tradingsymbol':'TCS','quantity':1}]})),
    ('upstox', ('get-holdings','get-positions'), ({'status':'success','data':[{'trading_symbol':'INFY','quantity':2}]}, {'status':'error','data':[{'trading_symbol':'TCS','quantity':1}]})),
    ('upstox', ('get-holdings','get-positions'), ({'status':'success','data':[{'trading_symbol':'INFY','quantity':2}]}, {'status':'success','data':[{'trading_symbol':'TCS'}]})),
])
def test_bad_row_or_second_tool_failure_preserves_existing_snapshot(tmp_path, provider, names, payloads):
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    db.upsert_provider_snapshot(normalize_provider_snapshot(provider, provider, 'acct', {'holdings':[{'symbol':'OLD','quantity':1}], 'positions':[{'symbol':'POS','quantity':1}]}))
    a, _ = adapter(provider, [tool(n) for n in names], [text(v) for v in payloads])
    assert ProviderSyncService(db).sync(a)['status'] == 'error'
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id=provider, account_id='acct')] == ['OLD']
    with db.connect() as conn:
        assert [r[0] for r in conn.execute('SELECT symbol FROM provider_positions WHERE broker_id=? AND account_id=?', (provider,'acct'))] == ['POS']
        assert conn.execute('SELECT count(*) FROM provider_reconciliations').fetchone()[0] == 0


def test_empty_positions_read_is_not_a_complete_sync(tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    a, _ = adapter('kite', [tool('get_holdings'), tool('get_positions')], [text([{'tradingsymbol':'INFY','quantity':2}]), text({'net':[], 'day':[]})])
    assert ProviderSyncService(db).sync(a)['status'] == 'error'
    assert db.list_provider_holdings(broker_id='kite', account_id='acct') == []


def test_complete_sync_prunes_only_matching_account(tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.init_db()
    for account in ('acct', 'other'):
        db.upsert_provider_snapshot(normalize_provider_snapshot('kite','kite',account,{'holdings':[{'symbol':'OLD','quantity':1}],'positions':[{'symbol':'POS','quantity':1}]}))
    a, _ = adapter('kite', [tool('get_holdings'), tool('get_positions')], [text([{'tradingsymbol':'NEW','quantity':2}]), text({'net':[{'tradingsymbol':'TCS','quantity':1}]})])
    assert ProviderSyncService(db).sync(a)['status'] == 'succeeded'
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id='kite',account_id='acct')] == ['NEW']
    assert [r['symbol'] for r in db.list_provider_holdings(broker_id='kite',account_id='other')] == ['OLD']
