# FUTURE.md — the live deployment, and how to change it

Notes for later-me. What is running, where, and the exact procedure for the
things that will eventually need doing: buying a domain, turning on HTTPS,
resetting the passwords, making the IP permanent.

Nothing here is urgent. Everything here is written down because it is the kind
of thing that is obvious today and completely forgotten in three months.

---

## What is running right now

**All five apps live on one machine**, not five. One Oracle Cloud Always Free
ARM VM, one `docker compose` project, six containers.

| | |
|---|---|
| Instance | `lichess-1`, region `uk-london-1`, AD-2 |
| Shape | `VM.Standard.A1.Flex`, **1 OCPU / 6 GB**, ARM (`aarch64`) |
| OS | Ubuntu 24.04.4 LTS |
| Public IP | `79.72.84.141` — **see "Make the IP permanent" below** |
| SSH | `ssh -i <your-key>.key ubuntu@79.72.84.141` |
| Repo on the VM | `~/Lichess-Essentials` |
| Docker | 29.8.0, Compose v5.5.1 |

### Live URLs

No domain is owned yet. `sslip.io` is a free public DNS service that resolves
`<anything>.79-72-84-141.sslip.io` to `79.72.84.141`, which is what gives each
app its own hostname without buying anything.

| App | URL | Login |
|---|---|---|
| Lichess Study to PDF | http://study.79-72-84-141.sslip.io | none — open to anyone |
| Chess Analyzer | http://analyzer.79-72-84-141.sslip.io | `aayush` / `xLpRoJIALb3Abp` |
| Player Prepper | http://prepper.79-72-84-141.sslip.io | `aayush` / `5okPTQeWV6AdRC` |
| Repertoire Creator | http://repertoire.79-72-84-141.sslip.io | `aayush` / `MXNgiboHu4kTcv` |
| Weakness Report | http://weakness.79-72-84-141.sslip.io | `aayush` / `VdMainT5WEuWkr` |

### Why hostnames and not `IP:8001`, `IP:8002`, …

Because every frontend in this repo asks for its own assets by **absolute**
path — `<script src="/static/app.js">` and `fetch("/api/...")`. Serve an app
under a path prefix like `/analyzer/` and every one of those requests resolves
to the wrong place; the page loads blank. Giving each app a hostname keeps it
at `/`, which is where its own code believes it lives. This is the same reason
a single combined app was rejected earlier.

### How the pieces fit

```
             internet
                │  :80
          ┌─────▼─────┐
          │   Caddy   │  routes on Host header
          └─────┬─────┘
   ┌──────┬─────┼─────┬────────┐
   ▼      ▼     ▼     ▼        ▼
 study analyzer prepper repertoire weakness     (all :7860, internal only)
```

The five app containers publish **no host ports at all** — only Caddy is
exposed. That is deliberate: it means there is no way to reach an app while
bypassing its password gate.

### Cross-app wiring works

Three of the apps import their siblings, and that survives containerisation.
Verified live on the running deployment:

- **Weakness Report** → `analyzer: true (via folder)`, `study: true`
- **Player Prepper** → `sibling: true`, `repertoireDir: /repertoires`
- Stockfish resolves to `/usr/games/stockfish` in all five

The Prepper one needed help. Its image contains only `Lichess-Study-to-PDF`
and `Player-Prepper`, so its default book path
(`/app/Repertoire-Creator/repertoires`) does not exist inside it — hosted, it
would report *"coverage was not measured"* for every line. `docker-compose.yml`
fixes that by mounting the shared `repertoires` volume into Prepper read-only
and setting `REPERTOIRE_DIR`. Repertoire Creator writes it, Prepper reads it.

### Data that survives restarts

Named Docker volumes, so `docker compose down` / `up` / rebuild all keep them:
imported games, engine reviews, saved scouts, repertoires, eval caches.

`docker compose down -v` **deletes** them. That is what the `-v` means.

Backup one:

```bash
docker run --rm -v lichess-essentials_repertoires:/data -v $PWD:/backup \
  alpine tar czf /backup/repertoires-$(date +%F).tar.gz -C /data .
```

---

## Reset the passwords

The gate is HTTP Basic Auth, implemented as middleware in each app's
`server.py`. It activates only when **both** `*_AUTH_USER` and `*_AUTH_PASS`
are non-empty. Blank either one and that app is wide open.

