# Review patches

Applied with user approval on 2026-09-08 to
`/home/cristina/selfhosted/telemetry`. Grafana's mounted file and provisioned
dashboard now match candidate SHA256
`0934cac7deb772fd9160977cd2bcae48f7dfc581485ae80de93cf28d177f0a24`.
All seven live queries pass; backend tests: 27 passed, 4 skipped. Do not reapply
this patch to the updated checkout.

`2026-09-08-telemetry-dashboard.patch` is a review-only patch for the live
session-handoff Grafana dashboard. It was built from the read-only baseline
SHA256
`be5a627fc1657522fd6c450db9bdac5e8d4be0eeab049139c69ebcacc22e389f`.

It changes all seven dashboard queries to select `origin="real"`, filters the
failure panel to `result=failure|fallback`, and renames/redefines panel 7 as
thresholded active environment-days by plugin version. It includes the
matching `tests/test_deploy.py` regression test. It does not claim a unique
installation or user count.

The staged candidate was validated as JSON and by the isolated dashboard test;
all seven LogQL queries were also executed read-only against Loki successfully.
The live mounted file was unchanged during review; the approved deployment is
recorded above. Because `git apply` replaced the host inode, a restart of only
the existing Grafana container was needed to refresh its single-file bind mount.

Original application commands, for a checkout still matching the baseline and
with deployment approval:

```sh
cd /home/cristina/selfhosted/telemetry
sha256sum grafana-dashboard-session-handoff.json
git apply --check /home/cristina/session-handoff/docs/patches/2026-09-08-telemetry-dashboard.patch
git apply /home/cristina/session-handoff/docs/patches/2026-09-08-telemetry-dashboard.patch
rtk pytest -q tests/test_deploy.py::test_session_handoff_dashboard_uses_real_rows_and_bounded_active_days
```

Approval for this deployment was received. Any subsequent deployment requires
its own applicable authorization.
