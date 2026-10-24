# Staging runbook: validate the 1.0.0 → 1.1.0 upgrade before it touches a real site

Nothing here modifies a live site. Everything runs on a **copy**.

## A. Frappe Cloud (recommended for the real rollout)

1. Take a backup of production (database **and** files) and keep a copy off-platform.
2. Create a **new site on a test bench** (or restore that backup onto a test bench). Same Frappe/ERPNext versions as production.
3. Bench → Apps → add this branch (`feature/schedule-revisions`) → **Deploy**. Frappe Cloud builds the bundles and runs `bench migrate` for the site.
4. Read the migrate output. Expect, for a site that had PPC 1.0.0 cards with baselines: `PPC: added schedule history to N Job Card(s)`, and **no** `Unknown column`.
   If migrate fails, nothing is lost: re-deploy after fixing; the patch re-runs (it is only logged on success).
5. In the browser as a PPC Manager: Job Cards tab → **Check Schedule Consistency**. Cards that have a schedule but no baseline show as *missing baseline*:
   use **Initialize Missing Baselines** (preview first). They are recorded as *"initialized from existing schedule"* - the true original is unknowable.
6. Run the manual checks in [VALIDATION_CHECKLIST.md](VALIDATION_CHECKLIST.md#manual-staging-steps-30-minutes).
7. Only then repeat steps 1-4 on production in a quiet window. Rollback = redeploy the previous commit (the extra fields/table are inert for 1.0.0); a full
   restore from the step-1 backup is the last resort.

## B. A local disposable copy (what was done for the release report)

You need a dump of the real site made **by 1.0.0** (so it has the old schema) and the bench the app runs in. No MariaDB root password is required:
run a **private MariaDB** under your own user on another port.

```bash
# 1. private database (own data dir and port; the system MariaDB is not touched)
ROOT=~/ppc-staging; mkdir -p $ROOT/data $ROOT/run
cat > $ROOT/my.cnf <<EOF
[mysqld]
datadir=$ROOT/data
socket=$ROOT/run/mysqld.sock
port=3307
bind-address=127.0.0.1
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
character-set-client-handshake=FALSE
innodb_buffer_pool_size=384M
max_allowed_packet=256M
EOF
mariadb-install-db --no-defaults --datadir=$ROOT/data --auth-root-authentication-method=normal --skip-test-db
nohup mariadbd --defaults-file=$ROOT/my.cnf > $ROOT/mysqld.log 2>&1 &
mariadb --defaults-file=$ROOT/my.cnf -uroot -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'CHANGE-ME'; \
  CREATE USER 'root'@'127.0.0.1' IDENTIFIED BY 'CHANGE-ME'; GRANT ALL ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION; FLUSH PRIVILEGES;"

# 2. the staging site on that database, then restore the 1.0.0 dump into it
cd ~/frappe-bench
bench new-site stage.local --db-host 127.0.0.1 --db-port 3307 --db-root-username root --db-root-password CHANGE-ME --admin-password CHANGE-ME
bench --site stage.local --force restore /path/to/1.0.0-dump.sql.gz --db-root-username root --db-root-password CHANGE-ME
bench --site stage.local set-config mute_emails 1        # a copy of real data must never send mail
bench --site stage.local set-config pause_scheduler 1
bench --site stage.local set-config allow_tests true      # only if you will run the test-suites
#   keep developer_mode OFF on the copy: it would let the copy write DocType files into the shared app folder

# 3. snapshot, upgrade, verify (scripts in ppc_production_control/tests/migration/, run from ~/frappe-bench/sites)
cd ~/frappe-bench/sites
../env/bin/python ../apps/ppc_production_control/ppc_production_control/tests/migration/snapshot_site.py stage.local before before.json
cd ..; bench --site stage.local migrate; cd sites
../env/bin/python ../apps/ppc_production_control/ppc_production_control/tests/migration/verify_upgrade.py stage.local before.json   # prints 21 checks
cd ..
bench --site stage.local serve --port 8001                # separate web server; the browser tests use PPC_E2E_BASE=http://localhost:8001
```

**Rollback test**: `bench --site stage.local --force restore <the same dump>` returns the database to exactly its 1.0.0 state; or run the 1.0.0 code
(`git archive <1.0.0 commit>` into a folder, put it first on `PYTHONPATH`) against the upgraded database to prove an app rollback works.

**Clean up**: `kill` the `mariadbd` and `bench serve` processes, then `rm -rf ~/ppc-staging` and `rm -rf ~/frappe-bench/sites/stage.local`.
The copy contains real data: do not leave it around.

## C. What the migration check must show

| Check | Expected |
|---|---|
| `bench migrate` exit code | 0 (a second and third run are no-ops for PPC) |
| `PPC Schedule Revision` DocType and the nine new Job Card columns | exist |
| `ppc_original_start/end` of every card | **byte-identical** to before (compare a checksum) |
| Cards with a 1.0.0 baseline | each has revision 0; cards moved since also one *Migration* revision |
| `changed_by` on migrated rows | empty (nothing invented) |
| Cards without any baseline | untouched (left for the preview-first button) |
| Audit log rows | unchanged |
| Re-running the patch / `migrate` | adds nothing |
