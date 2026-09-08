"""Configuration loading must reject silently shadowed YAML keys."""

import pytest
import yaml

from deerflow.config.app_config import load_yaml_with_unique_keys


def test_duplicate_yaml_key_is_rejected_with_location() -> None:
    source = """summarization:
  keep:
    type: tokens
    value: 32000
  keep:
    type: messages
    value: 20
"""

    with pytest.raises(yaml.constructor.ConstructorError, match=r"duplicate key 'keep'.*line 5"):
        load_yaml_with_unique_keys(source)


def test_nested_unique_yaml_keys_load_normally() -> None:
    assert load_yaml_with_unique_keys("outer:\n  first: 1\n  second: 2\n") == {"outer": {"first": 1, "second": 2}}
