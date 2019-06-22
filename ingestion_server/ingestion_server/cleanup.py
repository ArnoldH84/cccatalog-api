import logging as log
import time
import multiprocessing
import uuid
import requests as re
import psycopg2
from psycopg2.extras import DictCursor, Json
from ingestion_server.indexer import database_connect, DB_BUFFER_SIZE
from urllib.parse import urlparse
"""
Functions for processing data when it is imported into the CC Catalog. This 
includes cleaning up malformed URLs and filtering out undesirable tags.
"""

# Number of records to buffer in memory at once
CLEANUP_BUFFER_SIZE = DB_BUFFER_SIZE

# Filter out automatically generated tags that aren't of any use to us.
# Note this list is case insensitive.
TAG_BLACKLIST = {
    'no person',
    'squareformat',
    'uploaded:by=flickrmobile',
    'uploaded:by=instagram',
    'flickriosapp:filter=flamingo',
    'cc0',
    'by',
    'by-nc',
    'by-nd',
    'by-sa',
    'by-nc-nd',
    'by-nc-sa',
    'pdm'
}

# Filter out low-confidence tags, which indicate that the machine-generated tag
# may be inaccurate.
TAG_MIN_CONFIDENCE = 0.90


class CleanupFunctions:
    """
    A cleanup function takes one parameter and returns the "cleaned" version if
    an update is required, otherwise None.

    Cleanup functions are dispatched in the _cleanup_config dictionary.
    """
    @staticmethod
    def cleanup_url(**kwargs):
        """
        Add protocols to the URI if they are missing, else return None.
        """
        provider = kwargs.get('provider')
        tls_support = kwargs.get('tls')
        url = kwargs.get('url')
        parsed = urlparse(url)
        if parsed.scheme == '':
            try:
                tls_supported = tls_support[provider]
            except KeyError:
                # The upstream content provider table is missing this provider,
                # so it wasn't tested for TLS support.
                tls_supported = TlsTest.test_tls_supported(url)
                tls_support[provider] = tls_supported
            if tls_supported:
                return "'https://{}'".format(url)
            else:
                return "'http://{}'".format(url)
        else:
            return None

    @staticmethod
    def cleanup_tags(tags):
        """
        Delete tags because they have low accuracy or because they are in the
        blacklist. If no change is made, return None.
        :return: A SQL fragment if an update is required or None
        """
        update_required = False
        tag_output = []
        if not tags:
            return None
        for tag in tags:
            below_threshold = False
            if 'accuracy' in tag and tag['accuracy'] < TAG_MIN_CONFIDENCE:
                below_threshold = True
            should_filter = (tag['name'].lower() in TAG_BLACKLIST or
                             below_threshold)
            if not should_filter:
                tag_output.append(tag)
                update_required = True

        if update_required:
            fragment = Json(tag_output)
            return fragment
        else:
            return None


# Define which tables, providers, and fields require cleanup. Map the field
# to a cleanup function that returns either a cleaned version of the field
# or 'None' to signal that no update is required.
_cleanup_config = {
    'tables': {
        'image': {
            'providers': {
                # Applies to all providers.
                '*': {
                    'fields': {
                        'tags': CleanupFunctions.cleanup_tags,
                        'url': CleanupFunctions.cleanup_url,
                        'creator_url': CleanupFunctions.cleanup_url,
                        'foreign_landing_url': CleanupFunctions.cleanup_url,
                        'thumbnail': CleanupFunctions.cleanup_url
                    }
                }
            }
        }
    }
}


class TlsTest:
    """
    URLs crawled from upstream are often lacking protocol information, or
    use HTTP when HTTPS is available. We have to test a small sample of the
    URLs to determine what protocol should be appended to each URL in the
    event that it is missing or incorrect.
    """
    @classmethod
    def test_tls_supported(cls, url):
        # No protocol provided
        if 'https://' not in url and 'http://' not in url:
            fixed_url = 'http://' + url
            return cls.test_tls_supported(fixed_url)
        # HTTP provided, but we want to check if HTTPS is supported as well.
        elif 'http://' in url:
            https = url.replace('http://', 'https://')
            try:
                res = re.get(https, timeout=2)
                log.info('{}:{}'.format(https, res.status_code))
                return 200 <= res.status_code < 400
            except re.RequestException:
                return False
        # If HTTPS is in the URL already, we're going to trust that HTTPS is
        # supported.
        return True

    @staticmethod
    def test_provider_tls_images_available(table, upstream_db):
        """
        Given a table, find 10 sample images and test whether HTTPS is
        available. If the majority supports TLS, we assume TLS is supported by
        the provider for all items.

        :return: A dict with key "provider" mapped to value True if TLS is
        available, else False.
        """
        provider_tls_supported = {}
        up_conn = psycopg2.connect(
            dbname='openledger',
            user='deploy',
            port=upstream_db['port'],
            password=upstream_db['password'],
            host=upstream_db['host'],
            connect_timeout=5
        )
        up_cur = up_conn.cursor(cursor_factory=DictCursor)
        provider_query = 'SELECT provider_identifier FROM content_provider;'
        up_cur.execute(provider_query)
        providers = up_cur.fetchall()

        for p in providers:
            p = p[0]
            sample_query = "SELECT thumbnail, url FROM temp_import_{table}" \
                           " WHERE provider='{provider}' LIMIT 10" \
                           .format(provider=p, table=table)
            up_cur.execute(sample_query)
            img_list = [
                r['thumbnail'] if r['thumbnail'] else r['url'] for r in up_cur
            ]

            http_score = 0
            https_score = 0
            for thumb in img_list:
                tls_supported = TlsTest.test_tls_supported(thumb)
                if tls_supported:
                    https_score += 1
                else:
                    http_score += 1
            if https_score >= http_score:
                provider_tls_supported[p] = True
            else:
                provider_tls_supported[p] = False
            log.info(
                "Provider '{}' TLS support: {}"
                .format(p, provider_tls_supported[p])
            )
        up_cur.close()
        up_conn.close()
        return provider_tls_supported


