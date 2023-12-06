# Copyright (c) Meta Platforms, Inc. and affiliates.

"""
The default store for accessing persistent data on OMM.
"""
import time
import typing as t

import flask
import flask_migrate
from sqlalchemy import select, delete, func, Select
from sqlalchemy.sql.expression import ClauseElement, Executable
from sqlalchemy.ext.compiler import compiles

from threatexchange.utils import dataclass_json
from threatexchange.signal_type.pdq.signal import PdqSignal
from threatexchange.signal_type.md5 import VideoMD5Signal
from threatexchange.exchanges.signal_exchange_api import (
    TSignalExchangeAPICls,
    TSignalExchangeAPI,
)
from threatexchange.signal_type.index import SignalTypeIndex
from threatexchange.signal_type.signal_base import SignalType
from threatexchange.exchanges.fetch_state import (
    FetchCheckpointBase,
    CollaborationConfigBase,
)

from OpenMediaMatch.storage.postgres import database, flask_utils
from OpenMediaMatch.storage import interface
from OpenMediaMatch.storage.mocked import MockedUnifiedStore
from OpenMediaMatch.storage.interface import (
    SignalTypeConfig,
    BankContentConfig,
)

from flask import current_app


class DefaultOMMStore(interface.IUnifiedStore):
    """
    The default store for accessing persistent data on OMM.

    During the initial development, the storage is mostly mocked, but
    that will go away as implementation progresses.

    In implementation, don't refer to DefaultOMMStore directly, but
    instead to the interfaces to allow future authors more ease in
    extending.

    Data is stored in a combination of:
      * Static config set by deployment (e.g. installed SignalTypes)
      * PostGres-backed tables (e.g. info downloaded from external APIs)
      * Blobstore (e.g. built indices)
    """

    signal_types: list[t.Type[SignalType]]

    def __init__(self, signal_types: list[t.Type[SignalType]]) -> None:
        self.signal_types = signal_types
        if signal_types is not None:
            assert isinstance(signal_types, list)
            for element in signal_types:
                assert issubclass(element, SignalType)
            self.signal_types = signal_types
        assert len(self.signal_types) == len(
            {s.get_name() for s in self.signal_types}
        ), "All signal must have unique names"

    def is_ready(self) -> bool:
        """
        Whether we have finished pre-loading indices.
        """
        return True

    def get_content_type_configs(self) -> t.Mapping[str, interface.ContentTypeConfig]:
        # TODO
        return MockedUnifiedStore().get_content_type_configs()

    def exchange_get_type_configs(self) -> t.Mapping[str, TSignalExchangeAPICls]:
        # TODO
        return MockedUnifiedStore().exchange_get_type_configs()

    def exchange_get_api_instance(self, api_cls_name: str) -> TSignalExchangeAPI:
        # TODO
        return MockedUnifiedStore().exchange_get_api_instance(api_cls_name)

    def get_signal_type_configs(self) -> t.Mapping[str, SignalTypeConfig]:
        # If a signal is installed, then it is enabled by default. But it may be disabled by an
        # override in the database.
        signal_type_overrides = self._query_signal_type_overrides()
        get_enabled_ratio = (
            lambda s: signal_type_overrides[s.get_name()]
            if s.get_name() in signal_type_overrides
            else 1.0
        )
        return {
            s.get_name(): interface.SignalTypeConfig(
                # Note - we do this logic here because this function is re-executed each request
                get_enabled_ratio(s),
                s,
            )
            for s in self.signal_types
        }

    def _create_or_update_signal_type_override(
        self, signal_type: str, enabled_ratio: float
    ) -> None:
        """Create or update database entry for a signal type, setting a new value."""
        db_record = database.db.session.execute(
            select(database.SignalTypeOverride).where(
                database.SignalTypeOverride.name == signal_type
            )
        ).scalar_one_or_none()
        if db_record is not None:
            db_record.enabled_ratio = enabled_ratio
        else:
            database.db.session.add(
                database.SignalTypeOverride(
                    name=signal_type, enabled_ratio=enabled_ratio
                )
            )

        database.db.session.commit()

    @staticmethod
    def _query_signal_type_overrides() -> dict[str, float]:
        db_records = database.db.session.execute(
            select(database.SignalTypeOverride)
        ).all()
        return {record.name: record.enabled_ratio for record, in db_records}

    # Index
    def get_signal_type_index(
        self, signal_type: type[SignalType]
    ) -> t.Optional[SignalTypeIndex[int]]:
        db_record = database.db.session.execute(
            select(database.SignalIndex).where(
                database.SignalIndex.signal_type == signal_type.get_name()
            )
        ).scalar_one_or_none()

        return db_record.deserialize_index() if db_record is not None else None

    def store_signal_type_index(
        self,
        signal_type: t.Type[SignalType],
        index: SignalTypeIndex,
        checkpoint: interface.SignalTypeIndexBuildCheckpoint,
    ) -> None:
        db_record = database.db.session.execute(
            select(database.SignalIndex).where(
                database.SignalIndex.signal_type == signal_type.get_name()
            )
        ).scalar_one_or_none()
        if db_record is None:
            db_record = database.SignalIndex(
                signal_type=signal_type.get_name(),
            )
            database.db.session.add(db_record)
        db_record.serialize_index(index).update_checkpoint(checkpoint)
        database.db.session.commit()

    def get_last_index_build_checkpoint(
        self, signal_type: t.Type[SignalType]
    ) -> t.Optional[interface.SignalTypeIndexBuildCheckpoint]:
        row = database.db.session.execute(
            select(
                database.SignalIndex.updated_to_ts,
                database.SignalIndex.updated_to_id,
                database.SignalIndex.signal_count,
            ).where(database.SignalIndex.signal_type == signal_type.get_name())
        ).one_or_none()

        if row is None:
            return None
        updated_to_ts, updated_to_id, total_count = row._tuple()
        return interface.SignalTypeIndexBuildCheckpoint(
            last_item_timestamp=updated_to_ts,
            last_item_id=updated_to_id,
            total_hash_count=total_count,
        )

    # Collabs
    def exchange_update(
        self, cfg: CollaborationConfigBase, *, create: bool = False
    ) -> None:
        if create:
            exchange = database.CollaborationConfig()
        else:
            exchange = database.db.session.execute(
                select(database.CollaborationConfig)
            ).scalar_one()
        exchange.set_typed_config(cfg)
        database.db.session.add(exchange)
        database.db.session.commit()

    def exchange_delete(self, name: str) -> None:
        database.db.session.execute(
            delete(database.CollaborationConfig).where(
                database.CollaborationConfig.name == name
            )
        )
        database.db.session.commit()

    def exchanges_get(self) -> t.Dict[str, CollaborationConfigBase]:
        types = self.exchange_get_type_configs()

        results = database.db.session.execute(
            select(database.CollaborationConfig)
        ).scalars()

        return {cfg.name: cfg.as_storage_iface_cls(types) for cfg in results}

    def _exchange_get_cfg(self, name: str) -> t.Optional[database.CollaborationConfig]:
        return database.db.session.execute(
            select(database.CollaborationConfig).where(
                database.CollaborationConfig.name == name
            )
        ).scalar_one_or_none()

    def exchange_get_fetch_status(self, name: str) -> interface.FetchStatus:
        collab_config = self._exchange_get_cfg(name)
        assert collab_config is not None, "Config was deleted?"
        status = collab_config.fetch_status
        if status is None:
            return interface.FetchStatus.get_default()
        ret = status.as_storage_iface_cls()

        query = database.db.session.query(database.ExchangeData).where(
            database.ExchangeData.collab_id == collab_config.id
        )
        statement = t.cast(Select[database.ExchangeData], query.statement)
        count = query.session.execute(
            statement.with_only_columns(func.count()).order_by(None)
        ).scalar()
        ret.fetched_items = count or 0
        return ret

    def exchange_get_fetch_checkpoint(
        self, name: str
    ) -> t.Optional[FetchCheckpointBase]:
        collab_config = self._exchange_get_cfg(name)
        assert collab_config is not None, "Config was deleted?"
        return collab_config.as_checkpoint(self.exchange_get_type_configs())

    def exchange_commit_fetch(
        self,
        collab: CollaborationConfigBase,
        old_checkpoint: t.Optional[FetchCheckpointBase],
        dat: t.Dict[t.Any, t.Any],
        checkpoint: FetchCheckpointBase,
        up_to_date: bool,
    ) -> None:
        cfg = self._exchange_get_cfg(collab.name)
        assert cfg is not None, "Config was deleted?"
        fetch_status = cfg.fetch_status
        existing_checkpoint = cfg.as_checkpoint(self.exchange_get_type_configs())
        assert (
            existing_checkpoint == old_checkpoint
        ), "Old checkpoint doesn't match, race condition?"

        sesh = database.db.session

        normalized_dat = {str(k): v for k, v in dat.items()}

        existing_record_list = sesh.execute(
            select(database.ExchangeData)
            .where(database.ExchangeData.collab_id == cfg.id)
            .where(database.ExchangeData.fetch_id.in_(list(normalized_dat.keys())))
        ).scalars()

        existing_records = {e.fetch_id: e for e in existing_record_list}

        for k, val in normalized_dat.items():
            if val is None:
                sesh.execute(
                    delete(database.ExchangeData)
                    .where(database.ExchangeData.collab_id == cfg.id)
                    .where(database.ExchangeData.fetch_id == k)
                )
            else:
                record = existing_records.get(k)
                if record is None:
                    record = database.ExchangeData()
                    record.collab_id = cfg.id
                    record.fetch_id = k
                    sesh.add(record)
                record.fetch_data = dataclass_json.dataclass_dump_dict(val)

        if fetch_status is None:
            fetch_status = database.ExchangeFetchStatus()
            fetch_status.collab = cfg
            sesh.add(fetch_status)
        fetch_status.set_checkpoint(checkpoint)
        fetch_status.last_fetch_succeeded = True
        fetch_status.is_up_to_date = up_to_date
        fetch_status.last_fetch_complete_ts = int(time.time())
        sesh.commit()

    def exchange_get_data(
        self,
        collab_name: str,
        key: str,
        checkpoint: FetchCheckpointBase,
    ) -> t.Any:
        return MockedUnifiedStore().exchange_get_data(collab_name, key, checkpoint)

    def get_banks(self) -> t.Mapping[str, interface.BankConfig]:
        return {
            b.name: b.as_storage_iface_cls()
            for b in database.db.session.execute(select(database.Bank)).scalars().all()
        }

    def get_bank(self, name: str) -> t.Optional[interface.BankConfig]:
        """Override for more efficient lookup."""
        bank = database.db.session.execute(
            select(database.Bank).where(database.Bank.name == name)
        ).scalar_one_or_none()

        return None if bank is None else bank.as_storage_iface_cls()

    def _get_bank(self, name: str) -> t.Optional[database.Bank]:
        return database.db.session.execute(
            select(database.Bank).where(database.Bank.name == name)
        ).scalar_one_or_none()

    def bank_update(
        self,
        bank: interface.BankConfig,
        *,
        create: bool = False,
        rename_from: t.Optional[str] = None,
    ) -> None:
        if create:
            database.db.session.add(database.Bank.from_storage_iface_cls(bank))
        else:
            previous = database.Bank.query.filter_by(
                name=rename_from if rename_from is not None else bank.name
            ).one_or_404()
            previous.name = bank.name
            previous.enabled_ratio = bank.matching_enabled_ratio

        database.db.session.commit()

    def bank_delete(self, name: str) -> None:
        database.db.session.execute(
            delete(database.Bank).where(database.Bank.name == name)
        )
        database.db.session.commit()

    def bank_content_get(self, ids: t.Iterable[int]) -> t.Sequence[BankContentConfig]:
        return [
            b.as_storage_iface_cls()
            for b in database.db.session.query(database.BankContent)
            .filter(database.BankContent.id.in_(ids))
            .all()
        ]

    def bank_content_update(self, val: BankContentConfig) -> None:
        # TODO
        raise Exception("Not implemented")

    def bank_add_content(
        self,
        bank_name: str,
        content_signals: t.Dict[t.Type[SignalType], str],
        config: t.Optional[BankContentConfig] = None,
    ) -> int:
        # Add content to the bank provided.
        # Returns the ID of the content added.
        sesh = database.db.session

        bank = self._get_bank(bank_name)
        content = database.BankContent(bank=bank)
        sesh.add(content)
        sesh.flush()
        for content_signal, value in content_signals.items():
            hash = database.ContentSignal(
                content_id=content.id,
                signal_type=content_signal.get_name(),
                signal_val=value,
            )
            sesh.add(hash)

        sesh.commit()
        return content.id

    def bank_remove_content(self, bank_name: str, content_id: int) -> None:
        # TODO
        raise Exception("Not implemented")

    def get_current_index_build_target(
        self, signal_type: t.Type[SignalType]
    ) -> t.Optional[interface.SignalTypeIndexBuildCheckpoint]:
        query = database.db.session.query(database.ContentSignal).where(
            database.ContentSignal.signal_type == signal_type.get_name()
        )
        statement = t.cast(Select[database.ContentSignal], query.statement)
        count = query.session.execute(
            statement.with_only_columns(func.count()).order_by(None)
        ).scalar()

        if not count:
            return interface.SignalTypeIndexBuildCheckpoint.get_empty()

        # Count non-zero, so get where we are in the order
        row = database.db.session.execute(
            select(
                database.ContentSignal.create_time, database.ContentSignal.content_id
            )
            .where(database.ContentSignal.signal_type == signal_type.get_name())
            .order_by(
                database.ContentSignal.create_time.desc(),
                database.ContentSignal.content_id.desc(),
            )
            .limit(1)
        ).one()
        create_datetime, content_id = row._tuple()

        return interface.SignalTypeIndexBuildCheckpoint(
            last_item_id=content_id,
            last_item_timestamp=int(create_datetime.timestamp()),
            total_hash_count=count,
        )

    def bank_yield_content(
        self, signal_type: t.Optional[t.Type[SignalType]] = None, batch_size: int = 100
    ) -> t.Iterator[interface.BankContentIterationItem]:
        # Query for all ContentSignals and stream results with the proper batch size
        query = (
            select(database.ContentSignal)
            .order_by(
                database.ContentSignal.signal_type,
                database.ContentSignal.create_time,
                database.ContentSignal.content_id,
            )
            .execution_options(stream_results=True, max_row_buffer=batch_size)
        )

        # Conditionally apply the filter if signal_type is provided
        if signal_type is not None:
            query = query.filter(
                database.ContentSignal.signal_type == signal_type.get_name()
            )

        # Execute the query and stream results with the proper yield batch size
        result = database.db.session.execute(query).yield_per(batch_size)

        for partition in result.partitions():
            # If there are no more results, break the loop
            if not partition:
                break

            # Yield the results as tuples (signal_val, content_id)
            for row in partition:
                yield row._tuple()[0].as_iteration_item()

    @classmethod
    def init_flask(cls, app: flask.Flask) -> t.Self:
        migrate = flask_migrate.Migrate()
        database.db.init_app(app)
        migrate.init_app(app, database.db)

        flask_utils.add_cli_commands(app)

        signal_types = app.config.get("SIGNAL_TYPES", [PdqSignal, VideoMD5Signal])
        return cls(signal_types)


def explain(q, analyze: bool = False):
    """
    Debugging tool to help test query optimization.

    How to use:

    q = select(database.Blah).where(...).order_by(...)...
    print(explain(q))

    """
    return database.db.session.execute(_explain(q, analyze)).fetchall()


class _explain(Executable, ClauseElement):
    """
    Debugging tool to help test query optimization.

    How to use:

    q = select(database.Blah).where(...).order_by(...)...
    print(database.db.session.execute(_explain(q)).fetchall())
    """

    def __init__(self, stmt, analyze: bool = False):
        self.statement = stmt
        self.analyze = analyze


@compiles(_explain, "postgresql")
def _pg_explain(element: _explain, compiler, **kw):
    text = "EXPLAIN "
    if element.analyze:
        text += "ANALYZE "
    text += compiler.process(element.statement, **kw)

    return text