# Deploying on Oracle Cloud's Always Free tier

A runbook for running the compose stack (DEPLOYMENT.md) on one Oracle Cloud Infrastructure (OCI) Always Free Arm instance at no cost. The images build natively for `linux/arm64`, so nothing in the project changes for this host.

## Why this host, and why this size

OCI's Always Free tier includes Arm (Ampere A1) compute worth 1,500 OCPU hours and 9,000 GB hours a month, which is 2 OCPUs and 12 GB of memory running continuously, and 200 GB of block storage ([Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm)). The stack needs far less: about 1.9 GB of memory across its six containers at rest, a few GB more while a refresh or a dashboard download runs, and under 100 MB of data.

Create a **1 OCPU, 6 GB** instance, not the full allowance. OCI may reclaim an Always Free instance it deems idle: one whose 95th-percentile CPU, network and memory utilisation all stay under 20% for seven days (memory counts on A1 shapes). On a 12 GB instance the stack sits near 16% memory and could qualify; on 6 GB it sits near 32%, so it is not idle by that measure, and it still has room for a refresh. The rest of the allowance stays free for something else.

OCI asks for a card at sign-up. Staying within the Always Free resources costs nothing, and a budget alert (step 2) tells you if anything billable is ever created.

## 1. Create the instance

In the OCI console, create a compute instance:

- **Image:** Canonical Ubuntu 24.04 (the Arm build is chosen automatically for an Ampere shape).
- **Shape:** Ampere, `VM.Standard.A1.Flex`, **1 OCPU, 6 GB memory**. Check that the shape is labelled Always Free-eligible.
- **Networking:** a public subnet with a public IPv4 address. Leave the default security list, which allows only SSH (port 22) in; nothing else needs to be opened.
- **SSH key:** upload your public key.
- **Boot volume:** the default size (about 47-50 GB) is enough for the images, the Docker build cache and the registers.

If the console reports that it is out of capacity for the shape, try another availability domain, or try again later; Arm capacity in busy regions comes and goes.

## 2. Add a budget alert

In Billing and Cost Management, create a budget for the tenancy with a small amount (for example 1 of your currency) and an alert to your email. A free-only deployment never triggers it; if it fires, something billable was created.

## 3. Prepare the host

SSH in as `ubuntu`, clone the repository and run the bootstrap script, which installs Docker and the compose plugin from Docker's apt repository, git, make and python3-yaml, adds a 2 GB swap file, turns on unattended security upgrades and adds you to the `docker` group:

```bash
ssh ubuntu@<public-ip>
git clone https://github.com/Behordeun/nigeria-facility-register.git
cd nigeria-facility-register
sudo bash scripts/bootstrap_host.sh
exit
```

Log back in so the `docker` group applies. The script is safe to run again.

## 4. Configure the secrets

Create `.env` from the example and fill the three required values with generated ones, without printing them:

```bash
cd ~/nigeria-facility-register
cp .env.example .env
chmod 600 .env
sed -i "s|^SUPERSET_SECRET_KEY=.*|SUPERSET_SECRET_KEY=$(openssl rand -base64 42)|" .env
sed -i "s|^SUPERSET_ADMIN_PASSWORD=.*|SUPERSET_ADMIN_PASSWORD=$(openssl rand -base64 24)|" .env
sed -i "s|^SUPERSET_DB_PASSWORD=.*|SUPERSET_DB_PASSWORD=$(openssl rand -hex 24)|" .env
```

`SUPERSET_ADMIN_USERNAME` defaults to `admin`. The admin password is the one you sign in with; open `.env` in an editor on the host to read it, and keep it in your password manager. `MAPBOX_API_KEY` (basemap tiles) and `NFR_ALERT_WEBHOOK_URL` (failure alerts, see DEPLOYMENT.md) are optional. `.env` is gitignored and never enters the images.

## 5. Build and start

```bash
docker compose up -d --build
docker compose ps
```

The first build compiles both images on the instance and takes a while on one OCPU; later builds reuse the cache.

## 6. Place the raw inputs

The two GRID3 geopackages are downloaded by hand (README, "Run it"), so copy them from the machine that has them. Copy into a staging folder first and then move both into place, so the refresh sensor never sees a half-copied file or one new file beside an old one:

```bash
# on your own machine, from the repository root
ssh ubuntu@<public-ip> 'mkdir -p ~/nigeria-facility-register/data/raw/.incoming'
scp data/raw/nga_health_facilities_v2_0.gpkg data/raw/grid3_nga_health_facilities_v3_0.gpkg \
    ubuntu@<public-ip>:~/nigeria-facility-register/data/raw/.incoming/
ssh ubuntu@<public-ip> 'cd ~/nigeria-facility-register/data/raw && mv .incoming/*.gpkg . && rmdir .incoming'
```

## 7. Publish the first register and load the dashboard

The new-vintage sensor launches a refresh on its own within about an hour of the files appearing. To start one now:

```bash
docker compose exec -T daemon uv run dagster job launch -m nfr.orchestration.definitions -j refresh_register
```

When the run has succeeded (watch it in the Dagster UI, step 8), load the dashboard:

```bash
make dashboard PY=python3
```

It stops with a pointer to the refresh if no register has been published yet.

## 8. Open the UIs

Both UIs listen on the instance's `127.0.0.1` only. Reach them through an SSH tunnel from your own machine:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8088:127.0.0.1:8088 ubuntu@<public-ip>
```

Then open `http://127.0.0.1:3000` (Dagster) and `http://127.0.0.1:8088` (Superset) locally. To share the dashboard with people who should not have SSH access, put an authenticating proxy or tunnel in front of Superset first, and work through "Who can see what" in DEPLOYMENT.md; do not publish the ports directly.

## Updating

```bash
cd ~/nigeria-facility-register
git pull
docker compose build
docker compose up -d
make dashboard PY=python3
```

The register, the archived registers and Superset's records live in named volumes, so they survive a rebuild. Rolling back a register works as in DEPLOYMENT.md.

## Keeping it free

- After the first week, check the instance's metrics page: memory utilisation should sit above 20%. If OCI ever reclaims the instance, recreate it with these steps; the dashboard comes back from the bundle with `make dashboard`, and the first refresh rebuilds the register.
- Keep the instance at 1 OCPU and 6 GB, and do not attach block volumes beyond the free 200 GB in total.
- Remove old images and build cache now and then with `docker image prune` and `docker builder prune`.
