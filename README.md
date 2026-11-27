# ckanext-aiextract

[![CI](https://github.com/MBueschelberger/ckanext-aiextract/actions/workflows/ci.yml/badge.svg)](https://github.com/MBueschelberger/ckanext-aiextract/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ckanext-aiextract)](https://pypi.org/project/ckanext-aiextract/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

A CKAN extension that automates feeding information from documents into AI applications.

Whenever a document resource (by default: PDF) is created or updated, the extension enqueues a
background job that sends the file to a configurable text-extraction service and uploads the
extracted content back to the dataset. A new tab in the dataset view shows the extraction status
and lets users trigger or delete extractions manually.

## Requirements

| CKAN version    | Compatible?   |
| --------------- | ------------- |
| 2.9 and earlier | no            |
| 2.10            | yes           |
| 2.11            | yes           |

- Python 3.10 or newer
- CKAN background jobs worker (`ckan jobs worker`) running
- A text-extraction HTTP endpoint that accepts multipart file uploads and returns a file response

## Installation

1. Activate your CKAN virtual environment, for example:

   ```bash
   . /usr/lib/ckan/default/bin/activate
   ```

2. Install the extension from PyPI:

   ```bash
   pip install ckanext-aiextract
   ```

3. Add `aiextract` to the `ckan.plugins` setting in your CKAN config file
   (by default `/etc/ckan/default/ckan.ini`).

4. Restart CKAN and the background jobs worker.

### Development installation

```bash
git clone git@github.com:MBueschelberger/ckanext-aiextract.git
cd ckanext-aiextract
pip install -e ".[dev]"
pre-commit install
```

Unit tests need CKAN 2.11 installed (`pip install "ckan[requirements]==2.11.*"`), but no
CKAN config, database, Solr or Redis:

```bash
pytest
```

## Config settings

| Option                          | Default                         | Description |
| ------------------------------- | ------------------------------- | ----------- |
| `ckanext.aiextract.extract_url` | `""`                            | URL of the text-extraction API endpoint |
| `ckanext.aiextract.ckan_token`  | `""`                            | API token used by background jobs. Without it, only public datasets can be processed |
| `ckanext.aiextract.db_url`      | value of `sqlalchemy.url`       | Database for the extension's job table |
| `ckanext.aiextract.formats`     | `pdf`                           | Space-separated list of resource formats to process |
| `ckanext.aiextract.job_timeout` | `1800`                          | Seconds after which a pending task is considered stale |
| `ckanext.aiextract.ssl_verify`  | `true`                          | Verify TLS certificates for outgoing requests. Set to `false` only for development setups with self-signed certificates |
| `ckanext.aiextract.callback_base_url` | value of `ckan.site_url`  | Base URL background jobs use to reach CKAN: status callbacks (`aiextract_hook`) and downloads of files hosted by this CKAN. In Docker setups set it to the internal address (e.g. `http://ckan:5000`) so jobs bypass the public reverse proxy / WAF |

Status callbacks of a job use a 30 s timeout and are not retried on HTTP errors (e.g. 504);
a failed callback is logged and the job continues.

In the official CKAN Docker images, every option can also be set as an environment variable using
the `CKANINI__` prefix, e.g.:

```bash
CKANINI__CKANEXT__AIEXTRACT__EXTRACT_URL=https://extract.example.org/api/extract
CKANINI__CKANEXT__AIEXTRACT__CKAN_TOKEN=<BACKGROUND_JOBS_API_TOKEN>
CKANINI__CKANEXT__AIEXTRACT__JOB_TIMEOUT=1800
CKANINI__CKANEXT__AIEXTRACT__SSL_VERIFY=false   # development only
CKANINI__CKANEXT__AIEXTRACT__CALLBACK_BASE_URL=http://ckan:5000
```

## Operations

Versions before 1.1.0 could make a CKAN worker block itself on a `task_status` row lock
when a PDF was uploaded or updated while an old extraction task existed. Independent of the
fix, the following safeguards are recommended:

| Measure | Effect |
| ------- | ------ |
| uWSGI `harakiri` of a few minutes (e.g. `UWSGI_HARAKIRI=120`) instead of hours | A hanging request is killed after 2 min |
| More uWSGI capacity, e.g. `-p 4 --threads 4` | A single hanging request no longer blocks the whole instance |
| PostgreSQL: `ALTER DATABASE <ckan-db> SET idle_in_transaction_session_timeout = '5min';` | Open transactions holding locks are terminated automatically |
| Docker autoheal (e.g. `willfarrell/autoheal`) or a healthcheck with restart | Docker does not restart `unhealthy` containers on its own |

One-time cleanup of stuck and legacy dataset-level tasks (before or after updating to 1.1.0):

```sql
-- inspect
SELECT id, entity_id, entity_type, state, last_updated
FROM task_status
WHERE task_type = 'aiextract'
  AND (entity_type = 'package' OR state IN ('running', 'pending', 'submitting'));

-- delete
DELETE FROM task_status
WHERE task_type = 'aiextract'
  AND (entity_type = 'package'
       OR (state IN ('running', 'pending', 'submitting')
           AND last_updated < now() - interval '1 hour'));
```

## Releasing

Versions follow `MAJOR.MINOR.PATCH` and are managed with [bumpver](https://github.com/mbarkhau/bumpver):

```bash
bumpver update --patch   # or --minor / --major
```

This updates `pyproject.toml`, commits, tags and pushes. The pushed tag triggers the CI workflow,
which builds the package and publishes it to PyPI via Trusted Publishing.

## Authors

- **Thomas Hanke** ([@ThHanke](https://github.com/ThHanke)) — main author

## Maintainers

- **Matthias Büschelberger** ([@MBueschelberger](https://github.com/MBueschelberger))

## License

This project is licensed under the GNU Affero General Public License v3.0 or later —
see [LICENSE](LICENSE).