```bash
ssh -i <your-key>.key ubuntu@79.72.84.141
cd ~/Lichess-Essentials
nano .env                 # edit the *_AUTH_USER / *_AUTH_PASS pairs
docker compose up -d      # recreates the containers with the new values
```

**`docker compose restart` does not work here.** It restarts containers with
the environment they already have; it does not re-read `.env`. It must be
`up -d`, which notices the config changed and recreates them. No rebuild
happens, so it takes seconds.

Generate a strong one:

```bash
openssl rand -base64 12 | tr -d '/+=' | head -c 14
```

Afterwards, update the tables in this file and in the six READMEs, or they
become lies.

### What the gate does and does not do

Verified against the live deployment — every path returns 401 without
credentials, including `/`, `/static/app.js`, `/api/health`, `/api/settings`,
`/favicon.ico`, `/docs` and `/openapi.json`. Wrong username and wrong password
are both rejected. There is no bypass, no guest mode, no unauthenticated
health endpoint.

Two real caveats:

1. **Lichess Study to PDF has no gate at all.** Not misconfigured — it is
   simply not implemented in that app. Anyone with the link can use it.
2. **The passwords are published in this repo,** which is public. That is a
   deliberate choice so people can try the apps, but it means the gate is a
   speed bump, not a wall. Anyone reading GitHub can log in. If these ever
   hold anything private, change the passwords and remove them from the
   READMEs in the same commit.

And regardless of the gate: **the site is plain HTTP.** Passwords and any
Lichess token pasted into a page cross the network in clear text. Fix that by
getting a domain — next section.

---

## Buy a domain and turn on HTTPS

Roughly 20 minutes, most of it waiting for DNS. **No rebuild, no code change.**

### 1. Buy a domain

Any registrar. Cloudflare and Porkbun are cheap and do not upsell.

### 2. Point five A records at the machine

At the registrar's DNS panel, five records, all to `79.72.84.141`:

```
study        A    79.72.84.141
analyzer     A    79.72.84.141
prepper      A    79.72.84.141
repertoire   A    79.72.84.141
weakness     A    79.72.84.141
```

Wait until they resolve before step 3 — Caddy asks Let's Encrypt for
certificates on startup and that only works once the names are public:

```bash
dig +short study.yourdomain.com     # must print 79.72.84.141
```

### 3. Switch the deployment over

```bash
ssh -i <your-key>.key ubuntu@79.72.84.141
cd ~/Lichess-Essentials

nano .env
#   DOMAIN=yourdomain.com
#   ACME_EMAIL=you@yourdomain.com

rm docker-compose.override.yml     # this is the whole switch

docker compose up -d
docker compose logs -f caddy       # watch the certificates get issued
```

That is the entire change. `docker-compose.override.yml` exists only to swap
the repo's HTTPS `Caddyfile` for the HTTP-only one in `~/caddy-http`. Delete
it and compose falls back to the tracked `Caddyfile`, which has no `http://`
prefixes — so Caddy does automatic HTTPS, including redirecting port 80.

Port 443 is already open in both firewalls. Nothing else to do.

### 4. Afterwards

- Update the URL tables here and in the six READMEs.
- The old `*.sslip.io` URLs keep working. Delete those blocks from the
  `Caddyfile` if you want them to stop.

---

## Make the IP permanent

**This is the thing most likely to break the links in the READMEs.**

Oracle's create-instance wizard assigns an **ephemeral** public IP by default.
An ephemeral IP is released when the instance is stopped or terminated, and a
different one is assigned when it comes back. Stop the VM once and every URL
in this repo is wrong.

I was not able to confirm whether `79.72.84.141` is ephemeral or reserved —
the OCI console repeatedly failed to render the VNIC detail page. Assume
ephemeral until checked:

> Instance → **Networking** → **Attached VNICs** → click the VNIC →
> **IPv4 Addresses** → look at the primary row's **Public IP Type**

To make it permanent, assign a **reserved** public IP. Note that Oracle does
not convert an ephemeral address into a reserved one in place — you get a
**different address**, so do this *before* sharing links widely, and update
every URL afterwards.

Reserved public IPs are included in Always Free.

---

## Give it the other half of the free allowance

