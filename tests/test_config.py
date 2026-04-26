"""Tests for the config package."""

from __future__ import annotations

import pytest

from config import AppConfig, load_config
from config.storage import StorageModel


class TestDefaults:
    def test_empty_load_returns_defaults(self):
        cfg = load_config(None)
        assert isinstance(cfg, AppConfig)
        # Default parser backend is the no-extra-deps one.
        assert cfg.parser.backend == "pymupdf"
        assert cfg.storage.mode == "local"

    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "does_not_exist.yaml")
        assert cfg.storage.mode == "local"


class TestYamlLoading:
    def test_load_minimal_yaml(self, tmp_path):
        yml = tmp_path / "cfg.yaml"
        yml.write_text(
            """
parser:
  backend: mineru
  backends:
    mineru:
      device: cpu
storage:
  mode: local
  local:
    root: /tmp/hi
""",
            encoding="utf-8",
        )
        cfg = load_config(yml)
        assert cfg.parser.backend == "mineru"
        assert cfg.parser.backends.mineru.device == "cpu"
        assert cfg.storage.local.root == "/tmp/hi"

    def test_load_invalid_parser_backend_raises(self, tmp_path):
        yml = tmp_path / "cfg.yaml"
        yml.write_text(
            """
parser:
  backend: not-a-real-backend
""",
            encoding="utf-8",
        )
        with pytest.raises(Exception):  # pydantic ValidationError
            load_config(yml)


class TestStorageValidation:
    def test_s3_mode_without_section_raises(self):
        with pytest.raises(ValueError):
            StorageModel(mode="s3", s3=None)

    def test_oss_mode_without_section_raises(self):
        with pytest.raises(ValueError):
            StorageModel(mode="oss", oss=None)

    def test_local_mode_autofills_section(self):
        m = StorageModel(mode="local", local=None)
        assert m.local is not None

    def test_to_dataclass_local(self):
        m = StorageModel(mode="local")
        dc = m.to_dataclass()
        assert dc.mode == "local"
        assert dc.local is not None
        assert dc.s3 is None
