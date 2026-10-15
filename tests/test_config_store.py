"""D-13: config_store — the change a save makes (config_diff) and how it is written (save_config).

save_config sends only the changed keys to kriya_config_apply (migration 110). Until 110 is applied on a
database, it falls back to the old whole-object write, so deploying the code first breaks nothing.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services import config_store
from app.services.config_store import config_diff, save_config

CID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize("before, after, sets, removes", [
    ({}, {}, [], []),
    ({"a": 1}, {"a": 1}, [], []),
    ({"a": 1}, {"a": 2}, [{"path": ["a"], "value": 2}], []),
    ({"a": 1, "b": 2}, {"a": 1}, [], [["b"]]),
    ({"v": {"x": 1, "y": 2}}, {"v": {"x": 1, "y": 3}}, [{"path": ["v", "y"], "value": 3}], []),
    ({"v": {"x": 1}}, {"v": 5}, [{"path": ["v"], "value": 5}], []),
    ({"v": 5}, {"v": {"x": 1}}, [{"path": ["v"], "value": {"x": 1}}], []),
    ({"w": [1, 2]}, {"w": [1, 2, 3]}, [{"path": ["w"], "value": [1, 2, 3]}], []),   # lists are values
    ({"v": {"x": 1}}, {"v": {}}, [], [["v", "x"]]),
])
def test_config_diff(before, after, sets, removes):
    assert config_diff(before, after) == (sets, removes)


def _client(rpc_result=None, rpc_error=None):
    client = MagicMock()
    if rpc_error:
        client.rpc.return_value.execute.side_effect = rpc_error
    else:
        client.rpc.return_value.execute.return_value = MagicMock(data=rpc_result)
    client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = \
        MagicMock(data=[{"id": CID}])
    client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": CID}])
    return client


def _save(client, table="clinics", before=None, after=None):
    with patch.object(config_store, "supabase", client):
        return asyncio.run(save_config(table, CID, CID, before or {"a": 1, "b": 2}, after or {"a": 9, "b": 2}))


def test_only_the_change_is_sent():
    client = _client(rpc_result={"a": 9, "b": 2, "c": "from someone else"})
    assert _save(client) == {"a": 9, "b": 2, "c": "from someone else"}
    name, args = client.rpc.call_args.args
    assert name == "kriya_config_apply" and args == {
        "p_table": "clinics", "p_id": CID, "p_clinic_id": CID,
        "p_sets": [{"path": ["a"], "value": 9}], "p_removes": []}
    client.table.assert_not_called()


def test_no_change_writes_nothing():
    client = _client()
    assert _save(client, after={"a": 1, "b": 2}) == {"a": 1, "b": 2}
    client.rpc.assert_not_called() and client.table.assert_not_called()


def test_a_row_that_is_not_there_returns_none():
    assert _save(_client(rpc_result=None)) is None


@pytest.mark.parametrize("table", ["clinics", "branches"])
def test_without_migration_110_the_old_whole_write_is_used(table):
    client = _client(rpc_error=Exception("{'code': 'PGRST202', 'message': 'Could not find the function "
                                         "public.kriya_config_apply'}"))
    assert _save(client, table=table) == {"a": 9, "b": 2}
    client.table.assert_called_with(table)
    assert client.table.return_value.update.call_args.args == ({"config": {"a": 9, "b": 2}},)


def test_any_other_error_is_raised_not_hidden():
    with pytest.raises(RuntimeError):
        _save(_client(rpc_error=RuntimeError("connection reset")))


def test_an_unknown_table_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(save_config("appointments", CID, CID, {}, {"a": 1}))
