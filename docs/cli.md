# Command line

`fastgrpc3` (or `python -m fastgrpc3`) has five commands. TARGET is `module:attribute`, a
`GrpcApp` or a `SchemaRegistry`; modules of the current directory are importable.

## `generate`

```text
fastgrpc3 generate [PROTO_PATH ...] [-o OUTPUT] [-I INCLUDE ...] [--check]
```

Python models, contracts and clients from `.proto` files (see [Proto first](proto-first.md)).

| Option | |
|--------|-|
| `PROTO_PATH` | directories (every `.proto` under them) or files |
| `-o, --output` | the package directory to write |
| `-I, --include` | extra import directories (repeatable) |
| `--check` | write nothing; exit 1 if a file is missing, outdated or stale |

Without arguments, `proto_paths`, `include` and `output` come from
`[tool.fastgrpc3.generate]` in `./pyproject.toml`.

## `run`

```text
fastgrpc3 run TARGET [--address HOST:PORT] [--workers N] [--grace SECONDS]
              [--transport grpc_aio|tonic]
```

Serve a `GrpcApp` until SIGINT or SIGTERM. `--workers` starts N processes sharing the port
(default 1); `--grace` is the time left to running calls on stop (default 5 s);
`--transport` overrides `FASTGRPC_TRANSPORT` and the app's choice (see
[Transport](server.md#transport)).

## `lock`

```text
fastgrpc3 lock TARGET [--lock proto.lock]
```

Number the fields that have no number yet and write `proto.lock`. It lists what changed and
warns about wire-breaking changes (a removed or renumbered field).

## `check`

```text
fastgrpc3 check TARGET [--lock proto.lock]
```

Exit 1 when `proto.lock` is missing or does not match the models; for CI.

## `breaking`

```text
fastgrpc3 breaking TARGET --against REF [--lock proto.lock]
```

List the changes since `REF` that break existing peers, and exit 1 if there is one. `REF`
is a git ref (`origin/main`, a tag): the code at that ref is exported in a temporary
worktree, with this fastgrpc3; or a directory of `.proto` files, such as an earlier
`fastgrpc3 export`.

- **breaking**: a message, enum, service or method removed; a field or enum value removed
  without reserving its number; a field whose wire type, cardinality (singular, repeated,
  map) or oneof changed; a reserved number reused; a method whose request, response or
  streaming changed. Types sharing an encoding stay compatible (`int32` → `int64`,
  `string` → `bytes`), as in `buf breaking`'s WIRE rules.
- **warning** (does not fail): a field or enum value renamed, `optional` added or removed.
  The wire still works, but JSON (the docs gateway, other languages) and the generated code
  change.

Pydantic constraints are not compared: a new field without a default is required by the
model, though proto3 has no required fields.

## `export`

```text
fastgrpc3 export TARGET [-o proto] [--lock proto.lock]
```

Write the `.proto` files of the contract, as the server compiles them. Fields without a
number in the models or the lock are an error: run `lock` first.

## `openapi`

```text
fastgrpc3 openapi TARGET [-o FILE]
```

Write the OpenAPI document of a `GrpcApp`'s RPCs (what `app.enable_docs` serves at
`/openapi.json`) to `FILE`, or to the standard output.

All commands exit 1 with an `error: ...` line on a contract or compilation error.
