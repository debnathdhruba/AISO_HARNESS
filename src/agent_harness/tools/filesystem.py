"""Bounded local filesystem operations that accept paths outside the project tree."""

import fnmatch
import json
import os
import stat
from pathlib import Path

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.tooling import Invocation


class FileSystemService:
    def __init__(
        self,
        max_file_bytes: int = 131_072,
        max_search_files: int = 500,
        max_search_bytes: int = 2_097_152,
    ) -> None:
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
    def _display(path: Path) -> str:
        text = str(path)
        if os.name == "nt" and text.startswith("\\\\?\\UNC\\"):
            return "\\\\" + text[8:]
        if os.name == "nt" and text.startswith("\\\\?\\"):
            return text[4:]
        return text

    @classmethod
    def _path(cls, value: str) -> Path:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        require(bool(value) and "\x00" not in value, "File path must be non-empty")
        try:
            return cls._native(Path(value).expanduser().absolute())
        except (OSError, RuntimeError):
            raise HarnessError(ErrorCode.STORAGE, "Cannot resolve file path") from None

    def _read_text(self, target: Path, limit: int) -> str:
        try:
            info = target.stat()
            require(stat.S_ISREG(info.st_mode), "Path is not a regular file")
            require(info.st_size <= limit, "File exceeds the configured size limit")
            with target.open("rb") as stream:
                data = stream.read(limit + 1)
            require(len(data) <= limit, "File exceeds the configured size limit")
            return data.decode("utf-8")
        except (OSError, UnicodeError):
            raise HarnessError(ErrorCode.STORAGE, "Cannot read UTF-8 file") from None

    async def read(self, invocation: Invocation, path: str) -> JsonObject:
        target = self._path(path)
        content = self._read_text(target, self._max_file_bytes)
        return JsonObject(json.dumps({"path": self._display(target), "content": content}))

    def read_range(self, path: str, start_line: int, line_count: int) -> JsonObject:
        require(start_line >= 1, "File range start line must be positive")
        require(1 <= line_count <= 100, "File range line count must be 1..100")
        target = self._path(path)
        try:
            info = target.stat()
            require(stat.S_ISREG(info.st_mode), "Path is not a regular file")
            lines: list[dict[str, str | int]] = []
            scanned_bytes = 0
            content_bytes = 0
            line_number = 0
            truncated = False
            with target.open("rb") as stream:
                while line_number < start_line + line_count - 1:
                    raw = stream.readline(65_537)
                    if not raw:
                        break
                    require(len(raw) <= 65_536, "File line exceeds the range-read limit")
                    scanned_bytes += len(raw)
                    require(scanned_bytes <= 2_097_152, "File range scan exceeds the 2 MiB limit")
                    line_number += 1
                    if line_number < start_line:
                        continue
                    text = raw.decode("utf-8").rstrip("\r\n")
                    encoded = text.encode("utf-8")
                    remaining = 12_000 - content_bytes
                    if len(encoded) > remaining:
                        text = encoded[:remaining].decode("utf-8", errors="ignore")
                        truncated = True
                    lines.append({"line": line_number, "text": text})
                    content_bytes += len(text.encode("utf-8"))
                    if truncated or content_bytes >= 12_000:
                        break
        except (OSError, UnicodeError):
            raise HarnessError(ErrorCode.STORAGE, "Cannot read UTF-8 file range") from None
        return JsonObject(
            json.dumps(
                {
                    "path": self._display(target),
                    "start_line": start_line,
                    "end_line": lines[-1]["line"] if lines else None,
                    "lines": lines,
                    "truncated": truncated,
                    "scanned_bytes": scanned_bytes,
                }
            )
        )

    def _write_text(self, invocation: Invocation, path: str, content: str) -> tuple[Path, int]:
        data = content.encode("utf-8")
        require(len(data) <= self._max_file_bytes, "File content exceeds the configured size limit")
        target = self._path(path)
        temporary = target.with_name(f".{target.name}.{invocation.invocation_id}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                require(stat.S_ISREG(target.stat().st_mode), "Path is not a regular file")
            if temporary.exists():
                require(
                    temporary.is_file() and temporary.read_bytes() == data,
                    "Interrupted file write requires reconciliation",
                )
            else:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot write file") from None
        return target, len(data)

    async def write(self, invocation: Invocation, path: str, content: str) -> JsonObject:
        target, size = self._write_text(invocation, path, content)
        return JsonObject(json.dumps({"path": self._display(target), "bytes_written": size}))

    async def edit(
        self, invocation: Invocation, path: str, old_text: str, new_text: str
    ) -> JsonObject:
        require(bool(old_text), "Edit match text cannot be empty")
        target = self._path(path)
        original = self._read_text(target, self._max_file_bytes)
        require(original.count(old_text) == 1, "Edit text must match exactly once")
        updated = original.replace(old_text, new_text, 1)
        size = len(updated.encode("utf-8"))
        require(size <= self._max_file_bytes, "Edited file exceeds the configured size limit")
        _, written = self._write_text(invocation, str(target), updated)
        return JsonObject(
            json.dumps(
                {"path": self._display(target), "replacements": 1, "bytes_written": written}
            )
        )

    async def search(self, query: str, pattern: str, directory: str = ".") -> JsonObject:
        require(bool(query) and len(query) <= 512, "Search query must contain 1..512 characters")
        require(bool(pattern) and len(pattern) <= 200, "Search pattern is invalid")
        root = self._path(directory)
        if not root.exists():
            return JsonObject(
                '{"matches":[],"scanned_files":0,"skipped_non_utf8":0,"truncated":false}'
            )
        require(root.is_dir(), "Search path is not a directory")

        matches: list[dict[str, str | int]] = []
        scanned_files = scanned_bytes = skipped_non_utf8 = 0
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
            raise HarnessError(ErrorCode.STORAGE, "Cannot search files") from None

        for base, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
            directories.sort()
            for filename in sorted(filenames):
                candidate = Path(base) / filename
                relative = candidate.relative_to(root).as_posix()
                if not fnmatch.fnmatchcase(relative, pattern):
                    continue
                try:
                    info = candidate.stat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if (
                        scanned_files >= self._max_search_files
                        or scanned_bytes + info.st_size > self._max_search_bytes
                    ):
                        truncated = True
                        break
                    with candidate.open("rb") as stream:
                        data = stream.read(self._max_search_bytes - scanned_bytes + 1)
                except OSError:
                    raise HarnessError(ErrorCode.STORAGE, "Cannot search file") from None
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
                                "path": self._display(candidate),
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
                if truncated:
                    break
            if truncated:
                break
        return result_payload()

    def list_files(self, directory: str, pattern: str = "*", limit: int = 100) -> JsonObject:
        require(bool(pattern) and len(pattern) <= 200, "Invalid file listing pattern")
        require(1 <= limit <= 200, "File listing limit must be 1..200")
        target = self._path(directory or ".")
        require(target.is_dir(), "Listing path is not a directory")
        entries: list[dict[str, str | int]] = []
        scanned = 0
        truncated = False
        try:
            with os.scandir(target) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > 2000:
                        truncated = True
                        break
                    if not fnmatch.fnmatchcase(entry.name, pattern):
                        continue
                    is_directory = entry.is_dir(follow_symlinks=True)
                    info = entry.stat(follow_symlinks=True)
                    if not is_directory and not stat.S_ISREG(info.st_mode):
                        continue
                    if len(entries) >= limit:
                        truncated = True
                        break
                    entries.append(
                        {
                            "name": entry.name,
                            "type": "directory" if is_directory else "file",
                            "size_bytes": 0 if is_directory else info.st_size,
                        }
                    )
        except OSError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot list directory") from None
        entries.sort(key=lambda item: str(item["name"]).casefold())
        payload: dict[str, object] = {
            "directory": self._display(target),
            "entries": entries,
            "scanned_entries": min(scanned, 2000),
            "truncated": truncated,
        }
        while len(json.dumps(payload).encode("utf-8")) > 30_000 and entries:
            entries.pop()
            payload["truncated"] = True
        return JsonObject(json.dumps(payload))
