"""Dropbox для Пепе: большие файлы из Telegram и записи встреч (/ecoCRM/records/inbox).

Ключи — только в переменных Railway (DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN);
без них Dropbox выключен, и бот прямо пишет в Буфер, что файл не сохранён.
"""
from __future__ import annotations

import json
import time

import httpx

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"


class DropboxStore:
    def __init__(self, app_key: str, app_secret: str, refresh_token: str) -> None:
        self._auth = (app_key, app_secret)
        self._refresh = refresh_token
        self._token = ""
        self._expires = 0.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))

    async def _bearer(self) -> dict:
        if not self._token or time.time() > self._expires - 60:
            resp = await self._client.post(TOKEN_URL, auth=self._auth,
                                           data={"grant_type": "refresh_token", "refresh_token": self._refresh})
            resp.raise_for_status()
            data = resp.json()
            self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 14400))
        return {"Authorization": f"Bearer {self._token}"}

    async def _rpc(self, endpoint: str, args: dict) -> dict:
        resp = await self._client.post(f"{API}/{endpoint}", headers=await self._bearer(), json=args)
        resp.raise_for_status()
        return resp.json()

    async def upload(self, path: str, content: bytes) -> str:
        """Файл до 150 МБ одним запросом; при совпадении имени Dropbox добавит номер. Возвращает путь."""
        headers = dict(await self._bearer(), **{
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add", "autorename": True}, ensure_ascii=True),
        })
        resp = await self._client.post(f"{CONTENT}/files/upload", headers=headers, content=content)
        resp.raise_for_status()
        return resp.json().get("path_display", path)

    async def download(self, path: str) -> bytes:
        headers = dict(await self._bearer(), **{"Dropbox-API-Arg": json.dumps({"path": path}, ensure_ascii=True)})
        resp = await self._client.post(f"{CONTENT}/files/download", headers=headers)
        resp.raise_for_status()
        return resp.content

    async def list_files(self, path: str) -> list[dict]:
        """Файлы папки (без подпапок); папки нет — пустой список."""
        try:
            data = await self._rpc("files/list_folder", {"path": path})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return []
            raise
        entries = list(data.get("entries", []))
        while data.get("has_more"):
            data = await self._rpc("files/list_folder/continue", {"cursor": data["cursor"]})
            entries += data.get("entries", [])
        return [e for e in entries if e.get(".tag") == "file"]

    async def move(self, src: str, dst: str) -> str:
        data = await self._rpc("files/move_v2", {"from_path": src, "to_path": dst, "autorename": True})
        return data.get("metadata", {}).get("path_display", dst)

    async def aclose(self) -> None:
        await self._client.aclose()
