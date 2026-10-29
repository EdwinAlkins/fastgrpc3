"""Smoke tests: the package installs, and the protoruf features it builds on exist."""

from importlib.metadata import version

import fastgrpc3


def test_version_matches_the_installed_distribution() -> None:
    assert fastgrpc3.__version__ == version("fastgrpc3")


def test_protoruf_codec_is_available() -> None:
    from protoruf import Codec, OpenIntEnum, compile_proto_from_sources

    descriptor = compile_proto_from_sources(
        {"t.proto": 'syntax = "proto3"; package t; message M { string id = 1; }'},
        root="t.proto",
    )
    codec = Codec(descriptor, "t.M")
    assert codec.decode_dict(codec.encode({"id": "x"})) == {"id": "x"}
    assert issubclass(OpenIntEnum, int)
