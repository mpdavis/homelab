# Outbound email

**Alert:** `GatusEndpointDown` for `smtp-relay`. It only means the relay stopped
accepting connections; mail the provider refuses shows up in the relay's logs.

Services hand mail to the Postfix relay in `docker/infra/mail/` at
`10.0.1.58:587` (no auth, no TLS, sender `@notify.mpdavis.com`). The relay
relays to Resend.

## Send a test message

From either host, through the same path a service uses:

```sh
printf 'From: test@notify.mpdavis.com\nTo: michael@mpdavis.com\nSubject: relay test\n\nhello\n' \
  | curl -sS smtp://10.0.1.58:587 --mail-from test@notify.mpdavis.com \
      --mail-rcpt michael@mpdavis.com -T -
```

## Mail not arriving

```sh
ssh root@10.0.1.58
docker compose -p mail logs --tail 100 smtp-relay
docker compose -p mail exec smtp-relay postqueue -p   # what is stuck, and why
docker compose -p mail exec smtp-relay postqueue -f   # retry now
```

| Sign in the logs | Cause | Fix |
| --- | --- | --- |
| `Sender address rejected: Access denied` | The service sends from a domain other than `notify.mpdavis.com` | Change the service's from address |
| `Client host rejected: Access denied` | The client is outside `POSTFIX_mynetworks` | Send from the compose host or a container; add the address to `mynetworks` only if it should relay |
| `SASL authentication failed` | The API key in Bitwarden is wrong or revoked | Make a new sending-only key in Resend, update `SMTP Relay - Password` in Bitwarden, redeploy the stack |
| Upstream `domain is not verified` | The DNS records in `tofu/cloudflare` (`mail_records`) are missing or stale | Compare them with Resend's Domains page and `mise tofu apply cloudflare` |
| Delivered, but in Gmail's spam | DKIM or SPF failing | Gmail → "Show original" shows which; fix the record |

Queued mail is kept in the `mail_spool` volume and retried for five days, so
fixing the cause and running `postqueue -f` sends the backlog.

## Switching provider

Services never see the provider, so a switch touches three places:

1. `docker/infra/mail/compose.yaml`: `RELAYHOST` and `RELAYHOST_USERNAME`
   (and `POSTFIX_message_size_limit` if the new limit is lower).
2. Bitwarden: the new provider's SMTP password, in the secret
   `docker/infra/mail/.doco-cd.yml` points at (or a new one, with the UUID
   updated there).
3. `tofu/cloudflare`: replace `mail_records` with the new provider's DKIM, SPF
   and bounce records for `notify.mpdavis.com`, then
   `mise tofu apply cloudflare`.

Apply the DNS first and wait for the provider to mark the domain verified, then
merge the relay change.
