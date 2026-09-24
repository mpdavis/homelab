# Infra host stacks

Same conventions as `../stacks/CLAUDE.md`, deployed by the infra host (poll
target `infra`, so `doco-cd/.doco-cd.infra.yaml`) rather than the compose host.

What belongs here: ingress and services for things that do not run on the
compose host, and anything that has to keep working while it is down — such as
monitoring (`docs/compose.md`).
