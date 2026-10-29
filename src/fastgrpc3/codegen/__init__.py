"""Proto-first code generation: `.proto` files -> Python models, services, descriptor."""

from fastgrpc3.codegen.generate import CodegenError, check, generate, write

__all__ = ["CodegenError", "check", "generate", "write"]
