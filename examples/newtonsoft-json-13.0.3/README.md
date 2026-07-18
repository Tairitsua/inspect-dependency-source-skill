# Exact NuGet source replay: Newtonsoft.Json 13.0.3

This real network-backed replay was captured on 2026-07-17. The exact NuGet
package [`Newtonsoft.Json 13.0.3`](https://www.nuget.org/packages/Newtonsoft.Json/13.0.3)
declared repository commit
[`0a2e291c0d9c0c7675d445703e51750363a549ef`](https://github.com/JamesNK/Newtonsoft.Json/commit/0a2e291c0d9c0c7675d445703e51750363a549ef),
and the acquired source tree verified at that same commit.

Run the replay from the repository root:

```bash
example_catalog="$(mktemp -d /tmp/inspect-dependency-source-example.XXXXXX)"

python3 scripts/inspect_dependency_source.py \
  --catalog-root "$example_catalog" init --no-dashboard

python3 scripts/inspect_dependency_source.py \
  --catalog-root "$example_catalog" \
  package fetch-nuget Newtonsoft.Json 13.0.3

python3 scripts/inspect_dependency_source.py \
  --catalog-root "$example_catalog" \
  resolve Newtonsoft.Json --ref 13.0.3 --json
```

The final JSON should identify:

- Package `Newtonsoft.Json` at version `13.0.3`.
- Repository `JamesNK/Newtonsoft.Json`.
- Expected and observed commit
  `0a2e291c0d9c0c7675d445703e51750363a549ef`.
- `exact_commit` resolution and a verified source artifact.
- A local `source_path` containing the tree the agent should inspect read-only.

The `source_path`, catalog IDs, operation timestamps, and other local runtime
details vary by machine, so no JSON output is checked into this example.