def _clean_data_worker(rows, temp_table, providers_config, tls_support):
    log.info('Starting data cleaning worker')
    global_field_to_func = providers_config['*']['fields']
    worker_conn = database_connect()
    log.info('Data cleaning worker connected to database')
    write_cur = worker_conn.cursor(cursor_factory=DictCursor)
    log.info('Cleaning {} rows'.format(len(rows)))
    start_time = time.time()
    for row in rows:
        # Map fields that need updating to their cleaning functions
        provider = row['provider']
        _id = row['id']
        if provider in providers_config:
            provider_field_to_func = providers_config[provider]['fields']
            # Merge provider-local and global function field mappings
            fields_to_update = \
                {**global_field_to_func, **provider_field_to_func}
        else:
            fields_to_update = global_field_to_func
        # Map fields to their cleaned data
        cleaned_data = {}
        for update_field in fields_to_update:
            dirty_value = row[update_field]
            if not dirty_value:
                continue
            cleaning_func = fields_to_update[update_field]
            if cleaning_func == CleanupFunctions.cleanup_url:
                clean = cleaning_func(
                    url=dirty_value, tls=tls_support, provider=provider
                )
            else:
                clean = cleaning_func(dirty_value)
            if clean:
                cleaned_data[update_field] = clean
        # Generate SQL update for all the fields we just cleaned
        update_field_expressions = []
        for field in cleaned_data:
            update_field_expressions.append(
                '{field} = {cleaned}'.format(
                    field=field,
                    cleaned=cleaned_data[field]
                )
            )
        if len(update_field_expressions) > 0:
            update_query = '''
                UPDATE {temp_table} SET {field_expressions} WHERE id = {_id}
            '''.format(
                temp_table=temp_table,
                field_expressions=', '.join(update_field_expressions),
                _id=_id
            )
            write_cur.execute(update_query)
    log.info('Worker committing changes...')
    worker_conn.commit()
    write_cur.close()
    worker_conn.close()
    end_time = time.time()
    total_time = end_time - start_time
    log.info('Worker finished batch in {}'.format(total_time))
    return True


def clean_image_data(table, upstream_db):
    """
    Data from upstream can be unsuitable for production for a number of reasons.
    Clean it up before we go live with the new data.

    :param table: The staging table for the new data
    :param upstream_db: A dict specifying the connection details of the upstream
    database.
    :return: None
    """
    log.info('Testing TLS support for each provider...')
    tls_support = TlsTest.test_provider_tls_images_available(table, upstream_db)
    # Map each table to the fields that need to be cleaned up. Then, map each
    # field to its cleanup function.
    log.info('Cleaning up data...')
    start_time = time.time()
    table_config = _cleanup_config['tables'][table]

    # Pull data from selected providers only.
    providers = list(_cleanup_config['tables'][table]['providers'])

    # Determine which fields will need updating
    fields_to_clean = set()
    for p in providers:
        _fields = list(table_config['providers'][p]['fields'])
        for f in _fields:
            fields_to_clean.add(f)

    cleanup_selection = "SELECT id, provider, {fields} from {table}".format(
                            fields=', '.join(fields_to_clean),
                            table='temp_import_{}'.format(table),
                        )
    log.info('Running cleanup on selection "{}"'.format(cleanup_selection))
    conn = database_connect(autocommit=True)
    cursor_name = '{}-{}'.format(table, str(uuid.uuid4()))
    with conn.cursor(
            name=cursor_name, cursor_factory=DictCursor, withhold=True
    ) as iter_cur:
        iter_cur.itersize = CLEANUP_BUFFER_SIZE
        iter_cur.execute(cleanup_selection)

        # Clean each field as specified in _cleanup_config.
        provider_config = table_config['providers']

        log.info('Fetching first batch')
        batch = iter_cur.fetchmany(size=CLEANUP_BUFFER_SIZE)
        jobs = []
        num_workers = multiprocessing.cpu_count()
        num_cleaned = 0
        while batch:
            # Divide updates into jobs for parallel execution.
            start = time.time()
            temp_table = 'temp_import_{}'.format(table)
            job_size = int(len(batch) / num_workers)
            last_end = -1
            log.info('Dividing work')
            for n in range(1, num_workers + 1):
                log.info('Scheduling job {}'.format(n))
                start = last_end + 1
                end = job_size * n
                last_end = end
                # Arguments for parallel _clean_data_worker calls
                jobs.append(
                    (batch[start:end], temp_table, provider_config, tls_support)
                )
            pool = multiprocessing.Pool(processes=num_workers)
            log.info('Starting {} cleaning jobs'.format(len(jobs)))
            conn.commit()
            pool.starmap(_clean_data_worker, jobs)
            pool.close()
            num_cleaned += len(batch)
            end = time.time()
            rate = len(batch) / (end - start)
            log.info('Batch finished, records/s: cleanup_rate={}'.format(rate))
            log.info(
                'Fetching next batch. Num records cleaned so far: {}'
                .format(num_cleaned))
            jobs = []
            batch = iter_cur.fetchmany(size=CLEANUP_BUFFER_SIZE)
    conn.commit()
    iter_cur.close()
    conn.close()
    end_time = time.time()
    cleanup_time = end_time - start_time
    log.info('Cleaned all records in {} seconds'.format(
        cleanup_time)
    )