# Public nginx

`zaza-vpn.conf` is the production reverse-proxy configuration for both the
canonical domain and the censorship fallback hostname.

Deploy it after the required certificates exist:

```sh
install -m 0644 deploy/nginx/zaza-vpn.conf /etc/nginx/sites-available/zaza-vpn
ln -sfn /etc/nginx/sites-available/zaza-vpn /etc/nginx/sites-enabled/zaza-vpn
mkdir -p /var/www/letsencrypt
nginx -t
systemctl reload nginx
```

The fallback certificate must renew through the webroot exposed by this
configuration:

```sh
certbot certonly --webroot -w /var/www/letsencrypt \
  --cert-name panel.94-249-180-48.sslip.io \
  -d panel.94-249-180-48.sslip.io
```
