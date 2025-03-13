"""Sketch analyzer plugin for hashR lookup."""

import logging
import sys

from math import ceil
from typing import Optional, Union
from flask import current_app
import sqlalchemy as sqla
from timesketch.lib.analyzers import interface, manager

logger = logging.getLogger("timesketch.analyzers.hashR")


class HashRLookup(interface.BaseAnalyzer):
    """Analyzer for HashRLookup."""

    NAME = "hashr_lookup"
    DISPLAY_NAME = "hashR lookup"
    DESCRIPTION = (
        "Lookup file hash values in the known hashes database generated by the "
        "hashR project. (https://github.com/google/hashr)"
    )

    DEPENDENCIES = frozenset()

    zerobyte_file_counter = 0
    known_hash_event_counter = 0
    unique_known_hash_counter = 0
    hashr_conn = None
    add_source_attribute = None
    query_batch_size = None
    DEFAULT_BATCH_SIZE = 50000

    def __init__(self, index_name, sketch_id, timeline_id=None):
        """Initialize The Sketch Analyzer.

        Args:
            index_name: Opensearch index name
            sketch_id: Sketch ID
            timeline_id: Timeline ID, default is None
        """
        self.index_name = index_name
        super().__init__(index_name, sketch_id, timeline_id=timeline_id)

    def connect_hashr(self):
        """Connect to the hashR postgres database.

        Returns:
          True: If connection is setup and successfully tested.

        Raises:
          Exception:  Raises an exception if the database connection details
                      cannot be loaded from the timesketch.conf file.
          Exception: If the connection cannot be setup or tested.
        """

        # Note: Provide connection infos in the data/timesketch.conf file!
        db_user = current_app.config.get("HASHR_DB_USER")
        db_pass = current_app.config.get("HASHR_DB_PW")
        db_address = current_app.config.get("HASHR_DB_ADDR")
        db_port = current_app.config.get("HASHR_DB_PORT")
        db_name = current_app.config.get("HASHR_DB_NAME")
        self.add_source_attribute = current_app.config.get("HASHR_ADD_SOURCE_ATTRIBUTE")
        self.query_batch_size = int(
            current_app.config.get("HASHR_QUERY_BATCH_SIZE", self.DEFAULT_BATCH_SIZE)
        )

        if not all(
            config_param
            for config_param in [
                db_user,
                db_pass,
                db_address,
                db_port,
                db_name,
                isinstance(self.add_source_attribute, bool),
            ]
        ):
            msg = (
                "The hashR analyzer is not able to load the hashR database "
                "information from the timesketch.conf file. Please make sure"
                " to uncomment the section and provide the required "
                "connection details!"
            )
            sys.tracebacklimit = 0
            raise Exception(msg)  # pylint: disable=broad-exception-raised

        db_string = (
            f"postgresql://{db_user}:{db_pass}@" f"{db_address}:{db_port}/{db_name}"
        )
        db_string_redacted = (
            f"postgresql://{db_user}:***@" f"{db_address}:{db_port}/{db_name}"
        )
        db = sqla.create_engine(db_string, connect_args={"connect_timeout": 10})
        try:
            db.connect()
            logger.debug(
                "Successful connected to hashR postgresql database: %s",
                db_string_redacted,
            )
            self.hashr_conn = db
        except sqla.exc.OperationalError as err:
            msg = (
                "Connection to the hashR postgres database failed! "
                '-- Provided connection string: "%s" '
                "-- Error message: %s",
                db_string_redacted,
                str(err),
            )
            logger.error(msg)
            sys.tracebacklimit = 0
            raise Exception(msg) from err  # pylint: disable=broad-exception-raised

        # Check if the required tables are present in the hashR database
        meta_data = sqla.MetaData(bind=self.hashr_conn)
        sqla.MetaData.reflect(meta_data)
        if not all(
            table_name in meta_data.tables
            for table_name in ["samples", "sources", "samples_sources"]
        ):
            msg = (
                "Could not find the required tables in the hashR database! "
                "Please ensure you have populated your hashR database using "
                "the most recent hashR project version!"
                "Required tables: samples, sources, samples_sources",
            )
            logger.error(msg)
            sys.tracebacklimit = 0
            raise Exception(msg) from KeyError  # pylint: disable=broad-exception-raised

        return True

    def batch_hashes(self, hash_list, batch_size=DEFAULT_BATCH_SIZE):
        """Generator function for slicing the hash list into batches

        Args:
          hash_list: A list of hash values.
          batch_size: Size of each batch. Default defined in class var.
        """
        for i in range(0, len(hash_list), batch_size):
            yield hash_list[i : i + batch_size]

    def check_against_hashr(self, sample_hashes: list):
        """Check a list of hashes against the hashR database.

        Args:
          sample_hashes:  A list of hash values that shall be checked
                          against the hashR database.

        Returns:
          matching_hashes:  A dict containing all hashes that are found in the
                            hashR database. (If add_source_attribute is True
                            it also contains the corresponding source images.)
          None:             If there are no matches, the function returns None.

        Raises:
          Exception:  Raises an exception if the provided sample_hashes
                      parameter is not of type list or dict.
        """
        if not isinstance(sample_hashes, list):
            raise Exception(  # pylint: disable=broad-exception-raised
                "The check_against_hashR function only accepts "
                "type<list> as input. But type "
                f"{type(sample_hashes)} was provided!"
            )

        meta_data = sqla.MetaData(bind=self.hashr_conn)
        sqla.MetaData.reflect(meta_data)
        samples_table = meta_data.tables["samples"]
        sources_table = meta_data.tables["sources"]
        samples_sources_table = meta_data.tables["samples_sources"]

        matching_hashes = {}

        with self.hashr_conn.connect() as conn:
            batch_counter = 1
            total_batches = ceil(len(sample_hashes) / self.query_batch_size)
            for batch in self.batch_hashes(sample_hashes, self.query_batch_size):
                logger.debug(
                    "Processing %d/%d batches...", batch_counter, total_batches
                )
                batch_counter += 1

                if self.add_source_attribute:
                    select_statement = (
                        sqla.select(
                            [
                                samples_sources_table.c.sample_sha256,
                                samples_sources_table.c.source_sha256,
                                sources_table.c.sourceid,
                                sources_table.c.reponame,
                            ]
                        )
                        .select_from(
                            samples_sources_table.join(
                                sources_table,
                                samples_sources_table.c.source_sha256
                                == sources_table.c.sha256,
                            )
                        )
                        .where(samples_sources_table.c.sample_sha256.in_(batch))
                    )

                    results = conn.execute(select_statement)

                else:
                    select_statement = sqla.select([samples_table.c.sha256]).where(
                        samples_table.c.sha256.in_(batch)
                    )

                    results = conn.execute(select_statement)

                for entry in results:
                    sample_hash = entry[0]
                    try:
                        if isinstance(entry[2], list):
                            source = f'{entry[3]}:{";".join(entry[2])}'
                        else:
                            source = f"{entry[3]}:{entry[2]}"
                    except (IndexError, sqla.exc.NoSuchColumnError):
                        source = "TagsOnly"
                    matching_hashes.setdefault(sample_hash, set()).add(source)

        logger.debug("Found %d matching hashes in hashR DB.", len(matching_hashes))

        # close database engine
        self.hashr_conn.dispose()
        logger.debug("Closed database connection.")
        return matching_hashes

    def annotate_event(
        self,
        hash_value: str,
        sources: Optional[Union[list, bool]],
        event: interface.Event,
    ):
        """Add tags and attributes to the given event, based on the rules for
        the analyzer.

        Args:
          hash_value: A string of a sha256 hash value.
          sources: A list of sources (image names) where this hash is known from.
          event:  The OpenSearch event object that contains this hash and needs
                  to be tagged or to add an attribute.
        """
        tags_container = ["known-hash"]
        if (
            hash_value
            == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        ):
            tags_container.append("zerobyte-file")
            self.zerobyte_file_counter += 1
            # Do not add any source attribute for zerobyte files,
            # since it exists in all sources.
            sources = False

        event.add_tags(tags_container)

        if sources:
            event.add_attributes({"hashR_sample_sources": list(sources)})

        event.commit()

    def run(self):
        """Entry point for the analyzer.

        Returns:
            String with summary of the analyzer result
        """
        # Connect to the hashR database
        self.connect_hashr()

        # Note: Add fieldnames that contain sha256 values in your events.
        query = (
            "_exists_:hash_sha256 OR _exists_:sha256 OR _exists_:hash OR "
            "_exists_:sha256_hash"
        )

        # Note: Add fieldnames that contain sha256 values in your events.
        return_fields = ["hash_sha256", "hash", "sha256", "sha256_hash"]

        # Generator of events based on your query.
        # Swap for self.event_pandas to get pandas back instead of events.
        events = self.event_stream(query_string=query, return_fields=return_fields)
        known_hash_counter = 0
        error_hash_counter = 0
        total_event_counter = 0
        hash_events_dict = {}
        logger.debug("Collecting a list of unique hashes to check against hashR.")
        for event in events:
            total_event_counter += 1
            hash_value = None
            for key in return_fields:
                if key in event.source.keys():
                    hash_value = event.source.get(key)
                    break
            if not hash_value:
                error_hash_counter += 1
                logger.warning(
                    "No matching field with a hash found in this event! "
                    "-- Event Source: %s",
                    str(event.source),
                )
                continue

            if len(hash_value) != 64:
                logger.warning(
                    "The extracted hash does not match the required "
                    "length (64) of a SHA256 hash. Skipping this "
                    "event! Hash: %s - Length: %d",
                    hash_value,
                    len(hash_value),
                )
                error_hash_counter += 1
                continue

            hash_events_dict.setdefault(hash_value, []).append(event)

        if len(hash_events_dict) <= 0:
            self.output.result_status = "SUCCESS"
            self.output.result_priority = "NOTE"
            self.output.result_summary = (
                "This timeline does not contain any fields with a sha256 hash."
            )
            return str(self.output)

        logger.debug(
            "Found %d unique hashes in %d events.",
            len(hash_events_dict),
            total_event_counter,
        )

        matching_hashes = self.check_against_hashr(list(hash_events_dict.keys()))
        if self.add_source_attribute:
            logger.debug("Start adding tags and attributes to events.")
            for sample_hash, hashr_value in matching_hashes.items():
                for event in hash_events_dict[sample_hash]:
                    known_hash_counter += 1
                    self.annotate_event(sample_hash, hashr_value, event)
            self.unique_known_hash_counter = len(matching_hashes)
        else:
            logger.debug("Start adding tags to events.")
            for sample_hash in matching_hashes:
                for event in hash_events_dict[sample_hash]:
                    known_hash_counter += 1
                    self.annotate_event(sample_hash, False, event)
            self.unique_known_hash_counter = len(matching_hashes)

        self.output.result_status = "SUCCESS"
        self.output.result_priority = "NOTE"
        self.output.result_summary = (
            f"Found a total of {total_event_counter} events that contain a "
            f"sha256 hash value - {self.unique_known_hash_counter} / "
            f"{len(hash_events_dict)} unique hashes known in hashR - "
            f"{known_hash_counter} events tagged - "
            f"{self.zerobyte_file_counter} entries were tagged as zerobyte "
            f"files - {error_hash_counter} events raised an error"
        )
        self.output.result_markdown = (
            f"Found a total of {total_event_counter} events that contain a "
            f"sha256 hash value\n* {self.unique_known_hash_counter} / "
            f"{len(hash_events_dict)} unique hashes known in hashR\n"
            f"* {known_hash_counter} events tagged\n"
            f"* {self.zerobyte_file_counter} entries were tagged as zerobyte "
            f"files\n* {error_hash_counter} events raised an error"
        )
        return str(self.output)


manager.AnalysisManager.register_analyzer(HashRLookup)
