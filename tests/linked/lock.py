"""Drive-native cross-session test lock.

Drive has no atomic create-exclusive, so acquisition is: list for the
lock file; if a non-stale lock exists, back off and retry; else create
one, re-list, and confirm we own the earliest-created instance.
"""

import time
from datetime import datetime, timezone

from bora.changes import parse_drive_time
from bora.client import GoogleDriveClient

_STALE_SECONDS = 600
_RETRY_SECONDS = 5
_MAX_WAIT_SECONDS = 300


class DriveTestLock:
    def __init__(self, client: GoogleDriveClient, lock_name: str):
        self._client = client
        self._name = lock_name
        self._lock_id: str | None = None

    def acquire(self) -> bool:
        deadline = _MAX_WAIT_SECONDS
        while deadline > 0:
            existing = self._find_locks()
            self._purge_stale(existing)
            fresh = [r for r in existing if not self._is_stale(r)]
            if not fresh:
                created_id = self._create_lock()
                contenders = sorted(
                    (r for r in self._find_locks() if not self._is_stale(r)),
                    key=lambda r: (r["createdTime"], r["id"]),
                )
                if contenders and contenders[0]["id"] == created_id:
                    self._lock_id = created_id
                    return True
                self._delete(created_id)
            time.sleep(_RETRY_SECONDS)
            deadline -= _RETRY_SECONDS
        return False

    def release(self) -> None:
        if self._lock_id:
            self._delete(self._lock_id)
            self._lock_id = None

    def _find_locks(self) -> list[dict]:
        svc = self._client._get_service()
        resp = (
            svc.files()
            .list(
                q=f"name = '{self._name}' and trashed = false",
                fields="files(id, name, createdTime)",
                pageSize=100,
            )
            .execute()
        )
        return resp.get("files", [])

    def _create_lock(self) -> str:
        svc = self._client._get_service()
        resp = (
            svc.files()
            .create(body={"name": self._name}, fields="id")
            .execute()
        )
        return resp["id"]

    def _delete(self, file_id: str) -> None:
        svc = self._client._get_service()
        svc.files().delete(fileId=file_id).execute()

    def _is_stale(self, rec: dict) -> bool:
        age = (
            datetime.now(timezone.utc) - parse_drive_time(rec["createdTime"])
        ).total_seconds()
        return age > _STALE_SECONDS

    def _purge_stale(self, recs: list[dict]) -> None:
        for r in recs:
            if self._is_stale(r):
                try:
                    self._delete(r["id"])
                except Exception:
                    pass
