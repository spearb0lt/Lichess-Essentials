# Hosting all five apps free on Oracle Cloud

A runbook for putting this whole repo on one Oracle Cloud **Always Free** ARM
machine, behind HTTPS, with data that survives restarts.

**No application code changes.** The `Dockerfile`s here are the same ones
Render builds. The ARM question is already settled by test: Debian ships
Stockfish for `aarch64`, so `apt-get install stockfish` works unmodified —
verified as Stockfish 17.1 running on `aarch64`.

What is new is three files, all infrastructure, none of it touching an app:
`docker-compose.yml`, `Caddyfile`, `.env.example`.

## Why bother, versus Render free

| | Render free | Oracle Always Free |
|---|---|---|
| RAM | 512 MB | **12 GB** |
| CPU | ~0.1 | **2 ARM cores** |
| Disk | wiped every restart | **persistent volumes** |
| Idle | spins down, ~1 min cold start | always on |

That RAM row is the one that matters. ChessAnalyzer's own default engine
settings (`Hash=256, Threads=2`) were measured peaking at **478 MB of Render
free's 512 MB — 93%** during a *single* search on an otherwise idle server,
with no environment variable to turn it down. On 12 GB that problem simply
does not exist.

---

## Before you start

- A **credit card**, for identity verification. Always Free does not charge
  it, but you cannot sign up without one.
- A **domain name** — but **not until step 4**. You can do steps 1–3 (create
  the machine, open the ports, install Docker) without owning one. It is
  needed only for the HTTPS certificates. Without one, see the commented
  fallback at the bottom of `Caddyfile` — but read the warning there first,
  because these apps take a Lichess token by paste.
- **~1 hour**, most of it waiting on builds.

> **"Domain" means two different things here.** Oracle asks for a **Cloud
> Account Name** at signup and an **identity domain** at login (the answer is
> normally `Default`) — that is Oracle's own login container and has nothing
> to do with DNS. The domain in step 4 is a website domain like
> `yourname.com`. Do not go buy anything because Oracle's login page said
> "domain".

### The one thing that may stop you

Ampere A1 (the free ARM shape) is in heavy demand and your home region may
report **"Out of Capacity"** — sometimes for days. This is the single most
common reason people abandon Oracle. Your region is fixed at signup, so if
capacity never appears there, your options are retrying on a schedule or
starting over with a different account region.

Also note Oracle **halved** the Always Free ARM allocation on 15 June 2026,
from 4 OCPU / 24 GB to **2 OCPU / 12 GB**, with no grandfathering. Build the
VM at 2/12 and it stays free; ask for more and it can be stopped until
resized.

---

## 1. Create the machine

1. Sign up at cloud.oracle.com, choosing your region carefully — it is
   permanent.
2. **Compute → Instances → Create instance**.
3. **Image:** Ubuntu 24.04. **Shape:** change it — the default is AMD.
   Pick **Ampere → VM.Standard.A1.Flex**, then set **2 OCPU / 12 GB**.
4. Confirm the shape card sVays **Always Free eligible**. VIf it doesn't, you
   are about to create a billable machine.
5. Save the SSH private key it offers. There is nov second chance.
6. Create. Note the **public IP**.

## 2. Open the ports — both firewalls

This is the classic trap: Oracle has a cloud firewall *and* the Ubuntu image
ships its own `iptables` rules. Doing only the first is why "the port is open
but nothing connects."

**Cloud side:** Networking → Virtual Cloud Networks → your VCN → Security
Lists →v default → **Add Ingress Rules**, twice:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

**Machine side**, over SSH.

> **Which machine?** The VM — not your laptop, and **not Cloud Shell**.
> Oracle's Cloud Shell and Code Editor are separate environments Oracle runs
> for the console; `sudo iptables` typed there configures Cloud Shell and
> changes nothing about your server, while appearing to succeed. Everything
> from here to the end of the runbook is typed at a prompt that reads
> `ubuntu@<your-vm>:~$`.
>
> From Windows PowerShell, the key Oracle gave you is too readable for `ssh`
> to accept, so fix its permissions once:
>
> ```powershell
> icacls "C:\path\to\YOUR-KEY.key" /inheritance:r
> icacls "C:\path\to\YOUR-KEY.key" /grant:r "$($env:USERNAME):(R)"
> ```
>
> The login user is `ubuntu` on an Ubuntu image, `opc` on Oracle Linux. If you
> prefer Cloud Shell, upload the private key there and SSH into the VM from
> it — Cloud Shell is a place to run `ssh` from, not the destination.