The VM was created at **1 OCPU / 6 GB**. Always Free allows **2 OCPU / 12 GB**
at no cost, so half the allowance is unused. Stockfish is CPU-bound and these
apps are nothing but Stockfish, so this is the single biggest speed win
available:

> Instance → **Stop** → wait for Stopped → **Actions → Edit** → **Edit shape**
> → 2 OCPUs, 12 GB → **Save** → **Start**

**Stopping releases an ephemeral public IP.** Do the reserved-IP step first,
or expect the address to change and every URL to need updating.

Oracle's own console still advertises the old 4 OCPU / 24 GB allowance, while
the docs now say the free tier was halved to 2/12 on 15 June 2026 with no
grandfathering. Build at 2/12; anything above may be stopped until resized.

---

## Routine operations

```bash
cd ~/Lichess-Essentials

git pull && docker compose up -d --build   # deploy the latest commit
docker compose ps                          # what is running
docker compose logs -f weakness-report     # one app's logs
docker compose restart chess-analyzer      # bounce one app
docker compose down                        # stop all (data survives)
```

The VM runs the **last committed** code. Uncommitted work on the laptop is not
deployed — push first, then pull on the VM.

A first build takes 15–25 minutes on this hardware. Later builds reuse cached
layers.

### The heartbeat, and why the obvious version does not work

Oracle's documented rule: an Always Free instance is idle if, over **7 days**,
**95th-percentile CPU is under 20%**. Idle instances get stopped and can be
reclaimed.

Read that carefully, because it defeats the naive fix. "95th percentile under
20%" means you must be **above 20% CPU for more than 5% of the time**. A short
periodic ping does not do it. The first version here was:

```
*/20 * * * * timeout 25 sha256sum /dev/zero >/dev/null 2>&1     # WRONG
```

25 seconds in every 1200 is a **2% duty cycle**. The 95th percentile of that
sits at approximately zero, so the instance still reads as idle — the cron job
runs faithfully, achieves nothing, and you find out when the box is reclaimed.
What is actually installed:

```
*/20 * * * * nice -n 19 timeout 180 sha256sum /dev/zero >/dev/null 2>&1
```

180 seconds in 1200 is a **15% duty cycle** — three times the 5% needed, with
margin for however Oracle samples. `nice -n 19` means it has the lowest
possible scheduling priority, so it yields instantly to a real analysis: it
counts as CPU utilisation for Oracle's metric without competing with the apps.

Do not remove it, and do not "optimise" it back down to a quick ping.

Two caveats worth keeping in mind:

- This is reasoning from Oracle's published criteria, not something that has
  been observed working for 7 days on this machine. If the instance is ever
  stopped unexpectedly, raise the duty cycle before assuming anything else.
- The idle rule applies to **Always Free**. This account is still on the Free
  Trial, so it does not bite yet.

### Files on the VM that are not in git

| Path | What |
|---|---|
| `~/Lichess-Essentials/.env` | passwords, `DOMAIN`. `chmod 600`, gitignored |
| `~/Lichess-Essentials/docker-compose.override.yml` | the HTTP-only switch; delete it to go HTTPS |
| `~/caddy-http/Caddyfile` | HTTP-only Caddy config, kept outside the repo so `git pull` never conflicts |

---

## Storage: what grows, how fast, and what it touches

### Does using the live apps take real space? Yes — on the VM only

Measured, not estimated:

```
/dev/sda1   45G total   9.2G used   35G free
```

Most of that 9.2 GB is the OS, Docker images (1.45 GB), build cache (1.38 GB)
and the 4 GB swapfile. **All sixteen data volumes together came to 4.1 MB.**

| Volume | Grows with |
|---|---|
| `weakness-history` | one engine review per game — the fastest grower |
| `analyzer-games` | games imported into Chess Analyzer |
| `prepper-prep` / `prepper-data` | cached opponent games, saved scouts |
| `repertoires` | repertoires you author |
| `*-cache` | Stockfish eval caches |

The three volumes that showed 1.2 MB are not app data — that is **pip's HTTP
cache**, left inside `/root/.cache` by the editable installs during the image
build. A one-off, not growth.

### Nothing on the VM touches GitHub

Data flows **one way**: GitHub → VM, when you run `git pull`. Analysing a game
on the live site changes nothing in the repo, ever.

