# Orion public mobile web beta

Use the existing responsive web UI on Safari/Chrome through a stable AWS-generated
`https://<api-id>.execute-api.ap-south-1.amazonaws.com` address. No purchased domain,
Apple membership or Expo account is needed. This is a browser app, not an App Store
release. The URL stays the same while the API resource is retained.

## Architecture and boundaries

Browser HTTPS → API Gateway HTTP API → private VPC link → internal ALB → private
nginx port 8080 → loopback portal port 8000. The UI and API use the same origin.
The private hops inside the VPC use HTTP; this is not end-to-end TLS. No public
inbound EC2 port is opened. The EC2 instance, data, existing EIP/Kotak whitelist and
paper workers stay in place. The two new subnets have only local routes, no NAT.

The gateway overwrites the visitor IP header before forwarding. Nginx accepts that
header only from the ALB subnets and overwrites it again for the portal. The portal
trusts it only from loopback when explicitly configured. Arbitrary X-Forwarded-For
headers cannot bypass application login/registration limits. The gateway has an
aggregate throttle; nginx additionally limits each visitor to 10 requests/second
with a burst of 40. These are beta safeguards, not a guarantee against all abuse.

Existing controls remain mandatory: owner-approved signup; per-user authorization;
encrypted saved credentials; Secure/HttpOnly/SameSite cookies; Origin and CSRF
checks; no-store responses; CSP and frame protection. HTTPS responses add HSTS.
Live order execution remains disabled. No CDN or API response cache is configured.
API request/body logs and nginx access logs are disabled to avoid collecting
authentication material. System/service error logs still exist.

## Costs and operational limits

This is **not free hosting**. New billable resources are one internal Application
Load Balancer (hours and LCUs), API Gateway requests/data, and applicable data
transfer. Existing EC2/EBS/IP costs continue. Review Mumbai pricing in the AWS
calculator and set an AWS Budget before applying. No NAT gateway, public ALB IPv4,
custom domain or certificate purchase is added.

The backend is still one EC2 instance with SQLite; the ALB does not make the
application highly available. Before inviting users, take and verify a private
backup of `/var/lib/orion/portal` and the encryption key separately. Plan automated
backups/restore drills, monitoring and security updates for ongoing use. This
change is not a penetration test or a production security certification.

HTTP API integrations have a 30-second timeout and 10 MB payload limit. Test the
built UI assets and Telegram/Kotak connection flows on the deployed endpoint. If a
connection operation times out, check its account status before repeating it.
Do not enable real orders to test this ingress.

## Deploy in order

1. Merge the reviewed change and run **Deploy paper application** from main.
   The existing deploy builds the web bundle and installs it with the portal.
2. Confirm an approved owner exists. If not, bootstrap your existing owner through
   SSM using the documented `bootstrap-owner` command; never promote public signup
   automatically. Activation refuses to proceed without an approved owner.
3. An AWS administrator re-runs `bootstrap/create_backend.py` with the **same**
   prefix and exact main OIDC subject as the existing installation, plus
   `--public-web`. This opts into deployment permissions for ALB, API Gateway,
   ingress rules and their service-linked roles. It does not grant runtime users
   infrastructure access. These permissions cover matching services in Mumbai,
   not exclusively individual Orion resource ARNs; restrict repository writers.
4. Set the repository Actions variable `PUBLIC_WEB_ENABLED` to `true`. It defaults
   to false when absent, so merging/deploying alone does not expose the app.
5. Run **Infrastructure → plan**. Review the saved plan and costs. Expect new web
   resources and an in-place EC2 security-group attachment. **Stop if EC2, its
   volume, EIP or existing subnet would be replaced or destroyed.** Apply only
   that reviewed plan with its exact manifest key and SHA256, per `setup.md`.
6. Copy the apply output `public_web_url`. Run **Enable public web** from main and
   paste this URL into its single input. The workflow validates the API belongs
   to this AWS account, is tagged Orion, and that the installed app matches main.
   It configures nginx, the HTTPS origin and trusted proxy, then restarts only the
   portal. It leaves the worker/supervisor processes alone. Wait for ALB health.
7. Complete the checks below before sharing the URL. Until configuration finishes,
   the endpoint can return 503; this is not a ready-to-share state.

After activation, use the HTTPS URL. The previous localhost browser URL will no
longer be the allowed origin. SSM administration remains available. Later normal
app deployments preserve the public configuration and use its host for health
checks. Keep `PUBLIC_WEB_ENABLED=true` while retaining the endpoint.

## Verify before inviting users

- On a phone over cellular, open the URL and verify the actual UI, graph, filters,
  asset loading, login, logout and refresh; no laptop tunnel should be required.
- An unauthenticated `/api/me` must return 401, never account details.
- Register a second test account. It must remain pending until the owner approves
  it. It must not read the owner's trades, credentials, analytics or admin APIs.
- Owner approves it; confirm it starts with its own isolated, paused paper account.
  Suspend it and confirm trading controls/data endpoints are blocked again.
- Confirm cookies are Secure/HttpOnly/SameSite, responses are no-store, foreign
  Origin mutations fail, and missing CSRF tokens cannot change browser settings.
- Confirm EC2 public ports 8000/8080 cannot be reached directly. Inspect gateway
  metrics and service logs for errors. Test reauthentication and stale-feed alerts.

These cloud checks are not replaced by local unit tests. Do not share test passwords
or broker/Telegram credentials; each user signs in and connects their own accounts.

## Disable / rollback

To immediately stop public ingress without deleting the stable URL, use SSM:

```bash
sudo systemctl stop nginx
```

Paper workers continue. To restore the private loopback portal, remove only the
`30-public-web.conf` systemd drop-in and `/etc/orion/public-web.env`, run
`sudo systemctl daemon-reload`, then `sudo systemctl restart orion-portal`.
The original `/etc/orion/portal.env` is never overwritten by activation. Previous
public configuration is backed up under `/var/backups/orion-public-web` and restored
automatically if activation fails. Do not roll back database files to change URLs.

Stopping nginx does not stop ALB charges. Removing the optional web infrastructure
requires a fresh reviewed Terraform plan with `PUBLIC_WEB_ENABLED=false`; it deletes
the public API and its URL. Re-enabling later generates a different URL.

## References

- [AWS HTTP API private integrations](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-private.html)
- [HTTP API parameter mapping](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-parameter-mapping.html)
- [ALB pricing](https://aws.amazon.com/elasticloadbalancing/pricing/)
- [API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/)