```bash
ssh -i /path/to/your-key.key ubuntu@YOUR_PUBLIC_IP

sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

(On an Oracle Linux image instead of Ubuntu, it is
`sudo firewall-cmd --permanent --add-port=80/tcp --add-port=443/tcp && sudo firewall-cmd --reload`.)

## 3. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# so you can run docker without sudo -- log out and back in after this
sudo usermod -aG docker $USER
```

Log out, log back in, then check: `docker run --rm hello-world`.

## 4. Point DNS at the machine

At your registrar, five **A records**, all to the VM's public IP:

```
study.yourdomain.com        A    YOUR_PUBLIC_IP
analyzer.yourdomain.com     A    YOUR_PUBLIC_IP
prepper.yourdomain.com      A    YOUR_PUBLIC_IP
repertoire.yourdomain.com   A    YOUR_PUBLIC_IP
weakness.yourdomain.com     A    YOUR_PUBLIC_IP
```

Do this **before** step 6. Caddy asks Let's Encrypt for certificates on
startup, and that only works once the names resolve. Give DNS a few minutes.

## 5. Clone and configure

```bash
git clone https://github.com/spearb0lt/Lichess-Essentials.git
cd Lichess-Essentials
cp .env.example .env
nano .env
```

Set `DOMAIN` and `ACME_EMAIL`. Then set a user and password for each of the
four apps that have a gate.

**Leaving a pair blank leaves that app open to anyone with the URL.** These
will be public hostnames on a public IP; they get found. Lichess-Study-to-PDF
has no gate implemented, so it will be open regardless — that is a property
of the app, not of this setup.

Do **not** add `LICHESS_TOKEN`. On a shared deployment it becomes everyone's
token. Paste one into the UI per session instead.

## 6. Build and start

```bash
docker compose up -d --build
```

**This takes a while** — five images on 2 ARM cores, roughly 15–25 minutes on
the first run. Later runs reuse cached layers and are much faster.

Then:

```bash
docker compose ps          # all six should be "running"
docker compose logs -f caddy   # watch the certificates get issued
```

Visit `https://study.yourdomain.com`. If the certificate fails, it is almost
always DNS not resolving yet, or port 80 blocked at one of the two firewalls.

## 7. Keep it from being reclaimed

Oracle can reclaim an Always Free instance after roughly **7 days of
near-zero CPU**. Five idle containers may not clear that bar, so give it a
heartbeat:

```bash
( crontab -l 2>/dev/null; \
  echo "*/20 * * * * timeout 25 sha256sum /dev/zero >/dev/null 2>&1" ) | crontab -
```

25 seconds of one core every 20 minutes — invisible on the bill, plainly
non-idle to Oracle.

---

## Running it

```bash
# update to the latest commit
cd ~/Lichess-Essentials && git pull && docker compose up -d --build

# logs for one app
docker compose logs -f weakness-report

# restart one app
docker compose restart chess-analyzer

# stop everything (data survives -- it is in volumes, not containers)
docker compose down
```

### Your data

Everything worth keeping is in named Docker volumes and survives
`down`/`up`/rebuild: imported games, engine reviews, saved scouts,
repertoires, and the eval caches. `docker compose down -v` deletes them —
that is what the `-v` means, so do not reach for it casually.

To back up:

```bash
docker run --rm -v lichess-essentials_repertoires:/data -v $PWD:/backup \
  alpine tar czf /backup/repertoires-$(date +%F).tar.gz -C /data .
```

### Two things this setup fixes for free

- **Player-Prepper's opening book now works.** Its image does not contain
  Repertoire-Creator, so hosted it normally reports "coverage was not
  measured" for every line. The compose file mounts the shared `repertoires`
  volume read-only into Prepper and sets `REPERTOIRE_DIR`, so it reads your
  actual repertoires.
- **Repertoire-Creator's gitsync is redundant.** It exists to survive hosts
  with no disk. You have a disk. Leave `REPERTOIRE_GIT_REMOTE_URL` unset and
  ignore the "ephemeral storage" line it prints at boot — that warning is
  about container disk, and your repertoires are on a volume.

### If something goes wrong

| Symptom | Cause |
|---|---|
| "Out of Capacity" creating the VM | ARM demand. Retry, or retry on a schedule. Nothing you can fix. |
| Site unreachable, containers running | The second firewall. Re-check step 2 — the `iptables` half is the one people skip. |
| Caddy loops on certificates | DNS not resolving yet, or port 80 not open. `dig study.yourdomain.com` should return your IP. |
| A container restarts repeatedly | `docker compose logs <name>`. If it is OOM, raise that service's `mem_limit`. |
| Everything slow | 2 cores shared by five apps. Analysing many games at once will contend; run one heavy job at a time. |
