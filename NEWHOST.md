# NEWHOST.md — rebuilding the whole thing from nothing

Everything needed to recreate the live deployment on a fresh Oracle Cloud
machine, in the order it has to happen, so that **the published URLs keep
working**. Written to be followed without thinking, because it will be read on
a bad day.

Companion documents: [FUTURE.md](FUTURE.md) describes the deployment that
exists and how to change it. This one is for building it again.

---

## The one idea that makes this safe

**The URLs are tied to a DuckDNS name, not to a machine or an IP address.**

```
study.lichess-essentials.duckdns.org  ──DuckDNS──▶  whatever IP you tell it
```

So a rebuild does not break the links. A rebuild gets a new IP, you paste that
IP into DuckDNS, and the same five URLs point at the new box. Caddy then asks
Let's Encrypt for fresh certificates against the same names and gets them,
because certificates are issued to **names**, not addresses.

This is why the READMEs and PyPI descriptions can safely contain those URLs.
It is also why the raw IP appears in exactly one place worth caring about — the
landing page — and nowhere that matters.

### What changes the IP and what does not

| Action | IP changes? |
|---|---|
| Reboot the instance | No |
| **Stop and start** the instance (e.g. to resize it) | **No** |
| Terminate and recreate — a rebuild | **Yes** |
| Assign a reserved public IP | Yes, once, to a permanent one |

Oracle's documentation is explicit: *"When you stop an instance, its ephemeral
public IPs remain assigned to the instance."* An ephemeral IP is deleted only
when the private IP is deleted, the VNIC is detached, or **the instance is
terminated**. This is not AWS — a stop/start does not shuffle the address.

**Consequence: a reserved public IP is a convenience, not a prerequisite.** It
saves one DuckDNS edit after a rebuild. Nothing more. Do not treat it as a
blocker for anything below.

---

## Part 0 — Before you destroy anything

Skip this and you lose the repertoires. Everything else regenerates; those do
not.

```bash
ssh -i <your-key>.key ubuntu@<CURRENT_IP>
mkdir -p ~/backup && cd ~/backup

for v in repertoires weakness-history analyzer-games prepper-prep prepper-data \
         analyzer-data study-out; do
  docker run --rm \
    -v lichess-essentials_$v:/data \
    -v ~/backup:/backup \
    alpine tar czf /backup/$v-$(date +%F).tar.gz -C /data . 2>/dev/null \
    && echo "backed up $v"
done
ls -la ~/backup
```

Then copy them off the machine, because the machine is what you are about to
delete:

```bash
# from your laptop
scp -i <your-key>.key "ubuntu@<CURRENT_IP>:~/backup/*.tar.gz" .
```

`caddy-data` holds the certificates. **Do not bother backing it up** — a new
machine will get fresh certificates in seconds, and restoring old ones onto a
new IP achieves nothing.

---

## Part 1 — Create the machine

Oracle Cloud console → **Compute → Instances → Create instance**.

| Setting | Value | Why |
|---|---|---|
| Name | `lichess-1` | anything |
| Image | **Canonical Ubuntu 24.04** | what the runbook assumes |
| Shape | **Ampere → VM.Standard.A1.Flex** | the default is AMD; that is the wrong one |
| OCPU / Memory | **2 OCPU / 12 GB** | the full Always Free allowance — see Part 8 |
| Networking | leave defaults, **"Assign a public IPv4 address" = Yes** | without this there is no way in |
| SSH key | **download the private key** | there is no second chance |

Confirm the shape card says **"Always Free-eligible"** before creating. If it
does not, you are about to make a billable machine.

If you get **"Out of Capacity"**: that is the ARM shortage, not a mistake. Try
another Availability Domain, or retry later. Nothing you can do about it.

Note the **public IP** when it comes up.

> Region is fixed at signup. The current deployment is `uk-london-1`, AD-2.

---

## Part 2 — Open the ports, in both firewalls

Oracle has a cloud firewall **and** the Ubuntu image ships its own iptables
rules. Doing only the first is the classic "the port is open but nothing
connects".

### Cloud side

Networking → Virtual Cloud Networks → your VCN → Security Lists → default →
**Add Ingress Rules**:

| Source CIDR | Protocol | Destination port | For |
|---|---|---|---|
| `0.0.0.0/0` | TCP | `80` | HTTP, and the Let's Encrypt challenge |
| `0.0.0.0/0` | TCP | `443` | HTTPS |

Port 22 is already there from the default rules.

**Get source and destination the right way round.** A rule with *source* port
80 and destination "All" does not open your web server — it lets anything reach
*any* port on the machine, including SSH, because the sender picks its own
source port. Two rules exactly like that existed on the old deployment and were
deleted. Destination is the field that matters.

### Machine side

```bash
ssh -i <your-key>.key ubuntu@<NEW_IP>

sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
sudo iptables -L INPUT -n | grep -E "dpt:(80|443)"   # expect two lines
```

If `netfilter-persistent` is missing:

```bash
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | sudo debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | sudo debconf-set-selections
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
sudo netfilter-persistent save
```

