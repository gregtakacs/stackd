"""Tiny typed constructor: build a dataclass tree from parsed YAML, with
unknown-key rejection and readable path-qualified errors. Keeps stackd's only
runtime dependency PyYAML."""

from __future__ import annotations

import dataclasses
import types
import typing
from enum import Enum
from typing import Any, get_args, get_origin


class ConfigError(ValueError):
    pass


def build(tp: Any, data: Any, path: str = "$") -> Any:
    origin = get_origin(tp)

    # Optional[X] / Union[...]
    if origin in (typing.Union, types.UnionType):
        members = get_args(tp)
        non_none = [a for a in members if a is not type(None)]
        if data is None and len(non_none) < len(members):
            return None
        if len(non_none) == 1:
            return build(non_none[0], data, path)
        for a in non_none:
            try:
                return build(a, data, path)
            except ConfigError:
                continue
        raise ConfigError(f"{path}: {data!r} matches none of {tp}")

    if origin in (list, typing.List):
        if not isinstance(data, list):
            raise ConfigError(f"{path}: expected a list, got {type(data).__name__}")
        (item_t,) = get_args(tp) or (Any,)
        return [build(item_t, v, f"{path}[{i}]") for i, v in enumerate(data)]

    if origin in (dict, typing.Dict):
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a mapping, got {type(data).__name__}")
        _, vt = get_args(tp) or (str, Any)
        return {k: build(vt, v, f"{path}.{k}") for k, v in data.items()}

    if tp is Any or tp in (dict, list):
        return data

    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(data)
        except ValueError:
            raise ConfigError(
                f"{path}: {data!r} is not one of {[e.value for e in tp]}"
            ) from None

    if dataclasses.is_dataclass(tp):
        if not isinstance(data, dict):
            raise ConfigError(
                f"{path}: expected a mapping for {tp.__name__}, got {type(data).__name__}"
            )
        hints = typing.get_type_hints(tp)
        fields = {f.name: f for f in dataclasses.fields(tp)}
        unknown = sorted(set(data) - set(fields))
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) {unknown} for {tp.__name__}")
        kwargs: dict[str, Any] = {}
        for name, f in fields.items():
            if name in data:
                kwargs[name] = build(hints[name], data[name], f"{path}.{name}")
            elif f.default is not dataclasses.MISSING:
                kwargs[name] = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                kwargs[name] = f.default_factory()
            else:
                raise ConfigError(f"{path}: missing required key {name!r} for {tp.__name__}")
        return tp(**kwargs)

    if tp is float:
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            raise ConfigError(f"{path}: expected a number, got {type(data).__name__}")
        return float(data)
    if tp is int:
        if isinstance(data, bool) or not isinstance(data, int):
            raise ConfigError(f"{path}: expected an integer, got {type(data).__name__}")
        return data
    if tp is str:
        if not isinstance(data, str):
            raise ConfigError(f"{path}: expected a string, got {type(data).__name__}")
        return data
    if tp is bool:
        if not isinstance(data, bool):
            raise ConfigError(f"{path}: expected true/false, got {type(data).__name__}")
        return data

    return data
