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
```

Serve a `GrpcApp` until SIGINT or SIGTERM. `--workers` starts N processes sharing the port
(default 1); `--grace` is the time left to running calls on stop (default 5 s).

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

## `export`

```text
fastgrpc3 export TARGET [-o proto] [--lock proto.lock]
```

Write the `.proto` files of the contract, as the server compiles them. Fields without a
number in the models or the lock are an error: run `lock` first.

All commands exit 1 with an `error: ...` line on a contract or compilation error.
