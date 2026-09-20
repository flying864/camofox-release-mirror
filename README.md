# Camofox official release mirror

Personal maintenance pipeline for [jo-inc/camofox-browser](https://github.com/jo-inc/camofox-browser). Not an upstream project. No source rebuilds or NAS deployment.

Manually dispatch **Verify and mirror official Camofox**. Default is test-only. Publishing requires the `DOCKERHUB_TOKEN` Actions secret for Docker Hub account `flying864`; configure it in GitHub Settings > Secrets and variables > Actions. Never commit credentials.

The pipeline selects the greatest stable `vX.Y.Z` release (excluding backup releases), resolves its official GHCR `linux/amd64` manifest, captures the previous Docker Hub digest for rollback, pulls by digest, and verifies image config/architecture. A temporary loopback-only container with 2 GB shared memory must pass health, version and three independent example.com navigation/content/cleanup checks. Only then may it push `flying864/camofox-browser:latest`. Publication uses digest-preserving registry copy and is confirmed by exact platform manifest, config and ordered layer digest equality.

A failed test blocks publishing. There is no source-build fallback. Previous digest is in the run log; rollback requires an explicit decision. Runs daily at 10:00 Asia/Shanghai (02:00 UTC), with publishing enabled for scheduled events. If the official amd64 manifest and Docker Hub identity already match, it exits silently without pulling, testing or pushing. No image artifacts, persistent caches, paid larger runners, production proxy settings or LAN connections are configured. GitHub-hosted standard Ubuntu is used with a 25-minute deadline and serialized runs.

GitHub schedules can be delayed or dropped under load; public-repository schedules may be disabled after 60 days without repository activity. Failed runs are visible in Actions; enable GitHub Actions failure notifications in account notification settings. No Telegram alert integration is configured.

Cloud tests do not validate a NAS proxy path. Validate separately after any explicitly authorized NAS deployment.
