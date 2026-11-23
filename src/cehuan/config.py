"""配置读取与策略参数覆盖。

本模块为 CLI、研究和回测提供统一 TOML 配置入口；它只解析和复制配置，
不会连接数据源、修改磁盘配置或选择适合某个账户的风险参数。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "default.toml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 TOML 配置并返回普通字典。

    ``path`` 可指定配置文件；省略时读取项目默认配置。函数只读文件，
    路径不存在或 TOML 语法无效时分别传播文件与解析异常。
    """
    config_path = Path(path) if path else DEFAULT_CONFIG
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def overlay_strategy(config: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """返回应用已登记覆盖项后的浅层隔离配置副本。

    ``config`` 是原始配置，``overrides`` 按已存在的 strategy、market、
    costs、plan 字段名匹配；值为 ``None`` 或未匹配的键会被忽略。
    返回副本，不改写输入字典；它不验证参数业务范围。
    """
    copied = {key: dict(value) if isinstance(value, dict) else value for key, value in config.items()}
    for section in ("strategy", "market", "costs", "plan"):
        if section in copied:
            copied[section] = dict(copied[section])
    for key, value in overrides.items():
        if value is None:
            continue
        for section in ("strategy", "market", "costs", "plan"):
            if key in copied.get(section, {}):
                copied[section][key] = value
                break
    return copied
