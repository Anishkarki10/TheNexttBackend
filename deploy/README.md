# Deploying to a Contabo VPS

Layout on the server:

```
internet ──443──▶ nginx (host, Let's Encrypt cert) ──▶ 127.0.0.1:5000 gunicorn (docker: api) ──▶ postgres (docker: db)
```

Examples use `api.example.com` for the API domain and Ubuntu 24.04. Replace both with your own.

## 1. DNS

At your DNS provider, add an **A record**: `api` → your Contabo server's IPv4 address. Add an **AAAA** record too if the server has IPv6. Wait until `dig +short api.example.com` returns the server IP before you start step 4.

If you use Cloudflare, keep the record **DNS only (grey cloud)** until certbot has issued the certificate. If you switch it to proxied later, set Cloudflare's SSL mode to *Full (strict)* and set `TRUSTED_PROXY_COUNT=2`.

## 2. Server setup (once)

```bash
ssh root@SERVER_IP
apt update && apt upgrade -y
apt install -y nginx certbot python3-certbot-nginx git ufw
curl -fsSL https://get.docker.com | sh

ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw enable
```

## 3. App

```bash
git clone <your-repo-url> /opt/TheNexttBackend
cd /opt/TheNexttBackend
cp .env.example .env
nano .env            # fill in every value (see comments in the file)
docker compose up -d --build
docker compose logs -f api      # wait for "Listening at: http://0.0.0.0:5000"
docker compose exec api flask seed-products   # once, adds the Protein Loaf product
curl http://127.0.0.1:5000/api/health        # {"status":"ok"}
```

In `.env`, `CORS_ORIGINS` must list **every** frontend origin exactly, with `https://` and no trailing slash, for example `https://thenextprotein.com,https://www.thenextprotein.com,https://admin.thenextprotein.com`.

## 4. nginx + HTTPS

```bash
cp deploy/nginx.conf /etc/nginx/sites-available/thenext-api
sed -i 's/api.example.com/api.YOURDOMAIN.com/' /etc/nginx/sites-available/thenext-api
ln -s /etc/nginx/sites-available/thenext-api /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

certbot --nginx -d api.YOURDOMAIN.com --redirect -m you@example.com --agree-tos
curl https://api.YOURDOMAIN.com/api/health
```

certbot sets up automatic renewal. Check it with `certbot renew --dry-run`.

## 5. Connect Vercel

In each Vercel project, go to Settings → Environment Variables and set the API URL to `https://api.YOURDOMAIN.com`. The admin panel uses `VITE_API_BASE_URL`; use whatever variable name the storefront reads. Then **redeploy**, because Vite bakes env vars in at build time.

## 6. Backups

```bash
crontab -e
0 3 * * * cd /opt/TheNexttBackend && ./deploy/backup.sh >> /var/log/thenext-backup.log 2>&1
```

Copy `/var/backups/thenext` off the server from time to time. A backup that lives only on the same VPS doesn't protect you if the VPS is lost.

Restore: `gunzip -c db-XXXX.sql.gz | docker compose exec -T db psql -U $POSTGRES_USER $POSTGRES_DB`

## Updating

```bash
cd /opt/TheNexttBackend && git pull && docker compose up -d --build
```

Migrations run automatically when the container starts.

## Troubleshooting

- **CORS error in the browser**: the origin isn't in `CORS_ORIGINS` exactly as the browser sends it. Fix `.env`, then run `docker compose up -d` again.
- **502 Bad Gateway**: the api container isn't running. Check `docker compose ps` and `docker compose logs api`.
- **413 on image upload**: `client_max_body_size` is missing from the nginx site config.
- **Admin gets logged out after about 15 minutes**: JWT access tokens expire after 15 minutes (the Flask-JWT-Extended default). Set `JWT_ACCESS_TOKEN_EXPIRES` (in seconds) in `.env` to change this.
