# Newtonsoft.Json 13.0.3 Source Receipt

This is a real network-backed replay captured on 2026-07-17. The exact NuGet
package [`Newtonsoft.Json 13.0.3`](https://www.nuget.org/packages/Newtonsoft.Json/13.0.3)
declared repository commit
[`0a2e291c0d9c0c7675d445703e51750363a549ef`](https://github.com/JamesNK/Newtonsoft.Json/commit/0a2e291c0d9c0c7675d445703e51750363a549ef),
and the downloaded source tree verified at the same commit.

The resulting [Source Receipt](source-receipt.md) omits the temporary catalog
path, remote URL, aliases, and internal IDs. Reproduce the evidence from the
repository root:

```bash
receipt_demo_catalog="$(mktemp -d /tmp/inspect-dependency-source-receipt.XXXXXX)"

python3 scripts/inspect_dependency_source.py \
  --catalog-root "$receipt_demo_catalog" init --no-dashboard

python3 scripts/inspect_dependency_source.py \
  --catalog-root "$receipt_demo_catalog" \
  package fetch-nuget Newtonsoft.Json 13.0.3

python3 scripts/inspect_dependency_source.py \
  --catalog-root "$receipt_demo_catalog" \
  resolve Newtonsoft.Json --ref 13.0.3 --receipt
```

The verification timestamp will reflect the replay time. The package,
repository, expected commit, observed commit, provenance class, and `PROVEN`
verdict should match the checked-in receipt.
