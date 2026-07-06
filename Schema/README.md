# Schema

This folder contains a simple, readable copy of the SQLite schema used by the load balancer.

## Files

- `001_tables.sql` creates the schema metadata, configuration, event, metrics, and rule-version tables.
- `002_indexes.sql` creates the indexes used for common history and metrics lookups.

## Apply manually

From the repository root:

```bash
sqlite3 load_balancer.db < Schema/001_tables.sql
sqlite3 load_balancer.db < Schema/002_indexes.sql
```

The scripts are idempotent and can be run more than once.

## Runtime note

The application currently initializes and versions its schema from `src/load_balancer/store.py`. These SQL files mirror schema version 1 for documentation and manual database setup; they do not change the application's existing startup behavior.
