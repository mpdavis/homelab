# Ansible Guidelines

## Use Built-in Modules

Prefer `ansible.builtin.*` modules over shell/command whenever a built-in exists:

- `ansible.builtin.apt` for package management, not `apt-get` via shell
- `ansible.builtin.systemd` for service management (start/stop/enable/restart)
- `ansible.builtin.template` for deploying config files and unit files
- `ansible.builtin.file` for permissions, symlinks, directories
- `ansible.builtin.get_url` for downloading files
- `ansible.builtin.user` / `ansible.builtin.group` for user management
- `ansible.builtin.uri` for HTTP requests / API calls

Only use `ansible.builtin.shell` or `ansible.builtin.command` when no built-in module covers the operation (e.g., `nvidia-ctk runtime configure`, `cloud-init status`). When you must use shell/command, set `changed_when` and use `creates`/`removes` args where applicable to ensure idempotency.

## Role Structure

- Keep roles focused on a single concern (e.g., `common`, `lxc`, `vm`, `gpu`, `gitea`)
- Do not mix conditional platform logic (LXC vs VM) into shared roles — use separate roles and apply them conditionally in the playbook
- Use `defaults/main.yml` for non-sensitive configuration defaults
- Use `templates/` for config files and systemd unit files (not inline `content:` in copy tasks)
- Use `handlers/` for service restarts triggered by config changes

## Secrets

Never commit secrets or default passwords to the repository. Use `vars_prompt` in playbooks for credentials needed at runtime. Sensitive values can also be passed via `-e` flags or environment variables.

## Playbook Conventions

- Always use fully qualified collection names (`ansible.builtin.apt`, not `apt`)
- Use `become: true` at the play level, not per-task
- Target inventory groups, not individual hostnames, when possible
- Run `ansible-playbook --syntax-check` before committing
