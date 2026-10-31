"""Bounded text-file operations confined to the configured workspace."""

import fnmatch
import json
import os
import stat
from pathlib import Path, PurePosixPath

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.tooling import Invocation

_IGNORED_DIRECTORIES = {".git", ".local", ".venv", "__pycache__", "node_modules", "venv"}


class WorkspaceFileService:
    def __init__(
        self,
        root: Path,
        artifact_root: Path,
        max_file_bytes: int = 16_384,
        max_search_files: int = 500,
        max_search_bytes: int = 2_097_152,
    ) -> None:
        self._root = root.absolute()
        self._artifact_root = artifact_root.absolute()
        self._max_file_bytes = max_file_bytes
        self._max_search_files = max_search_files
        self._max_search_bytes = max_search_bytes

    @staticmethod
    def _native(path: Path) -> Path:
        text = str(path)
        if os.name == "nt" and not text.startswith("\\\\?\\"):
            return Path("\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text)
        return path

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    def _check_chain(self, path: Path) -> None:
        for item in (path, *path.parents):
            native = self._native(item)
            if native.exists() or native.is_symlink():
                try:
                    require(
                        not self._is_reparse_point(native),
                        "Workspace paths cannot contain symlinks or reparse points",
                    )
                except OSError:
                    raise HarnessError(ErrorCode.STORAGE, "Cannot inspect workspace path") from None

    def _path(self, relative: str) -> Path:
        require(
            bool(relative)
            and "\\" not in relative
            and ":" not in relative
            and "\x00" not in relative,
            "File path must be a non-empty workspace-relative path",
        )
        part = PurePosixPath(relative)
        require(
            not part.is_absolute() and ".." not in part.parts,
            "File path escapes the configured workspace",
        )
        candidate = self._root.joinpath(*part.parts)
        self._check_chain(candidate)
        try:
            resolved = candidate.resolve()
            require(
                resolved.is_relative_to(self._root.resolve()),
                "File path escapes the configured workspace",
            )
            require(
                not resolved.is_relative_to((Path.cwd() / ".local").resolve()),
                "Harness runtime data is not accessible through file tools",
            )
            require(
                not self._is_artifact_path(candidate),
                "Harness artifact storage is not accessible through file tools",
            )
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot resolve workspace path") from None
        require(
            not part.parts or part.parts[0].casefold() != ".local",
            "Harness runtime data is not accessible through file tools",
        )
        return self._native(candidate)

    def _is_artifact_path(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self._artifact_root.resolve())
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot resolve artifact storage path") from None

    def _ensure_parent(self, target: Path) -> None:
        self._check_chain(self._root)
        current = self._native(self._root)
        try:
            current.mkdir(parents=True, exist_ok=True)
            for name in target.relative_to(self._native(self._root)).parts[:-1]:
                current = current / name
                self._check_chain(current)
                current.mkdir(exist_ok=True)
                require(current.is_dir(), "Workspace parent path is not a directory")
                self._check_chain(current)
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot create workspace directory") from None

    def _read_text(self, target: Path, limit: int) -> str:
        try:
            info = target.lstat()
            require(stat.S_ISREG(info.st_mode), "Workspace path is not a regular file")
            require(info.st_size <= limit, "File exceeds the configured size limit")
            with target.open("rb") as stream:
                data = stream.read(limit + 1)
            require(len(data) <= limit, "File exceeds the configured size limit")
            return data.decode("utf-8")
        except (OSError, UnicodeError):
            raise HarnessError(ErrorCode.STORAGE, "Cannot read UTF-8 workspace file") from None

    def _write_text(self, invocation: Invocation, relative: str, content: str) -> int:
        data = content.encode("utf-8")
        require(len(data) <= self._max_file_bytes, "File content exceeds the configured size limit")
        target = self._path(relative)
        self._ensure_parent(target)
        self._path(relative)
        temporary = target.with_name(f".{target.name}.{invocation.invocation_id}.tmp")
        try:
            self._check_chain(temporary)
            if target.exists():
                require(
                    stat.S_ISREG(target.lstat().st_mode),
                    "Workspace path is not a regular file",
                )
            if temporary.exists():
                require(
                    temporary.is_file() and temporary.read_bytes() == data,
                    "Interrupted workspace write requires reconciliation",
                )
            else:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._path(relative)
            self._check_chain(temporary)
            os.replace(temporary, target)
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot write workspace file") from None
        return len(data)

    async def read(self, invocation: Invocation, relative: str) -> JsonObject:
        target = self._path(relative)
        content = self._read_text(target, self._max_file_bytes)
        return JsonObject(json.dumps({"path": relative, "content": content}))

    async def write(self, invocation: Invocation, relative: str, content: str) -> JsonObject:
        size = self._write_text(invocation, relative, content)
        return JsonObject(json.dumps({"path": relative, "bytes_written": size}))

    async def edit(
        self, invocation: Invocation, relative: str, old_text: str, new_text: str
    ) -> JsonObject:
        require(bool(old_text), "Edit match text cannot be empty")
        target = self._path(relative)
        original = self._read_text(target, self._max_file_bytes)
        matches = original.count(old_text)
        require(matches == 1, "Edit text must match exactly once")
        updated = original.replace(old_text, new_text, 1)
        size = len(updated.encode("utf-8"))
        require(size <= self._max_file_bytes, "Edited file exceeds the configured size limit")
        self._write_text(invocation, relative, updated)
        return JsonObject(json.dumps({"path": relative, "replacements": 1, "bytes_written": size}))

    async def search(self, query: str, pattern: str) -> JsonObject:
        require(bool(query) and len(query) <= 512, "Search query must contain 1..512 characters")
        require(len(pattern) <= 200, "Search pattern is too long")
        root = self._path(".")
        if not root.exists():
            return JsonObject(
                '{"matches":[],"scanned_files":0,"skipped_non_utf8":0,"truncated":false}'
            )
        require(root.is_dir(), "Workspace root is not a directory")

        matches: list[dict[str, str | int]] = []
        scanned_files = scanned_bytes = 0
        skipped_non_utf8 = 0
        truncated = False

        def result_payload() -> JsonObject:
            return JsonObject(
                json.dumps(
                    {
                        "matches": matches,
                        "scanned_files": scanned_files,
                        "skipped_non_utf8": skipped_non_utf8,
                        "truncated": truncated,
                    }
                )
            )

        def walk_error(_: OSError) -> None:
            raise HarnessError(ErrorCode.STORAGE, "Cannot search workspace files") from None

        for base, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
            base_path = Path(base)
            directories[:] = [
                name
                for name in sorted(directories)
                if name.casefold() not in _IGNORED_DIRECTORIES
                and not self._is_link_or_reparse(base_path / name)
                and not self._is_artifact_path(base_path / name)
            ]
            for filename in sorted(filenames):
                candidate = base_path / filename
                if self._is_link_or_reparse(candidate):
                    continue
                relative = candidate.relative_to(root).as_posix()
                if not fnmatch.fnmatchcase(relative, pattern):
                    continue
                self._path(relative)
                try:
                    info = candidate.lstat()
                except OSError:
                    raise HarnessError(ErrorCode.STORAGE, "Cannot inspect workspace file") from None
                if not stat.S_ISREG(info.st_mode):
                    continue
                if (
                    scanned_files >= self._max_search_files
                    or scanned_bytes + info.st_size > self._max_search_bytes
                ):
                    truncated = True
                    break
                try:
                    with candidate.open("rb") as stream:
                        data = stream.read(self._max_search_bytes - scanned_bytes + 1)
                except OSError:
                    raise HarnessError(ErrorCode.STORAGE, "Cannot search workspace file") from None
                if len(data) > self._max_search_bytes - scanned_bytes:
                    truncated = True
                    break
                scanned_files += 1
                scanned_bytes += len(data)
                try:
                    content = data.decode("utf-8")
                except UnicodeError:
                    skipped_non_utf8 += 1
                    continue
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if query in line:
                        matches.append(
                            {
                                "path": relative,
                                "line": line_number,
                                "text": line[:300],
                            }
                        )
                        if len(result_payload().text.encode("utf-8")) > 30_000:
                            matches.pop()
                            truncated = True
                            break
                        if len(matches) >= 20:
                            truncated = True
                            break
                if len(matches) >= 20:
                    break
            if truncated:
                break
        return result_payload()

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            return WorkspaceFileService._is_reparse_point(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot inspect workspace entry") from None