The only feature in this repo that could push anywhere is Repertoire Creator's
`gitsync`, and it needs `REPERTOIRE_GIT_REMOTE_URL` — confirmed **unset** on
this VM. Even switched on, it pushes to a *separate data repo you nominate*,
never this one.

One thing does leave the box: Repertoire Creator can publish a repertoire to
**Lichess** as a study. That is Lichess, not GitHub, and only when you click it.

### RAM versus disk — only one of them accumulates

**RAM is transient.** Stockfish's hash is allocated for an analysis and
released. Restart a container and it is all gone. It does not build up.

**Disk is persistent and does grow** — but slowly. The eval cache measured
**1,121 bytes for 8 positions, about 140 bytes each**. A million distinct
positions is roughly 140 MB against 35 GB free. You will not fill this disk by
analysing chess.

**The one real caveat:** `evals.json` has **no size cap and no eviction**
(`lichess_study_pdf/evals.py:198-204`). It is a plain dict serialised whole, so
the entire cache loads into RAM at startup and every save rewrites the file.
At tens of thousands of entries that is invisible; at millions it would get
slow and RAM-hungry. That is a performance ceiling, not a disk one — and
`prune.sh` below caps it anyway.

Two limits that do exist in code: `MAX_SESSIONS = 8` live analysis sessions,
and `MAX_LINES = 5` engine lines.

### The real risk is other people, not chess

The passwords are published, so strangers can fill the disk and burn the CPU.
On 1 OCPU, several simultaneous analyses will crawl for everyone. Nothing rate
limits them. If it is ever abused, change the passwords.

---

## Everything is shared. There are no user accounts.

If two people use the same app, **they are using the same app** — not two
copies of it. There are no accounts, no cookies, no sessions, no per-user
directories. Checked for all of it: the only `session_id` in the codebase is
Chess Analyzer's live-game follower, which is about following a game in
progress, not about who you are.

So:

- Save a scout, a repertoire, a report — **everyone who can log in sees it.**
- Anyone can overwrite or delete anyone else's, because it is one shared
  folder per app on one shared volume.
- Whatever you put in is visible to whoever has the password. The passwords are
  published in this repo.

That is fine for a demo, and it is the reason not to keep anything you care
about on this deployment.

### The part that actually matters: the Lichess token is shared too

Four of the five apps store a pasted Lichess token in a **module-level
global** — `global _SESSION_TOKEN`, set by `POST /api/token`, confirmed in
Chess Analyzer, Player Prepper, Repertoire Creator and Weakness Report.

It is not scoped to your browser. It is scoped to the **server process**. So:

> Paste your Lichess token into any of those four, and every other visitor of
> that app is acting with your token until the container restarts or someone
> pastes over it.

The consequence is not theoretical. Repertoire Creator *publishes studies to
Lichess*. With your token in that global, a stranger clicking publish creates a
study **in your Lichess account**.

The READMEs say to paste a token "per session instead" of setting
`LICHESS_TOKEN`. That advice is right about not setting the env var, but
"session" there means the server process, not your browser tab. On a private,
single-user deployment the distinction does not exist. On this one it does.

**So while the site is public with published passwords: do not paste a real
Lichess token into it.** If you already have, clear it by restarting that app —
the global lives only in memory:

```bash
docker compose restart chess-analyzer   # or whichever
```

Lichess Study to PDF is the exception; it resolves tokens from the environment
or a file rather than an in-memory global. It also has no password gate at all,
so it is the most exposed of the five in every other respect.

If this ever needs to be a real multi-user deployment, that global is the first
thing to fix — the token would have to move into a per-browser session, which
means adding a session mechanism none of these apps currently has.

---

## What happens if it runs out of memory

Short answer: **the instance does not halt.** Oracle does not stop a VM for
memory pressure. The Linux kernel kills a process and life goes on.

What actually happens depends on which limit is hit first, and the original
setup had this wrong:

- **A container exceeds its own `mem_limit`** → the kernel kills just that
  container. `restart: unless-stopped` brings it straight back. The other four
  apps never notice. This is the good case.
- **The host runs out** → the kernel OOM killer picks a victim by its own
  scoring, which could be `dockerd` or `sshd`. That can lock you out and take
  everything down at once. This is the bad case.

The first version of `docker-compose.yml` here made the bad case reachable:
five containers at `mem_limit: 2g` on a 5.8 GiB machine is **10 GiB of limits
on 5.8 GiB of RAM**, so the host could exhaust before any single container hit
its cap. There was also **no swap at all**. Both are fixed:

