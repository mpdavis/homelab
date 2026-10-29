# doco-cd deploys failing

**Alerts:** `DocoCdDeploymentFailed` — a stack's deploy errored in the last
10 minutes. `DocoCdPollFailing` — poll jobs have been erroring for 15 minutes
(`grafana-cloud/rules/docker-hosts.yml`).

The two usually arrive together. doco-cd retries a failed deploy on every
poll, and each retry fails the poll job, so one broken stack keeps both
firing. Other stacks still deploy; check that before assuming merges are stuck.

## Find which stack and why

```sh
ssh root@<host>
cd / && docker compose -p doco-cd logs --since 1h doco-cd | grep -E '"level":"(error|warning)"' | tail -5
```

The `error` field names the stack and the stage. To see whether other merges
are landing, compare a recently bumped image with `docker ps`.

## Usual causes

| Error | Cause | Fix |
| --- | --- | --- |
| `timed out after 3m0s waiting for services to become ready: <svc>` | The service never turned healthy within doco-cd's timeout | See below |
| Every poll fails, no stack named, error mentions a path in the repo | A dangling symlink anywhere in the repo | Remove or fix it on `main` |
| Secret fetch errors, `${VAR:?...}` unset | A Bitwarden UUID in the stack's `.doco-cd.yml` is wrong or not shared with the host's machine account | Fix the UUID or the project access in Bitwarden |
| Every poll fails, no stack named, error mentions fetching or cloning | GitHub unreachable, or doco-cd's GitHub token expired | Wait out a GitHub blip; otherwise rotate the token |

### A service never turns healthy

```sh
docker ps -a --filter label=com.docker.compose.project=<stack>
docker inspect <container> --format '{{json .State.Health}}'
docker logs --tail 50 <container>
```

- **The app is failing.** Its log says why; fix that and the next retry deploys.
- **The app is fine but slow to start.** If its healthcheck `start_period`
  is longer than 180s, the stack needs `timeout:` in its `.doco-cd.yml`
  (see `iptv`, `downloads`). Without it every retry recreates the
  container before it can finish starting.
