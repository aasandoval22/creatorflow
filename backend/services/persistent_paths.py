"""Canonical, symlink-resistant paths for CreatorFlow's persistent data."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PERSISTENT_DATA_ROOT = (PROJECT_ROOT / "data").resolve()
DEFAULT_LEGACY_DATA_ROOTS = (Path.home() / "clip-factory" / "data",)


class PersistentPathError(ValueError):
    """A stored path is ambiguous or escapes managed persistent storage."""


def infer_data_root(record_path: Path, container: str) -> Path:
    """Infer an injected test root while preserving the production data layout."""

    path = Path(record_path)
    lexical = _lexical_absolute(path)
    try:
        lexical.relative_to(Path("/tmp"))
    except ValueError:
        pass
    else:
        # Tests and explicitly injected scratch stores historically share /tmp.
        # Production defaults never enter this branch.
        return Path("/tmp")
    if path.parent.name == container:
        return path.parent.parent.resolve()
    return path.parent.resolve()


def _lexical_absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _relative(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PersistentPathError("Stored path must be a nonempty string.")
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute():
        raise PersistentPathError("Stored canonical path must be relative.")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise PersistentPathError("Stored path contains traversal or empty segments.")
    return Path(*pure.parts)


@dataclass(frozen=True)
class PathClassification:
    category: str
    relative_path: str | None
    reason: str


class PersistentPathResolver:
    """Store relative paths and safely materialize legacy active records."""

    def __init__(
        self, data_root: Path = DEFAULT_PERSISTENT_DATA_ROOT, *,
        legacy_roots: Iterable[Path] = DEFAULT_LEGACY_DATA_ROOTS,
        production_root: Path | None = None,
        owner_uid: int | None = None,
    ) -> None:
        self.data_root = _lexical_absolute(data_root).resolve()
        self.legacy_roots = tuple(_lexical_absolute(root) for root in legacy_roots)
        self.production_root = _lexical_absolute(
            production_root or Path.home() / "clip-factory-production"
        )
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid

    def classify(self, value: str) -> PathClassification:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            return PathClassification("unsafe_or_unrecognized", None, "malformed path")
        candidate = _lexical_absolute(value) if Path(value).is_absolute() else None
        if candidate is None:
            try:
                relative = _relative(value)
            except PersistentPathError as error:
                return PathClassification("unsafe_or_unrecognized", None, str(error))
            return PathClassification(
                "canonical_persistent_relative", relative.as_posix(),
                "canonical persistent-root-relative path",
            )
        try:
            relative = candidate.relative_to(self.data_root)
        except ValueError:
            pass
        else:
            return PathClassification(
                "legacy_persistent_absolute", relative.as_posix(),
                "absolute path under the canonical persistent root",
            )
        legacy_matches: list[tuple[Path, Path]] = []
        for root in self.legacy_roots:
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            legacy_matches.append((root, relative))
        suffixes = {relative.as_posix() for _, relative in legacy_matches}
        if len(suffixes) > 1:
            return PathClassification(
                "unsafe_or_unrecognized", None,
                "legacy path matches multiple historical roots with different targets",
            )
        if legacy_matches:
            root, relative = legacy_matches[0]
            return PathClassification(
                "legacy_development_absolute", relative.as_posix(),
                f"recognized historical data root {root}",
            )
        try:
            production_relative = candidate.relative_to(self.production_root)
        except ValueError:
            pass
        else:
            parts = production_relative.parts
            suffix: Path | None = None
            if len(parts) >= 3 and parts[:2] == ("current", "data"):
                suffix = Path(*parts[2:])
            elif (
                len(parts) >= 4 and parts[0] == "releases"
                and re.fullmatch(r"[0-9a-f]{40}", parts[1])
                and parts[2] == "data"
            ):
                suffix = Path(*parts[3:])
            if suffix is None:
                return PathClassification(
                    "unsafe_or_unrecognized", None,
                    "production path is not a recognized release data path",
                )
            return PathClassification(
                "production_release_absolute", suffix.as_posix(),
                "strict production-release data path requires identity-verified migration",
            )
        return PathClassification(
            "unsafe_or_unrecognized", None,
            "absolute path is outside recognized managed roots",
        )

    def store(self, value: Path | str) -> str:
        """Return the canonical representation for a newly written active field."""

        raw = os.fspath(value)
        classification = self.classify(raw)
        if classification.relative_path is None:
            raise PersistentPathError(classification.reason)
        return _relative(classification.relative_path).as_posix()

    def resolve(
        self, value: Path | str, *, allow_legacy: bool = True,
        must_exist: bool = False, regular: bool = False,
    ) -> Path:
        """Resolve a stored path by joining a validated suffix to the data root."""

        classification = self.classify(os.fspath(value))
        allowed = {"canonical_persistent_relative"}
        if allow_legacy:
            allowed |= {
                "legacy_persistent_absolute", "legacy_development_absolute",
                "production_release_absolute",
            }
        if classification.category not in allowed or classification.relative_path is None:
            raise PersistentPathError(classification.reason)
        relative = _relative(classification.relative_path)
        target = self.data_root / relative
        self._validate_target(target, must_exist=must_exist, regular=regular)
        return target

    def materialize(self, value: Path | str, *, allow_legacy: bool = True) -> Path:
        """Return the canonical lexical target for compatibility and diagnostics.

        This does not authorize opening the target. Consumers that access a file
        must call :meth:`resolve` with the appropriate safety requirements.
        """

        classification = self.classify(os.fspath(value))
        allowed = {"canonical_persistent_relative"}
        if allow_legacy:
            allowed |= {
                "legacy_persistent_absolute", "legacy_development_absolute",
                "production_release_absolute",
            }
        if classification.category not in allowed or classification.relative_path is None:
            raise PersistentPathError(classification.reason)
        return self.data_root / _relative(classification.relative_path)

    def validate_migration_target(
        self, legacy_value: str, *, checksum: str | None = None,
    ) -> tuple[Path, os.stat_result]:
        """Verify lexical authorization and legacy/canonical file identity."""

        classification = self.classify(legacy_value)
        if classification.category not in {
            "legacy_development_absolute", "legacy_persistent_absolute",
            "production_release_absolute",
        } or classification.relative_path is None:
            raise PersistentPathError(classification.reason)
        target = self.resolve(legacy_value, must_exist=True, regular=True)
        legacy = _lexical_absolute(legacy_value)
        try:
            legacy_info = legacy.stat()
            target_info = target.stat()
        except OSError as error:
            raise PersistentPathError(f"Cannot inspect migration identity: {error}.") from error
        if (legacy_info.st_dev, legacy_info.st_ino) != (
            target_info.st_dev, target_info.st_ino
        ):
            raise PersistentPathError(
                "Legacy path and canonical target do not identify the same file."
            )
        if checksum is not None:
            from backend.services.publication import sha256_file

            if sha256_file(target) != checksum:
                raise PersistentPathError("Canonical target checksum does not match.")
        return target, target_info

    def _validate_target(
        self, target: Path, *, must_exist: bool, regular: bool,
    ) -> None:
        try:
            relative = target.relative_to(self.data_root)
        except ValueError as error:
            raise PersistentPathError("Stored path escapes the persistent root.") from error
        current = self.data_root
        if self.data_root.is_symlink():
            raise PersistentPathError("Canonical persistent root must not be a symlink.")
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise PersistentPathError("Stored path traverses a symbolic link.")
        if not target.exists():
            if must_exist:
                raise PersistentPathError("Canonical target does not exist.")
            return
        info = target.lstat()
        if regular and not stat.S_ISREG(info.st_mode):
            raise PersistentPathError("Canonical target is not a regular file.")
        if info.st_uid != self.owner_uid:
            raise PersistentPathError("Canonical target ownership is unexpected.")
