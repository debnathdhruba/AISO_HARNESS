"""Bounded text artifacts, with explicit staging across the filesystem/SQLite gap."""

import json
import os
import re
import stat
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid5

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.tooling import ArtifactRecord, Invocation
from agent_harness.ports.state import StateStore


class ArtifactService:
    def __init__(self, store: StateStore, root: Path, max_bytes: int = 4096) -> None:
        self._store = store
        self._root = root.absolute()
        self._max_bytes = max_bytes

    def _path(self, relative: str) -> Path:
        candidate = self._root / relative
        require(
            not Path(relative).is_absolute() and ".." not in Path(relative).parts,
            "Artifact path escapes workspace",
        )
        # Check before resolving, since resolve alone would hide a symlinked workspace.
        for path in (candidate, *candidate.parents):
            path = self._native(path)
            if path.exists() or path.is_symlink():
                info = path.lstat()
                require(
                    not path.is_symlink()
                    and not (
                        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    ),
                    "Artifact path contains a symlink or reparse point",
                )
        require(
            candidate.resolve().is_relative_to(self._root.resolve()), "Artifact path escapes root"
        )
        return self._native(candidate)

    @staticmethod
    def _native(path: Path) -> Path:
        # Extended-length paths retain validation while avoiding Windows MAX_PATH failures.
        text = str(path)
        if os.name == "nt" and not text.startswith("\\\\?\\"):
            return Path("\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text)
        return path

    async def write(self, invocation: Invocation, name: str, content: str) -> JsonObject:
        require(
            bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name)) and ".." not in name,
            "Artifact name must be a simple logical filename",
        )
        data = content.encode("utf-8")
        require(len(data) <= self._max_bytes, "Artifact content exceeds size limit")
        artifact_id = str(uuid5(UUID(invocation.invocation_id), "artifact"))
        partition = sha256(invocation.user_id.encode()).hexdigest()
        relative = f"{partition}/{invocation.run_id}/{invocation.task_id}/{artifact_id}.txt"
        target = self._path(relative)
        staged = self._path(relative + ".stage")
        async with self._store.transaction(invocation.user_id) as tx:
            records = await tx.artifacts(invocation.run_id)
            existing = next((a for a in records if a.artifact_id == artifact_id), None)
            version = 1 + max(
                (a.version for a in records if a.task_id == invocation.task_id and a.name == name),
                default=0,
            )
            record = ArtifactRecord(
                artifact_id,
                invocation.run_id,
                invocation.task_id,
                invocation.user_id,
                invocation.invocation_id,
                name,
                relative,
                version,
                sha256(data).hexdigest(),
                len(data),
            )
            if existing is None:
                await tx.save_artifact(record, None)
            else:
                require(
                    existing.checksum == record.checksum and existing.name == name,
                    "Idempotency key reused with different artifact content",
                )
                record = existing
        if record.status == "committed":
            await self.read(invocation, record.artifact_id)
            return self.reference(record)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._path(relative)
            if target.exists():
                require(
                    target.stat().st_size == record.size
                    and sha256(target.read_bytes()).hexdigest() == record.checksum,
                    "Staged artifact reconciliation failed",
                )
            else:
                if staged.exists():
                    require(
                        staged.stat().st_size == record.size
                        and sha256(staged.read_bytes()).hexdigest() == record.checksum,
                        "Incomplete staging file requires reconciliation",
                    )
                else:
                    with staged.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                self._path(relative)
                self._path(relative + ".stage")
                os.replace(staged, target)
            async with self._store.transaction(invocation.user_id) as tx:
                await tx.save_artifact(replace(record, status="committed"), "staged")
        except OSError:
            raise HarnessError(
                ErrorCode.UNCERTAIN_SIDE_EFFECT, "Artifact write requires reconciliation"
            ) from None
        return self.reference(replace(record, status="committed"))

    @staticmethod
    def reference(record: ArtifactRecord) -> JsonObject:
        return JsonObject(
            json.dumps(
                {
                    "artifact_id": record.artifact_id,
                    "name": record.name,
                    "version": record.version,
                    "checksum": record.checksum,
                    "size": record.size,
                }
            )
        )

    async def read(self, invocation: Invocation, artifact_id: str) -> JsonObject:
        async with self._store.transaction(invocation.user_id) as tx:
            record = next(
                (
                    a
                    for a in await tx.artifacts(invocation.run_id)
                    if a.artifact_id == artifact_id
                    and a.task_id == invocation.task_id
                    and a.status == "committed"
                ),
                None,
            )
        if record is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Artifact unavailable in this task scope")
        path = self._path(record.relative_path)
        try:
            with path.open("rb") as stream:
                data = stream.read(self._max_bytes + 1)
            require(
                len(data) <= self._max_bytes
                and len(data) == record.size
                and sha256(data).hexdigest() == record.checksum,
                "Artifact integrity check failed",
            )
            return JsonObject(
                json.dumps({"artifact_id": record.artifact_id, "content": data.decode("utf-8")})
            )
        except (OSError, UnicodeError):
            raise HarnessError(ErrorCode.STORAGE, "Cannot read artifact content") from None

    async def list(self, invocation: Invocation) -> JsonObject:
        async with self._store.transaction(invocation.user_id) as tx:
            records = await tx.artifacts(invocation.run_id)
        values = [
            json.loads(self.reference(record).text)
            for record in records
            if record.task_id == invocation.task_id and record.status == "committed"
        ]
        return JsonObject(json.dumps({"artifacts": values[:20], "truncated": len(values) > 20}))