On Windows, `ssh` will refuse the downloaded key as too readable. Fix once:

```powershell
icacls "C:\path\to\your-key.key" /inheritance:r
icacls "C:\path\to\your-key.key" /grant:r "$($env:USERNAME):(R)"
```

**Keep the key out of the repo directory.** `~/.ssh/` is where it belongs.
`.gitignore` covers `*.key` and `*.pem` as a seatbelt, not as permission.

---

## Part 3 — Point DuckDNS at the new machine

**Do this before Part 6.** Caddy asks Let's Encrypt for certificates the moment
it starts, over an HTTP-01 challenge to port 80 of whatever the name resolves
to. If the name still points at the dead machine, every certificate fails.

1. Log in at [duckdns.org](https://www.duckdns.org).
2. Put the **new IP** in the `lichess-essentials` row. Update.
3. Wait for it, and confirm from your laptop:

```bash
dig +short study.lichess-essentials.duckdns.org      # must print the NEW IP
```

DuckDNS resolves **every** subdomain of your name to that one address, which is
what gives each app its own hostname for free. Nothing needs configuring
per-subdomain.

> If the DuckDNS name has expired from inactivity, recreate it with the same
> label and the URLs are unchanged. If the label is taken, every published URL
> and both PyPI descriptions have to be reissued — which is the one genuinely
> painful failure mode here, so log in occasionally.

---

## Part 4 — Install Docker

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
sudo usermod -aG docker $USER
```

**Log out and back in**, then check the group took effect:

```bash
docker run --rm hello-world
```

Reference: the working deployment has Docker 29.8.0 and Compose v5.5.1 on
Ubuntu 24.04.4, `aarch64`.

---

## Part 5 — Swap, before anything heavy runs

The image has no swap. Without it a memory spike has nowhere to go and the
kernel OOM killer picks a victim, which may be `dockerd` or `sshd` — far worse
than one app dying.

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -w vm.swappiness=10
free -m        # expect ~4095 MB of swap
```

`swappiness=10` keeps it an emergency cushion rather than somewhere the apps
live.

---

## Part 6 — Clone, configure, start

Every file needed is in the repo. Nothing has to be pasted out of this
document.

```bash
cd ~
git clone https://github.com/spearb0lt/Lichess-Essentials.git
cd Lichess-Essentials

# the three things that live outside git on the running box
mkdir -p ~/caddy-http
cp deploy/Caddyfile                  ~/caddy-http/Caddyfile
cp -r deploy/site                    ~/caddy-http/site
cp deploy/docker-compose.override.yml .
cp deploy/prune.sh                   ~/prune.sh
chmod +x ~/prune.sh

# credentials and domain
cp .env.example .env
chmod 600 .env
nano .env          # confirm DOMAIN, set the *_AUTH_* pairs
```

`.env.example` already carries the live demo values, so for a like-for-like
rebuild there is nothing to change.

Then build. **This takes 15–25 minutes** on 2 ARM cores, longer on 1 — five
images, each compiling nothing but installing a lot:

```bash
docker compose up -d --build
```

Watch the certificates arrive:

```bash
docker compose logs -f caddy | grep -i certificate
# expect five: "certificate obtained successfully"
```

---

## Part 7 — Restore the data, then verify

```bash
# from your laptop
scp -i <your-key>.key *.tar.gz ubuntu@<NEW_IP>:~/backup/

# on the VM
cd ~/Lichess-Essentials
docker compose down                 # containers only; volumes survive
for f in ~/backup/*.tar.gz; do
  v=$(basename "$f" | sed 's/-[0-9-]*\.tar\.gz$//')
  docker run --rm -v lichess-essentials_$v:/data -v ~/backup:/backup \
    alpine sh -c "cd /data && tar xzf /backup/$(basename $f)" \
    && echo "restored $v"
done
docker compose up -d
```

Verify from a machine that is **not** the VM — ideally a phone, since that is
what the HTTPS work was for:

```bash
for h in study analyzer prepper repertoire weakness; do
  curl -s -o /dev/null -w "$h %{http_code} cert=%{ssl_verify_result}\n" \
    "https://$h.lichess-essentials.duckdns.org"
done
```

Expected: `study 200`, the other four `401` (the gate working), and
`cert=0` on all five (certificate verified). Then with credentials:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u 'test:testpassword1234@' \
  https://analyzer.lichess-essentials.duckdns.org/api/health     # 200
```

And the internal wiring, which is the part most likely to be quietly broken:

```bash
curl -s -u 'test:testpassword1234@' \
  https://weakness.lichess-essentials.duckdns.org/api/health
# expect analyzer:true, analyzerVia:"folder", study:true

curl -s -u 'test:testpassword1234@' \
  https://prepper.lichess-essentials.duckdns.org/api/health
# expect repertoireDir:"/repertoires"  <- the shared volume, not a missing path
```

---

## Part 8 — The two cron jobs

```bash
crontab -e
```

```cron
*/20 * * * * nice -n 19 timeout 180 sha256sum /dev/zero >/dev/null 2>&1
30 4 * * 0 /home/ubuntu/prune.sh --apply >/dev/null 2>&1
```

**The first one is not optional and the numbers matter.** Oracle reclaims an
Always Free instance whose **95th-percentile CPU is under 20% over 7 days**.
That means you have to be *above* 20% for more than 5% of the time. 180 seconds
in every 1200 is a 15% duty cycle — three times what is needed. An earlier
version used 25 seconds, a 2% duty cycle, which satisfies nobody and would have
let the box be reclaimed while the cron job ran faithfully every 20 minutes.
`nice -n 19` means it yields instantly to real work, so it costs nothing in
practice.

The second is housekeeping; `~/prune.sh` with no arguments is a dry run.
Details in [FUTURE.md](FUTURE.md).

---

## Part 9 — Optional, in the right order

Neither of these is needed for the links to work.

### Resize to 2 OCPU / 12 GB — safe, and the biggest speed win

Always Free allows 2 OCPU / 12 GB. If the machine was built smaller, Stockfish
is the thing that suffers, and Stockfish is what these apps are.

> Instance → **Stop** → wait for Stopped → **Actions → Edit → Edit shape** →
> 2 OCPUs, 12 GB → Save → **Start**

**The IP does not change.** Stopping keeps an ephemeral public IP assigned —
see the table at the top. There is nothing to do to DuckDNS afterwards, and no
reason to reserve an IP first. (An earlier note in FUTURE.md claimed the
opposite; it was wrong.)

Oracle's console may still advertise the old 4 OCPU / 24 GB allowance while the
docs say it was halved to 2/12 on 15 June 2026 with no grandfathering. Build at
2/12; anything larger risks being stopped until resized.

### Reserve the public IP — pure convenience

Only worth it to skip one DuckDNS edit after a future rebuild.

> Instance → Networking → **Attached VNICs** → the VNIC → **IPv4 Addresses** →
> primary row → **Edit** → Public IP Type → **Reserved**

Oracle does **not** convert an ephemeral address into a reserved one in place —
you get a **different** address. So doing this *is itself* an IP change: update
DuckDNS immediately afterwards.

---

## What lives where

| Path | In git? | What |
|---|---|---|
| `~/Lichess-Essentials` | yes | the repo; `git pull && docker compose up -d --build` to deploy |
| `~/Lichess-Essentials/.env` | **no** | credentials + `DOMAIN`. Template: `.env.example` |
| `~/Lichess-Essentials/docker-compose.override.yml` | **no** | Caddy paths + `mem_limit`. Copy: `deploy/` |
| `~/caddy-http/Caddyfile` | **no** | HTTPS + landing page. Copy: `deploy/` |
| `~/caddy-http/site/index.html` | **no** | landing page. Copy: `deploy/` |
| `~/prune.sh` | **no** | housekeeping. Copy: `deploy/` |
| Docker named volumes | **no** | all app data. Back these up |

Everything in the "no" column has a committed copy under `deploy/`, which is
the point: a rebuild needs this document for *order*, not for content.

---

## Things that will bite

- **Certificates fail on first start.** DuckDNS is not pointing at the new box
  yet, or port 80 is shut at one of the two firewalls. Check
  `dig +short study.lichess-essentials.duckdns.org` first.
- **Do not use an `sslip.io` or `nip.io` name.** They resolve fine but are not
  on the Public Suffix List, so all their users share one Let's Encrypt bucket
  of 50 certificates a week and issuance fails in practice. No certificate
  means no port 443, and **phones then cannot load the site at all**, because a
  mobile browser tries `https://` first and will not fall back. That failure
  looks like "it only works on my laptop". `duckdns.org` *is* on the list, as
  are `no-ip.org`, `ddns.net`, `dynu.net`, `hopto.org` and `is-a.dev`.
- **`docker compose restart` does not reload `.env`.** Use `up -d`.
- **`docker compose down -v` deletes every volume.** That is what `-v` means.
- **Don't raise `mem_limit` without more RAM.** Five containers times the limit
  must stay under host RAM, or the host OOM killer runs before any container
  cap does.
- **The first build is slow.** 15–25 minutes on 2 ARM cores. It is not stuck.
- **Don't paste a real Lichess token into the public deployment.** Four of the
  apps keep it in a process-wide global, so it is shared with every visitor
  until that container restarts.

---

## Rebuild order, one screen

```
0.  Back up volumes, copy the archives off the machine   <-- before terminating
1.  Create VM: Ubuntu 24.04, Ampere A1.Flex, 2 OCPU / 12 GB, public IP = Yes
2.  Open 80 + 443 in the VCN security list AND in iptables
3.  Point DuckDNS at the new IP, confirm with dig        <-- before any compose up
4.  Install Docker, log out and back in
5.  Add 4G swap, swappiness 10
6.  git clone, copy deploy/* into place, cp .env.example .env, compose up --build
7.  Restore the volume archives, compose up -d
8.  Install both cron jobs (the heartbeat is load-bearing)
9.  Verify from a phone, not from the VM
10. Optional: resize to 2/12 (IP is safe), reserve the IP (IP changes)
```
