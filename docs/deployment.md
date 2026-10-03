# Running Pinny on AWS

Status: 2026-09-28, Step 5 of `docs/training-site-plan.md`. Everything this
guide installs is in `deploy/`. It is written to be followed step by step,
by hand or by Claude Code on the server. Commands that start with `sudo`
run on the server; commands that start with `aws` can run anywhere the
AWS CLI is signed in to your account (for example AWS CloudShell in the
console).

Contents:

1. [How it fits together](#1-how-it-fits-together)
2. [AWS setup (once)](#2-aws-setup-once)
3. [Connect to the server](#3-connect-to-the-server)
4. [First-time setup on the server](#4-first-time-setup-on-the-server)
5. [First admin sign-in](#5-first-admin-sign-in)
6. [Deploy a new version](#6-deploy-a-new-version)
7. [Roll back](#7-roll-back)
8. [Backups](#8-backups)
9. [Restore](#9-restore)
10. [Add, remove or reset a user](#10-add-remove-or-reset-a-user)
11. [Rotate secrets](#11-rotate-secrets)
12. [Staging and production](#12-staging-and-production)
13. [Working hours: the server is off at night](#13-working-hours-the-server-is-off-at-night)
14. [Alerts: what each one means and what to do](#14-alerts-what-each-one-means-and-what-to-do)
15. [Logs](#15-logs)
16. [Training (torch)](#16-training-torch)
17. [Go-live checklist](#17-go-live-checklist)
18. [Decisions still open](#18-decisions-still-open)

## 1. How it fits together

```
 Browsers (office, Surface Pro, phone)
    |  https://pinny.ecinc.us          https://staging.pinny.ecinc.us
    v  (DNS A records -> the Elastic IP)
 +-- EC2 instance (Ubuntu 24.04, m7i.large: 2 vCPU / 8 GB, 100 GB gp3) ----------------+
 |  security group: TCP 80 + 443 open to the world; nothing else (connect with SSM)     |
 |                                                                                      |
 |  Caddy (ports 80, 443): Let's Encrypt certificates, HTTP -> HTTPS, body-size limits  |
 |     |  127.0.0.1:8001                             |  127.0.0.1:8002                  |
 |     v                                             v                                  |
 |  pinny-web@production (user pinny-production)   pinny-web@staging (pinny-staging)    |
 |     sign-in, API, pages; never opens a PDF        same, separate data and members    |
 |     |  jobs.sqlite3 (queue)                                                          |
 |     v                                                                                |
 |  pinny-worker-interactive@production  uploads, page images    no network at all,     |
 |  pinny-worker-scan@production         template scans          user                   |
 |  pinny-worker-train@production        training (when added)   pinny-production-worker|
 |     (and the same three for staging)                          (train: web user)      |
 |                                                                                      |
 |  /srv/pinny/production/   documents/ models/ crops/ exports/ *.sqlite3               |
 |  /srv/pinny/staging/      the same, separate                                         |
 |                                                                                      |
 |  timers: backup (daily 18:15 New York) -> S3 bucket, 30 days                         |
 |          health check every 5 min -> SNS topic -> email                              |
 |          restore test (first Monday of the month)                                    |
 +--------------------------------------------------------------------------------------+
    EventBridge Scheduler: start 07:00, stop 19:00, every day, New York time
    Data Lifecycle Manager: weekly EBS snapshot, Friday 21:30 UTC, keep 4
```

Where things are on the server:

| Path | What |
|---|---|
| `/etc/pinny/production.env`, `/etc/pinny/staging.env` | Pinny's settings per profile (from `deploy/env/*.env.example`) |
| `/etc/pinny/ops.env` | AWS region, backup bucket, alert topic, git URL (for the scripts) |
| `/opt/pinny/<profile>/releases/<time>-<sha>/` | one release: `src/` (the code), `venv/`, `release.env` (`PINNY_VERSION`) |
| `/opt/pinny/<profile>/current`, `previous` | links to the running and the previous release |
| `/srv/pinny/<profile>/` | `PINNY_DATA_DIR`: drawings, databases, models |
| `/usr/local/sbin/pinny-*` | the scripts from `deploy/bin/` |
| `/etc/systemd/system/pinny-*` | the units from `deploy/systemd/` |
| `/etc/caddy/Caddyfile` | from `deploy/caddy/Caddyfile` |
| `/var/log/caddy/pinny-<profile>.log` | access logs (rolled at 50 MB, kept 30 days) |
| `/var/lib/pinny-backup/<profile>/` | backup status and pre-deploy database snapshots |

Security, in short: HTTPS everywhere (Caddy); every page behind Pinny's
own sign-in (passwords, invite-only set-password links); Pinny listens only
on `127.0.0.1`; PDFs are opened only by the worker services, which have no
network, run as a separate user and can write only `documents/`, `tmp/`
and the job queue; every service runs with systemd's sandboxing
(read-only system, no home directories, no privilege gain, memory and CPU
caps); no AWS keys on disk (the instance role); backups encrypted in a
private, versioned bucket.

Rough monthly cost (us-east-1 prices, 2026): about **$52**. Compute about
$37 (m7i.large for about 365 hours a month: 12 hours x ~30.4 days),
disk $8 (100 GB gp3, charged all month), Elastic IP $3.65 (public IPv4
addresses are charged all month, running or not), and a few dollars for
S3 backups and EBS snapshots. SNS email, EventBridge Scheduler and Data
Lifecycle Manager are free at this size. With the instance off most of
the week, a Savings Plan or reserved instance does not pay off.

## 2. AWS setup (once)

Everything runs in **`us-east-1`** (N. Virginia); the commands and the
files in `deploy/aws/` already say so. Replace these placeholders
wherever they appear:

| Placeholder | Example | Where you find it |
|---|---|---|
| `ACCOUNT_ID` | `123456789012` | console, top right menu |
| `BUCKET_NAME` | `pinny-backups-123456789012` | step 2.1 (bucket names are global, so add your account id) |
| `INSTANCE_ID` | `i-0abc...` | step 2.5 |
| `YOUR_EMAIL` | `you@ecinc.us` | where alerts go |

### 2.1 Backup bucket (S3)

Console: S3 → Create bucket.

1. Name `BUCKET_NAME`, your region.
2. Object Ownership: ACLs disabled. Block *all* public access: on.
3. Bucket Versioning: **Enable**.
4. Default encryption: SSE-S3 (Amazon S3 managed keys). SSE-KMS also works
   but then the instance role also needs `kms:GenerateDataKey` and
   `kms:Decrypt` on that key.
5. Create. Then open the bucket → Permissions → Bucket policy → paste
   `deploy/aws/s3-bucket-policy.json` (with `BUCKET_NAME` replaced). It
   refuses any request that is not over HTTPS.
6. Management → Create lifecycle rule, twice, matching
   `deploy/aws/s3-lifecycle.json`: rule 1, prefix `pinny/`, expire current
   versions after 30 days, permanently delete noncurrent versions after 30
   days, delete incomplete multipart uploads after 7 days; rule 2, prefix
   `pinny/`, delete expired object delete markers.

CLI (the same):

```sh
aws s3api create-bucket --bucket BUCKET_NAME --region us-east-1
aws s3api put-public-access-block --bucket BUCKET_NAME --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket BUCKET_NAME --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket BUCKET_NAME --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-policy --bucket BUCKET_NAME --policy file://deploy/aws/s3-bucket-policy.json
aws s3api put-bucket-lifecycle-configuration --bucket BUCKET_NAME \
  --lifecycle-configuration file://deploy/aws/s3-lifecycle.json
```

The instance can add and read backups but cannot delete them (its policy
has no delete permission), and versioning keeps overwritten objects, so a
compromised server cannot wipe the backups.

### 2.2 Alert topic (SNS)

Console: Simple Notification Service → Topics → Create topic → Standard,
name `pinny-alerts`. Then Create subscription → protocol Email, endpoint
`YOUR_EMAIL`. Open the confirmation email and click the link (alerts are
not delivered until you do). Add more subscriptions for anyone else who
should get alerts.

```sh
aws sns create-topic --name pinny-alerts --region us-east-1
aws sns subscribe --topic-arn arn:aws:sns:us-east-1:ACCOUNT_ID:pinny-alerts \
  --protocol email --notification-endpoint YOUR_EMAIL --region us-east-1
```

### 2.3 Instance role (IAM)

Console: IAM → Roles → Create role → AWS service → EC2. Attach the managed
policy **AmazonSSMManagedInstanceCore** (for Session Manager, section 3).
Name it `pinny-instance`. Then open the role → Add permissions → Create
inline policy → JSON → paste `deploy/aws/iam-instance-policy.json` with the
placeholders replaced, name it `pinny-backups-and-alerts`.

That policy allows exactly: list the bucket under `pinny/`, put and get
objects under `pinny/`, and publish to the `pinny-alerts` topic. Nothing
else.

```sh
aws iam create-role --role-name pinny-instance --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name pinny-instance \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name pinny-instance --policy-name pinny-backups-and-alerts \
  --policy-document file://deploy/aws/iam-instance-policy.json
aws iam create-instance-profile --instance-profile-name pinny-instance
aws iam add-role-to-instance-profile --instance-profile-name pinny-instance --role-name pinny-instance
```

### 2.4 Security group

Console: EC2 → Security Groups → Create security group, name `pinny-web`,
in the default VPC (or yours). Inbound rules: HTTPS (443) from
`0.0.0.0/0` and `::/0`; HTTP (80) from `0.0.0.0/0` and `::/0`. **No SSH
rule** (use Session Manager). Leave outbound as "all traffic". See
`deploy/aws/security-group.txt`. Never open 8001, 8002 or anything else.

If you must use SSH instead of Session Manager: add SSH (22) from *your*
IP address only (`x.x.x.x/32`), and remove the rule when you are done.

### 2.5 The EC2 instance

Console: EC2 → Launch instance.

1. Name `pinny`. AMI: **Ubuntu Server 24.04 LTS** (64-bit x86).
2. Instance type **m7i.large** (2 vCPU, 8 GB).
3. Key pair: "Proceed without a key pair" if you use Session Manager.
4. Network: security group `pinny-web`; auto-assign public IP can stay on
   (the Elastic IP replaces it).
5. Storage: **100 GiB gp3**, **Encrypted** (the default `aws/ebs` key is
   fine). A customer-managed KMS key also works, but then the scheduler role
   in step 2.9 needs `kms:CreateGrant` on it.
6. Advanced details: IAM instance profile `pinny-instance`; Metadata
   version **V2 only (token required)**; Termination protection **enabled**;
   Shutdown behavior **Stop**.
7. Launch. Write down the instance id (`INSTANCE_ID`).
8. Open the instance → Storage → the volume → Tags → add
   `pinny-snapshots` = `weekly` (step 2.8 uses it).

### 2.6 Elastic IP

Console: EC2 → Elastic IPs → Allocate → then Actions → Associate → the
`pinny` instance. The address stays the same when the instance stops and
starts, so DNS keeps working while it is off.

```sh
aws ec2 allocate-address --domain vpc --region us-east-1              # note AllocationId and PublicIp
aws ec2 associate-address --allocation-id eipalloc-... --instance-id INSTANCE_ID --region us-east-1
```

### 2.7 DNS

Create two **A records** pointing at the Elastic IP, with a short TTL
(300 s) at first:

| Name | Type | Value |
|---|---|---|
| `pinny.ecinc.us` | A | the Elastic IP |
| `staging.pinny.ecinc.us` | A | the Elastic IP |

`ecinc.us` is on **Bluehost**. Sign in at bluehost.com → **Domains** →
`ecinc.us` → **Manage** (or the gear) → **DNS** tab → **DNS Records** →
**Add Record**:

1. Type **A**, Host Record **`pinny`**, Points To the Elastic IP, TTL
   the shortest offered (for example 1 hour). Save.
2. Again with Host Record **`staging.pinny`**.

Leave every other record alone. In particular the MX and other email
records stay as they are, so company email is not affected. If the DNS
page lists a **CAA** record, it must allow `letsencrypt.org` (most
domains have none, which is fine). If Bluehost says the domain uses
other nameservers, add the records wherever those nameservers are
managed instead.

Check from your computer after a few minutes: `nslookup pinny.ecinc.us`
and `nslookup staging.pinny.ecinc.us` must both answer the Elastic IP.
Caddy can only get certificates once both names resolve.

### 2.8 Weekly disk snapshots (Data Lifecycle Manager)

On top of the daily backups, a weekly snapshot of the whole disk lets you
rebuild the server in minutes. Snapshots must be taken while the instance
runs (07:00-19:00 New York time). DLM schedules are in UTC:
Friday **21:30 UTC** is 17:30 in New York in summer (EDT, UTC-4) and 16:30
in winter (EST, UTC-5), inside the window either way.

Console: EC2 → Lifecycle Manager → Create lifecycle policy → EBS snapshot
policy → target resource type Volume, target tag `pinny-snapshots` =
`weekly` → IAM role: default role → schedule: custom cron expression
`cron(30 21 ? * FRI *)`, retention **count 4** → Create.

```sh
aws dlm create-default-role --resource-type snapshot --region us-east-1
aws dlm create-lifecycle-policy --region us-east-1 --state ENABLED \
  --description "Pinny weekly snapshots" \
  --execution-role-arn arn:aws:iam::ACCOUNT_ID:role/AWSDataLifecycleManagerDefaultRole \
  --policy-details file://deploy/aws/dlm-weekly-snapshots.json
```

### 2.9 Start and stop schedule (EventBridge Scheduler)

The instance runs 07:00-19:00 New York time every day, and is stopped
at night. Two schedules in EventBridge Scheduler call EC2
directly (no Lambda). They need a role that may start and stop this one
instance and nothing else.

Console:

1. IAM → Roles → Create role → Custom trust policy → paste
   `deploy/aws/scheduler-role-trust.json` (with `ACCOUNT_ID`). Name it
   `pinny-scheduler`. Add an inline policy from
   `deploy/aws/scheduler-role-policy.json` (with `ACCOUNT_ID`,
   `INSTANCE_ID`).
2. Amazon EventBridge → Scheduler → Schedules → Create schedule:
   name `pinny-start-daily`; recurring, cron-based,
   `0 7 * * ? *`; time zone **America/New_York**; flexible time
   window Off. Target: **All APIs** → search "EC2" → **StartInstances**;
   input `{"InstanceIds": ["INSTANCE_ID"]}`. Permissions: use existing role
   `pinny-scheduler`. Create.
3. The same again: `pinny-stop-daily`, `0 19 * * ? *`,
   America/New_York, target EC2 **StopInstances**, same input and role.

CLI (edit `ACCOUNT_ID` and `INSTANCE_ID` in the four files first):

```sh
aws iam create-role --role-name pinny-scheduler \
  --assume-role-policy-document file://deploy/aws/scheduler-role-trust.json
aws iam put-role-policy --role-name pinny-scheduler --policy-name start-stop-pinny \
  --policy-document file://deploy/aws/scheduler-role-policy.json
aws scheduler create-schedule --region us-east-1 --cli-input-json file://deploy/aws/scheduler-start.json
aws scheduler create-schedule --region us-east-1 --cli-input-json file://deploy/aws/scheduler-stop.json
```

Check: EventBridge → Scheduler shows both schedules with their next run
times. Section 13 explains what happens at 19:00 and how to start the
server by hand.

## 3. Connect to the server

**Recommended: Session Manager** (no open SSH port, no keys). Console:
EC2 → Instances → `pinny` → Connect → Session Manager → Connect. You get a
shell as `ssm-user`, who can use `sudo`. (Ubuntu 24.04 images come with the
SSM agent; the `AmazonSSMManagedInstanceCore` policy from step 2.3 lets it
register. If "Connect" is greyed out, wait five minutes after the first
boot.) From a terminal with the AWS CLI and the Session Manager plugin:
`aws ssm start-session --target INSTANCE_ID --region us-east-1`.

**Alternative: SSH**, only with the temporary port 22 rule from step 2.4
and a key pair chosen at launch: `ssh -i key.pem ubuntu@<Elastic IP>`.

To use Claude Code on the server, install it in that shell and start it in
the repository checkout (step 4.1).

## 4. First-time setup on the server

### 4.1 Get the code

```sh
sudo git clone https://github.com/cnealblack8191/agentpinny.git /usr/local/src/agentpinny
sudo git -C /usr/local/src/agentpinny checkout training-site
```

The repository is currently **public**, so the server clones it over
HTTPS with no key. (Drawings, databases and settings are never in the
repository, only code.) If you make it private later, give the server a
read-only deploy key instead:

```sh
sudo ssh-keygen -t ed25519 -N "" -C pinny-server -f /root/.ssh/pinny_deploy
sudo cat /root/.ssh/pinny_deploy.pub
```

On GitHub: the repository → Settings → Deploy keys → Add deploy key →
paste it, leave "Allow write access" off. Then on the server:

```sh
printf 'Host github.com\n  IdentityFile /root/.ssh/pinny_deploy\n  IdentitiesOnly yes\n' | sudo tee -a /root/.ssh/config
sudo ssh -o StrictHostKeyChecking=accept-new -T git@github.com    # "successfully authenticated" is good
sudo git clone git@github.com:cnealblack8191/agentpinny.git /usr/local/src/agentpinny
```

and later set `PINNY_GIT_URL=git@github.com:cnealblack8191/agentpinny.git`
in `/etc/pinny/ops.env` (step 4.3).

### 4.2 Run the bootstrap

Look first (changes nothing, prints every command it would run):

```sh
sudo /usr/local/src/agentpinny/deploy/bin/pinny-bootstrap --dry-run
```

Then run it. For staging only first (Step 6b of the plan):

```sh
sudo /usr/local/src/agentpinny/deploy/bin/pinny-bootstrap --profiles staging
```

or both profiles: `sudo /usr/local/src/agentpinny/deploy/bin/pinny-bootstrap`
(also the command that adds production later, after a staging-only start).

It installs: Ubuntu packages (Python 3.12 and venv, git, sqlite3, curl,
unzip, and `tesseract-ocr` for the optional OCR in docs/ocr.md); Caddy from Caddy's own apt repository (signing key checked by
fingerprint); AWS CLI v2 (signature checked); the users `pinny-<profile>`,
`pinny-<profile>-worker` and `pinny-ops`; the directories with their
owners and modes; the settings files (only if missing); the scripts in
`/usr/local/sbin`; the systemd units; journald limits (2 GB, 30 days); the
Caddyfile; and the timers. It is safe to run again, and running it again
is how you install a newer `deploy/` (after `sudo git -C
/usr/local/src/agentpinny pull`).

### 4.3 Fill in the settings

```sh
sudo nano /etc/pinny/staging.env       # set PINNY_ADMIN_EMAILS to your email
sudo nano /etc/pinny/production.env    # the same, when you set up production
sudo nano /etc/pinny/ops.env           # AWS_REGION, AWS_DEFAULT_REGION, PINNY_BACKUP_BUCKET, PINNY_ALERT_TOPIC_ARN
```

These files hold no passwords or keys. Then run the bootstrap again (same
command as in 4.2): now that `ops.env` is filled in, it turns on the
backup and restore-test timers.

Test that alerts reach you:

```sh
sudo bash -c 'set -a; . /etc/pinny/ops.env; pinny-notify "Pinny test alert" "If you read this, alerts work."'
```

### 4.4 Deploy

```sh
sudo pinny-deploy staging training-site
```

It takes a few minutes the first time (it builds a Python environment).
It ends with `Deployed git:<sha> to staging.` Then open
<https://staging.pinny.ecinc.us/healthz>: it shows `{"ok":true,"version":"git:..."}`
with a valid certificate. (If the certificate is not ready, see section 14,
"fails from outside".)

## 5. First admin sign-in

The emails in `PINNY_ADMIN_EMAILS` become admins when Pinny starts, but
they have no password yet. Make yourself a one-time set-password link:

```sh
sudo pinny-members staging setup-link you@ecinc.us
```

It prints a link like `https://staging.pinny.ecinc.us/setup.html#token=...`,
valid for 72 hours and usable once. Open it, choose a password (at least
12 characters; a password manager helps), and you are signed in. From
then on, sign in at the site's address with your email and password.

Do the same for production after its first deploy (`sudo pinny-members
production setup-link you@ecinc.us`). Staging and production have separate
members and passwords.

## 6. Deploy a new version

Always staging first, then production:

```sh
sudo pinny-deploy staging training-site                 # a branch
sudo pinny-deploy staging training-site --run-tests     # also run the test suite first (slower)
# check the staging site
sudo pinny-deploy production training-site              # or a tag (v1.2.0) or a commit sha
```

What it does: fetches the repository, exports the commit into a new
release directory, builds a fresh Python environment from
`requirements.lock.txt` (every package checked against its hash), writes
`PINNY_VERSION=git:<12 hex>`, checks that Pinny accepts the settings,
copies the databases aside (for a rollback), switches `current` to the new
release, restarts the web and worker units, and waits until `/healthz`
reports the new version and a test job has gone through a worker's
sandbox. If any of that fails after the switch, it switches back to the
previous release by itself and says so. It keeps the newest 3 releases, and prunes download-cache files unused for 30 days.

**Training runs and deploys.** A deploy does not restart the train worker
while a training, dataset or benchmark job is running (it would cut the run
off). It says so; restart that worker after the run
(`sudo systemctl restart pinny-worker-train@production`), or it picks up the
new release at the evening stop. `PINNY_RESTART_TRAINING=yes sudo pinny-deploy ...`
restarts it anyway.


Restarting the workers interrupts a running job. A scan or page job is
requeued and runs again a few seconds later; a training run is marked
`interrupted` and an admin starts it again (section 13).

## 7. Roll back

```sh
sudo pinny-rollback production              # back to the previous release
sudo pinny-rollback production --list       # what is on disk
sudo pinny-rollback production 20261001T101500Z-3f2a9c1d0b7e   # a specific one
```

This changes the code only. If the release you are leaving changed a
database format (its release notes or commit would say so), also put back
the databases from before that deploy. `pinny-deploy` saved them in
`/var/lib/pinny-backup/<profile>/pre-deploy/<time>/`:

```sh
sudo systemctl stop pinny@production.target
sudo ls /var/lib/pinny-backup/production/pre-deploy/        # pick the time of that deploy
S=/var/lib/pinny-backup/production/pre-deploy/<time>
sudo rm -f /srv/pinny/production/*.sqlite3-wal /srv/pinny/production/*.sqlite3-shm /srv/pinny/production/jobs/*.sqlite3-*
for f in site.sqlite3 pinny.sqlite3; do
  sudo install -m 0660 -o pinny-production -g pinny-production "$S/$f" /srv/pinny/production/$f
done
sudo install -m 0660 -o pinny-production -g pinny-production-jobs "$S/jobs/jobs.sqlite3" /srv/pinny/production/jobs/jobs.sqlite3
sudo systemctl start pinny@production.target
```

Anything saved after that deploy (new reviews, members) is lost, so only
do this when the old release will not start otherwise.

## 8. Backups

Page images (`documents/*/pages/`) are left out: Pinny renders them again from each PDF when a page is opened.

* **When:** every day at 18:15 New York time (production; staging at
  18:35), before the 19:00 shutdown. If the server was off at that time, the
  backup runs a few minutes after the next start.
* **What:** every `*.sqlite3` database, copied with SQLite's online backup
  API (safe while Pinny runs) and checked with `PRAGMA integrity_check`;
  plus `documents/`, `models/`, `crops/`, `exports/` and everything else in
  the data directory except `tmp/`. One `.tar.gz` per run, with a manifest
  of every file's sha256.
* **Where:** `s3://BUCKET_NAME/pinny/<profile>/pinny-<profile>-<time>.tar.gz`
  (+ `.sha256` and `.manifest.json`). Encrypted, versioned, kept 30 days.
* **How long:** a few minutes. As an estimate, one to two minutes per 3 GB
  of data (compressing plus uploading), so even at the 20 GB storage limit
  it ends well before 19:00. The first run tells you the real figure:
  `systemctl status pinny-backup@production` shows how long it took. A
  backup still running at 19:00 is cut off by the shutdown (the previous
  day's backup is still there, and the next day's run makes a new one).
* **Plus:** the weekly disk snapshot (step 2.8), four weeks kept.

Run one now, and look at the result:

```sh
sudo systemctl start pinny-backup@production
sudo journalctl -u pinny-backup@production -n 20 --no-pager
sudo bash -c 'set -a; . /etc/pinny/ops.env; aws s3 ls s3://$PINNY_BACKUP_BUCKET/pinny/production/'
```

A failed backup sends an alert at once.

## 9. Restore

**Into a scratch directory** (never touches the live data; use it to
check a backup or to fish out one file):

```sh
sudo bash -c 'set -a; . /etc/pinny/ops.env; pinny-restore latest --profile production'
```

It downloads the newest backup, checks the archive's sha256, every file's
sha256 and every database's integrity, and prints where it put the copy
(`/var/tmp/pinny-restore-.../data`). A specific backup: pass its
`s3://...tar.gz` address instead of `latest`, or a local file path. Delete
the scratch copy when you are done (`sudo rm -rf /var/tmp/pinny-restore-...`).

**Monthly restore test.** A timer does this for you on the first Monday of
every month (`pinny-restore-test@<profile>`): it restores the newest backup
into a scratch directory, verifies it, deletes it, and alerts you if
anything fails. To run it by hand: `sudo systemctl start
pinny-restore-test@production`, then `sudo journalctl -u
pinny-restore-test@production -n 20`. Once a quarter, also look at a
restored copy with your own eyes (open one of its documents' `source.pdf`).

**Over the live data** (after a disaster, when the data directory is
damaged):

```sh
sudo systemctl stop pinny@production.target
sudo bash -c 'set -a; . /etc/pinny/ops.env; pinny-restore latest --profile production --target /srv/pinny/production --force'
sudo systemctl start pinny@production.target
```

It refuses while any Pinny unit of that profile runs. The damaged
directory is kept next to it as `/srv/pinny/production.before-restore-<time>`
(delete it when all is well), and owners and modes are set as the
bootstrap sets them. Everything since that backup is lost.

**A whole new server** (the instance is gone): either create a volume from
the latest weekly snapshot (EC2 → Snapshots → Create volume) and launch an
instance from it, or do sections 2.5-4 on a fresh instance and restore
the newest backup over its empty data directory as above.

## 10. Add, remove or reset a user

In the site, as an admin, the **Members** panel does all of this: adding a
member shows their one-time set-password link. Send the link privately
(for example by text message or in person); anyone with it can set that
account's password within 72 hours.

On the server, the same with `pinny-members` (it runs `python -m
pinny.viewer.members` as the right user with the right settings):

```sh
sudo pinny-members production list
sudo pinny-members production add someone@ecinc.us --role reviewer      # or --role admin
sudo pinny-members production setup-link someone@ecinc.us               # their set-password link
sudo pinny-members production reset someone@ecinc.us                    # forgot password: new link
sudo pinny-members production remove someone@ecinc.us
```

* **Forgot password:** `reset` (or the reset button in Members) removes the
  old password, ends that person's sessions and prints a new one-time link.
* **Someone leaves:** `remove` ends their sessions at once.
* Keep admins to one or two people. Reviewers can upload, scan and review;
  only admins manage members, delete other people's uploads and (later)
  train and promote models.

## 11. Rotate secrets

There are few secrets, and none in configuration files:

| What | How to rotate |
|---|---|
| Everyone's sign-in sessions (for example after a lost laptop) | `sudo pinny-members production sign-out-all` |
| One person's password | they change it in the site (account menu), or `reset` it (section 10) |
| All passwords (suspected leak of `site.sqlite3`) | `reset` every member, then send the new links |
| AWS access | nothing to do: the instance role's credentials are temporary and rotate by themselves; no keys exist |
| HTTPS certificates | nothing to do: Caddy renews them (while the server runs, which is enough) |
| GitHub deploy key (private repository) | make a new key (step 4.1), add it on GitHub, delete the old one there and `/root/.ssh/pinny_deploy*` |

If you think the server itself was broken into: stop the instance, keep its
disk for investigation, build a new instance (sections 2.5-4), restore the
latest backup from before the break-in, and reset every member.

## 12. Staging and production

| | Production | Staging |
|---|---|---|
| Address | `https://pinny.ecinc.us` | `https://staging.pinny.ecinc.us` |
| Settings | `/etc/pinny/production.env` | `/etc/pinny/staging.env` |
| Data | `/srv/pinny/production` | `/srv/pinny/staging` |
| Port on 127.0.0.1 | 8001 | 8002 |
| Users | `pinny-production`, `pinny-production-worker` | `pinny-staging`, `pinny-staging-worker` |
| Units | `pinny@production.target` and its units | `pinny@staging.target` and its units |
| Members | its own | its own |

Both run on the same instance and share its 2 CPUs and 8 GB. Staging runs
in production mode (sign-in, sandbox), so it tests exactly what
production will run. Never copy production drawings into staging. Deploy
to staging first, try the change there, then deploy the same ref to
production.

To keep staging off when you are not testing (saves memory):

```sh
sudo systemctl stop pinny@staging.target && sudo systemctl disable pinny@staging.target
sudo systemctl enable --now pinny@staging.target        # to turn it back on
```

Everyday commands:

```sh
sudo systemctl status 'pinny-*@production.service'
sudo systemctl restart pinny@production.target          # restart web and workers
sudo systemctl list-timers 'pinny-*'                    # next backup, check, restore test
```

## 13. Working hours: the server is off at night

EventBridge Scheduler (step 2.9) starts the instance at 07:00 and stops it
at 19:00 every day, New York time. While it is off, the sites do
not answer (browsers show a connection error). The disk, the data and the
Elastic IP stay, so everything comes back as it was at the next start.
Caddy's certificates last for weeks, and it renews them well before they
expire, during the hours the server runs.

**Starting it by hand** (working late, an early start): console →
EC2 → Instances → `pinny` → Instance state → **Start**; or

```sh
aws ec2 start-instances --instance-ids INSTANCE_ID --region us-east-1
```

It is ready about two minutes later. The 19:00 schedule stops it again; after
19:00, stop it yourself when done (Instance state → **Stop**, or
`aws ec2 stop-instances --instance-ids INSTANCE_ID --region us-east-1`). A missed backup runs a few minutes after a start.

**What a stop does to Pinny.** The web process shuts down cleanly. Each
worker stops its running job and hands it back: an upload, page or scan
job is requeued and runs the next morning; a training, dataset or
benchmark run is marked `interrupted` and is **not** rerun by itself, so
a several-hour run never restarts unnoticed. The Training runs page shows
it, and an admin starts it again. So **start long training runs early in
the day**, so they finish before 19:00. (If the machine dies outright
instead, the job is failed as `worker_lost` at the next start.)

A backup running at 19:00 is cut off (section 8). To skip a day's
shutdown, disable the stop schedule in EventBridge → Scheduler (and
enable it again afterwards).

## 14. Alerts: what each one means and what to do

The health check runs every 5 minutes **on the server**, so it only runs
while the server is up: there are no alerts at night, and
no alert says "the server is off" (that is expected; there is no outside
uptime monitor). Alerts arrive by email from AWS Notifications. A problem
that continues is repeated every 6 hours; when it clears you get one
"Resolved" email.

| Alert | What to do |
|---|---|
| **Disk ... is N% full** | See what is big: `df -h`, `sudo du -sh /srv/pinny/* /opt/pinny/*/releases /var/lib/pinny-backup/*`, `journalctl --disk-usage`. Delete documents nobody needs in the site; old releases go by themselves (5 kept). To grow the disk: EC2 → Volumes → Modify → larger size, then `sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/nvme0n1p1`. |
| **Pinny is not answering /healthz on 127.0.0.1** | `sudo systemctl status pinny-web@production` and `sudo journalctl -u pinny-web@production -n 100`. "Cannot start: ..." names a missing or wrong setting in `/etc/pinny/production.env`. Try `sudo systemctl restart pinny@production.target`. Right after a deploy: `sudo pinny-rollback production`. |
| **.../healthz fails from outside, but Pinny answers locally** | Caddy or HTTPS: `sudo systemctl status caddy`, `sudo journalctl -u caddy -n 100`. Certificate errors mean DNS does not point at the Elastic IP or ports 80/443 are closed (security group). Check the Elastic IP is still associated. |
| **pinny-...@... is not running** | `sudo systemctl status <unit>` and `sudo journalctl -u <unit> -n 100`, then `sudo systemctl restart <unit>`. A worker that says `sandbox_unavailable` is not isolated from the network: its unit must have `PrivateNetwork=yes` (reinstall with the bootstrap). |
| **N server error response(s) (5xx)** | `sudo journalctl -u pinny-web@production --since -30min`: look for `request <id> failed` and the error under it. Users see the same request id in the error message, so they can tell you which one was theirs. One-offs after a restart are harmless (502 while Pinny starts). |
| **N job(s) failed on the server** | `sudo journalctl -u 'pinny-worker-*@production' --since -30min`. `timeout` or `cpu_limit`: a very large or unusual PDF; `job_crashed` or `job_failed`: a bug, keep the log for a fix; `interrupted`: stopped by a restart or the evening shutdown, start it again; `worker_lost`: the machine or worker died mid-job. Failures caused by a user's input (a bad PDF) are not alerted. |
| **Unit pinny-backup@... failed** / **no successful backup for N hours** | `sudo journalctl -u pinny-backup@production -n 50`. Usual causes: `ops.env` bucket or region wrong, the instance role lacks the policy (step 2.3), or not enough disk space (the archive is built on the local disk first). Fix, then `sudo systemctl start pinny-backup@production`. |
| **Unit pinny-restore-test@... failed** | The newest backup could not be restored or verified. `sudo journalctl -u pinny-restore-test@production -n 50`. Run a backup now and the restore test again; if it still fails, keep the older backups (they expire after 30 days) and get help. |
| **cannot read the job queue** | `ls -l /srv/pinny/production/jobs/`: the files must belong to group `pinny-production-jobs` with mode `rw-rw----`. The bootstrap sets this up. |

## 15. Logs

```sh
sudo journalctl -u pinny-web@production -f                  # follow the web log
sudo journalctl -u 'pinny-worker-*@production' --since today
sudo journalctl -u pinny-backup@production -n 50
sudo journalctl -u caddy --since -1h
sudo tail -f /var/log/caddy/pinny-production.log            # access log, one JSON line per request
```

The journal keeps at most 2 GB and 30 days (`deploy/journald.conf.d/`);
Caddy's access logs roll at 50 MB and are kept 30 days.

## 16. Training (torch)

Training needs the optional `train` extra (torch and torchvision), which
is not in `requirements.lock.txt`. When the training pages are deployed,
add `--train` once:

```sh
sudo pinny-deploy production training-site --train
```

It installs exactly the versions pinned in `pyproject.toml` (for example
`torch==2.14.0`, `torchvision==0.29.0`), as CPU-only wheels from
`https://download.pytorch.org/whl/cpu` (no GPU libraries, much smaller).
These come without hash pinning, from PyTorch's own index. Later deploys
of that profile include them automatically (`--no-train` to drop them).
The train worker (`pinny-worker-train@<profile>`) runs one job at a time
with up to 5 GB of memory in production (2 GB in staging) and a low CPU
priority, so uploads and scans stay quick while a model trains. It starts
only when the deployed release has a `train` pool.

## 17. Go-live checklist

Before you invite anyone (plan Step 9):

1. From a phone on mobile data (not the office Wi-Fi), open
   `https://pinny.ecinc.us`. You must land on the sign-in page. A wrong
   password and an email that is not a member are both refused.
   `http://pinny.ecinc.us` must redirect to `https://`.
2. From outside, only ports 443 and 80 answer. Check the security group
   has no other inbound rule, and from a computer elsewhere
   `curl -m 5 http://<Elastic IP>:8001/` must fail (time out).
3. A backup has run (`aws s3 ls` in section 8) and you have restored it once
   (`pinny-restore latest --profile production`, section 9).
4. The test alert from step 4.3 reached your inbox.
5. The start/stop schedules exist (EventBridge → Scheduler), and you have
   stopped and started the instance once by hand and the site came back.
6. The weekly snapshot policy is enabled (EC2 → Lifecycle Manager).
7. Invite the first reviewers (section 10). Keep admins to one or two people.

## 18. Decisions still open

* **Alert email address(es)** for the SNS subscription (step 2.2).
* **Bucket name** (step 2.1).
* Whether the repository stays public (step 4.1).

Settled: DNS at Bluehost (step 2.7), region `us-east-1`.
