"""Codec benchmark (criteria P1, P2): encode and decode, µs per message.

    uv run --group bench python benches/codec_bench.py [--out benches/results/codec.json]

Compared, for each message size:

- fastgrpc3: `MessageCodec` (decode validates), and protoruf's `Codec` in construct mode
  (no validation) to show what an unvalidated path costs;
- fastgrpc2 0.5.0: its own codec (protobuf messages built field by field in Python);
- upb (`google.protobuf`): raw parse / serialize, and with the conversion to and from the
  Pydantic model (`MessageToDict` + `model_validate`), i.e. "generated code + Pydantic".
"""

import argparse
import json
import timeit
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from contract import SIZES, make_models, sample
from environment import environment, markdown_table
from fastgrpc2.core import codec as fastgrpc2_codec
from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from protoruf import Codec

from fastgrpc3 import Schema, SchemaRegistry

REPEAT = 7


def per_call(function: Callable[[], Any]) -> float:
    """Best time of one call over REPEAT rounds, in µs."""
    timer = timeit.Timer(function)
    number, _ = timer.autorange()
    return min(timer.repeat(REPEAT, number)) / number * 1e6


def fastgrpc3_rows() -> dict[str, dict[str, float]]:
    models = make_models()
    registry = SchemaRegistry(default_package="bench.v1")
    for size in SIZES:
        registry.add_model(models.of(size))
    schema = Schema.from_registry(registry)
    rows: dict[str, dict[str, float]] = {}
    for size in SIZES:
        model = models.of(size)
        message = sample(models, size)
        codec = schema.codec(model)
        data = codec.encode(message)
        assert codec.decode(data) == message
        construct = Codec(schema.descriptor, codec.full_name, model, mode="construct")
        rows[size] = {
            "bytes": len(data),
            "fastgrpc3 encode": per_call(partial(codec.encode, message)),
            "fastgrpc3 decode": per_call(partial(codec.decode, data)),
            "fastgrpc3 decode (construct)": per_call(partial(construct.decode, data)),
        }
        rows[size].update(upb_rows(schema.descriptor, codec.full_name, model, message, data))
    return rows


def upb_rows(
    descriptor: bytes, full_name: str, model: Any, message: Any, data: bytes
) -> dict[str, float]:
    pool = descriptor_pool.DescriptorPool()
    for file in descriptor_pb2.FileDescriptorSet.FromString(descriptor).file:
        pool.Add(file)
    upb_class = message_factory.GetMessageClass(pool.FindMessageTypeByName(full_name))
    parsed = upb_class.FromString(data)

    def decode_to_model() -> Any:
        values = json_format.MessageToDict(
            upb_class.FromString(data),
            preserving_proto_field_name=True,
            use_integers_for_enums=True,
        )
        return model.model_validate(values)

    def encode_from_model() -> bytes:
        upb_message = json_format.ParseDict(message.model_dump(mode="json"), upb_class())
        result: bytes = upb_message.SerializeToString()
        return result

    assert decode_to_model() == message
    return {
        "upb encode (raw)": per_call(parsed.SerializeToString),
        "upb decode (raw)": per_call(partial(upb_class.FromString, data)),
        "upb + Pydantic encode": per_call(encode_from_model),
        "upb + Pydantic decode": per_call(decode_to_model),
    }


def fastgrpc2_rows() -> dict[str, dict[str, float]]:
    models = make_models()
    rows: dict[str, dict[str, float]] = {}
    for size in SIZES:
        model = models.of(size)
        message = sample(models, size)
        serialize = fastgrpc2_codec.serializer(model)
        deserialize = fastgrpc2_codec.deserializer(model)
        data = serialize(message)
        assert deserialize(data) == message
        rows[size] = {
            "fastgrpc2 bytes": len(data),
            "fastgrpc2 encode": per_call(partial(serialize, message)),
            "fastgrpc2 decode": per_call(partial(deserialize, data)),
        }
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="write the results as JSON")
    args = parser.parse_args()

    rows = fastgrpc3_rows()
    for size, values in fastgrpc2_rows().items():
        rows[size].update(values)
    columns = list(rows["small"])
    print(markdown_table("µs per message (lower is better)", columns, rows))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"environment": environment(), "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
