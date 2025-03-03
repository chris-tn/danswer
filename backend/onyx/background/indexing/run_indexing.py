import time
import traceback
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.background.indexing.checkpointing import get_time_windows_for_index_attempt
from onyx.background.indexing.tracer import OnyxTracer
from onyx.configs.app_configs import INDEXING_SIZE_WARNING_THRESHOLD
from onyx.configs.app_configs import INDEXING_TRACER_INTERVAL
from onyx.configs.app_configs import POLL_CONNECTOR_OFFSET
from onyx.configs.constants import DocumentSource
from onyx.configs.constants import MilestoneRecordType
from onyx.connectors.connector_runner import ConnectorRunner
from onyx.connectors.factory import instantiate_connector
from onyx.connectors.models import Document
from onyx.connectors.models import IndexAttemptMetadata
from onyx.db.connector_credential_pair import get_connector_credential_pair_from_id
from onyx.db.connector_credential_pair import get_last_successful_attempt_time
from onyx.db.connector_credential_pair import update_connector_credential_pair
from onyx.db.engine import get_session_with_tenant
from onyx.db.enums import ConnectorCredentialPairStatus
from onyx.db.index_attempt import get_index_attempt
from onyx.db.index_attempt import mark_attempt_canceled
from onyx.db.index_attempt import mark_attempt_failed
from onyx.db.index_attempt import mark_attempt_partially_succeeded
from onyx.db.index_attempt import mark_attempt_succeeded
from onyx.db.index_attempt import transition_attempt_to_in_progress
from onyx.db.index_attempt import update_docs_indexed
from onyx.db.models import ConnectorCredentialPair
from onyx.db.models import IndexAttempt
from onyx.db.models import IndexingStatus
from onyx.db.models import IndexModelStatus
from onyx.document_index.factory import get_default_document_index
from onyx.indexing.embedder import DefaultIndexingEmbedder
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from onyx.indexing.indexing_pipeline import build_indexing_pipeline
from onyx.utils.logger import setup_logger
from onyx.utils.logger import TaskAttemptSingleton
from onyx.utils.telemetry import create_milestone_and_report
from onyx.utils.variable_functionality import global_version

logger = setup_logger()

INDEXING_TRACER_NUM_PRINT_ENTRIES = 5


def _get_connector_runner(
    db_session: Session,
    attempt: IndexAttempt,
    start_time: datetime,
    end_time: datetime,
    tenant_id: str | None,
) -> ConnectorRunner:
    """
    NOTE: `start_time` and `end_time` are only used for poll connectors

    Returns an iterator of document batches and whether the returned documents
    are the complete list of existing documents of the connector. If the task
    of type LOAD_STATE, the list will be considered complete and otherwise incomplete.
    """
    task = attempt.connector_credential_pair.connector.input_type

    try:
        runnable_connector = instantiate_connector(
            db_session=db_session,
            source=attempt.connector_credential_pair.connector.source,
            input_type=task,
            connector_specific_config=attempt.connector_credential_pair.connector.connector_specific_config,
            credential=attempt.connector_credential_pair.credential,
            tenant_id=tenant_id,
        )
    except Exception as e:
        logger.exception(f"Unable to instantiate connector due to {e}")
        # since we failed to even instantiate the connector, we pause the CCPair since
        # it will never succeed

        cc_pair = get_connector_credential_pair_from_id(
            db_session=db_session,
            cc_pair_id=attempt.connector_credential_pair.id,
        )
        if cc_pair and cc_pair.status == ConnectorCredentialPairStatus.ACTIVE:
            update_connector_credential_pair(
                db_session=db_session,
                connector_id=attempt.connector_credential_pair.connector.id,
                credential_id=attempt.connector_credential_pair.credential.id,
                status=ConnectorCredentialPairStatus.PAUSED,
            )
        raise e

    return ConnectorRunner(
        connector=runnable_connector, time_range=(start_time, end_time)
    )