- **4 GB swapfile**, persisted in `/etc/fstab`, with `vm.swappiness=10` so it
  is an emergency cushion rather than somewhere the apps live day to day.
- **`mem_limit` lowered to 1 GiB each** in `docker-compose.override.yml` —
  5 GiB total, fits inside 5.8 GiB, and still double the 478 MB measured peak
  for a single analysis at the default `Hash=256`.

The tracked `docker-compose.yml` now says `1g` too, so deleting
`docker-compose.override.yml` for the HTTPS switch is safe — the limits do not
revert. The override's copy is belt-and-braces. If you ever raise it, keep the
arithmetic in mind: five containers times the limit must stay under host RAM.

To check swap survived a reboot: `free -m` should show ~4095 MB of swap.

---

## Housekeeping: `~/prune.sh`

A cleanup script lives at `~/prune.sh` on the VM, with a weekly cron entry.

```bash
~/prune.sh            # show what WOULD be deleted, delete nothing
~/prune.sh --apply    # actually delete
crontab -l            # 30 4 * * 0  -> Sundays 04:30
```

It logs every deletion to `~/prune.log`.

**Be clear that this is precautionary, not needed.** At 4 MB of volumes against
35 GB free, nothing is close to a problem. It exists so that a year of use
cannot quietly creep up.

### Why it does not simply "keep 5 files"

Because the stores are not the same kind of data, and one flat rule would be
destructive. A scout is one file per opponent and costs a minute of network to
rebuild. A review is one file per *game* and costs real engine time — "keep 5"
against a 400-game import would delete 395 reviews and leave the app useless.
So there are two rules:

| Rule | Applied to | Setting |
|---|---|---|
| **Count cap** — keep the newest N | `prepper/scouts`, `prepper/games` | 20 |
| | `weakness/reports`, `weakness/games` | 20 |
| | `analyzer/games` | 20 |
| **Size valve** — trim oldest only past a threshold | `weakness/reviews` | 2000 MB |
| | every `*-cache` | 200 MB |

20 is safe everywhere it is applied, because each of those stores holds **one
file per subject** — a scout per opponent, a report per dataset. Twenty means
twenty subjects, and anything dropped is minutes of network away from coming
back.

`weakness/reviews` is the deliberate exception: it is one file per **game**,
not per subject. Weakness Report exists to read hundreds at once — its own
README makes the point with *"four hundred of them"*. A count cap of 20 there
would not crash anything; it would quietly reduce the app to answering from 20
games while presenting it as your history, which is worse than crashing. Hence
the size valve.

**Pruning cannot crash any app.** `store.py:_read_json` returns `None` for a
missing file and the API turns that into a clean 404 — a user sees "not found"
and rebuilds. Verified by reading the code, not assumed.

**`repertoires` is never touched, at any size.** It is hand-authored and
nothing regenerates it — the only genuinely irreplaceable data on the machine.
`settings.json` files are left alone too.

Change any threshold by editing the `cap_count` / `cap_size` lines at the
bottom of the script. Always run without `--apply` first and read the list.

> Note for anyone editing it: `/var/lib/docker` is mode 700 root-only, so a
> plain `[ -d "$dir" ]` from the `ubuntu` user is **always false**. The script
> uses `sudo test -d`. The first version did not, and silently did nothing at
> all — which is exactly the sort of bug a cleanup script gets to have for a
> year before anyone notices.

---

## Loose ends

- **The SSH key lives in the repo folder.** It is gitignored (`*.key`,
  `*.key.pub`) and was never committed — checked against `origin/main` and the
  full history. It still belongs in `~/.ssh/`, not next to source code.
- **Free Trial, not Always Free yet.** The account banner says trial. When it
  ends, anything not Always Free-eligible stops. The current shape is
  A1.Flex within free limits, so it should survive — worth confirming when the
  trial expires.
- **`sslip.io` is someone else's free service.** If it goes down, the URLs
  stop resolving. Owning a domain removes that dependency.
- **Render is still an option** — `render.yaml` in this repo deploys all five
  there. It was rejected because free Render is 512 MB and ~0.1 CPU, and
  ChessAnalyzer's default `Hash=256` alone peaked at 478 MB of that 512 MB.
  Here there is 6 GB.
