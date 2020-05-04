import asyncio
from typing import (
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import asyncpg
from asyncpg import (
    UndefinedFunctionError,
)

import settings
from core.db_entities import (
    DBColumn,
    DBTable,
    DstDatabase,
    SrcDatabase,
)
from core.enums import (
    ConstraintTypesEnum,
    TransferringStagesEnum,
)
from core.helpers import (
    logger,
    make_chunks,
    make_str_from_iterable,
    topological_sort,
)
from core.loggers import (
    StatisticIndexer,
    StatisticManager,
)
from core.repositories import (
    SQLRepository,
)


class Collector:
    """
    Класс комплексной транспортировки, который использует принципы обхода по
    внешним ключам и по таблицам с обратной связью
    """
    CHUNK_SIZE = 70000

    def __init__(
        self,
        src_database: SrcDatabase,
        dst_database: DstDatabase,
        statistic_manager: StatisticManager,
        key_column_values: Set[int],
    ):
        self._dst_database = dst_database
        self._src_database = src_database
        self._key_column_values = key_column_values
        self._structured_ent_ids = None
        # словарь с названиями таблиц и идентификаторами импортированных записей
        self._transfer_progress_dict = {}
        self.filling_tables = set()
        self._statistic_manager = statistic_manager

        self.content_type_table = {}

    async def _fill_table_rows_count(self, table_name: str):
        async with self._src_database.connection_pool.acquire() as connection:
            table = self._dst_database.tables[table_name]

            try:
                table_rows_counts_sql = (
                    SQLRepository.get_count_table_records(
                        primary_key=table.primary_key,
                    )
                )
            except AttributeError as e:
                logger.warning(
                    f'{str(e)} --- _fill_table_rows_count {"-"*10} - '
                    f"{table.name}"
                )
                raise AttributeError
            except UndefinedFunctionError:
                raise UndefinedFunctionError

            res = await connection.fetchrow(table_rows_counts_sql)

            if res and res[0] and res[1]:
                logger.debug(
                    f"table {table_name} with full count {res[0]}, "
                    f"max id - {res[1]}"
                )

                table.full_count = int(res[0])

                table.max_id = (
                    int(res[1])
                    if isinstance(res[1], int)
                    else table.full_count + 100000
                )

            del table_rows_counts_sql

    async def fill_tables_rows_counts(self):
        logger.info(
            "заполнение количества записей в табилце и максимального значения "
            "идентификатора.."
        )

        coroutines = [
            self._fill_table_rows_count(table_name)
            for table_name in sorted(self._dst_database.tables.keys())
        ]

        if coroutines:
            await asyncio.wait(coroutines)

        logger.info("заполнение значений счетчиков завершено")

    async def _collect_key_table_values(self):
        logger.info("transfer key table records...")

        key_table = self._dst_database.tables[settings.KEY_TABLE_NAME]

        key_table.need_transfer_pks.update(self._key_column_values)

        key_table.is_ready_for_transferring = True

        logger.info("transfer key table records finished!")

    async def _get_constraint_table_ids_part(
        self,
        constraint_table_ids_sql,
        constraint_table_ids,
    ):
        if constraint_table_ids_sql:
            logger.debug(constraint_table_ids_sql)
            async with self._src_database.connection_pool.acquire() as connection:
                try:
                    c_t_ids = await connection.fetch(constraint_table_ids_sql)
                except asyncpg.PostgresSyntaxError as e:
                    logger.warning(
                        f"{str(e)} --- {constraint_table_ids_sql} --- "
                        f"_get_constraint_table_ids_part"
                    )
                    c_t_ids = []

                constraint_table_ids.extend(
                    [
                        item[0]
                        for item in filter(lambda id_: id_[0] is not None, c_t_ids)
                    ]
                )

                del c_t_ids
                del constraint_table_ids_sql

    async def _get_table_column_values(
        self,
        table: DBTable,
        column: DBColumn,
        primary_key_values: Iterable[Union[int, str]] = (),
        where_conditions_columns: Optional[Dict[str, Set[Union[int, str]]]] = None,  # noqa
        is_revert=False,
    ) -> set:
        # если таблица находится в исключенных, то ее записи не нужно
        # импортировать
        try:
            if column.constraint_table.name in settings.EXCLUDED_TABLES:
                return set()
        except AttributeError as e:
            logger.warning(f"{str(e)} --- _get_table_column_values")
            return set()

        # формирование запроса на получения идентификаторов записей
        # внешней таблицы
        constraint_table_ids_sql_list = await SQLRepository.get_table_column_values_sql(
            table=table,
            column=column,
            key_column_values=self._key_column_values,
            primary_key_values=primary_key_values,
            where_conditions_columns=where_conditions_columns,
            is_revert=is_revert,
        )
        constraint_table_ids = []

        for constraint_table_ids_sql in constraint_table_ids_sql_list:
            await self._get_constraint_table_ids_part(
                constraint_table_ids_sql, constraint_table_ids
            )

        del constraint_table_ids_sql_list[:]

        result = set(constraint_table_ids)

        del constraint_table_ids[:]

        return result

    async def _collect_revert_table_ids(self, rev_table, fk_column, table):
        rev_table_pk_ids = (
            list(rev_table.need_transfer_pks)
            if not rev_table.is_full_transferred
            else []
        )

        rev_ids = await self._get_table_column_values(
            table=rev_table,
            column=fk_column,
            primary_key_values=rev_table_pk_ids,
            is_revert=True,
        )

        if rev_ids:
            table.need_transfer_pks.update(rev_ids)

        del rev_ids

    async def _collect_importing_revert_tables_data(
        self, rev_table_name, table
    ):
        constraint_types_for_importing = [ConstraintTypesEnum.FOREIGN_KEY]
        rev_table = self._dst_database.tables[rev_table_name]
        logger.info(f"prepare revert table {rev_table_name}")

        if rev_table.fks_with_key_column and not table.with_key_column:
            return

        if rev_table.need_transfer_pks:
            coroutines = [
                self._collect_revert_table_ids(rev_table, fk_column, table)
                for fk_column in rev_table.get_columns_by_constraint_table_name(
                    table.name,
                    constraint_types_for_importing,
                )
            ]

            if coroutines:
                await asyncio.wait(coroutines)

        table.revert_fk_tables[rev_table_name] = True

    async def _collect_importing_fk_tables_records_ids(
        self,
        table: DBTable,
    ):
        logger.info(
            f"start collecting records ids of table \"{table.name}\""
        )
        # обход таблиц связанных через внешние ключи
        where_conditions_columns = {}

        if table.fks_with_key_column:
            fk_columns = table.fks_with_key_column
            logger.debug(
                f"table with fks_with_ent_id - "
                f"{make_str_from_iterable(table.fks_with_key_column)}"
            )
        else:
            fk_columns = table.not_self_fk_columns
            logger.debug(
                f"table without fks_with_ent_id - {table.not_self_fk_columns}"
            )

        unique_fks_columns = table.unique_foreign_keys_columns
        if unique_fks_columns:
            fk_columns = unique_fks_columns

        with_full_transferred_table = False

        for fk_column in fk_columns:
            logger.debug(f"prepare column {fk_column.name}")
            fk_table = self._dst_database.tables[
                fk_column.constraint_table.name
            ]

            if fk_table.need_transfer_pks:
                if not fk_table.is_full_transferred:
                    where_conditions_columns[fk_column.name] = fk_table.need_transfer_pks
                else:
                    with_full_transferred_table = True

        if (
            fk_columns and
            not where_conditions_columns and
            not with_full_transferred_table
        ):
            return

        tasks = await asyncio.wait([self._get_table_column_values(
            table=table,
            column=table.primary_key,
            where_conditions_columns=where_conditions_columns,
        )])

        fk_ids = (
            tasks[0].pop().result() if (
                tasks and
                tasks[0] and
                isinstance(tasks[0], set)
            ) else
            None
        )

        if fk_columns and where_conditions_columns and not fk_ids:
            return

        table.need_transfer_pks.update(fk_ids)

        logger.debug(
            f'table "{table.name}" need transfer pks - {len(table.need_transfer_pks)}'
        )

        del fk_ids

        # обход таблиц ссылающихся на текущую таблицу
        logger.debug("prepare revert tables")

        rev_coroutines = [
            self._collect_importing_revert_tables_data(rev_table_name, table)
            for rev_table_name, is_ready_for_transferring in table.revert_fk_tables.items()
        ]

        if rev_coroutines:
            await asyncio.wait(rev_coroutines)

        if not table.need_transfer_pks:
            all_records = await self._get_table_column_values(
                table=table,
                column=table.primary_key,
            )

            table.need_transfer_pks.update(all_records)

            del all_records

        table.is_ready_for_transferring = True

        logger.info(
            f"finished collecting records ids of table \"{table.name}\""
        )

    async def _recursively_preparing_foreign_table_chunk(
        self,
        foreign_table: DBTable,
        foreign_table_pks_chunk: List[int],
        stack_tables: Tuple[str],
        deep_without_key_table: int,
    ):
        """
        Recursively preparing foreign table chunk
        """
        dwkt = (
            deep_without_key_table - 1 if
            not foreign_table.with_key_column else
            deep_without_key_table
        )

        await self._recursively_preparing_table(
            table=foreign_table,
            need_transfer_pks=foreign_table_pks_chunk,
            stack_tables=stack_tables,
            deep_without_key_table=dwkt,
        )

        del dwkt
        del foreign_table_pks_chunk[:]

    async def _recursively_preparing_foreign_table(
        self,
        table: DBTable,
        column: DBColumn,
        need_transfer_pks: Iterable[int],
        stack_tables: Tuple[str],
        deep_without_key_table: int,
    ):
        """
        Recursively preparing foreign table
        """
        foreign_table = self._dst_database.tables[column.constraint_table.name]

        # если таблица уже есть в стеке импорта таблиц, то он нас не
        # интересует; если талица с key_column, то записи в любом случае
        # будут импортированы
        if (
            foreign_table in stack_tables or
            foreign_table.with_key_column
        ):
            return

        # Если таблица с key_column, то нет необходимости пробрасывать
        # идентификаторы записей
        if table.with_key_column:
            foreign_table_pks = await self._get_table_column_values(
                table=table,
                column=column,
            )
        else:
            need_transfer_pks = (
                need_transfer_pks if
                not table.is_full_transferred else
                ()
            )

            foreign_table_pks = await self._get_table_column_values(
                table=table,
                column=column,
                primary_key_values=need_transfer_pks,
            )

        # если найдены значения внешних ключей отличающиеся от null, то
        # записи из внешней талицы с этими идентификаторами должны быть
        # импортированы
        if foreign_table_pks:
            logger.debug(
                f"table - {table.name}, column - {column.name} - reversed "
                f"collecting of fk_ids ----- {foreign_table.name}"
            )

            foreign_table_pks_difference = foreign_table_pks.difference(
                foreign_table.need_transfer_pks
            )

            # если есть разница между предполагаемыми записями для импорта
            # и уже выбранными ранее, то разницу нужно импортировать
            if foreign_table_pks_difference:
                foreign_table.need_transfer_pks.update(
                    foreign_table_pks_difference
                )

                foreign_table_pks_difference_chunks = make_chunks(
                    iterable=foreign_table_pks_difference,
                    size=self.CHUNK_SIZE,
                    is_list=True,
                )

                coroutines = [
                    self._recursively_preparing_foreign_table_chunk(
                        foreign_table=foreign_table,
                        foreign_table_pks_chunk=foreign_table_pks_difference_chunk,  # noqa
                        stack_tables=stack_tables,
                        deep_without_key_table=deep_without_key_table,
                    )
                    for foreign_table_pks_difference_chunk in foreign_table_pks_difference_chunks  # noqa
                ]

                if coroutines:
                    await asyncio.wait(coroutines)

            del foreign_table_pks_difference

        del foreign_table_pks

    async def _recursively_preparing_table(
        self,
        table: DBTable,
        need_transfer_pks: List[int],
        stack_tables=(),
        deep_without_key_table=None,
    ):
        """
        Recursively preparing table
        """
        if not deep_without_key_table:
            logger.debug("Max deep without key table")
            return

        stack_tables += (table,)

        logger.debug(make_str_from_iterable([t.name for t in stack_tables]))

        coroutines = [
            self._recursively_preparing_foreign_table(
                table=table,
                column=column,
                need_transfer_pks=need_transfer_pks,
                stack_tables=stack_tables,
                deep_without_key_table=deep_without_key_table,
            )
            for column in table.not_self_fk_columns
        ]

        if coroutines:
            await asyncio.wait(coroutines)

    async def _recursively_preparing_table_with_key_column(
        self,
        table: DBTable,
        need_transfer_pks_chunk: List[int],
    ):
        """
        Recursively preparing table with key column
        """
        await self._recursively_preparing_table(
            table=table,
            need_transfer_pks=need_transfer_pks_chunk,
            deep_without_key_table=1,
        )

        del need_transfer_pks_chunk[:]

    async def _prepare_tables_with_key_column(
        self,
        table: DBTable,
    ):
        """
        Preparing tables with key column and siblings
        """
        logger.info(
            f'start preparing table with key column "{table.name}"'
        )
        need_transfer_pks = await self._get_table_column_values(
            table=table,
            column=table.primary_key,
        )

        if need_transfer_pks:
            table.need_transfer_pks.update(need_transfer_pks)

            need_transfer_pks_chunks = make_chunks(
                iterable=need_transfer_pks,
                size=self.CHUNK_SIZE,
                is_list=True,
            )

            coroutines = [
                self._recursively_preparing_table_with_key_column(
                    table=table,
                    need_transfer_pks_chunk=need_transfer_pks_chunk,
                )
                for need_transfer_pks_chunk in need_transfer_pks_chunks
            ]

            if coroutines:
                await asyncio.wait(coroutines)

        table.is_ready_for_transferring = True

        del need_transfer_pks

        logger.info(
            f'finished preparing table with key column "{table.name}"'
        )

    async def _prepare_common_tables(self):
        """
        Метод сбора данных для дальнейшего импорта в целевую базу. Первоначально
        производится сбор данных из таблиц с key_column и всех таблиц, которые их
        окружают с глубиной рекурсивного обхода 1. Сюда входят таблицы связанные
        через внешние ключи и таблицы ссылающиеся на текущую. После чего
        производится сбор записей таблиц, из которых не был произведен сбор
        данных. Эти таблицы находятся дальше чем одна таблица от таблиц с
        key_column.
        """
        logger.info("start preparing common tables for transferring")

        # preparing tables with key table and siblings for transferring
        coroutines = [
            self._prepare_tables_with_key_column(table)
            for table in self._dst_database.tables_with_key_column
        ]

        if coroutines:
            await asyncio.wait(coroutines)

        not_transferred_tables = list(
            filter(
                lambda t: (
                    not t.is_ready_for_transferring
                    and t.name
                    not in settings.TABLES_WITH_GENERIC_FOREIGN_KEY
                ),
                self._dst_database.tables.values(),
            )
        )
        logger.debug(
            f"tables not transferring {str(len(not_transferred_tables))}"
        )

        not_transferred_relatives = []
        for table in self._dst_database.tables_without_generics:
            for fk_column in table.not_self_fk_columns:
                not_transferred_relatives.append(
                    (table.name, fk_column.constraint_table.name)
                )

        sorting_result = topological_sort(not_transferred_relatives)
        sorting_result.cyclic.reverse()
        sorting_result.sorted.reverse()

        sorted_not_transferred = sorting_result.cyclic + sorting_result.sorted

        without_relatives = list(
            {
                table.name
                for table in self._dst_database.tables_without_generics
            }.difference(
                sorted_not_transferred
            )
        )

        sorted_not_transferred = without_relatives + sorted_not_transferred

        # явно ломаю асинхронность, т.к. порядок импорта таблиц важен
        for table_name in sorted_not_transferred:
            table = self._dst_database.tables[table_name]
            if not table.is_ready_for_transferring:
                await self._collect_importing_fk_tables_records_ids(
                    table
                )

        logger.info("finished collecting common tables records ids")

    async def _prepare_content_type_tables(self):
        """
        Подготавливает соответствие content_type_id и наименование таблицы в БД
        """
        logger.info("prepare content type tables")

        content_type_table_list = await self._dst_database.fetch_raw_sql(
            SQLRepository.get_content_type_table_sql()
        )

        content_type_table_dict = {
            (app_label, model): table_name
            for table_name, app_label, model in content_type_table_list
        }

        content_type_list = await self._src_database.fetch_raw_sql(
            SQLRepository.get_content_type_sql()
        )

        content_type_dict = {
            (app_label, model): content_type_id
            for content_type_id, app_label, model in content_type_list
        }

        for key in content_type_table_dict.keys():
            self.content_type_table[content_type_table_dict[key]] = (
                content_type_dict[key]
            )

        del content_type_table_list[:]
        del content_type_table_dict
        del content_type_list[:]
        del content_type_dict

    async def _prepare_content_type_generic_data(
        self,
        target_table: DBTable,
        rel_table_name: str,
    ):
        if not rel_table_name:
            logger.debug('not send rel_table_name')
            return

        rel_table = self._dst_database.tables.get(rel_table_name)

        if not rel_table:
            logger.debug(f'table {rel_table_name} not found')
            return

        object_id_column = await target_table.get_column_by_name('object_id')

        if rel_table.primary_key.data_type != object_id_column.data_type:
            logger.debug(
                f'pk of table {rel_table_name} has an incompatible data type'
            )
            return

        logger.info('prepare content type generic data')

        where_conditions = {
            'object_id': rel_table.need_transfer_pks,
            'content_type_id': [self.content_type_table[rel_table.name]],
        }

        need_transfer_pks = await self._get_table_column_values(
            table=target_table,
            column=target_table.primary_key,
            where_conditions_columns=where_conditions,
        )

        logger.info(
            f'{target_table.name} need transfer pks {len(need_transfer_pks)}'
        )

        target_table.need_transfer_pks.update(need_transfer_pks)

        del where_conditions
        del need_transfer_pks

    async def _prepare_generic_table_data(self, target_table: DBTable):
        """
        Перенос данных из таблицы, содержащей generic foreign key
        """
        logger.info(f"prepare generic table data {target_table.name}")

        coroutines = [
            self._prepare_content_type_generic_data(
                target_table=target_table, rel_table_name=rel_table_name
            )
            for rel_table_name in self.content_type_table.keys()
        ]

        if coroutines:
            await asyncio.wait(coroutines)

    async def _collect_generic_tables_records_ids(self):
        """
        Собирает идентификаторы записей таблиц, содержащих generic key
        Предполагается, что такие таблицы имеют поля object_id и content_type_id
        """
        logger.info("collect generic tables records ids")

        await asyncio.wait([self._prepare_content_type_tables()])

        generic_table_names = set(
            settings.TABLES_WITH_GENERIC_FOREIGN_KEY
        ).difference(settings.EXCLUDED_TABLES)

        coroutines = [
            self._prepare_generic_table_data(
                self._dst_database.tables.get(table_name)
            )
            for table_name in filter(None, generic_table_names)
        ]

        if coroutines:
            await asyncio.wait(coroutines)

        logger.info("finish collecting")

    async def collect(self):
        with StatisticIndexer(
            self._statistic_manager,
            TransferringStagesEnum.TRANSFER_KEY_TABLE,
        ):
            await asyncio.wait([self._collect_key_table_values()])

        with StatisticIndexer(
            self._statistic_manager,
            TransferringStagesEnum.COLLECT_COMMON_TABLES_RECORDS_IDS
        ):
            await asyncio.wait([self._prepare_common_tables()])

        with StatisticIndexer(
            self._statistic_manager,
            TransferringStagesEnum.COLLECT_GENERIC_TABLES_RECORDS_IDS
        ):
            await asyncio.wait([self._collect_generic_tables_records_ids()])