def strip_null_characters(doc_batch: list[Document]) -> list[Document]:
    cleaned_batch = []
    for doc in doc_batch:
        cleaned_doc = doc.model_copy()

        if "\x00" in cleaned_doc.id:
            logger.warning(f"NUL characters found in document ID: {cleaned_doc.id}")
            cleaned_doc.id = cleaned_doc.id.replace("\x00", "")

        if "\x00" in cleaned_doc.semantic_identifier:
            logger.warning(
                f"NUL characters found in document semantic identifier: {cleaned_doc.semantic_identifier}"
            )
            cleaned_doc.semantic_identifier = cleaned_doc.semantic_identifier.replace(
                "\x00", ""
            )

        for section in cleaned_doc.sections:
            if section.link and "\x00" in section.link:
                logger.warning(
                    f"NUL characters found in document link for document: {cleaned_doc.id}"
                )
                section.link = section.link.replace("\x00", "")

        cleaned_batch.append(cleaned_doc)

    return cleaned_batch


class ConnectorStopSignal(Exception):
    """A custom exception used to signal a stop in processing."""


class RunIndexingContext(BaseModel):
    index_name: str
    cc_pair_id: int
    connector_id: int
    credential_id: int
    source: DocumentSource
    earliest_index_time: float
    from_beginning: bool
    is_primary: bool
    search_settings_status: IndexModelStatus


