# Profiles without provisioning databases

Read the engine and profile pages in the local NDB wiki. NDB profiles have separate compute, network, database-parameter, and software components. A READY network/compute profile is not proof that a requested engine or extension is installed.

The CLI's `profiles` command creates supporting compute and network profiles only. Config entries specify `engine`, a unique `label`, and optional compute sizing. Optional `compute_name` and `network_name` reuse separately named existing profiles. Defaults match the observed built-in profile: 1 socket × 4 cores, 32 GiB. These are future VM requirements; no capacity is allocated by creating the profile. Reuse existing parameter defaults when they meet the request.

Supported examples: `sqlserver_database` and `postgres_database`. Their parameter defaults include `DEFAULT_SQLSERVER_INSTANCE_PARAMS`, `DEFAULT_SQLSERVER_DATABASE_PARAMS`, and `DEFAULT_POSTGRES_PARAMS`. The installation creates built-in PostgreSQL, MariaDB, and MySQL software profiles. The wiki describes these OOB profiles as testing images; do not characterize them as production gold images.

SQL Server software profiles require a registered source VM with SQL Server installed and suitable OS/DB credentials. PostgreSQL vector search uses the pgvector extension; a PostgreSQL OOB software profile does not establish that pgvector is bundled. Capture a suitable registered PostgreSQL source with pgvector installed when requested. Creating/enabling `vector` in an actual database is a separate provisioning/configuration action.

If the user says to stop at profiles and has no source VMs, create/reuse the supporting profiles, report the software-image dependency, and stop. Do not provision seed VMs or database instances to work around that boundary. If source-image work is explicitly requested, perform it using the local engine guide rather than guessing packages or claiming an empty profile is ready.
