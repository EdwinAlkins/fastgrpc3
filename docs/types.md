# Type mapping

One table serves both directions: the `.proto` rendered from Python models (code first) and
the models generated from `.proto` files (proto first). Markers come from
`fastgrpc3.types`; generated code uses the same ones, so a generated model renders back to
the same `.proto`.

## Scalars

| Python | proto | Notes |
|--------|-------|-------|
| `str` | `string` | |
| `bytes` | `bytes` | |
| `bool` | `bool` | |
| `int` | `int64` | `Int64` checks the bounds |
| `Int32` | `int32` | bounds checked by Pydantic |
| `UInt32` / `UInt64` | `uint32` / `uint64` | |
| `SInt32` / `SInt64` | `sint32` / `sint64` | zigzag encoding |
| `Fixed32` / `Fixed64` | `fixed32` / `fixed64` | |
| `SFixed32` / `SFixed64` | `sfixed32` / `sfixed64` | |
| `float` (or `Double`) | `double` | |
| `Float32` | `float` | rounded to 32 bits on the wire |

## Composite types

| Python | proto |
|--------|-------|
| a `ProtoModel` / `BaseModel` | `message` |
| `X \| None` (scalar or enum `X`) | `optional X` |
| `M \| None`, or `M` (a model) | `M` (messages always have presence) |
| `list[X]` | `repeated X` |
| `dict[K, V]`, `K` an int, `str` or `bool` | `map<K, V>` |
| a `ProtoEnum`, an `IntEnum`, an `Enum` with `@proto_enum` | `enum` |
| `T \| None = field(n, oneof="g")` | member of `oneof g` |

## Well-known types

| Python | proto |
|--------|-------|
| `datetime.datetime` (aware) | `google.protobuf.Timestamp` |
| `datetime.timedelta` | `google.protobuf.Duration` |
| `datetime.date` | `google.type.Date` |
| `dict[str, Any]` / `dict[str, JsonValue]` | `google.protobuf.Struct` |
| `Any` / `JsonValue` (or `Value` in a map) | `google.protobuf.Value` |
| `ListValue` | `google.protobuf.ListValue` |
| `StringValue \| None`, `Int64Value \| None`… (the nine wrappers) | `google.protobuf.StringValue`… |
| `FieldMask \| None` | `google.protobuf.FieldMask` (a `list[str]` of paths) |
| `fastgrpc3.types.Empty` | `google.protobuf.Empty` |
| `fastgrpc3.types.AnyMessage` | `google.protobuf.Any` (`type_url`, `value`) |

A naive `datetime` is refused when encoding a `Timestamp`: attach a time zone. Timestamps
decode as UTC `datetime`s; nanoseconds are truncated to microseconds.

## Everything else: the JSON carrier

Any other type (`Decimal`, `UUID`, `Literal`, `tuple`, `set`, unions that are not
`X | None`, `list[list[X]]`, `list[X] | None`, `dict` with float keys…) is sent as a
`string` holding its JSON, as Pydantic dumps it, and validated back on decode. The
`.proto` marks the field `// json`.

## Names in generated code

Proto first, a few proto names cannot be Python names as they are:

| proto | Python |
|-------|--------|
| `message A { message B {} }` | class `A_B`, `proto="pkg.A.B"` |
| field `from`, `json`, `model_config`… | `from_`, with `proto_name="from"` |
| field `_internal` | `internal_` |
| values `ORDER_STATUS_PAID` of `enum OrderStatus` | `OrderStatus.PAID` (prefix removed when every value has it) |
| values without the prefix | kept, with `prefix=""` |