def _run_indexing(
    db_session: Session,
    index_attempt_id: int,
    tenant_id: str | None,
    callback: IndexingHeartbeatInterface | None = None,
) -> None:
    """
    1. Get documents which are either new or updated from specified application
    2. Embed and index these documents into the chosen datastore (vespa)
    3. Updates Postgres to record the indexed documents + the outcome of this run

    TODO: do not change index attempt statuses here ... instead, set signals in redis
    and allow the monitor function to clean them up
    """
    start_time = time.time()

    with get_session_with_tenant(tenant_id) as db_session_temp:
        index_attempt_start = get_index_attempt(db_session_temp, index_attempt_id)
        if not index_attempt_start:
            raise ValueError(
                f"Index attempt {index_attempt_id} does not exist in DB. This should not be possible."
            )

        if index_attempt_start.search_settings is None:
            raise ValueError(
                "Search settings must be set for indexing. This should not be possible."
            )

        # search_settings = index_attempt_start.search_settings
        db_connector = index_attempt_start.connector_credential_pair.connector
        db_credential = index_attempt_start.connector_credential_pair.credential
        ctx = RunIndexingContext(
            index_name=index_attempt_start.search_settings.index_name,
            cc_pair_id=index_attempt_start.connector_credential_pair.id,
            connector_id=db_connector.id,
            credential_id=db_credential.id,
            source=db_connector.source,
            earliest_index_time=(
                db_connector.indexing_start.timestamp()
                if db_connector.indexing_start
                else 0
            ),
            from_beginning=index_attempt_start.from_beginning,
            # Only update cc-pair status for primary index jobs
            # Secondary index syncs at the end when swapping
            is_primary=(
                index_attempt_start.search_settings.status == IndexModelStatus.PRESENT
            ),
            search_settings_status=index_attempt_start.search_settings.status,
        )

        last_successful_index_time = (
            ctx.earliest_index_time
            if ctx.from_beginning
            else get_last_successful_attempt_time(
                connector_id=ctx.connector_id,
                credential_id=ctx.credential_id,
                earliest_index=ctx.earliest_index_time,
                search_settings=index_attempt_start.search_settings,
                db_session=db_session_temp,
            )
        )

        embedding_model = DefaultIndexingEmbedder.from_db_search_settings(
            search_settings=index_attempt_start.search_settings,
            callback=callback,
        )

    # Indexing is only done into one index at a time
    document_index = get_default_document_index(
        primary_index_name=ctx.index_name, secondary_index_name=None
    )

    indexing_pipeline = build_indexing_pipeline(
        attempt_id=index_attempt_id,
        embedder=embedding_model,
        document_index=document_index,
        ignore_time_skip=(
            ctx.from_beginning
            or (ctx.search_settings_status == IndexModelStatus.FUTURE)
        ),
        db_session=db_session,
        tenant_id=tenant_id,
        callback=callback,
    )

    if INDEXING_TRACER_INTERVAL > 0:
        logger.debug(f"Memory tracer starting: interval={INDEXING_TRACER_INTERVAL}")
        tracer = OnyxTracer()
        tracer.start()
        tracer.snap()

    index_attempt_md = IndexAttemptMetadata(
        connector_id=ctx.connector_id,
        credential_id=ctx.credential_id,
    )

    batch_num = 0
    net_doc_change = 0
    document_count = 0
    chunk_count = 0
    run_end_dt = None
    for ind, (window_start, window_end) in enumerate(
        get_time_windows_for_index_attempt(
            last_successful_run=datetime.fromtimestamp(
                last_successful_index_time, tz=timezone.utc
            ),
            source_type=db_connector.source,
        )
    ):
        cc_pair_loop: ConnectorCredentialPair | None = None
        index_attempt_loop: IndexAttempt | None = None

        try:
            window_start = max(
                window_start - timedelta(minutes=POLL_CONNECTOR_OFFSET),
                datetime(1970, 1, 1, tzinfo=timezone.utc),
            )

            with get_session_with_tenant(tenant_id) as db_session_temp:
                index_attempt_loop_start = get_index_attempt(
                    db_session_temp, index_attempt_id
                )
                if not index_attempt_loop_start:
                    raise RuntimeError(
                        f"Index attempt {index_attempt_id} not found in DB."
                    )

                connector_runner = _get_connector_runner(
                    db_session=db_session_temp,
                    attempt=index_attempt_loop_start,
                    start_time=window_start,
                    end_time=window_end,
                    tenant_id=tenant_id,
                )

            all_connector_doc_ids: set[str] = set()

            tracer_counter = 0
            if INDEXING_TRACER_INTERVAL > 0:
                tracer.snap()
            for doc_batch in connector_runner.run():
                # Check if connector is disabled mid run and stop if so unless it's the secondary
                # index being built. We want to populate it even for paused connectors
                # Often paused connectors are sources that aren't updated frequently but the
                # contents still need to be initially pulled.
                if callback:
                    if callback.should_stop():
                        raise ConnectorStopSignal("Connector stop signal detected")

                # TODO: should we move this into the above callback instead?
                with get_session_with_tenant(tenant_id) as db_session_temp:
                    cc_pair_loop = get_connector_credential_pair_from_id(
                        db_session_temp,
                        ctx.cc_pair_id,
                    )
                    if not cc_pair_loop:
                        raise RuntimeError(f"CC pair {ctx.cc_pair_id} not found in DB.")

                    if (
                        (
                            cc_pair_loop.status == ConnectorCredentialPairStatus.PAUSED
                            and ctx.search_settings_status != IndexModelStatus.FUTURE
                        )
                        # if it's deleting, we don't care if this is a secondary index
                        or cc_pair_loop.status == ConnectorCredentialPairStatus.DELETING
                    ):
                        # let the `except` block handle this
                        raise RuntimeError("Connector was disabled mid run")

                    index_attempt_loop = get_index_attempt(
                        db_session_temp, index_attempt_id
                    )
                    if not index_attempt_loop:
                        raise RuntimeError(
                            f"Index attempt {index_attempt_id} not found in DB."
                        )

                    if index_attempt_loop.status != IndexingStatus.IN_PROGRESS:
                        # Likely due to user manually disabling it or model swap
                        raise RuntimeError(
                            f"Index Attempt was canceled, status is {index_attempt_loop.status}"
                        )

                batch_description = []

                doc_batch_cleaned = strip_null_characters(doc_batch)
                for doc in doc_batch_cleaned:
                    batch_description.append(doc.to_short_descriptor())

                    doc_size = 0
                    for section in doc.sections:
                        doc_size += len(section.text)

                    if doc_size > INDEXING_SIZE_WARNING_THRESHOLD:
                        logger.warning(
                            f"Document size: doc='{doc.to_short_descriptor()}' "
                            f"size={doc_size} "
                            f"threshold={INDEXING_SIZE_WARNING_THRESHOLD}"
                        )

                logger.debug(f"Indexing batch of documents: {batch_description}")

                index_attempt_md.batch_num = batch_num + 1  # use 1-index for this

                # real work happens here!
                new_docs, total_batch_chunks = indexing_pipeline(
                    document_batch=doc_batch_cleaned,
                    index_attempt_metadata=index_attempt_md,
                )

                batch_num += 1
                net_doc_change += new_docs
                chunk_count += total_batch_chunks
                document_count += len(doc_batch_cleaned)
                all_connector_doc_ids.update(doc.id for doc in doc_batch_cleaned)

                # commit transaction so that the `update` below begins
                # with a brand new transaction. Postgres uses the start
                # of the transactions when computing `NOW()`, so if we have
                # a long running transaction, the `time_updated` field will
                # be inaccurate
                db_session.commit()

                if callback:
                    callback.progress("_run_indexing", len(doc_batch_cleaned))

                # This new value is updated every batch, so UI can refresh per batch update
                with get_session_with_tenant(tenant_id) as db_session_temp:
                    update_docs_indexed(
                        db_session=db_session_temp,
                        index_attempt_id=index_attempt_id,
                        total_docs_indexed=document_count,
                        new_docs_indexed=net_doc_change,
                        docs_removed_from_index=0,
                    )

                tracer_counter += 1
                if (
                    INDEXING_TRACER_INTERVAL > 0
                    and tracer_counter % INDEXING_TRACER_INTERVAL == 0
                ):
                    logger.debug(
                        f"Running trace comparison for batch {tracer_counter}. interval={INDEXING_TRACER_INTERVAL}"
                    )
                    tracer.snap()
                    tracer.log_previous_diff(INDEXING_TRACER_NUM_PRINT_ENTRIES)

            run_end_dt = window_end
            if ctx.is_primary:
                with get_session_with_tenant(tenant_id) as db_session_temp:
                    update_connector_credential_pair(
                        db_session=db_session_temp,
                        connector_id=ctx.connector_id,
                        credential_id=ctx.credential_id,
                        net_docs=net_doc_change,
                        run_dt=run_end_dt,
                    )
        except Exception as e:
            logger.exception(
                f"Connector run exceptioned after elapsed time: {time.time() - start_time} seconds"
            )

            if isinstance(e, ConnectorStopSignal):
                with get_session_with_tenant(tenant_id) as db_session_temp:
                    mark_attempt_canceled(
                        index_attempt_id,
                        db_session_temp,
                        reason=str(e),
                    )

                    if ctx.is_primary:
                        update_connector_credential_pair(
                            db_session=db_session_temp,
                            connector_id=ctx.connector_id,
                            credential_id=ctx.credential_id,
                            net_docs=net_doc_change,
                        )

                if INDEXING_TRACER_INTERVAL > 0:
                    tracer.stop()
                raise e
            else:
                # Only mark the attempt as a complete failure if this is the first indexing window.
                # Otherwise, some progress was made - the next run will not start from the beginning.
                # In this case, it is not accurate to mark it as a failure. When the next run begins,
                # if that fails immediately, it will be marked as a failure.
                #
                # NOTE: if the connector is manually disabled, we should mark it as a failure regardless
                # to give better clarity in the UI, as the next run will never happen.
                if (
                    ind == 0
                    or (
                        cc_pair_loop is not None and not cc_pair_loop.status.is_active()
                    )
                    or (
                        index_attempt_loop is not None
                        and index_attempt_loop.status != IndexingStatus.IN_PROGRESS
                    )
                ):
                    with get_session_with_tenant(tenant_id) as db_session_temp:
                        mark_attempt_failed(
                            index_attempt_id,
                            db_session_temp,
                            failure_reason=str(e),
                            full_exception_trace=traceback.format_exc(),
                        )

                        if ctx.is_primary:
                            update_connector_credential_pair(
                                db_session=db_session_temp,
                                connector_id=ctx.connector_id,
                                credential_id=ctx.credential_id,
                                net_docs=net_doc_change,
                            )

                    if INDEXING_TRACER_INTERVAL > 0:
                        tracer.stop()
                    raise e

            # break => similar to success case. As mentioned above, if the next run fails for the same
            # reason it will then be marked as a failure
            break

    if INDEXING_TRACER_INTERVAL > 0:
        logger.debug(
            f"Running trace comparison between start and end of indexing. {tracer_counter} batches processed."
        )
        tracer.snap()
        tracer.log_first_diff(INDEXING_TRACER_NUM_PRINT_ENTRIES)
        tracer.stop()
        logger.debug("Memory tracer stopped.")

    if (
        index_attempt_md.num_exceptions > 0
        and index_attempt_md.num_exceptions >= batch_num
    ):
        with get_session_with_tenant(tenant_id) as db_session_temp:
            mark_attempt_failed(
                index_attempt_id,
                db_session_temp,
                failure_reason="All batches exceptioned.",
            )
            if ctx.is_primary:
                update_connector_credential_pair(
                    db_session=db_session_temp,
                    connector_id=ctx.connector_id,
                    credential_id=ctx.credential_id,
                )
            raise Exception(
                f"Connector failed - All batches exceptioned: batches={batch_num}"
            )

    elapsed_time = time.time() - start_time

    with get_session_with_tenant(tenant_id) as db_session_temp:
        if index_attempt_md.num_exceptions == 0:
            mark_attempt_succeeded(index_attempt_id, db_session_temp)

            create_milestone_and_report(
                user=None,
                distinct_id=tenant_id or "N/A",
                event_type=MilestoneRecordType.CONNECTOR_SUCCEEDED,
                properties=None,
                db_session=db_session_temp,
            )

            logger.info(
                f"Connector succeeded: "
                f"docs={document_count} chunks={chunk_count} elapsed={elapsed_time:.2f}s"
            )
        else:
            mark_attempt_partially_succeeded(index_attempt_id, db_session_temp)
            logger.info(
                f"Connector completed with some errors: "
                f"exceptions={index_attempt_md.num_exceptions} "
                f"batches={batch_num} "
                f"docs={document_count} "
                f"chunks={chunk_count} "
                f"elapsed={elapsed_time:.2f}s"
            )

        if ctx.is_primary:
            update_connector_credential_pair(
                db_session=db_session_temp,
                connector_id=ctx.connector_id,
                credential_id=ctx.credential_id,
                run_dt=run_end_dt,
            )


def run_indexing_entrypoint(
    index_attempt_id: int,
    tenant_id: str | None,
    connector_credential_pair_id: int,
    is_ee: bool = False,
    callback: IndexingHeartbeatInterface | None = None,
) -> None:
    try:
        if is_ee:
            global_version.set_ee()

        # set the indexing attempt ID so that all log messages from this process
        # will have it added as a prefix
        TaskAttemptSingleton.set_cc_and_index_id(
            index_attempt_id, connector_credential_pair_id
        )
        with get_session_with_tenant(tenant_id) as db_session:
            # TODO: remove long running session entirely
            attempt = transition_attempt_to_in_progress(index_attempt_id, db_session)

            tenant_str = ""
            if tenant_id is not None:
                tenant_str = f" for tenant {tenant_id}"

            connector_name = attempt.connector_credential_pair.connector.name
            connector_config = (
                attempt.connector_credential_pair.connector.connector_specific_config
            )
            credential_id = attempt.connector_credential_pair.credential_id

        logger.info(
            f"Indexing starting{tenant_str}: "
            f"connector='{connector_name}' "
            f"config='{connector_config}' "
            f"credentials='{credential_id}'"
        )

        with get_session_with_tenant(tenant_id) as db_session:
            _run_indexing(db_session, index_attempt_id, tenant_id, callback)

        logger.info(
            f"Indexing finished{tenant_str}: "
            f"connector='{connector_name}' "
            f"config='{connector_config}' "
            f"credentials='{credential_id}'"
        )
    except Exception as e:
        logger.exception(
            f"Indexing job with ID '{index_attempt_id}' for tenant {tenant_id} failed due to {e}"
        )
